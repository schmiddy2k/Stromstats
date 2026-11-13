"""Ingress-only HTTP application backed by Home Assistant's recorder service."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from model import calculate, consumption, fill_from_contract_readings, parse_date, preview_contract, sections, validate_contract
import energy
import history

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB = DATA_DIR / "contracts.sqlite3"
LOCK = Lock()
ENERGY_WAKE = Event()
ENERGY_LOCK = Lock()
ENERGY_STATUS = {"message": "Energy-Zuordnung noch nicht geprüft", "entity_id": energy.PRICE_ENTITY}
STATIC = Path(__file__).with_name("index.html")
SCRIPT = Path(__file__).with_name("app.js")
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor")


def database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS contracts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS readings (contract_id INTEGER NOT NULL, day TEXT NOT NULL, kwh TEXT NOT NULL, PRIMARY KEY(contract_id, day))")
    conn.execute("CREATE TABLE IF NOT EXISTS history_jobs (token TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)")
    return conn


def ha(method: str, path: str, body: dict | None = None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("Home Assistant ist nicht verbunden (SUPERVISOR_TOKEN fehlt)")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = Request(SUPERVISOR + "/core/api" + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        raise RuntimeError(f"Home Assistant API: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Home Assistant nicht erreichbar: {exc.reason}") from exc


def contracts():
    with database() as conn:
        result = [dict(id=row["id"], **json.loads(row["body"])) for row in conn.execute("SELECT id, body FROM contracts")]
    return sorted(result, key=lambda contract: (contract["start"], contract["id"]), reverse=True)


def sync_energy_once():
    with ENERGY_LOCK:
        try:
            tz_name = ha("GET", "/config").get("time_zone") or "Europe/Berlin"
            message = energy.sync(contracts(), ha, datetime.now(ZoneInfo(tz_name)).date())
        except Exception as exc:
            message = f"Energy-Zuordnung fehlgeschlagen: {exc}"
            logging.warning("%s", message)
        ENERGY_STATUS.update(message=message, checked_at=datetime.now().isoformat(timespec="seconds"))
        return dict(ENERGY_STATUS)


def sync_energy_forever():
    """Refresh after edits, contract date changes, and Home Assistant restarts."""
    while True:
        sync_energy_once()
        ENERGY_WAKE.wait(60)
        ENERGY_WAKE.clear()


def run_history_job(plan: dict):
    try:
        history.apply_plan(plan)
        status, detail = "completed", "Historische Kostenstatistiken wurden an Home Assistant übergeben"
    except Exception as exc:
        status, detail = "failed", f"Korrekturlauf angehalten: {exc}. Vor einem neuen Versuch die Energy-Statistiken prüfen."
        logging.exception("Historische Energy-Kosten")
    with database() as conn:
        conn.execute("UPDATE history_jobs SET status=?, detail=? WHERE token=?", (status, detail, plan["token"]))


def latest_history_job() -> dict | None:
    with database() as conn:
        row = conn.execute("SELECT token, status, detail, created_at FROM history_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def readings(contract_id: int):
    with database() as conn:
        return {r["day"]: r["kwh"] for r in conn.execute("SELECT day, kwh FROM readings WHERE contract_id=?", (contract_id,))}


def boundary_updates(raw: dict, contract: dict) -> dict[str, str | None]:
    """Optional API fields; absent fields leave existing readings untouched."""
    updates = {}
    dates = {"start_reading": contract["start"], "end_reading": (date.fromisoformat(contract["end"]) + timedelta(days=1)).isoformat()}
    for field, day in dates.items():
        if field not in raw:
            continue
        value = str(raw[field]).strip().replace(",", ".") if raw[field] is not None else ""
        if value:
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise ValueError(f"{field}: ungültiger Zählerstand") from None
            if not number.is_finite() or number < 0:
                raise ValueError(f"{field}: Zählerstand muss mindestens 0 sein")
            updates[day] = str(number)
        else:
            updates[day] = None
    if updates.get(dates["start_reading"]) is not None and updates.get(dates["end_reading"]) is not None:
        if Decimal(updates[dates["end_reading"]]) < Decimal(updates[dates["start_reading"]]):
            raise ValueError("Endzählerstand liegt vor dem Anfangszählerstand")
    return updates


def reading_updates(raw: dict, contract: dict) -> dict[str, str | None]:
    """Optional interior readings submitted with a contract edit."""
    changes = raw.get("reading_changes", {})
    if not isinstance(changes, dict) or len(changes) > 200:
        raise ValueError("Ungültige Ablesungen")
    first = date.fromisoformat(contract["start"])
    last = date.fromisoformat(contract["end"]) + timedelta(days=1)
    updates = {}
    for day, raw_value in changes.items():
        parsed = parse_date(day, "Ablesedatum")
        if raw_value is not None and str(raw_value).strip() != "" and not first < parsed < last:
            raise ValueError("Ablesedatum muss innerhalb der Vertragslaufzeit liegen")
        if raw_value is None or str(raw_value).strip() == "":
            updates[day] = None
            continue
        try:
            value = Decimal(str(raw_value).replace(",", "."))
        except InvalidOperation:
            raise ValueError("Ungültiger Zählerstand") from None
        if not value.is_finite() or value < 0:
            raise ValueError("Zählerstand muss mindestens 0 sein")
        updates[day] = str(value)
    return updates


def save_boundaries(conn, contract_id: int, updates: dict[str, str | None]):
    for day, value in updates.items():
        if value is None:
            conn.execute("DELETE FROM readings WHERE contract_id=? AND day=?", (contract_id, day))
        else:
            conn.execute("INSERT OR REPLACE INTO readings VALUES(?,?,?)", (contract_id, day, value))


def report():
    tz_name = ha("GET", "/config").get("time_zone", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    output = []
    for contract in contracts():
        periods = sections(contract, datetime.now(tz).date())
        if not periods:
            output.append({**contract, "readings": readings(contract["id"]), "years": []})
            continue
        from datetime import time
        first, last = periods[0][0], periods[-1][1]
        payload = {"start_time": datetime.combine(first, time.min, tz).isoformat(), "end_time": datetime.combine(last, time.min, tz).isoformat(),
                   "statistic_ids": [contract["entity_id"]], "period": "hour", "types": ["change"], "units": {"energy": "kWh"}}
        rows, error = [], None
        if contract.get("entity_id"):
            try:
                result = ha("POST", "/services/recorder/get_statistics?return_response", payload)
                rows = result["service_response"]["statistics"].get(contract["entity_id"], [])
            except (RuntimeError, KeyError, TypeError) as exc:
                error = str(exc)
        # Recorder timestamps are UTC. Assign rows to local calendar years.
        for row in rows:
            row["start"] = datetime.fromisoformat(row["start"]).astimezone(tz).isoformat()
            row["end"] = datetime.fromisoformat(row["end"]).astimezone(tz).isoformat()
        manual = readings(contract["id"])
        measured = []
        for start, end in periods:
            try:
                kwh, status = consumption(start, end, rows, manual)
            except ValueError as exc:
                kwh, status = None, str(exc)
            if error and kwh is None:
                status = error
            measured.append((kwh, status))
        measured = fill_from_contract_readings(contract, periods, measured, manual)
        years = [calculate(contract, start, end, kwh, status) for (start, end), (kwh, status) in zip(periods, measured)]
        output.append({**contract, "readings": manual, "years": years})
    return output


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        return self.client_address[0] == "172.30.32.2" or (os.environ.get("LOCAL_DEV") == "1" and self.client_address[0] in ("127.0.0.1", "::1"))

    def _json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json" or self.headers.get("X-Requested-With") != "Stromvertrag":
            raise ValueError("Ungültige Anfrage")
        return json.loads(self.rfile.read(size))

    def _dispatch(self):
        if not self._allowed():
            self._send(403, {"error": "Zugriff nur über Home Assistant Ingress"})
            return
        path = urlparse(self.path).path
        if self.command == "GET" and path in ("/", "/index.html"):
            self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
        elif self.command == "GET" and path == "/app.js":
            self._send(200, SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
        elif self.command == "GET" and path == "/api/contracts":
            self._send(200, contracts())
        elif self.command == "GET" and path == "/api/report":
            self._send(200, report())
        elif self.command == "GET" and path == "/api/settings":
            config = ha("GET", "/config")
            tz = ZoneInfo(config.get("time_zone") or "Europe/Berlin")
            self._send(200, {"language": config.get("language") or "de", "today": datetime.now(tz).date().isoformat()})
        elif self.command == "GET" and path == "/api/energy/status":
            self._send(200, dict(ENERGY_STATUS))
        elif self.command == "POST" and path == "/api/energy/sync":
            if self.headers.get("X-Requested-With") != "Stromvertrag":
                raise ValueError("Ungültige Anfrage")
            self._send(200, sync_energy_once())
        elif self.command == "GET" and path == "/api/energy/history/preview":
            config = ha("GET", "/config")
            tz = ZoneInfo(config.get("time_zone") or "Europe/Berlin")
            plan = history.build_plan(contracts(), ha, datetime.now(tz).date())
            self._send(200, history.public_preview(plan))
        elif self.command == "GET" and path == "/api/energy/history/status":
            self._send(200, latest_history_job() or {"status": "none"})
        elif self.command == "POST" and path == "/api/energy/history/apply":
            token = self._json().get("token")
            if not isinstance(token, str) or len(token) != 64:
                raise ValueError("Ungültige Vorschau")
            config = ha("GET", "/config")
            tz = ZoneInfo(config.get("time_zone") or "Europe/Berlin")
            plan = history.build_plan(contracts(), ha, datetime.now(tz).date())
            if plan["token"] != token:
                raise ValueError("Die Daten haben sich seit der Vorschau geändert. Bitte erneut prüfen.")
            if abs(plan["adjustment"]) < 0.000001:
                raise ValueError("Die historischen Kosten stimmen bereits mit dem Vertragspreis überein")
            with LOCK, database() as conn:
                if conn.execute("SELECT 1 FROM history_jobs WHERE status='running'").fetchone():
                    raise ValueError("Ein Korrekturlauf läuft bereits")
                if conn.execute("SELECT 1 FROM history_jobs WHERE token=?", (token,)).fetchone():
                    raise ValueError("Diese Vorschau wurde bereits angewendet oder begonnen")
                conn.execute("INSERT INTO history_jobs VALUES(?,?,?,?)", (token, "running", "Korrekturlauf läuft", datetime.now().isoformat()))
            Thread(target=run_history_job, args=(plan,), daemon=True, name="energy-history-backfill").start()
            self._send(202, {"status": "running", "token": token})
        elif self.command == "POST" and path == "/api/preview":
            self._send(200, preview_contract(self._json()))
        elif self.command == "GET" and path == "/api/entities":
            states = ha("GET", "/states")
            sensors = []
            for state in states:
                unit = state.get("attributes", {}).get("unit_of_measurement")
                if state.get("entity_id", "").startswith("sensor.") and unit in ("kWh", "MWh", "Wh"):
                    sensors.append({"id": state["entity_id"], "name": state["attributes"].get("friendly_name", state["entity_id"]), "unit": unit})
            self._send(200, sorted(sensors, key=lambda s: s["name"].lower()))
        elif self.command == "POST" and path == "/api/contracts":
            raw = self._json()
            body = validate_contract(raw)
            updates = boundary_updates(raw, body)
            updates.update(reading_updates(raw, body))
            with LOCK, database() as conn:
                cursor = conn.execute("INSERT INTO contracts(body) VALUES(?)", (json.dumps(body),))
                save_boundaries(conn, cursor.lastrowid, updates)
                self._send(201, {"id": cursor.lastrowid})
            ENERGY_WAKE.set()
        elif self.command in ("PUT", "DELETE") and path.startswith("/api/contracts/"):
            contract_id = int(path.rsplit("/", 1)[1])
            with LOCK, database() as conn:
                if not conn.execute("SELECT 1 FROM contracts WHERE id=?", (contract_id,)).fetchone():
                    self._send(404, {"error": "Vertrag nicht gefunden"}); return
                if self.command == "PUT":
                    raw = self._json()
                    body = validate_contract(raw)
                    updates = boundary_updates(raw, body)
                    updates.update(reading_updates(raw, body))
                    conn.execute("UPDATE contracts SET body=? WHERE id=?", (json.dumps(body), contract_id))
                    save_boundaries(conn, contract_id, updates)
                else:
                    if self.headers.get("X-Requested-With") != "Stromvertrag":
                        raise ValueError("Ungültige Anfrage")
                    conn.execute("DELETE FROM readings WHERE contract_id=?", (contract_id,))
                    conn.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
                self._send(200, {"ok": True})
            ENERGY_WAKE.set()
        elif self.command in ("PUT", "DELETE") and path.startswith("/api/readings/"):
            _, _, _, cid, day = path.split("/")
            contract_id = int(cid)
            parsed = parse_date(day, "Ablesedatum")
            with LOCK, database() as conn:
                row = conn.execute("SELECT body FROM contracts WHERE id=?", (contract_id,)).fetchone()
                if row is None:
                    self._send(404, {"error": "Vertrag nicht gefunden"}); return
                contract = json.loads(row["body"])
                if not date.fromisoformat(contract["start"]) <= parsed <= date.fromisoformat(contract["end"]) + timedelta(days=1):
                    raise ValueError("Ablesedatum liegt außerhalb des Vertrags")
                if self.command == "PUT":
                    try:
                        value = Decimal(str(self._json().get("kwh", "")).replace(",", "."))
                    except InvalidOperation:
                        raise ValueError("Ungültiger Zählerstand") from None
                    if not value.is_finite() or value < 0:
                        raise ValueError("Zählerstand muss mindestens 0 sein")
                    conn.execute("INSERT OR REPLACE INTO readings VALUES(?,?,?)", (contract_id, day, str(value)))
                else:
                    if self.headers.get("X-Requested-With") != "Stromvertrag":
                        raise ValueError("Ungültige Anfrage")
                    conn.execute("DELETE FROM readings WHERE contract_id=? AND day=?", (contract_id, day))
                self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "Nicht gefunden"})

    def do_GET(self): self._safe()
    def do_POST(self): self._safe()
    def do_PUT(self): self._safe()
    def do_DELETE(self): self._safe()

    def _safe(self):
        try:
            self._dispatch()
        except (ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
            self._send(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._send(502, {"error": str(exc)})


if __name__ == "__main__":
    with database(): pass
    with database() as conn:
        conn.execute("UPDATE history_jobs SET status='interrupted', detail='Lauf durch Neustart unterbrochen; Energy-Statistiken prüfen' WHERE status='running'")
    if energy.install_integration():
        logging.warning("Stromvertrag-Integration installiert oder aktualisiert; Home Assistant muss neu gestartet werden")
    Thread(target=sync_energy_forever, daemon=True, name="energy-price-sync").start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8099"))), Handler).serve_forever()
