"""The Västtrafik M34 integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, TypeAlias

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_JOURNEYS

if TYPE_CHECKING:
    from .sensor import VasttrafikDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# Type alias for config entry with runtime data
VasttrafikConfigEntry: TypeAlias = "ConfigEntry[VasttrafikDataUpdateCoordinator]"


async def async_setup_entry(hass: HomeAssistant, entry: VasttrafikConfigEntry) -> bool:
    """Set up Västtrafik M34 from a config entry."""
    # Validate that we have the required data
    is_journey = (
        entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_JOURNEYS
        or "origin_gid" in entry.data
    )
    has_location_data = (
        "origin_gid" in entry.data and "destination_gid" in entry.data
        if is_journey
        else "station_gid" in entry.data
    )
    if "auth_key" not in entry.data or not has_location_data:
        _LOGGER.error("Missing required data in config entry")
        return False
    
    # Test connection before setting up platforms
    auth_key = entry.data["auth_key"]
    
    try:
        # Test OAuth2 token retrieval
        async with aiohttp.ClientSession() as session:
            auth_header = f"Basic {auth_key}"
            
            async with session.post(
                "https://ext-api.vasttrafik.se/token",
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    _LOGGER.error("Failed to authenticate with Västtrafik API: %s", error_text)
                    raise ConfigEntryNotReady(f"Authentication failed: {response.status}")
                
                token_data = await response.json()
                if "access_token" not in token_data:
                    raise ConfigEntryNotReady("No access token in response")
        
        _LOGGER.info("Successfully authenticated with Västtrafik API")
        
    except aiohttp.ClientError as ex:
        _LOGGER.error("Network error while testing connection: %s", ex)
        raise ConfigEntryNotReady(f"Network error: {ex}") from ex
    except Exception as ex:
        _LOGGER.error("Failed to test Västtrafik M34 connection: %s", ex)
        raise ConfigEntryNotReady(f"Failed to connect: {ex}") from ex
    
    # Connection test passed, now set up platforms
    # The coordinator will be stored in runtime_data by sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VasttrafikConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
