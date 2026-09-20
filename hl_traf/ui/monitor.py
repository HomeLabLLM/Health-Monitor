"""nvtop-style TUI, fed from the server's JSON API.

Pages are reached from the F-key bar along the bottom and give the TUI
near-parity with the web UI: profiles, series selection (including the
ADD GROUP pre-select-then-unselect flow), time ranges, the database
switcher and the engine settings.

Colour follows the same rule as the web UI -- one hue per base unit,
shades of it within a unit -- so a graph reads the same in both places.
"""

from __future__ import annotations

import os
import sys
import termios
import time
import tty
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .canvas import Trace, plot
from ..client import Client, ServerUnavailable

# One hue family per unit, mirroring UNIT_HUE in the web UI.
UNIT_COLORS: dict[str, list[str]] = {
    "C":     ["orange1", "dark_orange", "orange3", "light_salmon1", "indian_red"],
    "W":     ["yellow1", "yellow3", "gold1", "khaki1", "wheat1"],
    "V":     ["green1", "green3", "spring_green2", "pale_green1", "dark_sea_green"],
    "A":     ["cyan1", "cyan3", "turquoise2", "light_cyan1", "dark_turquoise"],
    "%":     ["dodger_blue1", "deep_sky_blue2", "steel_blue1", "cornflower_blue",
              "light_sky_blue1"],
    "B/s":   ["medium_purple1", "purple", "violet", "plum1", "orchid"],
    "MHz":   ["magenta1", "magenta3", "hot_pink", "pink1", "orchid1"],
    "GT/s":  ["medium_orchid", "purple3"],
    "tok/s": ["spring_green1", "aquamarine1", "sea_green2"],
    "s":     ["deep_pink2", "hot_pink2", "pink3"],
    "B":     ["slate_blue1", "medium_purple3"],
    "mJ":    ["light_goldenrod1", "yellow4"],
    "count": ["grey70", "grey54", "grey82", "grey42"],
}
MAX_AXES = 4
QUICK_RANGES = [("5m", 300), ("30m", 1800), ("1h", 3600), ("3h", 10800),
                ("8h", 28800), ("24h", 86400), ("7d", 604800)]


def color_for(unit: str, i: int) -> str:
    pal = UNIT_COLORS.get(unit, UNIT_COLORS["count"])
    return pal[i % len(pal)]


def fmt(v, unit: str) -> str:
    if v is None:
        return "–"
    if unit in ("B", "B/s"):
        u, x, i = ["B", "KiB", "MiB", "GiB", "TiB"], abs(v), 0
        while x >= 1024 and i < 4:
            x /= 1024
            i += 1
        return f"{'-' if v < 0 else ''}{x:.1f} {u[i]}" + ("/s" if unit == "B/s" else "")
    a = abs(v)
    d = 0 if a >= 1000 else 1 if a >= 100 else 2 if a >= 1 else 4
    return f"{v:.{d}f}{(' ' + unit) if unit and unit != 'count' else ''}"


@dataclass
class State:
    client_id: str
    catalog: dict = field(default_factory=dict)
    groups: list = field(default_factory=list)
    dbs: list = field(default_factory=list)
    live_db: str | None = None
    replay: bool = False
    profile_name: str | None = None
    profile_version: int | None = None
    config: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    page: str = "graphs"
    message: str = ""
    dirty: bool = False
    sel: int = 0                      # cursor within the current page
    data: dict = field(default_factory=dict)   # graph index -> traces


