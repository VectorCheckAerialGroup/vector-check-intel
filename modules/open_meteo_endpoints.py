"""
VECTOR CHECK AERIAL GROUP INC. — Open-Meteo Endpoint Resolver

Centralizes Open-Meteo URL construction so the entire codebase respects the
paid-subscription endpoint when an API key is configured.

When secrets.toml has:
    [open_meteo]
    api_key = "..."

…this module returns URLs against `customer-api.open-meteo.com` with
`apikey=` appended. Otherwise it falls back to the free `api.open-meteo.com`
endpoint (which is rate-limited, subject to IP blocks, and lower priority).

Usage from any module:
    from modules.open_meteo_endpoints import build_url, base_url
    url = build_url("ecmwf")   # → https://customer-api.open-meteo.com/v1/ecmwf?apikey=XYZ
"""

import logging
from typing import Optional

logger = logging.getLogger("arms.open_meteo")

FREE_BASE = "https://api.open-meteo.com"
CUSTOMER_BASE = "https://customer-api.open-meteo.com"
FREE_ARCHIVE_BASE = "https://archive-api.open-meteo.com"
CUSTOMER_ARCHIVE_BASE = "https://customer-archive-api.open-meteo.com"


def _get_api_key() -> Optional[str]:
    """Returns Open-Meteo API key from secrets.toml, or None."""
    try:
        import streamlit as st
        key = st.secrets["open_meteo"]["api_key"]
        if key and isinstance(key, str) and key.strip():
            return key.strip()
    except (KeyError, FileNotFoundError, AttributeError, ImportError):
        return None
    return None


def has_paid_subscription() -> bool:
    """True if Open-Meteo API key is configured."""
    return _get_api_key() is not None


def base_url() -> str:
    """Returns the appropriate forecast base URL: customer if paid, free otherwise."""
    return CUSTOMER_BASE if has_paid_subscription() else FREE_BASE


def archive_base_url() -> str:
    """Returns the appropriate ERA5 archive base URL: customer if paid, free otherwise.
    The archive API has its own hostname (archive-api.open-meteo.com) because it's
    served from different infrastructure (90+ TB of historical data, slower disk-
    backed query path) than the realtime forecast API.
    """
    return CUSTOMER_ARCHIVE_BASE if has_paid_subscription() else FREE_ARCHIVE_BASE


def append_apikey(url: str) -> str:
    """Appends apikey= to a URL when paid subscription is configured.

    Handles both URL forms — those with existing query params and those without.
    Idempotent: if apikey= is already in the URL, returns the URL unchanged.
    Safe to call on free-tier URLs: returns them unchanged when no API key.
    """
    key = _get_api_key()
    if not key:
        return url
    if "apikey=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}apikey={key}"


def build_url(model_slug: str, query_suffix: str = "") -> str:
    """Builds a full Open-Meteo forecast URL for the given model.

    Args:
        model_slug:    the path segment after /v1/ (e.g. "ecmwf", "gfs", "gem",
                       "dwd-icon", "bom"). May also include a query string for
                       multi-model endpoints like "gfs?models=ncep_hrrr_conus".
        query_suffix:  optional extra query params to append (rare; usually
                       caller adds latitude/longitude/hourly itself).

    Returns:
        Full URL with apikey appended when paid subscription is configured.
        Example: "https://customer-api.open-meteo.com/v1/ecmwf?apikey=ABC123"
    """
    url = f"{base_url()}/v1/{model_slug}"
    if query_suffix:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query_suffix}"
    return append_apikey(url)


def build_archive_url(endpoint: str = "archive", query_suffix: str = "") -> str:
    """Builds a full Open-Meteo ERA5 archive URL.

    Args:
        endpoint:      "archive" for ERA5 reanalysis (1940-present), or
                       "forecast" for historical archived forecasts (2017+
                       depending on model). Default "archive".
        query_suffix:  extra query params (caller appends lat/lon/hourly/dates).

    Returns:
        Full URL pointing at customer-archive-api when paid, archive-api otherwise.
        Example: "https://customer-archive-api.open-meteo.com/v1/archive?apikey=ABC"
    """
    url = f"{archive_base_url()}/v1/{endpoint}"
    if query_suffix:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query_suffix}"
    return append_apikey(url)
