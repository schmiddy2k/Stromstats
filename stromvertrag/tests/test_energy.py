import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import energy


class EnergyTest(unittest.TestCase):
    def test_install_integration_updates_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            target = Path(temp) / "homeassistant" / "custom_components" / "stromvertrag_price"
            source.mkdir()
            target.mkdir(parents=True)
            for name in energy.INTEGRATION_FILES:
                (source / name).write_text(name)
            (target / "user_note.txt").write_text("keep")
            with patch.object(energy, "INTEGRATION_SOURCE", source), patch.object(energy, "INTEGRATION_TARGET", target):
                self.assertTrue(energy.install_integration())
                self.assertFalse(energy.install_integration())
            self.assertEqual((target / "user_note.txt").read_text(), "keep")

    def test_active_contract_boundaries_and_overlap(self):
        first = {"id": 1, "start": "2026-01-01", "end": "2026-06-30"}
        second = {"id": 2, "start": "2026-07-01", "end": "2026-12-31"}
        self.assertEqual(energy.active_contract([first, second], date(2026, 6, 30)), first)
        self.assertEqual(energy.active_contract([first, second], date(2026, 7, 1)), second)
        with self.assertRaises(ValueError):
            energy.active_contract([first, first], date(2026, 5, 1))

    def test_only_matching_free_grid_source_is_changed(self):
        prefs = {"energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.meter", "number_energy_price": 0.4, "stat_cost": None},
            {"type": "grid", "stat_energy_from": "sensor.other", "number_energy_price": 0.2},
            {"type": "solar", "stat_energy_from": "sensor.solar"},
        ]}
        updated, changed = energy.linked_sources(prefs, "sensor.meter")
        self.assertTrue(changed)
        self.assertEqual(updated[0]["entity_energy_price"], energy.PRICE_ENTITY)
        self.assertIsNone(updated[0]["number_energy_price"])
        self.assertEqual(updated[1:], prefs["energy_sources"][1:])
        self.assertEqual(prefs["energy_sources"][0]["number_energy_price"], 0.4)
        updated, changed = energy.linked_sources(prefs, "sensor.missing")
        self.assertFalse(changed)
        prefs["energy_sources"][0]["stat_cost"] = "sensor.cost"
        self.assertFalse(energy.linked_sources(prefs, "sensor.meter")[1])

    def test_contract_switch_removes_old_meter_link(self):
        prefs = {"energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.old", "entity_energy_price": energy.PRICE_ENTITY},
            {"type": "grid", "stat_energy_from": "sensor.new", "entity_energy_price": None},
        ]}
        updated, changed = energy.linked_sources(prefs, "sensor.new")
        self.assertTrue(changed)
        self.assertIsNone(updated[0]["entity_energy_price"])
        self.assertEqual(updated[1]["entity_energy_price"], energy.PRICE_ENTITY)
        self.assertIsNone(energy.linked_sources(prefs, None)[0][0]["entity_energy_price"])

    def test_legacy_grid_connection_is_found(self):
        prefs = {"energy_sources": [{"type": "grid", "flow_from": [
            {"stat_energy_from": "sensor.meter", "stat_cost": None, "number_energy_price": 0.35},
        ], "flow_to": []}]}
        updated, changed = energy.linked_sources(prefs, "sensor.meter")
        self.assertTrue(changed)
        self.assertEqual(updated[0]["flow_from"][0]["entity_energy_price"], energy.PRICE_ENTITY)
        self.assertEqual(energy.link_status(updated, "sensor.meter"), "Preissensor der passenden Grid Connection zugeordnet")
        self.assertIn("nicht als Netzbezug", energy.link_status(updated, "sensor.other"))

    @patch.object(energy, "websocket_command")
    @patch.object(energy, "options", return_value={})
    def test_sync_publishes_price_then_links_meter(self, _options, websocket):
        websocket.side_effect = [
            {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.meter", "stat_cost": None}]},
            {},
        ]
        calls = []
        def ha(method, path, body=None):
            calls.append((method, path, body))
            if path == "/states":
                return [{"entity_id": energy.PRICE_ENTITY, "attributes": {"stromvertrag_source": "stromvertrag_price"}}]
            if path.startswith("/config/config_entries/entry"):
                return [{"domain": "stromvertrag_price"}]
            return {}
        contract = {"id": 7, "start": "2026-01-01", "end": "2026-12-31", "entity_id": "sensor.meter", "price_kwh": "0.31"}
        energy.sync([contract], ha, date(2026, 5, 1))
        self.assertEqual([c for c in calls if c[1] == "/events/stromvertrag_price_update"][0][2]["price"], "0.31")
        self.assertEqual(websocket.call_args.args[0]["energy_sources"][0]["entity_energy_price"], energy.PRICE_ENTITY)


if __name__ == "__main__":
    unittest.main()
