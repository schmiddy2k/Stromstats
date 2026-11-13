"""Register one price sensor for the Stromvertrag app."""
from homeassistant import config_entries

DOMAIN = "stromvertrag_price"


class StromvertragPriceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Stromvertrag Arbeitspreis", data={})
