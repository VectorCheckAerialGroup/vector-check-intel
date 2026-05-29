"""
VECTOR CHECK AERIAL GROUP INC. — Centralized HTTP Client

Provides a single retry-aware fetch helper used across all external API calls.

The previous pattern of `urllib.request.urlopen(...)` directly in every module
had no retry logic, so a single transient 5xx from any upstream produced a hard
dashboard failure. With multiple API providers (Open-Meteo, AviationWeather.gov,
Synoptic, NASA POWER, ECCC, and the upcoming Meteomatics integration), the
failure surface grows multiplicatively. This module gives every fetch the same
defensive behavior:

  - Exponential backoff on 502/503/504 and on network/timeout errors
  - Up to N retries (default 2 → max 3 attempts total)
  - Specific exception handling so real bugs (e.g. JSON parse errors)
    aren't silently swallowed
  - Optional Basic Auth for credentialed providers (Meteomatics)
  - Consistent User-Agent identification
"""

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger("arms.http")

DEFAULT_USER_AGENT = "VectorCheck-ARMS/2.6"
DEFAULT_TIMEOUT_S = 12.0
DEFAULT_MAX_RETRIES = 2     # 3 total attempts
RETRYABLE_HTTP_STATUS = {500, 502, 503, 504}


class HttpFetchError(Exception):
    """Raised when an HTTP fetch fails after all retry attempts."""
    def __init__(self, url: str, message: str, status: Optional[int] = None):
        super().__init__(f"{message} (url: {url[:120]})")
        self.url = url
        self.status = status
        self.message = message


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_MAX_RETRIES,
    headers: Optional[dict] = None,
    basic_auth: Optional[tuple] = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Fetches raw bytes from a URL with retry on transient failures.

    Args:
        url: full URL to GET
        timeout: per-attempt timeout in seconds
        retries: number of retry attempts after the first (so total = retries + 1)
        headers: optional extra request headers
        basic_auth: optional (username, password) tuple for HTTP Basic Auth
        user_agent: User-Agent header

    Returns:
        Raw response body bytes

    Raises:
        HttpFetchError: after all retries exhausted, or on non-retryable failure
    """
    req_headers = {"User-Agent": user_agent}
    if headers:
        req_headers.update(headers)
    if basic_auth:
        token = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        # Use Authorization header directly rather than the auth handler so
        # we avoid the 401-then-retry roundtrip that HTTPBasicAuthHandler does.
        import base64
        b64 = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        req_headers["Authorization"] = f"Basic {b64}"

    last_err: Optional[str] = None
    last_status: Optional[int] = None

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()

        except urllib.error.HTTPError as e:
            last_status = e.code
            last_err = f"HTTP {e.code} {e.reason}"
            if e.code in RETRYABLE_HTTP_STATUS and attempt < retries:
                _sleep_backoff(attempt)
                continue
            # Non-retryable HTTP error (4xx, or final attempt on 5xx) → give up
            raise HttpFetchError(url, last_err, status=e.code) from e

        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err = f"network: {e}"
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            raise HttpFetchError(url, last_err) from e

    # Should be unreachable, but keep mypy happy
    raise HttpFetchError(url, last_err or "fetch failed", status=last_status)


def fetch_json(url: str, **kwargs) -> dict:
    """Convenience: fetch + JSON decode. Raises HttpFetchError on any failure
    including JSON decode (rewrapped as fetch error for uniform handling).
    """
    body = fetch(url, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HttpFetchError(url, f"json decode: {e}") from e


def fetch_text(url: str, **kwargs) -> str:
    """Convenience: fetch + UTF-8 decode."""
    body = fetch(url, **kwargs)
    return body.decode("utf-8", errors="replace")


def _sleep_backoff(attempt: int) -> None:
    """Exponential backoff with a tiny jitter. attempt is 0-indexed."""
    # Attempt 0 → 0.4s, attempt 1 → 0.8s, attempt 2 → 1.6s, ...
    base = 0.4 * (2 ** attempt)
    time.sleep(base)
