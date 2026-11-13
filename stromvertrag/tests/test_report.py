import sys
import tempfile
import unittest
import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server
from model import validate_contract


class ReportTest(unittest.TestCase):
    def test_recorder_details_count_only_valid_contract_hours(self):
        rows = [
            {"start": "2025-01-01T00:00:00+00:00", "change": 1.5},
            {"start": "2025-01-01T01:00:00+00:00", "change": None},
            {"start": "2025-01-01T02:00:00+00:00", "change": -1},
            {"start": "2025-01-02T00:00:00+00:00", "change": 99},
        ]
        summary = server.recorder_summary(rows, "sensor.energy", date(2025, 1, 1), date(2025, 1, 2), ZoneInfo("UTC"))
        self.assertEqual(summary["expected_hours"], 24)
        self.assertEqual(summary["hours"], 1)
        self.assertEqual(summary["missing_hours"], 23)
        self.assertEqual(summary["kwh"], 1.5)

    def test_frontend_script_is_served_through_ingress(self):
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ("172.30.32.2", 1234)
        handler.path = "/app.js"
        handler.command = "GET"
        handler._send = Mock()
        handler._dispatch()
        status, body, content_type = handler._send.call_args.args
        self.assertEqual(status, 200)
        self.assertIn(b"schedulePreview", body)
        self.assertEqual(content_type, "text/javascript; charset=utf-8")

    def test_contracts_sorted_by_start_not_creation(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "DATA_DIR", Path(temp)), patch.object(server, "DB", Path(temp) / "contracts.sqlite3"):
            with server.database() as conn:
                for start in ("2025-01-01", "2024-01-01", "2026-01-01"):
                    contract = validate_contract({"provider": start, "tariff": "B", "start": start, "end": start, "base_monthly": "10", "price_kwh": "0.2", "contract_kwh": "1000"})
                    conn.execute("INSERT INTO contracts(body) VALUES(?)", (json.dumps(contract),))
            self.assertEqual([c["start"] for c in server.contracts()], ["2026-01-01", "2025-01-01", "2024-01-01"])

    def test_interior_readings_update_without_removing_existing(self):
        contract = validate_contract({"provider": "A", "tariff": "B", "start": "2025-01-01", "end": "2026-12-31", "base_monthly": "10", "price_kwh": "0.2", "contract_kwh": "1000"})
        self.assertEqual(server.reading_updates({}, contract), {})
        self.assertEqual(server.reading_updates({"reading_changes": {"2026-01-01": "550", "2025-06-01": None}}, contract), {"2026-01-01": "550", "2025-06-01": None})
        with self.assertRaises(ValueError):
            server.reading_updates({"reading_changes": {"2027-01-01": "900"}}, contract)

    def test_finished_contract_without_sensor_uses_boundary_readings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "DATA_DIR", Path(temp)), patch.object(server, "DB", Path(temp) / "contracts.sqlite3"):
            contract = validate_contract({"provider": "A", "tariff": "B", "start": "2025-12-31", "end": "2026-01-01", "base_monthly": "10", "price_kwh": "0.2", "contract_kwh": "1000", "entity_id": ""})
            with server.database() as conn:
                cid = conn.execute("INSERT INTO contracts(body) VALUES(?)", (json.dumps(contract),)).lastrowid
                server.save_boundaries(conn, cid, server.boundary_updates({"start_reading": "100", "end_reading": "110"}, contract))
            def fake_ha(method, path, body=None):
                self.assertEqual(path, "/config")
                return {"time_zone": "Europe/Berlin"}
            with patch.object(server, "ha", fake_ha):
                years = server.report()[0]["years"]
            self.assertEqual(sum(year["kwh"] for year in years), 10)
            self.assertTrue(all(year["status"] == "Aus Endständen geschätzt" for year in years))

    def test_existing_readings_survive_legacy_update(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "DATA_DIR", Path(temp)), patch.object(server, "DB", Path(temp) / "contracts.sqlite3"):
            contract = validate_contract({"provider": "A", "tariff": "B", "start": "2025-01-01", "end": "2025-12-31", "base_monthly": "10", "price_kwh": "0.2", "contract_kwh": "1000", "entity_id": "sensor.energy"})
            with server.database() as conn:
                cid = conn.execute("INSERT INTO contracts(body) VALUES(?)", (json.dumps(contract),)).lastrowid
                conn.execute("INSERT INTO readings VALUES(?,?,?)", (cid, "2025-06-01", "500"))
                server.save_boundaries(conn, cid, server.boundary_updates(contract, contract))
            self.assertEqual(server.readings(cid), {"2025-06-01": "500"})
            with server.database() as conn:
                server.save_boundaries(conn, cid, server.boundary_updates({"start_reading": "100", "end_reading": "1100"}, contract))
            self.assertEqual(server.readings(cid), {"2025-01-01": "100", "2025-06-01": "500", "2026-01-01": "1100"})

    def test_recorder_response_and_manual_boundaries(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "DATA_DIR", Path(temp)), patch.object(server, "DB", Path(temp) / "contracts.sqlite3"):
            contract = validate_contract({"provider": "A", "tariff": "B", "start": "2025-12-31", "end": "2026-01-01", "base_monthly": "31", "price_kwh": "0.5", "contract_kwh": "10", "bonus1": "2", "bonus2": "0", "bonus3": "0", "entity_id": "sensor.energy"})
            with server.database() as conn:
                cid = conn.execute("INSERT INTO contracts(body) VALUES(?)", (__import__("json").dumps(contract),)).lastrowid
                conn.executemany("INSERT INTO readings VALUES(?,?,?)", [(cid, "2025-12-31", "100"), (cid, "2026-01-01", "103"), (cid, "2026-01-02", "107")])
            def fake_ha(method, path, body=None):
                if path == "/config":
                    return {"time_zone": "Europe/Berlin"}
                self.assertEqual(body["period"], "hour")
                self.assertEqual(body["units"], {"energy": "kWh"})
                return {"service_response": {"statistics": {"sensor.energy": []}}}
            with patch.object(server, "ha", fake_ha):
                result = server.report()[0]
            self.assertEqual([year["kwh"] for year in result["years"]], [3.0, 4.0])
            self.assertEqual([year["status"] for year in result["years"]], ["Zählerstände", "Zählerstände"])


if __name__ == "__main__":
    unittest.main()
