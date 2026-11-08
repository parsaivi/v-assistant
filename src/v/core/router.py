r"""Intent router: what a message *is*, decided only where a rule cannot be wrong.

**Commands are routed here. Meaning is not.** That inversion is recent and it
is the whole design. What came before was a rule cascade — forty-odd patterns
scoring intents against each other — with a haiku classifier bolted on top, and
between them they decided every message before anything that could read it got
a turn. They were confidently wrong in the way only pattern matching is:

  "so i should learn about those vulnerabilities. do some search for me and
   create a simple and compact document so i can read with examples."

opened with `i should`, which the classifier is instructed to treat as a
commitment, so it filed a checkbox reading *Learn about those vulnerabilities*
and dropped the request. The reply — "no i want you to actually search…" —
began with `no`, which the memory layer's correction pattern owned, so it was
stored as a permanent belief and never reached a handler at all. Two messages,
two different rules, neither of which could see it was being asked to do
something.

No confidence threshold catches that, because the rules were certain. The only
fix is to stop having them decide. So:

* **Fast paths** are what remains: an attachment, a location pin, an empty
  message, `/status`, a bare `undo`, `hi`. Each matches the whole message
  against a fixed string. None of them is an interpretation.
* **Everything else is `AGENT`** — handed to the model with the vault as tools
  (`vault_query`, `vault_search`, `vault_add_task`, `WebSearch`, …). It decides
  what the message wants and which tools serve it, which is the job the
  classifier was being asked to do without the ability to do it.
* **The cascade survives as the offline path.** `route_offline` is the old
  scoring chain, unchanged, and it runs when no shell is available. A degraded
  V that guesses is better than one that stops; a V that guesses *while a model
  is sitting right there* is what this file used to be.

The cost is real and was the reason for all of it: the free tier answered most
messages for nothing, and the agent runs ~$0.03–0.29 each. That trade was made
deliberately. A free wrong answer is worth less than nothing — it costs the
correction, the re-ask, and the junk it wrote on the way through.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings, settings as default_settings

# --- intents ---------------------------------------------------------
QUERY = "query"                 # a question about the vault
CAPTURE_NOTE = "capture.note"   # a stray thought → daily note
CAPTURE_LYRIC = "capture.lyric"
CAPTURE_TASK = "capture.task"
CAPTURE_PLACE = "capture.place"
CAPTURE_PERSON = "capture.person"
CAPTURE_WORKOUT = "capture.workout"
CAPTURE_EXPENSE = "capture.expense"
CAPTURE_DECISION = "capture.decision"
CAPTURE_IDEA = "capture.idea"
CAPTURE_LINK = "capture.link"
REMINDER = "reminder"
BRIEF = "brief"
NUDGE_CHECK = "nudges"
STATUS = "status"
UNDO = "undo"
HELP = "help"
SMALLTALK = "smalltalk"   # greetings, thanks, "who are you" — answered locally
TEACH = "teach"           # "remember that…", "always…", "no, actually…"
RECALL = "recall"         # "/memory", "what do you know about me"
TASKS = "tasks"           # "what's on my plate", "priorities", "how far behind"
TASK_DONE = "task.done"   # "mark X done"
TASK_ADD = "task.add"     # "add a task to review the pricing doc"
CALENDAR = "calendar"     # "what's on tomorrow", "meetings this week"
SCHEDULE = "schedule"     # "on Aug 8 at 12:30 I have a meeting"
COMPOUND = "compound"     # several verbs at once — must reach the agent
CHAT = "chat"             # talking *to* V — answered by the model, in V's voice
REFILE = "refile"         # "no, that was a lyric" — moves the last capture
AGENT = "agent"           # the default: the model reads it and picks its own tools
SUMMON = "summon"         # "friday" / "edith" / "V" — calling an agent by name

CAPTURE_INTENTS = {
    CAPTURE_NOTE, CAPTURE_LYRIC, CAPTURE_TASK, CAPTURE_PLACE, CAPTURE_PERSON,
    CAPTURE_WORKOUT, CAPTURE_EXPENSE, CAPTURE_DECISION, CAPTURE_IDEA, CAPTURE_LINK,
}


@dataclass
class Route:
    intent: str
    confidence: float
    reason: str
    slots: dict = field(default_factory=dict)
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    cost: float = 0.0
    # Whether a model (the decider) chose this route and filled its slots.
    # Provenance needs it: a slot the model rephrased is the model's words,
    # even when the code that writes it is deterministic.
    by_model: bool = False

    @property
    def certain(self) -> bool:
        return self.confidence >= default_settings.confidence_threshold

    def __str__(self) -> str:
        return f"{self.intent} ({self.confidence:.0%}) — {self.reason}"


# --- signals ---------------------------------------------------------
URL_RE = re.compile(r"https?://\S+")
QUESTION_WORDS = (
    "what", "which", "when", "where", "who", "how", "why", "did i", "do i",
    "have i", "am i", "is there", "are there", "show me", "list", "find",
    "search", "چی", "کی", "کجا", "چطور", "چرا", "کدام", "آیا", "نشون",
)
LYRIC_HINTS = (
    "lyric", "verse", "chorus", "hook", "bridge", "melody", "song", "آهنگ",
    "ترانه", "شعر", "کوروس",
)
TASK_HINTS = (
    "remind me", "todo", "to do", "don't forget", "dont forget", "i need to",
    "i should", "task:", "یادم", "باید", "فراموش",
)
DECISION_HINTS = (
    "we decided", "i decided", "decision:", "we're going with", "we will use",
    "chose to", "going with", "تصمیم",
)
WORKOUT_HINTS = (
    "sets", "reps", "bench", "squat", "deadlift", "pull-up", "pullup", "press",
    "curl", "workout", "gym", "kg", "lifted", "باشگاه", "تمرین",
)
EXPENSE_HINTS = ("spent", "paid", "cost me", "bought", "تومان", "تومن", "هزینه", "خریدم")
PLACE_HINTS = ("cafe", "café", "restaurant", "bar", "park", "bookstore",
               "کافه", "رستوران", "پارک")
IDEA_HINTS = ("idea:", "what if", "we could", "it would be cool", "ایده")
PERSON_HINTS = ("met with", "coffee with", "talked to", "called", "lunch with",
                "1on1", "دیدم", "صحبت کردم")
# Deliberately excludes "remind"/"tell"/"give" — those collide with reminders
# and tasks, where a misroute costs more than it saves.
RETRIEVAL_VERBS = {"show", "list", "find", "search", "recommend", "suggest",
                   "نشون", "لیست", "پیدا"}
SUPERLATIVES = {"best", "top", "worst", "favourite", "favorite", "closest",
                "nearest", "cheapest", "any", "بهترین", "نزدیک‌ترین"}

REMINDER_TIME = re.compile(
    r"\b(?:in\s+(\d+)\s*(min|minute|minutes|h|hour|hours|d|day|days)"
    r"|at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
    r"|tomorrow|tonight|today|next week)\b", re.I)

WORKOUT_SETS = re.compile(r"\b\d+\s*(?:x|×)\s*\d+\b|\b\d+\s*(?:kg|kilos?)\b", re.I)
MONEY = re.compile(r"\b\d[\d,]*\s*(?:k|تومان|تومن|toman|rial|ریال|\$|usd)\b", re.I)

# --- expense capture: category/direction inference -------------------------
# Best-effort only — ambiguous text leaves category unset rather than guessing
# wrong, since a wrong category is worse than "uncategorized" on the dashboard.
INCOME_HINTS = ("got paid", "salary", "paycheck", "received", "income",
                "حقوق", "دریافت کردم", "واریز شد")
SAVINGS_HINTS = ("saved", "savings", "invested", "investment",
                 "پس‌انداز", "سرمایه‌گذاری")
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dining": ("cafe", "café", "coffee", "restaurant", "lunch", "dinner",
               "breakfast", "کافه", "رستوران"),
    "groceries": ("groceries", "grocery", "supermarket", "خرید روزانه", "سوپرمارکت"),
    "transport": ("taxi", "snapp", "uber", "gas", "petrol", "bus", "metro",
                  "رفت و آمد", "تاکسی", "بنزین"),
    "entertainment": ("movie", "cinema", "concert", "تفریح", "سرگرمی", "سینما"),
    "health-beauty": ("doctor", "pharmacy", "haircut", "salon",
                       "سلامت", "آرایشگاه", "داروخانه"),
    "culture-arts": ("book", "museum", "کتاب", "موزه", "فرهنگی", "هنری"),
    "fashion": ("clothes", "clothing", "shoes", "پوشاک", "لباس"),
    "bills": ("bill", "internet bill", "phone bill", "قبض", "شارژ"),
    "home": ("furniture", "خانه", "کاشانه"),
    "fitness": ("gym", "supplement", "protein powder", "ورزش", "باشگاه"),
    "shared-expense": ("split the bill", "dong", "دنگ"),
    "loan": ("installment", "loan payment", "قسط", "وام"),
}
AMOUNT_TOMAN = re.compile(
    r"(\d[\d,]*)\s*(k|تومان|تومن|toman|rial|ریال)\b", re.I)


def amount_to_toman(amount_text: str) -> int | None:
    """Best-effort parse of a MONEY match into an integer toman amount.

    Returns None for currencies with no fixed conversion (USD) rather than
    fabricating an exchange rate.
    """
    m = AMOUNT_TOMAN.search(amount_text or "")
    if not m:
        return None
    num = int(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    if unit == "k":
        return num * 1000
    if unit in ("تومان", "تومن", "toman"):
        return num
    if unit in ("ریال", "rial"):
        return round(num / 10)
    return None

# --- lookups that carry no question mark and no retrieval verb -------------
# The engine's `_structured()` already answers all of these exactly and for
# free; without these rules the router never sends them there, so a working
# capability is unreachable by the phrasing people actually use. "open MRs"
# was landing in `capture.note` — one confidence point away from writing a
# junk note called *open MRs*.
SCHEMA_NOUNS = ("issue", "issues", "mr", "mrs", "merge request", "merge requests",
                "assignment", "assignments", "workout", "workouts", "sprint",
                "goal", "goals", "reminder", "reminders", "place", "places",
                "note", "notes")
STATUS_WORDS = ("open", "closed", "merged", "in-review", "in review", "stale",
                "in-progress", "in progress", "pending", "overdue", "due",
                "blocked", "orphan", "broken", "unrated", "dormant")
STANDUP = re.compile(r"\bstand[\s-]?up\b", re.I)
# A bare identifier is a lookup — "ISSUE-139", "MR-747", "[[some note]]".
# These used to score nothing and land in the ambiguity prompt, so the most
# natural way to ask about a specific issue was also the one way that couldn't.
NOTE_REF = re.compile(
    r"^\s*(?:\[\[[^\]]+\]\]|(?:issue|mr|merge[\s-]?request|sprint)[\s-]?#?\d+)"
    r"\s*\??\s*$", re.I)
# "somewhere quiet to work", "a place to sit", "where can I study"
WHERE_TO = re.compile(
    r"\b(somewhere|some\s?place|a\s+place|anywhere|where)\b.{0,30}?"
    r"\bto\s+(work|sit|study|read|write)\b", re.I)
# Bare "met Sara" — a capitalised name is what separates meeting a person from
# "met the deadline", which must not create a CRM note.
MET_PERSON = re.compile(r"\b(?:met|ran into|caught up with|saw)\s+[A-Z][\w’']+")
HELP_EXACT = {"help", "?", "what can you do", "what can you do?", "commands"}

# The whole message, or it isn't a command — "undo" alone means undo, while
# "undo culture is exhausting" is a thought about undo. Anchored on both ends
# so the second one can never revert a write.
UNDO_EXACT = re.compile(
    r"^\s*(?:undo|revert|nevermind|never mind|scratch that|"
    r"undo that|take that back|لغو|برگردون)\s*[.!]?\s*$", re.I)

# --- smalltalk -------------------------------------------------------------
# "hi" used to score nothing, land in capture.note at 50% confidence, and open
# a four-button "where should this go?" prompt. "how are you" scored as a
# question, found no schema, and escalated to the paid agent — $0.04 to be told
# the assistant is fine. Both are now answered locally, for nothing.
GREETING = re.compile(
    # "good night" is a sign-off, not a greeting, so it lives in BYE — and
    # GREETING is matched first, so leaving it in both meant it never reached
    # BYE at all.
    r"^\s*(?:hi|hey|hello|yo|sup|hiya|howdy|good\s+(?:morning|afternoon|"
    r"evening)|morning|evening|salam|salaam|سلام|درود)"
    r"[\s!.,?]*$", re.I)
THANKS = re.compile(
    r"^\s*(?:thanks|thank you|thx|ty|cheers|nice|great|perfect|awesome|"
    r"cool|got it|ok|okay|k|sure|yep|yes|no worries|ممنون|مرسی|دمت گرم)"
    r"[\s!.,]*$", re.I)
HOW_ARE_YOU = re.compile(
    r"^\s*(?:how (?:are|r) (?:you|u)|how'?s it going|you (?:ok|okay|good|there)|"
    r"what'?s up|wassup|چطوری|خوبی)[\s!.,?]*$", re.I)
WHO_ARE_YOU = re.compile(
    r"^\s*(?:who (?:are|r) (?:you|u)|what (?:are|r) (?:you|u)|"
    r"what'?s your name|who am i talking to|introduce yourself|"
    r"tell me about yourself|تو کی هستی|اسمت چیه)[\s!.,?]*$", re.I)
BYE = re.compile(
    r"^\s*(?:bye|goodbye|good\s?night|see (?:you|ya)|later|cya|"
    r"خداحافظ|شب بخیر)[\s!.,]*$", re.I)

# The names, not the keys — `persona.ALIASES` is the authority on which name
# maps to which agent, so a new character needs no edit here. Built at import
# from that map so the two cannot drift.
def _summon_re():
    import re as _re
    from .persona import ALIASES
    names = sorted(ALIASES, key=len, reverse=True)
    alt = "|".join(_re.escape(n) for n in names)
    # optional summoning verb · the name · optional trailing address or
    # punctuation. "friday", "hey edith", "V?", "edith, you there"
    return _re.compile(
        rf"^\s*(?:hey|hi|hello|ok|okay|yo|salam|سلام)?[\s,]*"
        rf"(?P<name>{alt})"
        rf"\s*[?!.،,]*\s*(?:are you there|you there|wake up|online)?[?!.\s]*$",
        _re.I)


SUMMON_RE = _summon_re()

SMALLTALK_PATTERNS = (
    ("greeting", GREETING), ("thanks", THANKS), ("how", HOW_ARE_YOU),
    ("who", WHO_ARE_YOU), ("bye", BYE),
)

# --- how long an answer he wants -------------------------------------------
# V's default is one or two sentences (see `identity.py`'s "## Length"), which
# is right for the twenty lookups a day and wrong for the one time he wants the
# reasoning. This is that one time. It is a pattern rather than a judgement
# because the cost of getting it wrong is asymmetric: a long answer to "explain
# this" is what he asked for, and a long answer to "is MR-1648 merged" is the
# behaviour the length rule exists to stop.
#
# Deliberately narrow. "why" has to be a question about the reasoning ("why did
# you", "why is it") and not any sentence containing the word, or "remind me
# why I filed this" lifts the ceiling on a reminder.
DETAIL = re.compile(
    r"\b(?:explain|elaborate|in detail|in full|walk me through|"
    r"talk me through|break (?:it|this|that) down|"
    r"how (?:did|do) you\b|what did you (?:do|change|try)\b|"
    r"why (?:did|do|does|is|are|was|were|would|isn.t|didn.t|can.t)\b|"
    r"(?:tell|give) me (?:more|everything|the (?:whole|full|long))|"
    r"(?:the )?(?:whole|full|long) (?:story|version|answer|thing)|"
    r"چرا|توضیح بده)", re.I)


def wants_detail(text: str) -> bool:
    """Did he ask for the long answer? See `DETAIL`."""
    return bool(DETAIL.search(text or ""))


# --- talking to V ----------------------------------------------------------
# "hey V, tell me about yourself and your personality" matched nothing, scored
# nothing, and landed in capture.note at 50% — which used to open a "where
# should this go?" prompt over a message that was never a note. Two rules fix
# the whole class: being addressed by name is itself a signal, and so is
# second-person language aimed at the assistant.
ADDRESSED = re.compile(
    r"^\s*(?:hey|hi|hello|yo|ok(?:ay)?|so|please)?[\s,]*\bv\b[\s,:؛—–-]*", re.I)
TALK_RE = re.compile(
    r"\b(?:talk|chat|speak) (?:to|with) me\b"
    r"|\btell me about (?:yourself|you\b|your\b)"
    r"|\b(?:yourself|urself)\b"
    r"|\bwho (?:are|r) you\b|\bwhat (?:are|r) you\b"
    r"|\bwhat do you think\b"
    r"|\bhow do you (?:work|feel|think|decide)\b"
    r"|\b(?:can|could|will|would) you\b"
    r"|\byour (?:personality|opinion|thoughts?|abilities|capabilities|name)\b"
    r"|\bباهام حرف بزن\b|\bخودت\b",
    re.I)

# --- moving the last capture ----------------------------------------------
# This is what the ambiguity prompt was really for: a way to say "wrong bucket".
# Asking *before* writing blocked every message that wasn't a capture at all;
# asking after costs nothing, because the write is a git commit that can be
# reverted. Same correction, same learning loop, no gate in front of the chat.
REFILE_RE = re.compile(
    r"^\s*(?:no[,.\s]+)?(?:that|it|this|the last one)\s+(?:was|is|were)\s+"
    r"(?:a|an|my)?\s*(\w+)"
    r"|^\s*(?:file|move|put|save)\s+(?:that|it|this)\s+(?:as|in|to|under)\s+"
    r"(?:a|an|the)?\s*(\w+)", re.I)
REFILE_KINDS = {
    "lyric": CAPTURE_LYRIC, "lyrics": CAPTURE_LYRIC, "song": CAPTURE_LYRIC,
    "verse": CAPTURE_LYRIC, "idea": CAPTURE_IDEA, "task": CAPTURE_TASK,
    "todo": CAPTURE_TASK, "note": CAPTURE_NOTE, "thought": CAPTURE_NOTE,
    "expense": CAPTURE_EXPENSE, "workout": CAPTURE_WORKOUT,
    "place": CAPTURE_PLACE, "person": CAPTURE_PERSON,
    "decision": CAPTURE_DECISION, "link": CAPTURE_LINK,
}

# --- tasks, calendar, and the compound guard -------------------------------
# "what are my priorities", "how far behind am I", "what's on my plate" — all
# task questions, and before the task layer existed all of them either fell
# through to the paid agent or matched one noun and printed a table.
PRIORITY_RE = re.compile(
    r"\b(priorit(?:y|ies)|most important|what should i (?:do|work on)|"
    r"what'?s (?:on my plate|next|important)|top of the list|focus on|"
    r"اولویت|مهم‌?ترین)\b", re.I)
BEHIND_RE = re.compile(
    # "how far behind", but also "how far we are behind" / "how far am I
    # behind" — the words between are the ones people actually say.
    r"\b(how far\b.{0,18}\bbehind|behind (?:on|with|schedule)?|catch(?:ing)? up|"
    r"backlog|falling behind|how (?:am i|are we) doing|where (?:am i|are we)|"
    r"عقب|عقب‌?افتاده)\b", re.I)
TASKLIST_RE = re.compile(
    r"\b(my )?(?:open |outstanding |unfinished |pending )?"
    r"(tasks?|to-?dos?|todos?|checklist|action items?|کارها|تسک)\b", re.I)
OVERDUE_RE = re.compile(r"\b(overdue|late|past due|missed|stale tasks?)\b", re.I)

# "mark the debugging task done", "tick off buy milk", "X is done"
DONE_RE = re.compile(
    r"\b(?:mark|tick|check|cross)\s+(?:off\s+)?(?:the\s+)?(.{2,80}?)"
    r"\s+(?:as\s+)?(?:done|complete[d]?|finished|off)\b"
    r"|\b(?:done|finished|completed)\s+(?:with\s+)?(?:the\s+)?(.{2,80})$",
    re.I)

CALENDAR_RE = re.compile(
    r"\b(calendar|meeting?s?|appointments?|schedule|agenda|standup|1[\s-]?on[\s-]?1|"
    r"جلسه|قرار|تقویم|برنامه)\b", re.I)
# "on Aug 8 at 12:30", "tomorrow at 3pm", "next Monday 09:00"
WHEN_AT = re.compile(
    r"\b(?:on\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:tomorrow|today|tonight|next\s+\w+day|this\s+\w+day)\b", re.I)
CLOCK = re.compile(r"\b\d{1,2}(?::(\d{2}))?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b", re.I)

# Verbs that mean "change something", as opposed to "tell me something".
ACTION_VERBS = re.compile(
    r"\b(update|create|make|write|add|draft|generate|build|plan|"
    r"summari[sz]e|review|analyz?se|analyze|prioriti[sz]e|organi[sz]e|"
    r"refactor|clean\s?up|fix|mark|close|file|"
    r"cancel|reschedule|move|remove|delete|clear|split|merge)\b", re.I)

# "add a task to review the pricing doc" is one instruction, not a compound —
# it just happens to contain two verbs, one of which is inside the task's own
# text. Matched before the compound guard so a plain task-add stays free.
ADD_TASK_RE = re.compile(
    r"^\s*(?:hey\s+v[,\s]+)?(?:please\s+)?(?:add|create|put|note)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:task|todo|to-do|item|reminder\s+task)"
    r"\s*(?::|to|for|that|about)?\s*(?P<what>.+)$", re.I)
CONJOINED = re.compile(r"\b(and|then|also|plus|و\s|سپس)\b", re.I)

# Asking for a judgement, not a list. "which tasks should I just delete" and
# "what's worth dropping" are the same shape as "what are my tasks" to a noun
# matcher, and answering them with all 178 open tasks is the exact failure the
# compound guard exists to prevent — one noun matched, a table printed, the
# actual question ignored.
JUDGEMENT_RE = re.compile(
    r"\b(should i|shall i|worth (?:doing|dropping|keeping)|"
    r"which (?:ones? )?(?:should|can|could|do you think)|"
    r"help me (?:decide|choose|pick|figure)|what do you think|"
    r"recommend|advise|drop|abandon|give up on)\b", re.I)

# Where the tasks came from. "what's left from my 1on1 action items" names a
# source the flat list cannot honour; without this it returned everything.
TASK_SOURCE = (
    ("1on1", "02 - Work/Meetings/1on1s"),
    ("1:1", "02 - Work/Meetings/1on1s"),
    ("one on one", "02 - Work/Meetings/1on1s"),
    ("meeting", "02 - Work/Meetings"),
    ("sprint", "02 - Work/Sprints"),
    ("issue", "02 - Work/Issues"),
    ("goal", "07 - Goals"),
    ("daily note", "01 - Daily Notes"),
    ("university", "03 - University"),
    ("assignment", "03 - University/Assignments"),
)

NEXT_EVENT_RE = re.compile(
    r"\b(next|upcoming|coming up|after this)\b.{0,20}"
    r"\b(meeting|event|appointment|call|1[\s-]?on[\s-]?1)\b"
    r"|\bwhen('?s| is)\s+(my|the)\s+next\b", re.I)


def _task_source(low: str) -> str:
    for word, folder in TASK_SOURCE:
        if word in low:
            return folder
    return ""


@dataclass
class Router:
    settings: Settings = field(default_factory=lambda: default_settings)
    llm: object | None = None
    decider: object | None = None
    # Whether a reasoning shell is actually reachable. The engine sets this
    # from `hermes.available`; False collapses `route` onto `route_offline`,
    # which is the only reason the cascade below still exists.
    agentic: bool = True

    def __post_init__(self):
        self._examples = _load_examples(self.settings)
        if self.decider is None:
            from .decide import Decider   # local: decide imports config only
            self.decider = Decider(self.settings)

    # -----------------------------------------------------------------
    def _ask_model(self, raw: str) -> Route | None:
        """The model's read of a content-shaped message, or None.

        None on every failure — no model, a timeout, unparseable output, an
        intent that isn't real, or a confidence too low to beat a heuristic.
        The caller treats None as "carry on with the rules", so the worst case
        of turning the decider on is the behaviour we had before it existed.
        """
        if self.decider is None or not getattr(self.decider, "available", False):
            return None
        try:
            d = self.decider.decide(raw)
        except Exception:
            return None                      # a dead decider must not 500
        if d is None or d.confidence < self.settings.decider_min_confidence:
            return None
        slots = dict(d.slots)
        # `when` is what every downstream handler reads for "today"/"tomorrow",
        # and the model has no reason to know that name. Fill it from the text
        # rather than trusting it to guess our schema.
        slots.setdefault("when", _when_of(raw.lower()))
        return Route(d.intent, round(d.confidence, 2), d.reason,
                     slots=slots, cost=d.list_cost_usd, by_model=True)

    # -----------------------------------------------------------------
    def route(self, text: str, *, message_type: str = "text",
              has_location: bool = False, has_file: bool = False) -> Route:
        """Classify a message. `message_type` short-circuits content analysis."""
        raw = (text or "").strip()

        # --- calling an agent by name ------------------------------------
        # A *name on its own line*, and nothing looser. This is the same class
        # of rule as `/undo`: a literal, closed set, matching a command rather
        # than a meaning. The looseness is deliberately not here — "ask friday
        # what she thinks" is a sentence about Friday, not a summons, and the
        # cost of getting that wrong is that a question silently gets answered
        # by a different agent with a different memory. An optional greeting
        # verb is allowed because that is how anyone actually says it out loud.
        m = SUMMON_RE.match(raw.strip())
        if m:
            return Route(SUMMON, 1.0, f"summoned {m.group('name')}",
                         slots={"name": m.group("name").lower()})

        # Above `ADDRESSED` on purpose. That rule reads a message consisting of
        # nothing but a name as a greeting, which was right when there was one
        # name and wrong now that a name is also how an agent is called: typing
        # "V" while EDITH is answering has to bring V back, not return a
        # good-evening from EDITH. A summons for whoever is already speaking
        # falls through to the same greeting anyway — see `engine._summon`.

        # Being addressed by name is the strongest "this is not a note" signal
        # there is. Strip it so every rule below sees the actual request, and
        # remember that it was there — it decides the fallback at the bottom.
        addressed = False
        m = ADDRESSED.match(raw)
        if m:
            if m.end() >= len(raw):          # the whole message was "hey V"
                return Route(SMALLTALK, 0.98, "greeting — answered locally",
                             slots={"flavour": "greeting"})
            raw, addressed = raw[m.end():].strip(), True

        low = raw.lower()

        # --- fast path: classify on message type, not content -----------
        if has_location:
            return Route(CAPTURE_PLACE, 0.99, "message carried a location pin")
        if message_type == "voice":
            pass  # transcribed upstream; fall through to content rules
        if has_file or message_type in ("document", "photo", "audio"):
            return Route(CAPTURE_LINK if URL_RE.search(raw) else CAPTURE_NOTE,
                         0.9, f"{message_type} attachment — stored and filed")

        if not raw:
            return Route(HELP, 1.0, "empty message")

        # --- exact commands (the few that earn their keep) -------------
        for cmd, intent in (("/status", STATUS), ("/undo", UNDO),
                            ("/brief", BRIEF), ("/nudges", NUDGE_CHECK),
                            ("/help", HELP)):
            if low.startswith(cmd):
                return Route(intent, 1.0, f"explicit {cmd}")

        # Bare "undo" had no rule at all — only `/undo` did — so typing it
        # scored nothing, fell to the bottom of the chain, and got *appended to
        # today's note as a thought*. Three times in a row, each one another
        # line of the word "Undo" in the daily log. The button under a write
        # even emits this exact string, so the affordance we advertise was the
        # one input the router couldn't read.
        if UNDO_EXACT.match(low):
            return Route(UNDO, 1.0, "undoes the last write")

        if low.strip(" .!") in HELP_EXACT:
            return Route(HELP, 1.0, "asked what it can do")

        # --- "no, that was a lyric" — a command with one slot ------------
        # This looks like the meaning-matching the rest of the file just lost,
        # and it isn't: the trailing word must be one of fourteen literal kinds
        # in `REFILE_KINDS` or the rule declines, so "no, that was pointless"
        # falls through to the model. It stays local because it operates on
        # state the agent has no tool for — the last thing V wrote.
        refile = REFILE_RE.match(raw)
        if refile:
            word = (refile.group(1) or refile.group(2) or "").lower()
            if word in REFILE_KINDS:
                return Route(REFILE, 0.95, "moves what I just filed",
                             slots={"intent": REFILE_KINDS[word]})

        # --- memory commands, not memory content -------------------------
        # All that survives of the phrase-matching memory layer. `/memory` and
        # `/forget 3` are fixed strings; what to *store* is decided by the model
        # through `vault_memory_write`, because a prefix like "no" or "i want
        # you to" says nothing about whether a sentence is a lesson or a retry.
        # Reading it as a lesson is how the store came to hold two verbatim
        # requests and inject them into every prompt. See `memory.read_command`.
        from .memory import read_command  # local: memory imports config only

        cmd = read_command(raw)
        if cmd:
            if cmd[0] == "list":
                return Route(RECALL, 1.0, "asked what I remember")
            return Route(TEACH, 1.0, "asked me to forget something",
                         slots={"forget": cmd[1]})

        # --- smalltalk: answered locally, never escalated ----------------
        # Whole-message patterns over a closed word list — "hi", "thanks",
        # "goodnight". A greeting cannot turn out to have been a request, so
        # this stays a rule; paying an agent to say good morning is the one
        # thing the free tier was unambiguously right about.
        for flavour, pattern in SMALLTALK_PATTERNS:
            if pattern.match(raw):
                return Route(SMALLTALK, 0.98, f"{flavour} — answered locally",
                             slots={"flavour": flavour})

        # A bare identifier — "ISSUE-139", "MR-747", "[[some note]]" — names
        # one note and asks for it. `NOTE_REF` is anchored on both ends, so
        # there is no sentence for it to misread; this is a lookup key, not an
        # interpretation, and answering it costs nothing.
        if NOTE_REF.match(raw):
            return Route(QUERY, 0.95, "names a specific note",
                         slots=self._slots(QUERY, raw))

        # --- everything else: the model reads it -------------------------
        # Not "the model classifies it" — the model *handles* it, with the
        # vault as tools. The distinction is the fix: a classifier could only
        # pick a label from a fixed list and fill slots, so a message asking
        # for a web search and a written document had no label to land on and
        # became the nearest one, `task.add`.
        if self.agentic:
            return Route(AGENT, 0.99,
                         "handed to the model with tools — it decides what "
                         "this needs")

        return self.route_offline(raw, low, addressed=addressed)

    # -----------------------------------------------------------------
    def route_offline(self, raw: str, low: str = "",
                      *, addressed: bool = False) -> Route:
        """The old rule cascade. Runs only when no reasoning shell is reachable.

        Kept whole and unchanged rather than deleted. It is wrong often enough
        that it should not decide anything a model could decide instead, but a
        V with no shell that files a workout to `04 - Gym` on a keyword is worth
        a great deal more than a V with no shell that shrugs. Everything here
        was written against real misroutes and the comments explaining them are
        the record of why each rule has the shape it has.
        """
        low = low or raw.lower()

        # --- learned corrections beat every heuristic ------------------
        learned = self._match_example(low)
        if learned:
            intent, sim = learned
            return Route(intent, min(0.97, 0.7 + sim * 0.3),
                         "matches a past correction you made")

        # --- rules that are cheap and narrow enough to trust -------------
        words = low.split()
        if STANDUP.search(low):
            return Route(QUERY, 0.9, "asks for the standup")
        if len(words) <= 6 and any(s in low for s in STATUS_WORDS) \
                and any(n in words for n in SCHEMA_NOUNS):
            return Route(QUERY, 0.9, "names a note type and a status")

        # --- everything content-shaped is the model's call ---------------
        # Below this line every rule is a guess about meaning rather than
        # form, and guesses about meaning are what produced a calendar table
        # in answer to "i should set a fixed time for my 1on1s". The model
        # gets first refusal; the heuristics stay underneath it, unchanged,
        # for when there is no model or it declines to commit.
        decided = self._ask_model(raw)
        if decided:
            return decided

        add = ADD_TASK_RE.match(raw)
        if add:
            what = add.group("what").strip(" .!\"'")
            if what:
                return Route(TASK_ADD, 0.9, "adds a task",
                             slots={"text": what, "when": _when_of(low)})

        # --- compound requests: refuse to answer them cheaply -----------
        # "based on my last 1on1, update my goals and create a todo list" used
        # to match the word "goals", print a two-row table of goal note titles,
        # and stop — instantly, for free, and having done none of the three
        # things asked. A structured query can only ever answer one clause; a
        # message with an action verb *and* a second clause needs the agent, and
        # the cheap path must decline it rather than half-serve it.
        if _is_compound(raw, low):
            return Route(COMPOUND, 0.9,
                         "asks for several things at once — needs reasoning",
                         slots={"verbs": ACTION_VERBS.findall(raw)})

        # --- tasks ------------------------------------------------------
        # Checked before the generic question rules so "what are my priorities"
        # resolves to the ranked task list rather than a keyword search.
        if BEHIND_RE.search(low):
            return Route(TASKS, 0.9, "asks how far behind you are",
                         slots={"view": "backlog"})
        if PRIORITY_RE.search(low):
            return Route(TASKS, 0.9, "asks what matters most",
                         slots={"view": "priorities", "when": _when_of(low)})
        if OVERDUE_RE.search(low) and TASKLIST_RE.search(low):
            return Route(TASKS, 0.88, "asks for late tasks",
                         slots={"view": "overdue"})
        done = DONE_RE.search(raw)
        if done:
            target = (done.group(1) or done.group(2) or "").strip(" .!\"'“”")
            # "mark the debugging task done" names the task "debugging", not
            # "debugging task" — the trailing noun is grammar, and leaving it in
            # costs a match against the checkbox text.
            target = re.sub(r"\s+(task|item|todo|one)$", "", target, flags=re.I)
            if target:
                return Route(TASK_DONE, 0.9, "marks a task finished",
                             slots={"target": target})
        if TASKLIST_RE.search(low) and not any(
                h in low for h in ("remind me", "add a task", "new task")):
            # A judgement about tasks is not a request for the task list.
            if JUDGEMENT_RE.search(low):
                return Route(COMPOUND, 0.85,
                             "asks me to judge, not to list",
                             slots={"about": "tasks"})
            return Route(TASKS, 0.8, "asks about your tasks",
                         slots={"view": "open", "when": _when_of(low),
                                "source": _task_source(low)})

        # --- calendar ---------------------------------------------------
        if NEXT_EVENT_RE.search(low):
            return Route(CALENDAR, 0.9, "asks what's next",
                         slots={"view": "next"})
        # "standup" is a vault report here, not a meeting to look up, and it
        # has its own handler further down. Checked first because the calendar
        # rule below would otherwise swallow the word.
        if CALENDAR_RE.search(low) and not STANDUP.search(low):
            # A time *and* a date with no question mark is someone telling the
            # calendar about an event, not asking it.
            if (WHEN_AT.search(raw) and CLOCK.search(raw)
                    and not raw.endswith("?")
                    and not low.startswith(QUESTION_WORDS)):
                return Route(SCHEDULE, 0.88, "states a dated appointment",
                             slots={"when": _when_of(low)})
            return Route(CALENDAR, 0.85, "asks about the calendar",
                         slots={"when": _when_of(low)})

        scores: dict[str, float] = {}
        why: dict[str, str] = {}

        def bump(intent: str, amount: float, reason: str) -> None:
            scores[intent] = scores.get(intent, 0.0) + amount
            why.setdefault(intent, reason)

        # --- reminders (must outrank plain tasks) ----------------------
        if any(h in low for h in TASK_HINTS):
            if REMINDER_TIME.search(low):
                bump(REMINDER, 0.85, "a task with a time in it")
            else:
                bump(CAPTURE_TASK, 0.6, "reads like a task")
        elif REMINDER_TIME.search(low) and len(raw.split()) < 25:
            bump(REMINDER, 0.5, "contains a time expression")

        # --- questions -------------------------------------------------
        # This has to be generous. A lookup misrouted as a capture *writes a
        # note*, which is a far worse failure than a capture misrouted as a
        # lookup — that one just shows you search results.
        words = low.split()
        if raw.endswith("?") or low.startswith(QUESTION_WORDS):
            bump(QUERY, 0.8, "phrased as a question")
        elif any(w in words[:3] for w in RETRIEVAL_VERBS):
            bump(QUERY, 0.7, "starts with a retrieval verb")
        elif words and words[0] in SUPERLATIVES:
            bump(QUERY, 0.65, "asks for the best/worst of something")
        elif any(w in words for w in RETRIEVAL_VERBS | SUPERLATIVES):
            bump(QUERY, 0.45, "contains lookup language")

        # Verbless lookups: "open MRs", "stale reviews", "overdue assignments".
        # Requiring *both* a status word and a schema noun keeps this tight —
        # "bench 3x8 at 80kg" and "spent 250k on groceries" match neither.
        if len(words) <= 6 and any(s in low for s in STATUS_WORDS) \
                and any(n in words for n in SCHEMA_NOUNS):
            bump(QUERY, 0.75, "names a note type and a status")
        if NOTE_REF.match(raw):
            bump(QUERY, 0.9, "names a specific note")
        if STANDUP.search(low):
            bump(QUERY, 0.8, "asks for the standup")
        if WHERE_TO.search(low):
            bump(QUERY, 0.75, "asks where to go")

        # --- links -----------------------------------------------------
        if URL_RE.search(raw):
            bump(CAPTURE_LINK, 0.85 if len(raw.split()) < 30 else 0.5,
                 "contains a URL")

        # --- typed captures --------------------------------------------
        # A topic word ("cafe", "bench", "spent") tells you the *subject*, not
        # whether the user is filing or asking. When a lookup signal is already
        # present, the subject must not be allowed to outvote it.
        asking = scores.get(QUERY, 0.0)
        damp = 0.35 if asking >= 0.6 else (0.7 if asking else 1.0)

        for hints, intent, reason in (
            (LYRIC_HINTS, CAPTURE_LYRIC, "mentions songwriting"),
            (DECISION_HINTS, CAPTURE_DECISION, "states a decision"),
            (WORKOUT_HINTS, CAPTURE_WORKOUT, "mentions training"),
            (EXPENSE_HINTS, CAPTURE_EXPENSE, "mentions money"),
            (PLACE_HINTS, CAPTURE_PLACE, "mentions a venue"),
            (IDEA_HINTS, CAPTURE_IDEA, "reads like an idea"),
            (PERSON_HINTS, CAPTURE_PERSON, "mentions meeting someone"),
        ):
            if any(h in low for h in hints):
                bump(intent, 0.55 * damp, reason)

        if WORKOUT_SETS.search(raw):
            bump(CAPTURE_WORKOUT, 0.35 * damp, "contains sets/reps/weight")
        if MONEY.search(raw):
            bump(CAPTURE_EXPENSE, 0.3 * damp, "contains an amount of money")
        if MET_PERSON.search(raw):
            bump(CAPTURE_PERSON, 0.55 * damp, "names someone you saw")

        # A short line with no verb and heavy imagery is usually a lyric.
        if (CAPTURE_LYRIC not in scores and 3 <= len(raw.split()) <= 14
                and "\n" in raw and not raw.endswith("?")):
            bump(CAPTURE_LYRIC, 0.25, "short multi-line fragment")

        if not scores:
            # Nothing matched. That is not a reason to interrogate the user —
            # it is a fork: either they are talking to V, in which case the
            # model answers, or they are thinking out loud, in which case it
            # goes in today's note and can be moved afterwards.
            if addressed or TALK_RE.search(low):
                return Route(CHAT, 0.9, "talking to me — needs a real answer",
                             slots={"addressed": addressed})
            return Route(CAPTURE_NOTE, 0.6,
                         "nothing specific matched — filing to today's note")

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top, score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        # Confidence is the margin, not the raw score: two strong competing
        # signals should *lower* certainty, not raise it.
        confidence = min(0.97, score * (1.0 - 0.5 * runner / max(score, 0.01)))

        # A weak capture guess on a message addressed to V is almost always a
        # topic word inside a sentence aimed at the assistant — "hey V, was the
        # café thing worth it?" is not a place note.
        if (addressed and top in CAPTURE_INTENTS
                and confidence < self.settings.confidence_threshold):
            return Route(CHAT, 0.85, "you were talking to me, not filing a note",
                         slots={"addressed": True})

        return Route(top, round(confidence, 2), why.get(top, "heuristic match"),
                     slots=self._slots(top, raw),
                     alternatives=[(i, round(s, 2)) for i, s in ranked[1:4]])

    # -----------------------------------------------------------------
    def _slots(self, intent: str, text: str) -> dict:
        slots: dict = {}
        if intent == CAPTURE_LINK:
            m = URL_RE.search(text)
            if m:
                slots["url"] = m.group(0)
        if intent == REMINDER:
            m = REMINDER_TIME.search(text)
            if m:
                slots["when_text"] = m.group(0)
        if intent == CAPTURE_WORKOUT:
            slots["sets"] = WORKOUT_SETS.findall(text)
        if intent == CAPTURE_EXPENSE:
            m = MONEY.search(text)
            if m:
                slots["amount_text"] = m.group(0)
                amount = amount_to_toman(m.group(0))
                if amount is not None:
                    slots["amount_toman"] = amount
            low = text.lower()
            if any(h in low for h in INCOME_HINTS):
                slots["direction"] = "income"
            elif any(h in low for h in SAVINGS_HINTS):
                slots["direction"] = "savings"
            else:
                slots["direction"] = "expense"
            cat = next((c for c, kws in CATEGORY_KEYWORDS.items()
                       if any(k in low for k in kws)), None)
            if cat:
                slots["category"] = cat
        return slots

    # ---- learning from corrections ----------------------------------
    def _match_example(self, low: str) -> tuple[str, float] | None:
        best: tuple[str, float] | None = None
        tokens = set(low.split())
        if not tokens:
            return None
        for ex_text, ex_intent in self._examples:
            ex_tokens = set(ex_text.split())
            if not ex_tokens:
                continue
            sim = len(tokens & ex_tokens) / len(tokens | ex_tokens)
            if sim > 0.55 and (best is None or sim > best[1]):
                best = (ex_intent, sim)
        return best

    def learn(self, text: str, intent: str) -> None:
        """Record a correction. This is the whole self-improvement loop."""
        path = _examples_path(self.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"text": text.strip().lower(), "intent": intent},
                                ensure_ascii=False) + "\n")
        self._examples.append((text.strip().lower(), intent))

    def accuracy_report(self) -> dict:
        by_intent: dict[str, int] = {}
        for _, intent in self._examples:
            by_intent[intent] = by_intent.get(intent, 0) + 1
        return {"corrections": len(self._examples), "by_intent": by_intent}


def _when_of(low: str) -> str:
    """The day a task/calendar question is about, as a keyword."""
    for word in ("tomorrow", "today", "tonight", "this week", "next week",
                 "yesterday"):
        if word in low:
            return word
    if "فردا" in low:
        return "tomorrow"
    if "امروز" in low:
        return "today"
    return ""


def _is_compound(raw: str, low: str) -> bool:
    """Does this message ask for more than a lookup?

    Two independent signals, both required, because either alone is too eager:
    an action verb ("update", "summarise", "create") says the user wants
    something *changed*, and a second clause says one query cannot cover it.
    "update my goals and create a todo list" trips both. "show me open issues"
    trips neither. "summarise this" has the verb but no second clause, and is
    left to the ordinary rules.
    """
    if len(raw.split()) < 5:
        return False
    verbs = {v.lower() for v in ACTION_VERBS.findall(raw)}
    if not verbs:
        return False
    # Two different action verbs is a compound on its own.
    if len(verbs) >= 2:
        return True
    # One verb plus a joining word, where the join is not the last word.
    m = CONJOINED.search(low)
    if m and m.end() < len(low) - 3:
        return True
    # "based on X, do Y" — a premise clause the query layer cannot honour.
    return bool(re.search(r"\b(based on|according to|using|from) (my|our|the)\b",
                          low))


def _examples_path(s: Settings) -> Path:
    return s.state_dir / "routing_examples.jsonl"


def _load_examples(s: Settings) -> list[tuple[str, str]]:
    path = _examples_path(s)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            out.append((d["text"], d["intent"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return out
