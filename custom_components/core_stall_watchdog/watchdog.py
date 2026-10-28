"""Preserve thread stacks when the event loop cannot run its own diagnostics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import faulthandler
import logging
import os
from pathlib import Path
import time
from typing import BinaryIO

_LOGGER = logging.getLogger(__name__)
# If native cancellation itself fails, its timer may still hold the descriptor.
_RETAINED_STREAMS: list[BinaryIO] = []


async def _finish_worker(future: asyncio.Future):
    """Defer repeated task cancellation until a file worker releases ownership."""
    while True:
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.cancelled():
                raise


def _open_log(path: Path, max_bytes: int) -> BinaryIO | None:
    stream = path.open("ab", buffering=0)
    try:
        if os.fstat(stream.fileno()).st_size >= max_bytes:
            stream.close()
            return None
        stream.write(f"\nWatchdog armed at {datetime.now(timezone.utc).isoformat()}\n".encode())
    except Exception:
        stream.close()
        raise
    return stream


def _record_recovery(stream: BinaryIO, previous: str, gap: float, max_bytes: int) -> bool:
    stream.write(
        (
            f"Heartbeat recovered at {datetime.now(timezone.utc).isoformat()}, "
            f"after {gap:.3f}s; previous heartbeat {previous}\n"
        ).encode()
    )
    return os.fstat(stream.fileno()).st_size < max_bytes


async def run_watchdog(
    stop: asyncio.Event,
    path: Path,
    *,
    timeout: float = 15,
    interval: float = 5,
    max_bytes: int = 5 * 1024 * 1024,
) -> None:
    """Rearm a native watchdog only while the event loop keeps making progress.

    Owns the process-global faulthandler timeout watchdog, not its crash handler.
    One native dump may exceed max_bytes; the limit prevents arming another dump.
    """
    if not 0 < interval < timeout or max_bytes <= 0:
        raise ValueError("Require 0 < interval < timeout and a positive log limit")
    loop = asyncio.get_running_loop()
    stream = None
    armed = False
    try:
        opening = loop.run_in_executor(None, _open_log, path, max_bytes)
        try:
            stream = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The worker still owns its result until it finishes opening it.
            stream = await _finish_worker(opening)
            raise
        if stream is None:
            _LOGGER.warning("Stall watchdog log reached its limit: %s", path)
            return
        previous_monotonic = time.monotonic()
        previous_utc = datetime.now(timezone.utc).isoformat()
        while not stop.is_set():
            now = time.monotonic()
            gap = now - previous_monotonic
            if gap >= timeout:
                # Join any native writer before appending the recovery marker.
                faulthandler.cancel_dump_traceback_later()
                armed = False
                _LOGGER.warning("Core heartbeat delayed %.3fs; inspect %s", gap, path)
                recording = loop.run_in_executor(
                    None, _record_recovery, stream, previous_utc, gap, max_bytes
                )
                try:
                    can_arm = await asyncio.shield(recording)
                except asyncio.CancelledError:
                    await _finish_worker(recording)
                    raise
                if not can_arm:
                    _LOGGER.warning("Stall watchdog log reached its limit: %s", path)
                    return
            faulthandler.dump_traceback_later(timeout, repeat=False, file=stream, exit=False)
            armed = True
            previous_monotonic = time.monotonic()
            previous_utc = datetime.now(timezone.utc).isoformat()
            try:
                async with asyncio.timeout(interval):
                    await stop.wait()
            except TimeoutError:
                pass
    except Exception:
        _LOGGER.exception("Stall watchdog disabled after a diagnostic failure")
    finally:
        cancel_succeeded = True
        try:
            if armed:
                faulthandler.cancel_dump_traceback_later()
        except Exception:
            cancel_succeeded = False
            _LOGGER.exception("Could not cancel the stall watchdog")
        finally:
            if stream is not None:
                if not cancel_succeeded:
                    _RETAINED_STREAMS.append(stream)
                else:
                    try:
                        closing = loop.run_in_executor(None, stream.close)
                        try:
                            await asyncio.shield(closing)
                        except asyncio.CancelledError:
                            await _finish_worker(closing)
                            raise
                    except Exception:
                        _LOGGER.exception("Could not close the stall watchdog log")
