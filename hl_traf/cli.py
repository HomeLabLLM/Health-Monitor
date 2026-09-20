"""typer CLI for hl-traf."""

from __future__ import annotations

import asyncio
import os
import logging

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .hlml import Hlml, HlmlError
from .log import setup_logging
from .topology import TOPOLOGY_FILE, Wiring, discover, wiring_from_qual
from .ui.live import TuiApp

app = typer.Typer(
    name="hl-traf",
    help="Live NIC fabric traffic monitor for Habana Gaudi systems.",
    add_completion=False,
    no_args_is_help=False,
)
console = Console()


def _open_hlml() -> Hlml:
    try:
        return Hlml()
    except HlmlError as exc:
        console.print(f"[bold red]error:[/] {exc}")
        raise typer.Exit(1)


def _load_wiring(hlml: Hlml) -> Wiring | None:
    """Topology cache first, then the qual static map as fallback."""
    log = logging.getLogger("hl-traf")
    wiring = Wiring.load()
    if wiring:
        log.info("topology: %d links from %s", len(wiring.links), TOPOLOGY_FILE)
        return wiring
    try:
        wiring = wiring_from_qual(hlml)
        log.info(
            "topology: static qual map (%d direct links, %d routed) — "
            "run 'hl-traf discover' to verify with traffic",
            len(wiring.links), len(wiring.routed_links),
        )
        return wiring
    except (OSError, ValueError) as exc:
        log.info("topology: no cache and no qual map (%s) — run 'hl-traf discover'", exc)
        return None


# ---------------------------------------------------------------------- #
# watch (default)
# ---------------------------------------------------------------------- #
def _watch(
    view: str,
    interval: float,
    ports: str,
    line_rate: float,
    history: int,
    log_file: str | None,
    verbose: bool,
    once: bool,
) -> None:
    panel = setup_logging(verbose, log_file)
    hlml = _open_hlml()
    try:
        wiring = _load_wiring(hlml)
        tui = TuiApp(
            hlml,
            wiring,
            panel,
            view=view,
            interval=interval,
            history=history,
            ports=ports,
            line_rate_gbps=line_rate,
            once=once,
            width=200 if once else None,
        )
        asyncio.run(tui.run())
    finally:
        hlml.shutdown()


