# V

The assistant over this vault. Local by default: structured queries, semantic
search, capture, reminders and nudges are SQL and numpy, they cost nothing, and
a test asserts it. A model is reached for only when the answer genuinely needs
one — and V says so, in the tail of every response, with what it cost.

```
you ──► Engine.handle(text) ──► Response(text, citations, buttons, cost)
```

Nothing in `core/` knows what Telegram is except `telegram.py` itself — the
shim over `handle()` that renders `Response.buttons` as an inline keyboard.
See **Telegram** below.

## Quick start

V is a package now, and it lives outside the vault it reads. Install it once,
tell it where the notes are, and every surface uses the same `v` command.

```bash
git clone <this repo> ~/projects/v && cd ~/projects/v
python3 -m venv .venv && .venv/bin/pip install -e ".[all]"

# Where the notes are. Env wins; the file is the durable answer.
mkdir -p ~/.config/v
echo '{"vault": "/path/to/your/vault"}' > ~/.config/v/config.json
```

```bash
v index --embed          # build both indexes (once)
v repl                   # talk to it
v hud                    # the day on one screen — see The HUD below
v brief                  # the morning brief, on demand
v briefing --status      # the standing 10:00 push
v brag                   # entries drafted from closed work
v tasks                  # ranked; --backlog --overdue --done
v calendar --days 7      # --add, --sync, --import-ical
v memory                 # what it has learned from you
v docs                   # regenerate 19 - Assistant/V/
v telegram               # the bot — see Telegram below
v phone enrol pixel      # issue the phone's certificate
v serve                  # the phone transport — see The phone
```

```bash
python3 -m v.tests.test_core       # 90 · test_shell 33 · test_mcp 20
python3 -m v.tests.test_tasks      # 51 · test_memory 31
python3 -m v.tests.test_briefing   # 15 · test_telegram 31 · test_tts 12
python3 -m v.tests.test_httpapi    # 39 · test_ui_animation 25        (347 total)
```

### Where things live

Nothing V owns is written into the vault any more — the notes folder holds
notes, and that is all.

| | |
|---|---|
| package | `~/projects/v` (`src/v/`, installed with `pip install -e .`) |
| vault | `$V_VAULT`, else `~/.config/v/config.json`, else the nearest `.obsidian/` above the cwd |
| state | `$V_STATE`, else `~/.local/state/v` — index, memory db, keys, logs |
| models | `$V_MODELS`, else `~/.cache/v/models` — the embedding and STT weights |

## What's here

| Module | Job | Costs |
|---|---|---|
| `identity.py` | who V is, its goals, its capability manifest | — |
| `config.py` | paths, folders, thresholds, tiers, privacy | — |
| `notes.py` | frontmatter parse/render, wikilinks, sections | $0 |
| `vaultfs.py` | **the only writer.** append-only, git-committed, undoable | $0 |
| `index.py` | SQLite mirror of the vault + FTS5 + chunking + staleness | $0 |
| `schema.py` | CLAUDE.md frontmatter schemas + validator | $0 |
| `query.py` | structured queries — the free half of retrieval | $0 |
| `tasks.py` | **checkbox tasks** — parse, classify, rank, tick | $0 |
| `calendar.py` | **the vault calendar**, and the only source of truth for it | $0 |
| `gcal.py` | **live Google Calendar** — his and others', read and write | $0 |
| `gmail.py` | **live Gmail** — search, read, draft; send only on confirmation | $0 |
| `embed.py` | local embeddings: bge-m3, or a no-download tf-idf fallback | $0 |
| `rag.py` | hybrid dense+keyword search, numpy, no vector DB | $0 |
| `router.py` | rules-based intent routing + learning from corrections | $0 |
| `memory.py` | **what V has learned from you**, with decay and supersession | $0 |
| `journal.py` | V's own notes in `19 - Assistant/V/` | $0 |
| `deferred.py` | **questions the plan window refused**, asked again when it opens | $0 |
| `session.py` | **one conversation**, its thread and its written recap | $0 |
| `capture.py` | world → vault, with confidence gating | $0 |
| `reminders.py` | SQLite reminders, natural-time parsing, catch-up | $0 |
| `scheduler.py` | the proactive loop, with catch-up on boot | $0 |
| `briefing.py` | **the standing 10:00 push** — cron entry, channels, thin-signal guard | $0 |
| `brag.py` | **brag entries drafted as work closes**, with a substance gate | $0 |
| `templates.py` | fills `05 - Templates/` so V's notes match Obsidian's | $0 |
| `nudges.py` | 15 vault-state checks | $0 |
| `stt.py` | faster-whisper, local | $0 |
| `tts.py` | the voice going the other way — the persona's, with Piper under it | ~$0.002/reply |
| `persona.py` | **which agent is answering** — V, Friday or EDITH: voice, memory scope, reach, model floor, write posture | $0 |
| `traits.py` | the half of that character it **learns** — habits of speech, capped and outranked by the persona's fixed core | $0 |
| `work.py` | glab → schema-valid notes (template fill, not reasoning) | $0 |
| `quera.py` | Quera (course deadlines, cookie auth) → schema-valid notes | $0 |
| `shells.py` | the model seam: tiering, cost, the Claude Code driver | tiered |
| `telegram.py` | **the bot** — owner-only, archive groups, buttons, proactive push, voice | $0 |
| `pki.py` | the private CA — one certificate per device, never transmitted | $0 |
| `httpapi.py` | **the phone** — mutual TLS, chat, voice both ways, proactive SSE, the home-screen snapshot | $0 |
| `files.py` | attachments in and out — where they land, and the outbox between processes | $0 |
| `ui.py` | the design tokens: colour, glyphs, markdown, cost lines, the aperture | $0 |
| `tui.py` | **the window** — frame, panes, palette, key handling, ambient state | $0 |
| `hud.py` | **the desktop surface** — the whole day on one screen, over loopback | $0 |
| `engine.py` | the single entry point | — |

## The terminal

`ui.py` is presentation and nothing else — no vault, no engine, stdlib only.
`Response.text` is markdown, and `ui.markdown()` is what turns it into
something worth reading: headings, bullets with hanging indents, dim code and
quotes, tables measured in rendered columns rather than characters so the
Persian ones stop coming out ragged. A spinner turns while the engine works,
and answers arrive line by line rather than all at once.

Colour switches itself off when stdout is not a terminal, so `cli status >
file` still writes plain text; `--plain` or `NO_COLOR=1` forces it, and
`V_NO_ANIM=1` keeps the colour but drops the motion.

The fallback prompt imports `readline`, which is what gives it ←/→, ↑/↓
history across sessions, Ctrl-A/E/W and bracketed paste. The prompt wraps its
escape codes in `\001`/`\002` so readline knows they take no space — without
that its cursor column drifts and left-arrow walks over the prompt. The window
implements the same editing itself, because it reads keys directly.

The scrolling prompt never captures the mouse, so selecting output works
exactly as your terminal normally does. The window does capture it, and has to
— see below. `/copy` puts the last answer on the clipboard as markdown when
you'd rather not select anything at all.

That split is also the point of doing this now: when the voice interface
lands, it consumes `Response` and never touches `ui.py`.

### What the screen is saying

The design is at `docs/voice-interface-design-brief/`. Four of its
rules are load-bearing here, and all four are in `ui.py` rather than left to
each call site:

**`✦` is V's voice and nothing else's.** It prefixes everything V says and is
never a logo — the banner uses `◎`, the aperture reduced to one cell. Its
colour is the whole state vocabulary: violet speaking, amber unsure, red
refused, grey degraded. That is why a refusal needs no warning box and a
degraded answer needs no banner. `_voice_state()` reads it off the `Response`,
so a fallback looks like a fallback whichever path produced it.

**Red is for refusals, never for errors.** A refusal is V holding a rule you
gave him; an error is a fault. Colouring them alike teaches you to read one as
the other, and the one you learn to ignore is the refusal. Errors are amber.

**The cost line is under every answer, always.**

```
  ✦ You have not trained in eleven days, sir.
    ◈ nudges:gym-gap · ⟡ 2 notes · 0.00s · $0.0000
```

`◈` route or model · `⟡` notes read · elapsed · cost · `◉` confidence. A local
answer prints an exact `$0.0000` in green rather than the word "free", because
the number is the claim: nothing was billed *and* nothing left the machine. A
model run prints `≈$0.0507 allowance`, which is the honest form on a
subscription — see the cost model below.

