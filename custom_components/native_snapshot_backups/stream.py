"""Bounded async download producer with a blocking archive-reader interface."""

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
import threading


class ReadAborted(Exception):
    """The consumer was deliberately stopped."""


class DownloadError(Exception):
    """The remote stream failed, rather than the archive being invalid."""


class AsyncDownloadReader:
    def __init__(self, loop, source):
        self.loop = loop
        self.source = source
        self.queue = asyncio.Queue(maxsize=2)
        self.buffer = b""
        self.exhausted = False
        self.aborted = threading.Event()
        self.pending = None
        self.transport_failed = False
        self.producer = loop.create_task(self._produce())

    async def _produce(self):
        try:
            async for chunk in self.source:
                if chunk:
                    await self.queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.transport_failed = True
            await self.queue.put(DownloadError("B2 download interrupted"))
        else:
            await self.queue.put(None)

    def read(self, size=-1):
        if size < 0:
            raise ValueError("The archive download must be read in bounded chunks")
        result = bytearray()
        while len(result) < size:
            if self.aborted.is_set():
                raise ReadAborted
            if not self.buffer:
                if self.exhausted:
                    break
                self.pending = asyncio.run_coroutine_threadsafe(self.queue.get(), self.loop)
                if self.aborted.is_set():
                    self.pending.cancel()
                    raise ReadAborted
                try:
                    chunk = self.pending.result()
                except FutureCancelledError as exc:
                    raise ReadAborted from exc
                finally:
                    self.pending = None
                if isinstance(chunk, DownloadError):
                    raise chunk
                if chunk is None:
                    self.exhausted = True
                    break
                self.buffer = chunk
            take = min(size - len(result), len(self.buffer))
            result.extend(self.buffer[:take])
            self.buffer = self.buffer[take:]
        return bytes(result)

    def abort(self):
        self.aborted.set()
        if self.pending:
            self.pending.cancel()

    async def aclose(self):
        self.abort()
        self.producer.cancel()
        await asyncio.gather(self.producer, return_exceptions=True)
        if hasattr(self.source, "aclose"):
            await self.source.aclose()
