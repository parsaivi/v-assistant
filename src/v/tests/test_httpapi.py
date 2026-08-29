"""Tests for the phone transport (`core/pki.py`, `core/httpapi.py`).

Run:  python3 -m v.tests.test_httpapi

Same posture as `test_telegram.py`: no pytest, a throwaway vault per test, no
network and no model. The end-to-end tests stand a real listener on loopback
with a real certificate chain and talk to it with Python's own `ssl` — because
the one thing worth proving here cannot be proved with a mock. **The claim is
that an unenrolled client cannot get a request through**, and a fake socket
that returns whatever the test asked for proves nothing about that at all.

The engine is never constructed: every test below either exercises a pure
function or replaces `VServer.bridge` with a stub, so the suite stays free and
stays fast.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import ssl
import sys
import threading
import time
import traceback
from urllib.parse import quote
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import httpapi, pki
from v.tests.test_core import make_vault

PASS, FAIL = [], []


def test(fn):
    def run():
        try:
            fn()
            PASS.append(fn.__name__)
            print(f"  \x1b[32m✓\x1b[0m {fn.__name__}")
        except Exception as exc:
            FAIL.append((fn.__name__, exc, traceback.format_exc()))
            print(f"  \x1b[31m✗\x1b[0m {fn.__name__}: {exc}")
    run.fn = fn
    return run


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg} expected {b!r}, got {a!r}")


def ok(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


# ---- stand-ins ------------------------------------------------------------
@dataclass
class FakeResponse:
    """Shaped like `engine.Response`, which is all `payload()` reads."""

    text: str = "Noted, sir."
    intent: str = "capture"
    citations: list = field(default_factory=list)
    buttons: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    cost: float = 0.0
    ms: float = 12.0
    route: str = "capture.idea"
    decision: str = "capture (91%)"


class StubConn:
    """Just enough sqlite3 for `h_hello`'s note count."""

    def execute(self, *a, **kw):
        return self

    def fetchone(self):
        return (412,)


class StubBridge:
    """`VServer.bridge` without an Engine behind it.

    `call` runs the closure against this object, so anything a handler reaches
    for through the engine it is handed has to exist here too — which is the
    point: the stub is the contract the handlers depend on, written down.
    """

    class _Persona:
        def __init__(self, key="v"):
            self.key = key

    class _Outbox:
        """The two outbox methods `files.take_queued_files` uses.

        Empty by default, because the common turn has no file waiting — and a
        stub that always had one would hide the case that matters, which is
        that a plain chat must not pay for this.
        """

        def __init__(self, rows=None):
            self.rows = list(rows or [])
            self.claimed = []

        def unsent(self):
            return list(self.rows)

        def claim(self, oid):
            if oid in self.claimed:
                return False        # the interlock, so a test can prove it
            self.claimed.append(oid)
            return True

    class _Vault:
        def __init__(self, present=True):
            self.present = present

        def exists(self, rel):
            return self.present

        def path(self, rel):
            from pathlib import Path as _P
            return _P("/nowhere") / rel

    def __init__(self, response=None, outbox=None, settings=None):
        self.response = response or FakeResponse()
        self.calls = []
        self.conn = StubConn()
        # `h_chat` hands over any file V queued since the last turn, so every
        # handler that returns a turn now touches these two.
        self.reminders = StubBridge._Outbox(outbox)
        self.vault = StubBridge._Vault()
        self.s = settings or __import__("v.core.config", fromlist=["settings"]).settings
        # `h_wake` reports who answered, so the stub needs an agent to be. A
        # handler that reached for `engine.persona` and found nothing here
        # would raise inside the closure and fail every wake at runtime while
        # every test still passed.
        self.persona = StubBridge._Persona()
        self.switched = []

    def handle(self, text, **kw):
        self.handled = getattr(self, "handled", [])
        self.handled.append((text, kw))
        return self.response

    def end_session(self, reason="quit"):
        pass

    def start_session(self):
        pass

    def switch_agent(self, key):
        self.switched.append(key)
        if key not in ("v", "friday", "edith"):
            return False, f"I don't know an agent called {key!r}."
        self.persona = StubBridge._Persona(key)
        return True, ""

    def call(self, fn, timeout=0):
        self.calls.append(fn)
        return fn(self)

    def transcribe(self, data, suffix=".ogg"):
        return "the transcript"

    def speak(self, text):
        # One frame per sentence, and the text in it, so a streaming test can
        # prove *which* sentences arrived and in what order rather than only
        # that some bytes did.
        return b"OggS:" + text.encode("utf-8")

    # ---- the streaming halves -----------------------------------------
    def push_chunk(self, utt, pcm, *, final):
        self.chunks = getattr(self, "chunks", [])
        self.chunks.append((utt, len(pcm), final))
        # Each chunk contributes one word, so the transcript grows visibly.
        heard = getattr(self, "heard", [])
        if pcm:
            heard.append(f"chunk{len(heard) + 1}")
        self.heard = heard
        return " ".join(heard)

    def synth(self):
        """What `SpeechSpool` asks how many sentences it may run at once."""
        return self

    #: Serial by default, like Piper. Overridden by the concurrency test.
    concurrency = 1

    def spool(self, turn, text):
        # The real `SpeechSpool`, not a fake: the thing worth testing is that
        # it splits, synthesizes in order and terminates, and a stub of it
        # would test nothing but the stub.
        self.spools = getattr(self, "spools", {})
        self.spools[turn] = httpapi.SpeechSpool(self, text)
        return self.spools[turn]

    def spooled(self, turn):
        return getattr(self, "spools", {}).get(turn)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stand_up(s, bridge=None) -> tuple[httpapi.VServer, threading.Thread, int]:
    """A real TLS listener on loopback, with a stub brain."""
    port = free_port()
    server = httpapi.VServer(s, bind="127.0.0.1", port=port, scheduler=False)
    server.bridge = bridge or StubBridge()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):           # wait for the socket, not a fixed sleep
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, thread, port


def client_context(s, *, with_cert: bool) -> ssl.SSLContext:
    d = pki.pki_dir(s)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(d / "ca.crt"))
    ctx.check_hostname = False     # the cert is issued for IPs, not this name
    if with_cert:
        ctx.load_cert_chain(certfile=str(d / "device-pixel.crt"),
                            keyfile=str(d / "device-pixel.key"))
    return ctx


