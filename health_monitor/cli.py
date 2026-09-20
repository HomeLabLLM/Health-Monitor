"""health-monitor command line.

    health-monitor monitor            run the GPU monitor on this box
    health-monitor manager            run the manager (mTLS listener)
    health-monitor web                run the web server (HTTPS, users)
    health-monitor tui                the terminal UI (client of the web server)
    health-monitor keys ...           CA, server and client certificates
    health-monitor users ...          web-server user administration
    health-monitor gpus ...           this monitor's GPU identity registry
    health-monitor config ...         show / set config values per role
    health-monitor reset              rotate the manager's samples database
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, config
from .log import setup_logging

app = typer.Typer(name="health-monitor", add_completion=False, no_args_is_help=True,
                  help="Multi-vendor GPU health monitoring: monitor, manager, web, TUI.")
keys_app = typer.Typer(help="Certificate authority and certificates (run on the manager).")
users_app = typer.Typer(help="Web-server user administration.")
gpus_app = typer.Typer(help="GPU identity registry on this monitor.")
config_app = typer.Typer(help="Configuration per role.")
app.add_typer(keys_app, name="keys")
app.add_typer(users_app, name="users")
app.add_typer(gpus_app, name="gpus")
app.add_typer(config_app, name="config")
console = Console()


def _build_info() -> str:
    try:
        from . import _build
        return f"{_build.BUILD_HASH} ({_build.BUILD_DATE})"
    except Exception:                                # noqa: BLE001
        return "unknown build"


@app.callback()
def _root(version: bool = typer.Option(False, "--version", "-V")) -> None:
    if version:
        console.print(f"health-monitor {__version__}  build {_build_info()}")
        raise typer.Exit(0)


# ---------------------------------------------------------------------- #
# roles
# ---------------------------------------------------------------------- #
@app.command()
def monitor(config_path: str = typer.Option(None, "--config", "-c"),
            log_file: str = typer.Option(None, "--log-file"),
            verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Sweep this box's GPUs, spool locally, forward to the manager."""
    setup_logging(verbose, log_file)
    from .monitor.app import main
    main(config_path)