@app.command()
def watch(
    view: str = typer.Option("matrix", "--view", "-w", help="matrix | table"),
    interval: float = typer.Option(0.3, "--interval", "-i", help="Min seconds between sweeps (fast path ~0.2-0.5s)"),
    ports: str = typer.Option("all", "--ports", help="all | internal | external"),
    line_rate: float = typer.Option(100.0, "--line-rate", help="NIC line rate in Gb/s for utilization"),
    history: int = typer.Option(60, "--history", help="Sparkline history length (sweeps)"),
    log_file: str | None = typer.Option(None, "--log-file", help="Also write logs to this file"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    once: bool = typer.Option(False, "--once", help="Render one snapshot after priming, then exit"),
) -> None:
    """Live monitor (default command)."""
    if view not in ("matrix", "table"):
        console.print("[red]--view must be matrix or table[/]")
        raise typer.Exit(2)
    if ports not in ("all", "internal", "external"):
        console.print("[red]--ports must be all, internal or external[/]")
        raise typer.Exit(2)
    _watch(view, interval, ports, line_rate, history, log_file, verbose, once)


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Print version and exit"),
    server: str = typer.Option("http://127.0.0.1:5678", "--server", "-s",
                               help="Monitoring server to connect to."),
) -> None:
    if version:
        console.print(f"hl-traf {__version__}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        # The graph TUI is a client of the server, so the hardware is read
        # once however many people are watching.  There is deliberately no
        # direct-device fallback: a second reader slows the server's sweeps
        # for everyone (236 ms becomes 517 ms with two readers).
        from .ui.monitor import run
        raise typer.Exit(run(server))


# ---------------------------------------------------------------------- #
# discover
# ---------------------------------------------------------------------- #
@app.command(name="discover")
def discover_cmd(
    windows: int = typer.Option(60, "--windows", "-n", help="Number of delta windows to sample"),
    interval: float = typer.Option(5.0, "--interval", "-i", help="Window length in seconds (>= ~3s sweep time)"),
    tol: float = typer.Option(0.10, "--tol", help="Relative tx/rx matching tolerance"),
    floor: float = typer.Option(4096.0, "--floor", help="Min octets/window for a port to count as active"),
    links_per_pair: int = typer.Option(3, "--links-per-pair", help="Max links per GPU pair (0 = unlimited)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without confirmation"),
    output: str = typer.Option(TOPOLOGY_FILE, "--output", "-o", help="Topology cache path"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Discover internal NIC wiring via traffic correlation.

    Needs fabric traffic flowing (e.g. an HCCL collective) while sampling.
    Bursts can be sparse: default is 60 windows x 5s = 5 minutes.
    """
    setup_logging(verbose)
    hlml = _open_hlml()
    try:
        from rich.progress import Progress

        with Progress(console=console, transient=True) as prog:
            task = prog.add_task("sampling fabric traffic", total=windows)

            def progress(done: int, total: int, active: int) -> None:
                prog.update(task, completed=done, description=f"window {done}/{total} ({active} active ports)")

            report, meta = discover(
                hlml,
                windows=windows,
                interval=interval,
                tol=tol,
                floor_bytes=floor,
                links_per_pair=links_per_pair or None,
                progress=progress,
            )

        tab = Table(title="discovered links", header_style="bold cyan")
        for col in ("GPU A", "Port A", "GPU B", "Port B", "score"):
            tab.add_column(col, justify="right" if col != "score" else "left")
        for lk in sorted(report.wiring.links, key=lambda l: (l.gpu_a, l.port_a)):
            tab.add_row(str(lk.gpu_a), str(lk.port_a), str(lk.gpu_b), str(lk.port_b), f"{lk.score:.2f}")
        console.print(tab)

        if meta["total_tx_bytes"] <= 0:
            console.print(
                "[bold yellow]warning:[/] no internal NIC traffic observed — "
                "run a collective (HCCL) workload during discovery."
            )
        if report.idle_ports:
            console.print(f"[yellow]{len(report.idle_ports)} ports idle[/] (no measurable tx): "
                          + ", ".join(f"G{g}:p{p}" for g, p in report.idle_ports))
        if report.unmatched_ports:
            console.print(f"[yellow]{len(report.unmatched_ports)} active ports unmatched[/]: "
                          + ", ".join(f"G{g}:p{p}" for g, p in report.unmatched_ports))

        if not report.wiring.links:
            console.print("[red]nothing discovered; not writing cache[/]")
            raise typer.Exit(1)

        if yes or typer.confirm(f"Save {len(report.wiring.links)} links to {output}?"):
            report.wiring.save(output)
            console.print(f"[green]saved[/] {output}")
    finally:
        hlml.shutdown()




# ---------------------------------------------------------------------- #
# ports
# ---------------------------------------------------------------------- #
@app.command()
def ports(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """One-shot map of ports, link state and netdevs."""
    setup_logging(verbose)
    hlml = _open_hlml()
    wiring = _load_wiring(hlml)
    try:
        tab = Table(title="NIC ports", header_style="bold cyan")
        for col, justify in (
            ("GPU", "right"), ("port", "right"), ("type", "left"),
            ("link", "center"), ("peer", "left"), ("netdev", "left"),
        ):
            tab.add_column(col, justify=justify)
        for dev in hlml.devices:
            for p in dev.ports:
                try:
                    up = dev.link_up(p.port)
                    link_txt = Text("UP", style="green") if up else Text("DOWN", style="grey50")
                except HlmlError:
                    link_txt = Text("?", style="red")
                peer = wiring.peer(dev.index, p.port) if wiring else None
                if peer:
                    peer_txt = f"G{peer[0]}:p{peer[1]}"
                elif wiring and (rp := wiring.routed_peer(dev.index, p.port)):
                    peer_txt = f"G{rp[0]}:p{rp[1]} (sw r{rp[2]})"
                else:
                    peer_txt = "—"
                tab.add_row(
                    str(dev.index),
                    str(p.port),
                    Text("ext", style="magenta3") if p.external else Text("int", style="grey62"),
                    link_txt,
                    peer_txt,
                    dev.netdev(p.port) or "",
                )
        console.print(tab)
        if wiring:
            console.print(f"[grey62]topology cache: {TOPOLOGY_FILE} "
                          f"({len(wiring.links)} links, {wiring.discovered_at})[/]")
    finally:
        hlml.shutdown()


# ---------------------------------------------------------------------- #
# qual-map
# ---------------------------------------------------------------------- #
@app.command(name="qual-map")
def qual_map(
    save: bool = typer.Option(False, "--save", "-y", help=f"Write to {TOPOLOGY_FILE}"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the static NIC wiring from the qual stack (libNICTests.so).

    Decodes g_card_location_N_mapping tables and maps card locations to
    this system's GPUs via hlml module IDs. No traffic required.
    """
    setup_logging(verbose)
    hlml = _open_hlml()
    try:
        mods = {dev.index: dev.module_id() for dev in hlml.devices}
        wiring = wiring_from_qual(hlml)

        tab = Table(title="direct internal links (from qual static map)", header_style="bold cyan")
        for col, j in (("GPU A", "right"), ("Port", "right"), ("GPU B", "right"), ("Port", "right")):
            tab.add_column(col, justify=j)
        for lk in sorted(wiring.links, key=lambda l: (l.gpu_a, l.port_a)):
            tab.add_row(str(lk.gpu_a), str(lk.port_a), str(lk.gpu_b), str(lk.port_b))
        console.print(tab)

        rtab = Table(title="external ports (routed via switch)", header_style="bold cyan")
        for col, j in (("GPU A", "right"), ("Port", "right"), ("GPU B", "right"), ("Port", "right"), ("route", "right")):
            rtab.add_column(col, justify=j)
        for lk in sorted(wiring.routed_links, key=lambda l: (l.gpu_a, l.port_a)):
            rtab.add_row(str(lk.gpu_a), str(lk.port_a), str(lk.gpu_b), str(lk.port_b), str(lk.route))
        console.print(rtab)

        console.print(
            f"[grey62]module ids: "
            + ", ".join(f"gpu{i}=m{m}" for i, m in sorted(mods.items()))
            + f"  |  {len(wiring.links)} direct links, {len(wiring.routed_links)} routed[/]"
        )
        if save:
            wiring.save()
            console.print(f"[green]saved[/] {TOPOLOGY_FILE}")
        else:
            console.print("[grey62]dry run — pass --save to write the topology cache[/]")
    finally:
        hlml.shutdown()


# ---------------------------------------------------------------------- #
# selftest (synthetic matcher validation)
# ---------------------------------------------------------------------- #
@app.command()
def selftest(
    seed: int = typer.Option(1, "--seed"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Validate the correlation matcher against synthetic counter traces."""
    import random

    from .topology import LinkKey, match_correlation

    setup_logging(verbose)
    rng = random.Random(seed)
    n_gpu, n_ports, per_pair = 8, 24, 3
    internal = [p for p in range(n_ports) if p not in (8, 22, 23)]
    assert len(internal) == 21

    # Ground truth wiring: 3 links between every GPU pair.
    truth: dict[LinkKey, LinkKey] = {}
    pools = {g: list(internal) for g in range(n_gpu)}
    for a in range(n_gpu):
        for b in range(a + 1, n_gpu):
            for _ in range(per_pair):
                pa = rng.choice(pools[a])
                pools[a].remove(pa)
                pb = rng.choice(pools[b])
                pools[b].remove(pb)
                truth[(a, pa)] = (b, pb)
                truth[(b, pb)] = (a, pa)

    windows = 10
    keys = sorted(truth)
    tx = {k: [0.0] * windows for k in keys}
    rx = {k: [0.0] * windows for k in keys}
    for w in range(windows):
        for a_k, b_k in truth.items():
            if a_k >= b_k:
                continue  # handle each physical link once
            if rng.random() < 0.25:
                continue  # quiet window for this link
            base = rng.uniform(1e6, 5e9)
            tx[a_k][w] = base
            tx[b_k][w] = base * rng.uniform(0.2, 1.0)  # asymmetric duplex
    for w in range(windows):
        for k in keys:
            if tx[k][w] > 0:
                rx[truth[k]][w] += tx[k][w] * rng.uniform(1.010, 1.020)  # wire overhead

    report = match_correlation(tx, rx, tol=0.05, floor_bytes=4096.0, links_per_pair=per_pair)
    got = {lk.normalized() for lk in report.wiring.links}
    want = set()
    for a_k, b_k in truth.items():
        if a_k < b_k:
            want.add((a_k, b_k))

    missing = want - got
    extra = got - want
    ok = not missing and not extra and report.matched == len(want)
    console.print(f"links truth={len(want)} matched={report.matched} "
                  f"missing={len(missing)} extra={len(extra)} idle={len(report.idle_ports)}")
    if ok:
        console.print("[bold green]SELFTEST PASS[/] — matcher recovered full synthetic wiring")
    else:
        for m in sorted(missing)[:10]:
            console.print(f"  missing: G{m[0][0]}:p{m[0][1]} <-> G{m[1][0]}:p{m[1][1]}")
        for e in sorted(extra)[:10]:
            console.print(f"  extra:   G{e[0][0]}:p{e[0][1]} <-> G{e[1][0]}:p{e[1][1]}")
        console.print("[bold red]SELFTEST FAIL[/]")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------- #
# serve -- the monitoring server
# ---------------------------------------------------------------------- #
DEFAULT_DATA_DIR = os.path.expanduser("~/.hl-traf")


@app.command(name="serve")
def serve_cmd(
    port: int = typer.Option(5678, "--port", "-p", help="TCP port to listen on."),
    host: str = typer.Option("0.0.0.0", "--bind", help="Address to bind."),
    data_dir: str = typer.Option(DEFAULT_DATA_DIR, "--data-dir",
                                 help="Where the sample and profile databases live."),
    vllm: list[str] = typer.Option([], "--vllm",
                                   help="vLLM base URL to scrape; repeatable."),
    replay: str = typer.Option(None, "--replay", metavar="PATH",
                               help="Serve an archive read-only; no hardware is "
                                    "touched and nothing is recorded."),
    nic: bool = typer.Option(False, "--nic",
                             help="Also poll the NIC fabric so the matrix "
                                  "and table views work over JSON."),
    nic_interval: float = typer.Option(0.5, "--nic-interval",
                                       help="Seconds between fabric sweeps."),
    log_file: str = typer.Option(None, "--log-file"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run the HTTP server: JSON API for the TUI plus the web UI."""
    from .serve import serve
    setup_logging(verbose, log_file)
    serve(host, port, data_dir, list(vllm), replay, nic, nic_interval)


@app.command(name="serve-reset")
def serve_reset_cmd(
    data_dir: str = typer.Option(DEFAULT_DATA_DIR, "--data-dir"),
) -> None:
    """Rotate the samples database and begin a fresh recording.

    Nothing is deleted -- the rotated file stays readable and appears in
    the UI's database switcher.
    """
    from .serve import reset
    moved = reset(data_dir)
    if moved:
        console.print(f"rotated to [bold]{os.path.basename(moved)}[/]")
        console.print("a fresh samples.db starts on the next 'serve'")
    else:
        console.print("nothing to rotate")


@app.command(name="serve-list")
def serve_list_cmd(
    data_dir: str = typer.Option(DEFAULT_DATA_DIR, "--data-dir"),
) -> None:
    """List the sample databases with their time extents."""
    from .serve import describe
    rows = describe(data_dir)
    if not rows:
        console.print(f"no databases in {data_dir}")
        return
    for line in rows:
        console.print(line)