# ---- pki ------------------------------------------------------------------
@test
def enrolling_twice_keeps_the_ca_and_replaces_only_the_device():
    """The CA must be stable across runs. Regenerating it would invalidate a
    phone already carrying a certificate, and the failure would show up days
    later as a handshake error rather than as anything the second run said."""
    root, s = make_vault()
    try:
        ca_first = pki.ensure_ca(s)[0].read_bytes()
        first = pki.enrol("pixel", s).cert.read_bytes()
        ca_second = pki.ensure_ca(s)[0].read_bytes()
        second = pki.enrol("pixel", s).cert.read_bytes()
        eq(ca_first, ca_second, "the CA must not be regenerated")
        ok(first != second, "re-enrolling must issue a fresh device certificate")
    finally:
        shutil.rmtree(root)


@test
def the_device_key_is_pkcs8_because_android_cannot_read_sec1():
    """OpenSSL's `ecparam` writes SEC1; `KeyFactory` on the phone reads PKCS#8
    and fails with an `InvalidKeySpecException` that names neither."""
    root, s = make_vault()
    try:
        device = pki.enrol("pixel", s)
        head = device.key.read_text(encoding="utf-8").splitlines()[0]
        eq(head, "-----BEGIN PRIVATE KEY-----", "assets must carry PKCS#8")
    finally:
        shutil.rmtree(root)


@test
def a_public_key_can_be_signed_without_ever_seeing_the_private_half():
    """The fix for the design's worst weakness. The phone generates its keypair
    in the Android Keystore, where the private half has no API that returns it,
    and sends only the public key. If this needed the private key the whole
    scheme would collapse back to shipping one inside a world-readable APK."""
    import subprocess
    import tempfile as tf

    root, s = make_vault()
    try:
        with tf.TemporaryDirectory() as td:
            # Stand in for the phone. Its private key never reaches `pki`.
            key = Path(td) / "phone.key"
            pub = Path(td) / "phone.pub"
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1",
                            "-genkey", "-noout", "-out", str(key)], check=True,
                           capture_output=True)
            subprocess.run(["openssl", "ec", "-in", str(key), "-pubout",
                            "-out", str(pub)], check=True, capture_output=True)

            crt = pki.sign_public_key("pixel", pub.read_text(), s)
            eq(pki.subject_cn(crt), "pixel")
            eq(pki.enrolled(s), ["pixel"], "and it counts as enrolled")

            # The certificate must carry the phone's key, not one of ours.
            embedded = subprocess.run(
                ["openssl", "x509", "-in", str(crt), "-noout", "-pubkey"],
                capture_output=True, text=True, check=True).stdout
            eq(embedded.strip(), pub.read_text().strip(),
               "the certificate must be bound to the key the phone holds")

            # And no private key may exist on this side for that device.
            d = pki.pki_dir(s)
            for leftover in ("device-pixel.key", "device-pixel.pk8"):
                ok(not (d / leftover).exists(),
                   f"{leftover} must not exist — the point is that we never "
                   f"have it")
    finally:
        shutil.rmtree(root)


@test
def signing_a_public_key_clears_a_private_key_left_by_the_old_scheme():
    """Migrating from the bundled-key scheme has to remove the old private key,
    not leave a retired copy of a credential lying in `state/pki/`."""
    root, s = make_vault()
    try:
        device = pki.enrol("pixel", s)          # the old way: key generated here
        ok(device.key.exists(), "the old scheme writes a private key")

        import subprocess
        import tempfile as tf
        with tf.TemporaryDirectory() as td:
            key, pub = Path(td) / "k", Path(td) / "p"
            subprocess.run(["openssl", "ecparam", "-name", "prime256v1",
                            "-genkey", "-noout", "-out", str(key)], check=True,
                           capture_output=True)
            subprocess.run(["openssl", "ec", "-in", str(key), "-pubout",
                            "-out", str(pub)], check=True, capture_output=True)
            pki.sign_public_key("pixel", pub.read_text(), s)

        d = pki.pki_dir(s)
        ok(not (d / "device-pixel.key").exists(), "the SEC1 key must be gone")
        ok(not (d / "device-pixel.pk8").exists(), "and so must the PKCS#8 copy")
    finally:
        shutil.rmtree(root)


@test
def a_public_key_that_is_not_one_is_refused():
    """A private key pasted in by mistake must not be signed and quietly
    treated as a public one."""
    root, s = make_vault()
    try:
        try:
            pki.sign_public_key("pixel", "-----BEGIN PRIVATE KEY-----\nx\n", s)
            raise AssertionError("that should not have been accepted")
        except pki.PkiError as exc:
            ok("public key" in str(exc))
    finally:
        shutil.rmtree(root)


@test
def revoking_removes_it_from_the_enrolled_list():
    """`enrolled()` is read off the filesystem rather than a config list, so
    deleting the certificate *is* the revocation — there is no second place
    for the same fact to disagree."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        pki.enrol("spare", s)
        eq(sorted(pki.enrolled(s)), ["pixel", "spare"])
        ok(pki.revoke("pixel", s))
        eq(pki.enrolled(s), ["spare"])
        eq(pki.revoke("pixel", s), False, "revoking twice is not an error")
    finally:
        shutil.rmtree(root)


@test
def the_server_certificate_is_reissued_when_the_addresses_change():
    """A new network lease silently stops matching the address the phone
    dials, and it surfaces as a TLS error on the phone with nothing in the
    server log. The SAN set is compared on every call so it cannot."""
    root, s = make_vault()
    try:
        first = pki.ensure_server(s, hosts=["10.0.0.5"])[0].read_bytes()
        same = pki.ensure_server(s, hosts=["10.0.0.5"])[0].read_bytes()
        eq(first, same, "an unchanged address set must not reissue")
        moved = pki.ensure_server(s, hosts=["10.0.0.6"])[0].read_bytes()
        ok(first != moved, "a changed address set must reissue")
    finally:
        shutil.rmtree(root)


# ---- the access control ---------------------------------------------------
@test
def a_client_with_no_certificate_cannot_get_a_request_through():
    """The claim the whole surface rests on. Not "is rejected with a 403" —
    *cannot complete the handshake*, so no request handler ever runs."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=False),
                timeout=10)
            try:
                conn.request("GET", "/v1/hello")
                conn.getresponse()
                raise AssertionError("an unenrolled client got a response")
            except (ssl.SSLError, OSError, http.client.HTTPException):
                pass               # exactly right: rejected, and told nothing
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_client_signed_by_another_ca_is_refused():
    """A certificate is not a password: one issued by anybody else must fail
    even though it is a perfectly valid certificate."""
    root, s = make_vault()
    other_root, other = make_vault()
    try:
        pki.enrol("pixel", s)
        pki.enrol("pixel", other)          # a different CA entirely
        server, thread, port = stand_up(s)
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=str(pki.pki_dir(s) / "ca.crt"))
            ctx.check_hostname = False
            d = pki.pki_dir(other)
            ctx.load_cert_chain(certfile=str(d / "device-pixel.crt"),
                                keyfile=str(d / "device-pixel.key"))
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=10)
            try:
                conn.request("GET", "/v1/hello")
                conn.getresponse()
                raise AssertionError("a foreign certificate was accepted")
            except (ssl.SSLError, OSError, http.client.HTTPException):
                pass
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)
        shutil.rmtree(other_root)


