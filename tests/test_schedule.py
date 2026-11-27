"""Defaults from a schedule helper's active block, and color."""

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    setup_test_component_platform,
)

from .recording_light import RecordingLight, make_lights
from homeassistant.components.light import ColorMode
from pytest_homeassistant_custom_component.common import MockConfigEntry

@pytest.fixture
def expected_lingering_timers() -> bool:
    """The schedule helper keeps a timer for its next block."""
    return True


# Friday 27 Nov 2026, UTC.
START = "2026-11-27 12:00:00+00:00"

SCHEDULE = {
    "schedule": {
        "evening": {
            "name": "Evening",
            "friday": [
                {"from": "10:00:00", "to": "14:00:00", "data": {"brightness": 55, "temperature": 3500}},
                {"from": "14:00:00", "to": "16:00:00", "data": {"brightness": 20, "color": "#8c00ff"}},
            ],
        }
    }
}


class NativeGroup(RecordingLight):
    """Like a Zigbee2MQTT group: one entity that lists its members."""

    @property
    def extra_state_attributes(self):
        return {"group_entities": ["light.ct_lamp", "light.color_bulb"]}


async def _setup(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    light_conf: dict,
    schedule_first: bool = True,
    extra: list | None = None,
):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    entities = make_lights() + (extra or [])
    setup_test_component_platform(hass, "light", entities)
    assert await async_setup_component(hass, "light", {"light": [{"platform": "test"}]})
    if schedule_first:
        assert await async_setup_component(hass, "schedule", SCHEDULE)
    assert await async_setup_component(hass, "light_defaults", {"light_defaults": light_conf})
    await hass.async_block_till_done()
    return {light.entity_id: light for light in entities}


async def call(hass, service, **data):
    await hass.services.async_call("light", service, data, blocking=True)
    await hass.async_block_till_done()


async def _move(hass, freezer, when):
    freezer.move_to(when)
    async_fire_time_changed(hass, dt_util.parse_datetime(when))
    await hass.async_block_till_done()


async def test_block_values_fill_turn_on(hass, freezer):
    lights = await _setup(
        hass, freezer, {"light.ct_lamp": {"brightness": 10, "temperature": 2000, "schedule": "schedule.evening"}}
    )
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    assert lights["light.ct_lamp"].calls[-1] == {"brightness": 140, "color_temp_kelvin": 3500}


async def test_block_color_beats_temperature(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {
            "all": {"temperature": 2700},
            "light.color_bulb": {"schedule": "schedule.evening"},
        },
    )
    await _move(hass, freezer, "2026-11-27 15:00:00+00:00")
    await call(hass, "turn_on", entity_id="light.color_bulb")
    call_ = lights["light.color_bulb"].calls[-1]
    assert call_["brightness"] == 51
    assert "color_temp_kelvin" not in call_
    assert call_["hs_color"] == pytest.approx((272.9, 100.0), abs=0.2)


async def test_schedule_off_uses_own_values(hass, freezer):
    lights = await _setup(
        hass, freezer, {"light.ct_lamp": {"brightness": 10, "temperature": 2200, "schedule": "schedule.evening"}}
    )
    await _move(hass, freezer, "2026-11-27 17:00:00+00:00")
    assert hass.states.get("schedule.evening").state == "off"
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    assert lights["light.ct_lamp"].calls[-1] == {"brightness": 26, "color_temp_kelvin": 2200}


async def test_block_change_updates_lights_that_are_on(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {
            "light.color_bulb": {"schedule": "schedule.evening"},
            "light.ct_lamp": {"schedule": "schedule.evening"},
        },
    )
    await call(hass, "turn_on", entity_id="light.color_bulb")
    assert lights["light.color_bulb"].calls[-1] == {"brightness": 140, "color_temp_kelvin": 3500}
    await _move(hass, freezer, "2026-11-27 14:00:01+00:00")
    call_ = lights["light.color_bulb"].calls[-1]
    assert call_["brightness"] == 51 and "hs_color" in call_
    # Off lights aren't turned on.
    assert lights["light.ct_lamp"].calls == []
    assert hass.states.get("light.ct_lamp").state == "off"


