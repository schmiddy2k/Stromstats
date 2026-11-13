import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import history


class HistoryTest(unittest.TestCase):
    def _plan(self, missing=False, priced_hours=False):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(hours=48)
        meter_rows = [{"start": int((start + timedelta(hours=n)).timestamp() * 1000), "change": 1}
                      for n in range(48 if not missing else 47)]
        cost_rows = ([{"start": int((start + timedelta(hours=n)).timestamp() * 1000),
                       "sum": (n + 1) * 0.1, "change": 0.1, "state": (n + 1) * 0.1}
                      for n in range(48)] if priced_hours else [])
        cost_rows.append({"start": int(end.timestamp() * 1000),
                          "sum": 4.8 if priced_hours else 0, "change": 0, "state": 4.8 if priced_hours else 0})

        def ws(command, timeout=15):
            if command["type"] == "energy/get_prefs":
                return {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.meter",
                                            "entity_energy_price": "sensor.stromvertrag_arbeitspreis", "stat_cost": None}]}
            if command["type"] == "energy/info":
                return {"cost_sensors": {"sensor.meter": "sensor.meter_cost"}}
            if command["type"] == "recorder/get_statistics_metadata":
                return [{"statistic_id": "sensor.meter_cost", "source": "recorder",
                         "statistics_unit_of_measurement": "EUR", "mean_type": 0,
                         "has_sum": True, "unit_class": None, "name": None}]
            raise AssertionError(command)

        def rows(statistic_id, *_args):
            return meter_rows if statistic_id == "sensor.meter" else cost_rows

        contract = {"id": 1, "start": "2026-01-01", "end": "2026-12-31",
                    "entity_id": "sensor.meter", "price_kwh": "0.30"}
        with patch.object(history, "websocket_command", side_effect=ws), patch.object(history, "_rows", side_effect=rows):
            return history.build_plan([contract], lambda *_: {"currency": "EUR", "time_zone": "UTC"}, date(2026, 1, 3))

    def test_preview_calculates_past_hours_without_writing(self):
        plan = self._plan()
        self.assertEqual(plan["hours"], 48)
        self.assertAlmostEqual(plan["calculated_cost"], 14.4)
        self.assertAlmostEqual(plan["adjustment"], 14.4)
        self.assertNotIn("rows", history.public_preview(plan))

    def test_missing_hour_blocks_backfill(self):
        with self.assertRaisesRegex(ValueError, "1 Vertragsstunden"):
            self._plan(missing=True)

    def test_existing_costs_are_replaced_hour_by_hour(self):
        plan = self._plan(priced_hours=True)
        self.assertAlmostEqual(plan["existing_cost"], 4.8)
        self.assertAlmostEqual(plan["adjustment"], 9.6)
        self.assertAlmostEqual(plan["rows"][-1]["sum"], 14.4)

    def test_apply_imports_then_adjusts_current_sum(self):
        plan = self._plan()
        last = datetime.fromisoformat(plan["rows"][-1]["start"])
        future = datetime.fromisoformat(plan["future_time"])
        confirmed = [
            {"start": int(last.timestamp() * 1000), "sum": plan["rows"][-1]["sum"]},
            {"start": int(future.timestamp() * 1000), "sum": plan["future_sum_before"] + plan["adjustment"]},
        ]
        with patch.object(history, "websocket_command") as ws, patch.object(history, "_rows", return_value=confirmed):
            history.apply_plan(plan)
        self.assertEqual([call.args[0]["type"] for call in ws.call_args_list],
                         ["recorder/import_statistics", "recorder/adjust_sum_statistics"])
        self.assertEqual(ws.call_args.args[0]["start_time"], plan["end"])


if __name__ == "__main__":
    unittest.main()