**Persian is isolated per run, not per line.** Each Persian run is wrapped in
bidi isolates and the surrounding line stays left-to-right. Isolating the
whole line instead is correct by the standard and awful in practice: it sets
the paragraph direction to RTL, so `مرور کنم (overdue, due 2026-07-26)` — a
task with its metadata after it — renders with the date thrown to the far
left, and every carried task appeared to begin with its own timestamp. Per-run
isolation keeps the bullets, the dashes and the dates where they were written
while the Persian inside each run still shapes and orders correctly. The
isolate characters are counted as zero-width, or every Persian column comes
out one cell short per run.

### The window

`v repl` opens the terminal from §03 of the design: a
titled window, a transcript pane, a side pane that does not scroll with the
log, and the command palette floating above the prompt.

```
╭─ ●●● v — repl — 110×30                       ◈ claude-code · ● online · $0.20 ─╮
│                                                 │ L I S T E N I N G            │
│                                                 │ buffering                    │
│                                                 │        .:::::::··...:•:      │
│                                                 │      .··:•••••:::·..·:::::   │
│                                                 │      ..·:•••••:::·..·:::·    │
│   ✦ Today (Aug 2), sir.                         │     . .·:•●•::•::·.·:•:·     │
│     • 12:00–12:15 دیلی قیمت‌گذاری                │       ..·:::·:•••:::•:       │
│     • 15:30–16:00 1on1 with Arash               │        ····:··•••:••:        │
│     ◈ sonnet-5 · 20.25s · ≈$0.2007              │          :·:··••••:          │
│                                                 │ ▁▁▂▃▂▃▅▆▄▄▆▆▄▄▂▃▃▃▂▂▁▁▁▁▂▂▂▁ │
│                                                 │ Not sent yet.                │
│                                                 │                              │
│                                                 │ S E S S I O N                │
│                                                 │ turns                      3 │
├─────────────────────────────────────────────────┴──────────────────────────────┤
│ › ask, or / for commands                                                     ⏎ │
╰────────────────────────────────────────────────────────────────────────────────╯
```

`core/tui.py` owns layout, input and the frame loop; `ui.py` still owns every
colour and glyph, and `Entry` stores content rather than rendered lines so a
resize re-wraps the whole log instead of guessing where the escapes were.

**The ambient vessel is in the pane, and the vessel is the state.**
`ui.aperture()` implies one stable silhouette with a five-weight particle
field (`. · : • ●`): flat roof, short outward shoulders, long inward taper,
and a grounded basin. Idle convects around a wandering ember; wake flashes and
throws one ring out through the shoulders; listening answers in crossing bands
over a sideways drift; thinking folds rings inward and turns one arm per tool
in flight; speaking radiates coherent wavefronts; hard mute settles into cold,
motionless dust. Geometry never changes, so the states feel like different
behavior in one living material rather than unrelated spinners.

The particles *travel* rather than blink, and that is one function: `_flow()`
samples a single fixed noise lattice at a moving, fractional point and
smoothsteps between its corners. Re-rolling noise per cell per frame — the
obvious way — gives every particle an independent life, which at any frame
rate reads as static; sliding one field under the character grid makes the eye
track particles across the vessel instead. Each state advects it in its own
direction, in *character* cells rather than vessel coordinates, or the drift
would visibly speed up as the V narrows toward the basin.

`Tui.ambient()` *reads* the state rather than being told it, from things that
are already true: `thinking` while a turn is in flight, `speaking` for as long
as the answer takes to read, `wake` on the first keystroke after silence,
`listening` while a line is being typed, `muted` after `/mute`, and `idle` the
rest of the time — which is most of it. Nothing has to remember to clear a
flag, which is how a light and the program behind it drift apart.

There is no microphone on this surface and nothing here pretends there is one.
The waveform's amplitude is your typing speed, decayed; the spec forbids
replaying a canned level and calling it a signal, so the bars are driven by the
only real input a terminal has. `/ambient` walks all six states in order if you
want to see them without waiting for them.

**The aperture runs at 30fps, and the transcript is the reason it can.** A
frame used to mean the whole screen — every entry re-wrapped through
`ui.markdown()` and 3.4KB of escapes written — so the frame rate was pinned to
the cost of the most expensive thing in the window, and the vessel got eight
frames a second. `Tui._repaint_stage()` writes only the aperture's rectangle,
by absolute coordinate, from measurements the last full frame left behind:
543 bytes, a fifth of the work, and nothing outside the pane is touched. It
returns `False` rather than guess when there has been no full frame yet, when
the window has been resized under it, or when the pane is too short to hold a
stage at all — and then the loop pays for a whole frame. The spinner during a
turn runs both clocks: the braille wheel at 12fps, because it lives in the
transcript and costs a full frame to move, and the aperture at 30 in between.

`V_NO_ANIM` is reduced motion: every loop stops and the frame clock stops with
it, but the colour and the size still change, because colour is information and
motion is not. Muted gets no clock either — a state no software path can
produce has to look like one.

The caret is the terminal's own, moved into place at the end of each frame and
shown only then: painting a whole screen with a visible cursor drags it along
every line as it lands, and never showing it at all leaves you typing into a
field with nothing marking your place. A line longer than the field scrolls
sideways and the window is remembered between frames — recomputing it from the
end of the buffer put the caret under the wrong character the moment you
pressed left-arrow.

**It takes the alternate screen, and the mouse with it.** What is lost to the
alternate screen is your terminal's scrollback: PgUp/PgDn scroll the transcript
here instead. `repl --plain-repl` gives back the old scrolling prompt, and so
does a pipe, `--plain`, a dumb terminal, or a platform with no `termios` —
`tui.usable()` decides, and the fallback is the same code path it always was.

The mouse was left alone at first, on the reasoning that not reporting it kept
the terminal's own select-to-copy working. It did not, for two reasons that
only appear once the window is really drawn:

- **The wheel arrives as arrow keys.** Alternate scroll mode (`?1007`) is on by
  default in VTE and most of its relatives, and on the alternate screen it
  translates the wheel into `\x1b[A`/`\x1b[B` — on the assumption the program
  up there is `less`. Scrolling therefore walked the command history, and no
  amount of care downstream can help: those bytes are identical to the ones the
  keyboard sends. `_enter()` turns the translation off (saving the mode with
  `?1007s` and restoring it with `?1007r`, since it outlives the process) and
  reads the wheel as a wheel.
- **A native drag copies the frame.** The selection is a rectangle of *screen*,
  and the screen here is a box: every line comes back carrying a `│`, a strip
  of side pane and the padding between them.

So the window reports the mouse (`?1002` for press/release/drag, `?1006` for
SGR — the X10 encoding cannot express a column past 223, and this window is
routinely wider). The wheel scrolls the transcript three lines a notch, and a
drag over the transcript selects text by *pane cell*, lights it with reverse
video, and copies it on release with the frame stripped out — clipboard and
PRIMARY both, so middle-click paste survives. Shift-drag is still the
terminal's own selection, which every terminal that reports the mouse leaves
alone, and `/mouse` hands it back entirely.

Six bugs are worth recording because each one looked like something else:

- **`select()` with buffered `sys.stdin` goes deaf after one character.**
  Reading one character pulls the whole burst out of the kernel and parks the
  rest in Python's buffer, where `select` cannot see it. Typing `/tasks⏎` fast
  enough registered the `/` and dropped the rest. Input reads the raw
  descriptor and decodes incrementally — a Persian character is two or three
  bytes and a read can land inside one.
- **A frame one column too wide does not look like an off-by-one.** The row
  wraps, and the window appears to shift two columns right and draw itself
  twice. The arithmetic is written out in `_split`.
- **Never draw from a signal handler.** `SIGWINCH` runs on whichever thread is
  executing, so redrawing there re-enters the paint lock the interrupted
  thread already holds. Resize sets a flag; the loop picks it up.
- **`redirect_stdout` is global.** The command handlers are run under it so
  their views can be captured into a pane — and the window's own frames went
  into the capture buffer and were replayed *inside* that pane, nesting the
  whole window one frame deep in itself. The window writes to `sys.__stdout__`.
- **One character missing from a regex types the mouse into the prompt.** The
  key tokenizer's CSI pattern allowed `[0-9;?]` between the `[` and the final
  letter, and an SGR mouse report is `\x1b[<0;40;7M`. The `<` did not match, so
  the sequence was never recognised as one key: the Esc came back alone and the
  digits and semicolons after it were typed into the input line.
- **A circle tested cell by cell comes out with holes in the top and bottom.**
  One row covers two columns' worth of distance, so the band a radius test
  accepts is thinner than the gap between the rows sampling it. Every ring is
  walked parametrically instead, and its glyph is picked off the tangent —
  which is also what makes a nine-row circle read as a circle.