class Monitor:
    def __init__(self, api: Client, console: Console | None = None) -> None:
        self.api = api
        self.console = console or Console()
        self.st = State(client_id="tui-" + hex(int(time.time() * 1000))[-8:])
        self._last_fetch = 0.0

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        cat = self.api.catalog()
        self.st.catalog = {s["key"]: s for s in cat["series"]}
        self.st.groups = [g for g in cat["groups"] if g["keys"]]
        self.st.replay = cat.get("replay", False)
        status = self.api.status()
        self.st.live_db = status.get("live_db")
        self.st.status = status
        self.st.dbs = self.api.databases()
        profs = self.api.profiles()
        if profs:
            self.open_profile(profs[0]["name"])
        else:
            self.st.config = {"graphs": [], "range": {"mode": "last", "seconds": 1800}}

    def open_profile(self, name: str) -> None:
        p = self.api.profile(name)
        if p.get("_status"):
            self.st.message = f"cannot open {name}: {p.get('error')}"
            return
        self.st.profile_name = p["name"]
        self.st.profile_version = p["version"]
        cfg = p.get("config") or {}
        cfg.setdefault("graphs", [])
        cfg.setdefault("range", {"mode": "last", "seconds": 1800})
        if not cfg.get("sources"):
            cfg["sources"] = [{"dbs": [self.st.live_db] if self.st.live_db else [],
                               "shift": 0, "label": "A"}]
        self.st.config = cfg
        self.st.dirty = False
        self.push_subscription()

    def push_subscription(self) -> None:
        keys = sorted({k for g in self.st.config.get("graphs", [])
                       for k in g.get("series", [])})
        try:
            self.api.subscribe(self.st.client_id, keys)
        except ServerUnavailable:
            pass

    # ------------------------------------------------------------------ #
    def window(self) -> tuple[float, float]:
        r = self.st.config.get("range") or {"mode": "last", "seconds": 1800}
        now = time.time()
        if r.get("mode") == "abs":
            return float(r["t0"]), float(r["t1"])
        return now - float(r.get("seconds", 1800)), now

    def sources(self) -> list[dict]:
        return self.st.config.get("sources") or [
            {"dbs": [self.st.live_db] if self.st.live_db else [],
             "shift": 0, "label": "A"}]

    def fetch(self, width: int) -> None:
        graphs = self.st.config.get("graphs", [])
        keys = sorted({k for g in graphs for k in g.get("series", [])
                       if k in self.st.catalog})
        if not keys:
            self.st.data = {}
            return
        t0, t1 = self.window()
        srcs = [s for s in self.sources() if s.get("dbs")]
        if not srcs:
            return
        res = self.api.query(keys, t0, t1, srcs,
                             max_points=max(120, width * 2))
        if res.get("_status"):
            self.st.message = f"query failed: {res.get('error')}"
            return
        self.st.data = {"t0": t0, "t1": t1, "sources": res["sources"]}

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def render(self, width: int, height: int):
        head = self._header(width)
        foot = self._fkeys(width)
        body_h = max(6, height - 4)
        page = {
            "graphs": self._page_graphs,
            "add": self._page_add,
            "group": self._page_group,
            "profiles": self._page_profiles,
            "range": self._page_range,
            "dbs": self._page_dbs,
            "settings": self._page_settings,
            "nic": self._page_nic,
            "help": self._page_help,
        }.get(self.st.page, self._page_graphs)
        return Group(head, page(width, body_h), foot)

    def _header(self, width: int) -> Text:
        st = self.st.status.get("engine") or {}
        rec = st.get("recorder") or {}
        t = Text()
        t.append(" hl-traf ", style="bold white on dark_blue")
        t.append(f"  profile: {self.st.profile_name or '-'}")
        if self.st.dirty:
            t.append("  [unsaved]", style="yellow")
        if self.st.replay:
            t.append("  REPLAY — no live data", style="bold yellow")
        elif rec:
            if rec.get("recording"):
                t.append("  ● recording", style="green")
            else:
                t.append(f"  RECORDING STOPPED — {rec.get('reason', 'ERROR')}",
                         style="bold red")
        if st:
            t.append(f"  {st.get('sweep_ms', 0)}ms/sweep", style="grey62")
            t.append(f"  {st.get('active_series', 0)} read/"
                     f"{st.get('recorded_series', 0)} rec", style="grey62")
        r = self.st.config.get("range") or {}
        if r.get("mode") == "abs":
            t.append("  range: fixed", style="grey62")
        else:
            t.append(f"  last {_secs(r.get('seconds', 1800))}", style="grey62")
        return t

    def _fkeys(self, width: int) -> Text:
        keys = [("F1", "help"), ("F2", "profiles"), ("F3", "add"),
                ("F4", "group"), ("F5", "range"), ("F6", "dbs"),
                ("F7", "settings"), ("F8", "save"), ("F9", "fabric"),
                ("F10", "quit")]
        t = Text()
        for k, lbl in keys:
            t.append(f" {k} ", style="bold black on grey70")
            t.append(f"{lbl} ", style="grey74")
        if self.st.message:
            t.append("  " + self.st.message[: max(10, width - 70)],
                     style="yellow")
        return t

    # -- graphs --------------------------------------------------------- #
    def _traces_for(self, g: dict) -> tuple[list[Trace], list[str]]:
        keys = [k for k in g.get("series", []) if k in self.st.catalog]
        units: list[str] = []
        for k in keys:
            u = self.st.catalog[k]["unit"]
            if u not in units:
                units.append(u)
        units = units[:MAX_AXES]
        srcs = self.st.data.get("sources") or []
        traces: list[Trace] = []
        for u in units:
            in_unit = [k for k in keys if self.st.catalog[k]["unit"] == u]
            for i, k in enumerate(in_unit):
                for si, sr in enumerate(srcs):
                    pts = sr.get("data", {}).get(k) or []
                    if not pts and si > 0:
                        continue
                    label = self.st.catalog[k]["label"]
                    if len(srcs) > 1:
                        label = f"{sr.get('label') or chr(65+si)}·{label}"
                    traces.append(Trace(
                        label=label, color=color_for(u, i),
                        points=[(p[0], p[1]) for p in pts], unit=u))
        return traces, units

    def _page_graphs(self, width: int, height: int):
        graphs = self.st.config.get("graphs", [])
        if not graphs:
            return Panel(Text("No graphs in this profile.\n"
                              "F3 adds series, F4 adds a whole group.",
                              justify="center"),
                         title="graphs", border_style="grey37")
        t0 = self.st.data.get("t0", 0.0)
        t1 = self.st.data.get("t1", 1.0)
        per = max(6, (height - len(graphs)) // max(1, len(graphs)))
        out = []
        for gi, g in enumerate(graphs):
            traces, units = self._traces_for(g)
            plot_cols = max(20, width - 12)
            rows, lo, hi = plot([t for t in traces if t.points],
                                plot_cols, max(3, per - 3), t0, t1)
            body = Table.grid(padding=0)
            body.add_column(width=10, justify="right")
            body.add_column()
            n = len(rows)
            for ri, line in enumerate(rows):
                if ri == 0:
                    axis = fmt(hi, units[0] if units else "")
                elif ri == n - 1:
                    axis = fmt(lo, units[0] if units else "")
                else:
                    axis = ""
                body.add_row(Text(axis, style="grey50"), Text.from_markup(line))
            legend = Text()
            for tr in traces:
                cur = next((v for _, v in reversed(tr.points) if v is not None),
                           None)
                legend.append("━ ", style=tr.color)
                legend.append(f"{tr.label} ", style="grey74")
                legend.append(f"{fmt(cur, tr.unit)}   ", style="white")
            if not traces:
                legend.append("no data in this window", style="grey50")
            sel = " ◀" if gi == self.st.sel and self.st.page == "graphs" else ""
            axes = ("  axes: " + ", ".join(units)) if len(units) > 1 else ""
            out.append(Panel(
                Group(body, legend),
                title=f"[bold]{g.get('title', 'Graph')}[/]{axes}{sel}",
                border_style="cyan" if sel else "grey37"))
        return Group(*out)

    # -- series picker -------------------------------------------------- #
    def _page_add(self, width: int, height: int):
        rows = self._series_rows()
        return self._list_panel(
            "ADD series  (↑↓ move, SPACE toggle, ENTER add to graph "
            f"'{self._cur_graph_title()}', ESC back)", rows, height)

    def _series_rows(self) -> list[tuple[str, str]]:
        out = []
        for g in self.st.groups:
            out.append((f"__group__{g['name']}",
                        f"[bold]{g['label']}[/]  "
                        f"[grey50]{len(g['keys'])}"
                        f"{' · ' + g['unit'] if g.get('unit') else ' · mixed'}[/]"))
            for k in g["keys"]:
                s = self.st.catalog.get(k)
                if not s:
                    continue
                mark = "✓" if k in self._cur_series() else " "
                note = f"  [grey42]{s['note']}[/]" if s.get("note") else ""
                out.append((k, f"  [{mark}] {s['label']}{note}"))
        return out

    def _page_group(self, width: int, height: int):
        """ADD GROUP: everything preselected, unselect, then confirm."""
        pend = getattr(self, "_pending_group", None)
        if pend is None:
            rows = [(g["name"],
                     f"{g['label']}  [grey50]{len(g['keys'])}"
                     f"{' · ' + g['unit'] if g.get('unit') else ' · mixed'}[/]")
                    for g in self.st.groups]
            return self._list_panel(
                "ADD GROUP — choose a group (ENTER to open, ESC back)",
                rows, height)
        g, chosen = pend
        rows = []
        for k in g["keys"]:
            s = self.st.catalog.get(k)
            if not s:
                continue
            already = k in self._cur_series()
            mark = "-" if already else ("✓" if k in chosen else " ")
            suffix = "  [grey42]already on graph[/]" if already else ""
            rows.append((k, f"[{mark}] {s['label']}{suffix}"))
        title = (f"{g['label']} — {len(chosen)}/{len(g['keys'])} selected  "
                 f"(SPACE unselect, a=all, n=none, ENTER=ADD, ESC=Cancel)")
        return self._list_panel(title, rows, height)

    def _list_panel(self, title: str, rows: list[tuple[str, str]], height: int):
        view = max(4, height - 2)
        if self.st.sel >= len(rows):
            self.st.sel = max(0, len(rows) - 1)
        top = max(0, min(self.st.sel - view // 2, max(0, len(rows) - view)))
        body = Text()
        for i in range(top, min(len(rows), top + view)):
            _key, label = rows[i]
            if i == self.st.sel:
                body.append("▶ ", style="bold cyan")
                body.append_text(Text.from_markup(label))
                body.append("\n")
            else:
                body.append("  ")
                body.append_text(Text.from_markup(label))
                body.append("\n")
        if len(rows) > view:
            body.append(f"  [{self.st.sel + 1}/{len(rows)}]", style="grey50")
        return Panel(body, title=title, border_style="cyan")

    # -- other pages ---------------------------------------------------- #
    def _page_profiles(self, width: int, height: int):
        profs = self.api.profiles()
        self._profile_rows = profs
        rows = [(p["name"],
                 f"{p['name']}  [grey50]v{p['version']}  "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p['updated_at']))}"
                 f"  {p['updated_by'][:18]}[/]"
                 + ("  [cyan](open)[/]" if p["name"] == self.st.profile_name else ""))
                for p in profs]
        return self._list_panel(
            "Profiles  (ENTER open, d delete, ESC back)", rows, height)

    def _page_range(self, width: int, height: int):
        r = self.st.config.get("range") or {}
        rows = [(str(secs), f"last {lbl}"
                 + ("   [cyan]◀ current[/]"
                    if r.get("mode") == "last" and r.get("seconds") == secs else ""))
                for lbl, secs in QUICK_RANGES]
        if self.st.replay:
            rows.append(("__note__",
                         "[grey50]live ranges are still valid; this server is "
                         "serving an archive so they simply end where it ends[/]"))
        return self._list_panel("Time range  (ENTER select, ESC back)",
                                rows, height)

    def _page_dbs(self, width: int, height: int):
        src = self.sources()[0]
        rows = []
        for db in self.st.dbs:
            mark = "✓" if db["path"] in src.get("dbs", []) else " "
            span = (f"{time.strftime('%m-%d %H:%M', time.localtime(db['start']))}"
                    f" → {time.strftime('%m-%d %H:%M', time.localtime(db['end']))}"
                    if db.get("start") else "empty")
            live = "[green]● live[/]" if db["live"] else "      "
            mib = db["bytes"] / 1024**2
            err = f"  [red]{db['error'][:30]}[/]" if db.get("error") else ""
            rows.append((db["path"],
                         f"[{mark}] {live} {db['name']:28s} "
                         f"[grey50]{span}  {mib:7.1f} MiB  "
                         f"{db['series']} series[/]{err}"))
        return self._list_panel(
            "Databases  (SPACE toggle — several = stitched timeline, ESC back)",
            rows, height)

    def _page_settings(self, width: int, height: int):
        s = (self.api.settings() or {}).get("settings", {})
        self._settings_cache = s
        rows = [
            ("record_interval", f"Recording interval      {s.get('record_interval')} s"
                                "   [grey50](+/- to change)[/]"),
            ("record_exotic", f"Record exotic rails     "
                              f"{'yes' if s.get('record_exotic') else 'no'}"
                              "   [grey50](SPACE toggles)[/]"),
            ("record_nic_rates", f"Record NIC rates        "
                                 f"{'yes' if s.get('record_nic_rates') else 'no'}"),
            ("record_nic_errors", f"Record NIC errors       "
                                  f"{'yes' if s.get('record_nic_errors') else 'no'}"),
            ("min_free_bytes", f"Stop below free space   "
                               f"{s.get('min_free_bytes', 0) / 1024**3:.1f} GiB"),
            ("__note__", "[grey50]These apply to the engine, for everyone. "
                         "A graph's redraw rate is separate.[/]"),
        ]
        return self._list_panel("Engine settings  (ESC back)", rows, height)


    def _page_nic(self, width: int, height: int):
        """NIC fabric: per-port rates and the GPU-pair matrix.

        Served from the server's own poller, so opening this view costs
        no extra firmware reads -- which is the whole reason the TUI does
        not touch the device itself.
        """
        try:
            d = self.api.nic()
        except ServerUnavailable as exc:
            return Panel(Text(str(exc)), title="fabric", border_style="red")
        if not d.get("enabled"):
            return Panel(
                Text("The NIC fabric poller is not running.\n\n"
                     "Restart the server with --nic to enable the matrix and\n"
                     "table views:\n\n"
                     "    hl-traf serve --port 5678 --nic\n\n"
                     "It is off by default because the fabric sweep is another\n"
                     "~0.2-0.5s of firmware time per interval.",
                     justify="left"),
                title="fabric — disabled", border_style="grey37")

        ports = d.get("ports", [])
        gpus = d.get("gpus", {})
        links = d.get("links", [])

        tbl = Table(box=None, pad_edge=False, expand=False)
        for col, just in (("gpu", "right"), ("port", "right"), ("type", "left"),
                          ("link", "left"), ("rx", "right"), ("tx", "right"),
                          ("torn", "right")):
            tbl.add_column(col, justify=just, style="grey62")
        shown = [p for p in ports if p.get("up") or p["rx"] or p["tx"]]
        if not shown:
            shown = ports[: max(4, height - 12)]
        for p in shown[: max(4, height - 12)]:
            up = p.get("up")
            tbl.add_row(
                str(p["gpu"]), str(p["port"]),
                "ext" if p["external"] else "int",
                Text("up" if up else "down", style="green" if up else "grey42"),
                Text(fmt(p["rx"], "B/s"), style="cyan" if p["rx"] else "grey42"),
                Text(fmt(p["tx"], "B/s"), style="magenta" if p["tx"] else "grey42"),
                Text(str(p.get("torn") or ""), style="yellow" if p.get("torn") else ""),
            )

        head = Text()
        head.append(f"backend {d.get('backend')}  ", style="grey62")
        head.append(f"{d.get('sweep_secs')}s/sweep  ", style="grey62")
        head.append(f"{len(ports)} ports  ", style="grey62")
        head.append(f"{sum(1 for p in ports if p.get('up'))} up  ", style="green")
        head.append(f"{len(links)} known links", style="grey62")
        if not links:
            head.append("   (no topology cached — run 'hl-traf discover')",
                        style="yellow")

        gl = Text()
        for idx, g in sorted(gpus.items()):
            gl.append(f"\ngpu{idx}  ", style="bold")
            gl.append(f"util {g.get('util')}%  ", style="dodger_blue1")
            mp = g.get("mem_pct")
            gl.append(f"hbm {mp:.1f}%  " if mp is not None else "hbm  -   ",
                      style="dodger_blue1")
            pw = g.get("pwr_mw")
            gl.append(f"pwr {pw/1000:.0f}W  " if pw else "pwr  -   ",
                      style="yellow")
            gl.append(f"pcie rx {fmt(g.get('pcie_rx') or 0, 'B/s')}  ",
                      style="medium_purple1")
            gl.append(f"tx {fmt(g.get('pcie_tx') or 0, 'B/s')}",
                      style="medium_purple1")

        return Panel(Group(head, tbl, gl), title="NIC fabric  (ESC back)",
                     border_style="cyan")

    def _page_help(self, width: int, height: int):
        txt = Text.from_markup(
            "[bold]hl-traf TUI[/]\n\n"
            "This is a client of the monitoring server; the hardware is read\n"
            "once no matter how many viewers there are.\n\n"
            "[bold]graphs page[/]\n"
            "  ↑ ↓      select a graph\n"
            "  n        new graph        D  delete selected graph\n"
            "  x        remove the last series from the selected graph\n"
            "  r        refresh now\n\n"
            "[bold]everywhere[/]\n"
            "  F1 help      F2 profiles   F3 add series   F4 add group\n"
            "  F5 range     F6 databases  F7 settings     F8 save profile\n"
            "  F9 NIC fabric (server must run with --nic)\n"
            "  F10/q quit\n\n"
            "Series sharing a unit share a Y axis and a colour family;\n"
            "at most four axes per graph.\n"
            "Gaps in a line are real gaps — they are never bridged.\n")
        return Panel(txt, title="help", border_style="grey37")

    # ------------------------------------------------------------------ #
    def _cur_graph(self) -> dict | None:
        gs = self.st.config.get("graphs", [])
        if not gs:
            return None
        return gs[min(self.st.sel, len(gs) - 1)] if self.st.page == "graphs" \
            else gs[min(self._graph_idx, len(gs) - 1)]

    def _cur_series(self) -> list[str]:
        g = self._cur_graph()
        return g.get("series", []) if g else []

    def _cur_graph_title(self) -> str:
        g = self._cur_graph()
        return g.get("title", "?") if g else "-"


