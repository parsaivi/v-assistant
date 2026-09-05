# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What This Is

**V** — a local-first assistant over an Obsidian Markdown vault. It answers
from the vault, writes back to it, and reaches a model only when the free path
genuinely cannot answer. ~24,500 lines of Python, 302 tests, no runtime
dependencies in the base install.

The vault is **data, not part of this repo**. Parsa's is at
`~/Documents/Brain`. Until 2026-08-22 this package lived *inside* that vault at
`Brain/assistant/` and was imported as `assistant.*`; it is now `~/projects/v`,
imported as `v.*`. Anything referring to the old layout is out of date.

## Layout

```
src/v/
  cli.py        34 subcommands + the REPL; entry point for the `v` console script
  core/         48 modules — see the table in README.md
  core/hud/     the desktop surface's page, stylesheet and script (package data)
  mcp/          the MCP server (40 tools) + the privacy guard
  tests/        472 tests, plain asserts, no pytest
android/        Kotlin + Compose client for the phone transport, plus two
                Glance home-screen widgets over `GET /v1/glance`
systemd/        v.service (telegram + scheduler), v-serve.service (phone)
docs/           the voice-interface design brief
```

| | |
|---|---|
| vault | `$V_VAULT` → `~/.config/v/config.json` → nearest `.obsidian/` above cwd |
| state | `~/.local/state/v` — SQLite index, memory db, PKI keys, run logs |
| models | `~/.cache/v/models` — embedding + speech weights (~12 GB) |

## Running Things

```bash
./.venv/bin/v repl                      # or just `v` if the venv is on PATH
./.venv/bin/v hud                       # the desktop surface, on 127.0.0.1
./.venv/bin/python -m v.tests.test_core  # one suite at a time; no runner
```

There is **no pytest, no linter, no build step.** Each test file is executable
and prints its own summary. The full suite:

```
test_core 90 · test_shell 33 · test_mcp 20 · test_tasks 51 · test_memory 31
test_briefing 15 · test_telegram 31 · test_tts 12 · test_httpapi 39
test_traits 11 · test_agents 19 · test_graph 95 ·
test_ui_animation 25 · test_hud 50                           (522 total)
```

Two tests are sensitive to the pinned clock — `config.py` forces
`TZ=Asia/Tehran`, which is often several hours off the machine's ambient zone.
Never write a test that builds a timestamp from `now()` and compares it to a
hardcoded hour; derive it from the instant under test.

## Architecture, and Where It's Going

The current shape and the target are documented in an architecture review; the
short version of what matters when editing:

**The load-bearing seam** is `Engine.handle(text) -> Response`. Every surface
(CLI, Telegram, HTTP/phone, MCP) is a thin shim over it. Do not let transport
concepts leak into `core/`.

**Cost discipline is the point.** Structured queries, FTS, RAG, capture,
reminders and nudges are SQL and numpy — `Response.cost` is `0.0` and tests
assert it. The only metered path is escalation to a shell, reached last, and a
failed escalation must degrade to the free path rather than replace it.

**Personas are agents, not costumes.** `persona.py` defines V, Friday and
EDITH; each owns a memory scope (`memories.agent`), a surface list, a model
floor and a write posture. Three rules that are asserted in `test_agents.py`
and easy to break by accident:

- A memory write never leaves its own scope. EDITH *reads* V's and Friday's;
  nothing writes across a boundary, including supersession, which is a write.
- A surface gate guards answering, not just switching. If the config names an
  agent that cannot be reached from this surface, `Engine._agent_for` falls
  back to V and says so. **A new surface is a new entry in
  `persona.SURFACES`, never a reuse of `TUI`** — `TUI` means a keyboard on
  this machine, and it is the only thing standing between EDITH's edit tools
  and anything that can open a socket. `v hud` is loopback-only and still gets
  its own `HUD`; `test_hud` asserts EDITH stays out of it.
- The agent reaches the MCP subprocess through `V_AGENT` in its environment.
  That is the seam where isolation is easiest to lose, because `mcp/server.py`
  is a different process and cannot see `engine.persona`.

`writes=DIRECT` (EDITH) skips the show-and-wait step for **vault** writes only
— they are append-only and `v undo` reverts them. It never extends to
`gmail_send` or anything else that leaves the vault.

`edits=True` (EDITH, and nobody else) adds `shells.EDIT_TOOLS` — `vault_edit`,
`vault_set_meta`, `vault_rewrite` — the only tools in the system that can
change text a human already wrote. Two gates, and the order matters: the
allowlist keeps them invisible to every other agent, and `server._may_edit()`
re-checks in case the MCP server was launched directly. The first is the real
protection; the second is what catches the day somebody changes the first.
`vault_edit` matches one exact, unique string and refuses both a miss and an
ambiguous hit — a model editing from a remembered version of a note is the
failure this is built against.

**`vaultfs.py` is the only writer.** Append-only, per-path git commit, `undo`
is `git revert`. Never write to the vault by another route.

**Known structural debt** (being worked through in order):
1. A capability is declared in five places — `engine._dispatch`, `mcp/server.py`,
   `cli.py`'s parser, `shells.py`'s allowlist, `identity.CAPABILITIES`. Adding
   one means editing all five, and only the MCP surface gets the privacy guard.
   A capability registry is the fix and the next major step.
2. `Engine` is a god object (1,449 lines, 48 methods) holding turn state, which
   is why `httpapi._Bridge` funnels every request onto one thread.
3. `config.Settings` is a 110-field mutable singleton imported by 39 modules.
4. Thirteen `core/` modules are specific to Parsa's vault (`work`, `quera`,
   `brag`, `gcal`, `contacts`, `mapsearch`, …) and eventually become plugins.

Prefer changes that move toward those fixes over ones that entrench the
current shape.

## House Style

The codebase documents **why**, not what — including the alternative that was
rejected and the bug that motivated the choice. Match that. A docstring that
says "parses frontmatter" adds nothing; one that says why it isn't
`python-frontmatter` is why this code is maintainable.

Stdlib-first. Every dependency was argued for: no vector DB (5,400 chunks is
one matmul), no APScheduler (catch-up on boot is the behaviour that matters),
no python-telegram-bot (the Bot API is JSON over HTTPS).

Persian text, ZWNJ and Jalali dates appear throughout the vault. Don't
normalise them.

## Privacy

`Settings.private_folders` and the deny rules in the **vault's**
`.claude/settings.json` are two halves of one guard, written in different
languages; `test_mcp` asserts they agree. If you add a private folder, change
both. Private notes stay indexed and searchable by the local $0 path — only
the MCP surface is blind to them.
