"""Durable effective-time behavior, without task or protocol dependencies."""

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mempalace_tasks.journal import JournalError, JsonStore
from mempalace_tasks.leases import ClockError, EffectiveClock


SECOND = 1_000_000_000
JANUARY_2024 = 1_704_067_200_000_000_000


class Samples:
    def __init__(self, wall=JANUARY_2024, monotonic=10 * SECOND):
        self.wall = wall
        self.monotonic = monotonic

    def wall_time_ns(self):
        return self.wall

    def monotonic_ns(self):
        return self.monotonic


@contextmanager
def fail_post_replace_directory_sync():
    replace = os.replace
    fsync = os.fsync
    replaced = False

    def replace_and_mark(*args, **kwargs):
        nonlocal replaced
        result = replace(*args, **kwargs)
        replaced = True
        return result

    def sync_before_replace_only(fd):
        if replaced:
            raise OSError("injected post-replace directory sync failure")
        return fsync(fd)

    with (
        patch("mempalace_tasks.journal.os.replace", new=replace_and_mark),
        patch("mempalace_tasks.journal.os.fsync", new=sync_before_replace_only),
    ):
        yield


class EffectiveClockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-clock-", dir=os.environ.get("MPTASK_TEST_TMPDIR")
        )
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "clock.json"
        self.store = JsonStore(self.path)
        self.samples = Samples()

    def clock(self, *, initialize=False, boot_id="boot-a", samples=None):
        samples = self.samples if samples is None else samples
        return EffectiveClock(
            self.store,
            wall_time_ns=samples.wall_time_ns,
            monotonic_ns=samples.monotonic_ns,
            boot_id=boot_id,
            initialize=initialize,
        )

    def test_missing_state_requires_explicit_initialization(self):
        with self.assertRaises(ClockError) as caught:
            self.clock()
        self.assertEqual(caught.exception.code, "missing_clock_state")
        self.assertFalse(self.path.parent.exists())

    def test_initialization_persists_integer_nanosecond_anchors(self):
        self.samples.wall += 123_456_789
        self.samples.monotonic += 987_654_321
        clock = self.clock(initialize=True)
        self.assertEqual(
            self.store.read(),
            {
                "version": 1,
                "boot_id": "boot-a",
                "effective_ns": 1_704_067_200_123_456_789,
                "monotonic_ns": 10_987_654_321,
                "pending_reboot": False,
                "previous_boot_id": None,
            },
        )
        self.assertFalse(clock.pending_reboot)
        self.assertEqual(clock.now(), "2024-01-01T00:00:00.123456Z")

    def test_initialize_flag_never_resets_existing_anchors(self):
        self.clock(initialize=True)
        before = self.store.read()
        self.samples.wall -= SECOND
        self.samples.monotonic += 2 * SECOND
        clock = self.clock(initialize=True)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(clock.now(), "2024-01-01T00:00:02.000000Z")

    def test_zero_samples_are_valid_at_unix_epoch(self):
        self.samples.wall = 0
        self.samples.monotonic = 0
        clock = self.clock(initialize=True)
        self.assertEqual(clock.now(), "1970-01-01T00:00:00.000000Z")

    def test_backward_wall_uses_elapsed_monotonic_time(self):
        clock = self.clock(initialize=True)
        self.samples.wall -= 50 * SECOND
        self.samples.monotonic += 7 * SECOND
        self.assertEqual(clock.now(), "2024-01-01T00:00:07.000000Z")
        self.assertEqual(
            self.store.read()["effective_ns"], JANUARY_2024 + 7 * SECOND
        )

    def test_forward_jump_then_backward_correction_does_not_freeze_new_lease(self):
        clock = self.clock(initialize=True)
        self.samples.wall += 3_600 * SECOND
        self.samples.monotonic += SECOND
        self.assertEqual(clock.now(), "2024-01-01T01:00:00.000000Z")
        self.samples.wall = JANUARY_2024
        self.samples.monotonic += 300 * SECOND - 1_000
        self.assertEqual(clock.now(), "2024-01-01T01:04:59.999999Z")
        self.samples.monotonic += 1_000
        self.assertEqual(clock.now(), "2024-01-01T01:05:00.000000Z")
        self.assertEqual(self.store.read()["monotonic_ns"], 311 * SECOND)

    def test_submicrosecond_rebases_preserve_carries_across_restart(self):
        self.samples.wall += 999
        clock = self.clock(initialize=True)
        self.assertEqual(clock.now(), "2024-01-01T00:00:00.000000Z")
        self.samples.monotonic += 1
        self.assertEqual(clock.now(), "2024-01-01T00:00:00.000001Z")
        self.samples.monotonic += 999
        self.assertEqual(clock.now(), "2024-01-01T00:00:00.000001Z")
        self.assertEqual(
            self.store.read()["effective_ns"], 1_704_067_200_000_001_999
        )
        clock = self.clock()
        self.samples.monotonic += 1
        self.assertEqual(clock.now(), "2024-01-01T00:00:00.000002Z")

    def test_equal_wall_and_monotonic_candidate_persists_new_anchor(self):
        clock = self.clock(initialize=True)
        self.samples.wall += 5 * SECOND
        self.samples.monotonic += 5 * SECOND
        self.assertEqual(clock.now(), "2024-01-01T00:00:05.000000Z")
        self.assertEqual(self.store.read()["monotonic_ns"], 15 * SECOND)

    def test_repeated_equal_samples_do_not_invent_elapsed_time(self):
        clock = self.clock(initialize=True)
        for _ in range(3):
            self.assertEqual(clock.now(), "2024-01-01T00:00:00.000000Z")
        self.assertEqual(self.store.read()["effective_ns"], JANUARY_2024)

    def test_same_boot_restart_counts_elapsed_time_from_persisted_anchor(self):
        clock = self.clock(initialize=True)
        self.samples.monotonic += 9 * SECOND
        self.assertEqual(clock.now(), "2024-01-01T00:00:09.000000Z")
        self.samples.wall -= SECOND
        self.samples.monotonic += 11 * SECOND
        reopened = self.clock()
        self.assertFalse(reopened.pending_reboot)
        self.assertEqual(reopened.now(), "2024-01-01T00:00:20.000000Z")

    def test_same_boot_monotonic_regression_is_not_clamped(self):
        clock = self.clock(initialize=True)
        before = self.store.read()
        self.samples.monotonic -= 1
        self.samples.wall += 100 * SECOND
        with self.assertRaises(ClockError) as caught:
            clock.now()
        self.assertEqual(caught.exception.code, "monotonic_discontinuity")
        self.assertEqual(self.store.read(), before)

    def test_same_boot_restart_does_not_hide_monotonic_regression(self):
        self.clock(initialize=True)
        before = self.store.read()
        self.samples.monotonic -= 1
        with self.assertRaises(ClockError):
            self.clock().now()
        self.assertEqual(self.store.read(), before)

    def test_reboot_persists_pending_and_rebases_lower_monotonic_sample(self):
        self.clock(initialize=True)
        self.samples.wall -= 100 * SECOND
        self.samples.monotonic = SECOND
        clock = self.clock(boot_id="boot-b")
        persisted = self.store.read()
        self.assertTrue(clock.pending_reboot)
        self.assertTrue(persisted["pending_reboot"])
        self.assertEqual(persisted["previous_boot_id"], "boot-a")
        self.assertEqual(persisted["boot_id"], "boot-b")
        self.assertEqual(persisted["effective_ns"], JANUARY_2024)
        self.assertEqual(persisted["monotonic_ns"], SECOND)
        self.samples.monotonic += SECOND
        self.assertEqual(clock.now(), "2024-01-01T00:00:01.000000Z")
        self.assertTrue(clock.pending_reboot)

    def test_reboot_uses_forward_wall_without_old_boot_elapsed_time(self):
        self.clock(initialize=True)
        self.samples.wall += 100 * SECOND
        self.samples.monotonic += 1_000 * SECOND
        clock = self.clock(boot_id="boot-b")
        self.assertEqual(clock.now(), "2024-01-01T00:01:40.000000Z")
        self.assertTrue(clock.pending_reboot)

    def test_pending_reboot_survives_repeated_process_reopening(self):
        self.clock(initialize=True)
        self.clock(boot_id="boot-b")
        for _ in range(3):
            clock = self.clock(boot_id="boot-b", initialize=True)
            self.assertTrue(clock.pending_reboot)
            clock.now()
            self.assertTrue(self.store.read()["pending_reboot"])

    def test_pending_reboot_survives_abrupt_process_exit(self):
        self.clock(initialize=True)
        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error",
                "-c",
                (
                    "import os, sys\n"
                    "from mempalace_tasks.journal import JsonStore\n"
                    "from mempalace_tasks.leases import EffectiveClock\n"
                    "EffectiveClock(JsonStore(sys.argv[1]), "
                    "wall_time_ns=lambda: 1704067199000000000, "
                    "monotonic_ns=lambda: 1, boot_id='boot-b')\n"
                    "os._exit(23)\n"
                ),
                str(self.path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        clock = self.clock(boot_id="boot-b")
        self.assertTrue(clock.pending_reboot)
        self.assertEqual(self.store.read()["previous_boot_id"], "boot-a")

    def test_further_reboot_does_not_acknowledge_earlier_pending_recovery(self):
        self.clock(initialize=True)
        self.clock(boot_id="boot-b")
        clock = self.clock(boot_id="boot-c")
        self.assertTrue(clock.pending_reboot)
        self.assertEqual(self.store.read()["previous_boot_id"], "boot-b")

    def test_zero_active_attempts_durably_acknowledges_reboot(self):
        self.clock(initialize=True)
        clock = self.clock(boot_id="boot-b")
        clock.acknowledge_reboot(active_attempts=0)
        self.assertFalse(clock.pending_reboot)
        self.assertIsNone(self.store.read()["previous_boot_id"])
        self.assertFalse(self.clock(boot_id="boot-b").pending_reboot)

    def test_acknowledgment_rejects_nonzero_or_noninteger_active_attempts(self):
        self.clock(initialize=True)
        clock = self.clock(boot_id="boot-b")
        before = self.store.read()
        for invalid in (1, 10, -1, True, False, 0.0, None, "0"):
            with self.subTest(active_attempts=invalid):
                with self.assertRaises(ClockError):
                    clock.acknowledge_reboot(active_attempts=invalid)
                self.assertTrue(clock.pending_reboot)
                self.assertEqual(self.store.read(), before)

    def test_acknowledgment_requires_count_even_without_pending_reboot(self):
        clock = self.clock(initialize=True)
        with self.assertRaises(ClockError):
            clock.acknowledge_reboot(active_attempts=1)
        clock.acknowledge_reboot(active_attempts=0)
        self.assertFalse(clock.pending_reboot)

    def test_invalid_initial_samples_do_not_create_state(self):
        invalid_samples = (
            True, False, -1, 1.0, float("nan"), float("inf"),
            float("-inf"), None, "123",
        )
        for field in ("wall", "monotonic"):
            for invalid in invalid_samples:
                with self.subTest(field=field, invalid=invalid):
                    self.samples = Samples()
                    setattr(self.samples, field, invalid)
                    with self.assertRaises(ClockError):
                        self.clock(initialize=True)
                    self.assertFalse(self.path.exists())

    def test_invalid_now_samples_do_not_modify_state(self):
        clock = self.clock(initialize=True)
        before = self.store.read()
        for field in ("wall", "monotonic"):
            for invalid in (True, -1, 1.0, float("nan"), float("inf"), None):
                with self.subTest(field=field, invalid=invalid):
                    self.samples.wall = JANUARY_2024
                    self.samples.monotonic = 10 * SECOND
                    setattr(self.samples, field, invalid)
                    with self.assertRaises(ClockError):
                        clock.now()
                    self.assertEqual(self.store.read(), before)

    def test_unrepresentable_wall_time_fails_before_persisting(self):
        self.samples.wall = 253_402_300_800_000_000_000
        with self.assertRaises(ClockError):
            self.clock(initialize=True)
        self.assertFalse(self.path.exists())

    def test_effective_time_overflow_does_not_persist_unusable_anchor(self):
        self.samples.wall = 253_402_300_799_999_999_999
        clock = self.clock(initialize=True)
        self.assertEqual(clock.now(), "9999-12-31T23:59:59.999999Z")
        before = self.store.read()
        self.samples.monotonic += 1
        with self.assertRaises(ClockError):
            clock.now()
        self.assertEqual(self.store.read(), before)

    def test_sample_provider_failure_is_typed_and_preserves_cause(self):
        failure = OSError("sample unavailable")
        with patch.object(self.samples, "wall_time_ns", side_effect=failure):
            with self.assertRaises(ClockError) as caught:
                self.clock(initialize=True)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertFalse(self.path.exists())

    def test_noncallable_sample_providers_are_rejected(self):
        for field in ("wall_time_ns", "monotonic_ns"):
            with self.subTest(field=field):
                with self.assertRaises(ClockError):
                    EffectiveClock(
                        self.store, boot_id="boot-a", initialize=True, **{field: 0}
                    )
                self.assertFalse(self.path.exists())

    def test_initialize_requires_boolean(self):
        for invalid in (1, 0, "yes", None):
            with self.subTest(initialize=invalid):
                with self.assertRaises(ClockError):
                    self.clock(initialize=invalid)
                self.assertFalse(self.path.exists())

    def test_invalid_explicit_boot_ids_are_rejected_without_disclosure(self):
        for invalid in ("", " ", "boot\nsecret", " boot-a", "boot-a ", True, 0):
            with self.subTest(boot_id=invalid):
                with self.assertRaises(ClockError) as caught:
                    self.clock(initialize=True, boot_id=invalid)
                self.assertNotIn("secret", str(caught.exception))
                self.assertFalse(self.path.exists())

    def test_default_providers_read_private_boot_id_and_nanosecond_clocks(self):
        with (
            patch("mempalace_tasks.leases.Path.read_text", return_value="boot-a\n"),
            patch("mempalace_tasks.leases.time.time_ns", return_value=JANUARY_2024),
            patch("mempalace_tasks.leases.time.monotonic_ns", return_value=123),
        ):
            clock = EffectiveClock(self.store, initialize=True)
            self.assertEqual(clock.now(), "2024-01-01T00:00:00.000000Z")
        self.assertEqual(self.store.read()["boot_id"], "boot-a")
        self.assertEqual(self.store.read()["monotonic_ns"], 123)

    def test_unreadable_default_boot_id_is_typed_and_creates_nothing(self):
        failure = OSError("unavailable")
        with patch("mempalace_tasks.leases.Path.read_text", side_effect=failure):
            with self.assertRaises(ClockError) as caught:
                EffectiveClock(self.store, initialize=True)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertFalse(self.path.exists())

    def test_null_or_other_nonobject_state_is_not_missing_initialization(self):
        for invalid in (None, [], "", False, 0):
            with self.subTest(state=invalid):
                self.store.write(invalid)
                with self.assertRaises(ClockError):
                    self.clock(initialize=True)
                self.assertEqual(self.store.read(), invalid)

    def test_invalid_persisted_fields_are_never_silently_reset(self):
        self.clock(initialize=True)
        baseline = self.store.read()
        invalid_fields = (
            ("version", True), ("version", 2), ("version", 1.0),
            ("boot_id", ""), ("boot_id", None), ("boot_id", "boot\nsecret"),
            ("effective_ns", True), ("effective_ns", -1),
            ("effective_ns", 1.0), ("effective_ns", None),
            ("effective_ns", 253_402_300_800_000_000_000),
            ("monotonic_ns", False), ("monotonic_ns", -1),
            ("monotonic_ns", "10"), ("monotonic_ns", 10.0),
            ("pending_reboot", 0), ("pending_reboot", None),
            ("pending_reboot", True), ("previous_boot_id", "boot-old"),
        )
        for field, invalid in invalid_fields:
            with self.subTest(field=field, invalid=invalid):
                state = {**baseline, field: invalid}
                self.store.write(state)
                with self.assertRaises(ClockError):
                    self.clock(initialize=True)
                self.assertEqual(self.store.read(), state)
        for field in baseline:
            with self.subTest(missing=field):
                state = dict(baseline)
                del state[field]
                self.store.write(state)
                with self.assertRaises(ClockError):
                    self.clock(initialize=True)
                self.assertEqual(self.store.read(), state)
        self.store.write({**baseline, "unknown": 1})
        with self.assertRaises(ClockError):
            self.clock(initialize=True)

    def test_pending_reboot_requires_a_different_valid_previous_boot(self):
        self.clock(initialize=True)
        self.clock(boot_id="boot-b")
        baseline = self.store.read()
        for invalid in (None, "", True, "boot-b"):
            with self.subTest(previous_boot_id=invalid):
                self.store.write({**baseline, "previous_boot_id": invalid})
                with self.assertRaises(ClockError):
                    self.clock(boot_id="boot-b", initialize=True)

    def test_corrupt_json_errors_are_preserved_without_reinitialization(self):
        self.clock(initialize=True)
        for contents in ('{"version":', '{"version":1,"version":1}', '{"x":NaN}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                with self.assertRaises(JournalError) as caught:
                    self.clock(initialize=True)
                self.assertEqual(caught.exception.code, "corrupt_state")
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)

    def test_missing_existing_store_fails_closed_on_every_public_operation(self):
        clock = self.clock(initialize=True)
        self.store.clear()
        for operation in (
            clock.now,
            lambda: clock.pending_reboot,
            lambda: clock.acknowledge_reboot(active_attempts=0),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ClockError):
                    operation()
                self.assertFalse(self.path.exists())

    def test_corruption_after_construction_is_not_overwritten_by_now(self):
        clock = self.clock(initialize=True)
        contents = '{"version":'
        self.path.write_text(contents, encoding="utf-8")
        with self.assertRaises(JournalError):
            clock.now()
        self.assertEqual(self.path.read_text(encoding="utf-8"), contents)

    def test_invalid_state_after_construction_is_not_overwritten_by_now(self):
        clock = self.clock(initialize=True)
        state = self.store.read()
        state["effective_ns"] = False
        self.store.write(state)
        with self.assertRaises(ClockError):
            clock.now()
        self.assertEqual(self.store.read(), state)

    def test_read_failure_preserves_journal_error(self):
        failure = JournalError("io_error", "read failed")
        with patch.object(self.store, "read", side_effect=failure):
            with self.assertRaises(JournalError) as caught:
                self.clock(initialize=True)
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.path.exists())

    def test_initial_persistence_failure_does_not_produce_a_clock(self):
        failure = JournalError("io_error", "write failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError) as caught:
                self.clock(initialize=True)
        self.assertIs(caught.exception, failure)
        self.assertFalse(self.path.exists())

    def test_now_persistence_failure_never_returns_an_uncommitted_time(self):
        clock = self.clock(initialize=True)
        before = self.store.read()
        self.samples.wall += 100 * SECOND
        failure = JournalError("io_error", "write failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError) as caught:
                clock.now()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(clock.now(), "2024-01-01T00:01:40.000000Z")

    def test_post_replace_failure_is_raised_and_retry_repersists_time(self):
        clock = self.clock(initialize=True)
        self.samples.wall += 100 * SECOND
        write = self.store.write
        failure = JournalError("io_error", "directory sync failed", ambiguous=True)

        def write_then_fail(value):
            write(value)
            raise failure

        with patch.object(self.store, "write", side_effect=write_then_fail):
            with self.assertRaises(JournalError) as caught:
                clock.now()
        self.assertIs(caught.exception, failure)
        self.samples.wall = JANUARY_2024
        self.samples.monotonic += SECOND
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError):
                clock.now()
        self.assertEqual(clock.now(), "2024-01-01T00:01:41.000000Z")

    def test_unchanged_time_still_requires_successful_persistence(self):
        clock = self.clock(initialize=True)
        failure = JournalError("io_error", "sync failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError):
                clock.now()

    def test_reboot_persistence_failure_cannot_silently_use_old_state(self):
        self.clock(initialize=True)
        before = self.store.read()
        failure = JournalError("io_error", "write failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError):
                self.clock(boot_id="boot-b")
        self.assertEqual(self.store.read(), before)
        self.assertTrue(self.clock(boot_id="boot-b").pending_reboot)

    def test_acknowledgment_persistence_failure_keeps_pending(self):
        self.clock(initialize=True)
        clock = self.clock(boot_id="boot-b")
        failure = JournalError("io_error", "write failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError) as caught:
                clock.acknowledge_reboot(active_attempts=0)
        self.assertIs(caught.exception, failure)
        self.assertTrue(clock.pending_reboot)
        self.assertTrue(self.clock(boot_id="boot-b").pending_reboot)

    def test_real_post_replace_fsync_failure_never_returns_time(self):
        clock = self.clock(initialize=True)
        self.samples.wall += SECOND
        with fail_post_replace_directory_sync():
            with self.assertRaises(JournalError) as caught:
                clock.now()
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(self.store.read()["effective_ns"], JANUARY_2024 + SECOND)
        self.samples.wall = JANUARY_2024
        self.samples.monotonic += SECOND
        self.assertEqual(clock.now(), "2024-01-01T00:00:02.000000Z")

    def test_acknowledgment_retry_after_ambiguous_failure_must_repersist(self):
        self.clock(initialize=True)
        clock = self.clock(boot_id="boot-b")
        with fail_post_replace_directory_sync():
            with self.assertRaises(JournalError) as caught:
                clock.acknowledge_reboot(active_attempts=0)
        self.assertTrue(caught.exception.ambiguous)
        self.assertFalse(self.store.read()["pending_reboot"])
        failure = JournalError("io_error", "retry sync failed")
        with patch.object(self.store, "write", side_effect=failure):
            with self.assertRaises(JournalError):
                clock.acknowledge_reboot(active_attempts=0)
        clock.acknowledge_reboot(active_attempts=0)
        self.assertFalse(self.clock(boot_id="boot-b").pending_reboot)


if __name__ == "__main__":
    unittest.main()