@app.command()
def manager(config_path: str = typer.Option(None, "--config", "-c"),
            log_file: str = typer.Option(None, "--log-file"),
            verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Accept monitors and web servers over mutual TLS; archive samples."""
    setup_logging(verbose, log_file)
    from .manager.app import main
    main(config_path)


@app.command()
def web(config_path: str = typer.Option(None, "--config", "-c"),
        log_file: str = typer.Option(None, "--log-file"),
        verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Serve the web UI over HTTPS; users, profiles, sessions."""
    setup_logging(verbose, log_file)
    from .web.app import main
    main(config_path)


@app.command()
def tui(server: str = typer.Option("https://127.0.0.1:5678", "--server", "-s"),
        token: str = typer.Option(None, "--token", envvar="HM_TOKEN"),
        insecure: bool = typer.Option(False, "--insecure", help="Skip TLS verification.")) -> None:
    """Terminal UI: a client of the web server."""
    from .ui.monitor import run
    raise typer.Exit(run(server, token=token, insecure=insecure))


@app.command()
def reset(config_path: str = typer.Option(None, "--config", "-c")) -> None:
    """Rotate the manager's samples database and start a fresh one."""
    from .manager.app import reset as _reset
    moved = _reset(config_path)
    console.print(f"rotated to [bold]{os.path.basename(moved)}[/]" if moved else "nothing to rotate")


# ---------------------------------------------------------------------- #
# keys
# ---------------------------------------------------------------------- #
@keys_app.command("init-ca")
def keys_init_ca(name: str = typer.Option("health-monitor CA", "--name"),
                 certs: str = typer.Option(None, "--certs"),
                 server_names: list[str] = typer.Option(None, "--server-name",
                     help="Hostnames/IPs clients use to reach this manager; repeatable. "
                          "Defaults to this host's name and FQDN."),
                 force: bool = typer.Option(False, "--force")) -> None:
    """Create the CA and this manager's server certificate."""
    from . import keys
    d = certs or config.certs_dir()
    names = server_names or sorted({socket.gethostname().split(".")[0], socket.getfqdn()})
    keys.init_ca(d, name, force=force)
    keys.new_server(d, names)
    console.print(f"CA created in {d}\nserver certificate for: {', '.join(names)}")


@keys_app.command("new-monitor")
def keys_new_monitor(name: str = typer.Argument(..., help="Monitor id (its hostname)."),
                     certs: str = typer.Option(None, "--certs"),
                     out: str = typer.Option(None, "--out", help="Bundle directory.")) -> None:
    """Issue a client bundle for one monitor and register it."""
    _new_client("monitor", name, certs, out)


@keys_app.command("new-web")
def keys_new_web(name: str = typer.Argument("web"),
                 certs: str = typer.Option(None, "--certs"),
                 out: str = typer.Option(None, "--out")) -> None:
    """Issue a client bundle for a web server and register it."""
    _new_client("web", name, certs, out)


def _new_client(role: str, name: str, certs: str | None, out: str | None) -> None:
    from . import keys
    d = certs or config.certs_dir()
    bundle = keys.new_client(d, role, name, bundle_dir=out or os.path.join(d, "bundles"))
    cfg = config.load("manager")
    key = "monitors" if role == "monitor" else "web_clients"
    if name not in cfg.values[key]:
        cfg.values[key].append(name)
        cfg.save()
    console.print(f"bundle: [bold]{bundle}[/]\nregistered {role} [bold]{name}[/] in {cfg.path}\n"
                  f"install on {name} with:  health-monitor keys install {os.path.basename(bundle)}")


@keys_app.command("new-web-server")
def keys_new_web_server(server_names: list[str] = typer.Argument(...),
                        certs: str = typer.Option(None, "--certs"),
                        out: str = typer.Option(None, "--out")) -> None:
    """Issue a *server* certificate (HTTPS) for a web server's hostnames."""
    from . import keys
    d = certs or config.certs_dir()
    where = keys.new_server(d, list(server_names), out_dir=out or os.path.join(d, "web-server"))
    console.print(f"server.key / server.crt written to {where}; copy them to the web server's certs dir")


@keys_app.command("install")
def keys_install(bundle: str, certs: str = typer.Option(None, "--certs")) -> None:
    """Unpack a client bundle into this box's certs directory."""
    from . import keys
    for p in keys.install_bundle(bundle, certs or config.certs_dir()):
        console.print(f"  {p}")


@keys_app.command("show")
def keys_show(certs: str = typer.Option(None, "--certs")) -> None:
    from . import keys
    d = certs or config.certs_dir()
    for f in ("ca.crt", "server.crt", "client.crt"):
        p = os.path.join(d, f)
        if os.path.exists(p):
            console.print(f"[bold]{f}[/]\n" + "\n".join("  " + l for l in keys.describe(p).splitlines()))


@keys_app.command("export-ca")
def keys_export_ca(certs: str = typer.Option(None, "--certs")) -> None:
    """Print the CA certificate (import it into browsers for HTTPS)."""
    with open(os.path.join(certs or config.certs_dir(), "ca.crt")) as fh:
        sys.stdout.write(fh.read())


# ---------------------------------------------------------------------- #
# users (web server)
# ---------------------------------------------------------------------- #
def _users():
    from .web.users import Users
    cfg = config.load("web")
    return Users(os.path.join(cfg.data_dir, "users.db"))


@users_app.command("list")
def users_list() -> None:
    t = Table("id", "name", "role", "disabled", "created")
    for u in _users().list():
        t.add_row(str(u["id"]), u["name"], u["role"], "yes" if u["disabled"] else "",
                  u["created"][:19])
    console.print(t)


@users_app.command("add")
def users_add(name: str, role: str = typer.Option("user", "--role"),
              password: str = typer.Option(None, "--password", prompt=True, hide_input=True,
                                           confirmation_prompt=True)) -> None:
    _users().add(name, password, role)
    console.print(f"added {role} [bold]{name}[/]")


@users_app.command("passwd")
def users_passwd(name: str, password: str = typer.Option(None, "--password", prompt=True,
                                                          hide_input=True, confirmation_prompt=True)) -> None:
    """Set a password (also how the admin password is reset)."""
    _users().set_password(name, password)
    console.print(f"password set for [bold]{name}[/]; existing sessions revoked")


@users_app.command("del")
def users_del(name: str, yes: bool = typer.Option(False, "--yes")) -> None:
    if not yes and not typer.confirm(f"delete user {name} and all their profiles?"):
        raise typer.Exit(1)
    _users().delete(name)
    console.print(f"deleted [bold]{name}[/]")


@users_app.command("disable")
def users_disable(name: str, enable: bool = typer.Option(False, "--enable")) -> None:
    _users().set_disabled(name, not enable)
    console.print(f"{'enabled' if enable else 'disabled'} [bold]{name}[/]")


@users_app.command("token")
def users_token(name: str, label: str = typer.Option("cli", "--label")) -> None:
    """Issue an API token for the TUI (shown once)."""
    tok = _users().new_token(name, label)
    console.print(f"token for {name} ({label}):\n\n  [bold]{tok}[/]\n\n"
                  f"use:  health-monitor tui --token {tok}   or  HM_TOKEN=...")


# ---------------------------------------------------------------------- #
# gpus (monitor)
# ---------------------------------------------------------------------- #
def _registry():
    from .identity import Registry
    cfg = config.load("monitor")
    return Registry(os.path.join(config.config_dir(), "gpus.json"), cfg.get("gpu_names") or {}), cfg


@gpus_app.command("list")
def gpus_list() -> None:
    reg, _ = _registry()
    t = Table("gpu_id", "name", "state", "vendor", "model", "serial", "pci", "note")
    for c in reg.listing():
        t.add_row(c["gpu_id"], c["name"], c["state"], c["vendor"], c["model"][:32],
                  c["serial"] or "-", c["pci_addr"], c["note"])
    console.print(t)


@gpus_app.command("rename")
def gpus_rename(gpu_id: str, name: str) -> None:
    reg, cfg = _registry()
    got = reg.rename(gpu_id, name)
    names = dict(cfg.values.get("gpu_names") or {})
    names[gpu_id] = name
    cfg.values["gpu_names"] = names
    cfg.save()
    console.print(f"{gpu_id} is now [bold]{got}[/] (restart the monitor to apply)")


@gpus_app.command("map")
def gpus_map(gpu_id: str, pci_addr: str) -> None:
    """Declare that the card at PCI_ADDR is GPU_ID (resolves a pending card)."""
    reg, _ = _registry()
    reg.remap(gpu_id, pci_addr)
    console.print(f"{gpu_id} mapped to {pci_addr} (restart the monitor to apply)")


@gpus_app.command("forget")
def gpus_forget(gpu_id: str) -> None:
    reg, _ = _registry()
    reg.forget(gpu_id)
    console.print(f"forgot {gpu_id}; it will be minted afresh if seen again")


# ---------------------------------------------------------------------- #
# config
# ---------------------------------------------------------------------- #
@config_app.command("show")
def config_show(role: str) -> None:
    cfg = config.load(role, create=False)
    console.print(f"[bold]{cfg.path}[/]")
    console.print_json(json.dumps(cfg.values))


@config_app.command("set")
def config_set(role: str, key: str, value: str) -> None:
    """Set one value; JSON is accepted (lists, numbers, booleans)."""
    cfg = config.load(role)
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    cfg.values[key] = parsed
    cfg.save()
    console.print(f"{role}.{key} = {parsed!r}  ({cfg.path})")


@config_app.command("path")
def config_path(role: str = typer.Argument("monitor")) -> None:
    cfg = config.load(role)
    console.print(f"config: {cfg.path}\ndata:   {cfg.data_dir}\ncerts:  {cfg.certs}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
