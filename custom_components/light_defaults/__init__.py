"""Light Defaults: default brightness and color temperature for lights.

When a light is turned on from off (light.turn_on or light.toggle) and the call
leaves out brightness and/or color, the configured defaults fill the gap:

    light_defaults:
      all:                  # every light
        brightness: 100     # percent
      exclude:              # lights that never get defaults
        - light.sunroom
      light.office_lamp:
        temperature: 6500   # Kelvin; the brightness comes from `all`
      light.living_room_lamp:
        brightness: 80
        temperature: none   # no default temperature, whatever `all` says

- Anything in the call wins, per attribute: `brightness_pct: 100` on its own
  still gets the default temperature, and an explicit color keeps the default
  brightness.
- Lights that are already on, and calls with an effect or flash, are left alone.
- Each value comes from the most specific place that sets it: the light's own
  entry, then the configured groups it's in from smallest to largest, then
  `all`. `none` sets it to nothing.
- Excluded lights, and the lights in excluded groups, never get defaults.
  Neither do HA light groups: they pass the turn-on to their lights, and each
  of those gets its own.
- After editing the map, run the `light_defaults.reload` action. No restart needed.

Lights can also be set up in the UI (config_flow.py), where one config entry
gives one or more lights the same defaults, and one entry each holds the
defaults for all lights and the excluded lights. A light set up in the UI
ignores its YAML entry (and the UI's all lights entry ignores YAML's `all`),
and its entry follows the light when its entity ID changes.

How it works: Home Assistant's light integration already runs a per-light
"defaults" step on every turn-on and toggle (the hook behind light_profiles.csv).
This wraps that step, so it covers every path through HA: dashboards, voice,
HomeKit/Google, scenes, automations, areas and light groups. That step is
internal; if a future HA release changes it, setup fails with a logged error
and lights fall back to stock behavior.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_FLASH,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENTITIES,
    SERVICE_RELOAD,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "light_defaults"
CONF_ALL = "all"
CONF_BRIGHTNESS = "brightness"
CONF_EXCLUDE = "exclude"
CONF_TEMPERATURE = "temperature"
# The values an entry can set.
VALUES = (CONF_BRIGHTNESS, CONF_TEMPERATURE)
# A schedule helper whose active block's data (brightness, temperature, color)
# overrides the entry's own values while the schedule is on.
CONF_SCHEDULE = "schedule"
# Re-apply the schedule's values to lights that are on when its block changes.
CONF_APPLY_ON_CHANGE = "apply_on_change"
# Only from a schedule block: an RGB color, "#8c00ff" or "140,0,255". Schedule
# block data only holds plain values, so it's a string.
CONF_COLOR = "color"
SCHEDULE_DOMAIN = "schedule"

# Where the light integration keeps its profiles object (light.const.DATA_PROFILES).
PROFILES_KEY = "light_profiles"

# Where ZHA, WLED and MQTT groups (Zigbee2MQTT 2.10+) list their members
# (const.ATTR_GROUP_ENTITIES, which older HA doesn't have). HA light groups
# use the entity_id attribute instead.
ATTR_GROUP_ENTITIES = "group_entities"

# Color attributes as they look at the defaults step: color_name has already
# become rgb_color and any `profile:` has already been applied.
COLOR_ATTRS = (
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_XY_COLOR,
    ATTR_WHITE,
)


def _none(value: Any) -> None:
    """`none` (or an empty value): no default for this, whatever a group or
    `all` says."""
    if value is None or str(value).lower() == "none":
        return None
    raise vol.Invalid("expected a number or none")


def _value(low: int, high: int) -> vol.Any:
    # The number first, so an out-of-range number reports the range.
    return vol.Any(vol.All(vol.Coerce(int), vol.Range(min=low, max=high)), _none)


LIGHT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BRIGHTNESS): _value(1, 100),
        vol.Optional(CONF_TEMPERATURE): _value(1000, 10000),
        vol.Optional(CONF_SCHEDULE): cv.entity_domain(SCHEDULE_DOMAIN),
        vol.Optional(CONF_APPLY_ON_CHANGE, default=True): cv.boolean,
    }
)


def _parse_color(value: Any) -> tuple[int, int, int] | None:
    """An RGB color from a schedule block: "#8c00ff", "8c00ff" or "140,0,255"."""
    text = str(value).strip()
    if m := re.fullmatch(r"#?([0-9a-fA-F]{6})", text):
        return tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    parts = re.split(r"[\s,;]+", text.strip("[]() "))
    if len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    return None


def _schedule_values(hass: HomeAssistant, schedule_id: str) -> dict[str, Any]:
    """The active block's values, or nothing when the schedule is off. Values
    that don't parse are logged and left out."""
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
        if (color := _parse_color(attrs[CONF_COLOR])) is None:
            _LOGGER.warning("%s: color %r isn't #rrggbb or r,g,b, ignored", schedule_id, attrs[CONF_COLOR])
        else:
            values[CONF_COLOR] = color
            # A color replaces the temperature from anywhere less specific.
            values.setdefault(CONF_TEMPERATURE, None)
    return values

