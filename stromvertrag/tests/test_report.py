import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server
from model import validate_contract


class ReportTest(unittest.TestCase):
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