async def test_no_update_when_apply_on_change_off(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {"light.color_bulb": {"schedule": "schedule.evening", "apply_on_change": False}},
    )
    await call(hass, "turn_on", entity_id="light.color_bulb")
    await _move(hass, freezer, "2026-11-27 14:00:01+00:00")
    assert len(lights["light.color_bulb"].calls) == 1


async def test_already_on_light_not_touched_by_turn_on(hass, freezer):
    lights = await _setup(hass, freezer, {"light.ct_lamp": {"schedule": "schedule.evening"}})
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    assert lights["light.ct_lamp"].calls[-1] == {}


@pytest.mark.parametrize(
    ("raw", "rgb"),
    [
        ("#8c00ff", (140, 0, 255)),
        ("#8C00FF", (140, 0, 255)),
        ("140,0,255", (140, 0, 255)),
        ("140, 0, 255", (140, 0, 255)),
        ("8c00ff", None),
        ("140 0 255", None),
        ("nope", None),
        ("300,0,0", None),
    ],
)
def test_parse_color(raw, rgb):
    from custom_components.light_defaults.schedule import parse_color

    assert parse_color(raw) == rgb


BOUNDARY = "2026-11-27 14:00:01+00:00"


async def test_manual_change_kept_when_schedule_drives_nothing_on_the_light(hass, freezer):
    """The schedule on all lights is overridden by the lamp's own values, so a
    block change sends the lamp nothing and a manual dim stays."""
    lights = await _setup(
        hass,
        freezer,
        {
            "all": {"schedule": "schedule.evening"},
            "light.ct_lamp": {"brightness": 80, "temperature": 2700},
        },
    )
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    await call(hass, "turn_on", entity_id="light.ct_lamp", brightness_pct=5)
    count = len(lights["light.ct_lamp"].calls)
    await _move(hass, freezer, BOUNDARY)
    assert len(lights["light.ct_lamp"].calls) == count


async def test_only_changed_schedule_values_are_sent(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {"light.color_bulb": {"brightness": 80, "schedule": "schedule.evening"}},
    )
    await call(hass, "turn_on", entity_id="light.color_bulb")
    await _move(hass, freezer, BOUNDARY)
    call_ = lights["light.color_bulb"].calls[-1]
    assert call_["brightness"] == 51 and "hs_color" in call_


async def test_specific_temperature_beats_all_lights_schedule_color(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {
            "all": {"schedule": "schedule.evening"},
            "light.color_bulb": {"temperature": 2700},
        },
    )
    await _move(hass, freezer, "2026-11-27 15:00:00+00:00")
    await call(hass, "turn_on", entity_id="light.color_bulb")
    call_ = lights["light.color_bulb"].calls[-1]
    assert call_["color_temp_kelvin"] == 2700 and "hs_color" not in call_


async def test_white_only_light_falls_back_to_temperature_in_color_block(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {"all": {"temperature": 2700}, "light.ct_lamp": {"schedule": "schedule.evening"}},
    )
    await _move(hass, freezer, "2026-11-27 15:00:00+00:00")
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    assert lights["light.ct_lamp"].calls[-1] == {"brightness": 51, "color_temp_kelvin": 2700}


async def test_schedule_appearing_later_does_not_touch_lights(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {"light.ct_lamp": {"brightness": 30, "schedule": "schedule.evening"}},
        schedule_first=False,
    )
    await call(hass, "turn_on", entity_id="light.ct_lamp", brightness_pct=5)
    assert await async_setup_component(hass, "schedule", SCHEDULE)
    await hass.async_block_till_done()
    assert len(lights["light.ct_lamp"].calls) == 1