### Slash is a hint, not a language

`COMMANDS` in `cli.py` carries three columns, and the third is a contract: a
command may not exist unless V already understands it spoken.

```
/why      the route, cost and notes behind the last answer   “why did you say that”
/quiet    no unprompted messages until tomorrow              “leave me alone”
/local    local answers only until further notice            “stay offline”
```

`tui.WINDOW_COMMANDS` adds `/mute` and `/ambient` on top of that table, and
only inside the window — the scrolling prompt has no aperture to mute and no
pane to walk. They are handled in `Tui._window_command` before anything reaches
the engine, and they obey the same contract.

A bare `/` prints the table, and so does Tab — readline's own match list is a
bare column of names, which is the one thing this table must not be, so the
completion display hook is replaced. Taking the terminal into raw mode would
give a nicer live palette and cost select-to-copy; it is not worth it.

`/why` opens one answer: the route it took with a sentence on *why* each step
was necessary, what it cost, and which notes it read. Only the last answer,
reachable only from it — no home, no history, nothing to browse. Everything
durable is already a note, and a browsable log of past answers is the
dashboard this project decided not to build.

`/quiet` and `/local` are written to `state/surface.json` rather than held in
the process, because the thing they govern — proactive messages — is decided
by the scheduler, which is a different process. A quiet switch that only
quiets the window you typed it into is not a quiet switch. Scheduler output is
rendered as an interruption, marked as one:

```
  ┃ ✦ Thursday's the last dentist slot this month.
  nudges · unprompted · /quiet to stop
```

### Deliberately not built

The design specifies five surfaces. The ambient device (wake word, always-on
mic, the aperture as the only light in the room), mobile and web are all
separate builds and are not here.

Two smaller deviations, both because a terminal is not a browser: body text
stays the terminal's default foreground rather than the design's `#D6D6E2`,
which would be unreadable on a light background this REPL still has to work
on; and the state-card headers are not letter-spaced, because `LOW CONFIDENCE
· THE TRANSCRIPT IS WRONG` at one space per character runs past eighty
columns.

`PENDING WRITES` shows recent captures with the confidence they were written
at, marked `committed`. Nothing is held: a capture still lands in the note
immediately, by rule 3, so the amber there is a prompt to correct it in a
sentence rather than a queue waiting on you.

The design says a wait over four seconds should print the route step by step so
the wait is legible instead of blank, and it now does — see **The run, as it
happens** below. It was the last thing in the spec still missing here, for the
same reason it was easy to miss: `Spinner.step()` and the aperture's tool arms
were both written for it, and a turn was one opaque `subprocess.run` that had
nothing to tell them until it was over.

```
    ◈ vault_search · UUID primary keys decision
    ◈ vault_search · UUID primary key sequential ID Pricing database schema
    ◈ writing the answer
  ✦ Your team decided on UUID primary keys rather than auto-increment…
```

## Sessions

A run of the REPL is one conversation. It stays open until you end it — `/new`,
or quitting — and while it is open every escalation resumes the same model
thread, so "give me Arash's calendar today" followed by "book him 15:30" is one
exchange rather than two strangers. Before this existed only `_chat` continued
a thread and every other path opened a fresh one, which is precisely how the
second of those two messages came back asking who "him" was.

The cold-thread expiry (`shell_session_minutes`) still governs one-off asks, but
does not apply inside a session: a long pause is not you saying you were
finished. What it costs is honest — resuming re-sends the whole history every
turn, so a session that has run all day is being re-read all day. `/new` ends
one and starts the next; that is the lever when the subject changes.

**When a session ends, it is written to `19 - Assistant/V/Logs/<date>.md` under
`## Sessions`** — every turn, both halves, verbatim. No model is asked to
summarise it. That is the whole design: a recap that costs allowance is one you
stop being able to afford, and it would fail exactly when the model is down —
which is one of the times you most want to read back what happened. So the
recap *is* the output, built with string formatting. Bodies are markdown-quoted
so a `#` in something you typed cannot forge a heading in the log.

Turns are recorded to `state/sessions/<id>.jsonl` as they happen rather than at
the end, which is what makes the recap survive the run that produced it. A
clean exit, Ctrl-D, two Ctrl-Cs and SIGTERM all write it (the last via
`atexit`); `kill -9` can be caught by nothing, so the next run notices a
session marked open under a dead pid and files it then — under the day it
happened, not the day it was found.

## The rules this code enforces

1. **Append-only.** `rewrite()` raises unless `allow_rewrite=True`, which only
   an explicit confirmation — or V's own generated pages — set.
2. **Every write is a `[bot]` git commit.** `git log --grep='\[bot\]'` is a
   complete audit trail, and `undo` is `git revert`. Which is why V commits
   whatever you left uncommitted *before* it starts, under your name and
   without the prefix: its own commits are path-limited, so appending to a
   note you had edited in Obsidian and not committed would fold your work into
   a `[bot]` commit — inside the blast radius of `undo`, and inside the audit
   trail as something the assistant did. The startup commit covers the CLI;
   `_preserve` does the same per note for the long-lived MCP server, which
   never gets a second startup. The commit is path-limited to the note tree:
   V's own source lives outside the vault now, so the only thing the startup
   commit can ever sweep up is notes.
3. **Low confidence files, then lets you move it.** Below
   `confidence_threshold` (0.55) the message goes to today's note — the one
   bucket that is never *wrong*, only unspecific — and "no, that was a lyric"
   reverts the commit, refiles it, and saves a routing example. V never opens a
   "where should this go?" prompt: a gate in front of every unrecognised
   message is a gate in front of every attempt to talk to it.
4. **Lookups beat captures on ties.** Misrouting a question as a capture writes
   a junk note; misrouting a capture as a question just shows search results.
5. **Schema first.** Anything V creates is validated against the same schemas in
   `CLAUDE.md` that the Dataview dashboards rely on.
6. **The only egress points are `shells.py` and `llm.py`**, and folders in
   `settings.private_folders` never reach either — not through the MCP guard,
   the retrieval path, or the Read/Grep/Glob deny rules. The one hole is
   deliberate: the agent has `Bash`, and a subprocess is bound by none of
   those. Past that line the guard is the system prompt plus the run log, not
   the process. See `HOST_TOOLS` in `shells.py` for why that trade was made.
   One documented exception, `core/contacts.py`: emails and phone numbers out
   of `10 - Personal CRM/`, reachable only through `vault_contact` and
   `gcal.resolve`, structurally incapable of returning prose. It exists
   because the blanket rule made V ask Parsa for an address he had already
   written down — protecting a folder rather than what is private about it.
7. **A failed model call degrades, it doesn't end the turn.** Auth expiry and
   session limits fall back to local search with one line saying why.

## Tasks

Tasks in this vault are `- [ ]` checkboxes in note bodies, not frontmatter, so
`query.py` could not see a single one of ~1,100. That one gap was why "what are
my priorities", "how far behind am I", "what's left from my 1on1" and "mark X
done" all failed.

The parser is the easy half. The hard half is that **most checkboxes are not
tasks**, and a layer that reports all 1,100 is as useless as one that reports
none. Three classes, and only the first is a commitment:

- **task** — under a heading that says so (Tasks, Action Items, TODO), in a
  note whose purpose is commitments, in a daily note, or carrying a date.
- **reference** — a checkbox in a lesson, wiki, MR or anti-library note.
  `- [ ] UUID PKs by default` is a rule learned, not work owed.
- **recurring** — the identical checkbox in four or more notes. Derived from
  the data, not a blocklist, so the next template adopted is handled without a
  code change. Catches the MR review checklist (~180 notes) and
  `- [ ] Trained today` (every daily note) with one rule.

A task copied forward across days collapses to one entry, pointing at the
newest copy, with `carried 3×` recorded — that's a signal, not noise.

Ranking is deliberately not "oldest first". Undated staleness peaks in the
first three weeks and decays after two months: a line written in April is a
backlog problem, not the next thing to do. The first version ranked purely by
age and answered "what should I do next" with April, burying everything in
flight. Rot is counted separately by `backlog_shape()` instead.

Ticking a box reads the file, changes one line, and **refuses to write if any
other line moved**. It's the one structural edit worth automating; letting it
reformat a hand-written note is not.

## Calendar

`01 - Daily Notes/Calendar/YYYY-MM.md` — a flat, dated list, one line per
event, and the single source of truth. Everything else derives from it: the
remote iCal feed imports *into* it, each daily note's `## 📅 Calendar` section
renders *from* it, and `vault_schedule` appends to it.

