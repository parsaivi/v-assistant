"""pki — the credential that never travels.

The phone surface has one requirement the other four don't: *only Parsa's phone
may reach it*, and "reach" has to mean the TCP handshake, not a password check
somewhere behind it. Telegram solved the same problem with a claimed owner id,
which is the right answer when the transport belongs to someone else and every
stranger can already open a chat. Here the transport is ours, so the check can
happen a layer lower and be absolute.

So: a private CA lives in `state/pki/`, it signs exactly one server certificate
and one certificate per enrolled device, and the server refuses the TLS
handshake for anything it didn't sign. There is no password to guess, no token
to leak from a log, and no third party holding a copy — an unenrolled client
does not get far enough to send a request, let alone a wrong one.

**The device key is generated here and compiled into the APK.** It is never
sent over any network, not even once during pairing, which is the usual hole in
this kind of scheme: a pairing flow needs a channel to pair over, and that
channel is either unauthenticated (the hole) or already-authenticated (in which
case why pair). Building the credential into the binary sidesteps it — the APK
*is* the credential, `adb install` is the enrolment, and rotating a key is a
rebuild. That trade is only affordable because there is exactly one user and
one phone, which is the same assumption the rest of this package already makes.

`openssl(1)` rather than the `cryptography` package, for the reason `gcal.py`
has no `google-api-python-client` and `telegram.py` has no `python-telegram-bot`:
the binary is already on the box, this is a hundred lines against it, and the
premise of this package is that it runs with nothing installed.

Elliptic curve (P-256) rather than RSA — smaller, faster on a phone, and
supported by every Android version that can run the app. Certificates are
issued for 825 days, which is the longest a modern client will accept for a
server certificate.
"""

from __future__ import annotations

import datetime as dt
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, settings as default_settings

CA_DAYS = 3650          # the CA outlives every leaf it signs
LEAF_DAYS = 825         # the ceiling clients will accept for serverAuth
CURVE = "prime256v1"    # NIST P-256


class PkiError(RuntimeError):
    """openssl said no, or a file that should exist doesn't."""


# ---- where it lives -------------------------------------------------------
def pki_dir(s: Settings | None = None) -> Path:
    """`state/pki/`. Under `state/` because it is machinery, not content — the
    same boundary `assistant.db` and the OAuth tokens sit on. It must never be
    in the vault: the vault is a git repo that gets pushed."""
    s = s or default_settings
    d = s.state_dir / "pki"
    d.mkdir(parents=True, exist_ok=True)
    # 0700 rather than the default, for the same reason `quera_cookie.json` is
    # 0600 — this directory holds the key that is the whole access control.
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


@dataclass
class Device:
    """One enrolled device, and the files an app build needs from it."""

    name: str
    cert: Path
    key: Path          # PKCS#8, unencrypted — see `assets()` below
    ca: Path

    def assets(self) -> dict[str, Path]:
        """What gets copied into the Android project's `assets/`.

        The key is unencrypted PKCS#8 rather than a password-protected PKCS#12.
        A PKCS#12's password would have to ship inside the same APK to be
        usable, so it protects nothing, and OpenSSL 3 and Android's BouncyCastle
        disagree often enough about which PBE algorithms are acceptable that
        the encrypted form is a portability problem bought with no security.
        The real protection is that the APK is only ever installed on one
        phone, and Android keeps an app's assets out of reach of other apps.
        """
        return {"device.crt": self.cert, "device.pk8": self.key, "ca.crt": self.ca}