@test
def the_enrolled_device_gets_through_and_is_named_back():
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("GET", "/v1/hello")
            res = conn.getresponse()
            eq(res.status, 200)
            body = json.loads(res.read())
            eq(body["name"], "V")
            eq(body["device"], "pixel", "the response should name the caller")
            eq(body["notes"], 412)
            eq(res.getheader("X-V-Device"), "pixel")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def revoking_takes_effect_without_a_restart():
    """There is no CRL, so a revoked certificate still chains to the CA and
    still completes the handshake — the 403 in `_dispatch` *is* the revocation
    mechanism, and it is only as good as how fresh the enrolled set is.

    Checked against a real `pki.revoke()` rather than a faked-out attribute,
    because the bug this guards against was precisely that the set was read
    once at start-up: `cli phone revoke pixel` reported success, deleted the
    certificate, and the lost phone went on being served until someone
    restarted the service. The one moment revocation has to work is the moment
    you are least likely to be sitting at the machine.

    It also has to stay *told*, not dropped. Unlike a stranger on Telegram this
    is a device Parsa owns, and "no longer enrolled" must not look identical to
    the laptop being asleep.
    """
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            ctx = client_context(s, with_cert=True)

            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("GET", "/v1/hello")
            eq(conn.getresponse().status, 200, "enrolled, so it should be served")

            ok(pki.revoke("pixel", s), "the certificate should have been there")

            # Same certificate, same CA, still a valid handshake — and refused.
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("GET", "/v1/hello")
            res = conn.getresponse()
            eq(res.status, 403, "revocation must not need a restart")
            ok("enrolled" in json.loads(res.read())["error"])
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_device_enrolled_while_the_server_runs_is_accepted():
    """The other direction, and the reason the enrolled set is re-read rather
    than merely forgotten on revoke: adding a second phone must not need a
    restart either."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            pki.enrol("spare", s)          # enrolled after the listener came up
            d = pki.pki_dir(s)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_verify_locations(cafile=str(d / "ca.crt"))
            ctx.check_hostname = False
            ctx.load_cert_chain(certfile=str(d / "device-spare.crt"),
                                keyfile=str(d / "device-spare.key"))
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("GET", "/v1/hello")
            res = conn.getresponse()
            eq(res.status, 200)
            eq(json.loads(res.read())["device"], "spare")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_revoked_device_loses_the_event_stream_it_is_already_holding():
    """Every other endpoint is checked on the way in and answers in seconds.
    `/v1/events` is held open for hours by design, so a check only at the door
    would let a revoked phone go on receiving reminders down a socket it opened
    while it was still trusted — the same failure one layer along."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        ctx = client_context(s, with_cert=True)
        try:
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("GET", "/v1/events")
            res = conn.getresponse()
            eq(res.status, 200, "the stream opens while the device is enrolled")

            ok(pki.revoke("pixel", s))
            # Wakes the handler out of its keepalive wait. It must re-check and
            # drop the connection rather than write this down the wire.
            server.bus.publish("reminder", "dentist at 4")
            body = res.read().decode("utf-8", "replace")
            ok("dentist" not in body,
               f"a revoked device was sent an event: {body!r}")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def serving_with_nobody_enrolled_refuses_to_start():
    """Standing up a listener nothing can connect to is not a working server,
    it is a port. Better to say so than to look healthy."""
    root, s = make_vault()
    try:
        server = httpapi.VServer(s, bind="127.0.0.1", port=free_port(),
                                 scheduler=False)
        server.bridge = StubBridge()
        try:
            server.serve_forever()
            raise AssertionError("it started with no devices enrolled")
        except httpapi.ApiError as exc:
            ok("enrol" in exc.message)
    finally:
        shutil.rmtree(root)


# ---- the wire shape -------------------------------------------------------
@test
def a_local_answer_reports_an_exact_zero_and_a_model_run_reports_allowance():
    """The cost line is the product feature, and the distinction is the point:
    `cost` is what was billed, `allowance` is what a subscription run consumed.
    Calling the second one spend would be a lie in the one place the design
    says not to tell one."""
    local = httpapi.payload(FakeResponse())
    eq(local["cost"], 0.0)
    eq(local["allowance"], 0.0)
    eq(local["local"], True)

    escalated = httpapi.payload(FakeResponse(
        route="agent:claude-code",
        data={"list_cost_usd": 0.0507, "model": "claude-sonnet-5"}))
    eq(escalated["cost"], 0.0, "a subscription run is not billed per call")
    eq(escalated["allowance"], 0.0507)
    eq(escalated["local"], False)
    eq(escalated["model"], "claude-sonnet-5")


@test
def the_voice_state_is_decided_here_not_on_the_phone():
    """One implementation of the rule. A refusal is refused whichever path
    produced it, and a fallback looks degraded without the client guessing."""
    eq(httpapi.voice_state(FakeResponse(data={"refused": True})), "refused")
    eq(httpapi.voice_state(FakeResponse(route="rag:tfidf:fallback")), "degraded")
    eq(httpapi.voice_state(FakeResponse(data={"degraded": True})), "degraded")
    eq(httpapi.voice_state(FakeResponse(data={"confidence": 0.41})), "unsure")
    eq(httpapi.voice_state(FakeResponse(data={"confidence": 0.93})), "speaking")
    eq(httpapi.voice_state(FakeResponse()), "speaking")


@test
def the_spoken_form_drops_the_citation_line():
    """A list of vault links read aloud is noise, not information."""
    resp = FakeResponse(text="Two places qualify.\n\nSources: a.md b.md")
    out = httpapi.payload(resp)
    ok("Sources:" in out["text"], "the bubble keeps them")
    ok("Sources:" not in out["spoken"], "the voice does not")


@test
def the_backlog_replays_only_what_a_client_missed():
    """A phone is asleep for most of the day. A reminder delivered into a
    socket nobody was holding is a reminder that silently never fired — the
    exact failure the scheduler's catch-up exists to prevent."""
    bus = httpapi.Broadcast(keep=10)
    bus.publish("reminders", "one")
    bus.publish("reminders", "two")
    q = bus.subscribe(since=1)
    eq(q.qsize(), 1, "only the event after the cursor")
    eq(q.get_nowait()["text"], "two")

    bus.publish("nudges", "three")
    eq(q.get_nowait()["text"], "three", "and live events after that")