async def test_apply_on_change_is_per_entry(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {
            "light.color_bulb": {"schedule": "schedule.evening", "apply_on_change": False},
            "light.ct_lamp": {"schedule": "schedule.evening"},
        },
    )
    await call(hass, "turn_on", entity_id="light.color_bulb")
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    await _move(hass, freezer, BOUNDARY)
    assert len(lights["light.color_bulb"].calls) == 1
    assert len(lights["light.ct_lamp"].calls) == 2


async def test_running_effect_is_left_alone(hass, freezer):
    lights = await _setup(hass, freezer, {"light.ct_lamp": {"schedule": "schedule.evening"}})
    await call(hass, "turn_on", entity_id="light.ct_lamp", effect="candle")
    await _move(hass, freezer, BOUNDARY)
    assert len(lights["light.ct_lamp"].calls) == 1


async def test_block_end_reverts_lights_that_are_on(hass, freezer):
    lights = await _setup(
        hass,
        freezer,
        {"light.ct_lamp": {"brightness": 10, "temperature": 2200, "schedule": "schedule.evening"}},
    )
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    await _move(hass, freezer, "2026-11-27 16:00:01+00:00")
    assert lights["light.ct_lamp"].calls[-1] == {"brightness": 26, "color_temp_kelvin": 2200}


async def test_native_group_gets_one_command(hass, freezer):
    group = NativeGroup("Z2M Group", [ColorMode.COLOR_TEMP, ColorMode.HS])
    lights = await _setup(
        hass,
        freezer,
        {"light.z2m_group": {"schedule": "schedule.evening"}},
        extra=[group],
    )
    await call(hass, "turn_on", entity_id="light.z2m_group")
    await call(hass, "turn_on", entity_id="light.ct_lamp")
    await call(hass, "turn_on", entity_id="light.color_bulb")
    ct_calls = len(lights["light.ct_lamp"].calls)
    bulb_calls = len(lights["light.color_bulb"].calls)
    await _move(hass, freezer, BOUNDARY)
    assert len(lights["light.z2m_group"].calls) == 2
    assert len(lights["light.ct_lamp"].calls) == ct_calls
    assert len(lights["light.color_bulb"].calls) == bulb_calls


async def test_invalid_block_data_is_ignored(hass, freezer, caplog):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    entities = make_lights()
    setup_test_component_platform(hass, "light", entities)
    assert await async_setup_component(hass, "light", {"light": [{"platform": "test"}]})
    assert await async_setup_component(
        hass,
        "schedule",
        {"schedule": {"bad": {"name": "Bad", "friday": [
            {"from": "10:00:00", "to": "14:00:00", "data": {"brightness": "lots", "color": "purple", "temperature": 3000}},
        ]}}},
    )
    assert await async_setup_component(
        hass, "light_defaults", {"light_defaults": {"light.color_bulb": {"brightness": 40, "schedule": "schedule.bad"}}}
    )
    await hass.async_block_till_done()
    await call(hass, "turn_on", entity_id="light.color_bulb")
    assert entities[2].calls[-1] == {"brightness": 102, "color_temp_kelvin": 3000}
    assert "isn't 1–100" in caplog.text and "purple" in caplog.text


async def test_ui_entry_resubscribes_on_options_change(hass, freezer):
    lights = await _setup(hass, freezer, {})
    entry = MockConfigEntry(
        domain="light_defaults",
        title="Bulb",
        data={},
        options={"entities": ["light.color_bulb"], "schedule": "schedule.evening", "apply_on_change": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await call(hass, "turn_on", entity_id="light.color_bulb")
    await _move(hass, freezer, BOUNDARY)
    assert len(lights["light.color_bulb"].calls) == 2
    hass.config_entries.async_update_entry(entry, options={**entry.options, "apply_on_change": False})
    await hass.async_block_till_done()
    await _move(hass, freezer, "2026-11-27 16:00:01+00:00")
    assert len(lights["light.color_bulb"].calls) == 2