# ---- openssl --------------------------------------------------------------
def _openssl(*args: str, stdin: str | None = None) -> str:
    try:
        p = subprocess.run(("openssl",) + args, capture_output=True, text=True,
                           input=stdin, timeout=60)
    except FileNotFoundError:
        raise PkiError("openssl is not installed — apt install openssl") from None
    except subprocess.TimeoutExpired:
        raise PkiError("openssl timed out") from None
    if p.returncode != 0:
        raise PkiError(f"openssl {args[0]} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def _genkey(path: Path) -> None:
    _openssl("ecparam", "-name", CURVE, "-genkey", "-noout", "-out", str(path))
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---- the CA ---------------------------------------------------------------
def ensure_ca(s: Settings | None = None) -> tuple[Path, Path]:
    """(cert, key). Created once; every later call is a no-op.

    Idempotent on purpose: `cli phone enrol` is the kind of command someone
    runs twice, and regenerating the CA the second time would silently
    invalidate the phone already carrying a certificate from the first — with
    the failure showing up as a handshake error days later rather than as
    anything the second run printed.
    """
    d = pki_dir(s)
    crt, key = d / "ca.crt", d / "ca.key"
    if crt.exists() and key.exists():
        return crt, key
    _genkey(key)
    _openssl(
        "req", "-x509", "-new", "-key", str(key), "-sha256",
        "-days", str(CA_DAYS), "-out", str(crt),
        "-subj", "/CN=V Home CA/O=Brain",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )
    return crt, key


def _sign(name: str, subject: str, ext: str, s: Settings | None = None
          ) -> tuple[Path, Path]:
    """Generate a key, sign a leaf certificate for it, return (cert, key)."""
    d = pki_dir(s)
    ca_crt, ca_key = ensure_ca(s)
    key, crt = d / f"{name}.key", d / f"{name}.crt"
    _genkey(key)
    with tempfile.TemporaryDirectory() as td:
        csr = Path(td) / "req.csr"
        cnf = Path(td) / "ext.cnf"
        cnf.write_text(ext, encoding="utf-8")
        _openssl("req", "-new", "-key", str(key), "-out", str(csr),
                 "-subj", subject)
        _openssl("x509", "-req", "-in", str(csr),
                 "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                 "-out", str(crt), "-days", str(LEAF_DAYS), "-sha256",
                 "-extfile", str(cnf))
    return crt, key


# ---- the server's own certificate ----------------------------------------
def local_hosts() -> list[str]:
    """Every address this machine can plausibly be reached on, for the SANs.

    A certificate is only valid for the names in it, and the phone will reach
    this box by a different address at home (the LAN lease) than away (the
    tailnet), so both have to be in the certificate issued *now* — discovering
    the second one later means reissuing and rebuilding the app. Tailscale's
    address is picked out of the interface list rather than by shelling out to
    `tailscale ip`, so this works before the CLI is installed and keeps working
    if it is removed.
    """
    out: list[str] = ["127.0.0.1"]
    try:
        p = subprocess.run(("ip", "-4", "-o", "addr"), capture_output=True,
                           text=True, timeout=5)
        for line in p.stdout.splitlines():
            m = re.match(r"\s*\d+:\s*(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
            if not m:
                continue
            iface, ip = m.group(1), m.group(2)
            if ip.startswith("127.") or ip in out or _virtual(iface):
                continue
            out.append(ip)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


# Interfaces the phone can never route to. They were being put in the server
# certificate's SANs, which is only noise — but `resolve_bind()` falls back to
# the first non-loopback address, and `ip addr` orders by interface index, so
# on a boot where docker0 came up before the wifi V would bind to a bridge no
# phone can reach and report itself as listening.
_VIRTUAL = ("docker", "br-", "veth", "virbr", "vboxnet", "lxc", "podman",
            "cni", "flannel", "kube")


def _virtual(iface: str) -> bool:
    name = iface.split("@")[0]
    return any(name.startswith(p) for p in _VIRTUAL)


def tailnet_ip() -> str | None:
    """The 100.64.0.0/10 address, if this box is on a tailnet.

    Worth naming separately from `local_hosts()` because it is the one address
    that does not change when Parsa leaves the house, which makes it the right
    default for the app to try first.
    """
    for ip in local_hosts():
        a, b = (int(x) for x in ip.split(".")[:2])
        if a == 100 and 64 <= b <= 127:
            return ip
    return None


def ensure_server(s: Settings | None = None, hosts: list[str] | None = None,
                  force: bool = False) -> tuple[Path, Path]:
    """(cert, key) for the listener. Reissued when the address set changes.

    The SAN list is stored beside the certificate and compared on every call,
    because the failure it prevents is invisible otherwise: move to a new
    network, get a new lease, and the certificate silently stops matching the
    address the phone dials — which surfaces as a TLS error on the phone and
    nothing at all in the server log.
    """
    d = pki_dir(s)
    crt, key, sans = d / "server.crt", d / "server.key", d / "server.sans"
    hosts = hosts or local_hosts()
    names = ["localhost", "v.local", socket.gethostname()]
    want = "\n".join(sorted(set(names)) + sorted(set(hosts)))
    if not force and crt.exists() and key.exists() and sans.exists():
        if sans.read_text(encoding="utf-8") == want:
            return crt, key
    alt = ",".join([f"DNS:{n}" for n in sorted(set(names))]
                   + [f"IP:{h}" for h in sorted(set(hosts))])
    ext = ("basicConstraints=CA:FALSE\n"
           "keyUsage=critical,digitalSignature,keyEncipherment\n"
           "extendedKeyUsage=serverAuth\n"
           f"subjectAltName={alt}\n")
    out = _sign("server", "/CN=v.local/O=Brain", ext, s)
    sans.write_text(want, encoding="utf-8")
    return out


# ---- devices --------------------------------------------------------------
def _safe(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-")
    return n[:40] or "device"


def enrol(name: str, s: Settings | None = None) -> Device:
    """Issue a client certificate for one device. Re-running replaces it.

    Replacing rather than refusing is deliberate: the only way to use this
    command twice for the same name is to be reissuing after losing the phone
    or rebuilding the app, and in both cases the old key should stop working.
    """
    name = _safe(name)
    ext = ("basicConstraints=CA:FALSE\n"
           "keyUsage=critical,digitalSignature\n"
           "extendedKeyUsage=clientAuth\n")
    crt, key = _sign(f"device-{name}", f"/CN={name}/O=Brain Devices", ext, s)
    # PKCS#8 is what Android's `KeyFactory` reads. OpenSSL's `ecparam` writes
    # SEC1 ("BEGIN EC PRIVATE KEY"), which it does not, and the resulting
    # `InvalidKeySpecException` on the phone says nothing about why.
    pk8 = key.with_suffix(".pk8")
    _openssl("pkcs8", "-topk8", "-nocrypt", "-in", str(key), "-out", str(pk8))
    try:
        pk8.chmod(0o600)
    except OSError:
        pass
    return Device(name=name, cert=crt, key=pk8, ca=pki_dir(s) / "ca.crt")


def sign_public_key(name: str, public_key_pem: str, s: Settings | None = None
                    ) -> Path:
    """Issue a client certificate for a public key we were handed, and only that.

    This is the half of enrolment that makes the private key unstealable. The
    phone generates its keypair inside the Android Keystore, where the private
    half is held by the TEE and has no API that returns it — not to the app
    that made it, not to root, not to someone holding the unlocked handset. All
    that ever leaves is the public key, which is public.

    `enrol()` above is the older shape: it generates both halves here and
    compiles the private one into the APK, which is fast to set up and has a
    real cost — an APK is world-readable on Android, so any installed app can
    read the key straight out of it. That trade is written up in
    `19 - Assistant/Threat Model.md`. This function is what replaces it.

    No PKCS#10. A CSR exists to prove possession of the private key, and on
    Android there is no bundled builder for one, so producing it would mean
    hand-rolling DER inside the app. `openssl x509 -force_pubkey` signs the
    bare public key instead. Skipping the proof-of-possession costs nothing
    here: the certificate is useless without the private key regardless, and
    the key was handed over on a USB cable by the person who owns both ends.
    """
    name = _safe(name)
    d = pki_dir(s)
    ca_crt, ca_key = ensure_ca(s)
    crt = d / f"device-{name}.crt"

    with tempfile.TemporaryDirectory() as td:
        pub = Path(td) / "device.pub"
        pub.write_text(public_key_pem.strip() + "\n", encoding="utf-8")
        if "BEGIN PUBLIC KEY" not in public_key_pem:
            raise PkiError("that is not a PEM public key — expected a "
                           "'BEGIN PUBLIC KEY' block")
        cnf = Path(td) / "ext.cnf"
        cnf.write_text("basicConstraints=CA:FALSE\n"
                       "keyUsage=critical,digitalSignature\n"
                       "extendedKeyUsage=clientAuth\n", encoding="utf-8")
        _openssl("x509", "-new", "-force_pubkey", str(pub),
                 "-subj", f"/CN={name}/O=Brain Devices",
                 "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                 "-days", str(LEAF_DAYS), "-sha256",
                 "-extfile", str(cnf), "-out", str(crt))

    # Any private key left over from an earlier `enrol()` of the same name is
    # deleted rather than left lying about. It is no longer the credential and
    # a spare copy of a retired key is only ever a liability.
    for stale in (d / f"device-{name}.key", d / f"device-{name}.pk8"):
        if stale.exists():
            stale.unlink()
    return crt


def enrolled(s: Settings | None = None) -> list[str]:
    """The CNs the server will accept, read off the issued certificates.

    Read from the files rather than kept in a config list, so there is one
    source of truth: a certificate that exists is enrolled, and deleting it is
    how you revoke. A list that could disagree with the filesystem is a list
    that eventually does.
    """
    out = []
    for crt in sorted(pki_dir(s).glob("device-*.crt")):
        cn = subject_cn(crt)
        if cn:
            out.append(cn)
    return out


def revoke(name: str, s: Settings | None = None) -> bool:
    """Delete a device's certificate and key. It stops handshaking immediately.

    No CRL and no OCSP: with one CA, one server and a handful of devices, the
    revocation list *is* the directory listing, and a CRL would be a second
    place for the same fact to be wrong.
    """
    d = pki_dir(s)
    hit = False
    for p in (d / f"device-{_safe(name)}.crt", d / f"device-{_safe(name)}.key",
              d / f"device-{_safe(name)}.pk8"):
        if p.exists():
            p.unlink()
            hit = True
    return hit


# ---- reading certificates -------------------------------------------------
def subject_cn(cert: Path) -> str:
    try:
        out = _openssl("x509", "-in", str(cert), "-noout", "-subject")
    except PkiError:
        return ""
    m = re.search(r"CN\s*=\s*([^,/\n]+)", out)
    return m.group(1).strip() if m else ""


def fingerprint(cert: Path) -> str:
    """SHA-256, colon-separated — what you compare by eye against the phone."""
    out = _openssl("x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256")
    return out.split("=", 1)[-1].strip()


def expires(cert: Path) -> dt.datetime | None:
    try:
        out = _openssl("x509", "-in", str(cert), "-noout", "-enddate")
    except PkiError:
        return None
    raw = out.split("=", 1)[-1].strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def status(s: Settings | None = None) -> dict:
    """What `cli phone status` prints: is there a CA, a server cert, devices."""
    d = pki_dir(s)
    ca = d / "ca.crt"
    server = d / "server.crt"
    return {
        "dir": str(d),
        "ca": bool(ca.exists()),
        "ca_fingerprint": fingerprint(ca) if ca.exists() else "",
        "server": bool(server.exists()),
        "server_expires": expires(server).isoformat() if server.exists() else "",
        "sans": (d / "server.sans").read_text(encoding="utf-8").split("\n")
                if (d / "server.sans").exists() else [],
        "devices": enrolled(s),
        "tailnet": tailnet_ip() or "",
        "hosts": local_hosts(),
    }
