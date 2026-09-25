"""Process-local clock contract; no palace, files, or boot identity required."""

import os
import select
import unittest

from mempalace_tasks.runtime_clock import ClockError, RuntimeClock


class Samples:
    def __init__(self):
        self.wall = 1_000_000_000
        self.continuous = 10_000_000_000

    def clock(self, initial_time=None):
        return RuntimeClock(initial_time, wall_time_ns=lambda: self.wall,
                            continuous_time_ns=lambda: self.continuous)


class RuntimeClockTests(unittest.TestCase):
    def setUp(self):
        self.samples = Samples()

    def test_initial_timestamp_is_a_replayed_lower_bound(self):
        clock = self.samples.clock("2026-09-25T12:00:00Z")
        self.assertEqual(clock.now(), "2026-09-25T12:00:00.000000Z")

    def test_backward_wall_jump_cannot_extend_elapsed_lease_time(self):
        clock = self.samples.clock()
        self.samples.wall = 0
        self.samples.continuous += 4_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:05.000000Z")

    def test_forward_wall_jump_reanchors_elapsed_time(self):
        clock = self.samples.clock()
        self.samples.wall = 20_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:20.000000Z")
        self.samples.wall = 0
        self.samples.continuous += 2_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:22.000000Z")

    def test_observation_advances_lower_bound_and_reanchors(self):
        clock = self.samples.clock()
        self.samples.continuous += 3_000_000_000
        clock.observe("1970-01-01T00:00:10.000001Z")
        self.samples.continuous += 2_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:12.000001Z")

    def test_old_observation_does_not_discard_elapsed_time(self):
        clock = self.samples.clock()
        self.samples.continuous += 5_000_000_000
        clock.observe("1970-01-01T00:00:00Z")
        self.samples.continuous += 2_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:08.000000Z")

    def test_submicrosecond_time_is_retained_not_rounded_per_sample(self):
        clock = self.samples.clock()
        self.samples.continuous += 600
        self.assertEqual(clock.now(), "1970-01-01T00:00:01.000000Z")
        self.samples.continuous += 600
        self.assertEqual(clock.now(), "1970-01-01T00:00:01.000001Z")

    def test_new_incarnation_has_no_persistent_monotonic_anchor(self):
        self.samples.clock("2026-09-25T12:00:00Z")
        self.samples.continuous = 0
        self.assertEqual(self.samples.clock().now(), "1970-01-01T00:00:01.000000Z")

    def test_regression_is_explicit_and_does_not_move_anchor(self):
        clock = self.samples.clock()
        self.samples.continuous -= 1
        with self.assertRaises(ClockError) as caught:
            clock.now()
        self.assertEqual(caught.exception.code, "continuous_discontinuity")
        self.samples.continuous += 1_000_000_001
        self.assertEqual(clock.now(), "1970-01-01T00:00:02.000000Z")

    def test_initial_timestamp_requires_bounded_unambiguous_utc(self):
        for value in ("1969-12-31T23:59:59Z", "2026-01-01", "bad", 0, True,
                      "2026-01-01T00:00:00+01:00", "2026-01-01T00:00:60Z",
                      "2026-01-01T00:00:00.1234567890Z"):
            with self.subTest(value=value), self.assertRaises(ClockError):
                self.samples.clock(value)

    def test_timestamp_accepts_utc_offset_and_nine_fractional_digits(self):
        clock = self.samples.clock("2026-01-01T00:00:00.123456789+00:00")
        self.samples.continuous += 211
        self.assertEqual(clock.now(), "2026-01-01T00:00:00.123457Z")

    def test_bad_observation_does_not_change_state(self):
        clock = self.samples.clock()
        with self.assertRaises(ClockError):
            clock.observe("not-time")
        self.assertEqual(clock.now(), "1970-01-01T00:00:01.000000Z")

    def test_invalid_samples_are_not_coerced(self):
        for value in (-1, True, 1.2, None, "2"):
            for field in ("wall", "continuous"):
                with self.subTest(value=value, field=field), self.assertRaises(ClockError):
                    samples = Samples()
                    setattr(samples, field, value)
                    samples.clock()

    def test_effective_time_overflow_is_explicit(self):
        clock = self.samples.clock("9999-12-31T23:59:59.999999999Z")
        self.samples.continuous += 1
        with self.assertRaises(ClockError):
            clock.now()

    def test_wall_and_continuous_sample_ranges_are_checked(self):
        for field, value in (("wall", 253_402_300_800_000_000_000),
                             ("continuous", 2**64)):
            with self.subTest(field=field), self.assertRaises(ClockError):
                samples = Samples()
                setattr(samples, field, value)
                samples.clock()

    def test_provider_failure_is_explicit_no_wall_fallback(self):
        def failed():
            raise OSError("unavailable")
        with self.assertRaises(ClockError) as caught:
            RuntimeClock(wall_time_ns=lambda: 0, continuous_time_ns=failed)
        self.assertEqual(caught.exception.code, "clock_sample_failed")

    def test_noncallable_provider_is_rejected(self):
        with self.assertRaises(ClockError):
            RuntimeClock(wall_time_ns=0)

    def test_anchor_precedes_provider_latency(self):
        samples = Samples()
        def wall():
            samples.continuous += 2_000_000_000
            return samples.wall
        clock = RuntimeClock(wall_time_ns=wall,
                             continuous_time_ns=lambda: samples.continuous)
        # Both expensive wall sampling and later I/O count against elapsed time.
        self.assertEqual(clock.now(), "1970-01-01T00:00:03.000000Z")
        self.assertEqual(clock.now(), "1970-01-01T00:00:05.000000Z")

    def test_provider_failure_leaves_last_anchor_intact(self):
        failing = False
        def continuous():
            if failing:
                raise OSError("clock failed")
            return self.samples.continuous
        clock = RuntimeClock(wall_time_ns=lambda: self.samples.wall,
                             continuous_time_ns=continuous)
        failing = True
        self.samples.continuous += 2_000_000_000
        with self.assertRaises(ClockError):
            clock.now()
        failing = False
        self.samples.continuous += 1_000_000_000
        self.assertEqual(clock.now(), "1970-01-01T00:00:04.000000Z")

    @unittest.skipUnless(hasattr(os, "fork"), "native POSIX fork")
    def test_inherited_clock_fails_instead_of_using_parent_incarnation(self):
        clock = self.samples.clock()
        read_end, write_end = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_end)
            try:
                try:
                    clock.now()
                except ClockError as error:
                    os.write(write_end, error.code.encode("ascii"))
            finally:
                os._exit(0)
        os.close(write_end)
        try:
            ready, _, _ = select.select([read_end], [], [], 10)
            self.assertTrue(ready)
            self.assertEqual(os.read(read_end, 100), b"clock_process_changed")
        finally:
            os.close(read_end)
            os.waitpid(child, 0)


if __name__ == "__main__":
    unittest.main()
