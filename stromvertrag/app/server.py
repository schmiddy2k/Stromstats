"""Ingress-only HTTP application backed by Home Assistant's recorder service."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from model import calculate, consumption, parse_date, sections, validate_contract

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB = DATA_DIR / "contracts.sqlite3"
LOCK = Lock()
STATIC = Path(__file__).with_name("index.html")
SUPERVISOR = os.environ.get("SUPERVISOR_URL", "http://supervisor")


def database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS contracts (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS readings (contract_id INTEGER NOT NULL, day TEXT NOT NULL, kwh TEXT NOT NULL, PRIMARY KEY(contract_id, day))")
    return conn


def ha(method: str, path: str, body: dict | None = None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("Home Assistant ist nicht verbunden (SUPERVISOR_TOKEN fehlt)")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = Request(SUPERVISOR + "/core/api" + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urlopen(request, timeout=45) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Home Assistant API: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Home Assistant nicht erreichbar: {exc.reason}") from exc


def contracts():
    with database() as conn:
        return [dict(id=row["id"], **json.loads(row["body"])) for row in conn.execute("SELECT id, body FROM contracts ORDER BY id DESC")]


def readings(contract_id: int):
    with database() as conn:
        return {r["day"]: r["kwh"] for r in conn.execute("SELECT day, kwh FROM readings WHERE contract_id=?", (contract_id,))}


def report():
    tz_name = ha("GET", "/config").get("time_zone", "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    output = []
    for contract in contracts():
        periods = sections(contract)
        if not periods:
            output.append({**contract, "readings": readings(contract["id"]), "years": []})
            continue
        from datetime import datetime, time
        first, last = periods[0][0], periods[-1][1]
        payload = {"start_time": datetime.combine(first, time.min, tz).isoformat(), "end_time": datetime.combine(last, time.min, tz).isoformat(),
                   "statistic_ids": [contract["entity_id"]], "period": "hour", "types": ["change"], "units": {"energy": "kWh"}}
        try:
            result = ha("POST", "/services/recorder/get_statistics?return_response", payload)
            rows = result["service_response"]["statistics"].get(contract["entity_id"], [])
        except (RuntimeError, KeyError, TypeError) as exc:
            rows = []
            error = str(exc)
        else:
            error = None
        # Recorder timestamps are UTC. Assign rows to local calendar years.
        for row in rows:
            from datetime import datetime
            row["start"] = datetime.fromisoformat(row["start"]).astimezone(tz).isoformat()
            row["end"] = datetime.fromisoformat(row["end"]).astimezone(tz).isoformat()
        manual = readings(contract["id"])
        years = []
        for start, end in periods:
            try:
                kwh, status = consumption(start, end, rows, manual)
            except ValueError as exc:
                kwh, status = None, str(exc)
            if error and kwh is None:
                status = error
            years.append(calculate(contract, start, end, kwh, status))
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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self'")
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
        elif self.command == "GET" and path == "/api/contracts":
            self._send(200, contracts())
        elif self.command == "GET" and path == "/api/report":
            self._send(200, report())
        elif self.command == "GET" and path == "/api/entities":
            states = ha("GET", "/states")
            sensors = []
            for state in states:
                unit = state.get("attributes", {}).get("unit_of_measurement")
                if state.get("entity_id", "").startswith("sensor.") and unit in ("kWh", "MWh", "Wh"):
                    sensors.append({"id": state["entity_id"], "name": state["attributes"].get("friendly_name", state["entity_id"]), "unit": unit})
            self._send(200, sorted(sensors, key=lambda s: s["name"].lower()))
        elif self.command == "POST" and path == "/api/contracts":
            body = validate_contract(self._json())
            with LOCK, database() as conn:
                cursor = conn.execute("INSERT INTO contracts(body) VALUES(?)", (json.dumps(body),))
                self._send(201, {"id": cursor.lastrowid})
        elif self.command in ("PUT", "DELETE") and path.startswith("/api/contracts/"):
            contract_id = int(path.rsplit("/", 1)[1])
            with LOCK, database() as conn:
                if not conn.execute("SELECT 1 FROM contracts WHERE id=?", (contract_id,)).fetchone():
                    self._send(404, {"error": "Vertrag nicht gefunden"}); return
                if self.command == "PUT":
                    body = validate_contract(self._json())
                    conn.execute("UPDATE contracts SET body=? WHERE id=?", (json.dumps(body), contract_id))
                else:
                    if self.headers.get("X-Requested-With") != "Stromvertrag":
                        raise ValueError("Ungültige Anfrage")
                    conn.execute("DELETE FROM readings WHERE contract_id=?", (contract_id,))
                    conn.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
                self._send(200, {"ok": True})
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
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8099"))), Handler).serve_forever()