@test
def a_stalled_listener_cannot_stall_the_others():
    """One phone with a wedged socket must not hold up a second one, so a full
    queue drops rather than blocks."""
    bus = httpapi.Broadcast()
    slow = bus.subscribe()
    fast = bus.subscribe()
    for i in range(300):                   # past the 256 the queue holds
        bus.publish("nudges", f"n{i}")
    ok(slow.qsize() <= 256)
    ok(fast.qsize() <= 256)
    eq(bus.listeners, 2)
    bus.unsubscribe(slow)
    eq(bus.listeners, 1)


@test
def the_scheduler_sink_publishes_whatever_a_job_hands_it():
    """The same contract `telegram.build_sink` implements against the same
    jobs: something with a `.render()`, or a plain string, and no job has to
    know which surface is listening."""
    bus = httpapi.Broadcast()
    sink = httpapi.build_sink(bus)
    sink("reminders", FakeResponse(text="Dentist at four, sir."))
    sink("nudges", "You have not trained in eleven days.")
    sink("nudges", None)                   # jobs return these; must be ignored
    q = bus.subscribe()
    eq(q.qsize(), 2)
    ok("Dentist" in q.get_nowait()["text"])
    ok("trained" in q.get_nowait()["text"])


@test
def the_bind_address_is_never_a_wildcard():
    """mTLS stops a hostile client getting a request through, but it can still
    reach the port, and a port that answers is a port worth attacking."""
    root, s = make_vault()
    try:
        s.phone_bind = ""
        ok(httpapi.resolve_bind(s) != "0.0.0.0", "never bind everywhere")
        ok("0.0.0.0" not in httpapi.resolve_binds(s), "not in the list either")
        s.phone_bind = "127.0.0.1"
        eq(httpapi.resolve_bind(s), "127.0.0.1", "an explicit bind wins")
    finally:
        shutil.rmtree(root)


@test
def every_address_gets_its_own_socket():
    """Android permits one VPN at a time, so the phone arrives over the tailnet
    sometimes and over the LAN the rest of the time. A server that picked one
    address at start-up is unreachable for half of that — which is a failure
    the user experiences as "V is down" while V is running fine."""
    root, s = make_vault()
    try:
        s.phone_bind = "127.0.0.1, 127.0.0.2"
        eq(httpapi.resolve_binds(s), ["127.0.0.1", "127.0.0.2"])

        pki.enrol("pixel", s)
        port = free_port()
        server = httpapi.VServer(s, port=port, scheduler=False)
        server.bridge = StubBridge()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for address in ("127.0.0.1", "127.0.0.2"):
                for _ in range(100):
                    try:
                        with socket.create_connection((address, port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.05)
                conn = http.client.HTTPSConnection(
                    address, port, context=client_context(s, with_cert=True),
                    timeout=20)
                conn.request("GET", "/v1/hello")
                eq(conn.getresponse().status, 200, f"{address} must answer")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_wav_upload_does_not_hand_ffmpeg_its_own_input():
    """The phone records raw PCM and posts WAV, so the file arriving here is
    already `.wav` — and `convert_to_wav` derived its destination by swapping
    the suffix, which produced the source path. ffmpeg refuses ("Output ...
    same as Input #0 - exiting"), and every voice message from the phone
    failed while Telegram's OGG kept working perfectly."""
    import subprocess
    import tempfile as tf

    from v.core import stt

    with tf.TemporaryDirectory() as td:
        src = Path(td) / "voice.wav"
        made = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=1", "-ar", "44100", "-ac", "2",
             str(src)], capture_output=True)
        if made.returncode != 0:
            return                      # no ffmpeg — skip, same as test_tts
        out = stt.convert_to_wav(src)
        ok(out != src, "the destination must dodge the source")
        ok(out.exists() and out.stat().st_size > 0, "and must actually be written")


@test
def an_audio_content_type_picks_the_extension_ffmpeg_needs():
    eq(httpapi._suffix("audio/wav"), ".wav")
    eq(httpapi._suffix("audio/mp4"), ".m4a")
    eq(httpapi._suffix("audio/ogg; codecs=opus"), ".ogg")
    eq(httpapi._suffix(""), ".ogg", "an unknown type falls back, never crashes")


# ---- keep-alive -----------------------------------------------------------
@test
def a_body_no_handler_reads_does_not_corrupt_the_next_request():
    """`/v1/wake` takes its arguments in the query string and never reads a
    body, but the phone posts `{}` because OkHttp requires one on POST. Left
    unread, those two bytes sit in the socket and are parsed as the front of
    the *following* request line — the phone gets `Unsupported method
    ('{}POST')` for a request that was perfectly well formed, and only when
    connections happen to be reused, which is why it survived until the gate
    started calling wake often."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            ctx = client_context(s, with_cert=True)
            # One connection, reused — that is the whole point.
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("POST", "/v1/wake?source=gate", body=b"{}")
            first = conn.getresponse()
            eq(first.status, 200)
            first.read()

            conn.request("POST", "/v1/chat", body=json.dumps({"text": "hi"}).encode())
            res = conn.getresponse()
            eq(res.status, 200,
               "the second request on a reused connection must parse")
            ok("Unsupported method" not in res.read().decode(),
               "the previous body leaked into this request line")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_partially_read_body_is_also_drained():
    """The same hazard, one step subtler: a handler that reads *some* of the
    body. `_drain` tracks what was consumed rather than assuming all or
    nothing."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            ctx = client_context(s, with_cert=True)
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            # /v1/session/new reads nothing and is posted a body too.
            conn.request("POST", "/v1/session/new", body=b"{}")
            first = conn.getresponse()
            eq(first.status, 200)
            first.read()          # http.client wants the response drained too
            conn.request("GET", "/v1/hello")
            eq(conn.getresponse().status, 200,
               "a GET after an undrained POST must still parse")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


