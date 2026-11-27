"""Defaults that follow a schedule helper.

An entry (some lights, or all lights) can name a schedule helper. While one of
its blocks is active, the block's data overrides that entry's own values:
`brightness` (percent), `temperature` (Kelvin) and `color` ("#8c00ff" or
"140,0,255"). With apply_on_change, lights that are on get the values that
changed and came from the schedule when it moves to another block; lights that
are off are never turned on.
"""

from collections.abc import Callable, Iterable, Mapping
import logging
import re
from typing import Any

from homeassistant.components.light import ATTR_EFFECT, DOMAIN as LIGHT_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_APPLY_ON_CHANGE,
    CONF_BRIGHTNESS,
    CONF_COLOR,
    CONF_SCHEDULE,
    CONF_TEMPERATURE,
    DOMAIN,
    VALUES,
)

_LOGGER = logging.getLogger(__name__)

# Where the unsubscribe callback for the schedules in use is kept.
UNSUB_KEY = f"{DOMAIN}_schedules"
# Block data we use; a change in anything else (next_event) is ignored.
BLOCK_KEYS = (CONF_BRIGHTNESS, CONF_TEMPERATURE, CONF_COLOR)
# Where ZHA, WLED and MQTT (Zigbee2MQTT 2.10+) groups list their members.
ATTR_GROUP_ENTITIES = "group_entities"

# Looks up a state by entity ID; lets the listener resolve against the old
# schedule state.
Lookup = Callable[[str], State | None]
# Resolves a light: (values, source schedule per value).
Resolve = Callable[[HomeAssistant, str, Lookup | None], tuple[dict[str, Any], dict[str, str | None]]]
# Turns values into turn-on data for a light, given its state.
Fill = Callable[[dict[str, Any], dict[str, Any], State | None], None]


def parse_color(value: Any) -> tuple[int, int, int] | None:
    """An RGB color from a schedule block: "#8c00ff" or "140,0,255"."""
    text = str(value).strip()
    if m := re.fullmatch(r"#([0-9a-fA-F]{6})", text):
        hex_ = m.group(1)
        return int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)
    if m := re.fullmatch(r"(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", text):
        rgb = tuple(int(part) for part in m.groups())
        if all(part <= 255 for part in rgb):
            return rgb  # type: ignore[return-value]
    return None


def _number(schedule_id: str, key: str, raw: Any, low: int, high: int) -> int | None:
    """A whole number in range from block data, or None (logged)."""
    try:
        if isinstance(raw, bool):
            raise ValueError
        number = round(float(raw))
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is None or not low <= number <= high:
        _LOGGER.warning("%s: %s %r isn't %d–%d, ignored", schedule_id, key, raw, low, high)
        return None
    return number


def block_values(state: State | None, schedule_id: str) -> dict[str, Any]:
    """The active block's values, or nothing when the schedule is off or
    missing. Values that don't parse are logged and left out."""
    if state is None or state.state != STATE_ON:
        return {}
    attrs = state.attributes
    values: dict[str, Any] = {}
    for key, low, high in ((CONF_BRIGHTNESS, 1, 100), (CONF_TEMPERATURE, 1000, 10000)):
        if key in attrs and (number := _number(schedule_id, key, attrs[key], low, high)):
            values[key] = number
    if CONF_COLOR in attrs:
        if (color := parse_color(attrs[CONF_COLOR])) is None:
            _LOGGER.warning(
                "%s: color %r isn't \"#rrggbb\" or \"r,g,b\", ignored",
                schedule_id,
                attrs[CONF_COLOR],
            )
        else:
            values[CONF_COLOR] = color
    return values


def merge(
    place: Mapping[str, Any],
    values: dict[str, Any],
    sources: dict[str, str | None],
    lookup: Lookup,
) -> None:
    """Put one place's values (a light's, a group's or all lights' entry) over
    the less specific ones already in `values`. The active block of its
    schedule beats its own values. `sources` records, per value, the schedule
    it came from when that schedule updates lights that are on (else None).

    Color and temperature: a place that sets a color keeps a less specific
    temperature as the fallback for lights without color; a place that sets a
    temperature (and no color) drops any less specific color."""
    own = {key: place[key] for key in VALUES if key in place}
    schedule_id = place.get(CONF_SCHEDULE)
    block = block_values(lookup(schedule_id), schedule_id) if schedule_id else {}
    source = schedule_id if schedule_id and place.get(CONF_APPLY_ON_CHANGE, True) else None
    for key, value in own.items():
        values[key], sources[key] = value, None
    for key, value in block.items():
        values[key], sources[key] = value, source
    temperature = block.get(CONF_TEMPERATURE, own.get(CONF_TEMPERATURE))
    if CONF_COLOR not in block and temperature is not None:
        values.pop(CONF_COLOR, None)
        sources.pop(CONF_COLOR, None)


