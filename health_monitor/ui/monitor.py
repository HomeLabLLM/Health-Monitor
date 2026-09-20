"""nvtop-style TUI: a client of the web server.

Pages on the F-keys give near-parity with the web UI: profiles, series
selection with the ADD GROUP pre-select-then-unselect flow (per monitor),
time ranges, the database switcher, the fleet page and -- for admins --
monitor settings and nicknames.  Series are refs (monitor/gpu/key) and
colour follows the same one-hue-per-unit rule as the web UI.
"""

from __future__ import annotations

import os
import select
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

from ..client import Client, ServerUnavailable, Unauthorized
from .canvas import Trace, plot

UNIT_COLORS: dict[str, list[str]] = {
    "C": ["orange1", "dark_orange", "orange3", "light_salmon1", "indian_red"],
    "W": ["yellow1", "yellow3", "gold1", "khaki1", "wheat1"],
    "V": ["green1", "green3", "spring_green2", "pale_green1", "dark_sea_green"],
    "A": ["cyan1", "cyan3", "turquoise2", "light_cyan1", "dark_turquoise"],
    "%": ["dodger_blue1", "deep_sky_blue2", "steel_blue1", "cornflower_blue", "light_sky_blue1"],
    "B/s": ["medium_purple1", "purple", "violet", "plum1", "orchid"],
    "MHz": ["magenta1", "magenta3", "hot_pink", "pink1", "orchid1"],
    "GT/s": ["medium_orchid", "purple3"], "tok/s": ["spring_green1", "aquamarine1", "sea_green2"],
    "s": ["deep_pink2", "hot_pink2", "pink3"], "B": ["slate_blue1", "medium_purple3"],
    "mJ": ["light_goldenrod1", "yellow4"], "count": ["grey70", "grey54", "grey82", "grey42"],
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


def _secs(s: float) -> str:
    s = int(s)
    for div, suf in ((86400, "d"), (3600, "h"), (60, "m")):
        if s % div == 0:
            return f"{s // div}{suf}"
    return f"{s}s"


def _ago(t: float) -> str:
    s = max(0.0, time.time() - (t or 0))
    return f"{s:.0f}s" if s < 90 else f"{s/60:.0f}m" if s < 5400 else f"{s/3600:.1f}h"


@dataclass
class State:
    client_id: str
    me: dict = field(default_factory=dict)
    catalog: dict = field(default_factory=dict)      # ref -> series dict
    groups: list = field(default_factory=list)        # with .monitor
    monitors: list = field(default_factory=list)
    dbs: list = field(default_factory=list)
    live_db: str | None = None
    profile_name: str | None = None
    profile_version: int | None = None
    config: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    page: str = "graphs"
    message: str = ""
    dirty: bool = False
    sel: int = 0
    data: dict = field(default_factory=dict)
    mon_filter: str | None = None                     # picker in add/group pages


class Monitor:
    def __init__(self, api: Client, console: Console | None = None) -> None:
        self.api = api
        self.console = console or Console()
        self.st = State(client_id="tui-" + hex(int(time.time() * 1000))[-8:])
        self._last_fetch = 0.0
        self._graph_idx = 0
        self._pending_group = None
        self._confirm: dict = {}

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        me = self.api.me()
        self.st.me = me.get("user") or {}
        self.st.build = me.get("build") or {}
        self.st.version = me.get("version", "")
        self.reload_catalog()
        self.st.monitors = self.api.monitors()
        status = self.api.status()
        self.st.status = status
        self.st.live_db = (status.get("manager") or {}).get("live_db")
        self.st.dbs = self.api.databases()
        profs = self.api.profiles()
        if profs:
            self.open_profile(profs[0]["name"])

    def reload_catalog(self) -> None:
        cat = self.api.catalog()
        self.st.catalog = {s["ref"]: s for s in cat.get("series", [])}
        self.st.groups = [g for g in cat.get("groups", []) if g.get("keys")]

    # -- naming --------------------------------------------------------- #
    def mon_info(self, mid: str) -> dict:
        return next((m for m in self.st.monitors if m["monitor"] == mid),
                    {"monitor": mid, "nickname": "", "gpus": [], "state": "?"})

    def mon_label(self, mid: str) -> str:
        return self.mon_info(mid).get("nickname") or mid

    def gpu_label(self, mid: str, gid: str) -> str:
        if gid == "host":
            return "host"
        g = next((x for x in self.mon_info(mid).get("gpus", []) if x["gpu_id"] == gid), None)
        return g["name"] if g else gid

    def ref_label(self, ref: str, with_mon: bool = True) -> str:
        mid, gid, _ = ref.split("/", 2)
        s = self.st.catalog.get(ref, {})
        head = f"{self.mon_label(mid)}/{self.gpu_label(mid, gid)}" if with_mon else self.gpu_label(mid, gid)
        return f"{head} · {s.get('label', ref)}"

    # -- profile -------------------------------------------------------- #
    def open_profile(self, name: str) -> None:
        p = self.api.profile(name)
        if p.get("_status"):
            self.st.message = f"cannot open {name}: {p.get('error')}"
            return
        self.st.profile_name, self.st.profile_version = p["name"], p["version"]
        cfg = p.get("config") or {}
        cfg.setdefault("graphs", [])
        cfg.setdefault("range", {"mode": "last", "seconds": 1800})
        if not cfg.get("sources"):
            cfg["sources"] = [{"dbs": [self.st.live_db] if self.st.live_db else [], "shift": 0, "label": "A"}]
        self.st.config = cfg
        self.st.dirty = False
        self.push_subscription()

    def push_subscription(self) -> None:
        refs = sorted({r for g in self.st.config.get("graphs", []) for r in g.get("series", [])})
        try:
            self.api.subscribe(self.st.client_id, refs)
        except (ServerUnavailable, Unauthorized):
            pass

    def window(self) -> tuple[float, float]:
        r = self.st.config.get("range") or {}
        now = time.time()
        if r.get("mode") == "abs":
            return float(r["t0"]), float(r["t1"])
        return now - float(r.get("seconds", 1800)), now

    def sources(self) -> list[dict]:
        return self.st.config.get("sources") or []

    def fetch(self, width: int) -> None:
        refs = sorted({r for g in self.st.config.get("graphs", []) for r in g.get("series", [])
                       if r in self.st.catalog})
        if not refs:
            self.st.data = {}
            return
        t0, t1 = self.window()
        srcs = [s for s in self.sources() if s.get("dbs")]
        if not srcs:
            return
        res = self.api.query(refs, t0, t1, srcs, max_points=max(120, width * 2))
        if res.get("_status"):
            self.st.message = f"query failed: {res.get('error')}"
            return
        self.st.data = {"t0": t0, "t1": t1, "sources": res["sources"]}

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def render(self, width: int, height: int):
        page = {"graphs": self._page_graphs, "add": self._page_add, "group": self._page_group,
                "profiles": self._page_profiles, "range": self._page_range, "dbs": self._page_dbs,
                "fleet": self._page_fleet, "settings": self._page_settings,
                "help": self._page_help}.get(self.st.page, self._page_graphs)
        return Group(self._header(width), page(width, max(6, height - 4)), self._fkeys(width))

    def _header(self, width: int) -> Text:
        t = Text()
        t.append(" health-monitor ", style="bold white on dark_blue")
        t.append(f"  {self.st.me.get('name', '?')}", style="grey74")
        t.append(f"  profile: {self.st.profile_name or '-'}")
        if self.st.dirty:
            t.append("  [unsaved]", style="yellow")
        mgr = (self.st.status or {}).get("manager")
        if not mgr:
            t.append("  MANAGER UNREACHABLE", style="bold red")
        else:
            st = mgr.get("store", {})
            t.append("  ● recording" if st.get("recording") else f"  RECORDING STOPPED — {st.get('reason')}",
                     style="green" if st.get("recording") else "bold red")
            t.append(f"  {mgr.get('monitors_online', 0)} monitors online", style="grey62")
        r = self.st.config.get("range") or {}
        t.append("  range: fixed" if r.get("mode") == "abs" else f"  last {_secs(r.get('seconds', 1800))}",
                 style="grey62")
        return t

    def _fkeys(self, width: int) -> Text:
        keys = [("F1", "help"), ("F2", "profiles"), ("F3", "add"), ("F4", "group"), ("F5", "range"),
                ("F6", "dbs"), ("F7", "settings"), ("F8", "save"), ("F9", "fleet"), ("F10", "quit")]
        t = Text()
        for k, lbl in keys:
            t.append(f" {k} ", style="bold black on grey70")
            t.append(f"{lbl} ", style="grey74")
        if self.st.message:
            t.append("  " + self.st.message[: max(10, width - 80)], style="yellow")
        return t

    def _traces_for(self, g: dict) -> tuple[list[Trace], list[str]]:
        refs = [r for r in g.get("series", []) if r in self.st.catalog]
        units: list[str] = []
        for r in refs:
            u = self.st.catalog[r]["unit"]
            if u not in units:
                units.append(u)
        units = units[:MAX_AXES]
        multi = len({r.split("/", 1)[0] for r in refs}) > 1
        srcs = self.st.data.get("sources") or []
        traces: list[Trace] = []
        for u in units:
            in_unit = [r for r in refs if self.st.catalog[r]["unit"] == u]
            for i, r in enumerate(in_unit):
                for si, sr in enumerate(srcs):
                    pts = sr.get("data", {}).get(r) or []
                    if not pts and si > 0:
                        continue
                    label = self.ref_label(r, multi)
                    if len(srcs) > 1:
                        label = f"{sr.get('label') or chr(65 + si)}·{label}"
                    traces.append(Trace(label=label, color=color_for(u, i),
                                        points=[(p[0], p[1]) for p in pts], unit=u))
        return traces, units

    def _page_graphs(self, width: int, height: int):
        graphs = self.st.config.get("graphs", [])
        if not graphs:
            return Panel(Text("No graphs in this profile.\nF3 adds series, F4 adds a whole group.",
                              justify="center"), title="graphs", border_style="grey37")
        t0, t1 = self.st.data.get("t0", 0.0), self.st.data.get("t1", 1.0)
        per = max(6, (height - len(graphs)) // max(1, len(graphs)))
        out = []
        for gi, g in enumerate(graphs):
            traces, units = self._traces_for(g)
            rows, lo, hi = plot([t for t in traces if t.points], max(20, width - 12),
                                max(3, per - 3), t0, t1)
            body = Table.grid(padding=0)
            body.add_column(width=10, justify="right")
            body.add_column()
            for ri, line in enumerate(rows):
                axis = fmt(hi, units[0] if units else "") if ri == 0 else \
                       fmt(lo, units[0] if units else "") if ri == len(rows) - 1 else ""
                body.add_row(Text(axis, style="grey50"), Text.from_markup(line))
            legend = Text()
            for tr in traces:
                cur = next((v for _, v in reversed(tr.points) if v is not None), None)
                legend.append("━ ", style=tr.color)
                legend.append(f"{tr.label} ", style="grey74")
                legend.append(f"{fmt(cur, tr.unit)}   ", style="white")
            if not traces:
                legend.append("no data in this window", style="grey50")
            sel = " ◀" if gi == self.st.sel and self.st.page == "graphs" else ""
            axes = ("  axes: " + ", ".join(units)) if len(units) > 1 else ""
            out.append(Panel(Group(body, legend), title=f"[bold]{g.get('title', 'Graph')}[/]{axes}{sel}",
                             border_style="cyan" if sel else "grey37"))
        return Group(*out)

    # -- pickers -------------------------------------------------------- #
    def _mon_ids(self) -> list[str]:
        return sorted({g["monitor"] for g in self.st.groups})

    def _groups_visible(self) -> list[dict]:
        return [g for g in self.st.groups if self.st.mon_filter is None or g["monitor"] == self.st.mon_filter]

    def _mon_bar(self) -> str:
        parts = ["[bold cyan]all[/]" if self.st.mon_filter is None else "all"]
        for m in self._mon_ids():
            lbl = self.mon_label(m)
            parts.append(f"[bold cyan]{lbl}[/]" if self.st.mon_filter == m else lbl)
        return "monitor (m cycles): " + "  ".join(parts)

    def _series_rows(self) -> list[tuple[str, str]]:
        out = []
        cur = self._cur_series()
        for g in self._groups_visible():
            out.append((f"__group__{g['name']}",
                        f"[bold]{self.mon_label(g['monitor'])} · {g['label']}[/]  "
                        f"[grey50]{len(g['keys'])}{' · ' + g['unit'] if g.get('unit') else ' · mixed'}[/]"))
            for r in g["keys"]:
                s = self.st.catalog.get(r)
                if not s:
                    continue
                mark = "✓" if r in cur else " "
                note = f"  [grey42]{s['note']}[/]" if s.get("note") else ""
                out.append((r, f"  [{mark}] {s['label']}{note}"))
        return out

    def _page_add(self, width: int, height: int):
        return self._list_panel(f"ADD to '{self._cur_graph_title()}'  (SPACE toggle, ENTER done, ESC back)  {self._mon_bar()}",
                                self._series_rows(), height)

    def _page_group(self, width: int, height: int):
        if self._pending_group is None:
            rows = [(g["name"], f"{self.mon_label(g['monitor'])} · {g['label']}  [grey50]{len(g['keys'])}"
                                f"{' · ' + g['unit'] if g.get('unit') else ' · mixed'}[/]")
                    for g in self._groups_visible()]
            return self._list_panel(f"ADD GROUP — choose (ENTER open, ESC back)  {self._mon_bar()}", rows, height)
        g, chosen = self._pending_group
        cur = self._cur_series()
        rows = []
        for r in g["keys"]:
            s = self.st.catalog.get(r)
            if not s:
                continue
            already = r in cur
            mark = "-" if already else ("✓" if r in chosen else " ")
            rows.append((r, f"[{mark}] {s['label']}" + ("  [grey42]already on graph[/]" if already else "")))
        return self._list_panel(f"{self.mon_label(g['monitor'])} · {g['label']} — {len(chosen)}/{len(g['keys'])} "
                                "selected  (SPACE unselect, a=all, n=none, ENTER=ADD, ESC=Cancel)", rows, height)

    def _list_panel(self, title: str, rows: list[tuple[str, str]], height: int):
        view = max(4, height - 2)
        if self.st.sel >= len(rows):
            self.st.sel = max(0, len(rows) - 1)
        top = max(0, min(self.st.sel - view // 2, max(0, len(rows) - view)))
        body = Text()
        for i in range(top, min(len(rows), top + view)):
            body.append("▶ " if i == self.st.sel else "  ", style="bold cyan")
            body.append_text(Text.from_markup(rows[i][1]))
            body.append("\n")
        if len(rows) > view:
            body.append(f"  [{self.st.sel + 1}/{len(rows)}]", style="grey50")
        return Panel(body, title=title, border_style="cyan")

    def _page_profiles(self, width: int, height: int):
        profs = self.api.profiles()
        self._profile_rows = profs
        rows = [(p["name"], f"{p['name']}  [grey50]v{p['version']}  "
                            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p['updated_at']))}[/]"
                            + ("  [cyan](open)[/]" if p["name"] == self.st.profile_name else ""))
                for p in profs]
        return self._list_panel("Profiles (yours)  (ENTER open, d delete, ESC back)", rows, height)

    def _page_range(self, width: int, height: int):
        r = self.st.config.get("range") or {}
        rows = [(str(s), f"last {lbl}" + ("   [cyan]◀ current[/]" if r.get("mode") == "last" and r.get("seconds") == s else ""))
                for lbl, s in QUICK_RANGES]
        return self._list_panel("Time range  (ENTER select, ESC back)", rows, height)

    def _page_dbs(self, width: int, height: int):
        src = self.sources()[0] if self.sources() else {"dbs": []}
        rows = []
        for db in self.st.dbs:
            mark = "✓" if db["path"] in src.get("dbs", []) else " "
            span = (f"{time.strftime('%m-%d %H:%M', time.localtime(db['start']))} → "
                    f"{time.strftime('%m-%d %H:%M', time.localtime(db['end']))}" if db.get("start") else "empty")
            rows.append((db["path"], f"[{mark}] {'[green]● live[/]' if db['live'] else '      '} {db['name']:28s} "
                                     f"[grey50]{span}  {db['bytes']/1024**2:7.1f} MiB  {db['series']} series[/]"))
        return self._list_panel("Databases (manager archives)  (SPACE toggle — several = stitched, ESC back)", rows, height)

    def _page_fleet(self, width: int, height: int):
        try:
            self.st.monitors = self.api.monitors()
        except (ServerUnavailable, Unauthorized) as exc:
            return Panel(Text(str(exc)), title="fleet", border_style="red")
        rows = []
        for m in self.st.monitors:
            color = {"online": "green", "stale": "yellow"}.get(m["state"], "red")
            e = m.get("engine") or {}
            ob = m.get("outbox") or {}
            flags = ""
            if ob and not ob.get("recording", True):
                flags += "  [bold red]RECORDING STOPPED[/]"
            if ob.get("rows_dropped"):
                flags += f"  [bold red]DROPPED {ob['rows_dropped']}[/]"
            if abs(m.get("skew", 0)) > 5:
                flags += f"  [yellow]skew {m['skew']:+.1f}s[/]"
            gpus = "  ".join(f"[bold]{g['name']}[/] [grey50]{g['model'].split('[')[0][:22]}"
                             f"{'' if g['state'] == 'active' else ' (' + g['state'] + ')'}[/]" for g in m.get("gpus", []))
            rows.append((m["monitor"],
                         f"[{color}]●[/] [bold]{self.mon_label(m['monitor'])}[/] [grey50]({m['monitor']})[/]  "
                         f"{m['state']}{'' if m['state'] == 'online' else ' · ' + _ago(m.get('last_seen', 0)) + ' ago'}"
                         f"  [grey50]v{m.get('version', '?')}  sweep {e.get('sweep_ms', '?')}ms  "
                         f"{e.get('recorded_series', '?')} rec[/]{flags}\n      {gpus}"))
        title = "Fleet  (ESC back" + (", n nickname, s settings" if self.st.me.get("role") == "admin" else "") + ")"
        return self._list_panel(title, rows, height)

    def _page_settings(self, width: int, height: int):
        if self.st.me.get("role") != "admin":
            return Panel(Text("Engine settings are admin-only."), title="settings", border_style="grey37")
        m = self.mon_info(self._settings_monitor or (self._mon_ids() or [""])[0])
        s = m.get("settings") or {}
        self._settings_cache = (m["monitor"], dict(s))
        rows = [("record_interval", f"Recording interval      {s.get('record_interval', '?')} s   [grey50](+/-)[/]"),
                ("record_exotic", f"Record exotic rails     {'yes' if s.get('record_exotic') else 'no'}   [grey50](SPACE)[/]"),
                ("record_nic_rates", f"Record NIC rates        {'yes' if s.get('record_nic_rates') else 'no'}"),
                ("record_nic_errors", f"Record NIC errors       {'yes' if s.get('record_nic_errors') else 'no'}"),
                ("__note__", "[grey50]m cycles monitor · settings are sent to that monitor and saved there[/]")]
        return self._list_panel(f"Engine settings — {self.mon_label(m['monitor'])}  (ESC back)", rows, height)

    def _page_help(self, width: int, height: int):
        return Panel(Text.from_markup(
            "[bold]health-monitor TUI[/] — a client of the web server\n\n"
            "[bold]graphs[/]   ↑↓ select · n new graph · D delete (twice) · x drop last series · r refresh\n"
            "[bold]pickers[/]  m cycles the monitor filter · SPACE toggles · ENTER confirms\n"
            "[bold]keys[/]     F1 help  F2 profiles  F3 add  F4 group  F5 range  F6 databases\n"
            "         F7 settings (admin)  F8 save  F9 fleet  F10/q quit\n\n"
            f"signed in as {self.st.me.get('name')} ({self.st.me.get('role')})  ·  "
            f"version {getattr(self.st, 'version', '?')}  ·  build {getattr(self.st, 'build', {}).get('hash', '?')}\n"),
            title="help", border_style="grey37")

    # ------------------------------------------------------------------ #
    _settings_monitor: str | None = None

    def _cur_graph(self) -> dict | None:
        gs = self.st.config.get("graphs", [])
        if not gs:
            return None
        idx = self.st.sel if self.st.page == "graphs" else self._graph_idx
        return gs[min(idx, len(gs) - 1)]

    def _cur_series(self) -> list[str]:
        g = self._cur_graph()
        return g.get("series", []) if g else []

    def _cur_graph_title(self) -> str:
        g = self._cur_graph()
        return g.get("title", "?") if g else "-"


# ---------------------------------------------------------------------- #
# input
# ---------------------------------------------------------------------- #
KEYMAP = {"\x1bOP": "F1", "\x1b[11~": "F1", "\x1bOQ": "F2", "\x1b[12~": "F2", "\x1bOR": "F3", "\x1b[13~": "F3",
          "\x1bOS": "F4", "\x1b[14~": "F4", "\x1b[15~": "F5", "\x1b[17~": "F6", "\x1b[18~": "F7",
          "\x1b[19~": "F8", "\x1b[20~": "F9", "\x1b[21~": "F10", "\x1b[A": "UP", "\x1b[B": "DOWN",
          "\x1b[C": "RIGHT", "\x1b[D": "LEFT", "\x1b": "ESC", "\r": "ENTER", "\n": "ENTER", " ": "SPACE"}
PAGE_KEYS = {"F1": "help", "F2": "profiles", "F3": "add", "F4": "group", "F5": "range",
             "F6": "dbs", "F7": "settings", "F9": "fleet"}


class KeyReader:
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.saved = None
        self._buf = ""

    def __enter__(self) -> "KeyReader":
        try:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except termios.error:
            self.saved = None
        return self

    def __exit__(self, *exc) -> None:
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    @staticmethod
    def _match(buf: str) -> bool:
        return any(buf[:n] in KEYMAP for n in range(min(6, len(buf)), 0, -1))

    def poll(self, timeout: float) -> str | None:
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
        if self._buf.startswith("\x1b") and not self._match(self._buf):
            for _ in range(3):
                r, _, _ = select.select([self.fd], [], [], 0.02)
                if not r:
                    break
                try:
                    self._buf += os.read(self.fd, 256).decode("utf-8", "replace")
                except OSError:
                    break
                if self._match(self._buf):
                    break
        for n in range(min(6, len(self._buf)), 0, -1):
            head = self._buf[:n]
            if head in KEYMAP:
                self._buf = self._buf[n:]
                return KEYMAP[head]
        ch, self._buf = self._buf[0], self._buf[1:]
        return KEYMAP.get(ch, ch)


def _rows_for_page(mon: Monitor) -> list:
    p = mon.st.page
    if p == "add":
        return mon._series_rows()
    if p == "group":
        return [(k, k) for k in mon._pending_group[0]["keys"]] if mon._pending_group else \
               [(g["name"], g["label"]) for g in mon._groups_visible()]
    if p == "profiles":
        return [(x["name"], x["name"]) for x in getattr(mon, "_profile_rows", [])]
    if p == "range":
        return [(str(s), l) for l, s in QUICK_RANGES]
    if p == "dbs":
        return [(d["path"], d["name"]) for d in mon.st.dbs]
    if p == "fleet":
        return [(m["monitor"], m["monitor"]) for m in mon.st.monitors]
    if p == "settings":
        return [(k, k) for k in ("record_interval", "record_exotic", "record_nic_rates", "record_nic_errors")]
    return [("", "")] * len(mon.st.config.get("graphs", []))


def _axis_ok(mon: Monitor, refs: list[str]) -> bool:
    units = []
    for r in refs:
        u = mon.st.catalog.get(r, {}).get("unit", "count")
        if u not in units:
            units.append(u)
    if len(units) > MAX_AXES:
        mon.st.message = f"that needs {len(units)} Y axes; the limit is {MAX_AXES} — use another graph"
        return False
    return True


def _cycle_monitor(mon: Monitor) -> None:
    ids = [None] + mon._mon_ids()
    i = ids.index(mon.st.mon_filter) if mon.st.mon_filter in ids else 0
    mon.st.mon_filter = ids[(i + 1) % len(ids)]
    mon.st.sel = 0


def handle_key(mon: Monitor, key: str) -> bool:
    st = mon.st
    rows = _rows_for_page(mon)
    if key == "F10" or (key == "q" and st.page == "graphs"):
        return False
    if key in PAGE_KEYS:
        target = PAGE_KEYS[key]
        if target in ("add", "group") and not st.config.get("graphs"):
            st.message = "add a graph first (n on the graphs page)"
            return True
        if target in ("add", "group"):
            mon._graph_idx = st.sel
            mon._pending_group = None
        st.page, st.sel, st.message = target, 0, ""
        return True
    if key == "F8":
        _save(mon)
        return True
    if key == "ESC":
        if st.page == "group" and mon._pending_group:
            mon._pending_group = None
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
    if key == "m" and st.page in ("add", "group"):
        _cycle_monitor(mon)
        return True

    if st.page == "graphs":
        graphs = st.config.setdefault("graphs", [])
        if key == "n":
            graphs.append({"title": f"Graph {len(graphs) + 1}", "series": [], "update_ms": 1000})
            st.dirty, st.sel = True, len(graphs) - 1
        elif key == "D" and graphs:
            g = graphs[min(st.sel, len(graphs) - 1)]
            if mon._confirm.get("del") == id(g):
                graphs.pop(min(st.sel, len(graphs) - 1))
                mon._confirm.pop("del", None)
                st.dirty, st.sel, st.message = True, max(0, st.sel - 1), "graph deleted"
                mon.push_subscription()
            else:
                mon._confirm["del"] = id(g)
                st.message = f"delete '{g.get('title')}'? D again to confirm (settings only; no data deleted)"
        elif key == "x" and graphs:
            g = graphs[min(st.sel, len(graphs) - 1)]
            if g.get("series"):
                st.message = f"removed {g['series'].pop()}"
                st.dirty = True
                mon.push_subscription()
        elif key == "r":
            mon._last_fetch = 0.0
    elif st.page == "add" and rows:
        r = rows[st.sel][0]
        if key == "SPACE" and not r.startswith("__group__"):
            g = mon._cur_graph()
            if g is not None:
                series = g.setdefault("series", [])
                if r in series:
                    series.remove(r)
                elif _axis_ok(mon, series + [r]):
                    series.append(r)
                st.dirty = True
        elif key == "ENTER":
            st.page, st.sel = "graphs", mon._graph_idx
            mon.push_subscription()
            mon._last_fetch = 0.0
    elif st.page == "group":
        if mon._pending_group is None:
            if key == "ENTER" and rows:
                g = next(x for x in mon._groups_visible() if x["name"] == rows[st.sel][0])
                mon._pending_group = (g, {r for r in g["keys"] if r not in mon._cur_series()})
                st.sel = 0
        else:
            g, chosen = mon._pending_group
            if key == "SPACE" and g["keys"]:
                r = g["keys"][min(st.sel, len(g["keys"]) - 1)]
                if r in chosen:
                    chosen.discard(r)
                elif r not in mon._cur_series():
                    chosen.add(r)
            elif key == "a":
                chosen.clear(); chosen.update(r for r in g["keys"] if r not in mon._cur_series())
            elif key == "n":
                chosen.clear()
            elif key == "ENTER" and chosen:
                target = mon._cur_graph()
                if target is not None:
                    series = target.setdefault("series", [])
                    merged = series + [r for r in g["keys"] if r in chosen and r not in series]
                    if _axis_ok(mon, merged):
                        target["series"] = merged
                        st.dirty, st.message = True, f"added {len(chosen)} from {g['label']}"
                        mon.push_subscription(); mon._last_fetch = 0.0
                        mon._pending_group = None
                        st.page, st.sel = "graphs", mon._graph_idx
    elif st.page == "profiles" and rows:
        name = rows[st.sel][0]
        if key == "ENTER":
            if st.dirty and mon._confirm.get("switch") != name:
                mon._confirm["switch"] = name
                st.message = "unsaved changes — F8 to save, or ENTER again to discard"
                return True
            mon._confirm.pop("switch", None)
            mon.open_profile(name)
            st.page, st.sel, mon._last_fetch = "graphs", 0, 0.0
        elif key == "d":
            if mon._confirm.get("delp") == name:
                mon.api.delete_profile(name)
                mon._confirm.pop("delp", None)
                st.message = f"deleted profile {name}"
                if name == st.profile_name:
                    profs = mon.api.profiles()
                    if profs:
                        mon.open_profile(profs[0]["name"])
            else:
                mon._confirm["delp"] = name
                st.message = f"delete profile '{name}'? d again to confirm"
    elif st.page == "range" and key == "ENTER" and rows:
        st.config["range"] = {"mode": "last", "seconds": int(rows[st.sel][0])}
        st.dirty, st.page, st.sel = True, "graphs", 0
    elif st.page == "dbs" and key == "SPACE" and rows:
        _toggle_db(mon, rows[st.sel][0])
    elif st.page == "fleet" and rows and st.me.get("role") == "admin":
        mid = rows[st.sel][0]
        if key == "n":
            mon._prompt = ("nick", mid)
        elif key == "s":
            mon._settings_monitor = mid
            st.page, st.sel = "settings", 0
    elif st.page == "settings":
        _keys_settings(mon, key, rows)
    return True


def _toggle_db(mon: Monitor, path: str) -> None:
    src = mon.sources()[0]
    dbs = list(src.get("dbs", []))
    if path in dbs:
        dbs.remove(path)
    else:
        dbs.append(path)
        chosen = sorted([d for d in mon.st.dbs if d["path"] in dbs and d.get("start")], key=lambda d: d["start"])
        for i in range(1, len(chosen)):
            if chosen[i]["start"] < chosen[i - 1]["end"]:
                mon.st.message = f"{chosen[i-1]['name']} and {chosen[i]['name']} overlap; stitching would double-plot"
                return
    order = {d["path"]: (d.get("start") or 0) for d in mon.st.dbs}
    src["dbs"] = sorted(dbs, key=lambda p: order.get(p, 0))
    mon.st.dirty, mon._last_fetch = True, 0.0


def _keys_settings(mon: Monitor, key: str, rows) -> None:
    if key == "m":
        ids = mon._mon_ids()
        if ids:
            i = ids.index(mon._settings_monitor) if mon._settings_monitor in ids else -1
            mon._settings_monitor = ids[(i + 1) % len(ids)]
        return
    if not rows or not getattr(mon, "_settings_cache", None):
        return
    mid, cur = mon._settings_cache
    field = rows[min(mon.st.sel, len(rows) - 1)][0]
    changed = False
    if key == "SPACE" and field in ("record_exotic", "record_nic_rates", "record_nic_errors"):
        cur[field] = not cur.get(field); changed = True
    elif key in ("+", "=", "-") and field == "record_interval":
        cur[field] = max(0.5, float(cur.get(field) or 5) + (0.5 if key != "-" else -0.5)); changed = True
    if changed:
        r = mon.api.set_settings(mid, cur)
        mon.st.message = f"settings failed: {r.get('error')}" if r.get("_status") else f"settings sent to {mon.mon_label(mid)}"
        for m in mon.st.monitors:
            if m["monitor"] == mid:
                m["settings"] = r.get("settings", cur)


def _save(mon: Monitor) -> None:
    st = mon.st
    if not st.profile_name:
        st.message = "no profile open"
        return
    r = mon.api.save_profile(st.profile_name, st.config, st.profile_version)
    if r.get("_status") == 409:
        st.message = f"{r.get('name')} was updated elsewhere — F2 to reload, then try again"
        st.profile_version = r.get("current_version")
    elif r.get("_status"):
        st.message = f"save failed: {r.get('error')}"
    else:
        st.profile_version, st.dirty, st.message = r["version"], False, f"saved {r['name']} v{r['version']}"


def run(server: str, token: str | None = None, insecure: bool = False, cafile: str | None = None) -> int:
    console = Console()
    if not token:
        console.print("[bold red]error:[/] an API token is required.\n"
                      "Create one in the web UI (menu → API token) or with  health-monitor users token <name>\n"
                      "then pass --token or set HM_TOKEN.")
        return 2
    api = Client(server, token=token, cafile=cafile, insecure=insecure)
    mon = Monitor(api, console)
    try:
        mon.load()
    except ServerUnavailable as exc:
        console.print(f"[bold red]error:[/] {exc}")
        return 1
    except Unauthorized as exc:
        console.print(f"[bold red]error:[/] {exc} (bad or revoked token)")
        return 3
    last_status = 0.0
    with KeyReader() as keys, Live(console=console, screen=True, auto_refresh=False, redirect_stderr=False) as live:
        while True:
            now = time.time()
            width, height = console.size.width, console.size.height
            interval = min((g.get("update_ms", 1000) / 1000.0 for g in mon.st.config.get("graphs", [])), default=1.0)
            if now - mon._last_fetch >= max(0.25, interval):
                try:
                    mon.fetch(width)
                except (ServerUnavailable, Unauthorized) as exc:
                    mon.st.message = str(exc).splitlines()[0]
                mon._last_fetch = now
            if now - last_status >= 3.0:
                try:
                    mon.st.status = api.status()
                    if mon.st.page != "fleet":
                        mon.st.monitors = api.monitors()
                except (ServerUnavailable, Unauthorized):
                    mon.st.status = {}
                last_status = now
            live.update(mon.render(width, height), refresh=True)
            key = keys.poll(0.2)
            if key and not handle_key(mon, key):
                break
    return 0
