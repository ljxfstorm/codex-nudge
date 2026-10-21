"""One fixed, measured backend request; no model fallback or retries."""

from http.client import HTTPException
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import uuid

from .transport import NoRedirect, TransportError, _decode, _http_error


ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
MODEL = "gpt-6-luna"
RECIPE = "codex-backend-lite-v1"
MAX_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 65536


class InferenceError(Exception):
    """Safe to display; usage may have occurred even when collection fails."""

    def __init__(self, message, *, http_status=None, content_type=None):
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.content_type = safe_content_type(content_type)
        if http_status is not None or content_type is not None:
            message += f" (HTTP {self.http_status or 'unknown'}, Content-Type {self.content_type or 'missing'})"
        super().__init__(message)


def safe_content_type(value):
    if value is None:
        return None
    media = value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    # Do not echo arbitrary header contents, parameters or identifiers.
    return media if media in {"text/event-stream", "application/json", "text/plain", "text/html",
                              "application/octet-stream"} else "other"


def payload():
    # Preserve the independently measured 18-token recipe, including its
    # compatibility fields. This is an observation, not a token ceiling.
    return {"model": MODEL, "input": [
        {"type": "additional_tools", "role": "developer", "tools": []},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Say OK."}]}
    ], "stream": True, "store": False, "reasoning": {
        "effort": "low", "summary": "auto", "context": "all_turns"},
        "text": {"verbosity": "low"}, "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": False, "tool_choice": "auto"}


def usage_counts(value):
    if not isinstance(value, dict):
        raise InferenceError("Completed response lacks valid token usage; no retry permitted.")
    result = {key: value.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")}
    if any(type(count) is not int or count < 0 for count in result.values()):
        raise InferenceError("Completed response lacks valid token usage; no retry permitted.")
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise InferenceError("Token usage totals disagree; no retry permitted.")
    for field, detail, key, bound in (
        ("cached_tokens", "input_tokens_details", "cached_tokens", "input_tokens"),
        ("reasoning_tokens", "output_tokens_details", "reasoning_tokens", "output_tokens"),
    ):
        details = value.get(detail)
        count = details.get(key) if isinstance(details, dict) else value.get(field)
        if count is not None and (type(count) is not int or not 0 <= count <= result[bound]):
            raise InferenceError("Token usage details are invalid; no retry permitted.")
        result[field] = count
    return result


def _event(data):
    try:
        event = _decode(data)
    except (ValueError, RecursionError):
        raise InferenceError("Invalid response event; no retry permitted.") from None
    kind = event.get("type")
    if kind in ("error", "response.failed", "response.incomplete"):
        raise InferenceError("Inference did not complete; no retry permitted.")
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") not in ("message", "reasoning"):
        raise InferenceError("Unexpected output item; no retry permitted.")
    if kind not in ("response.completed", "response.done"):
        return None
    response = event.get("response")
    if not isinstance(response, dict) or response.get("status") != "completed" or response.get("model") != MODEL:
        raise InferenceError("Inference status or model differs from the fixed recipe; no retry permitted.")
    output = response.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) or item.get("type") not in
                                         ("message", "reasoning") for item in output):
        raise InferenceError("Unexpected output items; no retry permitted.")
    # Output text, response IDs and encrypted reasoning never enter state/logs.
    return usage_counts(response.get("usage"))


def collect(response, timeout=45):
    deadline = time.monotonic() + timeout
    buffer, data, total = b"", [], 0
    while True:
        if time.monotonic() >= deadline:
            raise InferenceError("Response collection exceeded its time budget; no retry permitted.")
        chunk = response.read1(4096)
        if time.monotonic() >= deadline:
            raise InferenceError("Response collection exceeded its time budget; no retry permitted.")
        if not chunk:
            raise InferenceError("Stream ended without a complete response; no retry permitted.")
        total += len(chunk)
        if total > MAX_BYTES:
            raise InferenceError("Response exceeded its size limit; no retry permitted.")
        buffer += chunk
        if b"\n" not in buffer and len(buffer) > MAX_EVENT_BYTES:
            raise InferenceError("Response event exceeded its size limit; no retry permitted.")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if len(line) > MAX_EVENT_BYTES:
                raise InferenceError("Response event exceeded its size limit; no retry permitted.")
            if not line:
                if data:
                    raw = b"\n".join(data)
                    data = []
                    if raw == b"[DONE]":
                        raise InferenceError("Stream lacks completion usage; no retry permitted.")
                    result = _event(raw)
                    if result is not None:
                        return result
            elif line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
            if len(buffer) + sum(map(len, data)) > MAX_EVENT_BYTES:
                raise InferenceError("Response event exceeded its size limit; no retry permitted.")


def post_once(access_token, account_id):
    for value in (access_token, account_id):
        if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise InferenceError("Invalid authentication header; no request sent.")
    request = Request(ENDPOINT, data=json.dumps(payload(), separators=(",", ":")).encode(), method="POST", headers={
        "Authorization": "Bearer " + access_token, "ChatGPT-Account-Id": account_id,
        "Accept": "text/event-stream", "Content-Type": "application/json",
        # Pinned compatibility envelope used by the verified prototype. This
        # does not imply this tool is Codex or an officially supported client.
        "User-Agent": "codex_cli_rs/0.163.0", "Originator": "codex_cli_rs", "Version": "0.163.0",
        "session-id": str(uuid.uuid4()), "x-openai-internal-codex-responses-lite": "true"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type")
            # The verified backend Lite route can omit Content-Type. Missing
            # metadata is tolerated only for HTTP 200; the body must still pass
            # complete SSE, model, output-item and usage validation below.
            if response.status != 200 or (content_type is not None and safe_content_type(content_type) != "text/event-stream"):
                raise InferenceError("Inference response was unexpected; usage is unknown and no retry is permitted.",
                                     http_status=response.status, content_type=content_type)
            try:
                return collect(response)
            except InferenceError as error:
                raise InferenceError(str(error), http_status=response.status, content_type=content_type) from None
    except HTTPError as error:
        raise _http_error(error) from None
    except (URLError, OSError, HTTPException):
        raise TransportError("Inference transport failed; usage may have occurred. No retry sent.") from None
