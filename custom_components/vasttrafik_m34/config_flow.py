"""Config flow for Västtrafik M34 integration."""
from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import CONF_ENTRY_TYPE, CONF_TRANSPORT_MODES, CONF_TRANSPORT_SUB_MODES, DOMAIN, ENTRY_TYPE_DEPARTURES, ENTRY_TYPE_JOURNEYS, TRANSPORT_MODE_OPTIONS, TRANSPORT_SUB_MODE_OPTIONS

_LOGGER = logging.getLogger(__name__)

# OAuth2 Configuration
TOKEN_URL = "https://ext-api.vasttrafik.se/token"
API_BASE = "https://ext-api.vasttrafik.se/pr/v4"


async def get_access_token(
    hass: HomeAssistant, auth_key: str
) -> tuple[str, int]:
    """Get OAuth2 access token from Västtrafik API.
    
    Args:
        hass: Home Assistant instance
        auth_key: Base64 encoded client_id:client_secret (Authentication Key from portal)
    
    Returns:
        Tuple of (access_token, expires_in_seconds)
    """
    headers = {
        "Authorization": f"Basic {auth_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    
    data = {"grant_type": "client_credentials"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, headers=headers, data=data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    _LOGGER.error("Token request failed: %s - %s", response.status, error_text)
                    raise CannotConnect(f"Failed to get access token: {response.status}")
                
                result = await response.json()
                return result["access_token"], result.get("expires_in", 86400)
    except aiohttp.ClientError as ex:
        _LOGGER.error("Network error during token request: %s", ex)
        raise CannotConnect(f"Network error: {ex}") from ex


async def validate_auth_key(hass: HomeAssistant, auth_key: str) -> bool:
    """Validate the authentication key by attempting to get an access token."""
    try:
        token, _ = await get_access_token(hass, auth_key)
        return token is not None and len(token) > 0
    except Exception as ex:
        _LOGGER.error("Failed to validate authentication key: %s", ex)
        return False


async def search_stations(
    hass: HomeAssistant, access_token: str, query: str
) -> list[dict[str, str]]:
    """Search for stations using the Västtrafik API v4.
    
    Args:
        hass: Home Assistant instance
        access_token: Valid OAuth2 Bearer token
        query: Search query string
    
    Returns:
        List of station dictionaries with 'gid', 'name', and 'type'
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    
    params = {
        "q": query,
        "limit": 10,
        "types": "stoparea",  # Only search for stop areas
    }
    
    url = f"{API_BASE}/locations/by-text"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 401:
                    raise InvalidAuth("Access token expired or invalid")
                if response.status != 200:
                    error_text = await response.text()
                    _LOGGER.error("Station search failed: %s - %s", response.status, error_text)
                    raise CannotConnect(f"Failed to search stations: {response.status}")
                
                result = await response.json()
                
                # Parse the results from API v4
                stations = []
                results_list = result.get("results", [])
                
                for location in results_list:
                    if location.get("locationType") == "stoparea":
                        stations.append({
                            "gid": location.get("gid"),
                            "name": location.get("name"),
                            "type": "StopArea",
                        })
                
                return stations
    except aiohttp.ClientError as ex:
        _LOGGER.error("Network error during station search: %s", ex)
        raise CannotConnect(f"Network error: {ex}") from ex


def _configuration_exists(
    hass: HomeAssistant,
    entry_type: str,
    *,
    station_gid: str | None = None,
    origin_gid: str | None = None,
    destination_gid: str | None = None,
    exclude_entry_id: str | None = None,
) -> bool:
    """Return whether the requested monitor is already configured."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        is_journey = (
            entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_JOURNEYS
            or "origin_gid" in entry.data
        )
        if entry_type == ENTRY_TYPE_JOURNEYS and is_journey:
            if (
                entry.data.get("origin_gid") == origin_gid
                and entry.data.get("destination_gid") == destination_gid
            ):
                return True
        elif entry_type == ENTRY_TYPE_DEPARTURES and not is_journey:
            if entry.data.get("station_gid") == station_gid:
                return True
    return False


class VasttrafikM34ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Västtrafik M34."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> VasttrafikM34OptionsFlow:
        """Create the quick settings flow shown by Home Assistant's cog."""
        return VasttrafikM34OptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._auth_key: str | None = None
        self._access_token: str | None = None
        self._stations: list[dict[str, str]] = []
        self._entry_type: str | None = None
        self._origin: dict[str, str] | None = None
        self._destination: dict[str, str] | None = None
        self._reconfigure_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - API credentials."""
        errors: dict[str, str] = {}
        
        # Check if we already have a valid auth_key from a previous entry
        existing_entries = self._async_current_entries()
        if existing_entries and not user_input:
            # Use auth_key from first existing entry
            existing_auth_key = existing_entries[0].data.get("auth_key")
            if existing_auth_key:
                try:
                    # Validate the existing auth key
                    is_valid = await validate_auth_key(self.hass, existing_auth_key)
                    if is_valid:
                        # Reuse existing auth key
                        self._auth_key = existing_auth_key
                        self._access_token, _ = await get_access_token(
                            self.hass, self._auth_key
                        )
                        # Skip to station search directly
                        return await self.async_step_mode()
                except Exception:
                    # If validation fails, continue to ask for auth_key
                    pass

        if user_input is not None:
            try:
                # Validate authentication key
                is_valid = await validate_auth_key(
                    self.hass,
                    user_input["auth_key"],
                )
                
                if not is_valid:
                    raise InvalidAuth("Invalid authentication key")
                
                # Store auth key and get token
                self._auth_key = user_input["auth_key"]
                self._access_token, _ = await get_access_token(
                    self.hass, self._auth_key
                )
                
                # Move to station search step
                return await self.async_step_mode()
                
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("auth_key"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "auth_key_url": "https://developer.vasttrafik.se/",
            },
        )

    async def async_step_mode(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose between a departure board and journey planning."""
        if user_input is not None:
            self._entry_type = user_input[CONF_ENTRY_TYPE]
            if self._entry_type == ENTRY_TYPE_JOURNEYS:
                return await self.async_step_origin()
            return await self.async_step_station()
        return self.async_show_form(
            step_id="mode",
            data_schema=vol.Schema({vol.Required(CONF_ENTRY_TYPE): vol.In({
                ENTRY_TYPE_DEPARTURES: "Avgångstavla för en hållplats",
                ENTRY_TYPE_JOURNEYS: "Reseplanering mellan två hållplatser",
            })}),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure an existing journey without creating a duplicate."""
        entry = self._get_reconfigure_entry()
        self._reconfigure_entry = entry
        self._auth_key = entry.data["auth_key"]
        self._access_token, _ = await get_access_token(self.hass, self._auth_key)
        if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_JOURNEYS:
            return await self.async_step_station()
        self._origin = {"gid": entry.data["origin_gid"], "name": entry.data["origin_name"]}
        self._destination = {"gid": entry.data["destination_gid"], "name": entry.data["destination_name"]}
        if user_input is not None:
            if user_input["Ändra start eller mål"]:
                return await self.async_step_origin()
            return await self.async_step_journey_options()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required("Ändra start eller mål", default=False): bool}
            ),
        )

    async def async_step_origin(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Search for, or accept the GID of, the journey origin."""
        return await self._async_step_journey_location("origin", user_input)

    async def async_step_destination(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Search for, or accept the GID of, the journey destination."""
        return await self._async_step_journey_location("destination", user_input)

    async def async_step_select_origin(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._async_step_select_journey_location("origin", user_input)

    async def async_step_select_destination(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._async_step_select_journey_location("destination", user_input)

    async def _async_step_journey_location(self, kind: str, user_input: dict[str, Any] | None) -> FlowResult:
        if user_input is not None:
            query = user_input["location"].strip()
            if query.isdigit() and len(query) == 16:
                location = {"gid": query, "name": query}
            else:
                matches = await search_stations(self.hass, self._access_token, query)
                if len(matches) == 1:
                    location = matches[0]
                else:
                    self._stations = matches
                    return await self._async_step_select_journey_location(kind)
            if kind == "origin":
                self._origin = location
                return await self.async_step_destination()
            self._destination = location
            return await self.async_step_journey_options()
        current_location = (
            (self._origin if kind == "origin" else self._destination) or {}
        )
        return self.async_show_form(
            step_id=kind,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "location", default=current_location.get("name", "")
                    ): str
                }
            ),
        )

    async def _async_step_select_journey_location(self, kind: str, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            location = next((item for item in self._stations if item["gid"] == user_input["location"]), None)
            if kind == "origin":
                self._origin = location
                return await self.async_step_destination()
            self._destination = location
            return await self.async_step_journey_options()
        return self.async_show_form(step_id=f"select_{kind}", data_schema=vol.Schema({vol.Required("location"): vol.In({item["gid"]: item["name"] for item in self._stations})}))

    async def async_step_journey_options(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        values = (
            {**self._reconfigure_entry.data, **self._reconfigure_entry.options}
            if self._reconfigure_entry
            else {}
        )
        if user_input is not None:
            transport_modes = list(user_input[CONF_TRANSPORT_MODES])
            if user_input[CONF_TRANSPORT_SUB_MODES] and "train" not in transport_modes:
                transport_modes.append("train")
            if self._reconfigure_entry:
                if _configuration_exists(
                    self.hass,
                    ENTRY_TYPE_JOURNEYS,
                    origin_gid=self._origin["gid"],
                    destination_gid=self._destination["gid"],
                    exclude_entry_id=self._reconfigure_entry.entry_id,
                ):
                    return self.async_abort(reason="already_configured")
                new_data = {
                    **self._reconfigure_entry.data,
                    "origin_gid": self._origin["gid"], "origin_name": self._origin["name"],
                    "destination_gid": self._destination["gid"], "destination_name": self._destination["name"],
                }
                new_options = {
                    **self._reconfigure_entry.options,
                    "journey_limit": user_input["journey_limit"],
                    CONF_TRANSPORT_MODES: transport_modes,
                    CONF_TRANSPORT_SUB_MODES: user_input[CONF_TRANSPORT_SUB_MODES],
                }
                for key in ("journey_limit", CONF_TRANSPORT_MODES, CONF_TRANSPORT_SUB_MODES):
                    new_data.pop(key, None)
                self.hass.config_entries.async_update_entry(
                    self._reconfigure_entry,
                    title=f"{self._origin['name']} → {self._destination['name']}",
                    data=new_data,
                    options=new_options,
                )
                await self.hass.config_entries.async_reload(self._reconfigure_entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")
            await self.async_set_unique_id(f"journey_{self._origin['gid']}_{self._destination['gid']}")
            if _configuration_exists(
                self.hass,
                ENTRY_TYPE_JOURNEYS,
                origin_gid=self._origin["gid"],
                destination_gid=self._destination["gid"],
            ):
                return self.async_abort(reason="already_configured")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=f"{self._origin['name']} → {self._destination['name']}", data={
                "auth_key": self._auth_key, CONF_ENTRY_TYPE: ENTRY_TYPE_JOURNEYS,
                "origin_gid": self._origin["gid"], "origin_name": self._origin["name"],
                "destination_gid": self._destination["gid"], "destination_name": self._destination["name"],
            }, options={
                "journey_limit": user_input["journey_limit"],
                CONF_TRANSPORT_MODES: transport_modes,
                CONF_TRANSPORT_SUB_MODES: user_input[CONF_TRANSPORT_SUB_MODES],
            })
        return self.async_show_form(step_id="journey_options", data_schema=vol.Schema({
            vol.Required("journey_limit", default=values.get("journey_limit", 3)): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Required(CONF_TRANSPORT_MODES, default=values.get(CONF_TRANSPORT_MODES, [])): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=k, label=v) for k, v in TRANSPORT_MODE_OPTIONS.items()], multiple=True)),
            vol.Required(CONF_TRANSPORT_SUB_MODES, default=values.get(CONF_TRANSPORT_SUB_MODES, [])): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=k, label=v) for k, v in TRANSPORT_SUB_MODE_OPTIONS.items()], multiple=True)),
        }))

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle station search step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if "Hållplatsnamn" in user_input and user_input["Hållplatsnamn"]:
                # User wants to search for stations
                try:
                    self._stations = await search_stations(
                        self.hass, self._access_token, user_input["Hållplatsnamn"]
                    )
                    
                    if not self._stations:
                        errors["base"] = "no_stations_found"
                    else:
                        # Show station selection
                        return await self.async_step_select_station()
                        
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception during station search")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "Hållplatsnamn",
                        default=(
                            self._reconfigure_entry.data.get("station_name", "")
                            if self._reconfigure_entry
                            else ""
                        ),
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "station_example": "e.g., 'Central', 'Brunnsparken', 'Järntorget'",
            },
        )

    async def async_step_select_station(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle station selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Find the selected station
            station_gid = user_input["station"]
            selected_station = next(
                (s for s in self._stations if s["gid"] == station_gid), None
            )
            
            if selected_station:
                if self._reconfigure_entry:
                    if _configuration_exists(
                        self.hass,
                        ENTRY_TYPE_DEPARTURES,
                        station_gid=selected_station["gid"],
                        exclude_entry_id=self._reconfigure_entry.entry_id,
                    ):
                        return self.async_abort(reason="already_configured")
                    self.hass.config_entries.async_update_entry(
                        self._reconfigure_entry,
                        title=selected_station["name"],
                        data={
                            **self._reconfigure_entry.data,
                            "station_gid": selected_station["gid"],
                            "station_name": selected_station["name"],
                        },
                    )
                    await self.hass.config_entries.async_reload(
                        self._reconfigure_entry.entry_id
                    )
                    return self.async_abort(reason="reconfigure_successful")
                # Check for duplicates - prevent same station being added twice
                if _configuration_exists(
                    self.hass,
                    ENTRY_TYPE_DEPARTURES,
                    station_gid=station_gid,
                ):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(f"vasttrafik_{station_gid}")
                self._abort_if_unique_id_configured()
                
                # Create the config entry
                return self.async_create_entry(
                    title=selected_station["name"],
                    data={
                        "auth_key": self._auth_key,
                        "station_gid": selected_station["gid"],
                        "station_name": selected_station["name"],
                    },
                )
            else:
                errors["base"] = "invalid_station"

        # Create station selector options
        station_options = {
            station["gid"]: station["name"]
            for station in self._stations
        }

        return self.async_show_form(
            step_id="select_station",
            data_schema=vol.Schema(
                {
                    vol.Required("station"): vol.In(station_options),
                }
            ),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class VasttrafikM34OptionsFlow(config_entries.OptionsFlow):
    """Full configuration flow opened from Home Assistant's cog."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._stations: list[dict[str, str]] = []
        self._origin: dict[str, str] | None = None
        self._destination: dict[str, str] | None = None

    def _label(self, swedish: str, english: str) -> str:
        """Return a visible field label in Home Assistant's configured language."""
        return swedish if self.hass.config.language.startswith("sv") else english

    async def _prepare(self) -> None:
        if self._access_token is None:
            self._access_token, _ = await get_access_token(
                self.hass, self.config_entry.data["auth_key"]
            )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose the monitoring type to configure."""
        await self._prepare()
        entry_type_label = self._label("Typ av övervakning", "Monitoring type")
        if user_input is not None:
            if user_input[entry_type_label] == ENTRY_TYPE_DEPARTURES:
                return await self.async_step_station()
            if self.config_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_JOURNEYS:
                return await self.async_step_origin()
            return await self.async_step_journey_action()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    entry_type_label,
                    default=self.config_entry.data.get(
                        CONF_ENTRY_TYPE, ENTRY_TYPE_DEPARTURES
                    ),
                ): vol.In({
                    ENTRY_TYPE_DEPARTURES: self._label(
                        "Avgångstavla för en hållplats", "Departure board for a stop"
                    ),
                    ENTRY_TYPE_JOURNEYS: self._label(
                        "Reseplanering mellan två hållplatser",
                        "Journey planning between two stops",
                    ),
                }),
            }),
        )

    async def async_step_journey_action(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose whether to edit the route or journey display settings."""
        action_label = self._label("Vad vill du ändra?", "What do you want to change?")
        change_route = "route"
        change_settings = "settings"
        if user_input is not None:
            if user_input[action_label] == change_route:
                return await self.async_step_origin()
            return await self.async_step_journey_options()
        return self.async_show_form(
            step_id="journey_action",
            data_schema=vol.Schema({
                vol.Required(action_label): vol.In({
                    change_route: self._label(
                        "Start eller mål", "Origin or destination"
                    ),
                    change_settings: self._label(
                        "Antal turer eller transportsätt",
                        "Number of trips or transport modes",
                    ),
                })
            }),
        )

    async def async_step_station(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        label = self._label("Hållplatsnamn eller GID", "Stop name or GID")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._stations = await search_stations(
                    self.hass, self._access_token, user_input[label]
                )
                if self._stations:
                    return await self.async_step_select_station()
                errors["base"] = "no_stations_found"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema({
                vol.Required(label, default=self.config_entry.data.get("station_name", "")): str
            }),
            errors=errors,
        )

    async def async_step_select_station(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        label = self._label("Välj hållplats", "Choose stop")
        if user_input is not None:
            selected = next(item for item in self._stations if item["gid"] == user_input[label])
            if _configuration_exists(
                self.hass,
                ENTRY_TYPE_DEPARTURES,
                station_gid=selected["gid"],
                exclude_entry_id=self.config_entry.entry_id,
            ):
                return self.async_abort(reason="already_configured")
            data = dict(self.config_entry.data)
            for key in ("origin_gid", "origin_name", "destination_gid", "destination_name", "journey_limit", CONF_TRANSPORT_MODES, CONF_TRANSPORT_SUB_MODES):
                data.pop(key, None)
            data.update({CONF_ENTRY_TYPE: ENTRY_TYPE_DEPARTURES, "station_gid": selected["gid"], "station_name": selected["name"]})
            options = dict(self.config_entry.options)
            for key in ("journey_limit", CONF_TRANSPORT_MODES, CONF_TRANSPORT_SUB_MODES):
                options.pop(key, None)
            self.hass.config_entries.async_update_entry(
                self.config_entry, title=selected["name"], data=data, options=options
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="select_station", data_schema=vol.Schema({vol.Required(label): vol.In({item["gid"]: item["name"] for item in self._stations})}))

    async def async_step_origin(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._location_step("origin", user_input)

    async def async_step_destination(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._location_step("destination", user_input)

    async def _location_step(self, kind: str, user_input: dict[str, Any] | None) -> FlowResult:
        current = self.config_entry.data.get(f"{kind}_name", "")
        errors: dict[str, str] = {}
        label = self._label(
            "Starthållplats eller GID" if kind == "origin" else "Målhållplats eller GID",
            "Departure stop name or GID" if kind == "origin" else "Destination stop name or GID",
        )
        if user_input is not None:
            query = user_input[label].strip()
            if query.isdigit() and len(query) == 16:
                location = {"gid": query, "name": query}
            else:
                try:
                    self._stations = await search_stations(self.hass, self._access_token, query)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                    self._stations = []
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                    self._stations = []
                if not self._stations:
                    errors["base"] = errors.get("base", "no_stations_found")
                elif len(self._stations) != 1:
                    return await self._select_location(kind)
                else:
                    location = self._stations[0]
            if errors:
                return self.async_show_form(
                    step_id=kind,
                    data_schema=vol.Schema({vol.Required(label, default=current): str}),
                    errors=errors,
                )
            if kind == "origin":
                self._origin = location
                return await self.async_step_destination()
            self._destination = location
            return await self.async_step_journey_options()
        return self.async_show_form(
            step_id=kind,
            data_schema=vol.Schema({
                vol.Required(
                    label,
                    default=current,
                    description={"description": self._label(
                        "Skriv namn eller Västtrafiks 16-siffriga GID.",
                        "Enter a name or Västtrafik's 16-digit GID.",
                    )},
                ): str
            }),
        )

    async def _select_location(self, kind: str, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            label = self._label(
                "Välj starthållplats" if kind == "origin" else "Välj målhållplats",
                "Choose departure stop" if kind == "origin" else "Choose destination stop",
            )
            location = next(item for item in self._stations if item["gid"] == user_input[label])
            if kind == "origin":
                self._origin = location
                return await self.async_step_destination()
            self._destination = location
            return await self.async_step_journey_options()
        label = self._label(
            "Välj starthållplats" if kind == "origin" else "Välj målhållplats",
            "Choose departure stop" if kind == "origin" else "Choose destination stop",
        )
        return self.async_show_form(step_id=f"select_{kind}", data_schema=vol.Schema({vol.Required(label): vol.In({item["gid"]: item["name"] for item in self._stations})}))

    async def async_step_select_origin(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._select_location("origin", user_input)

    async def async_step_select_destination(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self._select_location("destination", user_input)

    async def async_step_journey_options(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        values = {**self.config_entry.data, **self.config_entry.options}
        limit_label = self._label("Antal turer", "Number of trips")
        modes_label = self._label("Transportsätt", "Transport modes")
        sub_modes_label = self._label("Tågsort", "Train type")
        mode_options = {
            "tram": self._label("Spårvagn", "Tram"),
            "bus": self._label("Buss", "Bus"),
            "ferry": self._label("Färja", "Ferry"),
            "train": self._label("Tåg", "Train"),
        }
        sub_mode_options = {
            "vasttagen": self._label("Västtågen", "Västtågen"),
            "regionaltrain": self._label("Regionaltåg", "Regional train"),
            "longdistancetrain": self._label("Fjärrtåg", "Long-distance train"),
        }
        if user_input is not None:
            modes = list(user_input[modes_label])
            if user_input[sub_modes_label] and "train" not in modes:
                modes.append("train")
            origin = self._origin or {"gid": values["origin_gid"], "name": values["origin_name"]}
            destination = self._destination or {"gid": values["destination_gid"], "name": values["destination_name"]}
            data = dict(self.config_entry.data)
            data.pop("station_gid", None)
            data.pop("station_name", None)
            if _configuration_exists(
                self.hass,
                ENTRY_TYPE_JOURNEYS,
                origin_gid=origin["gid"],
                destination_gid=destination["gid"],
                exclude_entry_id=self.config_entry.entry_id,
            ):
                return self.async_abort(reason="already_configured")
            for key in ("journey_limit", CONF_TRANSPORT_MODES, CONF_TRANSPORT_SUB_MODES):
                data.pop(key, None)
            options = dict(self.config_entry.options)
            options.update({"journey_limit": user_input[limit_label], CONF_TRANSPORT_MODES: modes, CONF_TRANSPORT_SUB_MODES: user_input[sub_modes_label]})
            data.update({CONF_ENTRY_TYPE: ENTRY_TYPE_JOURNEYS, "origin_gid": origin["gid"], "origin_name": origin["name"], "destination_gid": destination["gid"], "destination_name": destination["name"]})
            self.hass.config_entries.async_update_entry(self.config_entry, title=f"{origin['name']} → {destination['name']}", data=data, options=options)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="journey_options", data_schema=vol.Schema({vol.Required(limit_label, default=values.get("journey_limit", 3)): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)), vol.Required(modes_label, default=values.get(CONF_TRANSPORT_MODES, [])): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=k, label=v) for k, v in mode_options.items()], multiple=True)), vol.Required(sub_modes_label, default=values.get(CONF_TRANSPORT_SUB_MODES, [])): selector.SelectSelector(selector.SelectSelectorConfig(options=[selector.SelectOptionDict(value=k, label=v) for k, v in sub_mode_options.items()], multiple=True))}))
