"""Publish the active contract price and link it to a matching Energy grid source."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date
from pathlib import Path
from shutil import copy2

PRICE_ENTITY = "sensor.stromvertrag_arbeitspreis"
OPTIONS = Path(os.environ.get("DATA_DIR", "/data")) / "options.json"
INTEGRATION_SOURCE = Path("/integration/stromvertrag_price")
INTEGRATION_TARGET = Path("/homeassistant/custom_components/stromvertrag_price")
INTEGRATION_FILES = ("manifest.json", "__init__.py", "config_flow.py", "sensor.py", "strings.json")


def install_integration() -> bool:
    """Install only this app's component; Home Assistant loads it after a restart."""
    if not INTEGRATION_SOURCE.is_dir() or not INTEGRATION_TARGET.parent.parent.is_dir():
        return False
    INTEGRATION_TARGET.mkdir(parents=True, exist_ok=True)
    changed = False
    for name in INTEGRATION_FILES:
        source, target = INTEGRATION_SOURCE / name, INTEGRATION_TARGET / name
        content = source.read_bytes()
        if not target.exists() or target.read_bytes() != content:
            copy2(source, target)
            changed = True
    return changed


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


def websocket_command(command: dict, timeout: int = 15) -> dict:
    # The Supervisor proxy authenticates WebSocket commands with its app token.
    from websocket import WebSocketException, create_connection

    supervisor = os.environ.get("SUPERVISOR_URL", "http://supervisor")
    url = supervisor.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/core/websocket"
    try:
        connection = create_connection(url, timeout=timeout)
    except (OSError, WebSocketException) as exc:
        raise RuntimeError(f"Home Assistant WebSocket nicht erreichbar: {exc}") from exc
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
    except (OSError, WebSocketException) as exc:
        raise RuntimeError(f"Home Assistant WebSocket-Verbindung unterbrochen: {exc}") from exc
    finally:
        connection.close()


def registered_price_entity() -> bool:
    """Unavailable entities omit custom state attributes; use the registry."""
    entries = websocket_command({"type": "config/entity_registry/list"})
    return any(entry.get("entity_id") == PRICE_ENTITY and entry.get("platform") == "stromvertrag_price"
               and entry.get("unique_id") == "stromvertrag_arbeitspreis" for entry in entries)


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
    source = existing.get("attributes", {}).get("stromvertrag_source") if existing else None
    if source == "stromvertrag":
        ha("DELETE", "/states/" + PRICE_ENTITY)
        existing = None
    elif existing and source != "stromvertrag_price" and not registered_price_entity():
        raise RuntimeError(f"{PRICE_ENTITY} ist bereits von einer anderen Quelle belegt")
    entries = ha("GET", "/config/config_entries/entry?domain=stromvertrag_price")
    if not entries:
        try:
            flow = ha("POST", "/config/config_entries/flow", {"handler": "stromvertrag_price"})
        except RuntimeError as exc:
            return f"Integration installiert; Home Assistant neu starten ({exc})"
        if flow.get("type") != "create_entry":
            return f"Integration konnte nicht automatisch eingerichtet werden: {flow.get('type', 'unbekannt')}"
        return "Integration eingerichtet; Sensor wird beim nächsten Abgleich geprüft"
    if not existing:
        return "Integration eingerichtet; Sensor noch nicht geladen. Home Assistant neu starten"
    ha("POST", "/events/stromvertrag_price_update", {
        "price": contract["price_kwh"] if contract else None,
        "name": name,
        "contract_id": contract["id"] if contract else None,
        "meter_entity": contract["entity_id"] if contract else None,
    })
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
