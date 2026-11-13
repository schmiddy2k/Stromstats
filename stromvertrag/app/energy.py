"""Publish the active contract price and link it to a matching Energy grid source."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date
from pathlib import Path

PRICE_ENTITY = "sensor.stromvertrag_arbeitspreis"
OPTIONS = Path(os.environ.get("DATA_DIR", "/data")) / "options.json"


def options() -> dict:
    try:
        return json.loads(OPTIONS.read_text())
    except (OSError, ValueError):
        return {}


def active_contract(contracts: list[dict], today: date) -> dict | None:
    active = [item for item in contracts if item["start"] <= today.isoformat() <= item["end"]]
    if len(active) > 1:
        raise ValueError("Mehrere Verträge sind gleichzeitig aktiv; der Arbeitspreis ist nicht eindeutig")
    return active[0] if active else None


def linked_sources(prefs: dict, meter: str | None) -> tuple[list[dict], bool]:
    """Keep our price linked only to the active contract's matching grid source."""
    sources = prefs.get("energy_sources", [])
    updated = deepcopy(sources)
    flows = grid_imports(updated)
    matches = [source for source in flows if source.get("stat_energy_from") == meter] if meter else []
    target = matches[0] if len(matches) == 1 else None
    if target and (target.get("stat_cost") or ":" in target.get("stat_energy_from", "") or target.get("entity_energy_price") not in (None, PRICE_ENTITY)):
        target = None
    for source in flows:
        if source is target:
            source["entity_energy_price"] = PRICE_ENTITY
            source["number_energy_price"] = None
        elif source.get("entity_energy_price") == PRICE_ENTITY:
            source["entity_energy_price"] = None
    return updated, updated != sources


def grid_imports(sources: list[dict]) -> list[dict]:
    """Read both current grid connections and the older flow_from layout."""
    return [flow for source in sources if source.get("type") == "grid"
            for flow in (source.get("flow_from", []) if "flow_from" in source else [source])]


def link_status(sources: list[dict], meter: str | None) -> str:
    if not meter:
        return "Kein aktiver Vertrag mit Strommesssensor"
    matches = [flow for flow in grid_imports(sources) if flow.get("stat_energy_from") == meter]
    if not matches:
        return f"{meter} ist nicht als Netzbezug unter Grid Connections eingetragen"
    if len(matches) > 1:
        return f"{meter} ist mehrfach unter Grid Connections eingetragen"
    flow = matches[0]
    if ":" in meter:
        return "Externe Statistik ohne Sensor-Entity unterstützt keinen aktuellen Preissensor"
    if flow.get("stat_cost"):
        return "Für diese Netzbezugsquelle ist bereits ein Kostensensor eingerichtet"
    if flow.get("entity_energy_price") not in (None, PRICE_ENTITY):
        return "Für diese Netzbezugsquelle ist bereits ein anderer Preissensor eingerichtet"
    return "Preissensor der passenden Grid Connection zugeordnet" if flow.get("entity_energy_price") == PRICE_ENTITY else "Preiszuordnung noch ausstehend"


def websocket_command(command: dict) -> dict:
    # The Supervisor proxy authenticates WebSocket commands with its app token.
    from websocket import create_connection

    supervisor = os.environ.get("SUPERVISOR_URL", "http://supervisor")
    url = supervisor.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/core/websocket"
    connection = create_connection(url, timeout=15)
    try:
        if json.loads(connection.recv()).get("type") != "auth_required":
            raise RuntimeError("Home Assistant WebSocket: Authentifizierung fehlt")
        connection.send(json.dumps({"type": "auth", "access_token": os.environ["SUPERVISOR_TOKEN"]}))
        if json.loads(connection.recv()).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket: Authentifizierung fehlgeschlagen")
        connection.send(json.dumps({"id": 1, **command}))
        while True:
            result = json.loads(connection.recv())
            if result.get("id") == 1:
                if not result.get("success"):
                    raise RuntimeError(f"Home Assistant WebSocket: {result.get('error', {})}")
                return result.get("result", {})
    finally:
        connection.close()


def sync(contracts: list[dict], ha, today: date) -> str:
    settings = options()
    name = str(settings.get("price_sensor_name") or "Stromvertrag Arbeitspreis").strip()[:100]
    ambiguous = False
    try:
        contract = active_contract(contracts, today) if settings.get("price_sensor_enabled", True) else None
    except ValueError:
        contract, ambiguous = None, True
    current = ha("GET", "/states")
    existing = next((state for state in current if state.get("entity_id") == PRICE_ENTITY), None)
    if existing and existing.get("attributes", {}).get("stromvertrag_source") != "stromvertrag":
        raise RuntimeError(f"{PRICE_ENTITY} ist bereits von einer anderen Quelle belegt")
    attributes = {"friendly_name": name, "unit_of_measurement": "EUR/kWh", "icon": "mdi:currency-eur", "stromvertrag_source": "stromvertrag"}
    if contract:
        attributes.update(contract_id=contract["id"], meter_entity=contract["entity_id"])
    ha("POST", "/states/" + PRICE_ENTITY, {"state": contract["price_kwh"] if contract else "unavailable", "attributes": attributes})
    prefs = websocket_command({"type": "energy/get_prefs"})
    meter = contract.get("entity_id") if contract else None
    sources, changed = linked_sources(prefs, meter)
    if changed:
        websocket_command({"type": "energy/save_prefs", "energy_sources": sources})
    if ambiguous:
        return "Mehrere Verträge gleichzeitig aktiv; Preissensor nicht verfügbar"
    if not settings.get("price_sensor_enabled", True):
        return "Preissensor deaktiviert"
    return link_status(sources, meter)
