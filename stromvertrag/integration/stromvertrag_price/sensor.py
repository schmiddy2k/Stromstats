"""Registered price entity updated by the Stromvertrag app through HA events."""
from decimal import Decimal, InvalidOperation

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback

EVENT = "stromvertrag_price_update"


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([StromvertragPriceSensor()])


class StromvertragPriceSensor(SensorEntity):
    _attr_unique_id = "stromvertrag_arbeitspreis"
    _attr_name = "Stromvertrag Arbeitspreis"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_icon = "mdi:currency-eur"
    _attr_should_poll = False
    _attr_available = False
    _attr_extra_state_attributes = {"stromvertrag_source": "stromvertrag_price"}

    def __init__(self):
        self.entity_id = "sensor.stromvertrag_arbeitspreis"

    async def async_added_to_hass(self):
        self.async_on_remove(self.hass.bus.async_listen(EVENT, self._update_price))

    @callback
    def _update_price(self, event):
        raw = event.data.get("price")
        if raw is None:
            self._attr_available = False
            self._attr_native_value = None
        else:
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                return
            if not value.is_finite() or value < 0:
                return
            self._attr_native_value = float(value)
            self._attr_available = True
        self._attr_name = str(event.data.get("name") or "Stromvertrag Arbeitspreis")[:100]
        self._attr_extra_state_attributes = {
            "stromvertrag_source": "stromvertrag_price",
            "contract_id": event.data.get("contract_id"),
            "meter_entity": event.data.get("meter_entity"),
        }
        self.async_write_ha_state()