# ---- the wake word --------------------------------------------------------
@test
def a_wake_runs_no_model_and_says_one_short_thing():
    """The gate has already spent its budget deciding this was Parsa saying
    the word. What answers has to be free, or the cheapest surface in the
    system becomes the one that costs money every time headphones move."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            ctx = client_context(s, with_cert=True)
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("POST", "/v1/wake?source=gate", body=b"{}")
            res = conn.getresponse()
            eq(res.status, 200)
            body = json.loads(res.read())
            eq(body["cost"], 0.0, "a wake must never be billable")
            eq(body["local"], True)
            ok(body["spoken"].startswith("Yes?"),
               f"the WAKE HEARD state says 'Yes?', got {body['spoken']!r}")
            eq(body["agent"], "v", "no agent asked for means V answers")
            eq(bridge.switched, [], "nothing was named, so nothing switched")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def the_wake_word_chooses_who_answers():
    """The gate is trained on a *name*. Hearing "Friday" has to summon the
    Friday persona — with her own memory scope — rather than label a reply
    from V, which is the difference between an agent and a costume."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            ctx = client_context(s, with_cert=True)
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("POST", "/v1/wake?source=gate&agent=friday", body=b"{}")
            res = conn.getresponse()
            eq(res.status, 200)
            body = json.loads(res.read())
            eq(bridge.switched, ["friday"], "the named agent must be summoned")
            eq(body["agent"], "friday", "and the reply must say who answered")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def an_unknown_wake_agent_still_answers():
    """A wake that fails because the config names something odd is a wake that
    silently stops working. `switch_agent` refuses and says why; the wake
    itself degrades to V rather than returning an error to a phone that has
    already decided someone is talking to it."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            ctx = client_context(s, with_cert=True)
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("POST", "/v1/wake?source=gate&agent=jarvis", body=b"{}")
            res = conn.getresponse()
            eq(res.status, 200, "an unknown agent is not a client error")
            body = json.loads(res.read())
            eq(body["agent"], "v", "and V is who is left to answer")
            ok(body["spoken"].startswith("Yes?"))
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)



# ---- the home-screen snapshot ---------------------------------------------
class _Ev:
    """Shaped like `calendar.Event` — the four things `_event` reads."""

    def __init__(self, summary, start=None, location="", day=None):
        import datetime as _dt
        self.summary, self.location = summary, location
        self.date = day or _dt.date(2026, 8, 25)
        self.start = start
        self.all_day = start is None

    @property
    def time_label(self):
        return "all-day" if self.all_day else self.start.strftime("%H:%M")

    def starts_at(self):
        import datetime as _dt
        return None if self.all_day else _dt.datetime.combine(self.date, self.start)


class _Task:
    def __init__(self, tid, text, due=None, state="open", carried=1):
        self.id, self.text, self.due, self.state = tid, text, due, state
        self.section, self.area, self.rel, self.carried = "Tasks", "Work", "n.md", carried

    def age_days(self, today=None):
        import datetime as _dt
        return (( today or _dt.date.today()) - self.due).days if self.due else None

    def is_overdue(self, today=None):
        return (self.age_days(today) or 0) > 0


class _Rem:
    def __init__(self, rid, text, due):
        self.id, self.text, self.due = rid, text, due


class StubGlanceEngine:
    """The engine surface `glance()` touches, and nothing else — so the day a
    handler reaches past it, this test is what says so."""

    def __init__(self, events=(), nxt=None, tasks=(), reminders=(), shape=None):
        import types
        cal = types.SimpleNamespace(on=lambda d: list(events),
                                    next_up=lambda now: nxt)
        tk = types.SimpleNamespace(
            priorities=lambda today=None, limit=6: list(tasks),
            backlog_shape=lambda today=None: shape or {
                "open": 31, "overdue": 4, "due_today": 2, "closed_last_7": 7})
        self.q = types.SimpleNamespace(calendar=cal, tasks=tk)
        self.reminders = types.SimpleNamespace(pending=lambda: list(reminders))


@test
def a_glance_is_one_call_and_costs_nothing():
    """The claim the widget rests on: one request, everything on the card, and
    no path through it that could reach a model."""
    import datetime as dtm
    now = dtm.datetime(2026, 8, 25, 13, 18)
    day = now.date()
    snap = httpapi.glance(StubGlanceEngine(
        events=[_Ev("standup", dtm.time(9, 30)),
                _Ev("روزانه قیمت گذاری", dtm.time(14, 0), "Divar")],
        nxt=_Ev("روزانه قیمت گذاری", dtm.time(14, 0), "Divar"),
        tasks=[_Task(11, "write the brief", dtm.date(2026, 8, 20)),
               _Task(12, "book the flight")],
        reminders=[_Rem(3, "call mom", dtm.datetime(2026, 8, 25, 17, 30))]), now)

    eq(snap["day"], day.isoformat())
    eq(snap["next"]["what"], "روزانه قیمت گذاری")
    eq(snap["next"]["at"], "2026-08-25T14:00", "an instant, never 'in 42m'")
    eq(snap["counts"]["events"], 2)
    eq(snap["counts"]["left"], 1, "09:30 is over; only 14:00 is still ahead")
    eq(snap["tasks"][0]["id"], 11)
    ok(snap["tasks"][0]["overdue"], "five days past its date")
    eq(snap["tasks"][1]["due"], "", "an undated task has no due string")
    eq(snap["reminders"][0]["at"], "2026-08-25T17:30")
    eq(snap["headline"], "1 event · 2 due · 4 overdue · 1 reminder")


@test
def a_glance_with_nothing_on_it_still_draws():
    """The empty case is the common one on a Friday, and a widget cannot show
    an error — so every field a renderer reads has to be present and empty
    rather than absent."""
    import datetime as dtm
    snap = httpapi.glance(StubGlanceEngine(shape={
        "open": 0, "overdue": 0, "due_today": 0, "closed_last_7": 0}),
        dtm.datetime(2026, 8, 25, 9, 0))
    eq(snap["next"], None)
    eq(snap["events"], [])
    eq(snap["tasks"], [])
    eq(snap["headline"], "nothing pressing")


@test
def a_relative_time_is_never_baked_into_the_snapshot():
    """A widget redraws every half hour at best. Any "in 42m" this endpoint
    wrote would go on being wrong for twenty-nine of those minutes, so the
    phone is given instants and does the arithmetic at draw time."""
    import datetime as dtm
    import re
    snap = httpapi.glance(StubGlanceEngine(
        events=[_Ev("standup", dtm.time(14, 0))],
        nxt=_Ev("standup", dtm.time(14, 0)),
        tasks=[_Task(11, "write the brief", dtm.date(2026, 8, 20))],
        reminders=[_Rem(3, "call mom", dtm.datetime(2026, 8, 25, 17, 30))]),
        dtm.datetime(2026, 8, 25, 13, 18))

    # Values only — `reminders` is a key, and a key is not a rendered label.
    relative = re.compile(r"\bin \d+\s*(m|h|min|hour)|\bago\b|\bnow\b", re.I)
    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            ok(not relative.search(node),
               f"{node!r} is a label that goes stale on a home screen")
    walk(snap)
    eq(snap["next"]["at"], "2026-08-25T14:00")
    eq(snap["tasks"][0]["days"], 5, "the gap is a number the phone can label")


@test
def the_glance_route_is_served_to_an_enrolled_phone():
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        import datetime as dtm

        class Bridge(StubBridge):
            def call(self, fn, timeout=0):
                return fn(StubGlanceEngine(
                    tasks=[_Task(11, "write the brief")]))

        server, thread, port = stand_up(s, Bridge())
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("GET", "/v1/glance")
            res = conn.getresponse()
            eq(res.status, 200)
            body = json.loads(res.read())
            eq(body["device"], "pixel")
            eq(body["tasks"][0]["text"], "write the brief")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)

# ---- files, both directions -----------------------------------------------
class StubFileEngine:
    """A real vault behind the two calls `files.save_attachment` makes."""

    def __init__(self, s):
        from v.core.vaultfs import Vault
        self.s = s
        self.vault = Vault(s)
        self.reminders = StubBridge._Outbox()


@test
def an_upload_lands_in_the_vault_as_an_undoable_commit():
    """An attachment is a vault write like any other. Saving it to a temp
    directory the next turn might pick up would mean a file that is not backed
    up, not indexed and not undoable — one more thing to lose."""
    root, s = make_vault()
    s.git_enabled = True
    try:
        pki.enrol("pixel", s)
        engine = StubFileEngine(s)

        class Bridge(StubBridge):
            def call(self, fn, timeout=0):
                return fn(engine)

        server, thread, port = stand_up(s, Bridge())
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("POST", "/v1/attach?name=" +
                         "%D8%B9%DA%A9%D8%B3.png",     # عکس.png
                         body=b"\x89PNG\r\n fake",
                         headers={"Content-Type": "image/png"})
            res = conn.getresponse()
            eq(res.status, 200)
            meta = json.loads(res.read())

            ok(meta["rel"].startswith("attachments/phone/"), meta["rel"])
            ok(meta["rel"].endswith("عکس.png"),
               f"a Persian filename must survive: {meta['rel']}")
            eq(meta["mime"], "image/png")
            eq(meta["bytes"], 11)
            ok((root / meta["rel"]).exists(), "the bytes should be on disk")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def an_attachment_with_no_caption_is_still_a_message():
    """A photo with no words is how most attachments are actually sent, and
    `router` already reads `has_file` as enough to route on. This endpoint used
    to reject it at the door with "text is required"."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            body = json.dumps({"text": "",
                               "file_rel": "attachments/phone/x.png"})
            conn.request("POST", "/v1/chat", body=body.encode(),
                         headers={"Content-Type": "application/json"})
            eq(conn.getresponse().status, 200)

            text, kw = bridge.handled[-1]
            eq(text, "")
            ok(kw["has_file"], "the file is what makes this routable")
            eq(kw["extra"]["file_rel"], "attachments/phone/x.png")
            eq(kw["message_type"], "document")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_turn_with_no_file_still_says_so():
    """`files` is present and empty rather than absent — the phone renders it
    unconditionally, and a key that only sometimes exists is a crash."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("POST", "/v1/chat", body=b'{"text": "hi"}',
                         headers={"Content-Type": "application/json"})
            eq(json.loads(conn.getresponse().read())["files"], [])
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_download_cannot_climb_out_of_the_vault():
    """The one check this endpoint really makes. Private folders are
    deliberately *not* blocked — that guard exists to keep notes away from a
    model, and this is Parsa's own phone behind a certificate this machine
    issued — so the vault boundary is the whole boundary, and it has to hold."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        engine = StubFileEngine(s)

        class Bridge(StubBridge):
            def call(self, fn, timeout=0):
                return fn(engine)

        server, thread, port = stand_up(s, Bridge())
        try:
            ctx = client_context(s, with_cert=True)
            for escape in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd"):
                conn = http.client.HTTPSConnection("127.0.0.1", port,
                                                   context=ctx, timeout=20)
                conn.request("GET", f"/v1/file?rel={escape}")
                res = conn.getresponse()
                body = res.read()
                ok(res.status in (400, 404),
                   f"{escape!r} returned {res.status}: {body[:120]!r}")

            # And the honest case still works, Persian name and all.
            rel = "attachments/phone/عکس.png"
            (root / "attachments/phone").mkdir(parents=True)
            (root / rel).write_bytes(b"PNGDATA")
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx,
                                               timeout=20)
            conn.request("GET", "/v1/file?rel=" +
                         "attachments/phone/%D8%B9%DA%A9%D8%B3.png")
            res = conn.getresponse()
            eq(res.status, 200)
            eq(res.read(), b"PNGDATA")
            eq(res.getheader("Content-Type"), "image/png")
            ok("filename*=UTF-8''" in (res.getheader("Content-Disposition") or ""),
               "a Persian filename needs RFC 5987, not latin-1")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_queued_file_rides_out_with_the_next_turn():
    """`vault_send_file` runs in a different process from the socket. The row
    it leaves is collected by whichever drainer gets there first — here, a chat
    turn, so a chart asked for from the phone arrives *with* the answer rather
    than a minute later on the event stream."""
    from v.core import files
    root, s = make_vault()
    try:
        rel = "attachments/phone/chart.png"
        (root / "attachments/phone").mkdir(parents=True)
        (root / rel).write_bytes(b"x" * 40)

        engine = StubFileEngine(s)
        engine.reminders.rows = [
            {"id": 1, "kind": "file",
             "body": json.dumps({"rel": rel, "caption": "spend by category"})},
            {"id": 2, "kind": "reminder", "body": "not a file"},
            {"id": 3, "kind": "file",
             "body": json.dumps({"rel": "attachments/phone/deleted.png"})},
        ]
        out = files.take_queued_files(engine)

        eq(len(out), 1, "the reminder is not a file and the deleted one is gone")
        eq(out[0]["name"], "chart.png")
        eq(out[0]["caption"], "spend by category")
        eq(out[0]["bytes"], 40)
        eq(engine.reminders.claimed, [1, 3],
           "a file that vanished between queue and delivery is still claimed, "
           "or it is retried forever")

        # The second drainer finds nothing: `claim` is the interlock.
        eq(files.take_queued_files(engine), [])
    finally:
        shutil.rmtree(root)


