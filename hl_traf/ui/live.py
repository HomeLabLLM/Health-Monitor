"""Live TUI: rich Live screen with header, view, log panel, key input."""

from __future__ import annotations

import asyncio
import logging
import sys
import termios
import time
import tty
from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from ..hlml import Hlml
from ..log import PanelHandler
from ..poller import PollerEngine
from ..topology import Wiring
from . import matrix_view, table_view

log = logging.getLogger("hl-traf")

KEYS_HELP = "[q]uit   [m]atrix   [t]able   [p]ause"


class KeyReader:
    """Non-canonical single-key reader; None when stdin is not a tty."""

    def __init__(self):
        self._fd = None
        self._old = None
        if sys.stdin.isatty():
            try:
                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            except (termios.error, ValueError, OSError):
                self._fd = None

    def get(self) -> str | None:
        if self._fd is None:
            return None
        import select

        r, _, _ = select.select([self._fd], [], [], 0)
        if not r:
            return None
        ch = sys.stdin.read(1)
        return ch or None

    def close(self) -> None:
        if self._fd is not None and self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


class TuiApp:
    def __init__(
        self,
        hlml: Hlml,
        wiring: Wiring | None,
        panel_handler: PanelHandler,
        view: str = "matrix",
        interval: float = 1.0,
        history: int = 60,
        ports: str = "all",
        line_rate_gbps: float = 100.0,
        log_lines: int = 6,
        once: bool = False,
        width: int | None = None,
    ):
        self.hlml = hlml
        self.wiring = wiring
        self.panel = panel_handler
        self.view = view
        self.line_rate_bps = line_rate_gbps * 1e9
        self.log_lines = log_lines
        self.once = once
        self.paused = False
        self.engine = PollerEngine(
            hlml, interval=interval, history=history, ports=ports,
            line_rate_gbps=line_rate_gbps,
        )
        self.console = Console(width=width)

    # ------------------------------------------------------------------ #
    def _header(self) -> Text:
        txt = Text()
        txt.append("hl-traf", style="bold cyan")
        if self.console.width >= 110:
            txt.append(f"  driver {self.hlml.driver_version()}", style="grey62")
            txt.append(f"  nic {self.hlml.nic_driver_version()}", style="grey62")
        txt.append(f"  {self.engine.gpu_count()} GPUs", style="grey74")
        txt.append(f"  {self.engine.backend}", style="grey58")
        txt.append(f"  sweep {self.engine.last_sweep_secs:.2f}s", style="grey62")
        txt.append(f"  #{self.engine.sweep_count}", style="grey62")
        txt.append("  PAUSED" if self.paused else "", style="bold yellow")
        stamp = datetime.now().strftime("%H:%M:%S")
        pad = max(1, self.console.width - 4 - len(txt.plain) - len(stamp))
        txt.append(" " * pad)
        txt.append(stamp, style="grey62")
        return txt

    def _log_panel(self) -> Panel:
        lines = self.panel.render(self.log_lines) or [Text("(no log messages)", style="grey37")]
        return Panel(Group(*lines), title="log", border_style="grey30", padding=(0, 1))

    def render(self):
        body = (
            matrix_view.build_matrix(
                self.engine, self.wiring, self.line_rate_bps, width=self.console.width
            )
            if self.view == "matrix"
            else table_view.build_table(self.engine, self.wiring, self.line_rate_bps)
        )
        if self.view == "matrix":
            mid = Group(
                body,
                matrix_view.build_totals_line(self.engine, self.line_rate_bps),
                matrix_view.build_sparkline_strip(self.engine),
            )
        else:
            mid = body
        return Group(
            Panel(self._header(), border_style="cyan3", padding=(0, 1)),
            mid,
            self._log_panel(),
            Text(KEYS_HELP, style="grey50"),
        )

    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        await self.engine.start()
        keys = KeyReader()
        try:
            if self.once:
                await self._wait_primed()
                self.console.print(self.render())
                return
            from rich.live import Live

            with Live(self.render(), console=self.console, refresh_per_second=4, screen=True) as live:
                while True:
                    self._handle_keys(keys)
                    live.update(self.render())
                    await asyncio.sleep(0.25)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            keys.close()
            await self.engine.stop()

    async def _wait_primed(self, timeout: float = 30.0) -> None:
        """Wait until every polled port has at least one rate sample."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            states = list(self.engine.state.values())
            if states and all(s._prev is not None and (s.stale or s.last_update > 0) for s in states) \
               and any(s.spark for s in states):
                # one more sweep so deltas/rates exist
                await asyncio.sleep(max(0.5, self.engine.interval))
                if all(s.spark or s.stale or s.link_up is False for s in states):
                    return
            await asyncio.sleep(0.5)

    def _handle_keys(self, keys: KeyReader) -> None:
        ch = keys.get()
        while ch:
            if ch in ("q", "Q"):
                raise KeyboardInterrupt
            elif ch in ("m", "M"):
                self.view = "matrix"
                log.info("view: matrix")
            elif ch in ("t", "T"):
                self.view = "table"
                log.info("view: table")
            elif ch in ("p", "P"):
                self.engine.paused = not self.engine.paused
                self.paused = self.engine.paused
                log.info("paused" if self.paused else "resumed")
            ch = keys.get()
