"""Tests for Hive light groups exposed as a single light entity."""

# pylint: disable=protected-access

from unittest.mock import AsyncMock

import pytest
from apyhiveapi import Hive

_HTTP_OK = 200
_GROUP_ID = "group-0000-0000-0000-000000000001"


def _group_product(**overrides):
    """Return a light group product as it might appear in /nodes/all."""
    product = {
        "id": _GROUP_ID,
        "type": "warmwhitelight",
        "isGroup": True,
        "parent": "hub-0000-0000-0000-000000000001",
        "props": {"online": True},
        "state": {"name": "Kitchen Lights", "status": "ON", "brightness": 60},
    }
    product.update(overrides)
    return product


@pytest.fixture(name="hive")
async def fixture_hive():
    """File-mode Hive session with one light group added to its products."""
    async with Hive(username="use@file.com", password="x") as hive:
        await hive.start_session({})
        hive.session.data.products[_GROUP_ID] = _group_product()
        await hive.session.create_devices()
        hive.api.set_state = AsyncMock(
            return_value={"original": _HTTP_OK, "parsed": {}}
        )
        hive.session.refresh_after_change = AsyncMock(return_value=True)
        yield hive


def _group_entities(hive, entity_type):
    """Return the entities of *entity_type* that belong to the test group."""
    return [d for d in hive.device_list[entity_type] if d.hive_id == _GROUP_ID]


class TestLightGroupDiscovery:
    """create_devices adds a light group as one light entity."""

    async def test_group_is_one_light_entity(self, hive):
        """The group becomes a light and gets no diagnostic sensors."""
        lights = _group_entities(hive, "light")
        assert len(lights) == 1
        assert lights[0].is_group is True
        assert lights[0].device_id == _GROUP_ID
        assert _group_entities(hive, "sensor") == []

    async def test_group_has_device_info_home_assistant_needs(self, hive):
        """HA reads model, manufacturer and version when it builds the entity."""
        group = _group_entities(hive, "light")[0]
        assert group["deviceData"]["model"] == "Light group"
        assert group["deviceData"]["manufacturer"] == "Hive"
        assert group["deviceData"]["version"] == ""
        assert group["parentDevice"] == "hub-0000-0000-0000-000000000001"

    async def test_group_props_override_defaults(self):
        """Props reported by the group win over the filled-in defaults."""
        async with Hive(username="use@file.com", password="x") as hive:
            await hive.start_session({})
            device = hive.session.helper.get_light_group_device(
                _group_product(props={"model": "X", "online": False})
            )
        assert device["props"]["model"] == "X"
        assert device["props"]["online"] is False
        assert device["props"]["manufacturer"] == "Hive"

    async def test_member_lights_still_discovered(self, hive):
        """Individual lights keep their own entities alongside the group."""
        assert any(not d.is_group for d in hive.device_list["light"])


class TestLightGroupState:
    """get_light reads a group's state from its own product."""

    async def test_get_light_reads_group_product(self, hive):
        """State and brightness come from the group product."""
        group = await hive.light.get_light(_group_entities(hive, "light")[0])
        assert group.status == {"state": True, "brightness": int(0.6 * 255)}
        assert group["deviceData"]["online"] is True
        assert group.attributes == {"available": True}

    async def test_offline_group_is_unavailable(self, hive):
        """A group reporting offline is unavailable."""
        hive.session.data.products[_GROUP_ID]["props"]["online"] = False
        group = await hive.light.get_light(_group_entities(hive, "light")[0])
        assert group["deviceData"]["online"] is False
        assert group.attributes == {"available": False}

    async def test_deleted_group_goes_unavailable(self, hive):
        """A group removed in the Hive app goes unavailable, not an error."""
        group = _group_entities(hive, "light")[0]
        del hive.session.data.products[_GROUP_ID]
        result = await hive.light.get_light(group)
        assert result is group
        assert result["deviceData"]["online"] is False

    @pytest.mark.parametrize("hive_type", ["tuneablelight", "colourtuneablelight"])
    async def test_tuneable_group_reads_colour(self, hive, hive_type):
        """Tuneable and colour groups also report colour temperature."""
        group = _group_entities(hive, "light")[0]
        group.hive_type = hive_type
        hive.session.data.products[_GROUP_ID]["state"].update(
            {
                "colourTemperature": 4000,
                "colourMode": "COLOUR",
                "hue": 120,
                "saturation": 100,
                "value": 100,
            }
        )
        result = await hive.light.get_light(group)
        assert "color_temp" in result.status
        if hive_type == "colourtuneablelight":
            assert result.status["mode"] == "COLOUR"
            assert "hs_color" in result.status

    async def test_colour_group_in_white_mode_has_no_hs_color(self, hive):
        """A colour group in white mode reports a mode but no hs_color."""
        group = _group_entities(hive, "light")[0]
        group.hive_type = "colourtuneablelight"
        hive.session.data.products[_GROUP_ID]["state"].update(
            {"colourTemperature": 4000, "colourMode": "TUNABLE"}
        )
        result = await hive.light.get_light(group)
        assert result.status["mode"] == "TUNABLE"
        assert "hs_color" not in result.status

    async def test_cached_group_returned_on_slow_poll(self, hive):
        """The cache short-circuit still applies to groups."""
        group = _group_entities(hive, "light")[0]
        hive.session.should_use_cached_data = lambda: True
        hive.session.get_cached_device = lambda _d: "cached"
        assert await hive.light.get_light(group) == "cached"


class TestLightGroupControl:
    """Changing a group is a single request to the group's ID."""

    async def test_turn_on_posts_once_to_group(self, hive):
        """Turning the group on sends one set_state to the group product."""
        group = _group_entities(hive, "light")[0]
        assert await hive.light.turn_on(group, None, None, None) is True
        hive.api.set_state.assert_awaited_once_with(
            "productgroup", _GROUP_ID, status="ON"
        )

    async def test_turn_off_posts_once_to_group(self, hive):
        """Turning the group off sends one set_state to the group product."""
        group = _group_entities(hive, "light")[0]
        assert await hive.light.turn_off(group) is True
        hive.api.set_state.assert_awaited_once_with(
            "productgroup", _GROUP_ID, status="OFF"
        )

    async def test_brightness_posts_once_to_group(self, hive):
        """Setting brightness sends one set_state to the group product."""
        group = _group_entities(hive, "light")[0]
        assert await hive.light.turn_on(group, 40, None, None) is True
        hive.api.set_state.assert_awaited_once_with(
            "productgroup", _GROUP_ID, status="ON", brightness=40
        )