@test
def a_file_name_from_outside_cannot_become_a_path():
    from v.core import files
    eq(files.safe_name("../../etc/passwd"), "etcpasswd")
    eq(files.safe_name("عکس من.png"), "عکس من.png")
    eq(files.safe_name("  ...  "), "")
    eq(files.mime_for("a/b/c.PDF"), "application/pdf")
    eq(files.mime_for("no-extension"), "application/octet-stream")

# ---- speaking a sentence at a time ----------------------------------------
#: Three sentences that each clear `tts.MIN_CHUNK_CHARS`, so the splitter keeps
#: them apart. Anything shorter is glued forward by design, and a spool test
#: fed short sentences is silently testing one chunk instead of three.
THREE_SENTENCES = (
    "one is the first sentence and it is long enough to stand by itself. "
    "two is the second sentence and it is also long enough to stand alone. "
    "three is the last sentence here and it stands by itself as well.")


@test
def an_answer_is_split_so_the_first_sentence_can_be_spoken_first():
    """The split is what makes time-to-first-word stop scaling with the length
    of the answer, and the rule that earns its keep is the *first* chunk being
    allowed to stand alone however short it is."""
    from v.core import tts
    eq(tts.sentences("Got it. Narmak or Vanak, sir?"),
       ["Got it.", "Narmak or Vanak, sir?"],
       "a two-word opener is a whole chunk — that is the point")
    eq(tts.sentences(""), [])
    eq(tts.sentences("no punctuation at all"), ["no punctuation at all"])

    # Later fragments are glued forward, so the tail is not a stutter of
    # one-word audio files.
    eq(tts.sentences("A long enough opening sentence to stand by itself here. "
                     "Yes. And then some more words after that one."),
       ["A long enough opening sentence to stand by itself here.",
        "Yes. And then some more words after that one."])

    # Persian ؟ ends a sentence; ، does not, being a comma.
    eq(tts.sentences("مرج ابوالفضل انجام شد؟ بله، انجام شد."),
       ["مرج ابوالفضل انجام شد؟", "بله، انجام شد."])

    long = "word " * 200
    parts = tts.sentences(long)
    ok(len(parts) > 1, "an unpunctuated monologue still gets cut")
    ok(all(len(x) <= tts.MAX_CHUNK_CHARS for x in parts),
       "and every piece is under the ceiling")
    eq(" ".join(parts).split(), long.split(), "without losing or duplicating a word")


