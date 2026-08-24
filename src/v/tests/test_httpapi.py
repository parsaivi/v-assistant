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

    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []
        self.conn = StubConn()
        # `h_wake` reports who answered, so the stub needs an agent to be. A
        # handler that reached for `engine.persona` and found nothing here
        # would raise inside the closure and fail every wake at runtime while
        # every test still passed.
        self.persona = StubBridge._Persona()
        self.switched = []

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
        return b"OggS-not-really"


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