Before this there were three answers to "what's on tomorrow" and no authority —
a feed only `daily_calendar.py` could read, a daily-note section that said
`_No events scheduled._` because nothing ever filled it, and V's reminders
table in SQLite where Obsidian cannot see it. So V answered *"I don't have
access to an external calendar"* to a question about the user's own notes.

A flat list rather than a note per event or a section per day, because both of
those need insertion into the middle of a file and the vault writer is
append-only. This sorts at render time and appends at write time.

## Google Calendar

Two calendars, and conflating them is how V ended up saying *"I do not have
access to Google Calendar"* about an account it could already read.

`01 - Daily Notes/Calendar/` is what Parsa **wrote down** — his own notes,
append-only, `[bot]`-committed, undoable. Google is the **live account**: where
invitations arrive, and the only place another person's schedule exists at all.
`gcal.py` reads and writes that one, via `gcal_events`, `gcal_calendars` and
`gcal_add_event`.

There are two ways in, and what they can do differs enough that the tools say
which one answered:

| | iCal feed | Calendar API |
|---|---|---|
| setup | already done — `.gcal_ics_url` | `cli gcal auth`, once |
| read own | yes | yes |
| read others | yes, if they send you their secret URL | yes, if they shared with you |
| busy/free for anyone else | no | yes |
| **write** | **never** | yes |

A feed is a subscription URL: there is no endpoint behind it to POST to, so no
amount of code turns one into a way to create an event. The tools say exactly
that rather than reporting an auth failure for something no credential fixes.

The `# Personal` / `# Work` comments in `.gcal_ics_url` are read as calendar
names — they were being discarded, so every event arrived from one anonymous
merged blob and "what's on my work calendar" had nothing to filter on. Adding a
colleague's feed is a `# Name` line and their URL, and needs no OAuth.

No `google-api-python-client`. Same reason there is no python-frontmatter and
no APScheduler: the API is JSON over HTTPS and OAuth refresh is one POST, which
is not worth ~40MB of transitive dependencies in a package whose premise is
that it runs with nothing installed.

**This is the third egress point**, after `shells.py` and `llm.py`, so rule 6
has to be read with it in mind. It is narrow: it sends what you ask it to
write and nothing else.

`resolve()` matches Google's own calendar list first, then falls back to
`contacts.lookup()` for a name Google has never heard of. The fallback was
originally refused on principle — reading `10 - Personal CRM/` to turn "Arash"
into an address looked like pushing a private note to Google. What it actually
produced was V telling Parsa it could not find Arash and asking him to type
out an email that was sitting in his own contacts. The address leaves; the
note does not. A CRM hit comes back flagged `unshared`, so it routes to
`freeBusy` and never renders as though titles were visible.

## Gmail

**The fourth egress point**, after `shells.py`, `llm.py` and Calendar.
`gmail.py` is the same shape as `gcal.py` for the same reason — no
`google-api-python-client`, one OAuth loopback flow, a token file in
`state/`, never in the vault — and can reuse Calendar's OAuth client
(`GCAL_CLIENT_ID`/`GCAL_CLIENT_SECRET`, same Google Cloud project) since it's
just a second scope grant. What it does *not* reuse is the token: Gmail's
scope is a materially bigger grant than Calendar's, so it gets its own
consent and its own file (`gmail_token.json`), on the theory that "Calendar
is connected" should never silently imply "so is Gmail."

```bash
v gmail auth   # enable the Gmail API on the same
                                       # project, then this walks through consent
v gmail --reset
```

Search, read and draft are conversational — ask V, it calls `gmail_search` /
`gmail_read` / `gmail_draft`. **Sending is not**, and is the one place this
vault treats a write as more dangerous than an appended note: `gmail_draft`
only ever creates a Gmail draft, nothing leaves the account, and `gmail_send`
takes a draft id and nothing else — there is no tool that composes and sends
in one call, so the model cannot reach one even by accident. `identity.py`'s
system prompt tells it never to call `gmail_send` in the same turn a draft
was created — show the exact text, wait for Parsa to confirm in his own next
message, then send. Every other write in this vault reverts (`vault_undo` is
a `git revert`; even a bad calendar invite is at worst an awkward follow-up
email); a sent email reaches a real person immediately and nothing here
undoes that, so it is gated by convention at the prompt level rather than
trusted to the same one-shot pattern as `gcal_add_event`.

## Quera

University deadlines, from quera.org — course homework/assignment due dates,
not to be confused with the unrelated quera.ir judge. There is no public
API (checked against three independent unofficial Quera tools before writing
`quera.py`: all of them scrape an authenticated session, none found a
documented API), so this does the same thing every other Quera tool does —
reads the dashboard HTML with a cookie lifted from a logged-in browser.

```bash
v quera auth --cookie "..."   # paste once
v quera sync                  # dry run
v quera sync --apply           # write the notes
```

**A pasted cookie, not a stored password** — decided deliberately: no
password ever touches disk, at the cost of the cookie eventually expiring
and needing a fresh paste. `state/quera_cookie.json` gets the same
`chmod 0o600` treatment as every other credential here. When a request
comes back looking like Quera's login page rather than the dashboard,
`quera.py` raises `NotAuthorised` rather than reporting zero deadlines —
"reconnect me" and "nothing is due" must never look the same.

Deadlines sync into the vault's existing `assignment`/`course` notes — the
same schema, same folders, same Dataview dashboards `CLAUDE.md` already
documents — via `Quera.sync()`, built the same way `work.py` turns `glab`
output into `ISSUE-`/`MR-` notes: a template fill, matched against what's
already there by (course, title) since Quera has no stable id the way
`glab`'s issue IID does, updating `due-date` on drift and never touching a
body Parsa has started filling in. `nudges.assignments_due` and
`query.assignments()` needed no changes at all to start working on this data
— they already rank and nudge on any `assignment` note, however it got
there.

**The parser is the one genuinely provisional part.** `parse_dashboard()` in
`quera.py` was written from prior art and documentation, without a real
logged-in session to test against while building it — same caveat
`mapsearch.py` carries for Neshan's response shape, for the same reason.
It takes two independent shots at the page (an embedded JSON state blob
first, then a generic link-plus-nearby-date scan as fallback) and is
expected to need adjustment once it runs against the real dashboard.

## The standing briefing

`cli brief` always assembled the right thing. What it had no way to do was
reach anyone: it printed to the stdout of whatever process ran it, so the
briefing existed and was still, in practice, something you had to ask for —
the standup-every-morning habit it was meant to replace.

`briefing.py` is the delivery half. **10:00 daily**, installed as a crontab
entry so it fires whether or not a `cli scheduler` process is running:

```bash
v briefing --install-cron   # 0 10 * * *
v briefing                  # what today's push holds
v briefing --status         # installed? what did it do?
v briefing --uninstall-cron
```

Two channels. **notify-send** is the interruption and is allowed to be lossy;
**today's daily note** is the record and is not, so missing the popup costs
nothing. The note append goes under `daily_log_heading` as a foldable callout —
a heading the Daily Note template actually has, because appending under one it
doesn't is how you get a stray section bolted to the bottom of every note.

The part that matters is when it **doesn't** push. A briefing that says "118
broken wikilinks" at you every morning trains you to dismiss it, and then it is
worth less than nothing, because the morning it does matter gets dismissed too.
`signal()` weighs what the brief contains: a meeting, a reminder or a nudge
urgent enough to act on today is worth a push on its own; two or more live
tasks is a shortlist; FYI-level nudges and the vault's permanent background
hum score **zero**. A morning that scores nothing is recorded as skipped and
`--status` will say so.

Three other things it refuses to do: push twice (the state file is keyed on the
date, so cron and a running scheduler cannot both fire it), push while `/quiet`
is set (read from disk on every run — the switch is thrown in the REPL and
honoured by a different process), and let a missing notification daemon stop
the note being written.

cron fires on the **system** zone, Asia/Tehran — the same one `config.TIMEZONE`
pins. Worth knowing because the shell V is usually launched from is not: the
`claude-parsa` alias carries `TZ=Asia/Singapore`, so `date` in that terminal
reads 4h30m ahead of when the entry actually fires.

## Telegram

```bash
export TELEGRAM_BOT_TOKEN=...        # from @BotFather; never committed
v telegram    # polls for messages, drives the proactive loop
```

**`cli telegram` has to actually be running for any of this to be proactive.**
Reminders firing every minute, the brag draft on the hour, and now the Gmail
and Quera checks (see below) are all jobs on `Scheduler`, and `Scheduler` only
ticks on the background thread `cli telegram` starts — closing that terminal
stops all of it, not just the chat. `briefing --install-cron` covers the
10:00 push independently of whether this is running, but that is the *only*
thing that does.

