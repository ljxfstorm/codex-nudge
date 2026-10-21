import copy
import unittest

from quota_window.quota import QuotaError, Snapshot, Window
from quota_window.schedule import Schedule, ScheduleError


BINDING = "a" * 64
USAGE = {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18, "cached_tokens": 3, "reasoning_tokens": 0}


def snapshot(now, *, reset=None, used=0, allowed=True, account=True, bucket="codex", duration=18000):
    return Snapshot(account, allowed, (Window(bucket, "primary", duration, now + duration if reset is None else reset, used),))


class ScheduleTests(unittest.TestCase):
    def ready(self):
        engine = Schedule(None, BINDING)
        self.assertEqual(engine.observe(snapshot(100), 100, 100), "confirming_reset")
        self.assertEqual(engine.observe(snapshot(102), 102, 102), "idle_window")
        return engine

    def completed(self):
        engine = self.ready()
        self.assertTrue(engine.prepare(snapshot(102), 102, 102))
        engine.completed(USAGE, 103)
        return engine

    def test_active_window_respects_boundary_and_grace_then_confirms_idle(self):
        engine = Schedule(None, BINDING)
        self.assertEqual(engine.observe(snapshot(100, reset=1000, used=20), 100, 100), "active_window")
        self.assertEqual(engine.delay(999), 6)
        self.assertEqual(engine.observe(snapshot(1001), 1001, 1001), "await_boundary")
        self.assertEqual(engine.delay(1001), 4)
        self.assertEqual(engine.observe(snapshot(1005), 1005, 1005), "confirming_reset")
        self.assertEqual(engine.observe(snapshot(1007), 1007, 1007), "idle_window")

    def test_full_stable_reset_and_tiny_external_use_are_active(self):
        engine = Schedule(None, BINDING)
        engine.observe(snapshot(100), 100, 100)
        self.assertEqual(engine.observe(snapshot(102, reset=18100), 102, 102), "stable_window")
        self.assertEqual(engine.data["window"]["deadline"], 18100)
        engine.observe(snapshot(400), 400, 400)
        self.assertEqual(engine.observe(snapshot(402), 402, 402), "idle_window")
        self.assertFalse(engine.prepare(snapshot(404, reset=18402), 404, 404))

    def test_slow_reads_do_not_misclassify_idle_as_active(self):
        engine = Schedule(None, BINDING)
        engine.observe(snapshot(100), 100, 120)
        self.assertEqual(engine.observe(snapshot(122), 122, 145), "idle_window")

    def test_stale_past_reset_and_single_sample_never_send(self):
        engine = Schedule(None, BINDING)
        self.assertEqual(engine.observe(snapshot(100, reset=90), 100, 100), "await_refresh")
        self.assertEqual(engine.delay(100), 30)
        with self.assertRaises(ScheduleError):
            engine.prepare(snapshot(100), 100, 100)
        engine.observe(snapshot(101), 101, 101)
        engine.observe(snapshot(101.5), 101.5, 101.5)
        with self.assertRaises(ScheduleError):
            engine.prepare(snapshot(101.5), 101.5, 101.5)

    def test_final_preflight_cancels_for_activity_permission_and_stale_evidence(self):
        for sample, started in ((snapshot(102, used=1), 102), (snapshot(102, allowed=False), 102),
                                (snapshot(102, allowed=None), 102), (snapshot(140), 140),
                                (snapshot(104, reset=18102), 104)):
            with self.subTest(sample=sample):
                engine = self.ready()
                self.assertFalse(engine.prepare(sample, started, started))
                self.assertIsNone(engine.data["attempt"])

    def test_post_request_fixed_reset_is_verified_even_at_100_percent(self):
        engine = self.completed()
        self.assertEqual(engine.delay(103), 5)
        self.assertEqual(engine.observe(snapshot(107, reset=18102), 107, 107), "await_verification")
        self.assertEqual(engine.observe(snapshot(108, reset=18102), 108, 108), "verification_sample")
        self.assertEqual(engine.delay(108), 30)
        restored = Schedule(copy.deepcopy(engine.data), BINDING)
        self.assertEqual(restored.observe(snapshot(138, reset=18102), 138, 138), "fixed_reset_observed")
        self.assertEqual(restored.data["attempt"]["phase"], "verified")
        self.assertEqual(restored.data["attempt"]["usage"]["cached_tokens"], 3)
        self.assertEqual(restored.data["window"], {"phase": "armed", "deadline": 18102})

    def test_drifting_reset_blocks_instead_of_resending(self):
        engine = self.completed()
        self.assertEqual(engine.observe(snapshot(108), 108, 108), "verification_sample")
        with self.assertRaises(ScheduleError):
            engine.observe(snapshot(138), 138, 138)
        self.assertEqual(engine.data["attempt"]["phase"], "blocked")
        with self.assertRaises(ScheduleError):
            Schedule(copy.deepcopy(engine.data), BINDING)

    def test_restart_does_not_replay_sending_and_resumes_received_success(self):
        engine = self.ready()
        engine.prepare(snapshot(102), 102, 102)
        with self.assertRaises(ScheduleError):
            Schedule(copy.deepcopy(engine.data), BINDING)
        engine.completed(USAGE, 103)
        restored = Schedule(copy.deepcopy(engine.data), BINDING)
        restored.observe(snapshot(200, reset=18102), 200, 200)
        self.assertEqual(restored.observe(snapshot(202, reset=18102), 202, 202), "fixed_reset_observed")

    def test_next_window_requires_new_pair_and_can_create_one_new_attempt(self):
        engine = self.completed()
        engine.observe(snapshot(108, reset=18102), 108, 108)
        engine.observe(snapshot(138, reset=18102), 138, 138)
        old_id = engine.data["attempt"]["id"]
        engine.observe(snapshot(18107), 18107, 18107)
        engine.observe(snapshot(18109), 18109, 18109)
        self.assertTrue(engine.prepare(snapshot(18109), 18109, 18109))
        self.assertNotEqual(engine.data["attempt"]["id"], old_id)

    def test_unknown_account_other_bucket_weekly_ambiguity_and_clock_rollback_stop(self):
        for value in (snapshot(100, account=None), snapshot(100, account=False), snapshot(100, bucket="other"),
                      snapshot(100, duration=604800), snapshot(100, used=None), snapshot(100, reset=999999)):
            with self.subTest(value=value), self.assertRaises(ScheduleError):
                Schedule(None, BINDING).observe(value, 100, 100)
        with self.assertRaises(QuotaError):
            Schedule(None, BINDING).observe(Snapshot(True, True, snapshot(100).windows * 2), 100, 100)
        engine = self.ready()
        with self.assertRaises(ScheduleError):
            engine.observe(snapshot(90), 90, 90)

    def test_invalid_persisted_state_is_not_silently_reinitialized(self):
        for value in ({}, {"schema": True}, {"schema": 1, "binding": BINDING}):
            with self.assertRaises(ScheduleError):
                Schedule(value, BINDING)
        valid = self.completed().data
        for mutate in (lambda x: x.update(binding="b" * 64),
                       lambda x: x["window"].update(phase=[]),
                       lambda x: x["attempt"].update(finished_at=float("nan")),
                       lambda x: x["attempt"].update(phase="verified")):
            value = copy.deepcopy(valid)
            mutate(value)
            with self.assertRaises(ScheduleError):
                Schedule(value, BINDING)
