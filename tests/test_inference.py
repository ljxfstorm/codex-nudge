from email.message import Message
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from quota_window import inference
from quota_window.transport import TransportError


def terminal(**overrides):
    response = {"status": "completed", "model": inference.MODEL,
                "usage": {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18,
                          "input_tokens_details": {"cached_tokens": 3},
                          "output_tokens_details": {"reasoning_tokens": 0}},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "PRIVATE_REPLY"}]}]}
    response.update(overrides)
    return {"type": "response.done", "response": response}


def stream(event):
    response = io.BytesIO(("event: response.done\r\ndata: " + json.dumps(event) + "\r\n\r\n").encode())
    response.status = 200
    response.headers = Message()
    response.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return response


class InferenceTests(unittest.TestCase):
    def test_fixed_recipe_and_one_post_have_no_history_or_tool_definitions(self):
        with patch.object(inference, "build_opener") as build:
            build.return_value.open.return_value = stream(terminal())
            usage = inference.post_once("SYNTHETIC_ACCESS", "synthetic-account")
        build.return_value.open.assert_called_once()
        request = build.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, inference.ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertEqual(body["reasoning"]["effort"], "low")
        self.assertEqual(len(body["input"]), 2)
        self.assertEqual(body["input"][0]["tools"], [])
        self.assertEqual(body["input"][1]["content"][0]["text"], "Say OK.")
        self.assertFalse(body["store"])
        self.assertEqual(usage["total_tokens"], 18)
        self.assertEqual(usage["cached_tokens"], 3)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertNotIn("PRIVATE_REPLY", json.dumps(usage))
        self.assertIsNone(build.call_args.args[0].redirect_request(request, None, 302, "", {}, "https://evil.test"))

    def test_both_terminal_event_spellings_are_accepted(self):
        for kind in ("response.done", "response.completed"):
            event = terminal()
            event["type"] = kind
            self.assertEqual(inference.collect(stream(event))["total_tokens"], 18)

    def test_fragmented_multiline_sse_is_collected_without_retaining_content(self):
        class Fragments(io.BytesIO):
            def read1(self, size):
                return super().read1(min(size, 7))

        data = b": ping\r\n\r\n" + b"\r\n".join(
            b"data: " + line for line in json.dumps(terminal(), indent=2).encode().splitlines()) + b"\r\n\r\n"
        self.assertEqual(inference.collect(Fragments(data))["total_tokens"], 18)

    def test_incomplete_model_switch_and_tools_are_not_success(self):
        for event in (terminal(status="incomplete"), terminal(model="different"),
                      terminal(output=[{"type": "function_call"}]), {"type": "response.failed"},
                      {"type": "response.output_item.added", "item": {"type": "function_call"}}):
            with self.subTest(event=event), self.assertRaises(inference.InferenceError):
                inference.collect(stream(event))

    def test_missing_boolean_negative_and_inconsistent_usage_fail_closed(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 5, "total_tokens": 6},
                      {"input_tokens": -1, "output_tokens": 5, "total_tokens": 4},
                      {"input_tokens": 13, "output_tokens": 5, "total_tokens": 19},
                      {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18,
                       "input_tokens_details": {"cached_tokens": 14}}):
            with self.subTest(usage=usage), self.assertRaises(inference.InferenceError):
                inference.collect(stream(terminal(usage=usage)))

    def test_normalized_usage_retains_optional_details_after_persistence(self):
        usage = inference.collect(stream(terminal()))
        self.assertEqual(inference.usage_counts(json.loads(json.dumps(usage))), usage)
        unknown = inference.usage_counts({"input_tokens": 13, "output_tokens": 5, "total_tokens": 18})
        self.assertIsNone(unknown["cached_tokens"])

    def test_eof_invalid_json_and_oversized_events_are_bounded(self):
        for data in (b"", b"data: [DONE]\n\n", b"data: {invalid}\n\n",
                     b"data: " + b"x" * (inference.MAX_EVENT_BYTES + 1),
                     b"data: {\"type\":\"response.done\",\"type\":\"other\"}\n\n"):
            with self.subTest(length=len(data)), self.assertRaises(inference.InferenceError):
                inference.collect(io.BytesIO(data))
        with patch.object(inference, "MAX_BYTES", 1), self.assertRaises(inference.InferenceError):
            inference.collect(stream(terminal()))
        with patch.object(inference.time, "monotonic", side_effect=[100, 146]), \
                self.assertRaises(inference.InferenceError):
            inference.collect(stream(terminal()))

    def test_http_errors_disconnects_and_invalid_headers_never_retry_or_leak(self):
        for error in (URLError("PRIVATE_SECRET"), HTTPError(inference.ENDPOINT, 530, "private", {},
                                                         io.BytesIO(b"PRIVATE_SECRET"))):
            with patch.object(inference, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(TransportError) as caught:
                    inference.post_once("SYNTHETIC_ACCESS", "synthetic-account")
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
                build.return_value.open.assert_called_once()
        with patch.object(inference, "build_opener") as build, self.assertRaises(inference.InferenceError):
            inference.post_once("bad\nheader", "synthetic-account")
        build.assert_not_called()

    def test_unexpected_response_retains_safe_metadata_without_consuming_body(self):
        for status, content_type in ((200, "application/json"), (202, "text/event-stream"),
                                     (200, "text/html"), (200, None)):
            response = stream(terminal())
            response.status = status
            del response.headers["Content-Type"]
            if content_type is not None:
                response.headers["Content-Type"] = content_type
            response.read1 = lambda *_: self.fail("Unexpected body must not be consumed or logged")
            with self.subTest(status=status, content_type=content_type), \
                    patch.object(inference, "build_opener") as build:
                build.return_value.open.return_value = response
                with self.assertRaises(inference.InferenceError) as caught:
                    inference.post_once("SYNTHETIC_ACCESS", "synthetic-account")
                self.assertEqual(getattr(caught.exception, "http_status", None), status)
                self.assertEqual(getattr(caught.exception, "content_type", None), content_type)
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn("PRIVATE_REPLY", str(caught.exception))
                build.return_value.open.assert_called_once()

    def test_response_metadata_redacts_header_parameters_and_unknown_types(self):
        for raw, expected in (("application/json; secret=PRIVATE_SECRET", "application/json"),
                              ("application/PRIVATE_SECRET", "other"), ("PRIVATE_SECRET\r\n", "other")):
            response = stream(terminal())
            response.headers.replace_header("Content-Type", raw)
            with patch.object(inference, "build_opener") as build:
                build.return_value.open.return_value = response
                with self.assertRaises(inference.InferenceError) as caught:
                    inference.post_once("SYNTHETIC_ACCESS", "synthetic-account")
                self.assertEqual(caught.exception.content_type, expected)
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))

    def test_stream_parse_failure_preserves_http_metadata_without_body_disclosure(self):
        response = stream(terminal(model="PRIVATE_MODEL"))
        with patch.object(inference, "build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(inference.InferenceError) as caught:
                inference.post_once("SYNTHETIC_ACCESS", "synthetic-account")
            self.assertEqual(caught.exception.http_status, 200)
            self.assertEqual(caught.exception.content_type, "text/event-stream")
            self.assertNotIn("PRIVATE_MODEL", str(caught.exception))
            self.assertNotIn("PRIVATE_REPLY", str(caught.exception))