```bash
v service install     # systemd --user, auto-restart
v service uninstall
systemctl --user status v.service            # is it actually up
```

Installs `systemd/v.service` to `~/.config/systemd/user/` and
starts it — `cli telegram`, restarted on crash or reboot, needing no terminal
open. Secrets go in `~/.config/v/env` (gitignored, one `KEY=value` per line —
`TELEGRAM_BOT_TOKEN`, `GCAL_CLIENT_ID`/`GCAL_CLIENT_SECRET`), which the unit
reads via `EnvironmentFile`. No systemd (macOS, a minimal container): run
`v telegram` under whatever supervisor is available —
the unit file is the reference for what it needs (working directory, the
venv's own `python`, restart-on-failure).

`telegram.py` is the shim `engine.py`'s own comment promised: it turns an
update into `engine.handle(text)` and turns the `Response` back into a
message, `Response.buttons` rendered as an inline keyboard whose presses come
back as `engine.act(action, payload)` — the same call a button press makes in
the terminal. No python-telegram-bot, no aiogram: the Bot API is JSON over
HTTPS and long polling is a GET in a loop, the same shape this vault already
chose for Google Calendar over `google-api-python-client`.

**Owner-only, claimed rather than configured.** A bot token is reachable by
anyone who finds the username, and there is exactly one user everywhere else
in this codebase — so the first private message the bot ever receives claims
ownership, written to `state/config.json`, and every message from anyone else,
in any chat, is dropped before it reaches the engine. No reply to a stranger
either: a bot that says "you're not authorised" confirms it exists and is
listening, which is worth telling no one. `cli telegram --reset-owner` un-claims
it, for re-bootstrapping onto a new account.

**Archive groups.** Add the bot to a group whose only other member is Parsa —
a place he forwards music, links, whatever to himself rather than to any
particular person — and every message in it is read, unasked: there is no
"hey V" to address in a group with no one to overhear it, so these skip the
router entirely. Audio and voice messages are saved and embedded under
`14 - Music/Telegram Archive/<chat title>.md`; everything else — text, photos,
video, documents, forwards — goes to `12 - Brain Dump/Telegram Archive/<chat
title>.md`. One note per group, appended to, never recreated; a 👍 reaction is
the receipt, because a reply into a group with no one to read it would be V
talking to itself. A message from anyone but the owner in one of these groups
is dropped the same as anywhere else — the archive rule only ever applies to
what Parsa himself posts.

**Direct messages are the conversational surface** — text, same as the REPL;
voice, transcribed locally through `stt.py` (the module that already
documented "Telegram sends OGG/Opus" before this transport existed to prove
it); a location pin, filed as a place; a photo, video or document, downloaded
via `save_binary` and embedded in whatever note the router files the caption
under (`capture._daily` grew a `file_rel` embed for exactly this — the same
path any transport with an attachment can use, not a Telegram special case).

**Every direct reply comes back as text and voice, always.** `_reply` — the
one method every `_commit_*` handler routes through now, instead of calling
`send_message` directly — sends the chat bubble first, then a spoken version
of the same words as a proper Telegram voice-message bubble (waveform, play
button), not a generic audio-file attachment. The voice comes from `tts.py`,
and there are two of them.

The one you hear is the persona's — `gb_oliver_confident` on Voxtral, over
OpenRouter's speech endpoint, which is British, level and about three seconds
for a short reply. It is chosen in `persona.py` rather than in the config,
because a character's accent and its register are the same decision: Jarvis
sounding like Jarvis is one entry, and Friday will be another. Billing is per
character, which is roughly $0.002 for a two-sentence answer and one more
reason the length rule in `identity.py` is worth enforcing.

The one underneath is Piper, running locally, $0 and offline — the original
choice, and the same reasoning as `stt.py`'s Whisper over a hosted API. It is
not a rival to the hosted voice, it is what happens when the hosted voice
fails: `tts.Fallback` catches a dead network, an expired key or a 402 and
speaks the same words in the local voice, so an outage costs the accent rather
than the reply. `cli persona` shows which is configured, and `--say` renders a
line so you can hear it.

Piper's shipped voice is `en_GB-alan-medium`, downloaded separately (it's a
~63MB model, gitignored under `models/piper/`, the same treatment `models/`
already gives the Whisper weights):

```bash
pip install piper-tts
mkdir -p ~/.cache/v/models/piper
curl -L -o ~/.cache/v/models/piper/en_GB-alan-medium.onnx \
  "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx"
curl -L -o ~/.cache/v/models/piper/en_GB-alan-medium.onnx.json \
  "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json"
```

Missing the package or the model isn't an error — `tts.Synthesizer.load()`
raises the typed `Unavailable`, `_send_voice` catches it and the reply stays
text-only, the same degrade-don't-crash posture `stt.py` already has for a
machine without `faster-whisper` installed. `tts.py` itself has no idea
Telegram exists: `synthesize(text) -> bytes` hands back an audio file — WAV
from Piper, MP3 from the hosted voice, and `to_ogg_opus` takes either because
ffmpeg identifies its input by content rather than by extension — and the OGG/Opus conversion `sendVoice` needs lives in
`tts.to_ogg_opus` rather than in `telegram.py` — a future interface (a
different chat app, a CLI with speakers) reuses the same class and picks its
own output format instead of inheriting Telegram's. `BRAIN_NO_TTS=1` is the
kill switch, same shape as `BRAIN_NO_TELEGRAM`; `tts_provider = "null"` in
config does the same thing more permanently.

**A "typing…" / "recording voice…" hint while V is actually working on it.**
`_chat_action` sends Telegram's `sendChatAction` on a timer (it only holds
for ~5s per call) for as long as the block underneath it runs — `"typing"`
around the `engine.handle`/`engine.act` call, `"record_voice"` around Piper
synthesis. Escalating to the model can take a while; without this, a slow
reply looked identical to the bot having gone quiet, which is exactly the
"did nothing" confusion a voice message caused earlier.

