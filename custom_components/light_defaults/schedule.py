"""Defaults that follow a schedule helper.

An entry (some lights, or all lights) can name a schedule helper. While one of
its blocks is active, the block's data overrides that entry's own values:
`brightness` (percent), `temperature` (Kelvin) and `color` ("#8c00ff" or
"140,0,255"). With apply_on_change, lights that are on are set to the new
values when the schedule moves to another block; lights that are off are never
turned on.
"""

from collections.abc import Callable, Iterable, Mapping
import logging
import re
from typing import Any

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON, STATE_ON
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_APPLY_ON_CHANGE,
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_SCHEDULE,
    CONF_TEMPERATURE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Where the unsubscribe callback for the schedules in use is kept.
UNSUB_KEY = f"{DOMAIN}_schedules"
# Block data we use; a change in anything else (next_event) is ignored.
BLOCK_KEYS = (CONF_BRIGHTNESS, CONF_TEMPERATURE, CONF_COLOR)


def parse_color(value: Any) -> tuple[int, int, int] | None:
    """An RGB color from a schedule block: "#8c00ff", "8c00ff" or "140,0,255"."""
    text = str(value).strip()
    if m := re.fullmatch(r"#?([0-9a-fA-F]{6})", text):
        hex_ = m.group(1)
        return int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)
    parts = re.split(r"[\s,;]+", text.strip("[]() "))
    if len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None


def block_values(hass: HomeAssistant, schedule_id: str) -> dict[str, Any]:
    """The active block's values, or nothing when the schedule is off. Values
    that don't parse are logged and left out. A color comes with temperature
    None, so it replaces a temperature set less specifically."""
    state = hass.states.get(schedule_id)
    if state is None or state.state != STATE_ON:
        return {}
    attrs = state.attributes
    values: dict[str, Any] = {}
    for key, low, high in ((CONF_BRIGHTNESS, 1, 100), (CONF_TEMPERATURE, 1000, 10000)):
        if key not in attrs:
            continue
        try:
            number = round(float(attrs[key]))
        except (TypeError, ValueError):
            number = None
        if number is None or not low <= number <= high:
            _LOGGER.warning("%s: %s %r isn't %d–%d, ignored", schedule_id, key, attrs[key], low, high)
            continue
        values[key] = number
    if CONF_COLOR in attrs:
        if (color := parse_color(attrs[CONF_COLOR])) is None:
            _LOGGER.warning("%s: color %r isn't #rrggbb or r,g,b, ignored", schedule_id, attrs[CONF_COLOR])
        else:
            values[CONF_COLOR] = color
            values.setdefault(CONF_TEMPERATURE, None)
    return values


def overlay(
    hass: HomeAssistant,
    place: Mapping[str, Any],
    values: dict[str, Any],
    schedules: set[str] | None,
) -> None:
    """If this place (a light's, a group's or all lights' entry) has a schedule,
    put the active block's values over `values`, and note the schedule."""
    if not (schedule_id := place.get(CONF_SCHEDULE)):
        return
    if schedules is not None:
        schedules.add(schedule_id)
    block = block_values(hass, schedule_id)
    if block.get(CONF_TEMPERATURE) is not None:
        values.pop(CONF_COLOR, None)  # A temperature replaces a less specific color.
    values.update(block)


def _watched(places: Iterable[Mapping[str, Any]]) -> list[str]:
    """Schedules in use by at least one place with apply_on_change."""
    return sorted(
        {
            place[CONF_SCHEDULE]
            for place in places
            if place.get(CONF_SCHEDULE) and place.get(CONF_APPLY_ON_CHANGE, True)
        }
    )


@callback
def track(
    hass: HomeAssistant,
    places: Iterable[Mapping[str, Any]],
    get: Callable[[HomeAssistant, str, set[str]], dict[str, Any]],
    fill: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    """(Re)subscribe to the schedules in use. `get` resolves a light's values
    (and notes the schedules it looked at), `fill` turns them into turn-on
    data. Called whenever the set of places changes."""
    if unsub := hass.data.pop(UNSUB_KEY, None):
        unsub()
    if not (watched := _watched(places)):
        return

    async def schedule_changed(event: Event[EventStateChangedData]) -> None:
        schedule_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or (
            old is not None
            and old.state == new.state
            and all(old.attributes.get(k) == new.attributes.get(k) for k in BLOCK_KEYS)
        ):
            return
        for state in hass.states.async_all(LIGHT_DOMAIN):
            if state.state != STATE_ON:
                continue
            try:
                used: set[str] = set()
                light = get(hass, state.entity_id, used)
                data: dict[str, Any] = {}
                fill(light, data)
                if schedule_id not in used or not data:
                    continue
                await hass.services.async_call(
                    LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: state.entity_id, **data}
                )
            except Exception:  # One light's trouble mustn't stop the rest.
                _LOGGER.exception("Couldn't apply %s to %s", schedule_id, state.entity_id)

    hass.data[UNSUB_KEY] = async_track_state_change_event(hass, watched, schedule_changed)
