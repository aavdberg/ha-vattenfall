"""Constants for the unsupported Mijn Vattenfall portal integration."""

from datetime import timedelta
from zoneinfo import ZoneInfo

DOMAIN = "vattenfall"
CONTRACTS_URL = "https://api.vattenfall.nl/api/mvf/contracts/v1/contracts"
PORTAL_URL = (
    "https://www.vattenfall.nl/service/mijn-vattenfall/vf/direct-regelen/contract"
)
CONF_ACCESS_TOKEN = "access_token"
CONF_SUBSCRIPTION_KEY = "subscription_key"
CONF_VF_CONTEXT = "vf_context"
UPDATE_INTERVAL = timedelta(hours=1)
CONTRACT_TIMEZONE = ZoneInfo("Europe/Amsterdam")