CONFIG_SCHEMA = vol.Schema(
    {
        # Own Schema so ALLOW_EXTRA below doesn't let non-light keys through.
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ALL): LIGHT_SCHEMA,
                # [cv.string] first: the entity ID validators would take a
                # mis-indented light entry's key as an excluded light.
                vol.Optional(CONF_EXCLUDE): vol.All(
                    cv.ensure_list, [cv.string], cv.entities_domain(LIGHT_DOMAIN)
                ),
                cv.entity_domain(LIGHT_DOMAIN): LIGHT_SCHEMA,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class LightDefaults:
    """Defaults from the YAML config and from UI config entries."""

    # Values (None for `none`) by entity ID, and by CONF_ALL for all lights.
    # A UI entry replaces the YAML one for the same key.
    yaml: dict[str, dict[str, int | None]] = field(default_factory=dict)
    entries: dict[str, dict[str, int | None]] = field(default_factory=dict)
    # Excluded lights; excluded in either place counts.
    yaml_excluded: set[str] = field(default_factory=set)
    entries_excluded: set[str] = field(default_factory=set)
    # Unsubscribes from the schedules in use.
    unsub_schedules: Any = None

    def load_yaml(self, conf: dict[str, Any]) -> None:
        conf = dict(conf)
        self.yaml_excluded = set(conf.pop(CONF_EXCLUDE, ()))
        self.yaml = conf

    def get(
        self, hass: HomeAssistant, entity_id: str, schedules: set[str] | None = None
    ) -> dict[str, Any]:
        """What to fill in for a light: each value from the most specific place
        that sets it. Nothing for excluded lights and HA light groups. A place
        with a schedule that's on uses the active block's values over its own.
        The schedules that were looked at go into `schedules`, if given."""
        excluded = self.yaml_excluded | self.entries_excluded
        if (
            entity_id in excluded
            or _is_light_group(hass, entity_id)
            or any(entity_id in _group_lights(hass, group_id) for group_id in excluded)
        ):
            return {}
        defaults = {**self.yaml, **self.entries}
        groups = sorted(
            (len(lights), group_id)
            for group_id in defaults.keys() - {CONF_ALL}
            if entity_id in (lights := _group_lights(hass, group_id))
        )
        # Least specific first, so later ones win: all lights, the groups from
        # largest to smallest, then the light's own entry.
        values: dict[str, Any] = {}
        for key in (CONF_ALL, *(group_id for _, group_id in reversed(groups)), entity_id):
            place = defaults.get(key, {})
            values.update({k: v for k, v in place.items() if k in VALUES})
            if schedule_id := place.get(CONF_SCHEDULE):
                if schedules is not None:
                    schedules.add(schedule_id)
                block = _schedule_values(hass, schedule_id)
                if CONF_TEMPERATURE in block and block[CONF_TEMPERATURE] is not None:
                    values.pop(CONF_COLOR, None)
                values.update(block)
        return {key: value for key, value in values.items() if value is not None}

    def schedules(self) -> dict[str, bool]:
        """Every schedule in use, and whether any place using it wants lights
        that are on updated when it changes."""
        found: dict[str, bool] = {}
        for place in (*self.yaml.values(), *self.entries.values()):
            if schedule_id := place.get(CONF_SCHEDULE):
                found[schedule_id] = found.get(schedule_id, False) or place.get(
                    CONF_APPLY_ON_CHANGE, True
                )
        return found

    def warn_overlap(self, keys: Iterable[str] | None = None) -> None:
        """Log what's set up twice, among keys if given: lights (or all lights)
        in both YAML and the UI, whose YAML entry is ignored, and excluded
        lights with defaults."""
        both = self.yaml.keys() & self.entries.keys()
        excluded = self.yaml_excluded | self.entries_excluded
        ignored = excluded & (self.yaml.keys() | self.entries.keys())
        if keys is not None:
            both &= set(keys)
            ignored &= set(keys)
        if both:
            _LOGGER.warning(
                "Set up both in YAML and in the UI, using the UI values: %s",
                ", ".join(sorted(both)),
            )
        if ignored:
            _LOGGER.warning(
                "Excluded, so their defaults are ignored: %s",
                ", ".join(sorted(ignored)),
            )


def _is_light_group(hass: HomeAssistant, entity_id: str) -> bool:
    """An HA light group. It hands the turn-on to its lights, where our values
    would arrive as explicit ones, so it gets none and they each get their own."""
    state = hass.states.get(entity_id)
    return state is not None and ATTR_ENTITY_ID in state.attributes


def _group_lights(hass: HomeAssistant, entity_id: str) -> set[str]:
    """The lights in a group, through nested groups; empty for a plain light."""
    found: set[str] = set()
    todo = [entity_id]
    while todo:
        if (state := hass.states.get(todo.pop())) is None:
            continue
        attrs = state.attributes
        members = attrs.get(ATTR_GROUP_ENTITIES) or attrs.get(ATTR_ENTITY_ID) or ()
        for member in members:
            if member != entity_id and member not in found:
                found.add(member)
                todo.append(member)
    return found


def _fill_defaults(light: dict[str, Any], params: dict[str, Any]) -> None:
    """Fill in what the call left out."""
    if CONF_BRIGHTNESS in light and ATTR_BRIGHTNESS not in params:
        params[ATTR_BRIGHTNESS] = round(255 * light[CONF_BRIGHTNESS] / 100)
    if not any(attr in params for attr in COLOR_ATTRS):
        if CONF_COLOR in light:
            params[ATTR_RGB_COLOR] = light[CONF_COLOR]
        elif CONF_TEMPERATURE in light:
            params[ATTR_COLOR_TEMP_KELVIN] = light[CONF_TEMPERATURE]


def _turn_on_data(light: dict[str, Any]) -> dict[str, Any]:
    """light.turn_on data that sets a light that's on to its values."""
    data: dict[str, Any] = {}
    _fill_defaults(light, data)
    return data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Wrap the light integration's per-light defaults step."""
    profiles = hass.data.get(PROFILES_KEY)
    original = getattr(profiles, "apply_default", None)
    if original is None:
        _LOGGER.error(
            "This Home Assistant version doesn't have the light defaults step "
            "this integration hooks into; light_defaults is inactive"
        )
        return False

    defaults = hass.data[DOMAIN] = LightDefaults()
    defaults.load_yaml(config.get(DOMAIN, {}))
    defaults.warn_overlap()

    @callback
    def apply_default(
        entity_id: str,
        state_on: bool | None,
        params: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            # Only for a light turning on from off, without an effect or flash.
            if not state_on and ATTR_EFFECT not in params and ATTR_FLASH not in params:
                _fill_defaults(defaults.get(hass, entity_id), params)
        except Exception:  # Never let this break light.turn_on.
            _LOGGER.exception("Couldn't apply defaults to %s", entity_id)
        # Any light_profiles.csv defaults still apply to whatever is left.
        original(entity_id, state_on, params, *args, **kwargs)

    profiles.apply_default = apply_default
    _track_schedules(hass, defaults)

    async def async_reload(call: ServiceCall) -> None:
        conf = await async_integration_yaml_config(hass, DOMAIN)
        if conf is None:  # Invalid config; HA has already logged why.
            return
        defaults.load_yaml(conf.get(DOMAIN, {}))
        _track_schedules(hass, defaults)
        _LOGGER.info(
            "Loaded YAML defaults for %d lights", len(defaults.yaml.keys() - {CONF_ALL})
        )
        defaults.warn_overlap()

    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, async_reload)

    @callback
    def entity_id_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Rename a light in its UI entry; the update listener does the rest."""
        old, new = event.data["old_entity_id"], event.data["entity_id"]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if old in (lights := entry.options.get(CONF_ENTITIES, ())):
                hass.config_entries.async_update_entry(
                    entry,
                    options={
                        **entry.options,
                        CONF_ENTITIES: [new if e == old else e for e in lights],
                    },
                )

    @callback
    def is_entity_id_change(data: er.EventEntityRegistryUpdatedData) -> bool:
        return "old_entity_id" in data

    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, entity_id_changed, is_entity_id_change
    )
    return True


@callback
def _track_schedules(hass: HomeAssistant, defaults: LightDefaults) -> None:
    """(Re)subscribe to the schedules in use. When one with apply on change
    moves to another block (or turns off or on), lights that are on and get
    values from it are set to their new values."""
    if defaults.unsub_schedules:
        defaults.unsub_schedules()
        defaults.unsub_schedules = None
    watched = [sid for sid, apply in defaults.schedules().items() if apply]
    if not watched:
        return

    async def schedule_changed(event: Event[EventStateChangedData]) -> None:
        schedule_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None or (
            old is not None
            and old.state == new.state
            and _schedule_block(old.attributes) == _schedule_block(new.attributes)
        ):
            return  # Only next_event moved, or nothing we use changed.
        for state in hass.states.async_all(LIGHT_DOMAIN):
            if state.state != STATE_ON:
                continue
            try:
                used: set[str] = set()
                light = defaults.get(hass, state.entity_id, used)
                if schedule_id not in used or not (data := _turn_on_data(light)):
                    continue
                await hass.services.async_call(
                    LIGHT_DOMAIN,
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: state.entity_id, **data},
                    blocking=False,
                )
            except Exception:  # One light's trouble mustn't stop the rest.
                _LOGGER.exception("Couldn't apply %s to %s", schedule_id, state.entity_id)

    defaults.unsub_schedules = async_track_state_change_event(hass, watched, schedule_changed)


def _schedule_block(attrs: Any) -> tuple:
    return tuple(attrs.get(key) for key in (CONF_BRIGHTNESS, CONF_TEMPERATURE, CONF_COLOR))


@callback
def _add_entry(defaults: LightDefaults, entry: ConfigEntry) -> None:
    """Add the entry's lights with their defaults, its defaults for all lights
    (unique ID CONF_ALL), or its excluded lights (unique ID CONF_EXCLUDE)."""
    # The keys to take out again when the entry changes or unloads.
    entry.runtime_data = (
        [CONF_ALL] if entry.unique_id == CONF_ALL else list(entry.options[CONF_ENTITIES])
    )
    if entry.unique_id == CONF_EXCLUDE:
        defaults.entries_excluded.update(entry.runtime_data)
    else:
        values = {
            key: entry.options[key]
            for key in (*VALUES, CONF_SCHEDULE, CONF_APPLY_ON_CHANGE)
            if key in entry.options
        }
        for key in entry.runtime_data:
            defaults.entries[key] = values
    defaults.warn_overlap(entry.runtime_data)


@callback
def _remove_entry(defaults: LightDefaults, entry: ConfigEntry) -> None:
    if entry.unique_id == CONF_EXCLUDE:
        defaults.entries_excluded.difference_update(entry.runtime_data)
    else:
        for key in entry.runtime_data:
            defaults.entries.pop(key, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add what the entry sets up; entry.options holds it."""
    _add_entry(hass.data[DOMAIN], entry)
    _track_schedules(hass, hass.data[DOMAIN])
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _remove_entry(hass.data[DOMAIN], entry)
    _add_entry(hass.data[DOMAIN], entry)
    _track_schedules(hass, hass.data[DOMAIN])


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _remove_entry(hass.data[DOMAIN], entry)
    _track_schedules(hass, hass.data[DOMAIN])
    return True
