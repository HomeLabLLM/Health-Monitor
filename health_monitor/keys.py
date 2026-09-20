"""Certificate authority and client bundles, via the openssl CLI.

The manager is the CA.  ``init_ca`` creates it plus the manager's own
server certificate; ``new_client`` issues a bundle (key, cert, CA) for
one monitor or web server, which you copy to that box and unpack into
its certs directory.  The web server additionally needs a *server*
certificate for HTTPS, which ``new_server`` issues.

Only the openssl binary is required -- no Python crypto dependency.
Private keys are written 0600.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass

CA_DAYS = 3650
CERT_DAYS = 1095
ROLES = ("monitor", "web")


class KeyError_(RuntimeError):
    pass


def _openssl(*args: str, cwd: str | None = None) -> str:
    if shutil.which("openssl") is None:
        raise KeyError_("openssl not found on PATH")
    r = subprocess.run(["openssl", *args], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise KeyError_(f"openssl {args[0]} failed:\n{r.stderr.strip()}")
    return r.stdout


def _write_private(path: str, text: str = "") -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _san_ext(names: list[str]) -> str:
    entries = []
    for n in names:
        try:
            socket.inet_aton(n)
            entries.append(f"IP:{n}")
        except OSError:
            entries.append(f"DNS:{n}")
    return "subjectAltName=" + ",".join(entries)


@dataclass
class CAInfo:
    dir: str
    subject: str


# ---------------------------------------------------------------------- #
def init_ca(certs: str, name: str = "health-monitor CA", *, force: bool = False) -> CAInfo:
    os.makedirs(certs, exist_ok=True)
    ca_key, ca_crt = os.path.join(certs, "ca.key"), os.path.join(certs, "ca.crt")
    if os.path.exists(ca_key) and not force:
        raise KeyError_(f"CA already exists at {ca_key} (use --force to replace it, "
                        f"which invalidates every issued certificate)")
    _write_private(ca_key)
    _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-out", ca_key)
    _openssl("req", "-x509", "-new", "-key", ca_key, "-sha256", "-days", str(CA_DAYS),
             "-subj", f"/O=health-monitor/OU=ca/CN={name}", "-out", ca_crt,
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    return CAInfo(dir=certs, subject=name)


def _issue(certs: str, key_out: str, crt_out: str, subject: str, ext: str) -> None:
    ca_key, ca_crt = os.path.join(certs, "ca.key"), os.path.join(certs, "ca.crt")
    if not os.path.exists(ca_key):
        raise KeyError_(f"no CA at {ca_key}; run 'health-monitor keys init-ca' first")
    with tempfile.TemporaryDirectory() as tmp:
        csr = os.path.join(tmp, "req.csr")
        extf = os.path.join(tmp, "ext.cnf")
        with open(extf, "w") as fh:
            fh.write(ext + "\n")
        _write_private(key_out)
        _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                 "-out", key_out)
        _openssl("req", "-new", "-key", key_out, "-subj", subject, "-out", csr)
        _openssl("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
                 "-CAcreateserial", "-days", str(CERT_DAYS), "-sha256",
                 "-extfile", extf, "-out", crt_out)


def new_server(certs: str, names: list[str], *, out_dir: str | None = None) -> str:
    """Server certificate (manager listener or web HTTPS) for these
    hostnames/IPs.  Returns the directory holding server.key/server.crt."""
    out_dir = out_dir or certs
    os.makedirs(out_dir, exist_ok=True)
    ext = "\n".join([
        "basicConstraints=CA:FALSE",
        "keyUsage=digitalSignature,keyEncipherment",
        "extendedKeyUsage=serverAuth",
        _san_ext(names),
    ])
    _issue(certs, os.path.join(out_dir, "server.key"), os.path.join(out_dir, "server.crt"),
           f"/O=health-monitor/OU=server/CN={names[0]}", ext)
    return out_dir


def new_client(certs: str, role: str, name: str, *, bundle_dir: str) -> str:
    """Client certificate for one monitor or web server, packed as a
    tar.gz containing ca.crt, client.key, client.crt.  Returns its path."""
    if role not in ROLES:
        raise KeyError_(f"role must be one of {ROLES}")
    os.makedirs(bundle_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        key, crt = os.path.join(tmp, "client.key"), os.path.join(tmp, "client.crt")
        ext = "\n".join(["basicConstraints=CA:FALSE",
                         "keyUsage=digitalSignature",
                         "extendedKeyUsage=clientAuth"])
        _issue(certs, key, crt, f"/O=health-monitor/OU={role}/CN={name}", ext)
        shutil.copy(os.path.join(certs, "ca.crt"), os.path.join(tmp, "ca.crt"))
        bundle = os.path.join(bundle_dir, f"{role}-{name}.tar.gz")
        with tarfile.open(bundle, "w:gz") as tf:
            for f in ("ca.crt", "client.key", "client.crt"):
                tf.add(os.path.join(tmp, f), arcname=f)
        os.chmod(bundle, 0o600)
    return bundle


def install_bundle(bundle: str, certs: str) -> list[str]:
    """Unpack a bundle into this box's certs directory."""
    os.makedirs(certs, exist_ok=True)
    names = []
    with tarfile.open(bundle, "r:gz") as tf:
        for m in tf.getmembers():
            if m.name not in ("ca.crt", "client.key", "client.crt"):
                raise KeyError_(f"unexpected member {m.name!r} in bundle")
            data = tf.extractfile(m).read().decode()
            dest = os.path.join(certs, m.name)
            _write_private(dest, data) if m.name.endswith(".key") else open(dest, "w").write(data)
            names.append(dest)
    return names


def describe(path: str) -> str:
    return _openssl("x509", "-in", path, "-noout", "-subject", "-issuer", "-enddate",
                    "-ext", "subjectAltName").strip()
