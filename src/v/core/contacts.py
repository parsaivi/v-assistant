"""Contact lookup — the one read into `10 - Personal CRM/`, and it returns no prose.

Asked "give me today's calendar for Arash", V said Arash had not shared his
calendar and asked Parsa for his email address to proceed. Parsa had already
written that address down. It is in `10 - Personal CRM/Arash Yousefi.md`, in
the vault V was answering from, and V asked him to type it out anyway.

Nothing malfunctioned. `10 - Personal CRM` is in `Settings.private_folders`,
so `mcp/guard.py` blinds every tool to it, `llm.py` blocks its bodies from
egress, and `identity.py` tells V not to reach around either with the shell.
`gcal.resolve()` went further and said so in its docstring: names resolve
against Google's calendar list, never through the CRM. Every layer did its
job and the answer was still wrong, because the rule protected the *folder*
when what deserves protecting is the *prose*.

A CRM note holds what Parsa thinks of someone, what they are going through,
what to buy them. That is the private part and none of it is here. An email
address is the opposite kind of fact: a routing token he wrote down precisely
so he would not have to remember it, whose only use is being handed to
something that routes. Withholding it from the assistant he asked to route
does not protect him from anything.

So this module is a deliberate, narrow hole in the wall, and every clause of
narrow is load-bearing:

  - **Identifiers only.** Addresses and phone numbers pulled by regex out of
    labelled fields. `Contact` carries a name, a path, and those two lists.
    It has no body, no `tier`, no `last_contact`, no notes — there is no path
    through this file that can return a sentence Parsa wrote.
  - **Pull, never sweep.** Nothing walks the folder at startup, nothing
    indexes it, and no tool lists its contents. You get a person by naming
    that person. `lookup("")` returns nothing, on purpose.
  - **Every other door stays shut.** `vault_search`, `vault_read`,
    `vault_query`, the nudge scrubber and the shell rules are all unchanged:
    still blind to this folder, still blocked. This module is not a general
    exception to the privacy tier; it is one field type escaping it.

The identifier does leave the machine — that is the point of asking. It goes
to Google's freeBusy for "is Arash free", or into an invitation's attendee
list, and it goes because Parsa's question sent it there. What does not go is
the note it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import notes
from .config import Settings, settings as default_settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Deliberately loose on separators and strict on digit count. Parsa writes
# Iranian mobiles as +98 912 ..., 0912-..., and ۰۹۱۲... in the same folder;
# requiring seven digits is what keeps a date out of the results.
PHONE_RE = re.compile(r"\+?[\d۰-۹][\d۰-۹ ()\-.]{5,}"
                      r"[\d۰-۹]")
MIN_PHONE_DIGITS = 7

# Matched case-folded against a field label, so one entry covers `Email`,
# `**Email:**` and `| Email |`.
EMAIL_KEYS = ("email", "e-mail", "mail", "gmail", "work email", "work_email",
              "personal email", "personal_email", "ایمیل")
PHONE_KEYS = ("phone", "mobile", "cell", "tel", "telephone", "number",
              "whatsapp", "telegram", "موبایل", "تلفن", "شماره")

# One line, one field. Written to survive all four shapes the same folder
# actually uses — a frontmatter key, `- **Email:** x`, `Email: x`, and a
# `| **Email** | x |` row of the template's Quick Profile table — because a
# parser that only knows the template's shape stops working the first time
# Parsa types a field by hand, which is how the one address in there got
# written in the first place.
_FIELD = re.compile(
    r"""^\s*
        (?:[-*+]\s+)?                          # bullet
        \|?\s*                                 # opening table pipe
        \**\s*(?P<key>[A-Za-z؀-ۿ][A-Za-z؀-ۿ _/'-]{1,23}?)
        \s*\**\s*
        [:|]                                   # 'Key:' or a table cell break
        \s*(?P<val>.*?)\s*\|?\s*$
    """,
    re.VERBOSE)


@dataclass(frozen=True)
class Contact:
    """A person, reduced to the parts that can be dialled or addressed.

    Frozen and deliberately thin. Anything added here is something a model
    can see, so the bar for a new field is that a tool would send it
    somewhere — not that it happens to be in the note.
    """

    name: str
    rel: str
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()

    @property
    def email(self) -> str | None:
        return self.emails[0] if self.emails else None

    def render(self) -> str:
        bits = list(self.emails) + list(self.phones)
        return f"{self.name} — {', '.join(bits)}" if bits else self.name


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s_.\-]+", text.lower()) if t]


def _matches(query: str, candidate: str) -> bool:
    """"arash" finds "Arash Yousefi"; "ali" does not find "Alireza".

    Whole tokens only. Substring matching reads as helpful right up to the
    moment it resolves a half-typed name to the wrong person and mails them.
    """
    q, c = _tokens(query), _tokens(candidate)
    return bool(q) and all(t in c for t in q)


def _names(note: notes.Note) -> list[str]:
    out = [note.path.stem]
    aliases = note.meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    out += [str(a) for a in aliases if str(a).strip()]
    return out


def _harvest(text: str, keys: tuple[str, ...], pattern: re.Pattern) -> list[str]:
    """Values of fields whose label is one of `keys`, matched by `pattern`.

    Label-gated, never a bare scan of the note. A CRM note quotes other
    people's addresses — a forwarded thread, a recruiter, someone's
    ex-colleague — and a free scan for anything @-shaped would hand back
    whichever one appeared first and call it this person's.
    """
    out: list[str] = []
    for line in text.splitlines():
        m = _FIELD.match(line)
        if not m:
            continue
        key = m.group("key").strip().strip("*").lower()
        if key not in keys:
            continue
        for hit in pattern.findall(m.group("val")):
            hit = hit.strip()
            if hit and hit not in out:
                out.append(hit)
    return out


def _digits(value: str) -> int:
    return sum(c.isdigit() for c in value)


def read(path: Path, settings: Settings | None = None) -> Contact | None:
    """Parse one CRM note down to its identifiers. Returns None if unreadable."""
    s = settings or default_settings
    try:
        note = notes.parse(path, s.vault)
    except (OSError, ValueError):
        return None

    # Frontmatter first: a typed field beats one dug out of the prose.
    front = "\n".join(f"{k}: {v}" for k, v in (note.meta or {}).items()
                      if isinstance(v, (str, int, float)))
    emails = _harvest(front, EMAIL_KEYS, EMAIL_RE)
    phones = _harvest(front, PHONE_KEYS, PHONE_RE)
    for e in _harvest(note.body, EMAIL_KEYS, EMAIL_RE):
        if e not in emails:
            emails.append(e)
    for p in _harvest(note.body, PHONE_KEYS, PHONE_RE):
        if p not in phones:
            phones.append(p)

    phones = [p for p in phones if _digits(p) >= MIN_PHONE_DIGITS]
    return Contact(name=path.stem, rel=str(path.relative_to(s.vault)),
                   emails=tuple(emails), phones=tuple(phones))


def _is_person(path: Path) -> bool:
    """Dashboards and index notes live in the folder too and are not people."""
    stem = path.stem
    return not (stem.startswith("_") or re.match(r"^\d+[_ -]", stem))


def lookup(who: str, settings: Settings | None = None) -> list[Contact]:
    """Every CRM entry whose name matches `who`, identifiers only.

    Returns all of them rather than picking. Two Alirezas in this folder is
    not hypothetical, and answering confidently about the wrong one is the
    failure this whole lookup exists to avoid repeating.
    """
    s = settings or default_settings
    if not who or not who.strip():
        return []
    folder = s.vault / s.folders.crm
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.md")):
        if not _is_person(path):
            continue
        try:
            note = notes.parse(path, s.vault)
        except (OSError, ValueError):
            continue
        if note.meta.get("type") not in (None, "person"):
            continue
        if not any(_matches(who, n) for n in _names(note)):
            continue
        c = read(path, s)
        if c is not None:
            out.append(c)
    return out


def emails_for(who: str, settings: Settings | None = None) -> list[str]:
    """Flat list of addresses for a name — the shape `gcal` actually wants."""
    return [e for c in lookup(who, settings) for e in c.emails]
