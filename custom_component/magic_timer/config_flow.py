"""OptionsFlow for configuring the assist_devices - room assignments."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    selector,
)

from .const import DOMAIN


class MagicTimerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Der minimale Config Flow, der nur als Registrierung dient."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Wird aufgerufen, wenn der Nutzer die Integration hinzufügt."""

        # Sicherheitsgurt: Verhindern, dass man die Integration mehrfach installiert
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        # Wenn der Nutzer auf "Hinzufügen" klickt, wird die Instanz SOFORT erstellt
        # Es wird kein Formular (data_schema=None) angezeigt!
        if user_input is not None:
            return self.async_create_entry(title="Magic Timer", data={})

        # Zeigt ein leeres Bestätigungsfenster ("Möchtest du Magic Timer einrichten?")
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Hier verknüpfen wir dein eigentliches Kontrollzentrum (den Options Flow)."""
        return MagicTimerOptionsFlowHandler(config_entry)


class MagicTimerOptionsFlowHandler(config_entries.OptionsFlow):
    """Erzeugt eine dynamische Übersicht aller Räume mit ihren jeweiligen Mediaplayern."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialisiert den Optionsflow."""
        super().__init__()

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Wird aufgerufen, wenn man auf 'Konfigurieren' klickt."""
        if user_input is not None:
            # Speichert die raumspezifischen Player-Zuordnungen direkt in den Optionen
            return self.async_create_entry(title="", data=user_input)

        # Registries laden
        a_reg = ar.async_get(self.hass)
        e_reg = er.async_get(self.hass)
        d_reg = dr.async_get(self.hass)

        # 1. Alle Mediaplayer im System nach ihrer Area ID gruppieren
        satellites_by_area: dict[str, dict[str, str]] = {}
        for entity in e_reg.entities.values():
            device = d_reg.async_get(entity.device_id)

            if entity.domain == "assist_satellite" and device.area_id:
                if entity.area_id not in satellites_by_area:
                    satellites_by_area[device.area_id] = {}

                # Sauberen Namen generiere
                device_name = "Unbekannter Satellite"
                if entity.device_id:
                    device = d_reg.async_get(entity.device_id)
                    if device:
                        device_name = device.name_by_user or device.name or device_name

                satellites_by_area[device.area_id][entity.entity_id] = device_name

        # 2. Dynamisches Schema für die UI aufbauen (mit Sektionen)

        schema_fields = {}

        # Wir gehen jeden Raum durch, der in HA existiert
        for area in a_reg.areas.values():
            # Nur Räume auflisten, die mindestens einen Mediaplayer enthalten
            if area.id in satellites_by_area:
                available_options = satellites_by_area[area.id]

                # Hole den aktuell bereits gespeicherten Wert
                current_selected = self.config_entry.options.get(area.id)

                if (
                    not current_selected
                    and self.config_entry.data.get("area") == area.id
                ):
                    current_selected = self.config_entry.data.get("assist_satellite ")

                # Erstelle die Liste der Optionen für den Selektor
                select_options = [
                    {"value": entity_id, "label": label}
                    for entity_id, label in available_options.items()
                ]

                # Der Select-Selector zwingt HA zu einem sauberen Dropdown-Menü!
                assistant_selector = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=select_options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
                # Wir packen das Feld in eine Sektion, die nach dem Raum benannt ist
                schema_fields[
                    vol.Optional(
                        f"assignment_{area.id}",
                        description={"section": area.id},
                        default=current_selected or select_options[0]["value"],
                    )
                ] = assistant_selector

                data_schema = vol.Schema(
                    {
                        vol.Required("room_assist_assignments"): section(
                            vol.Schema(schema_fields),
                            {"collapsed": False},
                        )
                    }
                )

        # Falls im gesamten System kein Raum mit Mediaplayern existiert
        if not schema_fields:
            return self.async_show_form(
                step_id="init", errors={"base": "no_hardware_found"}
            )

        return self.async_show_form(step_id="init", data_schema=data_schema)
