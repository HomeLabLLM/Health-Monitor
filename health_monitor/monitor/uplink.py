"""The monitor's connection to the manager.

A persistent WebSocket over mutual TLS, monitor-initiated so a box
behind NAT can still reach out, with a channel *back* so the manager can
push subscriptions, settings and renames.

Store-and-forward lives here: every batch handed over comes from the
outbox, and the outbox deletes it only when the manager's ``ack``
arrives.  On any disconnect the in-flight tags are cleared and the rows
go again -- the manager's inserts are idempotent, so a resend is
harmless there.  Catch-up after an outage runs in parallel with live
data but rate-limited, so a week's backlog neither starves the live
stream nor floods the manager.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

import aiohttp

from .. import proto, tls
from ..store.outbox import Outbox

log = logging.getLogger("health-monitor.uplink")

BACKOFF_MIN, BACKOFF_MAX = 2.0, 60.0
INFLIGHT_MAX = 8            # batches awaiting ack before we stop sending
PING_EVERY = 15.0


class Uplink:
    def __init__(self, url: str, certs: str, outbox: Outbox, *,
                 hello: Callable[[], dict],
                 on_message: Callable[[dict], Awaitable[None]],
                 catchup_batch: int = 5000, catchup_rate: int = 20000) -> None:
        self.url = url
        self.certs = certs
        self.outbox = outbox
        self._hello = hello
        self._on_message = on_message
        self.catchup_batch = max(100, catchup_batch)
        self.catchup_rate = max(1000, catchup_rate)
        self.connected = False
        self.server_time_skew = 0.0
        self.last_ack = 0.0
        self.sent_rows = 0
        self.acked_rows = 0
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._inflight: dict[int, int] = {}          # batch -> rows
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stop = False
        self._reason = "not started"

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="uplink")

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def poke(self) -> None:
        """New data in the outbox: wake the sender."""
        self._wake.set()

    def status(self) -> dict:
        return {"connected": self.connected, "url": self.url, "reason": self._reason,
                "inflight_batches": len(self._inflight), "sent_rows": self.sent_rows,
                "acked_rows": self.acked_rows, "last_ack": self.last_ack,
                "skew": self.server_time_skew}

    # ------------------------------------------------------------------ #
    async def send(self, t: str, **body) -> bool:
        if self._ws is None or self._ws.closed:
            return False
        async with self._send_lock:
            try:
                await self._ws.send_str(proto.encode(t, **body))
                return True
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                log.debug("send failed: %s", exc)
                return False

    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        backoff = BACKOFF_MIN
        while not self._stop:
            try:
                await self._session()
                backoff = BACKOFF_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                self._reason = f"{type(exc).__name__}: {exc}"
                log.warning("uplink to %s: %s", self.url, self._reason)
            self.connected = False
            self.outbox.reset_inflight()
            self._inflight.clear()
            if self._stop:
                break
            delay = backoff + random.uniform(0, backoff / 2)
            log.info("reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)
            backoff = min(BACKOFF_MAX, backoff * 2)

    async def _session(self) -> None:
        ssl_ctx = tls.client_context(self.certs)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(self.url, ssl=ssl_ctx, heartbeat=None,
                                          max_msg_size=64 * 1024 * 1024) as ws:
                self._ws = ws
                t0 = time.time()
                await self.send(proto.HELLO, **self._hello())
                first = await ws.receive(timeout=30)
                if first.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError(f"expected hello_ok, got {first.type}")
                msg = proto.decode(first.data)
                if msg.get("t") == proto.REFUSE:
                    self._reason = f"refused: {msg.get('reason')}"
                    raise ConnectionError(self._reason)
                if msg.get("t") != proto.HELLO_OK:
                    raise ConnectionError(f"expected hello_ok, got {msg.get('t')}")
                proto.check_proto(msg)
                rtt = time.time() - t0
                self.server_time_skew = float(msg.get("server_time", time.time())) - (t0 + rtt / 2)
                if abs(self.server_time_skew) > 5:
                    log.warning("clock skew vs manager: %+.1fs", self.server_time_skew)
                self.connected = True
                self._reason = "connected"
                log.info("connected to %s (proto %s, skew %+.2fs)", self.url,
                         msg.get("proto"), self.server_time_skew)
                await self._on_message(msg)      # settings / subscribe carried in hello_ok

                sender = asyncio.create_task(self._sender(), name="uplink-send")
                pinger = asyncio.create_task(self._pinger(), name="uplink-ping")
                try:
                    async for frame in ws:
                        if frame.type == aiohttp.WSMsgType.TEXT:
                            m = proto.decode(frame.data)
                            if m.get("t") == proto.ACK:
                                self._on_ack(int(m["batch"]))
                            elif m.get("t") == proto.PING:
                                await self.send(proto.PONG, t_=m.get("t_", time.time()))
                            else:
                                await self._on_message(m)
                        elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                            aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    sender.cancel()
                    pinger.cancel()
                    self._ws = None
                self._reason = "closed by peer"
                raise ConnectionError("connection closed")

    def _on_ack(self, batch: int) -> None:
        rows = self._inflight.pop(batch, 0)
        n = self.outbox.ack(batch)
        self.acked_rows += n
        self.last_ack = time.time()
        if rows and n != rows:
            log.debug("ack %d: expected %d rows, deleted %d", batch, rows, n)
        self._wake.set()

    async def _sender(self) -> None:
        """Drain the outbox: live and backlog alike, rate-limited."""
        loop = asyncio.get_running_loop()
        while True:
            if len(self._inflight) >= INFLIGHT_MAX:
                self._wake.clear()
                await self._wake.wait()
                continue
            got = await loop.run_in_executor(None, self.outbox.next_batch, self.catchup_batch)
            ev = await loop.run_in_executor(None, self.outbox.next_events)
            if ev is not None:
                batch, rows = ev
                if await self.send(proto.EVENTS, batch=batch, rows=rows):
                    self._inflight[batch] = 0
            if got is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue
            batch, rows = got
            if not await self.send(proto.SAMPLES, batch=batch, rows=rows):
                self.outbox.reset_inflight()
                return
            self._inflight[batch] = len(rows)
            self.sent_rows += len(rows)
            # Pace catch-up: a full batch means there is backlog.
            if len(rows) >= self.catchup_batch:
                await asyncio.sleep(len(rows) / self.catchup_rate)

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            if not await self.send(proto.PONG, t_=time.time()):
                return
