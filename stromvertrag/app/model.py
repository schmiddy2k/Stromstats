"""Contract validation and accounting. Dates are inclusive in the UI."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


def money(value, name: str) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}: ungültige Zahl") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name}: muss mindestens 0 sein")
    return result


def parse_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: ungültiges Datum") from None


def validate_contract(raw: dict) -> dict:
    required = ("provider", "tariff", "start", "end", "base_monthly", "price_kwh", "contract_kwh")
    for key in required:
        if raw.get(key) is None or str(raw[key]).strip() == "":
            raise ValueError(f"{key}: Pflichtfeld")
    start, end = parse_date(raw["start"], "Beginn"), parse_date(raw["end"], "Ende")
    if end < start:
        raise ValueError("Vertragsende liegt vor Vertragsbeginn")
    entity = str(raw.get("entity_id") or "").strip()
    if entity and (not entity.startswith("sensor.") or not all(c.islower() or c.isdigit() or c in "._" for c in entity)):
        raise ValueError("Bitte einen gültigen kWh-Sensor wählen")
    result = {key: str(raw[key]).strip() for key in ("provider", "tariff")}
    result.update(start=start.isoformat(), end=end.isoformat(), entity_id=entity)
    for key in ("base_monthly", "price_kwh", "contract_kwh", "bonus1", "bonus2", "bonus3"):
        result[key] = str(money(raw.get(key, 0), key))
    return result


def months_cost(start: date, end_exclusive: date, monthly: Decimal) -> Decimal:
    """Prorate each touched calendar month by its actual number of days."""
    total = Decimal(0)
    cursor = start
    while cursor < end_exclusive:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        boundary = min(next_month, end_exclusive)
        total += monthly * Decimal((boundary - cursor).days) / Decimal((next_month - date(cursor.year, cursor.month, 1)).days)
        cursor = boundary
    return total


def sections(contract: dict, today: date | None = None) -> list[tuple[date, date]]:
    start, end = date.fromisoformat(contract["start"]), date.fromisoformat(contract["end"]) + timedelta(days=1)
    end = min(end, (today or date.today()) + timedelta(days=1))
    if end <= start:
        return []
    result = []
    while start < end:
        stop = min(end, date(start.year + 1, 1, 1))
        result.append((start, stop))
        start = stop
    return result


def calculate(contract: dict, start: date, end: date, kwh: Decimal | None, status: str) -> dict:
    base = months_cost(start, end, Decimal(contract["base_monthly"]))
    energy = kwh * Decimal(contract["price_kwh"]) if kwh is not None else None
    contract_days = (date.fromisoformat(contract["end"]) - date.fromisoformat(contract["start"])).days + 1
    share = Decimal((end - start).days) / Decimal(contract_days)
    bonuses = sum((Decimal(contract[f"bonus{i}"]) for i in (1, 2, 3)), Decimal(0)) * share
    gross = base + energy if energy is not None else None
    net = gross - bonuses if gross is not None else None
    return {
        "year": start.year, "from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
        "kwh": float(kwh) if kwh is not None else None,
        "base": float(base), "energy": float(energy) if energy is not None else None,
        "bonus": float(bonuses), "gross": float(gross) if gross is not None else None,
        "net": float(net) if net is not None else None, "status": status,
    }


def consumption(start: date, end: date, rows: list[dict], readings: dict[str, str]) -> tuple[Decimal | None, str]:
    """Boundary readings win when both exist; otherwise sum recorded changes.

    Do not silently turn missing data into zero. Statistics rows are hourly and
    only complete hours are included. The result is marked partial if they do
    not span the requested period.
    """
    left, right = readings.get(start.isoformat()), readings.get(end.isoformat())
    if left is not None and right is not None:
        delta = Decimal(right) - Decimal(left)
        if delta < 0:
            raise ValueError("Endzählerstand liegt vor dem Anfangszählerstand")
        return delta, "Zählerstände"
    from datetime import datetime, time
    selected = [r for r in rows if start <= datetime.fromisoformat(r["start"]).date() < end and r.get("change") is not None]
    if not selected:
        return None, "Keine Messdaten"
    total = sum((Decimal(str(r["change"])) for r in selected), Decimal(0))
    spans = sorted((datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])) for r in selected)
    first, last = spans[0][0], max(stop for _, stop in spans)
    begins = datetime.combine(start, time.min, first.tzinfo)
    ends = datetime.combine(end, time.min, last.tzinfo)
    covered_until = first
    gap = False
    for span_start, span_end in spans:
        if span_start > covered_until:
            gap = True
        covered_until = max(covered_until, span_end)
    return total, "Statistik" if first <= begins and last >= ends and not gap else "Statistik (unvollständig)"


def fill_from_contract_readings(
    contract: dict,
    periods: list[tuple[date, date]],
    values: list[tuple[Decimal | None, str]],
    readings: dict[str, str],
) -> list[tuple[Decimal | None, str]]:
    """Allocate an unmeasured remainder when only contract boundary readings exist.

    Complete yearly statistics or yearly boundary readings remain authoritative.
    Missing/partial years share the remaining contract kWh by calendar days and
    are explicitly marked as estimates.
    """
    if not periods or periods[-1][1] != date.fromisoformat(contract["end"]) + timedelta(days=1):
        return values  # No extrapolation for an ongoing contract.
    left = readings.get(contract["start"])
    right = readings.get(periods[-1][1].isoformat())
    if left is None or right is None:
        return values
    total = Decimal(right) - Decimal(left)
    if total < 0:
        return [(None, "Endzählerstand liegt vor dem Anfangszählerstand") for _ in values]
    unknown = [i for i, (_, status) in enumerate(values) if status not in ("Zählerstände", "Statistik")]
    if not unknown:
        return values
    known = sum((value for i, (value, _) in enumerate(values) if i not in unknown and value is not None), Decimal(0))
    remaining = total - known
    if remaining < 0:
        return [(None, "Ablesungen widersprechen der Statistik") if i in unknown else item for i, item in enumerate(values)]
    days = sum((periods[i][1] - periods[i][0]).days for i in unknown)
    output = list(values)
    for i in unknown:
        share = Decimal((periods[i][1] - periods[i][0]).days) / Decimal(days)
        output[i] = (remaining * share, "Aus Endständen geschätzt")
    return output
