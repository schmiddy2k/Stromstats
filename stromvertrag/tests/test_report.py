import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server
from model import validate_contract


class ReportTest(unittest.TestCase):
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
