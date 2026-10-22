"""Bounded OpenAI JSON requests with no redirects or automatic retries."""

from http.client import HTTPException
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BYTES = 4096
ALLOWED_HOSTS = frozenset({"auth.openai.com", "chatgpt.com"})


class TransportError(Exception):
    """Contains only status and whitelisted codes, never raw request/body data."""

    def __init__(self, message, *, status=None, error_code=None, error_type=None, retryable=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.error_type = error_type
        self.retryable = (status is None or status == 429 or 500 <= status <= 599) if retryable is None else retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("non-finite JSON number")


def _decode(data):
    value = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _http_error(error):
    fields = {}
    try:
        data = error.read(MAX_ERROR_BYTES + 1)
        detail = _decode(data).get("error") if len(data) <= MAX_ERROR_BYTES else None
        if isinstance(detail, dict):
            for key in ("code", "type"):
                value = detail.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                    fields["error_" + key] = value
    except (OSError, HTTPException, ValueError, RecursionError):
        pass
    finally:
        error.close()
    return TransportError(f"OpenAI HTTP {error.code}; no retry sent.", status=error.code, **fields)


def request_json(url, *, body=None, form=False, access_token=None, account_id=None, timeout=15):
    """One network attempt. Callers persist intent before one-time operations.

    Endpoint domains are fixed for the independently tested Codex-compatible
    route. Being allowlisted here is not a public API support guarantee.
    """
    try:
        parsed = urlsplit(url)
        allowed = (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                   and parsed.port in (None, 443) and not parsed.username and not parsed.password
                   and not parsed.fragment)
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        raise TransportError("Request endpoint is not an allowed OpenAI HTTPS origin.", retryable=False)
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or not 0 < timeout <= 60):
        raise TransportError("Request timeout must be between zero and 60 seconds.", retryable=False)
    headers = {"Accept": "application/json", "User-Agent": "quota-window-development/0"}
    for value, header in ((access_token, "Authorization"), (account_id, "ChatGPT-Account-Id")):
        if value is not None:
            if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise TransportError("Invalid authentication header value.", retryable=False)
            headers[header] = "Bearer " + value if header == "Authorization" else value
    try:
        data = None if body is None else (urlencode(body).encode() if form else
                                         json.dumps(body, allow_nan=False, separators=(",", ":")).encode())
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        request = Request(url, data=data, headers=headers, method="GET" if body is None else "POST")
    except (ValueError, TypeError, RecursionError):
        raise TransportError("Request arguments could not be encoded.", retryable=False) from None
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise _http_error(error) from None
    except (URLError, OSError, HTTPException):
        raise TransportError("Network request failed; no retry sent. Its outcome may be unknown.") from None
    if len(data) > MAX_RESPONSE_BYTES:
        raise TransportError("Response exceeded the size limit.", retryable=False)
    try:
        return _decode(data)
    except (ValueError, RecursionError):
        raise TransportError("Response was not a valid JSON object.", retryable=False) from None
