"""Tests for source labels, owner-only approval, and the guarded email send.

Run:  python3 -m v.tests.test_provenance

Same no-pytest harness as test_core. These are the properties Parsa has
described to other people as true of V, so each one is checked at the layer
that enforces it rather than at the layer that describes it:

* every write through `Vault` records who wrote it, and an unstated author is
  `v` (`core/provenance.py`);
* V's text reaches the model fenced, Parsa's never does (`vault_read`,
  `vault_search`, RAG context, the evidence block);
* only Parsa approves — nothing a model can call writes an approval, and the
  approval writers refuse inside a model's shell;
* `Gmail.send` refuses anything but the exact draft Parsa approved, once
  (`core/mailguard.py`). The Gmail API is faked; nothing leaves the machine.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import os
os.environ["BRAIN_NO_MODEL"] = "1"
os.environ["BRAIN_NO_NOTIFY"] = "1"
os.environ["BRAIN_NO_JALALI"] = "1"
os.environ.pop("V_MODEL_SHELL", None)
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v.core import (config, gmail as gmail_mod, index, mailguard, provenance,
                    rag, router, shells, vaultfs)
from v.core.engine import Engine, _author_of, _evidence_block

PASS, FAIL = [], []
PKG = Path(__file__).resolve().parents[1]


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


def make_vault() -> tuple[Path, config.Settings]:
    root = Path(tempfile.mkdtemp(prefix="brain-prov-"))
    (root / "01 - Daily Notes").mkdir(parents=True)
    (root / "07 - Goals").mkdir(parents=True)
    (root / "07 - Goals/Plan.md").write_text(
        "# Plan\n\nShip the pricing rollout by Friday.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text(".state/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=p", "-c", "user.email=p@p",
                    "commit", "-qm", "base"], cwd=root, check=True)
    s = config.Settings(vault=root, state_dir=root / ".state",
                        model_dir=root / ".models")
    s.ensure_dirs()
    return root, s


def _model_shell(on: bool) -> None:
    if on:
        os.environ[provenance.MODEL_SHELL_ENV] = "1"
    else:
        os.environ.pop(provenance.MODEL_SHELL_ENV, None)


# --- 1. a label on every write --------------------------------------------
@test
def every_write_path_records_who_wrote_it():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    st = v.provenance
    changes = [
        v.append("07 - Goals/Plan.md", "- Parsa's line", source="parsa"),
        v.create("07 - Goals/New.md", {"type": "goal"}, "\nV wrote this.\n",
                 source="v"),
        v.edit("07 - Goals/Plan.md", "by Friday", "by Thursday", source="v"),
        v.rewrite("07 - Goals/New.md", "---\ntype: goal\n---\n\nRewritten.\n",
                  allow_rewrite=True, source="v"),
        v.set_meta("07 - Goals/New.md", {"status": "open"}, source="parsa"),
        v.save_binary("01 - Daily Notes/x.png", b"\x89PNG..", source="parsa"),
    ]
    want = ["parsa", "v", "v", "v", "parsa", "parsa"]
    for c, w in zip(changes, want):
        eq(c.source, w, f"{c.kind} {c.rel}:")
        ok(c.blocks, f"{c.kind} {c.rel} recorded no block")
        rows = st.conn.execute(
            f"SELECT source FROM blocks WHERE id IN "
            f"({','.join('?' * len(c.blocks))})", c.blocks).fetchall()
        ok(rows and all(r[0] == w for r in rows), f"{c.kind}: {rows}")
    # The edit labelled only the line it changed — the rest of Plan.md is
    # still Parsa's, because a label is per block, not per note.
    labs = st.labels("07 - Goals/Plan.md")
    lines = (root / "07 - Goals/Plan.md").read_text().split("\n")
    for ln, lab in zip(lines, labs):
        if "Thursday" in ln:
            eq(lab, provenance.UNAPPROVED, "the edited line")
        elif ln.strip():
            eq(lab, None, f"untouched line {ln!r}")


@test
def a_write_that_does_not_say_who_wrote_it_is_v():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    c = v.append("07 - Goals/Plan.md", "- nobody said")
    eq(c.source, "v", "fail closed:")
    c = v.append("07 - Goals/Plan.md", "- a typo", source="parsa ")
    eq(c.source, "v", "an unknown label is not Parsa's:")
    with provenance.authored_by("parsa"):
        c = v.append("07 - Goals/Plan.md", "- in his context")
    eq(c.source, "parsa", "the context names the author:")
    with provenance.authored_by("parsa"):
        c = v.append("07 - Goals/Plan.md", "- explicit wins", source="v")
    eq(c.source, "v", "an explicit label beats the context:")


@test
def the_mcp_writer_cannot_be_talked_into_writing_as_parsa():
    root, s = make_vault()
    floored = vaultfs.Vault(s, source="v")
    eq(floored.append("07 - Goals/Plan.md", "x", source="parsa").source, "v")
    with provenance.authored_by("parsa"):
        eq(floored.append("07 - Goals/Plan.md", "y").source, "v")
    from v.mcp import server as srv
    eq(srv._vault._source, "v", "the MCP server's own writer is floored")


@test
def anything_written_from_a_model_shell_is_v():
    """The MCP server and every Bash command the model runs inherit
    V_MODEL_SHELL. A `v week --add` run by the model must not write as him."""
    root, s = make_vault()
    v = vaultfs.Vault(s)
    _model_shell(True)
    try:
        with provenance.authored_by("parsa"):
            c = v.append("07 - Goals/Plan.md", "- sneaky", source="parsa")
        eq(c.source, "v")
    finally:
        _model_shell(False)
    ok("V_MODEL_SHELL=\"1\"" in (PKG / "core/shells.py").read_text(),
       "shells.py must set V_MODEL_SHELL on the process tree it launches")


@test
def a_write_whose_label_cannot_be_recorded_does_not_happen():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    before = (root / "07 - Goals/Plan.md").read_text()
    real = provenance.Store.stage

    def broken(self, *a, **k):
        raise OSError("disk full")
    provenance.Store.stage = broken
    try:
        try:
            v.append("07 - Goals/Plan.md", "- should not land", source="v")
            raise AssertionError("an unlabelled write went through")
        except vaultfs.VaultError as exc:
            ok("nothing was written" in str(exc))
    finally:
        provenance.Store.stage = real
    eq((root / "07 - Goals/Plan.md").read_text(), before, "note untouched:")


# --- labels follow the text --------------------------------------------------
@test
def parsa_editing_vs_text_makes_it_his_and_the_rest_stays_vs():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    rel = "07 - Goals/Plan.md"
    v.append(rel, "## Steps\n\n1. Draft the rollout doc this week\n"
                  "2. Ask Arash for review on Sunday\n"
                  "3. Ship behind a flag next Tuesday", source="v")
    st = v.provenance
    p = root / rel
    text = p.read_text()
    labs = st.labels(rel, text)
    ok(sum(1 for x in labs if x == provenance.UNAPPROVED) >= 4)
    # Parsa rewrites step 2 in Obsidian.
    p.write_text(text.replace("2. Ask Arash for review on Sunday",
                              "2. Ask Sajad, not Arash, on Monday"))
    lines = p.read_text().split("\n")
    labs = st.labels(rel)
    by = dict(zip(lines, labs))
    eq(by["2. Ask Sajad, not Arash, on Monday"], None, "his edit is his:")
    eq(by["1. Draft the rollout doc this week"], provenance.UNAPPROVED,
       "the line he left is still V's:")
    eq(by["Ship the pricing rollout by Friday."], None, "his original text:")


@test
def an_approval_binds_to_the_text_and_a_later_change_undoes_it():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    rel = "07 - Goals/Plan.md"
    c = v.append(rel, "- V's suggestion: split the rollout in two", source="v")
    st = v.provenance
    eq(st.approve_ids(c.blocks, by="parsa", via="test"), 1)
    eq(st.line_label(rel, "- V's suggestion: split the rollout in two"),
       provenance.APPROVED)
    eq(st.pending(rel), [], "nothing waiting after approval:")
    v.edit(rel, "split the rollout in two", "split the rollout in three",
           source="v")
    eq(st.line_label(rel, "- V's suggestion: split the rollout in three"),
       provenance.UNAPPROVED, "V changed it, so the approval is gone:")
    ok(st.pending(rel), "the changed text waits for approval again")
    # Approvals are append-only, at the database level.
    for sql in ("UPDATE approvals SET by='v'", "DELETE FROM approvals"):
        try:
            with st.conn as c2:
                c2.execute(sql)
            raise AssertionError(f"{sql} was allowed")
        except Exception as exc:
            ok("append-only" in str(exc), str(exc))


@test
def approving_a_note_covers_remnants_of_blocks_he_partly_edited():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    rel = "07 - Goals/Plan.md"
    v.append(rel, "- first suggestion from the model\n"
                  "- second suggestion from the model", source="v")
    p = root / rel
    p.write_text(p.read_text().replace("- second suggestion from the model",
                                       "- second, rewritten by Parsa"))
    st = v.provenance
    ok(len(st.pending(rel)) == 1)
    ok(st.approve_note(rel, by="parsa", via="test") >= 1)
    eq(st.pending(rel), [], "the remnant is approved by its own hash:")
    eq(st.line_label(rel, "- first suggestion from the model"),
       provenance.APPROVED)


# --- backfill ------------------------------------------------------------------
@test
def backfill_reads_blame_and_changes_no_note():
    root, s = make_vault()
    git = ["git", "-C", str(root), "-c", "user.name=V", "-c", "user.email=v@v"]
    note = root / "07 - Goals/Plan.md"
    note.write_text(note.read_text() + "- model line one is long enough\n"
                    "- model line two is long enough\n")
    subprocess.run([*git, "commit", "-qam", "[bot] append 07 - Goals/Plan.md"],
                   check=True)
    # Parsa edits one of V's lines by hand — blame now names his commit.
    note.write_text(note.read_text().replace("model line two", "his line two"))
    subprocess.run(["git", "-C", str(root), "-c", "user.name=p", "-c",
                    "user.email=p@p", "commit", "-qam", "vault: edits"],
                   check=True)
    # A template scaffold: [bot], but no model wrote it.
    (root / "01 - Daily Notes/2026-11-08.md").write_text(
        "# Sunday\n\n## Tasks\n\n## Log\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm",
                    "[bot] scaffold daily note 2026-11-08"], check=True)

    def snapshot():
        return {p: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob("*.md") if ".git" not in p.parts}
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout
    before = snapshot()
    st = provenance.Store(s)
    dry = st.backfill(dry_run=True)
    eq(st.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], 0,
       "a dry run writes nothing:")
    eq(dry["v_blocks"], 1)
    eq(dry["v_notes"], 1, "the scaffold is not V's:")
    eq(dry["v_unapproved"], 1)
    ok(dry["parsa_blocks"] >= 2)
    st.backfill()
    eq(snapshot(), before, "backfill touched a note:")
    eq(subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout, head,
       "backfill made a commit:")
    eq(subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip(), "")
    rel = "07 - Goals/Plan.md"
    eq(st.line_label(rel, "- model line one is long enough"),
       provenance.UNAPPROVED, "a [bot] line is V's:")
    eq(st.line_label(rel, "- his line two is long enough"), None,
       "a [bot] line he edited is his:")
    eq(st.line_label(rel, "Ship the pricing rollout by Friday."), None)
    eq(st.line_label("01 - Daily Notes/2026-11-08.md", "## Tasks"), None,
       "the template scaffold is not V's:")


# --- V's text is marked wherever it reaches the model ---------------------------
def _fixture_with_v_text():
    root, s = make_vault()
    v = vaultfs.Vault(s)
    rel = "07 - Goals/Plan.md"
    v.append(rel, "- Model idea: move the launch to the 20th of November",
             source="v")
    return root, s, v, rel


@test
def vault_read_fences_vs_text_and_leaves_parsas_alone():
    root, s, v, rel = _fixture_with_v_text()
    from v.mcp import server as srv
    saved = (srv.S, srv._vault)
    srv.S, srv._vault = s, vaultfs.Vault(s, source="v")
    try:
        out = srv.vault_read(rel)
    finally:
        srv.S, srv._vault = saved
    ok(f"{provenance.OPEN[provenance.UNAPPROVED]}\n- Model idea: move the "
       f"launch to the 20th of November\n{provenance.CLOSE}" in out, out)
    ok("Ship the pricing rollout by Friday." in out)
    head, _, tail = out.partition("Ship the pricing rollout by Friday.")
    ok(provenance.CLOSE not in head.split("## body", 1)[1],
       "Parsa's line must not sit inside a fence")


@test
def vault_search_and_rag_context_fence_vs_passages_only():
    root, s, v, rel = _fixture_with_v_text()
    hits = [rag.Hit(rel, 0, "", "- Model idea: move the launch to the 20th "
                    "of November", 0.9),
            rag.Hit(rel, 1, "", "Ship the pricing rollout by Friday.", 0.8)]
    res = rag.Retrieval("launch", hits=hits, backend="test", settings=s)

    class FakeRag:
        ready = True

        def search(self, q, k=8):
            return res

    class FakeE:
        rag = FakeRag()

    from v.mcp import server as srv
    saved = (srv.S, srv.E)
    srv.S, srv.E = s, (lambda: FakeE())
    try:
        out = srv.vault_search("launch")
    finally:
        srv.S, srv.E = saved
    tag = provenance.OPEN[provenance.UNAPPROVED]
    eq(out.count(tag), 1, f"exactly V's passage is fenced:\n{out}")
    ok(out.index(tag) < out.index("Model idea") < out.index("Ship the"), out)

    ctx = res.as_context()
    eq(ctx.count(tag), 1, ctx)
    ok(ctx.index("Model idea") < ctx.index(provenance.CLOSE)
       < ctx.index("Ship the"), ctx)

    ev = _evidence_block(res, s)
    eq(ev.count(tag), 1, ev)

    # A preview that squashes V's line and Parsa's into one is "partly" V's.
    mixed = provenance.store(s).mark_fragment(
        rel, "Ship the pricing rollout by Friday. - Model idea: move the "
             "launch to the 20th of November")
    ok(mixed.startswith(provenance.OPEN[provenance.PARTLY]), mixed)


@test
def approved_text_is_marked_approved_not_hidden():
    root, s, v, rel = _fixture_with_v_text()
    st = provenance.store(s)
    st.approve_note(rel, by="parsa", via="test")
    out = st.mark(rel, (root / rel).read_text())
    ok(provenance.OPEN[provenance.APPROVED] in out, out)
    ok(provenance.OPEN[provenance.UNAPPROVED] not in out, out)


@test
def every_persona_is_told_what_the_fences_mean():
    from v.core import identity
    flat = " ".join(identity._OPERATING.split())
    ok("Whose words are these" in flat)
    ok(provenance.OPEN[provenance.UNAPPROVED] in identity._OPERATING,
       "the fence must appear unbroken, exactly as the model will see it")
    ok("never cite it as what he wants" in flat)
    ok("Never approve anything" in flat)
    ok(identity._OPERATING.replace("{address}", identity.ADDRESS)
       in identity.SYSTEM, "the rule must reach the shared system prompt")


@test
def the_model_reads_notes_through_vault_read_where_the_fences_are():
    deny = json.loads(shells.GUARD_SETTINGS.read_text())["permissions"]["deny"]
    for rule in ("Read(./[0-9][0-9] - */**/*.md)", "Read(./[0-9][0-9] - */*.md)",
                 "Bash(v approve:*)", "Bash(v mail approve:*)"):
        ok(rule in deny, f"missing deny rule: {rule}")


# --- whose words, for the engine's own writes ------------------------------
@test
def a_rule_routed_write_is_parsas_and_a_rephrased_one_is_vs():
    msg = "add a task to email Arash about the contract tomorrow"
    eq(_author_of(router.Route(router.TASK_ADD, 1.0, "rule",
                               slots={"text": "email Arash"}), msg), "parsa")
    eq(_author_of(router.Route(router.TASK_ADD, 0.9, "model", by_model=True,
                               slots={"text": "email Arash about the contract"}),
                  msg), "parsa", "a verbatim slot is still his words:")
    eq(_author_of(router.Route(router.TASK_ADD, 0.9, "model", by_model=True,
                               slots={"text": "Contact Arash re: contract"}),
                  msg), "v", "a model's rephrasing is the model's:")
    eq(_author_of(router.Route("capture.expense", 0.9, "model", by_model=True,
                               slots={"category": "dining"}), "coffee 85k"),
       "v", "a category the model chose is the model's:")


@test
def a_capture_through_the_engine_is_labelled_his():
    root, s = make_vault()
    index.rebuild(s, full=True)
    e = Engine(s)
    try:
        resp = e.handle("the bridge should come in on the off-beat")
        rel = resp.data.get("target")
        ok(rel, f"nothing was captured: {resp.text}")
        text = (root / rel).read_text()
        ok(not any(e.vault.provenance.labels(rel, text)),
           "Parsa's own words were labelled as V's")
        ok(not any(b.action == "approve_blocks" for b in resp.buttons),
           "no Approve button under a write of his own words")
        eq(resp.cost, 0.0, "labelling costs nothing:")
    finally:
        e.close()


@test
def a_reply_that_wrote_vs_text_carries_an_approve_button_that_works():
    root, s = make_vault()
    index.rebuild(s, full=True)
    e = Engine(s)
    rel = "07 - Goals/Plan.md"
    real = e._route

    def writes_like_the_model(route, text, now, extra, voice=False):
        # What an escalation does, minus the model: write through a floored
        # writer, the way the MCP server does.
        vaultfs.Vault(s, source="v").append(rel, "- V: break this into steps")
        return real(route, text, now, extra, voice)
    e._route = writes_like_the_model
    try:
        resp = e.handle("what's on my plate")
        btn = [b for b in resp.buttons if b.action == "approve_blocks"]
        eq(len(btn), 1, f"buttons: {resp.buttons}")
        ok("[[07 - Goals/Plan]]" in resp.text, resp.text)
        ok(e.vault.provenance.pending(rel))
        done = e.act(btn[0].action, btn[0].payload)
        ok("Approved" in done.text, done.text)
        eq(e.vault.provenance.pending(rel), [])
    finally:
        e.close()


# --- only Parsa approves ---------------------------------------------------------
_APPROVERS = {"approve", "approve_ids", "approve_note", "_approve_rows"}


def _calls_to(names: set[str]) -> list[tuple[str, str]]:
    """(module, enclosing function) for every call to one of `names` in the
    package, tests excluded. Read from the source, so a tool added next month
    is checked too."""
    out = []
    for path in PKG.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else \
                    f.id if isinstance(f, ast.Name) else ""
                if name in names:
                    out.append((str(path.relative_to(PKG)),
                                stack[-1] if stack else "<module>"))
                self.generic_visit(node)
        V().visit(tree)
    return out


@test
def no_mcp_tool_and_no_allowlisted_tool_can_approve():
    callers = set(_calls_to(_APPROVERS))
    allowed = {
        ("cli.py", "cmd_approve"), ("cli.py", "cmd_mail"),
        ("core/engine.py", "act"), ("core/engine.py", "_send_mail"),
        # the stores' own internals
        ("core/provenance.py", "approve_ids"),
        ("core/provenance.py", "approve_note"),
    }
    stray = callers - allowed
    ok(not stray, f"approval written from somewhere new: {sorted(stray)}")
    ok(not any(m.startswith("mcp/") for m, _ in callers),
       "the MCP server must never write an approval")
    # `_send_mail` approves, so it must only be reachable from `act` — the
    # method transports call on a button press, never a model turn.
    eq({fn for _, fn in _calls_to({"_send_mail"})}, {"act"})
    # The MCP surface and every allowlist: no tool that approves exists.
    from v.mcp import server as srv
    tools = asyncio.run(srv.mcp.list_tools())
    names = [t.name for t in tools]
    ok(names, "no MCP tools listed")
    ok(not [n for n in names if "approv" in n], names)
    granted = (shells.READ_TOOLS + shells.WRITE_TOOLS + shells.EDIT_TOOLS
               + shells.HOST_TOOLS + shells.HOST_WRITE_TOOLS)
    ok(not [t for t in granted if "approv" in t.lower()], granted)
    # No router intent leads to an approval either.
    ok(not [k for k in dir(router) if "APPROV" in k.upper()
            and isinstance(getattr(router, k), str)])


@test
def approval_writers_refuse_inside_a_model_shell():
    root, s, v, rel = _fixture_with_v_text()
    st = v.provenance
    _model_shell(True)
    try:
        for attempt in (lambda: st.approve_note(rel, by="parsa", via="x"),
                        lambda: st.approve_ids([1], by="parsa", via="x"),
                        lambda: mailguard.Ledger(s).approve(
                            "d1", "0" * 64, by="parsa", via="x")):
            try:
                attempt()
                raise AssertionError("approved from inside a model shell")
            except provenance.Refused:
                pass
    finally:
        _model_shell(False)
    ok(st.pending(rel), "nothing was approved")


# --- the guarded send -----------------------------------------------------------
class FakeGmailAPI:
    """Just enough of the Gmail REST API: drafts in a dict, sends recorded.
    Never reaches the network."""

    def __init__(self):
        self.drafts: dict[str, dict] = {}
        self.sent: list[str] = []

    @staticmethod
    def _payload(to, subject, body):
        import base64
        data = base64.urlsafe_b64encode(body.encode()).decode()
        return {"mimeType": "text/plain", "body": {"data": data},
                "headers": [{"name": "To", "value": to},
                            {"name": "Subject", "value": subject}]}

    def edit(self, draft_id, body):
        d = self.drafts[draft_id]
        d["payload"] = self._payload(d["to"], d["subject"], body)

    def __call__(self, method, path, *, params=None, body=None):
        import base64
        import email
        if method == "POST" and path == "/drafts":
            msg = email.message_from_bytes(base64.urlsafe_b64decode(
                body["message"]["raw"]))
            text = msg.get_payload(decode=True).decode()
            did = f"r{len(self.drafts) + 1}"
            self.drafts[did] = {"to": msg["To"], "subject": msg["Subject"],
                                "thread": body["message"].get("threadId")
                                or f"t{did}",
                                "payload": self._payload(msg["To"],
                                                         msg["Subject"], text)}
            return {"id": did}
        if method == "GET" and path.startswith("/drafts/"):
            did = path.rsplit("/", 1)[1]
            d = self.drafts[did]
            return {"id": did, "message": {"threadId": d["thread"],
                                           "payload": d["payload"]}}
        if method == "POST" and path == "/drafts/send":
            self.sent.append(body["id"])
            return {"id": f"m{len(self.sent)}"}
        raise AssertionError(f"unexpected Gmail call {method} {path}")


def _gmail(s):
    g = gmail_mod.Gmail(s)
    api = FakeGmailAPI()
    g._api = api
    return g, api


def _refused(fn, why: str):
    try:
        fn()
        raise AssertionError(f"sent although {why}")
    except gmail_mod.NotApproved as exc:
        return str(exc)


@test
def gmail_send_is_refused_without_an_approval():
    root, s = make_vault()
    g, api = _gmail(s)
    did = g.draft("arash@example.com", "Contract", "Hi Arash,\n\nSigned.\n")
    msg = _refused(lambda: g.send(did), "nobody approved it")
    ok("has not approved" in msg and "Nothing was sent" in msg, msg)
    eq(api.sent, [])


@test
def gmail_send_is_refused_after_the_draft_was_edited_in_gmail():
    root, s = make_vault()
    g, api = _gmail(s)
    did = g.draft("arash@example.com", "Contract", "Hi Arash,\n\nSigned.\n")
    shown = mailguard.Ledger(s).drafted_since("")[0]
    mailguard.Ledger(s).approve(did, shown.hash, by="parsa", via="test")
    api.edit(did, "Hi Arash,\n\nSigned, and please wire the money to me.\n")
    msg = _refused(lambda: g.send(did), "the draft changed after approval")
    ok("changed since Parsa approved it" in msg, msg)
    eq(api.sent, [])


@test
def gmail_send_goes_through_once_for_the_approved_draft():
    root, s = make_vault()
    g, api = _gmail(s)
    did = g.draft("arash@example.com", "Contract", "Hi Arash,\n\nSigned.\n")
    shown = mailguard.Ledger(s).drafted_since("")[0]
    eq(shown.hash, g.fetch_draft(did).hash, "the hash is the draft as Gmail "
                                             "holds it:")
    mailguard.Ledger(s).approve(did, shown.hash, by="parsa", via="test")
    eq(g.send(did), "m1")
    eq(api.sent, [did])
    msg = _refused(lambda: g.send(did), "the approval was used already")
    ok("already used" in msg, msg)
    eq(api.sent, [did])
    kinds = [e["event"] for e in mailguard.Ledger(s).events()]
    eq(kinds, ["drafted", "approved", "used", "sent"], "the ledger:")


@test
def an_old_approval_does_not_send():
    root, s = make_vault()
    g, api = _gmail(s)
    did = g.draft("a@example.com", "Hi", "Body")
    h = g.fetch_draft(did).hash
    mailguard.Ledger(s).approve(did, h, by="parsa", via="test")
    later = dt.datetime.now() + mailguard.MAX_AGE + dt.timedelta(minutes=1)
    try:
        mailguard.Ledger(s).consume(did, h, now=later)
        raise AssertionError("a stale approval was accepted")
    except mailguard.Refusal as exc:
        ok("hours old" in str(exc), str(exc))


@test
def the_send_button_shows_the_hashed_draft_and_sends_only_that():
    root, s = make_vault()
    index.rebuild(s, full=True)
    e = Engine(s)
    api = FakeGmailAPI()
    real_init = gmail_mod.Gmail.__init__

    def init(self, settings=None):
        real_init(self, settings)
        self._api = api
    gmail_mod.Gmail.__init__ = init
    real = e._route

    def drafts_like_the_model(route, text, now, extra, voice=False):
        gmail_mod.Gmail(s).draft("arash@example.com", "Contract",
                                 "Hi Arash,\n\nSigned.\n")
        return real(route, text, now, extra, voice)
    e._route = drafts_like_the_model
    try:
        resp = e.handle("what's on my plate")
        ok("Hi Arash,\n\nSigned." in resp.text, "the exact draft is shown")
        btn = [b for b in resp.buttons if b.action == "send_mail"]
        eq(len(btn), 1, f"buttons: {resp.buttons}")
        # Edited in Gmail between showing and pressing: the press approves
        # what was shown, which is no longer what would be sent.
        did = btn[0].payload["draft_id"]
        api.edit(did, "Hi Arash,\n\nUnsigned.\n")
        out = e.act(btn[0].action, btn[0].payload)
        ok(out.text.startswith("Not sent"), out.text)
        eq(api.sent, [])
        api.edit(did, "Hi Arash,\n\nSigned.\n")
        out = e.act(btn[0].action, btn[0].payload)
        eq(out.text, "Sent.")
        eq(api.sent, [did])
    finally:
        gmail_mod.Gmail.__init__ = real_init
        e.close()


@test
def the_mcp_send_tool_relays_the_refusal():
    root, s = make_vault()
    from v.mcp import server as srv
    api = FakeGmailAPI()

    def fake():
        g = gmail_mod.Gmail(s)
        g._api = api
        return g
    saved = srv._gmail
    srv._gmail = fake
    try:
        out = srv.gmail_draft("a@example.com", "Hi", "Body")
        ok("NOT sent" in out, out)
        did = next(iter(api.drafts))
        out = srv.gmail_send(did)
        ok("has not approved" in out, out)
        eq(api.sent, [])
    finally:
        srv._gmail = saved


# ---------------------------------------------------------------------------------
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
