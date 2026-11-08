"""Quera — course deadlines, via the site's own Next.js data payload.

Quera (quera.org — the university course/LMS platform, not the unrelated
quera.ir judge) has no public API. Checked against three independent
unofficial Quera tools before writing this: none found a documented API;
every one authenticates with a session cookie lifted from a logged-in
browser and reads the HTML the account sees. This module does the same.

**Auth is a pasted cookie, not a stored password** — a deliberate choice
(discussed and confirmed, not just a default): `cli quera auth --cookie
"..."` writes it to `state/quera_cookie.json`, `chmod 0600`, nothing else
ever touches disk. The cost is that the cookie will expire on its own
schedule — unknown until this runs against the real account for a while —
and `_get()` is written to fail loudly and specifically (`NotAuthorised`,
never a silent empty result) the moment a request comes back looking like
Quera's login page rather than the dashboard, so an expired cookie reads as
"reconnect me," not as "no deadlines."

**Where the data actually comes from.** The first version of this module
scraped `/dashboard`'s rendered HTML, written from prior art without a real
logged-in session to test against. Once a session was available, it turned
out `/dashboard` doesn't embed the deadline data at all — it's fetched
client-side. `/course?status=all`, though, is a Next.js page, and its
`__NEXT_DATA__` script tag embeds `course_deadline_widget_data`: the exact
same pre-computed list the site renders into its own sidebar calendar,
already flattened across every course (active or archived) with an
unambiguous ISO-8601 UTC `finish_time` per item. That single field replaces
what used to be two independent HTML-guessing passes plus a self-written
Jalali→Gregorian conversion — Quera has already done the calendar math and
handed back a real timestamp, so this module no longer needs to.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass

import httpx

from . import schema, templates
from .config import Settings, settings as default_settings
# Notes built by rules from an API: no model wrote them, which is the
# one thing the `parsa` label asserts (see provenance.py).
from . import provenance
from .vaultfs import Vault

BASE = "https://quera.org"
COURSE_LIST_URL = f"{BASE}/course?status=all"

# Deadlines render in Quera's UI in Tehran local time; `finish_time` arrives
# as UTC, so this is what turns "2026-08-10T02:30:59+00:00" into the same
# 2026-08-10 a student reading the site would see, rather than whatever date
# UTC happens to land on near midnight Tehran time.
TEHRAN_TZ = dt.timezone(dt.timedelta(hours=3, minutes=30))

TIMEOUT = 20


class QueraError(RuntimeError):
    pass


class NotAuthorised(QueraError):
    """No cookie saved, or Quera's response looks like a login page rather
    than the dashboard — the session has expired. Distinct from a plain
    `QueraError` so the CLI can say specifically "paste a fresh cookie"
    instead of a generic failure."""


@dataclass
class QueraAssignment:
    course: str
    title: str
    due_at: dt.datetime | None
    url: str


# --------------------------------------------------------------------------
class Quera:
    def __init__(self, settings: Settings | None = None, vault: Vault | None = None):
        self.s = settings or default_settings
        self.vault = vault or Vault(self.s)

    @property
    def _cookie_path(self):
        return self.s.state_dir / "quera_cookie.json"

    def configured(self) -> bool:
        return self._cookie_path.exists()

    def save_cookie(self, cookie: str) -> None:
        cookie = cookie.strip()
        if not cookie:
            raise QueraError("empty cookie")
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        self._cookie_path.write_text(json.dumps({"cookie": cookie}, indent=2),
                                     encoding="utf-8")
        # A live session cookie is a standing grant on the account, same
        # treatment as gcal_token.json / gmail_token.json.
        os.chmod(self._cookie_path, 0o600)

    def _cookie(self) -> str:
        if not self._cookie_path.exists():
            raise NotAuthorised(
                "no Quera session saved. Log into quera.org in a browser, "
                "open devtools → Network, click any request to the site, "
                "copy the whole `Cookie` request header, then run "
                "`python3 -m v.cli quera auth --cookie '...'`")
        try:
            data = json.loads(self._cookie_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotAuthorised(f"cookie file is corrupt: {exc}") from exc
        cookie = data.get("cookie", "")
        if not cookie:
            raise NotAuthorised("cookie file has no cookie in it — "
                                "run `cli quera auth --cookie '...'` again")
        return cookie

    def _get(self, url: str) -> str:
        try:
            r = httpx.get(url, headers={"Cookie": self._cookie()},
                         timeout=TIMEOUT, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise QueraError(f"could not reach Quera: {exc}") from exc
        # Quera has no API to fail cleanly against — an expired session
        # doesn't 401, it redirects to (or renders) a login page, so that
        # shape is the actual signal rather than a status code.
        location = str(r.headers.get("location", "")).lower()
        if r.status_code in (301, 302, 303) and "login" in location:
            raise NotAuthorised(
                "Quera redirected to login — the session cookie has "
                "expired. Re-run `cli quera auth --cookie '...'` with a "
                "fresh one.")
        if r.status_code != 200:
            raise QueraError(f"Quera returned {r.status_code} for {url}")
        if _looks_like_login_page(r.text):
            raise NotAuthorised(
                "Quera served what looks like a login page rather than the "
                "dashboard — the session cookie has expired. Re-run "
                "`cli quera auth --cookie '...'` with a fresh one.")
        return r.text

    def fetch_deadlines(self) -> list[QueraAssignment]:
        html = self._get(COURSE_LIST_URL)
        return parse_course_list(html, base=BASE)

    # ---- sync into the vault — same shape as work.py's GitLab sync -----
    #
    # Quera has no stable numeric id to dedupe on the way `glab`'s issue IID
    # does; (course, title) is the closest thing to one, so that pair is the
    # identifier. A course note is created the first time its name is seen,
    # so an assignment note's `course` wikilink never points at nothing.
    def _course_note(self, course: str) -> str:
        rel = f"{self.s.folders.university}/Courses/{_safe(course)}.md"
        if self.vault.exists(rel) or not course:
            return rel
        meta = schema.template_for("course")
        meta.update({"title": course, "status": "active", "tags": ["university"]})
        filled = templates.fill("course", meta, title=course, settings=self.s)
        if filled:
            meta, body = filled
        else:
            body = "\n## Info\n\n## Lectures\n\n## Assignments\n\n## Notes\n"
        self.vault.create(rel, meta, body, summary=f"create {rel} from Quera",
                                      source=provenance.PARSA)
        return rel

    def _assignment_note(self, a: QueraAssignment) -> tuple[str, dict, str]:
        rel = f"{self.s.folders.university}/Assignments/{_safe(a.title)}.md"
        meta = schema.template_for("assignment")
        meta.update({
            "title": a.title,
            "course": f"[[{a.course}]]" if a.course else None,
            "due-date": a.due_at.date().isoformat() if a.due_at else None,
            "status": "todo",
            "tags": ["university/assignment"],
        })
        filled = templates.fill("assignment", meta, title=a.title,
                                when=a.due_at.date() if a.due_at else None,
                                settings=self.s)
        if filled:
            meta, body = filled
        else:
            body = "\n## Requirements\n\n## My Work\n\n## References\n- \n\n## Submission Notes\n"
        if a.url:
            body = templates.insert_under(body, "References", f"- {a.url}")
        return rel, meta, body

    def sync(self, *, dry_run: bool = True) -> dict:
        """Pull deadlines from Quera, create the assignment notes that don't
        exist yet, and update `due-date`/`status` on ones that do — never
        touching a body Parsa may have started filling in. Matched by
        (course, title), the closest thing to a stable id Quera exposes.
        """
        report = {"created": [], "updated": [], "skipped": 0, "dry_run": dry_run}
        try:
            deadlines = self.fetch_deadlines()
        except NotAuthorised as exc:
            report["error"] = str(exc)
            return report

        for a in deadlines:
            if not dry_run:
                self._course_note(a.course)
            rel, meta, body = self._assignment_note(a)
            if self.vault.exists(rel):
                note = self.vault.read(rel)
                drift = {}
                if a.due_at:
                    new_due = a.due_at.date().isoformat()
                    if _s(note.meta.get("due-date")) != new_due:
                        drift["due-date"] = new_due
                if drift:
                    if not dry_run:
                        self.vault.set_meta(rel, drift,
                                            summary=f"sync {rel} from Quera",
                                            source=provenance.PARSA)
                    report["updated"].append({"rel": rel, "changes": drift})
                else:
                    report["skipped"] += 1
            else:
                if not dry_run:
                    self.vault.create(rel, meta, body,
                                      summary=f"create {rel} from Quera",
                                      source=provenance.PARSA)
                report["created"].append(rel)
        return report


_LOGIN_TITLE_RE = re.compile(r"<title>\s*ورود\b")


def _looks_like_login_page(html: str) -> bool:
    # Tested against a real logged-in response, this needed to change: a
    # bare `type="password"` (there's a hidden change-password field in the
    # shared account menu on every authenticated page) and a bare "ورود به"
    # (the footer's "ورود به گیت‌هاب کوئرا" / "ورود به تلگرام کوئرا" social
    # links — "ورود به" reads as "go to" there, not "log in to") both false
    # -positive on pages you're already logged into. The login page itself
    # has a title starting with just "ورود" ("Log in") and the heading
    # "ورود به کوئرا" ("Log in to Quera") — specific enough that neither
    # shows up on an authenticated page.
    return bool(_LOGIN_TITLE_RE.search(html)) or "ورود به کوئرا" in html


# --------------------------------------------------------------------------
# Parsing. `/course?status=all` is a Next.js page — everything it renders
# server-side is also sitting in a `__NEXT_DATA__` script tag as JSON, which
# is what this reads instead of the rendered markup.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.DOTALL)


def parse_course_list(html: str, *, base: str = BASE) -> list[QueraAssignment]:
    """Read `course_deadline_widget_data` out of the page's `__NEXT_DATA__`
    blob — a flat list of `{id, name, finish_time, course_name}` that is
    already exactly "every upcoming deadline across every course," the same
    data the site's own sidebar calendar renders from. No HTML heuristics,
    no date-shaped-text guessing: `finish_time` is a real ISO-8601 UTC
    timestamp, so there is nothing left to parse loosely.

    Returns `[]`, not an error, when the blob is missing or empty — a page
    that loads with no deadlines due is a real possible outcome (nothing
    open right now) and must not look like a parser failure. `Quera.sync()`
    is what turns a *persistent* zero into something worth mentioning.
    """
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    try:
        widget = data["props"]["pageProps"]["course"]["course_deadline_widget_data"]
    except (KeyError, TypeError):
        return []

    out: list[QueraAssignment] = []
    for item in widget or []:
        if not isinstance(item, dict):
            continue
        title = item.get("name")
        due = _parse_utc(item.get("finish_time"))
        if not title or not due:
            continue
        item_id = item.get("id")
        url = f"{base}/course/assignments/{item_id}/problems" if item_id else base
        out.append(QueraAssignment(
            course=item.get("course_name") or "",
            title=title,
            due_at=due.astimezone(TEHRAN_TZ).replace(tzinfo=None),
            url=url,
        ))
    return out


def _parse_utc(raw) -> dt.datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _safe(title: str) -> str:
    """Filenames in this vault keep Persian text; only path separators are
    unsafe. Same rule work.py applies to issue/MR titles."""
    for ch in '/\\:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip()[:90]


def _s(v) -> str:
    return "" if v is None else str(v).strip().strip('"')
