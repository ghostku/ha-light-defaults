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

from .recording_light import make_lights

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


async def _setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, light_conf: dict):
    freezer.move_to(START)
    await hass.config.async_set_time_zone("UTC")
    entities = make_lights()
    setup_test_component_platform(hass, "light", entities)
    assert await async_setup_component(hass, "light", {"light": [{"platform": "test"}]})
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
    [("#8c00ff", (140, 0, 255)), ("8C00FF", (140, 0, 255)), ("140,0,255", (140, 0, 255)), ("140 0 255", (140, 0, 255)), ("nope", None), ("300,0,0", None)],
)
def test_parse_color(raw, rgb):
    from custom_components.light_defaults.schedule import parse_color

    assert parse_color(raw) == rgb
