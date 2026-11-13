"""Preview and explicitly apply historical work-price costs to HA's cost statistic."""
from __future__ import annotations

import hashlib
import json
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from energy import active_contract, grid_imports, websocket_command

MAX_HOURS = 24 * 366 * 3


def _rows(statistic_id: str, start: datetime, end: datetime, types: list[str], units: dict | None = None) -> list[dict]:
    command = {"type": "recorder/statistics_during_period", "start_time": start.isoformat(),
               "end_time": end.isoformat(), "statistic_ids": [statistic_id], "period": "hour", "types": types}
    if units:
        command["units"] = units
    result = websocket_command(command, timeout=60)
    return result.get(statistic_id, [])


def build_plan(contracts: list[dict], ha, today: date) -> dict:
    """Use completed local days only; require every corresponding meter hour."""
    config = ha("GET", "/config")
    if config.get("currency") != "EUR":
        raise ValueError("Historische Kosten benötigen die Home-Assistant-Währung EUR")
    tz = ZoneInfo(config.get("time_zone") or "Europe/Berlin")
    active = active_contract(contracts, today)
    if not active or not active.get("entity_id"):
        raise ValueError("Ein aktiver Vertrag mit Strommesssensor wird benötigt")
    meter = active["entity_id"]
    prefs = websocket_command({"type": "energy/get_prefs"})
    sources = [flow for flow in grid_imports(prefs.get("energy_sources", [])) if flow.get("stat_energy_from") == meter]
    if len(sources) != 1 or sources[0].get("entity_energy_price") != "sensor.stromvertrag_arbeitspreis" or sources[0].get("stat_cost"):
        raise ValueError("Der Strommesssensor muss im Energy Dashboard mit dem Vertragspreissensor verknüpft sein")
    info = websocket_command({"type": "energy/info"})
    cost_id = info.get("cost_sensors", {}).get(meter)
    if not cost_id:
        raise ValueError("Home Assistants Kostensensor ist noch nicht bereit")
    relevant = sorted((c for c in contracts if c.get("entity_id") == meter and c["start"] < today.isoformat()), key=lambda c: c["start"])
    if not relevant:
        raise ValueError("Keine vergangenen Vertragsstunden für diesen Strommesssensor")
    for previous, current in zip(relevant, relevant[1:]):
        if previous["end"] >= current["start"]:
            raise ValueError("Vertragszeiträume für den Strommesssensor überschneiden sich")
    start = datetime.combine(date.fromisoformat(relevant[0]["start"]), time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(today, time.min, tz).astimezone(timezone.utc)
    hours = int((end - start).total_seconds() // 3600)
    if hours < 1 or hours > MAX_HOURS:
        raise ValueError("Die Rückrechnung umfasst höchstens drei Jahre")
    meter_rows = _rows(meter, start, end, ["change"], {"energy": "kWh"})
    cost_rows = _rows(cost_id, start - timedelta(hours=1), datetime.now(timezone.utc), ["change", "sum", "state", "last_reset"])
    meter_by_hour = {int(row["start"] / 1000): row for row in meter_rows}
    cost_by_hour = {int(row["start"] / 1000): row for row in cost_rows}
    metadata_list = websocket_command({"type": "recorder/get_statistics_metadata", "statistic_ids": [cost_id]})
    metadata = metadata_list[0] if metadata_list else None
    if metadata and (metadata.get("source") != "recorder" or metadata.get("statistics_unit_of_measurement") != "EUR"):
        raise ValueError("Die Kostenstatistik hat eine unerwartete Quelle oder Währung")
    if not any(ts >= int(end.timestamp()) for ts in cost_by_hour):
        raise ValueError("Der Kostensensor benötigt zuerst eine abgeschlossene Stunde aktueller Statistik")
    future_time = min(ts for ts in cost_by_hour if ts >= int(end.timestamp()))
    future_sum = cost_by_hour[future_time].get("sum")
    if future_sum is None:
        raise ValueError("Die aktuelle Kostenstatistik enthält noch keine laufende Summe")
    baseline = cost_by_hour.get(int(start.timestamp()) - 3600, {}).get("sum") or 0
    old_sum = Decimal(str(baseline))
    delta = Decimal(0)
    expected = Decimal(0)
    previous = Decimal(0)
    missing = 0
    calculated = []
    contract_by_day = {}
    for contract in relevant:
        cursor = date.fromisoformat(contract["start"])
        last = min(date.fromisoformat(contract["end"]), today - timedelta(days=1))
        while cursor <= last:
            contract_by_day[cursor] = contract
            cursor += timedelta(days=1)
    for offset in range(hours):
        timestamp = int(start.timestamp()) + offset * 3600
        moment = datetime.fromtimestamp(timestamp, timezone.utc)
        contract = contract_by_day.get(moment.astimezone(tz).date())
        old = cost_by_hour.get(timestamp)
        meter_row = meter_by_hour.get(timestamp)
        if old and old.get("sum") is not None:
            old_sum = Decimal(str(old["sum"]))
        if contract:
            if not meter_row or meter_row.get("change") is None:
                missing += 1
                continue
            kwh = Decimal(str(meter_row["change"]))
            if not kwh.is_finite() or kwh < 0:
                missing += 1
                continue
            desired = kwh * Decimal(contract["price_kwh"])
            actual = Decimal(str(old.get("change") or 0)) if old else Decimal(0)
            if not desired.is_finite() or not actual.is_finite():
                missing += 1
                continue
            expected += desired
            previous += actual
            delta += desired - actual
        if contract or old:
            new_sum = old_sum + delta
            row = {"start": moment.isoformat(), "sum": float(new_sum),
                   "state": old.get("state") if old and old.get("state") is not None else float(new_sum)}
            if old and old.get("last_reset") is not None:
                row["last_reset"] = datetime.fromtimestamp(old["last_reset"] / 1000, timezone.utc).isoformat()
            calculated.append(row)
    if missing:
        raise ValueError(f"{missing} Vertragsstunden ohne vollständige Verbrauchsstatistik; Rückrechnung abgebrochen")
    if not calculated:
        raise ValueError("Keine berechenbaren historischen Stunden")
    target_metadata = {"mean_type": metadata.get("mean_type", 0) if metadata else 0,
                       "has_sum": True, "name": metadata.get("name") if metadata else None,
                       "source": "recorder", "statistic_id": cost_id,
                       "unit_class": metadata.get("unit_class") if metadata else None,
                       "unit_of_measurement": "EUR"}
    fingerprint = hashlib.sha256(json.dumps({"meter": meter, "cost": cost_id, "rows": calculated,
                                             "delta": str(delta), "contracts": relevant}, sort_keys=True).encode()).hexdigest()
    return {"token": fingerprint, "meter": meter, "cost_statistic_id": cost_id,
            "start": start.isoformat(), "end": end.isoformat(),
            "start_date": relevant[0]["start"], "end_date": today.isoformat(), "hours": len(calculated),
            "contract_count": len(relevant), "existing_cost": float(previous),
            "calculated_cost": float(expected), "adjustment": float(delta),
            "metadata": target_metadata, "rows": calculated,
            "future_time": datetime.fromtimestamp(future_time, timezone.utc).isoformat(),
            "future_sum_before": float(future_sum)}


def public_preview(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key not in ("metadata", "rows", "future_time", "future_sum_before")}


def apply_plan(plan: dict) -> None:
    """Queue ordered Recorder jobs; caller must enforce one-time token use."""
    rows = plan["rows"]
    for index in range(0, len(rows), 500):
        websocket_command({"type": "recorder/import_statistics", "metadata": plan["metadata"],
                           "stats": rows[index:index + 500]}, timeout=60)
    if plan["adjustment"]:
        websocket_command({"type": "recorder/adjust_sum_statistics", "statistic_id": plan["cost_statistic_id"],
                           "start_time": plan["end"], "adjustment": plan["adjustment"],
                           "adjustment_unit_of_measurement": "EUR"}, timeout=60)
    expected_last = plan["rows"][-1]["sum"]
    expected_future = plan["future_sum_before"] + plan["adjustment"]
    for _ in range(60):
        latest = _rows(plan["cost_statistic_id"], datetime.fromisoformat(plan["rows"][-1]["start"]),
                       datetime.fromisoformat(plan["future_time"]) + timedelta(hours=1), ["sum"])
        by_time = {int(row["start"] / 1000): row for row in latest}
        last_ts = int(datetime.fromisoformat(plan["rows"][-1]["start"]).timestamp())
        future_ts = int(datetime.fromisoformat(plan["future_time"]).timestamp())
        if (abs((by_time.get(last_ts, {}).get("sum") or 0) - expected_last) < 0.001
                and abs((by_time.get(future_ts, {}).get("sum") or 0) - expected_future) < 0.001):
            return
        clock.sleep(2)
    raise RuntimeError("Recorder hat die korrigierten Summen nicht innerhalb von zwei Minuten bestätigt")
