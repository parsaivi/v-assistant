"""Configuration. Everything the assistant needs to know about where things live.

Zero-cost by construction: no key here enables a paid call. The LLM section only
records *which* provider would be used if one were ever configured; the default
is the null provider, which never touches a network.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "v"
CONFIG_FILE = CONFIG_DIR / "config.json"


def _site_config() -> dict:
    """The one file read before a vault is known.

    `state/config.json` cannot hold the vault path: it lives *under* the state
    directory, which is itself derived from settings. This is the bootstrap
    half — vault and state locations only — and it sits outside both the
    package and the vault, because a reusable assistant cannot ship with one
    person's paths compiled into it.
    """
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _looks_like_a_vault(p: Path) -> bool:
    """Obsidian stamps `.obsidian/` into every vault it opens — cheaper and
    more honest than guessing from folder names, which differ per vault."""
    return (p / ".obsidian").is_dir()


class VaultNotConfigured(RuntimeError):
    """Raised when no vault can be found. Carries the fix, not just the fact."""


def resolve_vault() -> Path:
    """Where the notes are. Four sources, most explicit first.

    V used to answer this with `Path(__file__).parents[2]`: the package lived
    inside the vault, so the vault was wherever the code was. That is exactly
    the coupling that made V un-runnable against any other vault, and it had
    to go before anything else could be reused. The package now lives on its
    own and is *told* where the notes are.
    """
    env = os.environ.get("V_VAULT") or os.environ.get("BRAIN_VAULT")
    if env:
        return Path(env).expanduser()
    configured = _site_config().get("vault")
    if configured:
        return Path(configured).expanduser()
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if _looks_like_a_vault(candidate):
            return candidate
    raise VaultNotConfigured(
        "V does not know where your vault is. Set V_VAULT, or write it once:\n"
        f"  mkdir -p {CONFIG_DIR} && "
        f"echo '{{\"vault\": \"/path/to/vault\"}}' > {CONFIG_FILE}")


VAULT = resolve_vault()
# State and models belong to the assistant, not to the vault — they are
# rebuilt artefacts and private keys. Keeping them under the notes folder
# meant every vault sync carried a 12 GB model cache and a device certificate.
STATE_DIR = Path(os.environ.get("V_STATE") or os.environ.get("BRAIN_STATE")
                 or _site_config().get("state_dir")
                 or _xdg("XDG_STATE_HOME", ".local/state") / "v")
MODEL_DIR = Path(os.environ.get("V_MODELS") or os.environ.get("BRAIN_MODELS")
                 or _site_config().get("model_dir")
                 or _xdg("XDG_CACHE_HOME", ".cache") / "v" / "models")

# The timezone V thinks in, pinned rather than inherited.
#
# Nearly every clock call in this package is a naive `dt.datetime.now()` or
# `dt.date.today()`, and `daily_calendar.py` builds its `LOCAL_TZ` from
# `dt.datetime.now().astimezone()`. All of those read the ambient TZ of
# whatever process happened to start V — and that is not reliably this one:
# `~/.bashrc` carries `alias claude-parsa="TZ=Asia/Singapore ... claude"`, so
# a V session launched through it runs 4h30m ahead. That is exactly how the
# August calendar ended up with the 12:00 standup written as 16:30 — the iCal
# import converted every event into Singapore time and then wrote the result
# down as if it were local.
#
# Fixing it at the call sites would mean touching ~50 `now()`s and hoping the
# next one added remembers. Setting TZ for the process instead fixes them all
# at once, including the vendored script, and it has to happen here because
# `config` is the first module every other one imports.
TIMEZONE = os.environ.get("BRAIN_TZ", "Asia/Tehran")


def apply_timezone(name: str) -> None:
    """Pin the process clock. POSIX-only; a no-op where `tzset` is missing."""
    os.environ["TZ"] = name
    try:
        time.tzset()
    except AttributeError:      # Windows
        pass

# Must be set before huggingface_hub is imported anywhere — it snapshots these
# into module constants at import time, so setting them lazily is too late and
# silently re-downloads 2GB into ~/.cache instead of reusing the vault copy.
os.environ.setdefault("HF_HOME", str(MODEL_DIR))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(MODEL_DIR))


@dataclass
class Folders:
    """Vault folder names. Kept in one place because they start with numbers."""

    home: str = "00 - Home"
    daily: str = "01 - Daily Notes"
    weekly: str = "01 - Daily Notes/Weekly"
    monthly: str = "01 - Daily Notes/Monthly"
    work: str = "02 - Work"
    issues: str = "02 - Work/Issues"
    mrs: str = "02 - Work/Merge Requests"
    wikis: str = "02 - Work/Wikis"
    sprints: str = "02 - Work/Sprints"
    brag: str = "02 - Work/Brag Document"
    university: str = "03 - University"
    courses: str = "03 - University/Courses"
    assignments: str = "03 - University/Assignments"
    gym: str = "04 - Gym"
    workouts: str = "04 - Gym/Workouts"
    food: str = "04 - Gym/Food"
    bodyweight: str = "04 - Gym/Body Weight"
    templates: str = "05 - Templates"
    archive: str = "06 - Archive"
    goals: str = "07 - Goals"
    atlas: str = "08 - Atlas"
    antilibrary: str = "09 - Anti-Library"
    movies: str = "09 - Anti-Library/01 - Movies"
    series: str = "09 - Anti-Library/02 - Series"
    books: str = "09 - Anti-Library/03 - Books"
    youtube: str = "09 - Anti-Library/04 - YouTube"
    locations: str = "09 - Anti-Library/05 - Locations"
    crm: str = "10 - Personal CRM"
    travel: str = "11 - Travel Planning"
    braindump: str = "12 - Brain Dump"
    finance: str = "13 - Finance Tracker"
    music: str = "14 - Music"
    lyrics: str = "14 - Music/Lyrics"
    inbox: str = "_Inbox"


@dataclass
class Settings:
    vault: Path = VAULT
    state_dir: Path = STATE_DIR
    # Downloaded weights and voice references — 12 GB of them, which is why
    # they are a cache and not state. `tts.py` used to derive this as
    # `state_dir.parent / "models"`, which was right only while the two sat
    # side by side inside the vault; after the split it pointed at
    # ~/.local/state/models and every Piper voice read as "not installed".
    model_dir: Path = MODEL_DIR
    folders: Folders = field(default_factory=Folders)

    # --- time ---
    # See TIMEZONE above. Applied to the process in `load()`.
    timezone: str = TIMEZONE

    # --- git ---
    git_enabled: bool = True
    git_author_name: str = "brain-assistant"
    git_author_email: str = "assistant@localhost"
    commit_prefix: str = "[bot]"
    # Commit whatever is sitting in the working tree before V does anything.
    # Notes written by hand in Obsidian are uncommitted until something
    # commits them, and V's own writes are path-limited `git commit -- <rel>`:
    # append to a note the user had already edited and that one commit carries
    # both, under the `[bot]` prefix, where `undo` will happily revert the
    # hand-written half along with V's. Committing first keeps the two apart.
    git_commit_on_start: bool = True
    # …but only the notes. This used to matter a great deal: V's own source
    # sat inside the vault, and half-finished code getting committed every
    # time the CLI started is a different program than the one anyone asked
    # for. The package has since moved out, so this excludes nothing during
    # normal operation — it is kept pointing at the old path because the
    # vault still carries the uncommitted *deletion* of `assistant/`, and
    # that is a change to review by hand rather than one for a startup
    # commit to sweep in. Safe to empty once that deletion is committed.
    git_commit_on_start_exclude: tuple[str, ...] = ("assistant",)

    # --- routing ---
    # Below this, the router asks instead of guessing (the `question` pattern).
    confidence_threshold: float = 0.55

    # --- the model-backed decider (core/decide.py) -----------------------
    # Regexes keep only the decisions they cannot get wrong; everything
    # content-shaped goes to a cheap model. ON by default, and that default is
    # the point rather than a convenience: shipping it off meant the regexes
    # were still the whole decision, so "for today calander set my meeting on
    # 13:00 and not 17:30" filed an event titled "calander set meeting and
    # not" — instantly and for free, which is worth nothing when it's wrong.
    # The ~9s and ~$0.011 of plan allowance buy the message being understood.
    # Set false to go back to rules-only; every path still has a free fallback.
    decider_enabled: bool = True
    decider_model: str = "haiku"
    decider_timeout: int = 45
    # Below this the model's own answer is discarded and the heuristics run
    # instead. Set high: a hedging classifier is worth less than the rules,
    # which are at least fast and free when they are unsure.
    decider_min_confidence: float = 0.5

    # --- daily note sections ---
    # These MUST name headings that exist in `05 - Templates/Daily Note.md`.
    # Templater owns that template now (folder template on `01 - Daily Notes`),
    # so when the user opens today's note in Obsidian the file is built from it,
    # not by `ensure_daily`. Appending under a heading the template doesn't have
    # makes `_append_under_heading` fall back to bolting a new section onto the
    # bottom of every daily note. Keep these in sync with the template.
    daily_log_heading: str = "Day life"
    daily_task_heading: str = "Tasks"

    # --- freshness ---
    # Re-index on startup when the vault has moved since the last run. The
    # check is one stat() per note (~30ms here); skipping it is how the
    # assistant ended up answering "2 places" from a mirror that still held a
    # note deleted days earlier and had never seen the one added since.
    auto_index: bool = True

    # --- retrieval ---
    chunk_tokens: int = 400
    chunk_overlap: int = 60
    # Default is tfidf, not "auto", and that is a measured decision rather than
    # a timid one: bge-m3 is the better retriever but takes *minutes* to load on
    # this CPU, which a per-invocation CLI pays every single time. tf-idf loads
    # instantly, needs no download, and handles Persian via char n-grams. Set
    # this to "sentence-transformers" for a one-off high-quality index build, or
    # once the assistant runs as a long-lived process that loads the model once.
    embedding_backend: str = "tfidf"  # auto | sentence-transformers | tfidf
    embedding_model: str = "BAAI/bge-m3"
    embedding_fallback_model: str = "intfloat/multilingual-e5-small"
    rag_top_k: int = 10

    # --- privacy tiering -------------------------------------------------
    # Folders whose bodies must never be sent to a remote API. They are still
    # indexed and searched locally; only egress is blocked. Enforced in llm.py.
    private_folders: tuple[str, ...] = (
        "17 - AIT",
        "12 - Brain Dump",
        "10 - Personal CRM",
        # The folder name is the instruction. It was reachable by search until
        # now — a passage from it came back in a `search "expert"` result,
        # which means it would also have been pasted into a model's prompt.
        "19 - Assistant/My Thought(dont read)",
    )

    # --- speech ---
    whisper_model: str = "large-v3"
    whisper_fallback_model: str = "medium"
    whisper_compute_type: str = "int8"
    whisper_language: str | None = None  # None = autodetect (vault is bilingual)

    # --- llm (nothing here spends money by default) ---
    llm_provider: str = "null"  # null | anthropic
    llm_model: str = "claude-haiku-4-5-20251001"

    # --- reasoning shell (the agentic tier) ------------------------------
    # `claude-code` drives the Claude Code CLI headlessly, which authenticates
    # with the claude.ai subscription already on this machine — no API key and
    # no per-token bill. It draws on the same plan allowance as the chat app,
    # which is the real budget to watch on a Pro plan.
    shell_provider: str = "null"        # null | claude-code | hermes
    # How long the run may say *nothing* before it counts as stuck — an idle
    # deadline, not a ceiling on the whole run. A total ceiling cannot tell a
    # run that is working from one that is hung, and the log is unambiguous
    # about which one it kept killing: twelve runs died at their tier limit
    # having emitted no text at all, every one of them mid-tool on real work,
    # and the identical request re-sent minutes later finished in 83s. Under a
    # streaming reader the distinction is free — a working run emits tool
    # calls and message deltas continuously, a hung one emits nothing — so
    # "stuck" is now measured as silence, and a long job is simply allowed to
    # be long. `shell_idle` is the fallback for a tier not named here.
    #
    # Sized above the longest legitimate gap between events (one slow tool
    # call), not above the length of the job.
    shell_idle: int = 150
    shell_idle_trivial: int = 45
    shell_idle_light: int = 60
    shell_idle_normal: int = 150
    shell_idle_heavy: int = 240
    # A ceiling on one run, enforced by the CLI. Checked at the turn boundary,
    # so it stops a run that keeps going — the most expensive on record was
    # $0.92 over 16 turns, shopping for a desk mat — and cannot stop a single
    # expensive turn. Set to 0 to lift it.
    shell_max_budget_usd: float = 0.60
    # A ceiling on the *day*, across every surface — the HUD's budget gauge
    # fills against it. Not enforced: nothing refuses a turn for crossing it,
    # because the one thing worse than an expensive Tuesday is V going silent
    # in the middle of one. It is a number to notice, and the gauge turns
    # amber at 80% of it. Read `hermes.jsonl`, so what fills it is plan
    # allowance consumed — the figure that matters on a subscription, where
    # `Response.cost` is 0.0 by design and would draw a permanently empty bar.
    daily_budget_usd: float = 2.00
    # When the tier's model is overloaded or unavailable, drop to this rather
    # than failing the turn. Retried at the start of each user turn, so a
    # blip costs one cheap turn and not the conversation.
    shell_fallback_model: str = "haiku"
    # Read the run as it happens (`--output-format stream-json`) instead of
    # waiting for one JSON blob at the end. Two things depend on it: progress
    # the surfaces can show, and — the reason it defaults on — a timeout that
    # keeps the partial answer and the tool trace instead of discarding
    # everything the run had done. Set false to go back to a single blob.
    shell_stream: bool = True
    # The `claude` CLI reads its OAuth session from this directory. The
    # machine's default profile (~/.claude) has a dead token — empty access
    # and refresh tokens — so ClaudeCodeShell pins the profile known to work
    # instead of inheriting whatever CLAUDE_CONFIG_DIR the parent shell has.
    claude_config_dir: str = os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude-parsa"))
    # When both free paths miss, hand the question to the shell rather than
    # replying "nothing matches". Harmless while shell_provider is "null" —
    # there is nothing to escalate to — so zero-cost stays the default posture.
    shell_auto_escalate: bool = True

    # --- model tiers -----------------------------------------------------
    # The model is chosen per question, not set once. Most of what gets asked
    # here is a lookup or a short synthesis; paying top-tier reasoning rates
    # for "when did I last squat" is waste, and on a Pro plan that waste comes
    # out of the same allowance as normal Claude usage. `weigh()` in shells.py
    # picks the tier from the prompt, for free, before anything is spent.
    # Haiku 4.5 is the cheapest model available ($1/$5 per million tokens
    # against Sonnet's $3/$15 and Opus's $5/$25) and is the floor of the
    # ladder — `trivial` and `light` both sit on it and differ only in effort,
    # because there is nothing cheaper to drop to.
    tier_trivial_model: str = "haiku"   # rephrase, confirm, extract — no reading
    tier_trivial_effort: str = "low"
    tier_light_model: str = "haiku"     # one fact, one lookup, a short answer
    tier_light_effort: str = "low"
    tier_normal_model: str = "sonnet"   # the default — synthesis across notes
    tier_normal_effort: str = "medium"
    tier_heavy_model: str = "sonnet"    # audits, comparisons, multi-hop work
    tier_heavy_effort: str = "high"

    # Conversations do NOT continue by default. Resuming re-sends the entire
    # thread on every turn, so an assistant that silently keeps one open bills
    # a growing history for questions that had nothing to do with each other.
    # Each run is its own context unless you explicitly continue one.
    shell_continue_by_default: bool = False
    # Even an explicit continue gives up after this long — a thread from this
    # morning is expensive to reopen and rarely what you meant. Does not apply
    # inside an open session; see below.
    shell_session_minutes: int = 30
    # …because a session you started and have not ended is you saying the
    # opposite: keep the thread, however long I take between messages. While
    # one is open every escalation resumes it and the expiry above is
    # suspended. Turn this off to go back to per-message threads.
    shell_persistent_session: bool = True
    hermes_provider: str = "anthropic"
    hermes_model: str = "claude-sonnet-5"

    # --- proactive ---
    quiet_hours: tuple[int, int] = (0, 7)
    max_proactive_per_day: int = 4

    # --- telegram ---------------------------------------------------------
    # The token never belongs in the vault's git history, so it's read from
    # the environment (matching how the CRM/gcal egress points already avoid
    # hand-rolled secret files) with `state/config.json` — gitignored — as the
    # fallback a one-time `cli telegram` setup can write to.
    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    # Parsa's Telegram user id, hardcoded rather than left to the claim flow —
    # there is exactly one user of this vault, same as the rest of V, and no
    # reason to make every fresh checkout re-bootstrap it by DMing the bot
    # first. 0 still means unclaimed: the first person to say anything to the
    # bot in a private chat becomes the owner, permanently, and every message
    # from anyone else is dropped from then on — see `core/telegram.py`. That
    # path only matters now if this gets reset to 0 (a new bot token, a wiped
    # state/config.json); it's what enforces single-owner on a surface anyone
    # on the internet can otherwise open a chat with.
    telegram_owner_id: int = 0
    telegram_poll_timeout: int = 30

    # --- live map search ---------------------------------------------------
    # Free-tier POI search (platform.neshan.org) for finding places NOT yet in
    # the vault — "cafés near Divar" when nothing in `09 - .../Locations` is
    # close enough to answer from. Optional: empty means `vault_places_search`
    # tells the model to fall back to WebSearch/WebFetch against Google Maps
    # or Neshan's own site instead of failing outright.
    neshan_api_key: str = field(
        default_factory=lambda: os.environ.get("NESHAN_API_KEY", ""))
    # The *other* Neshan key, and it is genuinely a different string from the
    # same panel: `neshan_api_key` is the service key sent as an `Api-Key`
    # header by `/v1/search` and `/v4/direction`, while this one is the web
    # key the map SDK takes as a `key` option. `render.route_html` embeds it
    # in the page it writes, so it is only ever as private as that file.
    # Empty means the map pages still get written but load blank tiles — the
    # route and the pins draw, the streets under them don't.
    neshan_web_key: str = field(
        default_factory=lambda: os.environ.get("NESHAN_WEB_KEY", ""))
    # Which map vendor `mapsearch` talks to. One value today; it exists so
    # that adding a second (Google's Places/Routes, which need a billing
    # account Parsa doesn't have) is a new branch in `mapsearch` rather than
    # an edit to every tool that calls it.
    maps_provider: str = "neshan"

    # --- exchange rates ----------------------------------------------------
    # navasan.tech — dollar, euro, gold and coin in toman, for `core/rates.py`
    # and the HUD's RATES card. The free plan is 120 requests a month, which
    # is the whole reason that module keeps a ledger: one fetch a day, the
    # rest only when a person asks. Empty means the card says "not set up"
    # and `vault_rates` says so rather than answering from memory.
    navasan_api_key: str = field(
        default_factory=lambda: os.environ.get("NAVASAN_API_KEY", ""))

    # --- who V is ----------------------------------------------------------
    # The character V wears: how it speaks, and which voice reads the replies
    # out. `core/persona.py` holds them; this is which one. An unknown name
    # falls back to the default rather than failing, and the change takes
    # effect on the next start because the system prompt is snapshotted at
    # import (see `identity._PERSONA`).
    persona: str = "jarvis"

    # --- text to speech -----------------------------------------------
    # Two providers, and the second is the safety net rather than a rival.
    # "openrouter" is the good voice — a British male on a hosted model, ~3s
    # for a short reply, billed per character. "piper" is the local one that
    # cost nothing and never went down, and it is what every remote failure
    # degrades to, so an empty balance or a dead network costs the accent and
    # not the speech. "null" is the off switch, same shape as `shell_provider`.
    tts_provider: str = "openrouter"   # openrouter | piper | null
    # Per-character billing means a long reply is a more expensive reply,
    # which is one more reason the length rule in `identity.py` is worth
    # enforcing. A two-sentence answer on Voxtral is about $0.002.
    openrouter_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    # All four voice fields are overrides: empty means "whatever the persona
    # speaks with", which is the answer you want almost always — the point of
    # `persona.py` is that a character's voice and its accent travel together.
    # Set one only to try a voice without editing the persona.
    tts_model: str = ""                # an OpenRouter speech model id
    tts_remote_voice: str = ""         # a voice id that model accepts
    tts_voice: str = ""                # the local Piper voice
    # Notes and replies quote Persian words inside otherwise-English text;
    # a second voice for those runs beats mangling them through an
    # English phonemizer that doesn't know Persian script. Optional — if
    # the model file isn't present, Persian runs just fall back to
    # `tts_voice` (today's behavior) instead of failing the whole reply.
    tts_voice_fa: str = ""
    # Piper's `length_scale`, inverted so bigger feels like "faster" rather
    # than "longer": 1.0 is the voice's own default pace, 1.2 is 20% faster.
    # Tunable without a code change via state/config.json. Remote voices take
    # it as OpenAI's `speed`, which means the same thing and the same 1.0.
    tts_speed: float = 1.0
    # A voice reply nobody is waiting for is worthless: past this many seconds
    # the remote attempt is abandoned and Piper answers instead. Generous
    # enough for a long reply on a slow provider, short enough that Telegram's
    # "recording voice…" does not sit there for a minute.
    tts_timeout: int = 25

    # --- the phone ---------------------------------------------------------
    # `core/httpapi.py` — the one transport that belongs to us rather than to
    # a chat company. There is no token here and no allow-list of addresses:
    # access control is the TLS handshake against the private CA in
    # `state/pki/` (see `core/pki.py`), so the only thing configurable is
    # where the socket listens and on which port.
    phone_port: int = 8443
    # Empty means "work it out" — `httpapi.resolve_bind()` prefers the
    # tailnet interface, then the LAN address, and never 0.0.0.0. Set this
    # only to override that: it is the difference between a port a café can
    # reach and one it cannot, so widening it is a deliberate act.
    phone_bind: str = ""
    # The device name `cli phone enrol` issues a certificate for by default,
    # and the CN the app presents. One phone, named, rather than a wildcard —
    # revoking is deleting that certificate, so the name has to mean something.
    phone_device: str = "pixel"

    @property
    def tz(self):
        """The pinned zone as a tzinfo, for the few places that need an aware
        datetime. Everything naive is already correct via `apply_timezone`."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            import datetime as _dt
            return _dt.datetime.now().astimezone().tzinfo

    @property
    def db_path(self) -> Path:
        return self.state_dir / "assistant.db"

    @property
    def index_path(self) -> Path:
        return self.index_path_for(self.embedding_backend)

    def index_path_for(self, backend: str) -> Path:
        """One index file per backend, so a good index and a fast index coexist.

        Building the bge-m3 index takes ~3 minutes; there is no reason to throw
        it away every time the CLI runs with the instant-loading default.
        """
        name = "tfidf" if backend in ("tfidf", "auto") else "st"
        return self.state_dir / f"embeddings-{name}.npz"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def folder(self, name: str) -> Path:
        return self.vault / getattr(self.folders, name)

    def is_private(self, rel_path: str) -> bool:
        return any(rel_path.startswith(p) for p in self.private_folders)

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vault"] = str(self.vault)
        d["state_dir"] = str(self.state_dir)
        d["model_dir"] = str(self.model_dir)
        return d


_USER_CONFIG = STATE_DIR / "config.json"


def load() -> Settings:
    """Load settings, applying any overrides from state/config.json."""
    s = Settings()
    if _USER_CONFIG.exists():
        try:
            raw = json.loads(_USER_CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
        for k, v in raw.items():
            if k in ("vault", "state_dir", "model_dir"):
                setattr(s, k, Path(v))
            elif hasattr(s, k) and k != "folders":
                setattr(s, k, v)
    # Before anything in the package reads a clock, and before `agenda` loads
    # `daily_calendar.py`, which snapshots the zone into a module constant.
    apply_timezone(s.timezone)
    s.ensure_dirs()
    return s


settings = load()
