import unittest

from quota_window.quota import QuotaError, parse_snapshot


def window(seconds, used=0):
    return {"limit_window_seconds": seconds, "used_percent": used, "reset_at": 2000000000}


class QuotaTests(unittest.TestCase):
    def test_duration_identifies_five_hours_even_in_secondary_slot(self):
        result = parse_snapshot({"account_id": "synthetic", "rate_limit": {"allowed": True,
            "primary_window": window(604800, 20), "secondary_window": window(18000)}}, "synthetic")
        self.assertIs(result.account_matches, True)
        self.assertEqual(result.five_hour_window().slot, "secondary")
        self.assertEqual(result.five_hour_window().remaining_percent, 100)

    def test_weekly_or_other_bucket_never_substitutes_for_missing_five_hours(self):
        result = parse_snapshot({"rate_limit": {"primary_window": window(604800)},
            "additional_rate_limits": [{"metered_feature": "other", "rate_limit": {
                "primary_window": window(18000)}}]}, "synthetic")
        self.assertIsNone(result.five_hour_window())
        self.assertEqual(len(result.windows), 2)

    def test_missing_identity_or_permissions_remain_unknown(self):
        result = parse_snapshot({"rate_limit": {"primary_window": window(18000)}}, "synthetic")
        self.assertIsNone(result.account_matches)
        self.assertIsNone(result.ordinary_usage_allowed)
        missing = parse_snapshot({"rate_limit": None}, "synthetic")
        self.assertIsNone(missing.five_hour_window())

    def test_account_mismatch_and_exhaustion_are_preserved(self):
        result = parse_snapshot({"account_id": "other", "rate_limit": {"allowed": False,
            "primary_window": window(18000, 100)}}, "synthetic")
        self.assertIs(result.account_matches, False)
        self.assertIs(result.ordinary_usage_allowed, False)
        self.assertEqual(result.five_hour_window().remaining_percent, 0)

    def test_malformed_numbers_and_booleans_never_become_valid_windows(self):
        result = parse_snapshot({"rate_limit": {"allowed": 1, "primary_window": {
            "limit_window_seconds": True, "used_percent": float("nan"), "reset_at": -1}}}, "synthetic")
        self.assertIsNone(result.ordinary_usage_allowed)
        self.assertIsNone(result.five_hour_window())
        self.assertIsNone(result.windows[0].remaining_percent)
        self.assertIsNone(result.windows[0].resets_at)
        huge = parse_snapshot({"rate_limit": {"primary_window": {
            "limit_window_seconds": 10 ** 400, "used_percent": 10 ** 400, "reset_at": 10 ** 400}}}, "synthetic")
        self.assertIsNone(huge.five_hour_window())
        self.assertIsNone(huge.windows[0].used_percent)

    def test_duplicate_target_windows_are_ambiguous(self):
        result = parse_snapshot({"rate_limit": {"primary_window": window(18000),
            "secondary_window": window(18000)}}, "synthetic")
        with self.assertRaises(QuotaError):
            result.five_hour_window()

    def test_unrecognized_response_is_an_error_not_an_empty_success(self):
        for value in ({}, [], {"error": "synthetic"}):
            with self.subTest(value=value), self.assertRaises(QuotaError):
                parse_snapshot(value, "synthetic")


if __name__ == "__main__":
    unittest.main()
