"""TLS contexts and peer identity.

Mutual TLS throughout: the manager is the CA, every monitor and web
server holds a client certificate it signed, and the manager only
accepts clients whose CN it has registered.  Role comes from the
certificate's OU (``monitor`` / ``web``) so one listener serves both.

Files under the certs directory (see keys.py for how they get there):

    ca.crt                     everyone
    server.key / server.crt    manager listener; web listener (HTTPS)
    client.key / client.crt    monitor or web -> manager
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass


@dataclass(frozen=True)
class Peer:
    cn: str
    role: str


def _need(path: str, what: str) -> str:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{what} missing: {path}\nRun 'health-monitor keys' on the manager to "
            f"create the CA, then install the bundle it produces on this box.")
    return path


def server_context(certs: str, *, require_client: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(_need(os.path.join(certs, "server.crt"), "server certificate"),
                        _need(os.path.join(certs, "server.key"), "server key"))
    if require_client:
        ctx.load_verify_locations(_need(os.path.join(certs, "ca.crt"), "CA certificate"))
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def client_context(certs: str, *, check_hostname: bool = True) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                     cafile=_need(os.path.join(certs, "ca.crt"), "CA certificate"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(_need(os.path.join(certs, "client.crt"), "client certificate"),
                        _need(os.path.join(certs, "client.key"), "client key"))
    ctx.check_hostname = check_hostname
    return ctx


def peer_from_cert(cert: dict | None) -> Peer | None:
    """Parse ssl's peercert dict into (CN, OU)."""
    if not cert:
        return None
    cn = ou = ""
    for rdn in cert.get("subject", ()):
        for k, v in rdn:
            if k == "commonName":
                cn = v
            elif k == "organizationalUnitName":
                ou = v
    if not cn:
        return None
    return Peer(cn=cn, role=ou or "unknown")


def peer_of(request) -> Peer | None:
    """aiohttp request -> Peer (None when no client certificate)."""
    transport = request.transport
    if transport is None:
        return None
    return peer_from_cert(transport.get_extra_info("peercert"))