def _watched(places: Iterable[Mapping[str, Any]]) -> list[str]:
    """Schedules in use by at least one place with apply_on_change."""
    return sorted(
        {
            place[CONF_SCHEDULE]
            for place in places
            if place.get(CONF_SCHEDULE) and place.get(CONF_APPLY_ON_CHANGE, True)
        }
    )


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _changed_from(
    schedule_id: str, new: tuple[dict, dict], old: tuple[dict, dict]
) -> dict[str, Any]:
    """The values to send: those that came from this schedule before or now,
    and differ. Color and temperature go together so the light can pick."""
    (new_values, new_sources), (old_values, old_sources) = new, old
    keys = {
        key
        for key in new_values.keys() | old_values.keys()
        if schedule_id in (new_sources.get(key), old_sources.get(key))
        and new_values.get(key) != old_values.get(key)
    }
    if keys & {CONF_COLOR, CONF_TEMPERATURE}:
        keys |= {CONF_COLOR, CONF_TEMPERATURE}
    return {key: new_values[key] for key in keys if key in new_values}


@callback
def track(
    hass: HomeAssistant,
    places: Iterable[Mapping[str, Any]],
    resolve: Resolve,
    fill: Fill,
) -> None:
    """(Re)subscribe to the schedules in use; call whenever the places change."""
    if unsub := hass.data.pop(UNSUB_KEY, None):
        unsub()
    if not (watched := _watched(places)):
        return

    async def schedule_changed(event: Event[EventStateChangedData]) -> None:
        schedule_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        # Not when the schedule first appears (HA start, helper re-created) or
        # goes away, and not when only next_event moved.
        if not (_usable(old) and _usable(new)):
            return
        if old.state == new.state and all(
            old.attributes.get(key) == new.attributes.get(key) for key in BLOCK_KEYS
        ):
            return

        def old_lookup(entity_id: str) -> State | None:
            return old if entity_id == schedule_id else hass.states.get(entity_id)

        targets: dict[str, dict[str, Any]] = {}
        wanted: dict[str, dict[str, Any]] = {}  # The values behind each target's data.
        for state in hass.states.async_all(LIGHT_DOMAIN):
            if state.state != STATE_ON or state.attributes.get(ATTR_EFFECT) not in (None, "off", "none"):
                continue  # Off lights stay off; a running effect is left alone.
            try:
                light = _changed_from(
                    schedule_id,
                    resolve(hass, state.entity_id, None),
                    resolve(hass, state.entity_id, old_lookup),
                )
                data: dict[str, Any] = {}
                fill(light, data, state)
                if data:
                    targets[state.entity_id], wanted[state.entity_id] = data, light
            except Exception:  # One light's trouble mustn't stop the rest.
                _LOGGER.exception("Couldn't work out %s for %s", schedule_id, state.entity_id)

        # A native group's one command covers its members: don't send them the
        # same values again (their data may differ only in what they support).
        for group_id in list(targets):
            group = hass.states.get(group_id)
            for member in (group.attributes.get(ATTR_GROUP_ENTITIES) or ()) if group else ():
                if member in targets and wanted.get(member) == wanted[group_id]:
                    del targets[member]

        # One call per distinct set of values.
        batches: dict[str, tuple[dict[str, Any], list[str]]] = {}
        for entity_id, data in targets.items():
            batches.setdefault(repr(sorted(data.items())), (data, []))[1].append(entity_id)
        for data, entity_ids in batches.values():
            try:
                await hass.services.async_call(
                    LIGHT_DOMAIN,
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: entity_ids, **data},
                    context=event.context,
                )
            except Exception:
                _LOGGER.exception("Couldn't apply %s to %s", schedule_id, ", ".join(entity_ids))

    hass.data[UNSUB_KEY] = async_track_state_change_event(hass, watched, schedule_changed)

    @callback
    def warn_missing(_: Event | None = None) -> None:
        if missing := [sid for sid in watched if hass.states.get(sid) is None]:
            _LOGGER.warning("Schedules not found: %s", ", ".join(missing))

    if hass.state is CoreState.running:
        warn_missing()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, warn_missing)