@test
def the_spool_yields_each_sentence_as_it_is_made_and_then_stops():
    spool = httpapi.SpeechSpool(StubBridge(), "First one. Second one here, "
                                              "which is long enough to stand.")
    frames = list(spool.frames(timeout=10))
    eq(len(frames), 2, "one frame per sentence")
    eq(frames[0], b"OggS:First one.", "and the first sentence is first")
    ok(spool.done.is_set(), "the spool finishes rather than hanging on EOF")

    # An answer with nothing speakable in it must terminate, not block.
    empty = httpapi.SpeechSpool(StubBridge(), "   ")
    eq(list(empty.frames(timeout=5)), [])
    ok(empty.done.is_set())


@test
def sentences_are_published_in_order_however_they_finish():
    """The hosted voice runs four at a time, so a later sentence routinely
    finishes before an earlier one — it is a fixed round trip per call, not
    work proportional to length. Publishing them as they land would read the
    answer out of order, which is worse than reading it slowly."""
    import threading

    class Wide(StubBridge):
        concurrency = 4

        def __init__(self):
            super().__init__()
            self.started = threading.Barrier(3, timeout=10)

        def speak(self, text):
            # All three in flight together, then finish back to front: the
            # worst ordering the real thing can produce, made deterministic.
            self.started.wait()
            time.sleep({"one": 0.30, "two": 0.15}.get(text.split()[0], 0.0))
            return b"OggS:" + text.split()[0].encode()

    # Each sentence has to clear `tts.MIN_CHUNK_CHARS` on its own, or the
    # splitter glues it to its neighbour — which is right, and which is what
    # this test caught itself doing when the sentences were "One. Two. Three."
    spool = httpapi.SpeechSpool(Wide(), THREE_SENTENCES)
    frames = list(spool.frames(timeout=20))
    eq([f.decode() for f in frames], ["OggS:one", "OggS:two", "OggS:three"],
       "source order, not completion order")


@test
def one_failed_sentence_does_not_take_the_rest_of_the_answer_with_it():
    """A single sentence the voice choked on is a gap in the reading. The
    sentences around it were already made and are still worth hearing."""
    class Flaky(StubBridge):
        def speak(self, text):
            if text.startswith("two"):
                raise RuntimeError("that one broke")
            return b"OggS:" + text.split()[0].encode()

    spool = httpapi.SpeechSpool(Flaky(), THREE_SENTENCES)
    frames = [f.decode() for f in list(spool.frames(timeout=20))]
    eq(frames, ["OggS:one", "OggS:three"], "the gap is skipped, not fatal")
    ok(spool.done.is_set())


@test
def a_voice_that_cannot_speak_ends_the_stream_instead_of_hanging():
    """The words are already on screen; a broken Piper must not also cost the
    phone a two-minute wait on a socket that will never produce a frame."""
    class Mute(StubBridge):
        def speak(self, text):
            raise RuntimeError("piper is not installed")

    spool = httpapi.SpeechSpool(Mute(), "One. Two.")
    eq(list(spool.frames(timeout=10)), [])
    ok(spool.done.is_set())
    ok("piper" in spool.error, f"the reason is kept: {spool.error!r}")


