"""Constants for the Västtrafik M34 integration."""

DOMAIN = "vasttrafik_m34"
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_DEPARTURES = "departures"
ENTRY_TYPE_JOURNEYS = "journeys"
CONF_TRANSPORT_MODES = "transport_modes"
CONF_TRANSPORT_SUB_MODES = "transport_sub_modes"
TRANSPORT_MODE_OPTIONS = {"tram": "Spårvagn", "bus": "Buss", "ferry": "Färja", "train": "Tåg"}
TRANSPORT_SUB_MODE_OPTIONS = {"vasttagen": "Västtågen", "regionaltrain": "Regionaltåg", "longdistancetrain": "Fjärrtåg"}
