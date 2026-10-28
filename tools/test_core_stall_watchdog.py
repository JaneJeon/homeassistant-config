"""Isolated regression checks for the Core stall watchdog.

The timeout API in faulthandler is process-global. Each behavior case therefore
runs in a child process and imports watchdog.py directly, without Home Assistant.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
WATCHDOG_SOURCE = REPOSITORY / "custom_components/core_stall_watchdog/watchdog.py"

HARNESS = textwrap.dedent(
    r"""
    import asyncio
    import faulthandler
    import importlib.util
    import json
    import logging
    import os
    from pathlib import Path
    import sys
    import threading
    import time

    source = Path(os.environ["WATCHDOG_SOURCE"])
    path = Path(sys.argv[2])
    spec = importlib.util.spec_from_file_location("watchdog_under_test", source)
    watchdog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watchdog)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(message)s")

    async def healthy():
        stop = asyncio.Event()
        arms = []
        native_arm = watchdog.faulthandler.dump_traceback_later
        def count_arm(*args, **kwargs):
            arms.append(args[0])
            return native_arm(*args, **kwargs)
        watchdog.faulthandler.dump_traceback_later = count_arm
        crash_log = path.with_suffix(".crash.log").open("ab", buffering=0)
        faulthandler.enable(file=crash_log)
        task = asyncio.create_task(
            watchdog.run_watchdog(stop, path, timeout=0.20, interval=0.04)
        )
        await asyncio.sleep(0.44)
        stop.set()
        await task
        data = path.read_bytes()
        assert len(arms) > 1, arms
        assert b"Watchdog armed at" in data, data
        assert b"most recent call first" not in data, data
        assert faulthandler.is_enabled(), "the crash handler was disabled"
        crash_log.close()
        print(json.dumps({"bytes": len(data), "arm_calls": len(arms),
                          "traceback": False,
                          "crash_handler_enabled": faulthandler.is_enabled()}))

    def block_event_loop_for_stall(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            pass

    async def stalled(cap=1_000_000, tracked=False):
        stop = asyncio.Event()
        arms = []
        recovery_arm_counts = []
        first_arm = threading.Event()
        native_arm = watchdog.faulthandler.dump_traceback_later
        def count_arm(*args, **kwargs):
            arms.append(args[0])
            result = native_arm(*args, **kwargs)
            first_arm.set()
            return result
        watchdog.faulthandler.dump_traceback_later = count_arm
        if tracked:
            native_recovery = watchdog._record_recovery
            def count_recovery(*args, **kwargs):
                result = native_recovery(*args, **kwargs)
                recovery_arm_counts.append(len(arms))
                return result
            watchdog._record_recovery = count_recovery
        task = asyncio.create_task(
            watchdog.run_watchdog(
                stop, path, timeout=0.20, interval=0.04, max_bytes=cap
            )
        )
        await asyncio.to_thread(first_arm.wait, 3)
        assert first_arm.is_set(), "watchdog did not complete its first native arm"
        asyncio.get_running_loop().call_soon(block_event_loop_for_stall, 0.50)
        await asyncio.sleep(0.64)
        stop.set()
        await task
        data = path.read_bytes()
        assert b"Timeout (0:00:00.200000)!" in data, data
        assert b"block_event_loop_for_stall" in data, data
        if tracked:
            assert len(recovery_arm_counts) == 1, recovery_arm_counts
            assert len(arms) == recovery_arm_counts[0], (arms, recovery_arm_counts)
            dump = data.split(b"Heartbeat recovered", 1)[0]
            assert len(dump) > cap, (len(dump), cap, data)
        print(json.dumps({"bytes": len(data), "blocker_found": True,
                          "arm_calls": len(arms) if tracked else None,
                          "arm_calls_at_recovery": recovery_arm_counts[0] if tracked else None,
                          "cap": cap if tracked else None}))

    async def stopped():
        stop = asyncio.Event()
        crash_log = path.with_suffix(".crash.log").open("ab", buffering=0)
        faulthandler.enable(file=crash_log)
        task = asyncio.create_task(
            watchdog.run_watchdog(stop, path, timeout=0.20, interval=0.04)
        )
        for _ in range(100):
            if path.exists() and b"Watchdog armed at" in path.read_bytes():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("watchdog never opened its log")
        stop.set()
        await task
        stopped_data = path.read_bytes()
        await asyncio.sleep(0.28)
        assert path.read_bytes() == stopped_data, "log changed after stop cleanup"
        assert faulthandler.is_enabled(), "the crash handler was disabled"
        crash_log.close()
        print(json.dumps({"bytes_at_stop": len(stopped_data),
                          "unchanged_after_timeout": True,
                          "crash_handler_enabled": faulthandler.is_enabled()}))

    async def preexisting_cap():
        original = b"existing diagnostic data" * 10
        path.write_bytes(original)
        arms = []
        def count_arm(*args, **kwargs):
            arms.append(args[0])
            return None
        watchdog.faulthandler.dump_traceback_later = count_arm
        stop = asyncio.Event()
        await watchdog.run_watchdog(stop, path, timeout=0.20, interval=0.04,
                                    max_bytes=32)
        assert path.read_bytes() == original
        assert not arms, arms
        print(json.dumps({"bytes": len(original), "arm_calls": len(arms),
                          "contents_preserved": True}))

    async def injected_arm_failure():
        def fail(*args, **kwargs):
            raise RuntimeError("injected faulthandler failure")
        watchdog.faulthandler.dump_traceback_later = fail
        await watchdog.run_watchdog(asyncio.Event(), path, timeout=0.20,
                                    interval=0.04)
        print(json.dumps({"task_escaped": False,
                          "diagnostic_logged": "injected faulthandler failure"}))

    async def missing_parent():
        missing = path / "parent-that-does-not-exist" / "watchdog.log"
        await watchdog.run_watchdog(asyncio.Event(), missing, timeout=0.20,
                                    interval=0.04)
        assert not missing.exists()
        print(json.dumps({"task_escaped": False, "file_created": False,
                          "diagnostic_logged": "FileNotFoundError"}))

    async def cancel_during_open():
        entered = threading.Event()
        release = threading.Event()
        streams = []
        real_open = watchdog._open_log
        def delayed_open(log_path, max_bytes):
            entered.set()
            if not release.wait(5):
                raise AssertionError("open barrier timed out")
            stream = real_open(log_path, max_bytes)
            streams.append(stream)
            return stream
        watchdog._open_log = delayed_open
        task = asyncio.create_task(
            watchdog.run_watchdog(asyncio.Event(), path, timeout=0.20,
                                  interval=0.04)
        )
        await asyncio.to_thread(entered.wait, 2)
        assert entered.is_set(), "executor open did not reach its barrier"
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation did not propagate")
        assert len(streams) == 1, streams
        assert streams[0].closed, "opened stream leaked after cancellation"
        print(json.dumps({"cancelled": True, "stream_closed": True,
                          "stream_created": path.exists()}))

    async def cancel_during_recovery():
        entered = threading.Event()
        release = threading.Event()
        streams = []
        real_open = watchdog._open_log
        real_recovery = watchdog._record_recovery
        def track_open(log_path, max_bytes):
            stream = real_open(log_path, max_bytes)
            streams.append(stream)
            return stream
        def delayed_recovery(stream, previous, gap, max_bytes):
            entered.set()
            if not release.wait(5):
                raise AssertionError("recovery barrier timed out")
            return real_recovery(stream, previous, gap, max_bytes)
        watchdog._open_log = track_open
        watchdog._record_recovery = delayed_recovery
        first_arm = threading.Event()
        native_arm = watchdog.faulthandler.dump_traceback_later
        def track_arm(*args, **kwargs):
            result = native_arm(*args, **kwargs)
            first_arm.set()
            return result
        watchdog.faulthandler.dump_traceback_later = track_arm
        task = asyncio.create_task(
            watchdog.run_watchdog(asyncio.Event(), path, timeout=0.20,
                                  interval=0.04)
        )
        await asyncio.to_thread(first_arm.wait, 3)
        assert first_arm.is_set(), "watchdog did not complete its first native arm"
        asyncio.get_running_loop().call_soon(block_event_loop_for_stall, 0.50)
        await asyncio.to_thread(entered.wait, 3)
        assert entered.is_set(), "recovery worker did not reach its barrier"
        task.cancel()
        assert not task.done(), "task closed its stream before the worker finished"
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation did not propagate")
        assert len(streams) == 1, streams
        assert streams[0].closed, "stream was not closed after recovery completed"
        data = path.read_bytes()
        assert b"Heartbeat recovered" in data, data
        assert b"block_event_loop_for_stall" in data, data
        print(json.dumps({"cancelled": True, "recovery_joined": True,
                          "stream_closed_after_join": True,
                          "recovery_marker_written": True}))

    async def cancel_repeatedly_while_blocked(task, release, stream=None):
        cancel_calls = 0
        for _ in range(3):
            assert not release.is_set(), "worker barrier was released too early"
            assert not task.done(), "task finished while the worker owned its resource"
            if stream is not None:
                assert not stream.closed, "stream closed while recovery worker still owned it"
            assert task.cancel(), "task rejected a repeated cancellation request"
            cancel_calls += 1
            await asyncio.sleep(0)
            assert not task.done(), "task finished before the blocked worker returned"
        return cancel_calls

    async def cancel_repeatedly_during_open():
        entered = threading.Event()
        release = threading.Event()
        streams = []
        real_open = watchdog._open_log
        def delayed_open(log_path, max_bytes):
            entered.set()
            if not release.wait(5):
                raise AssertionError("open barrier timed out")
            stream = real_open(log_path, max_bytes)
            streams.append(stream)
            return stream
        watchdog._open_log = delayed_open
        task = asyncio.create_task(
            watchdog.run_watchdog(asyncio.Event(), path, timeout=0.20,
                                  interval=0.04)
        )
        try:
            await asyncio.to_thread(entered.wait, 2)
            assert entered.is_set(), "executor open did not reach its barrier"
            cancel_calls = await cancel_repeatedly_while_blocked(task, release)
        finally:
            release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation did not propagate")
        assert len(streams) == 1, streams
        assert streams[0].closed, "opened stream leaked after repeated cancellation"
        print(json.dumps({"cancel_calls_while_open_blocked": cancel_calls,
                          "worker_returned": True, "stream_closed": True}))

    async def cancel_repeatedly_during_recovery():
        entered = threading.Event()
        release = threading.Event()
        streams = []
        real_open = watchdog._open_log
        real_recovery = watchdog._record_recovery
        def track_open(log_path, max_bytes):
            stream = real_open(log_path, max_bytes)
            streams.append(stream)
            return stream
        def delayed_recovery(stream, previous, gap, max_bytes):
            entered.set()
            if not release.wait(5):
                raise AssertionError("recovery barrier timed out")
            return real_recovery(stream, previous, gap, max_bytes)
        watchdog._open_log = track_open
        watchdog._record_recovery = delayed_recovery
        first_arm = threading.Event()
        native_arm = watchdog.faulthandler.dump_traceback_later
        def track_arm(*args, **kwargs):
            result = native_arm(*args, **kwargs)
            first_arm.set()
            return result
        watchdog.faulthandler.dump_traceback_later = track_arm
        task = asyncio.create_task(
            watchdog.run_watchdog(asyncio.Event(), path, timeout=0.20,
                                  interval=0.04)
        )
        try:
            await asyncio.to_thread(first_arm.wait, 3)
            assert first_arm.is_set(), "watchdog did not complete its first native arm"
            asyncio.get_running_loop().call_soon(block_event_loop_for_stall, 0.50)
            await asyncio.to_thread(entered.wait, 3)
            assert entered.is_set(), "recovery worker did not reach its barrier"
            assert len(streams) == 1, streams
            cancel_calls = await cancel_repeatedly_while_blocked(
                task, release, streams[0]
            )
        finally:
            release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation did not propagate")
        assert streams[0].closed, "stream was not closed after recovery completed"
        data = path.read_bytes()
        assert b"Heartbeat recovered" in data, data
        assert b"block_event_loop_for_stall" in data, data
        print(json.dumps({"cancel_calls_during_recovery_blocked": cancel_calls,
                          "worker_returned": True,
                          "stream_closed_after_join": True,
                          "recovery_marker_written": True}))

    async def native_cancel_failure_retains_stream():
        armed = threading.Event()
        streams = []
        real_open = watchdog._open_log
        real_arm = watchdog.faulthandler.dump_traceback_later
        real_cancel = watchdog.faulthandler.cancel_dump_traceback_later
        def track_open(log_path, max_bytes):
            stream = real_open(log_path, max_bytes)
            streams.append(stream)
            return stream
        def track_arm(*args, **kwargs):
            result = real_arm(*args, **kwargs)
            armed.set()
            return result
        def fail_cancel():
            raise RuntimeError("injected native cancel failure")
        watchdog._open_log = track_open
        watchdog.faulthandler.dump_traceback_later = track_arm
        watchdog.faulthandler.cancel_dump_traceback_later = fail_cancel
        stop = asyncio.Event()
        task = asyncio.create_task(
            watchdog.run_watchdog(stop, path, timeout=2.0,
                                  interval=0.10)
        )
        try:
            await asyncio.to_thread(armed.wait, 2)
            assert armed.is_set(), "native watchdog was not armed"
            assert len(streams) == 1, streams
            stop.set()
            await task
            retained = streams[0] in watchdog._RETAINED_STREAMS
            still_open = not streams[0].closed
            assert retained, "stream was not retained after native cancel failed"
            assert still_open, "stream closed while the native timer could still write"
        finally:
            watchdog.faulthandler.cancel_dump_traceback_later = real_cancel
            real_cancel()
            if streams and not streams[0].closed:
                streams[0].close()
            if streams and streams[0] in watchdog._RETAINED_STREAMS:
                watchdog._RETAINED_STREAMS.remove(streams[0])
            watchdog.faulthandler.dump_traceback_later = real_arm
        print(json.dumps({"task_escaped": False, "stream_retained": retained,
                          "stream_closed_before_manual_cancel": not still_open,
                          "manual_cancel_and_close_completed": True}))

    def count_fds():
        for candidate in ("/dev/fd", "/proc/self/fd"):
            try:
                return len(os.listdir(candidate))
            except OSError:
                continue
        return None

    def measure_cycles():
        crash_log = path.with_suffix(".crash.log").open("ab", buffering=0)
        stream = path.open("ab", buffering=0)
        faulthandler.enable(file=crash_log)
        before_threads = len(threading.enumerate())
        before_fds = count_fds()
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        for _ in range(1000):
            faulthandler.dump_traceback_later(60, repeat=False, file=stream,
                                              exit=False)
            faulthandler.cancel_dump_traceback_later()
        wall = time.perf_counter() - wall_start
        cpu = time.process_time() - cpu_start
        time.sleep(0.15)
        after_threads = len(threading.enumerate())
        after_fds = count_fds()
        faulthandler.cancel_dump_traceback_later()
        faulthandler.disable()
        stream.close()
        crash_log.close()
        print(json.dumps({"cycles": 1000, "wall_seconds": wall,
                          "cpu_seconds": cpu, "threads_before": before_threads,
                          "threads_after": after_threads, "fds_before": before_fds,
                          "fds_after": after_fds,
                          "threads_accumulated": after_threads - before_threads,
                          "fds_accumulated": (None if before_fds is None or after_fds is None
                                               else after_fds - before_fds)}))

    cases = {
        "healthy": healthy,
        "stall": stalled,
        "stop": stopped,
        "preexisting_cap": preexisting_cap,
        "arm_failure": injected_arm_failure,
        "missing_parent": missing_parent,
        "cancel_open": cancel_during_open,
        "cancel_recovery": cancel_during_recovery,
        "cancel_open_repeated": cancel_repeatedly_during_open,
        "cancel_recovery_repeated": cancel_repeatedly_during_recovery,
        "native_cancel_failure": native_cancel_failure_retains_stream,
    }
    case = sys.argv[1]
    if case == "post_stall_cap":
        asyncio.run(stalled(cap=128, tracked=True))
    elif case == "measure":
        measure_cycles()
    else:
        asyncio.run(cases[case]())
    """
)


class WatchdogSubprocessTests(unittest.TestCase):
    def run_case(self, case: str, log_path: Path) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["WATCHDOG_SOURCE"] = str(WATCHDOG_SOURCE)
        return subprocess.run(
            [sys.executable, "-c", HARNESS, case, str(log_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )

    def assert_case_succeeded(
        self, case: str, log_path: Path
    ) -> subprocess.CompletedProcess[str]:
        result = self.run_case(case, log_path)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"{case} exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertTrue(result.stdout.strip(), f"{case} emitted no measurements")
        print(f"{case}: {result.stdout.strip()}")
        if result.stderr:
            print(f"{case} stderr: {result.stderr.strip()}")
        return result

    def test_healthy_heartbeats_reset_deadline_without_dump(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-healthy-") as directory:
            self.assert_case_succeeded("healthy", Path(directory) / "watchdog.log")

    def test_blocked_loop_persists_native_traceback_with_blocker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-stall-") as directory:
            self.assert_case_succeeded("stall", Path(directory) / "watchdog.log")

    def test_stop_cleans_up_timer_without_later_writes_and_keeps_crash_handler(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-stop-") as directory:
            self.assert_case_succeeded("stop", Path(directory) / "watchdog.log")

    def test_preexisting_file_at_limit_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-preexisting-") as directory:
            self.assert_case_succeeded(
                "preexisting_cap", Path(directory) / "watchdog.log"
            )

    def test_post_stall_dump_over_limit_stops_rearming(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-post-cap-") as directory:
            self.assert_case_succeeded("post_stall_cap", Path(directory) / "watchdog.log")

    def test_arm_runtime_error_is_logged_and_contained(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-arm-error-") as directory:
            result = self.assert_case_succeeded(
                "arm_failure", Path(directory) / "watchdog.log"
            )
            self.assertIn("Stall watchdog disabled after a diagnostic failure", result.stderr)

    def test_missing_parent_is_logged_and_contained(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-missing-parent-") as directory:
            result = self.assert_case_succeeded("missing_parent", Path(directory))
            self.assertIn("Stall watchdog disabled after a diagnostic failure", result.stderr)
            self.assertIn("FileNotFoundError", result.stderr)

    def test_cancellation_during_executor_open_closes_returned_stream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-cancel-open-") as directory:
            self.assert_case_succeeded("cancel_open", Path(directory) / "watchdog.log")

    def test_cancellation_during_recovery_joins_worker_before_close(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-cancel-recovery-") as directory:
            self.assert_case_succeeded(
                "cancel_recovery", Path(directory) / "watchdog.log"
            )

    def test_repeated_cancellation_during_executor_open_preserves_ownership(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-cancel-open-repeat-") as directory:
            self.assert_case_succeeded(
                "cancel_open_repeated", Path(directory) / "watchdog.log"
            )

    def test_repeated_cancellation_during_recovery_preserves_ownership(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-cancel-recovery-repeat-") as directory:
            self.assert_case_succeeded(
                "cancel_recovery_repeated", Path(directory) / "watchdog.log"
            )

    def test_failed_native_cancel_retains_stream_until_manual_cancel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-cancel-failure-") as directory:
            result = self.assert_case_succeeded(
                "native_cancel_failure", Path(directory) / "watchdog.log"
            )
            self.assertIn("Could not cancel the stall watchdog", result.stderr)

    def test_measure_one_thousand_native_cancel_rearm_cycles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="watchdog-measure-") as directory:
            result = self.assert_case_succeeded("measure", Path(directory) / "watchdog.log")
            self.assertIn('"cycles": 1000', result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
