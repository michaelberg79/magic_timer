import homeassistant.util.dt as dt_util  # Für zeitzonenkorrekte Timestamps
import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    intent,
)
from homeassistant.helpers.event import (
    async_track_point_in_time,
)  # KORREKTUR: Der effiziente Wecker
from homeassistant.helpers.storage import Store
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

DOMAIN = "magic_timer"
STORAGE_KEY = "magic_timer.timers"
STORAGE_VERSION = 1
INTENT_ADD_TIMER = "MagicTimerAdd"

SERVICE_ADD_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required(
            "duration"
        ): cv.string,  # Erwartet "HH:MM:SS" oder Minuten als Zahl
        vol.Required("message"): cv.string,
        vol.Optional("area"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird aufgerufen, wenn Magic Timer über die UI eingerichtet wurde."""

    room_device_assignments = entry.data.get("assignments")

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    # ==========================================
    # DEV-MODE: AUTOMATISCHE HARDWARE-DUMMIES
    # ==========================================
    _LOGGER.info("[Magic Timer] Erstelle virtuelle Test-Umgebung...")

    # 1. Registries holen
    area_registry = ar.async_get(hass)
    # device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    assignments = entry.options.get("room_assist_assignments")

    # 2. Räume (Areas) anlegen, falls sie fehlen
    kueche = area_registry.async_get_or_create("Küche")
    wohnzimmer = area_registry.async_get_or_create("Wohnzimmer")

    # 3. Virtuelle Entities direkt in die Registry mogeln
    # Dummy Küche
    entity_registry.async_get_or_create(
        domain="media_player",
        platform="magic_timer",
        unique_id="dummy_speaker_kueche",
        suggested_object_id="kuechen_sprecher",
    )
    # Die erstellte Entity dem Raum zuordnen
    entity_registry.async_update_entity(
        "media_player.kuechen_sprecher", area_id=kueche.id
    )

    # Dummy Wohnzimmer
    entity_registry.async_get_or_create(
        domain="media_player",
        platform="magic_timer",
        unique_id="dummy_speaker_wohnzimmer",
        suggested_object_id="wohnzimmer_echo",
    )
    entity_registry.async_update_entity(
        "media_player.wohnzimmer_echo", area_id=wohnzimmer.id
    )

    _LOGGER.info("[Magic Timer] Test-Räume und Dummy-Player wurden im RAM erstellt!")
    # ==========================================

    stored_timers = await store.async_load() or []

    hass.data[DOMAIN] = {"active_timers": {}, "store": store}

    # Hilfsfunktion: Wandelt Strings ("00:05:00" oder "5") in Sekunden um
    def parse_duration(duration_str: str) -> int:
        try:
            if ":" in duration_str:
                parts = list(map(int, duration_str.split(":")))
                if len(parts) == 3:
                    return parts[0] * 3600 + parts[1] * 60 + parts[2]
                if len(parts) == 2:
                    return parts[0] * 60 + parts[1]
            return (
                int(duration_str) * 60
            )  # Falls nur eine Zahl übergeben wurde -> Minuten
        except ValueError:
            return 60  # Fallback 1 Minute bei Fehlern

    # Kern-Funktion: Was passiert, wenn die Zeit abgelaufen ist?
    async def async_timer_finished(now):
        """Wird exakt zum Ablaufzeitpunkt vom HA-Core aufgerufen."""
        # Alle Timer durchsuchen, die jetzt abgelaufen sein müssten
        current_time = dt_util.utcnow()
        timers = list(hass.data[DOMAIN]["active_timers"].values())

        for timer in timers:
            end_dt = dt_util.parse_datetime(timer["end_time"])
            _LOGGER.info(
                "End_dt: %s, Timer: %s, Current_Time: %s", end_dt, timer, current_time
            )
            _LOGGER.info(assignments)
            if end_dt and end_dt <= current_time:
                _LOGGER.info(
                    "[Magic Timer] ABLAUF! '%s' im Raum %s Benutztes Gerät:  %s",
                    timer["message"],
                    timer["area"],
                    {assignments[f"assignment_{timer['area']}"]},
                )
                # Event abfeuern, damit Automationen / TTS darauf reagieren können
                hass.bus.async_fire(
                    "magic_timer_finished",
                    {"message": timer["message"]},
                )

                llm_text = f"Nachricht: Ich sollte ich an folgendes erinnern: {timer['message']}"
                hass.services.async_call(
                    "assist_satellite",
                    "start_conversation",
                    {
                        "entity_id": assignments[f"assignment_{timer['area']}"],
                        "start_message": llm_text,
                        "preannounce": True,
                    },
                )
                # Aus dem RAM löschen
                timer_id = f"{timer['end_time']}_{timer['message']}"
                if timer_id in hass.data[DOMAIN]["active_timers"]:
                    del hass.data[DOMAIN]["active_timers"][timer_id]

        # persistenten Speicher aktualisieren
        await store.async_save(list(hass.data[DOMAIN]["active_timers"].values()))

    # Funktion zum Einplanen eines Timers im HA-Core
    def schedule_timer(timer_data):
        end_dt = dt_util.parse_datetime(timer_data["end_time"])
        if end_dt:
            # Falls der Timer während HA offline war abgelaufen ist, triggern wir ihn sofort in 1 Sekunde
            if end_dt <= dt_util.utcnow():
                end_dt = dt_util.utcnow() + timedelta(seconds=1)

            # Hier sagen wir HA, wann er uns wecken soll
            async_track_point_in_time(hass, async_timer_finished, end_dt)

    # RECOVERY BEIM START: Alle geladenen Timer wieder aktivieren
    for timer in stored_timers:
        timer_id = f"{timer['end_time']}_{timer['message']}"
        hass.data[DOMAIN]["active_timers"][timer_id] = timer
        schedule_timer(timer)

    # Der Service-Handler für neue Timer
    async def handle_add_timer(call):
        duration_raw = call.data.get("duration")
        message = call.data.get("message")
        area = call.data.get("area", "Global")

        seconds = parse_duration(str(duration_raw))
        start_time = dt_util.utcnow()
        end_time = start_time + timedelta(seconds=seconds)

        new_timer = {
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "message": message,
            "area": area,
        }

        timer_id = f"{new_timer['end_time']}_{message}"
        hass.data[DOMAIN]["active_timers"][timer_id] = new_timer

        # Auf Festplatte sichern
        await store.async_save(list(hass.data[DOMAIN]["active_timers"].values()))

        # Den "Wecker" im Core stellen
        schedule_timer(new_timer)

        _LOGGER.info(
            f"[Magic Timer] Timer eingeplant für {end_time.strftime('%H:%M:%S')}: {message}"
        )
        hass.bus.async_fire("magic_timer_started", new_timer)

    hass.services.async_register(
        DOMAIN, "add_timer", handle_add_timer, schema=SERVICE_ADD_TIMER_SCHEMA
    )

    # Intent-Handler für das LLM
    class MagicTimerIntentHandler(intent.IntentHandler):
        intent_type = INTENT_ADD_TIMER
        slot_schema = {
            vol.Required("duration"): cv.string,
            vol.Required("message"): cv.string,
            vol.Optional("area"): cv.string,
        }

        async def async_handle(
            self, intent_obj: intent.Intent
        ) -> intent.IntentResponse:
            # Slots holen
            slots = intent_obj.slots

            # KORREKTUR: Da die Slots Dicts sind, nutzen wir .get("value")
            duration = (
                slots.get("duration", {}).get("value") if "duration" in slots else None
            )
            message = (
                slots.get("message", {}).get("value") if "message" in slots else None
            )
            area = slots.get("area", {}).get("value") if "area" in slots else None

            if not duration or not message:
                raise intent.IntentHandleError(
                    "Dauer und Nachricht fehlen im Intent-Aufruf."
                )

            service_data = {"duration": duration, "message": message}
            if area:
                service_data["area"] = area

            await hass.services.async_call(
                DOMAIN, "add_timer", service_data, context=intent_obj.context
            )

            response = intent_obj.create_response()
            response.async_set_speech(f"Ich habe einen Timer für {message} gestellt.")
            return response

    intent.async_register(hass, MagicTimerIntentHandler())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird aufgerufen, wenn die Integration gelöscht wird."""
    hass.services.async_remove(DOMAIN, "add_timer")
    return True