**Online and offline notices.** The bot messages the owner "Online, sir —
ready when you are" as `cli telegram` comes up (skipped if ownership isn't
claimed yet — there's no chat to send it to), and "Going offline, sir — back
shortly" when the process is asked to stop, whether that's Ctrl-C or
`pkill -TERM` (a restart looks exactly like a crash otherwise, from the
owner's side of the chat). `_announce_offline` is registered against both
`SIGINT` and `SIGTERM` and raises `SystemExit` after sending, which is also
what makes a signal arriving mid-`getUpdates` interrupt that blocking call
right away instead of waiting out its 30s poll timeout.

**Reminders, the brief and brag drafts arrive as messages, not just
notifications.** `cli telegram` runs the bot's polling loop on the main thread
and `Scheduler` on a background one, pointed at `telegram.build_sink` instead
of the terminal's — the identical job set from **The standing briefing**
above, delivered a different way. Two `Engine`s, not one: a sqlite3 connection
belongs to the thread that opened it, the same rule `mcp/server.py` gives each
worker thread its own bundle for. `briefing.py`'s cron-driven push also gained
a `"telegram"` channel alongside `notify-send` and the daily note — that one
needs no polling loop running at all, just the token and the claimed owner id,
because sending a message and receiving one are different API calls.

Everything here is a no-op with no cost and no network call when
`telegram_bot_token` is unset — `send_message` returns `False` before it ever
opens a connection, which is what lets `briefing.py` default the channel on
without breaking a vault that has never heard of Telegram.

## The HUD

```bash
v hud                    # opens http://127.0.0.1:7717 in a browser
v hud --port 8080 --no-open
```

`repl` draws one conversation in a window. `hud` draws the *day*: the same
conversation next to the agenda, the ranked tasks, the deadlines, what V has
learned, what it wrote this session and which of the fifteen nudges fired —
plus the aperture, the route the last answer actually took, and a tool bus
showing every call with its latency. That is a layout problem before it is
anything else, and at 80×24 there is no layout.

Four rules it inherits rather than invents:

**It is free.** Every panel is SQL and Markdown, the same paths `glance()`
uses for the phone widget, and `test_hud` asserts the snapshot costs `0.0`. A
dashboard that redraws every four seconds and bills for it is a dashboard you
turn off. The one exception is the inbox, which is a real Gmail round trip and
is therefore cached — and it calls `search`, never `unseen`, because `unseen`
marks what it returns and would quietly eat the queue `scheduler.py` uses to
decide what it has already pushed you.

**It is loopback, and it is its own surface.** The socket binds `127.0.0.1`
with no flag to widen it: unlike the phone transport there is no mutual TLS
underneath to make a wider bind survivable. It is `persona.HUD` rather than
`persona.TUI` for the same reason — a TTY is held by the person sitting at it,
a loopback port is reachable by every process on the machine. **EDITH is not
reachable here**; summoned, `_agent_for` falls back to V and says so.

**The ambient state is derived, not picked.** There is no microphone on a
browser tab, so nothing pretends there is: typing is `listening`, a turn in
flight is `thinking`, the seconds after an answer are `speaking`, a turn that
committed is `writing`, one that raised is `fault`. `tui.py` maps the same
states onto the same real input, and the state list on the left is an
indicator rather than a control.

**Nothing on the page is markup.** The client builds every node with
`textContent`; there is no `innerHTML` path at all, which is what makes it
safe to render a note title, an email subject or a scraped Quera page. The
markdown in an answer is rendered to DOM by the same grammar `ui.py` renders
to ANSI — `**bold**`, `` `code` ``, `[[wikilinks]]`, bullets, fences — so the
browser does not repeat the bug the terminal already fixed.

Two things the page is careful about because this vault is half Persian: text
copied out of the vault gets `unicode-bidi: plaintext` so each row picks its
own direction, while sentences V composed in English do not — a nudge reading
`'آزمایش هشتم' was due 12 days ago.` starts with a strong RTL character and
would otherwise move its full stop to the far end. Paths stay LTR throughout,
because a path is structure and reordering its segments makes a correct string
unreadable.

The clock in the header runs on **V's** clock. `config.py` pins the process to
`TZ=Asia/Tehran`, routinely hours off the machine's own zone; left on the
browser's time the header read 04:14 while the tool bus under it stamped the
same calls 23:40 and the agenda was headed with yesterday.


## The phone

```bash
v phone enrol pixel   # issue the device certificate
v serve               # listen
v phone status        # who is enrolled, on what address
v phone revoke pixel  # it stops connecting immediately
```

Telegram was the first remote surface and it works. What it cannot be is
private: every voice memo, every photograph of a book page and every answer
about the finance ledger travels through a company's servers and stays in
their storage. For a surface whose entire content is a personal journal, that
is the wrong shape — so this is a second transport with the same architecture
and one different premise. **Nothing leaves the machine but the answer, and it
goes to exactly one phone.**

`httpapi.py` is the shim, the same one `telegram.py` is: an update comes in,
`engine.handle()` runs, a `Response` goes back. Nothing in `core/` learned
what HTTP is.

**Access control is the TLS handshake, not a check behind it.** `pki.py` keeps
a private CA in `state/pki/`; it signs one server certificate and one
certificate per device, and the listener sets `CERT_REQUIRED` against that CA
alone. An unenrolled client does not reach a request handler — it does not
finish the handshake. There is deliberately **no bearer token** on top: the
token would ship inside the same APK as the client key, so it would be guarded
by exactly the thing it was meant to back up, and a second gate that falls
whenever the first one does is decoration.

**The device key is generated on this machine and compiled into the APK.** It
is never transmitted, not even once during pairing — which is the usual hole in
schemes like this, because a pairing flow needs a channel to pair over and that
channel is either unauthenticated or already authenticated. Here the APK *is*
the credential, `adb install` is the enrolment, and rotating a key is a
rebuild. That trade is only affordable because there is one user and one phone,
which is what the rest of this package already assumes.

Two things are refused rather than made to work. There is **no unauthenticated
health endpoint** — one that answers before the handshake tells a scanner this
is here, and "no reply to a stranger" applies to ports as much as to chats. And
there is **no `--insecure` flag**, because a flag that turns off the only access
control in the system is a flag that eventually gets left on.

The socket also does not bind to `0.0.0.0`. mTLS means a hostile client on café
wifi cannot get a request through, but it can still reach the port, so
`resolve_bind()` prefers the tailnet interface — where the café cannot reach it
at all — and falls back to the LAN address. Widening that is a config line, and
deliberately a deliberate act.

| Endpoint | Does |
|---|---|
| `GET /v1/hello` | note count, whether speech is loaded, the event cursor |
| `GET /v1/glance` | the home-screen snapshot: next event, priorities, counts |
| `POST /v1/chat` | text in, a turn out — plus `file_rel` for an attachment |
| `POST /v1/attach` | bytes in, a vault path out |
| `GET /v1/file` | a file out of the vault, by path |
| `POST /v1/voice` | WAV in, the transcript **and** the turn out |
| `POST /v1/speak` | text in, OGG/Opus out (Piper) |
| `POST /v1/act` | a button press — the same `engine.act` the REPL calls |
| `POST /v1/session/new` | end this conversation, start the next |
| `POST /v1/wake` | the handsfree trigger — no model, usually just "Yes?" |
| `GET /v1/events` | SSE: reminders, the brief and nudges as they fire |

`/v1/events` is why there is no Firebase in the Android app. Routing a reminder
about a dentist appointment through Google, to arrive on a phone that is
already holding an authenticated socket to the machine that generated it, would
put a third party back in the one path this transport exists to keep private.
The bus keeps a bounded backlog keyed on a sequence number, so a phone that was
asleep reconnects with `?since=` and is handed what it missed — the same
catch-up-on-boot rule `Scheduler` has, one layer up, where losing it would look
identical to a reminder that silently never fired.

Everything else mirrors the Telegram transport because the reasons are
unchanged: one `Engine` owned by one thread (a sqlite3 connection belongs to
whichever thread opened it), one shared `Transcriber` and one shared
`MixedSynthesizer` behind locks, the scheduler on its own thread with its own
engine, and replies carrying both text and speech.

### The app

`android/` — Kotlin and Compose, built from the design brief's §03.
Not a WebView and not a PWA: the two requirements that shaped it are a
**bluetooth-handsfree trigger** and **notifications that arrive when the app is
closed**, and a web surface can do neither.

```bash
cd ~/projects/v/android
ANDROID_HOME=~/Android JAVA_HOME=~/Android/jdk \
  ~/Android/gradle/gradle-8.11.1/bin/gradle assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The brief's four load-bearing rules survive intact. `✦` prefixes everything V
says, in one of four colours and nowhere else. The cost line is under every
answer, always, and never behind a setting — `httpapi.voice_state()` and
`Turn.costLine()` decide it on the server so a second implementation of the
rule cannot drift on the phone. Persian renders in a bidi block that owns its
direction rather than inline. Red is refusal, amber is error, and they are
never the same colour.

The aperture is one component that takes a state, not an asset plus
animations — `Aperture.kt` draws the three circles and reads its rotation
period, core diameter, glow and halo off the state, exactly as `ui.aperture()`
does in the terminal. The waveform's 21 bars are driven by real RMS out of
`AudioRecord`, which is also why the mic path records raw PCM and posts WAV:
the brief forbids replaying a canned level, and a `MediaRecorder` file cannot
tell you the level while it is recording. It is 16kHz mono, which is what
Whisper wants anyway, so nothing transcodes at either end.

`VService` is a foreground service holding the event stream and the handsfree
receivers. On a headset connecting it calls `/v1/wake`, speaks the reply over
SCO, records until Parsa stops talking, and speaks the answer — the one place
in the system where a silence threshold has to do the job a finger does on the
touch surface.

**The assets directory is gitignored.** `cli phone enrol` writes the device's
private key into it, and this repo gets pushed.

### Files, both directions

Attaching works in two calls, not one multipart request: `POST /v1/attach`
takes the bytes and the name and gives back a vault path, then `POST /v1/chat`
carries that path with the caption. The split is because of the caption — it is
UTF-8 prose that *routes the turn*, and the only places a raw-body upload could
carry it are a query string or an HTTP header, both of which would mangle every
Persian caption Parsa writes. So the bytes go up the way a voice note does,
with nothing to parse, and the sentence goes up as ordinary JSON.

The upload is a vault write like any other — `save_binary` into
`attachments/phone/`, a `[bot]` commit, revertible with `v undo` — for the same
reason the rest of them are: a file that is in the vault is backed up, indexed
and undoable, and a file in a temp directory is one more thing to lose. It caps
at 25 MB, stricter than Telegram's own 50, because every attachment is a commit
in a repository that gets cloned and a vault that gains a 400 MB screen
recording is one nobody can clone again. A photo with no caption is a whole
message, and `router` already reads `has_file` as enough to route on.

Coming back, a turn carries `files` — name, size and type, never the bytes.
Most files V mentions are never opened, and four megabytes riding along with
every answer would make the fast local ones feel like the slow ones; the phone
fetches from `GET /v1/file` when the chip is tapped. That endpoint enforces the
vault boundary and deliberately *not* the private-folder rule: `mcp/guard.py`
blocks those because the thing on the other side is a model, and the thing on
the other side of this socket is Parsa's phone behind a certificate this
machine issued — one that `/v1/chat` already answers out of those same folders
to. A rule that serves the sentence and withholds the file only stops the
honest half. Losing the phone is answered where it is answerable: `pki.revoke`.

`vault_send_file` no longer means "send to Telegram". It queues into the
`outbox` table — a channel that had been built and never used — because the
process that *makes* a file is not the process holding a socket to the phone:
it runs inside whatever MCP subprocess an agent turn spawned, while the event
stream lives in `v-serve`. A row in SQLite is the whole mechanism, and being
durable is the point, since a chart rendered while the phone was in a tunnel is
still waiting when it comes back rather than having been broadcast to nobody.
Telegram still goes inline alongside it, best-effort, because an HTTP call to
Telegram's API needs no socket of ours.

Two drainers read that queue: a chat turn takes anything waiting so a chart
asked for from the phone arrives *with* the answer, and a one-minute scheduler
sweep catches whatever no turn came to collect. `Reminders.claim` makes that
safe — `WHERE sent IS NULL` in the same statement as the update, so the loser
of the race gets nothing rather than sending the file twice. Arriving with no
app open, a file becomes a notification with a **Save** action that downloads
it into Downloads without opening anything.

### Two cards on the home screen

`TodayWidget` is the next thing on the calendar with the rest of the day under
it; `PrioritiesWidget` is what to do next, with a box that ticks the checkbox
in the Markdown note it came from. Glance, which is Compose compiled down to
`RemoteViews` — so they share `Design.kt`'s palette by construction rather than
by a second copy of the hexes, and lose the three things a launcher will not
render: IBM Plex (system mono instead), any animation (hence no aperture — the
mark *is* the state machine, and a still one would be the muted state), and any
way to show an error.

That last constraint is the design. A widget cannot say "request failed"
without becoming a bug report on someone's wallpaper, and for most of the day
the laptop is asleep or the phone is off the tailnet — so `Snapshot` keeps the
last answer on disk, every card draws from it instantly, and the header carries
the time it was true, in amber once it is over half an hour old. Same honesty
the cost line is built on, different surface.

Both cards read one `GET /v1/glance`, which is why that endpoint exists rather
than the widgets making four calls between them: the expensive part of reaching
V from a home screen is not the query — it is free and runs in milliseconds —
but waking a radio and completing a handshake. Three refreshes arriving at once
collapse into one request.

Refreshes come from three places, and only the first is Android's:
`updatePeriodMillis` at its 30-minute floor, which is the one that still works
when the service has been killed; the header tap; and `VService`, which
redraws both cards the moment anything comes down the event stream. That third
one is what a cloud-backed widget cannot do — the socket is already open to the
machine that owns the data, so a reminder firing or the 10:00 brief landing
reaches the home screen with no polling interval and no push service in the
path.

The tick is optimistic on purpose. The box strikes through and the card redraws
*before* the request goes out, because a home screen that waits for a sleeping
laptop before acknowledging a thumb feels broken even while it is working. The
write itself is unchanged — `engine.act("complete_task")`, the same call the
chat buttons make, `[bot]`-committed and undoable with `v undo`. If it fails,
the next fetch puts the task back, which is the correct outcome: the note is
the truth and the card is a picture of it.

## Brag entries, drafted as work closes

`nudges.brag_gap` had noticed the same thing every month since it was written:
no brag entry, N MRs merged. Noticing is not the fix. By the time a review asks
what you shipped, the evidence is six months of GitLab history you no longer
remember the shape of, and what gets written down is the last three weeks.

So when an issue turns `closed` or an MR turns `merged`, V writes the entry —
a vault query, a template fill, a git commit, hourly, no model. Which also
means **nothing is invented**: every line is copied out of the issue or MR note
it came from, and the two fields that need judgement (`impact`, `## Reflection`)
are left blank on purpose. A brag document full of a machine's guesses about
why your work mattered is worse than an empty one, because in six months you
cannot tell which parts are true. Drafts carry a `draft` tag until you write
that line.

```bash
v brag             # dry run — what it would file, and why
v brag --apply     # write them
v brag --pending   # drafts still missing their impact line
v brag --backfill --apply --since 2026-06-01
```

**It only writes when there is something to write**, which is the whole
difference between this and a script that turns every merged MR into a note.
`substance()` is the gate: a typo fix, a bump, a revert or an issue closed with
an empty body are not achievements, and filing them would bury the ten entries
a year that matter under two hundred that don't — the same failure as having no
brag document at all. What qualifies is a real description, review comments, or
a linked MR, and the report says which of those it was.

The **first run seeds without writing**. The vault already holds 162 closed
issues and merged MRs; drafting all of them in one commit would teach you the
folder is noise. `--backfill` is the deliberate catch-up.

Nothing is drafted twice, and that is checked against the vault — an item
already linked from an existing entry is skipped even if `state/brag.json` is
deleted.

## Notes V creates come from the vault's templates

Every creation path used to carry its own hand-written body, and copies drift.
The daily scaffold in `capture.py` had lost `## 📅 Calendar` (so the calendar
sync had nowhere to write on a day V got there first), the Work/University/Life
split under Tasks, and `### Blockers`. `work.py`'s issue body had no acceptance
criteria and no activity log. A note V made was recognisably V's.

`templates.py` reads the real file out of `05 - Templates/` and fills it —
both token dialects the vault uses, Templater's `<% tp.date.now("GGGG-[W]WW",
0, tp.file.title, "YYYY-MM-DD") %>` and Obsidian's `{{date:YYYY-MM-DD}}`.
Unknown tokens render **empty rather than being left in place**, because
`schema.validate_note` counts a surviving `{{…}}` as an error — a template
token in frontmatter is the single biggest source of notes that vanish from
every dashboard.

Daily notes, place notes, CRM notes, issues and MRs all go through it. A
missing template is never a reason to refuse a write: `render()` returns None
and the caller keeps its old inline body.

It also fixed a quiet bug. V appended CRM interactions under a heading called
`Interactions`, which no person note has — the template calls it
`🗓 Interaction Log`. Every interaction V logged started a second log at the
bottom of the note instead of joining the one already there.

## Memory

The part that makes V fit *this* brain rather than any brain. Four rules, all
enforced in `memory.py` rather than left to a prompt:

- **Feedback writes; conversation doesn't.** Memories come from "remember
  that…", "always…", "never…", and corrections ("no, actually…"). Ordinary chat
  writes nothing — a memory that writes itself from conversation fills with
  plausible junk and then injects the junk as though you'd said it.
- **Every memory carries its reason.** `why` is what lets a later contradiction
  be judged rather than blindly overwrite.
- **Contradiction supersedes, it doesn't delete.** The old belief stays
  readable with a pointer to what replaced it — when V behaves oddly, that's
  the explanation.
- **Unused memories decay**, computed on read rather than by a job that might
  not have run.

Behavioural memories (preferences, corrections) are injected into *every* model
run regardless of topic — "be brief" shares no words with the question it
governs. Factual ones have to match to earn their place.

Two memories are "about the same thing" at ≥0.55 similarity and a restatement
at ≥0.85, measured as the max of token overlap and local-embedding cosine.
Overlap alone can't see that *"put gym stuff in 04 - Gym"* and *"gym notes
belong in 04 - Gym, not the daily note"* are one belief; the embedder can.

Everything is visible at `19 - Assistant/V/Memory.md` and via
`v memory --why <id>`, which prints a memory's full
history: when it was learned, every reinforcement and contradiction, and what
replaced it.

## Cost model

Four tiers, chosen from the prompt text before a token is spent
(`shells.weigh`). Haiku 4.5 is the cheapest model available and the floor —
there is nothing below it, so the real saving is the paths that call no model
at all.

| Tier | Model | Rate /Mtok | For |
|---|---|---|---|
| — | none | $0 | greetings, meta, anything with a frontmatter answer |
| `trivial` | Haiku 4.5 | $1 / $5 | rephrase, confirm, extract — no reading |
| `light` | Haiku 4.5 | $1 / $5 | one fact, one lookup, a short answer |
| `normal` | Sonnet 5 | $3 / $15 | the default — synthesis across notes |
| `heavy` | Sonnet 5 | $3 / $15 | audits, comparisons, multi-hop work |

On a subscription nothing is billed per run, so `Response.cost` stays 0.0 and
the allowance consumed is reported separately as `list_cost_usd`. Calling that
"spend" would be a lie; it's rendered as an equivalent.

### What the run costs before it starts

Three things were being paid for on every call and used by none of them. All
three are measurements, taken on this machine on 2026-08-22 against the same
message and model, not estimates:

**stdin has to be closed.** Left open, the CLI waits three seconds for input
that is never coming. The decider paid it on every classification and the
agent on every escalation — half the median latency of the routing path was a
process listening to a pipe nobody was writing to.

**A denied tool still costs its definition.** `decide.py` runs with
`dontAsk` and no allowlist, so it could never call a tool; it was loading
12.5K tokens of tool definitions anyway. `--tools ""` takes the prefix from
19,554 tokens to 7,012 and the cost from ~$0.010 to ~$0.003 a call.

**A prompt that changes cannot be cached.** Memory and the local search
results are computed from the question, and they used to be appended to the
system prompt — so the prefix was different every run. Moved into the turn,
where they read the same way and still override the defaults:

| | cached | re-read | cost |
|---|---|---|---|
| memory in the system prompt | 54% | 14.1K | $0.031 |
| memory in the turn | 71% | 8.7K | $0.021 |

`--json-schema` was tried on the decider in the same sitting and rejected: it
runs as a validation pass rather than a constraint, and cost three turns,
2.2× the tokens and 1.5× the wall time to return JSON that `_extract_json`
was already getting for free.

Every run now records `tools`, token counts and the cache fraction to
`state/logs/hermes.jsonl`, because on a subscription a cheap run and an
expensive one look identical in the reported cost — `cli cost` reads it back.

## The run, as it happens

`--output-format stream-json` instead of one blob at the end, read line by
line as the run goes (`shells.ClaudeCodeShell._stream`). Three things follow
from it, and the third is why it defaults on:

**Progress.** `shells.ShellEvent` carries what the run is doing — the session
starting, each tool call with what it is about, the first text as it is
written, the plan's rate-limit window changing state. `Engine.progress` is
where a surface hooks in: the scrolling prompt prints the route above the
spinner, the window turns one aperture arm per tool in flight and puts the
tool's name where the generic verb was. Both were built for this and had
nothing to draw until now.

**Per-tier patience.** A flat 300s ceiling meant "stuck" was the same
statement about a two-word lookup and a vault-wide audit. Now: trivial 60s,
light 90s, normal 240s, heavy 600s.

**A timeout keeps the answer.** Four runs on record hit the ceiling and
returned *nothing* — two, four and five minutes of work, most of it done,
thrown away because the process was killed before it printed its one JSON
object. What had been written by then is now kept, marked as cut off, and the
tool trace comes with it.

Two more ceilings that are the CLI's rather than a number noticed afterwards:
`--max-budget-usd` stops a run that keeps spending — the most expensive on
record was $0.92 over sixteen turns, shopping for a desk mat — and
`--fallback-model` answers on Haiku rather than failing when the tier's model
is overloaded. The budget is checked at the turn boundary, so it stops a
runaway and cannot stop one expensive turn; when it fires it reports an error
with an empty result, and the answer that turn had written survives only
because the stream was being read.

## Questions the window refused

Two failures on this machine are neither faults nor permanent: the Pro plan's
window fills up, and the Claude Code login expires. Seven of the thirteen
failed runs on record are one of those two, and what happened to all seven is
the point — V degraded to local search, said one honest line about why, and
dropped the question. The window reopened four hours later with nobody there
to ask again.

`core/deferred.py` parks the question with the time it can be retried, and the
scheduler asks it then. The answer arrives unprompted, hours late, carrying
the question it belongs to:

```
  ✦ You asked, when I couldn't answer: "why did I pick append-only writes?"
    …
