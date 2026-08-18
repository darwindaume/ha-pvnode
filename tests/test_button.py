"""The manual refresh button."""

from __future__ import annotations

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import setup_integration


def _button(hass: HomeAssistant, entry_id: str) -> str:
    """Return the refresh button's entity id."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.unique_id == f"{entry_id}_refresh":
            return entity.entity_id
    raise AssertionError("refresh button missing")


async def test_pressing_the_button_fetches(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """Waiting out the slot is not an option right after a plan change."""
    await setup_integration(hass, config_entry)
    before = mock_api.async_get_forecast.call_count

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _button(hass, config_entry.entry_id)},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert mock_api.async_get_forecast.call_count > before


async def test_the_user_agent_identifies_the_client(
    hass: HomeAssistant, mock_api, config_entry
) -> None:
    """pvnode should be able to tell Home Assistant traffic apart in its access logs."""
    from custom_components.pvnode.coordinator import PvnodeApiClient

    await setup_integration(hass, config_entry)

    user_agent = PvnodeApiClient.call_args.kwargs["user_agent"]
    assert user_agent.startswith("ha-pvnode/")
    assert "HomeAssistant/" in user_agent