def _secs(s: float) -> str:
    s = int(s)
    if s % 86400 == 0:
        return f"{s // 86400}d"
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"

# ---------------------------------------------------------------------- #
# input handling and main loop
# ---------------------------------------------------------------------- #
KEYMAP = {
    "\x1bOP": "F1", "\x1b[11~": "F1",
    "\x1bOQ": "F2", "\x1b[12~": "F2",
    "\x1bOR": "F3", "\x1b[13~": "F3",
    "\x1bOS": "F4", "\x1b[14~": "F4",
    "\x1b[15~": "F5", "\x1b[17~": "F6", "\x1b[18~": "F7",
    "\x1b[19~": "F8", "\x1b[20~": "F9", "\x1b[21~": "F10",
    "\x1b[A": "UP", "\x1b[B": "DOWN", "\x1b[C": "RIGHT", "\x1b[D": "LEFT",
    "\x1b": "ESC", "\r": "ENTER", "\n": "ENTER", " ": "SPACE",
}

PAGE_KEYS = {"F1": "help", "F2": "profiles", "F3": "add", "F4": "group",
             "F5": "range", "F6": "dbs", "F7": "settings", "F9": "nic"}


class KeyReader:
    """Raw-mode keyboard reader with escape-sequence assembly."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None
        self._buf = ""

    def __enter__(self) -> "KeyReader":
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except termios.error:
            self.saved = None      # not a tty (piped); keys simply never arrive
        return self

    def __exit__(self, *exc) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def poll(self, timeout: float) -> str | None:
        """Return one key, or None if nothing arrived within `timeout`.

        Reads the file descriptor directly rather than sys.stdin, because
        the text wrapper buffers: after pulling one character the rest of
        an escape sequence sits in Python's buffer where select() cannot
        see it, and every function key reads as a bare ESC.

        A single read can also carry several keypresses -- key repeat, a
        fast typist, or a terminal replaying a paste.  They are held in
        `self._buf` and returned one at a time, so a burst of arrow keys
        is three arrow keys and not one unrecognised blob (which would
        otherwise decode as ESC and jump the user out of their page).
        """
        import select
        if not self._buf:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                return None
            try:
                data = os.read(self.fd, 256)
            except OSError:
                return None
            if not data:
                return None
            self._buf = data.decode("utf-8", "replace")

        # An escape sequence split across reads: give it a moment to land.
        if self._buf.startswith("\x1b") and not self._match(self._buf):
            for _ in range(3):
                r, _, _ = select.select([self.fd], [], [], 0.02)
                if not r:
                    break
                try:
                    more = os.read(self.fd, 256)
                except OSError:
                    break
                if not more:
                    break
                self._buf += more.decode("utf-8", "replace")
                if self._match(self._buf):
                    break

        # Longest match wins, so "\x1b[1" never shadows "\x1b[15~".
        for n in range(min(6, len(self._buf)), 0, -1):
            head = self._buf[:n]
            if head in KEYMAP:
                self._buf = self._buf[n:]
                return KEYMAP[head]
        ch, self._buf = self._buf[0], self._buf[1:]
        return KEYMAP.get(ch, ch)

    @staticmethod
    def _match(buf: str) -> bool:
        return any(buf[:n] in KEYMAP for n in range(min(6, len(buf)), 0, -1))



def _rows_for_page(mon: "Monitor") -> list[tuple[str, str]]:
    """The key list behind whatever the current page is showing."""
    page = mon.st.page
    if page == "add":
        return mon._series_rows()
    if page == "group":
        pend = getattr(mon, "_pending_group", None)
        if pend is None:
            return [(g["name"], g["label"]) for g in mon.st.groups]
        return [(k, k) for k in pend[0]["keys"]]
    if page == "profiles":
        return [(p["name"], p["name"]) for p in getattr(mon, "_profile_rows", [])]
    if page == "range":
        return [(str(s), lbl) for lbl, s in QUICK_RANGES]
    if page == "dbs":
        return [(d["path"], d["name"]) for d in mon.st.dbs]
    if page == "settings":
        return [(k, k) for k in ("record_interval", "record_exotic",
                                 "record_nic_rates", "record_nic_errors",
                                 "min_free_bytes")]
    return [("", "")] * len(mon.st.config.get("graphs", []))


def handle_key(mon: "Monitor", key: str) -> bool:
    """Returns False to quit."""
    st = mon.st
    rows = _rows_for_page(mon)

    if key in ("F10", "q") and st.page == "graphs":
        return False
    if key == "F10":
        return False
    if key in PAGE_KEYS:
        target = PAGE_KEYS[key]
        if target in ("add", "group") and not st.config.get("graphs"):
            st.message = "add a graph first (n on the graphs page)"
            return True
        if target in ("add", "group"):
            mon._graph_idx = st.sel
        if target == "group":
            mon._pending_group = None
        st.page = target
        st.sel = 0
        st.message = ""
        return True
    if key == "F8":
        _save(mon)
        return True
    if key == "ESC":
        if st.page == "group" and getattr(mon, "_pending_group", None):
            mon._pending_group = None      # back to the group list
            st.sel = 0
        else:
            st.page = "graphs"
            st.sel = 0
        return True

    if key == "UP":
        st.sel = max(0, st.sel - 1)
        return True
    if key == "DOWN":
        st.sel = min(max(0, len(rows) - 1), st.sel + 1)
        return True

    if st.page == "graphs":
        return _keys_graphs(mon, key)
    if st.page == "add":
        return _keys_add(mon, key, rows)
    if st.page == "group":
        return _keys_group(mon, key)
    if st.page == "profiles":
        return _keys_profiles(mon, key, rows)
    if st.page == "range":
        if key == "ENTER" and rows:
            st.config["range"] = {"mode": "last", "seconds": int(rows[st.sel][0])}
            st.dirty = True
            st.page = "graphs"
            st.sel = 0
    elif st.page == "dbs":
        if key == "SPACE" and rows:
            _toggle_db(mon, rows[st.sel][0])
    elif st.page == "settings":
        _keys_settings(mon, key, rows)
    return True


def _keys_graphs(mon: "Monitor", key: str) -> bool:
    st = mon.st
    graphs = st.config.setdefault("graphs", [])
    if key == "n":
        graphs.append({"title": f"Graph {len(graphs) + 1}", "series": [],
                       "update_ms": 1000})
        st.dirty = True
        st.sel = len(graphs) - 1
    elif key == "D" and graphs:
        g = graphs[min(st.sel, len(graphs) - 1)]
        st.message = (f"delete '{g.get('title')}'? press D again to confirm "
                      "(graph settings only; no samples are deleted)")
        if getattr(mon, "_confirm_del", None) == id(g):
            graphs.pop(min(st.sel, len(graphs) - 1))
            mon._confirm_del = None
            st.dirty = True
            st.sel = max(0, st.sel - 1)
            st.message = "graph deleted"
            mon.push_subscription()
        else:
            mon._confirm_del = id(g)
    elif key == "x" and graphs:
        g = graphs[min(st.sel, len(graphs) - 1)]
        if g.get("series"):
            dropped = g["series"].pop()
            st.dirty = True
            st.message = f"removed {dropped}"
            mon.push_subscription()
    elif key == "r":
        mon._last_fetch = 0.0
    return True


def _keys_add(mon: "Monitor", key: str, rows) -> bool:
    st = mon.st
    if not rows:
        return True
    k = rows[st.sel][0]
    if key == "SPACE" and not k.startswith("__group__"):
        g = mon._cur_graph()
        if g is not None:
            series = g.setdefault("series", [])
            if k in series:
                series.remove(k)
            else:
                if not _axis_ok(mon, series + [k]):
                    return True
                series.append(k)
            st.dirty = True
    elif key == "ENTER":
        st.page = "graphs"
        st.sel = getattr(mon, "_graph_idx", 0)
        mon.push_subscription()
        mon._last_fetch = 0.0
    return True


def _keys_group(mon: "Monitor", key: str) -> bool:
    st = mon.st
    pend = getattr(mon, "_pending_group", None)
    if pend is None:
        if key == "ENTER" and st.groups:
            g = st.groups[min(st.sel, len(st.groups) - 1)]
            cur = mon._cur_series()
            # Everything arrives selected; unselect what you don't want.
            mon._pending_group = (g, {k for k in g["keys"] if k not in cur})
            st.sel = 0
        return True

    g, chosen = pend
    if key == "SPACE" and g["keys"]:
        k = g["keys"][min(st.sel, len(g["keys"]) - 1)]
        if k in chosen:
            chosen.discard(k)
        elif k not in mon._cur_series():
            chosen.add(k)
    elif key == "a":
        cur = mon._cur_series()
        chosen.clear()
        chosen.update(k for k in g["keys"] if k not in cur)
    elif key == "n":
        chosen.clear()
    elif key == "ENTER":
        target = mon._cur_graph()
        if target is not None and chosen:
            series = target.setdefault("series", [])
            merged = series + [k for k in g["keys"]
                               if k in chosen and k not in series]
            if _axis_ok(mon, merged):
                target["series"] = merged
                st.dirty = True
                st.message = f"added {len(chosen)} from {g['label']}"
                mon.push_subscription()
                mon._last_fetch = 0.0
                mon._pending_group = None
                st.page = "graphs"
                st.sel = getattr(mon, "_graph_idx", 0)
    return True


def _axis_ok(mon: "Monitor", keys: list[str]) -> bool:
    units = []
    for k in keys:
        u = mon.st.catalog.get(k, {}).get("unit", "count")
        if u not in units:
            units.append(u)
    if len(units) > MAX_AXES:
        mon.st.message = (f"that needs {len(units)} Y axes ({', '.join(units)}); "
                          f"the limit is {MAX_AXES} — use another graph")
        return False
    return True


def _keys_profiles(mon: "Monitor", key: str, rows) -> bool:
    st = mon.st
    if not rows:
        return True
    name = rows[st.sel][0]
    if key == "ENTER":
        if st.dirty:
            st.message = "unsaved changes — F8 to save, or press ENTER again"
            if getattr(mon, "_confirm_switch", None) != name:
                mon._confirm_switch = name
                return True
        mon._confirm_switch = None
        mon.open_profile(name)
        st.page = "graphs"
        st.sel = 0
        mon._last_fetch = 0.0
    elif key == "d":
        if getattr(mon, "_confirm_delp", None) == name:
            mon.api.delete_profile(name)
            mon._confirm_delp = None
            st.message = f"deleted profile {name}"
            if name == st.profile_name:
                profs = mon.api.profiles()
                if profs:
                    mon.open_profile(profs[0]["name"])
        else:
            mon._confirm_delp = name
            st.message = (f"delete profile '{name}'? press d again to confirm "
                          "(graph settings only; no samples are deleted)")
    return True


def _toggle_db(mon: "Monitor", path: str) -> None:
    src = mon.sources()[0]
    dbs = list(src.get("dbs", []))
    if path in dbs:
        dbs.remove(path)
    else:
        dbs.append(path)
        chosen = [d for d in mon.st.dbs if d["path"] in dbs
                  and d.get("start") and d.get("end")]
        chosen.sort(key=lambda d: d["start"])
        for i in range(1, len(chosen)):
            if chosen[i]["start"] < chosen[i - 1]["end"]:
                mon.st.message = (f"{chosen[i-1]['name']} and {chosen[i]['name']} "
                                  "overlap in time; stitching would double-plot")
                return
    order = {d["path"]: (d.get("start") or 0) for d in mon.st.dbs}
    src["dbs"] = sorted(dbs, key=lambda p: order.get(p, 0))
    mon.st.config.setdefault("sources", [src])
    mon.st.dirty = True
    mon._last_fetch = 0.0


def _keys_settings(mon: "Monitor", key: str, rows) -> None:
    if not rows:
        return
    field = rows[mon.st.sel][0]
    cur = dict(getattr(mon, "_settings_cache", {}) or {})
    if not cur:
        return
    changed = False
    if key == "SPACE" and field in ("record_exotic", "record_nic_rates",
                                    "record_nic_errors"):
        cur[field] = not cur.get(field)
        changed = True
    elif key in ("+", "=", "-") and field in ("record_interval", "min_free_bytes"):
        step = 0.5 if field == "record_interval" else 0.5 * 1024**3
        delta = step if key in ("+", "=") else -step
        lo = 0.5 if field == "record_interval" else 0.1 * 1024**3
        cur[field] = max(lo, (cur.get(field) or 0) + delta)
        changed = True
    if changed:
        r = mon.api.save_settings(cur)
        if r.get("_status"):
            mon.st.message = f"settings failed: {r.get('error')}"
        else:
            mon._settings_cache = r.get("settings", cur)
            mon.st.message = f"recording {r.get('recorded_series')} series"


def _save(mon: "Monitor") -> None:
    st = mon.st
    if not st.profile_name:
        st.message = "no profile open"
        return
    r = mon.api.save_profile(st.profile_name, st.config,
                             st.profile_version, st.client_id)
    if r.get("_status") == 409:
        # Someone else saved while we were editing.
        st.message = (f"{r.get('name')} was updated by someone else — "
                      f"can't change. F2 to reload it, then try again.")
        st.profile_version = r.get("current_version")
    elif r.get("_status"):
        st.message = f"save failed: {r.get('error')}"
    else:
        st.profile_version = r["version"]
        st.dirty = False
        st.message = f"saved {r['name']} v{r['version']}"


def run(server: str) -> int:
    console = Console()
    api = Client(server)
    mon = Monitor(api, console)
    try:
        mon.load()
    except ServerUnavailable as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return 1

    status_every = 2.0
    last_status = 0.0
    with KeyReader() as keys, Live(console=console, screen=True,
                                   auto_refresh=False,
                                   redirect_stderr=False) as live:
        while True:
            now = time.time()
            width = console.size.width
            height = console.size.height

            interval = min((g.get("update_ms", 1000) / 1000.0
                            for g in mon.st.config.get("graphs", [])),
                           default=1.0)
            if now - mon._last_fetch >= max(0.25, interval):
                try:
                    mon.fetch(width)
                except ServerUnavailable as exc:
                    mon.st.message = str(exc).splitlines()[0]
                mon._last_fetch = now
            if now - last_status >= status_every:
                try:
                    mon.st.status = api.status()
                except ServerUnavailable:
                    mon.st.status = {}
                last_status = now

            live.update(mon.render(width, height), refresh=True)
            key = keys.poll(0.2)
            if key and not handle_key(mon, key):
                break
    return 0
