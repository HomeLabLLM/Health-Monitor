"""Per-port table view."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from ..poller import PollerEngine
from ..topology import Wiring
from .format import fmt_bps, heat_style, mini_bar, sparkline


def build_table(engine: PollerEngine, wiring: Wiring | None, line_rate_bps: float) -> Table:
    tab = Table(
        expand=True,
        pad_edge=False,
        header_style="bold cyan",
        border_style="grey30",
        title=None,
    )
    tab.add_column("GPU", justify="right", style="bold", no_wrap=True)
    tab.add_column("Port", justify="right", no_wrap=True)
    tab.add_column("Type", no_wrap=True)
    tab.add_column("Link", justify="center", no_wrap=True)
    tab.add_column("Peer", no_wrap=True)
    tab.add_column("Rx", justify="right", no_wrap=True)
    tab.add_column("Tx", justify="right", no_wrap=True)
    tab.add_column("Util", no_wrap=True)
    tab.add_column("History", no_wrap=True)
    tab.add_column("Errs", justify="right", no_wrap=True)

    for gpu in range(engine.gpu_count()):
        for st in engine.ports_for_gpu(gpu):
            if st.link_up is None:
                link_txt = Text("?", style="grey50")
            elif st.link_up:
                link_txt = Text("UP", style="green")
            else:
                link_txt = Text("DOWN", style="grey50")

            peer = wiring.peer(st.gpu, st.port) if wiring else None
            if peer:
                peer_txt = Text(f"G{peer[0]}:p{peer[1]}", style="cyan3")
            elif wiring and (rp := wiring.routed_peer(st.gpu, st.port)):
                peer_txt = Text(f"G{rp[0]}:p{rp[1]} sw", style="magenta3")
            else:
                peer_txt = Text("—", style="grey37")

            util = max(st.rx_smooth, st.tx_smooth) / line_rate_bps if line_rate_bps else 0.0
            util_txt = Text(mini_bar(max(st.rx_smooth, st.tx_smooth), line_rate_bps, 8))
            util_txt.stylize(heat_style(util))
            util_txt.append(f" {util * 100:5.1f}%", style="grey62")

            ptype = Text("ext", style="magenta3") if st.external else Text("int", style="grey62")

            errs = Text(str(st.errors_total))
            if st.errors_total > 0:
                errs.stylize("bold red" if st.had_errors_delta else "yellow3")

            row_style = "dim" if (st.link_up is False) else None
            if st.stale:
                row_style = "bold red"

            tab.add_row(
                str(st.gpu),
                str(st.port),
                ptype,
                link_txt,
                peer_txt,
                Text(fmt_bps(st.rx_smooth)),
                Text(fmt_bps(st.tx_smooth)),
                util_txt,
                Text(sparkline(list(st.spark), 20), style="dodger_blue2"),
                errs,
                style=row_style,
            )
    return tab