@test
def the_turn_only_spools_audio_when_the_caller_asks_for_it():
    """The widgets and the terminal call these endpoints and have no speaker.
    Warming Piper for an answer nobody hears is work done for nothing."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            def chat(path):
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", port, context=client_context(s, with_cert=True),
                    timeout=20)
                conn.request("POST", path, json.dumps({"text": "hi"}),
                             {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            quiet = chat("/v1/chat")
            ok("turn" not in quiet, "no speaker asked, no audio made")

            loud = chat("/v1/chat?speak=1")
            ok(loud.get("turn"), "asked for, so there is a turn id to read")

            # And that id streams the answer as length-prefixed frames.
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("GET", f"/v1/say?turn={loud['turn']}")
            res = conn.getresponse()
            eq(res.status, 200)
            raw = res.read()
            frames, i = [], 0
            while i + 4 <= len(raw):
                n = int.from_bytes(raw[i:i + 4], "big")
                frames.append(raw[i + 4:i + 4 + n])
                i += 4 + n
            ok(frames and frames[0].startswith(b"OggS:"),
               f"the first frame is audio, got {frames[:1]}")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def an_expired_or_unknown_turn_is_a_404_rather_than_a_hang():
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("GET", "/v1/say?turn=nosuchturn")
            eq(conn.getresponse().status, 404)
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


# ---- an utterance arriving in pieces --------------------------------------
@test
def the_bridge_keeps_one_stream_per_utterance_and_drops_it_when_it_ends():
    """The registry between the handler and `stt.Stream`, which the endpoint
    tests stub over. Built without an engine — `_Bridge.__init__` starts a
    thread that opens the index — because none of what is under test needs one.
    """
    import threading
    from v.core import stt

    class Model:
        """Whisper's shape, minus Whisper: one word per chunk it is given."""
        RATE = 16_000
        model_size = "fake"

        def __init__(self):
            self.calls = 0

        def transcribe_pcm(self, pcm, *, language=None, initial_prompt=None):
            self.calls += 1
            return stt.Transcript(
                text=f"word{self.calls}", language="en", language_probability=1.0,
                duration=len(pcm) / 2 / self.RATE, seconds_taken=0.0,
                model="fake")

    bridge = httpapi._Bridge.__new__(httpapi._Bridge)
    bridge._streams = {}
    bridge._streams_lock = threading.Lock()
    bridge._stt_lock = threading.Lock()
    model = Model()
    bridge.transcriber = lambda: model

    # A second of audio at a time. `stt.MIN_CHUNK_SECONDS` is 6, so the first
    # five are held rather than run — that holding is the point, not a bug.
    second = b"\x01\x00" * 16_000
    for _ in range(5):
        eq(bridge.push_chunk("utt-1", second, final=False), "")
    eq(model.calls, 0, "short pieces are held, not run one pass each")

    eq(bridge.push_chunk("utt-1", second, final=False), "word1",
       "the sixth second crosses the threshold and runs once")
    eq(model.calls, 1)
    ok("utt-1" in bridge._streams, "the utterance is still open")

    # Two utterances at once stay separate.
    bridge.push_chunk("utt-2", second, final=False)
    eq(len(bridge._streams), 2)

    eq(bridge.push_chunk("utt-1", second, final=True), "word1 word2",
       "the tail is transcribed and joined onto what came before")
    ok("utt-1" not in bridge._streams, "a finished utterance is dropped")
    ok("utt-2" in bridge._streams, "and the other one is untouched")

    # A tail shorter than the threshold must still be run, never dropped.
    model.calls = 0
    bridge.push_chunk("utt-3", second, final=False)
    eq(model.calls, 0, "one second is under the threshold")
    eq(bridge.push_chunk("utt-3", b"", final=True), "word1",
       "and `final` forces the held audio through rather than losing it")


@test
def an_abandoned_utterance_does_not_hold_a_transcript_forever():
    import threading
    bridge = httpapi._Bridge.__new__(httpapi._Bridge)
    bridge._streams = {}
    bridge._streams_lock = threading.Lock()
    bridge._stt_lock = threading.Lock()
    bridge.transcriber = lambda: None

    # An utterance from before the TTL — the app was killed mid-sentence.
    bridge._streams["old"] = (time.time() - httpapi._Bridge.STREAM_TTL - 1,
                              object())
    bridge._streams["new"] = (time.time(), object())
    with bridge._streams_lock:
        bridge._sweep_streams()
    ok("old" not in bridge._streams, "swept")
    ok("new" in bridge._streams, "and only the stale one")



@test
def chunks_transcribe_as_they_arrive_and_only_the_last_one_answers():
    """The whole point of the streaming path: everything but the tail is
    already through Whisper when the finger lifts, and the turn runs on the
    same request that ended the sentence rather than needing another."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            def chunk(final, body=b"\x00\x01" * 800):
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", port, context=client_context(s, with_cert=True),
                    timeout=20)
                conn.request("POST",
                             f"/v1/voice/chunk?utt=abc123&final={1 if final else 0}",
                             body, {"Content-Type": "application/octet-stream"})
                res = conn.getresponse()
                return res.status, json.loads(res.read())

            status, first = chunk(False)
            eq(status, 200)
            eq(first["transcript"], "chunk1")
            eq(first["final"], False)
            ok("text" not in first, "a mid-utterance chunk does not answer")
            eq(getattr(bridge, "handled", []), [],
               "and must not have run a turn")

            status, second = chunk(False)
            eq(second["transcript"], "chunk1 chunk2", "the transcript grows")

            status, last = chunk(True)
            eq(status, 200)
            eq(last["transcript"], "chunk1 chunk2 chunk3")
            eq(last["text"], "Noted, sir.", "the final chunk carries the turn")
            eq(len(bridge.handled), 1, "exactly one turn, on the last chunk")
            eq(bridge.handled[0][0], "chunk1 chunk2 chunk3",
               "and it is given the whole transcript, not the last piece")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def a_streamed_utterance_with_nothing_in_it_is_not_an_error():
    """Same posture as the one-shot path: a button brushed is an ordinary
    thing to do, and an error dialog for it trains you to dismiss dialogs."""
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        bridge = StubBridge()
        server, thread, port = stand_up(s, bridge)
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=client_context(s, with_cert=True),
                timeout=20)
            conn.request("POST", "/v1/voice/chunk?utt=quiet1&final=1", b"",
                         {"Content-Type": "application/octet-stream"})
            res = conn.getresponse()
            eq(res.status, 200)
            body = json.loads(res.read())
            ok(body["empty"], "an empty utterance is a state, not a failure")
            eq(getattr(bridge, "handled", []), [], "and runs no turn")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


@test
def an_utterance_id_from_outside_cannot_become_anything_else():
    root, s = make_vault()
    try:
        pki.enrol("pixel", s)
        server, thread, port = stand_up(s)
        try:
            for bad in ("../../etc", "a b", "'; drop", "x" * 65, ""):
                conn = http.client.HTTPSConnection(
                    "127.0.0.1", port,
                    context=client_context(s, with_cert=True), timeout=20)
                conn.request("POST",
                             "/v1/voice/chunk?utt=" + quote(bad) + "&final=0",
                             b"\x00\x01", {"Content-Type": "application/octet-stream"})
                eq(conn.getresponse().status, 400, f"{bad!r} should be refused")
        finally:
            server.stop()
    finally:
        shutil.rmtree(root)


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if callable(v) and hasattr(v, "fn")]
    print(f"\nrunning {len(tests)} tests\n")
    for t in tests:
        t()
    print(f"\n\x1b[1m{len(PASS)} passed, {len(FAIL)} failed\x1b[0m")
    for name, exc, tb in FAIL:
        print(f"\n\x1b[31m{name}\x1b[0m\n{tb}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
