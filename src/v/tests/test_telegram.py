"""Tests for the Telegram transport.

Run:  python3 -m v.tests.test_telegram

Every test here runs with `telegram_bot_token` unset, which means `_enabled()`
is False and every outbound path short-circuits before touching httpx — the
same posture `BRAIN_NO_NOTIFY` gives the notify-send channel. Nothing in this
file makes a real network call. Where a test needs to exercise the download
path, it monkeypatches `TelegramBot._download` rather than setting a token.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
os.environ["BRAIN_NO_MODEL"] = "1"
os.environ["BRAIN_NO_NOTIFY"] = "1"
os.environ["BRAIN_NO_TELEGRAM"] = "1"
# Every `_reply()` call in this file would otherwise try to load the real
# Piper voice and shell out to ffmpeg — slow and pointless for tests that
# don't care about audio. The TTS-specific tests below flip this off for
# just the call they're testing, then restore it.
os.environ["BRAIN_NO_TTS"] = "1"
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import router, telegram, vaultfs
from v.core.engine import Engine
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


def dispatch(bot, upd, engine):
    """`_prepare` (off-thread in production) then `_commit` (the serial
    worker), run back to back — what `_prep_and_enqueue` + `_worker` do
    across two threads, collapsed into one call for a synchronous test."""
    item = bot._prepare(upd)
    if item is not None:
        bot._commit(item, engine)
    return item


def archive(bot, msg, chat, engine):
    bot._commit_archive(bot._prepare_archive(msg, chat), engine)


# ---------------------------------------------------------------------
@test
def outbound_is_a_noop_with_no_token_configured():
    root, s = make_vault()
    try:
        eq(telegram._enabled(s), False)
        eq(telegram.send_message(s, 12345, "hello"), False)
        eq(telegram.notify_owner(s, "hello"), False)
        # No text, no owner id — still False, still no attempt made.
        eq(telegram.send_message(s, 0, "hello"), False)
        eq(telegram.send_message(s, 12345, "   "), False)
    finally:
        shutil.rmtree(root)


@test
def markdown_becomes_a_plain_chat_bubble():
    text = telegram._to_telegram("**bold** and a [[Some Note|link]] and _em_")
    ok("**" not in text, "double-star bold must not survive into the bubble")
    ok("[[" not in text, "wikilink brackets must not survive")
    ok("link" in text)


@test
def filenames_are_sanitised():
    eq(telegram._safe_filename("Amir's Songs 🎵/../etc"), "Amirs Songs etc")
    eq(telegram._safe_filename(""), "")


@test
def buttons_round_trip_through_the_callback_table():
    from v.core.engine import Button

    buttons = [Button("Undo", "undo", {"commit": "abc123"})]
    kb = telegram._keyboard(buttons)
    token = kb[0][0]["callback_data"]
    action, payload = telegram._CALLBACKS[token]
    eq(action, "undo")
    eq(payload, {"commit": "abc123"})


# ---- security: the token never reaches a log or a world-readable file ----
@test
def the_bot_token_is_redacted_from_anything_printed():
    """`httpx`'s own exception strings embed the full request URL, and the
    Bot API puts the token in that URL — confirmed against a real 404 during
    development. Every error this module might print has to go through
    `_scrub` first, or a caught network error becomes a token leak into
    whatever's capturing stdout (a log file, a terminal someone screenshots,
    a systemd journal)."""
    token = "123456789:AA-not-a-real-token-just-for-the-scrub-test"
    msg = (f"Client error '404 Not Found' for url "
          f"'https://api.telegram.org/bot{token}/getMe'")
    scrubbed = telegram._scrub(msg)
    ok(token not in scrubbed, "the raw token must not survive scrubbing")
    ok("REDACTED" in scrubbed)

    file_msg = (f"404 for url 'https://api.telegram.org/file/"
               f"bot{token}/documents/file_42.pdf'")
    ok(token not in telegram._scrub(file_msg),
       "the file-download URL carries the token too, and must be scrubbed too")


@test
def httpx_request_logging_cannot_leak_the_token():
    """The leak this actually caught in production: `httpx` logs
    `'HTTP Request: %s %s "%s %d %s"'` — full URL, so the token in it — at
    INFO through `logging.getLogger("httpx")`, silent until *something else*
    in the process attaches a root handler. `faster_whisper`'s model loader
    does exactly that as a side effect the first time it touches the
    network, and once it has, every `httpx` call in the whole process starts
    printing, this module's `sendMessage` calls included. Reproduces that
    ordering exactly: attach a root INFO handler first (as if huggingface_hub
    already ran), *then* confirm `telegram`'s import-time `setLevel` still
    wins for a real request afterwards."""
    import io

    buf = io.StringIO()
    logging.basicConfig(level=logging.INFO, stream=buf, force=True)
    try:
        # `telegram` is already imported (it's what's under test), so this
        # re-asserts the level rather than re-triggering the import-time
        # side effect — which is fine, it's the level that has to hold.
        eq(logging.getLogger("httpx").level, logging.WARNING)
        # The exact call `httpx/_client.py` makes on every real request — no
        # network needed to prove whether it's suppressed, and this file
        # makes no real network calls.
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"', "POST",
            "https://api.telegram.org/bot123:FAKESECRET/sendMessage",
            "HTTP/1.1", 200, "OK")
        ok("FAKESECRET" not in buf.getvalue(),
           "an httpx request log line leaked the token even with a root "
           "INFO handler already attached")
    finally:
        logging.basicConfig(level=logging.WARNING, force=True)


@test
def config_json_is_written_owner_only():
    """The token lives in this file. The process umask left it 0664 —
    world-readable — on the machine this was built on; every write must
    lock it to 0600 regardless of what wrote it or what umask is active."""
    root, s = make_vault()
    try:
        telegram._write_config(s, {"telegram_bot_token": "x", "telegram_owner_id": 1})
        path = s.state_dir / "config.json"
        mode = path.stat().st_mode & 0o777
        eq(mode, 0o600, f"config.json must be owner-only, was {oct(mode)}")
    finally:
        shutil.rmtree(root)


# ---- owner enforcement ------------------------------------------------
@test
def the_first_private_message_claims_ownership():
    root, s = make_vault()
    try:
        # Exercise the unclaimed-bot path explicitly — `telegram_owner_id`
        # now defaults to Parsa's real id (`config.py`, hardcoded rather than
        # bootstrapped via claim), so a fresh Settings() is no longer 0 on
        # its own.
        s.telegram_owner_id = 0
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        eq(s.telegram_owner_id, 0)
        upd = {"update_id": 1, "message": {
            "message_id": 1, "from": {"id": 999}, "chat": {"id": 999, "type": "private"},
            "text": "hi",
        }}
        dispatch(bot, upd, e)
        eq(s.telegram_owner_id, 999)
        # persisted, not just in-process — a restarted bot must remember it
        raw = json.loads((s.state_dir / "config.json").read_text())
        eq(raw["telegram_owner_id"], 999)
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_stranger_is_dropped_without_reaching_the_engine():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)

        called = []
        e.handle = lambda *a, **kw: called.append((a, kw)) or (_ for _ in ()).throw(
            AssertionError("engine.handle must not run for a non-owner"))

        upd = {"update_id": 2, "message": {
            "message_id": 2, "from": {"id": 222}, "chat": {"id": 222, "type": "private"},
            "text": "hello?",
        }}
        dispatch(bot, upd, e)  # must return quietly, not raise
        eq(called, [])
        e.close()
    finally:
        shutil.rmtree(root)


@test
def owner_messages_reach_the_engine():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        upd = {"update_id": 3, "message": {
            "message_id": 3, "from": {"id": 111}, "chat": {"id": 111, "type": "private"},
            "text": "what are my priorities?",
        }}
        dispatch(bot, upd, e)  # must not raise; send_message no-ops with no token
        e.close()
    finally:
        shutil.rmtree(root)


@test
def the_worker_opens_a_real_session():
    """Without this, Telegram never gets multi-turn context at all — every
    message would be its own one-off ask, the same failure a bare `_chat`
    call had before sessions existed anywhere in the engine."""
    root, s = make_vault()
    try:
        from v.core import session as session_mod

        bot = telegram.TelegramBot(s)
        threading.Thread(target=bot._worker, daemon=True).start()
        store = session_mod.SessionStore(s)
        for _ in range(100):
            if store.current():
                break
            time.sleep(0.02)
        ok(store.current() is not None,
           "the worker must open a session on startup")
    finally:
        shutil.rmtree(root)


@test
def slash_start_never_reaches_the_engine():
    """The regression from this run: `/start` — sent automatically by
    Telegram, never typed by Parsa — was routed as an ordinary question,
    escalated to the agent, and burned a turn on a stalled API call for a
    button press that meant nothing at all."""
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        called = []
        e.handle = lambda *a, **kw: called.append((a, kw)) or (_ for _ in ()).throw(
            AssertionError("/start must never reach engine.handle"))
        bot = telegram.TelegramBot(s)
        upd = {"update_id": 9, "message": {
            "message_id": 9, "from": {"id": 111},
            "chat": {"id": 111, "type": "private"}, "text": "/start",
        }}
        dispatch(bot, upd, e)
        eq(called, [])
        e.close()
    finally:
        shutil.rmtree(root)


@test
def slash_new_cuts_a_fresh_thread():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        e.start_session()
        first = e.sessions.current()
        ok(first is not None)

        bot = telegram.TelegramBot(s)
        upd = {"update_id": 10, "message": {
            "message_id": 10, "from": {"id": 111},
            "chat": {"id": 111, "type": "private"}, "text": "/new",
        }}
        dispatch(bot, upd, e)

        second = e.sessions.current()
        ok(second is not None, "a new session must be open after /new")
        ok(second.id != first.id, "/new must cut a fresh thread, not resume it")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def the_worker_survives_a_failing_update_and_keeps_going():
    """The exact regression from today: a stuck or raising update must not
    stop the worker from draining the rest of the queue — a single bad or
    slow message must not look like the whole bot went dark."""
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        seen = []

        def fake_commit(item, engine):
            if item["id"] == 1:
                raise RuntimeError("boom")
            seen.append(item["id"])
        bot._commit = fake_commit

        t = threading.Thread(target=bot._worker, daemon=True)
        t.start()
        bot._queue.put({"id": 1})
        bot._queue.put({"id": 2})
        for _ in range(100):
            if 2 in seen:
                break
            time.sleep(0.02)
        ok(2 in seen, "update 2 must still be processed after update 1 failed")
    finally:
        shutil.rmtree(root)


@test
def the_transcriber_is_built_and_loaded_exactly_once():
    """The regression that made one voice memo download the Whisper model
    twice in parallel: a fresh `Transcriber()` per call meant the warm-up
    thread and the first real voice message each triggered their own
    multi-gigabyte download, neither aware the other had started. `_stt` is
    now shared and `_stt_lock` serialises the one `load()` that matters."""
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        builds, loads = [], []

        class FakeTranscriber:
            def __init__(self, settings):
                builds.append(1)

            def load(self):
                loads.append(1)

        import v.core.stt as stt_mod
        orig = stt_mod.Transcriber
        stt_mod.Transcriber = FakeTranscriber
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(bot._transcriber()))
                      for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            eq(len(builds), 1, "only one Transcriber should ever be constructed")
            eq(len(loads), 1, "load() must run exactly once, however many "
                              "threads ask for it concurrently")
            ok(all(r is results[0] for r in results),
               "every caller must get back the same shared instance")
        finally:
            stt_mod.Transcriber = orig
    finally:
        shutil.rmtree(root)


# ---- attachment parsing -------------------------------------------------
@test
def attachment_kind_is_read_off_the_message_shape():
    A = telegram.TelegramBot._attachment
    eq(A({"text": "hi"}), (None, None, "text"))
    eq(A({"photo": [{"file_id": "a"}, {"file_id": "b"}]}), ("b", None, "photo"))
    eq(A({"audio": {"file_id": "x", "title": "Song"}}), ("x", "Song", "audio"))
    eq(A({"document": {"file_id": "y", "file_name": "notes.pdf"}}),
       ("y", "notes.pdf", "document"))


# ---- the archive path ---------------------------------------------------
@test
def a_text_message_in_a_group_is_filed_under_brain_dump():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        msg = {"message_id": 5, "from": {"id": 111}, "text": "check this out"}
        chat = {"id": -100, "type": "group", "title": "Cool Stuff"}
        archive(bot, msg, chat, e)

        rel = f"{s.folders.braindump}/Telegram Archive/Cool Stuff.md"
        ok(e.vault.exists(rel), "archive note was not created")
        body = e.vault.read(rel).body
        ok("check this out" in body)
        eq(e.vault.read(rel).meta["source-chat"], "Cool Stuff")
        e.close()
    finally:
        shutil.rmtree(root)


@test
def an_audio_file_in_a_group_is_filed_under_music_and_embedded():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        bot._download = lambda file_id: (b"fake mp3 bytes", "song.mp3")

        msg = {"message_id": 6, "from": {"id": 111}, "caption": "new demo",
              "audio": {"file_id": "abc", "title": "Demo"}}
        chat = {"id": -101, "type": "supergroup", "title": "Music Dump"}
        archive(bot, msg, chat, e)

        rel = f"{s.folders.music}/Telegram Archive/Music Dump.md"
        ok(e.vault.exists(rel))
        body = e.vault.read(rel).body
        ok("new demo" in body)
        ok("![[attachments/telegram/" in body, "the file must be embedded")

        # And the file itself really landed on disk, git-committed.
        embedded_rel = body.split("![[", 1)[1].split("]]", 1)[0]
        ok((root / embedded_rel).exists())
        e.close()
    finally:
        shutil.rmtree(root)


@test
def a_second_message_appends_rather_than_recreating():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        chat = {"id": -102, "type": "group", "title": "Notes"}
        archive(bot, {"message_id": 1, "from": {"id": 111}, "text": "first"}, chat, e)
        archive(bot, {"message_id": 2, "from": {"id": 111}, "text": "second"}, chat, e)
        rel = f"{s.folders.braindump}/Telegram Archive/Notes.md"
        body = e.vault.read(rel).body
        ok("first" in body and "second" in body)
        e.close()
    finally:
        shutil.rmtree(root)


@test
def an_empty_service_message_is_not_filed():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 111
        e = Engine(s)
        bot = telegram.TelegramBot(s)
        chat = {"id": -103, "type": "group", "title": "Empty"}
        archive(bot, {"message_id": 1, "from": {"id": 111}}, chat, e)
        rel = f"{s.folders.braindump}/Telegram Archive/Empty.md"
        eq(e.vault.exists(rel), False)
        e.close()
    finally:
        shutil.rmtree(root)


# ---- Vault.save_binary --------------------------------------------------
@test
def save_binary_writes_and_commits_and_refuses_to_clobber():
    root, s = make_vault()
    try:
        v = vaultfs.Vault(s)
        change = v.save_binary("attachments/telegram/x.bin", b"\x00\x01\x02",
                               summary="test binary")
        eq((root / "attachments/telegram/x.bin").read_bytes(), b"\x00\x01\x02")
        eq(change.kind, "create")
        try:
            v.save_binary("attachments/telegram/x.bin", b"\x03", summary="again")
            raise AssertionError("must refuse to overwrite")
        except vaultfs.VaultError:
            pass
    finally:
        shutil.rmtree(root)


# ---- capture.py's file embed ---------------------------------------------
@test
def a_captured_note_embeds_an_attached_file():
    root, s = make_vault()
    try:
        e = Engine(s)
        route = router.Route(router.CAPTURE_NOTE, 0.9, "attachment")
        result = e.capture.capture(
            "look at this", route, when=dt.datetime(2026, 7, 15, 9, 0),
            extra={"file_rel": "attachments/telegram/pic.jpg"})
        ok(result.change is not None)
        body = e.vault.read(result.target).body
        ok("![[attachments/telegram/pic.jpg]]" in body)
        e.close()
    finally:
        shutil.rmtree(root)


# ---- the scheduler sink --------------------------------------------------
@test
def build_sink_renders_and_sends_each_item():
    root, s = make_vault()
    try:
        sent = []
        orig = telegram.send_message
        telegram.send_message = lambda s_, chat, text, **kw: sent.append(
            (chat, text)) or True
        try:
            s.telegram_owner_id = 555
            sink = telegram.build_sink(s)

            class Fake:
                def render(self):
                    return "reminder text"
            sink("reminders", ("reminders", [Fake()]))
            eq(len(sent), 1)
            eq(sent[0], (555, "reminder text"))

            # A bare string, and None in a list — both handled without raising.
            sink("nudges", ("nudges", "just a string"))
            sink("brag", ("brag", [None]))
            eq(len(sent), 2)

            # The return value is what `Scheduler._deliver` settles on, so a
            # send that Telegram refused has to come back False — otherwise a
            # reminder is marked delivered on the strength of a dropped POST.
            ok(sink("reminders", ("reminders", [Fake()])) is not False,
               "a successful push reports success")
            telegram.send_message = lambda s_, chat, text, **kw: False
            ok(sink("reminders", ("reminders", [Fake()])) is False,
               "a failed push must be reported, not swallowed")
        finally:
            telegram.send_message = orig
    finally:
        shutil.rmtree(root)


# ---- voice replies -------------------------------------------------------
@test
def spoken_text_drops_the_sources_line_and_caps_length():
    text = "Here's the answer.\n\nSources: [[Note A]] [[Note B]]"
    eq(telegram._for_speech(text), "Here's the answer.")

    long_text = "x" * 5000
    eq(len(telegram._for_speech(long_text)), 2000,
       "a very long reply must be capped, not read out in full")


@test
def tts_respects_the_kill_switch():
    root, s = make_vault()
    try:
        eq(telegram._tts_enabled(s), False,
           "BRAIN_NO_TTS is set for the whole suite and must win")
        old = os.environ.pop("BRAIN_NO_TTS")
        try:
            eq(telegram._tts_enabled(s), True)
            s.tts_provider = "null"
            eq(telegram._tts_enabled(s), False, "provider='null' is its own off switch")
        finally:
            os.environ["BRAIN_NO_TTS"] = old
    finally:
        shutil.rmtree(root)


@test
def the_synthesizer_is_built_and_loaded_exactly_once():
    """Same regression class as the transcriber test above, mirrored for
    the outbound side: one shared `MixedSynthesizer`, `load()` run once no
    matter how many threads ask for it at once."""
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        builds, loads = [], []

        class FakeSynthesizer:
            def __init__(self, settings):
                builds.append(1)

            def load(self):
                loads.append(1)

        import v.core.tts as tts_mod
        orig = tts_mod.MixedSynthesizer
        tts_mod.MixedSynthesizer = FakeSynthesizer
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(bot._synth()))
                      for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            eq(len(builds), 1, "only one Synthesizer should ever be constructed")
            eq(len(loads), 1, "load() must run exactly once")
            ok(all(r is results[0] for r in results),
               "every caller must get back the same shared instance")
        finally:
            tts_mod.MixedSynthesizer = orig
    finally:
        shutil.rmtree(root)


@test
def a_reply_sends_both_text_and_a_voice_note_when_tts_is_enabled():
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        texted, voiced, sent_files = [], [], []

        orig_send = telegram.send_message
        telegram.send_message = lambda s_, chat, text, **kw: texted.append(
            (chat, text)) or True

        class FakeSynth:
            def synthesize(self, text):
                voiced.append(text)
                return b"fake-wav"
        bot._synth = lambda: FakeSynth()

        import v.core.tts as tts_mod
        orig_ogg = tts_mod.to_ogg_opus
        tts_mod.to_ogg_opus = lambda wav: b"fake-ogg"

        orig_send_file = telegram._send_voice_file
        telegram._send_voice_file = lambda s_, chat, ogg: sent_files.append((chat, ogg))

        old = os.environ.pop("BRAIN_NO_TTS")
        try:
            bot._reply(999, "hello sir")
        finally:
            os.environ["BRAIN_NO_TTS"] = old
            tts_mod.to_ogg_opus = orig_ogg
            telegram._send_voice_file = orig_send_file
            telegram.send_message = orig_send

        eq(texted, [(999, "hello sir")], "the text bubble must still go out")
        eq(voiced, ["hello sir"], "and the same text handed to synthesis")
        eq(sent_files, [(999, b"fake-ogg")], "and a voice note sent alongside it")
    finally:
        shutil.rmtree(root)


@test
def voice_synthesis_failure_still_leaves_the_text_reply_sent():
    """A missing model or a Piper crash must degrade to text-only, not take
    the whole reply down with it — the text has already gone out by the
    time voice synthesis is even attempted."""
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        texted = []
        orig_send = telegram.send_message
        telegram.send_message = lambda s_, chat, text, **kw: texted.append(
            (chat, text)) or True

        import v.core.tts as tts_mod

        def boom():
            raise tts_mod.Unavailable("no model")
        bot._synth = boom

        old = os.environ.pop("BRAIN_NO_TTS")
        try:
            bot._reply(42, "still here")  # must not raise
        finally:
            os.environ["BRAIN_NO_TTS"] = old
            telegram.send_message = orig_send

        eq(texted, [(42, "still here")])
    finally:
        shutil.rmtree(root)


@test
def chat_action_is_sent_while_the_block_runs():
    root, s = make_vault()
    try:
        bot = telegram.TelegramBot(s)
        calls = []
        orig_call = telegram._call

        def fake_call(s_, method, **params):
            calls.append((method, params.get("action")))
            return {}
        telegram._call = fake_call
        try:
            with bot._chat_action(123, "typing"):
                for _ in range(50):
                    if calls:
                        break
                    time.sleep(0.02)
        finally:
            telegram._call = orig_call
        ok(("sendChatAction", "typing") in calls,
           "the action must actually be sent while the block is running")
    finally:
        shutil.rmtree(root)


# ---- startup / shutdown notices -------------------------------------------
@test
def announce_offline_notifies_the_owner_and_exits():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 777
        sent = []
        orig = telegram.send_message
        telegram.send_message = lambda s_, chat, text, **kw: sent.append(
            (chat, text)) or True
        try:
            try:
                telegram._announce_offline(s)
                raise AssertionError("_announce_offline must raise SystemExit")
            except SystemExit:
                pass
        finally:
            telegram.send_message = orig
        eq(len(sent), 1)
        eq(sent[0][0], 777)
        ok("offline" in sent[0][1].lower())
    finally:
        shutil.rmtree(root)


@test
def announce_offline_is_silent_with_no_owner_claimed():
    root, s = make_vault()
    try:
        s.telegram_owner_id = 0
        sent = []
        orig = telegram.send_message
        telegram.send_message = lambda *a, **kw: sent.append(a) or True
        try:
            try:
                telegram._announce_offline(s)
                raise AssertionError("must still raise SystemExit with nothing to notify")
            except SystemExit:
                pass
        finally:
            telegram.send_message = orig
        eq(sent, [], "no owner claimed yet — nothing to send")
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
