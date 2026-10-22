import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from quota_window import transport


class TransportTests(unittest.TestCase):
    def test_untrusted_origins_and_header_injection_stop_before_network(self):
        with patch.object(transport, "build_opener") as build:
            for url in ("http://auth.openai.com/oauth/token", "https://example.test/token",
                        "https://auth.openai.com@evil.test/token", "https://auth.openai.com:444/token"):
                with self.subTest(url=url), self.assertRaises(transport.TransportError):
                    transport.request_json(url, access_token="SYNTHETIC_SECRET")
            with self.assertRaises(transport.TransportError):
                transport.request_json("https://chatgpt.com/backend-api/wham/usage", account_id="synthetic\r\ninjected")
            build.assert_not_called()

    def test_fixed_get_headers_and_no_redirect_handler(self):
        with patch.object(transport, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b'{"rate_limit":null}')
            result = transport.request_json("https://chatgpt.com/backend-api/wham/usage",
                access_token="SYNTHETIC_SECRET", account_id="synthetic-account")
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC_SECRET")
            self.assertIsNone(build.call_args.args[0].redirect_request(request, None, 302, "", {}, "https://example.test"))
        self.assertEqual(result, {"rate_limit": None})

    def test_token_post_preserves_form_encoding(self):
        with patch.object(transport, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(b'{"token_type":"Bearer"}')
            transport.request_json("https://auth.openai.com/oauth/token", body={"refresh_token": "a+b&c"}, form=True)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.data, b"refresh_token=a%2Bb%26c")
            self.assertEqual(request.get_method(), "POST")
            build.return_value.open.assert_called_once()

    def test_530_does_not_suggest_reauthorization_or_leak_error_body(self):
        error = HTTPError("https://auth.openai.com/oauth/token", 530, "SYNTHETIC_SECRET", {},
            io.BytesIO(json.dumps({"error": {"type": "cf_route_error", "message": "SYNTHETIC_SECRET"}}).encode()))
        with patch.object(transport, "build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(transport.TransportError) as caught:
                transport.request_json("https://auth.openai.com/oauth/token", body={"refresh_token": "SYNTHETIC_SECRET"})
            build.return_value.open.assert_called_once()
        self.assertEqual(caught.exception.status, 530)
        self.assertEqual(caught.exception.error_type, "cf_route_error")
        self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
        self.assertNotIn("reauthor", str(caught.exception).lower())

    def test_disconnect_after_post_is_never_replayed(self):
        with patch.object(transport, "build_opener") as build:
            build.return_value.open.side_effect = URLError("SYNTHETIC_SECRET")
            with self.assertRaisesRegex(transport.TransportError, "unknown") as caught:
                transport.request_json("https://auth.openai.com/oauth/token", body={"code": "SYNTHETIC_SECRET"})
            self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
            build.return_value.open.assert_called_once()

    def test_bad_or_oversized_json_is_rejected_without_body_disclosure(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'{"SYNTHETIC_SECRET":',
                     b"x" * (transport.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(data_length=len(data)), patch.object(transport, "build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(data)
                with self.assertRaises(transport.TransportError) as caught:
                    transport.request_json("https://auth.openai.com/.well-known/openid-configuration")
                self.assertNotIn("SYNTHETIC_SECRET", str(caught.exception))
                self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