```

The retry time comes from the stream's `resetsAt` when there is one — exact,
no interpretation — and falls back to reading "resets 3am" out of the error,
as *tomorrow's* 3am rather than this morning's. Held through quiet hours: the
five-hour window very often resets at 3am, which is when an unprompted message
is worth least. Three windows, and then it says it is giving up, out loud —
silence is the one outcome this is built to prevent.

Only failures that time fixes go in. A wrong answer, a denied tool, a prompt
too long: retrying those in four hours produces the same failure twice, four
hours apart.

## Design decisions worth knowing

The full list, with what was rejected and why, is append-only at
`19 - Assistant/V/Decisions.md`. The ones that shape the code most:

**No vector database.** 5,400 chunks × 2,048 dims is a 21 MB array; brute-force
cosine is one matmul. Chroma/Qdrant/pgvector would be more infrastructure for
less speed at this size.

**No APScheduler.** The job set is fixed and small, and hand-rolling it buys the
one behaviour that matters: catch-up on boot. A reminder that silently never
fires because the process was down is the worst failure this system can have.

**No python-frontmatter.** 80 lines, no dependency, and it round-trips this
vault's exact style (quoted dates, ZWNJ, Persian filenames) without reformatting
notes you hand-wrote.

**Deterministic chunk IDs** (`blake2b(rel|ord)`). With autoincrement rowids,
re-indexing renumbers chunks and every stored embedding silently points at the
wrong text — a bug that produces plausible, confidently wrong retrieval.

**Status vocabulary is normalised at query time, not migrated.** GitLab writes
`opened`; the schema says `open`. The notes are re-synced from GitLab, so a
migration only holds until the next `work --apply`. Comparing raw strings was
hiding 40 issues and 181 MRs behind the answer "0 open issues" — a wrong number
that reads like a fact.

**A blank frontmatter flag is unknown, not false.** `wifi:` unset is a gap in
the note, not a statement about the café. Excluding blanks answered "0 places"
to a question the vault could partly answer, with no hint anything was filtered.

**Re-index on startup.** One `stat()` per note (~30ms). The old build answered
from whatever was last indexed — a note written that morning was invisible, one
deleted last week still counted, and it reported neither.

**Staleness is per note, not a global timestamp.** Comparing every mtime
against one `last_index` marker made "stale" mean something subtly different
from "a rebuild would change something": a note V had just written and
re-indexed by itself still read as newer than the last full scan, so every
following call ordered a rebuild that did nothing — and a note edited *during*
a long rebuild was invisible forever, because the marker is stamped when the
scan finishes. Per-note mtimes are the same condition `rebuild` uses, so the
two agree by construction.

**A write re-indexes the note it wrote.** Tasks exist only in the mirror —
they are checkbox lines in note bodies, so nothing queries the markdown — which
makes an unsynced mirror not a stale answer but a wrong one. `add()` wrote the
checkbox and left the mirror alone; the CLI hid that behind a whole-vault
rebuild, and the MCP server, which has none, did not hide it at all. Now the
one note that changed is re-parsed, and the long-lived server re-checks
freshness at most once every few seconds so a hand edit in Obsidian shows up on
the next question rather than the next restart.

**The clock is pinned, not inherited.** `Settings.timezone` is applied to the
process in `config.load()`, before anything reads a clock. Nearly every time
call in here is a naive `now()`, and `daily_calendar.py` snapshots the zone
into a module constant at import — all of which read the ambient `TZ`, and
`~/.bashrc` carries `TZ=Asia/Singapore` on the `claude-parsa` alias. A V
session started that way ran 4h30m ahead and wrote the 12:00 standup into the
calendar as 16:30. Setting `TZ` once for the process fixes every call site at
once, including the vendored script; fixing them individually fixes only the
ones you remember.

**Two embedding backends, both kept.** A hashed char-ngram TF-IDF (instant, no
download) and `bge-m3` (better retrieval, minutes to build). Each gets its own
index file, so switching costs nothing. Character n-grams are why the tf-idf
side still works on Persian.

## Self-documentation

V writes its own notes into `19 - Assistant/V/`, refreshed nightly by the
scheduler and on demand with `cli docs`:

- **README** — what V is and how to teach it
- **Skills** — capabilities, *generated from the live code*, so a feature that
  breaks stops being claimed
- **Goals** — what V is for, and how it would know it's failing
- **Memory** — every belief, its reason, its strength, and what it replaced
- **Decisions** — append-only choices about V's own behaviour, with reasons
- **Logs/** — a note per day of what V actually did

## State

`~/.local/state/v/` holds `assistant.db` (index + memory),
`embeddings-*.npz`, `scheduler.json`, `briefing.json` (what the last standing
push did, and why it stayed silent if it did), `brag.json` (which closures have
been considered), `routing_examples.jsonl` and `logs/`.
The indexes are regenerable — delete and re-run `index --embed`. **Memory is
not**: it lives in the same db, and the vault mirror at
`19 - Assistant/V/Memory.md` is the readable backup.
