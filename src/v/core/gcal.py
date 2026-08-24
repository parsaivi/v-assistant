"""Google Calendar — read and write, Parsa's own and other people's.

The vault calendar at `01 - Daily Notes/Calendar/` stays the source of truth
for what Parsa has *written down*. This is different: it is the live account,
where the invitations actually arrive and where writing an event is what makes
other people see it. `calendar.py` already imports from it one-way through a
private iCal URL; that feed is read-only, covers one calendar, and cannot see
anyone else's. So "can you give me today's calendar for Arash" had no answer
but "I do not have access to Google Calendar."

**Why no `google-api-python-client`.** Same reason there is no
python-frontmatter and no APScheduler: the Calendar API is JSON over HTTPS and
OAuth refresh is one POST. The SDK is ~40MB of transitive dependencies to
avoid writing the 150 lines below, in a package whose whole premise is that it
runs with nothing installed.

**This is an egress point**, the third after `shells.py` and `llm.py`, and the
README's rule 6 has to be read with that in mind. It is a narrow one: it sends
what you explicitly ask it to write and nothing else.

Name resolution used to be narrower still, and wrongly. It matched only
against the calendar list Google already has, and refused on principle to look
in `10 - Personal CRM/`, on the grounds that reading a private note to turn
"Arash" into an address would push private content to Google. What that
produced, the first time it was asked, was V telling Parsa it could not find
Arash and asking him to supply the email — which was sitting in the CRM note,
in the vault V was reading from. The principle had protected a folder instead
of a fact, and the only thing it kept private was information from its owner.

So `resolve()` now falls back to `contacts.lookup()`, which returns addresses
and phone numbers and structurally cannot return prose — see that module for
why the hole is shaped the way it is. The address egresses; the note does not.
The fallback is last, after Google's own list, because a calendar that was
actually shared beats a guess from the address book.

**What "other people's calendars" can actually mean.** With Parsa's own
credentials there are three tiers, and conflating them produces confident
wrong answers:

  - calendars someone shared with him — full detail, they appear in
    `calendars()` and read like his own
  - anyone else — `freebusy()` only: busy/free blocks, no titles. This is all
    Google will give, and on a personal Gmail account (which this is) it is
    usually empty for people outside his contacts
  - writing to someone else's calendar — only with "make changes" permission.
    Without it the way to put something on their day is to create it here and
    add them as an attendee, which is what `add_event(attendees=[...])` does

Setup, once:

    python3 -m v.cli gcal auth

It prints what to create in the Google Cloud console, then opens a browser.
Credentials live in V's state dir (`~/.local/state/v`), never in the vault.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

# Read and write, on every calendar the account can reach. The narrower
# `calendar.events` scope cannot list which calendars exist, which is exactly
# what resolving a person's name needs.
SCOPE = "https://www.googleapis.com/auth/calendar"

# Fixed, because a Desktop-type OAuth client only accepts loopback redirects
# and the port has to match what was authorised. 0 would be simpler and would
# fail on the second run.
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/"

TIMEOUT = 20


class GoogleCalendarError(RuntimeError):
    pass


class NotConfigured(GoogleCalendarError):
    """No OAuth client. Distinct from NotAuthorised: the fix is different."""


class NotAuthorised(GoogleCalendarError):
    """Client is set up but nobody has consented yet, or the grant is gone."""


class PartiallyAuthorised(GoogleCalendarError):
    """Consent completed and the grant is stored, but the first API call
    failed — almost always the Calendar API not being enabled on the project.
    Separate from the others because the fix does not involve signing in
    again, and telling someone to re-auth here wastes their time."""


# --------------------------------------------------------------------------
@dataclass
class GEvent:
    id: str
    calendar_id: str
    summary: str
    start: dt.datetime | dt.date
    end: dt.datetime | dt.date | None = None
    all_day: bool = False
    location: str = ""
    description: str = ""
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)
    status: str = "confirmed"
    link: str = ""
    # Set on a freebusy result, where Google returns a block with no title.
    opaque: bool = False

    @property
    def time_label(self) -> str:
        if self.all_day:
            return "all-day"
        s = self.start.strftime("%H:%M")
        return f"{s}-{self.end.strftime('%H:%M')}" if self.end else s

    def render(self) -> str:
        where = f" @{self.location}" if self.location else ""
        n = len(self.attendees)
        who = f" · {n} guest{'' if n == 1 else 's'}" if n else ""
        title = "(busy)" if self.opaque else self.summary
        return f"{self.time_label} · {title}{where}{who}"

    def line(self) -> str:
        """The vault-calendar line for this event, so an import can reuse it."""
        d = self.start if self.all_day else self.start.date()
        where = f" @{self.location}" if self.location else ""
        return f"- {d.isoformat()} {self.time_label} · {self.summary}{where}"


# --------------------------------------------------------------------------
# iCal feeds — what is already set up here, and what it can and cannot do.
#
# `.gcal_ics_url` holds Google's "secret address in iCal format" per calendar.
# That is a *feed*: it is read-only by protocol, so no amount of code here
# turns it into a way to create an event. What it does do, and what OAuth is
# not needed for, is read any calendar whose secret URL you have — including
# a colleague's, if they send you theirs. So "other people's calendars" splits
# in two: reading can work today, writing cannot.
#
# The file already had `# Personal` and `# Work` above its two URLs and
# `get_sources` dropped both, so every event arrived from an anonymous merged
# blob. Those comments are the obvious names for the calendars and are read as
# such here.
FEED_LABEL = "#"


@dataclass
class Feed:
    name: str
    url: str

    @property
    def id(self) -> str:
        return f"ical:{self.name}"


def _feed_name_from(url: str) -> str:
    """Fall back to the calendar address when a URL has no `# label`."""
    try:
        part = url.split("/calendar/ical/")[1].split("/")[0]
        return urllib.parse.unquote(part).split("@")[0]
    except (IndexError, ValueError):
        return "calendar"


def ical_feeds(s: Settings) -> list[Feed]:
    path = s.vault / ".gcal_ics_url"
    lines: list[str] = []
    env = os.environ.get("GCAL_ICS_URL")
    if env:
        lines = [u.strip() for u in env.split(",")]
    elif path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    out, label = [], ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(FEED_LABEL):
            label = line.lstrip("# ").strip()
            continue
        out.append(Feed(label or _feed_name_from(line), line))
        label = ""
    return out


def _parse_when(node: dict, tz) -> tuple[dt.datetime | dt.date, bool]:
    """Google gives `date` for all-day and `dateTime` for timed. The two are
    different types and treating them alike is how all-day events end up at
    midnight in a list sorted by start time."""
    if "date" in node:
        return dt.date.fromisoformat(node["date"]), True
    raw = node["dateTime"]
    # `fromisoformat` handles the trailing Z only from 3.11; normalise so this
    # does not depend on the interpreter version.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return dt.datetime.fromisoformat(raw).astimezone(tz), False


# --------------------------------------------------------------------------
class GoogleCalendar:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    # ---- where the secrets live -----------------------------------------
    @property
    def _client_path(self):
        return self.s.state_dir / "gcal_client.json"

    @property
    def _token_path(self):
        return self.s.state_dir / "gcal_token.json"

    def _client(self) -> tuple[str, str]:
        cid = os.environ.get("GCAL_CLIENT_ID")
        secret = os.environ.get("GCAL_CLIENT_SECRET")
        if cid and secret:
            return cid, secret
        if not self._client_path.exists():
            raise NotConfigured(
                "no OAuth client. Create one at "
                "https://console.cloud.google.com/apis/credentials — "
                "'Create credentials' → 'OAuth client ID' → type 'Desktop app' "
                "— then save it as:\n"
                f"  {self._client_path}\n"
                '  {"client_id": "...", "client_secret": "..."}\n'
                "The Google Calendar API must be enabled for the project.")
        try:
            raw = json.loads(self._client_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotConfigured(f"{self._client_path} is not valid JSON: {exc}")
        # A client_secret.json downloaded from the console nests everything
        # under "installed"; accept it as-is rather than making the user edit
        # a file by hand to strip one level.
        raw = raw.get("installed", raw.get("web", raw))
        try:
            return raw["client_id"], raw["client_secret"]
        except KeyError as exc:
            raise NotConfigured(
                f"{self._client_path} has no {exc.args[0]}") from exc

    def configured(self) -> bool:
        try:
            self._client()
            return True
        except NotConfigured:
            return False

    def authorised(self) -> bool:
        return self._token_path.exists()

    def account(self) -> str:
        """Which Google account is connected, for `status` to be honest about."""
        try:
            return self._read_token().get("account", "")
        except GoogleCalendarError:
            return ""

    # ---- tokens ----------------------------------------------------------
    def _read_token(self) -> dict:
        if not self._token_path.exists():
            raise NotAuthorised(
                "not connected to Google Calendar yet — run "
                "`python3 -m v.cli gcal auth`")
        try:
            return json.loads(self._token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotAuthorised(f"token file is corrupt: {exc}") from exc

    def _write_token(self, data: dict) -> None:
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # A refresh token is a standing grant on the account. It has no
        # business being world-readable in a directory full of notes.
        os.chmod(self._token_path, 0o600)

    def _access_token(self) -> str:
        tok = self._read_token()
        expires = tok.get("expires_at", 0)
        # Refresh a minute early: a token that expires mid-request fails the
        # request, and the retry costs more than the clock skew it guards.
        if tok.get("access_token") and expires - 60 > dt.datetime.now().timestamp():
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise NotAuthorised(
                "the access token expired and there is no refresh token — "
                "run `python3 -m v.cli gcal auth` again")
        cid, secret = self._client()
        fresh = self._post_form(TOKEN_URL, {
            "client_id": cid, "client_secret": secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        })
        tok["access_token"] = fresh["access_token"]
        tok["expires_at"] = (dt.datetime.now().timestamp()
                             + int(fresh.get("expires_in", 3600)))
        # Google only re-issues a refresh token on re-consent; keep the old one.
        if fresh.get("refresh_token"):
            tok["refresh_token"] = fresh["refresh_token"]
        self._write_token(tok)
        return tok["access_token"]

    # ---- the consent flow ------------------------------------------------
    def authorize(self, *, open_browser: bool = True,
                  on_url=None) -> str:
        """Run the loopback OAuth flow and store the grant. Returns the account.

        `access_type=offline` with `prompt=consent` because without both,
        Google returns a refresh token only on the very first authorisation
        for that client — so a re-auth after revoking would appear to succeed
        and then stop working an hour later.
        """
        cid, secret = self._client()
        state = secrets.token_urlsafe(16)
        params = {
            "client_id": cid, "redirect_uri": REDIRECT_URI,
            "response_type": "code", "scope": SCOPE, "state": state,
            "access_type": "offline", "prompt": "consent",
        }
        url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

        result: dict = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                q = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                result.update({k: v[0] for k, v in q.items()})
                ok = q.get("state", [None])[0] == state and "code" in q
                body = ("<h2>Connected.</h2><p>You can close this tab and go "
                        "back to the terminal.</p>" if ok else
                        f"<h2>Failed.</h2><pre>{q}</pre>")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
                done.set()

            def log_message(self, *a):              # silence the access log
                pass

        try:
            server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        except OSError as exc:
            raise GoogleCalendarError(
                f"port {REDIRECT_PORT} is busy, and the OAuth client is "
                f"registered against exactly that port: {exc}") from exc

        threading.Thread(target=server.serve_forever, daemon=True).start()
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        try:
            if not done.wait(timeout=300):
                raise GoogleCalendarError("timed out waiting for consent")
        finally:
            server.shutdown()

        if "code" not in result:
            err = result.get("error", "")
            if err == "access_denied":
                # Overwhelmingly this is the consent screen's tester list, not
                # a refusal. Google's own page says "has not completed the
                # verification process", which sends people off to apply for
                # verification they do not need for an app only they use.
                raise GoogleCalendarError(
                    "access_denied — the OAuth app is in Testing mode and this "
                    "account is not on its tester list.\n"
                    "  Fix: console.cloud.google.com → APIs & Services → "
                    "OAuth consent screen → Audience → Test users → "
                    "+ Add users → add the account you are signing in as.\n"
                    "  Better: on the same page press 'Publish app'. In "
                    "Testing mode Google expires refresh tokens after 7 days, "
                    "so V would silently stop working every week. Publishing "
                    "shows a one-time 'unverified app' warning you can click "
                    "past via Advanced → Go to <app> (unsafe).")
            raise GoogleCalendarError(
                f"consent failed: {result.get('error_description') or err or result}")
        if result.get("state") != state:
            # Not paranoia for its own sake: without this the callback would
            # accept a code from any tab that happened to hit the port.
            raise GoogleCalendarError("state mismatch — refusing the callback")

        tok = self._post_form(TOKEN_URL, {
            "client_id": cid, "client_secret": secret,
            "code": result["code"], "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        })
        data = {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", ""),
            "expires_at": (dt.datetime.now().timestamp()
                           + int(tok.get("expires_in", 3600))),
        }
        self._write_token(data)
        # Recorded so `status` can say *which* account, not just "connected".
        # Best-effort, and that matters: the grant above is the thing worth
        # keeping, and this call is the first one to touch the API — so it is
        # where "the Calendar API is not enabled for this project" surfaces.
        # Letting that propagate reported a completed authorisation, refresh
        # token and all, as a failure, and sent the user back through consent
        # they had already given.
        try:
            for c in self._api_calendars():
                if c.get("primary"):
                    data["account"] = c["id"]
                    self._write_token(data)
                    return c["id"]
        except GoogleCalendarError as exc:
            raise PartiallyAuthorised(str(exc)) from exc
        return ""

    # ---- transport -------------------------------------------------------
    def _post_form(self, url: str, form: dict) -> dict:
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:400]
            if exc.code in (400, 401):
                if "invalid_grant" in body:
                    # The far more common cause than an actual revoke: the
                    # OAuth consent screen is still in Testing mode, where
                    # Google caps a refresh token's life at 7 days no matter
                    # how it was issued. `authorize()`'s access_denied branch
                    # already explains this at first consent; this is the
                    # same warning for the day-7 refresh failure it predicts,
                    # which otherwise surfaces as an opaque JSON blob.
                    raise NotAuthorised(
                        f"Google rejected the grant: {body}\n"
                        "  Likely cause: the OAuth app is in Testing mode, "
                        "where Google expires refresh tokens after 7 days —"
                        " not an actual revoke.\n"
                        "  Fix: console.cloud.google.com → APIs & Services → "
                        "OAuth consent screen → Audience → Publish app "
                        "(one-time 'unverified app' warning, click through "
                        "via Advanced → Go to <app>).\n"
                        "  Then: `python3 -m v.cli gcal auth` "
                        "once more.") from exc
                raise NotAuthorised(f"Google rejected the grant: {body}") from exc
            raise GoogleCalendarError(f"token request failed: {body}") from exc
        except urllib.error.URLError as exc:
            raise GoogleCalendarError(f"cannot reach Google: {exc.reason}") from exc

    def _api(self, method: str, path: str, *, params: dict | None = None,
             body: dict | None = None) -> dict:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None})
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        if payload:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code == 401:
                raise NotAuthorised(f"Google says unauthorised: {detail}") from exc
            if exc.code == 403:
                # By far the most common real failure, and "403" alone sends
                # people to re-auth when the actual answer is that the calendar
                # was never shared with them.
                raise GoogleCalendarError(
                    f"forbidden — the account may not have access to that "
                    f"calendar, or the Calendar API is not enabled for the "
                    f"project: {detail}") from exc
            if exc.code == 404:
                raise GoogleCalendarError(f"no such calendar or event: {detail}")
            raise GoogleCalendarError(f"{method} {path} failed "
                                      f"({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise GoogleCalendarError(f"cannot reach Google: {exc.reason}") from exc

    # ---- calendars -------------------------------------------------------
    def calendars(self) -> list[dict]:
        """Every calendar reachable, from both paths.

        The iCal feeds are listed even when OAuth is connected: a feed URL a
        colleague sent you is readable when their calendar was never shared
        with your account, so the two sets genuinely differ. Feeds are marked
        read-only because they are — that is the protocol, not a policy.
        """
        out = [{"id": f.id, "name": f.name, "primary": False,
                "access": "ical (read-only)", "writable": False,
                "source": "ical"} for f in ical_feeds(self.s)]
        if self.configured() and self.authorised():
            have = {c["name"].lower() for c in out}
            for c in self._api_calendars():
                # A feed and an API entry for the same calendar are one
                # calendar; prefer the API one, which can be written to.
                out = [o for o in out if o["name"].lower() != c["name"].lower()]
                out.append(c)
                have.add(c["name"].lower())
        return out

    def _api_calendars(self) -> list[dict]:
        """Its own, and each one someone else has shared with it. `accessRole`
        is what says whether writing will work: owner/writer yes, reader no."""
        out, page = [], None
        while True:
            data = self._api("GET", "/users/me/calendarList",
                             params={"pageToken": page, "maxResults": 250})
            for c in data.get("items", []):
                out.append({
                    "id": c["id"],
                    "name": c.get("summaryOverride") or c.get("summary", ""),
                    "primary": bool(c.get("primary")),
                    "access": c.get("accessRole", ""),
                    "writable": c.get("accessRole") in ("owner", "writer"),
                    "source": "api",
                })
            page = data.get("nextPageToken")
            if not page:
                return out

    def resolve(self, who: str) -> list[dict]:
        """Turn "arash" into the calendars it could mean.

        Returns every match rather than guessing one: picking the first of two
        plausible people and answering confidently is worse than saying which
        two. Matches on the calendar's name and its address, case-folded, and
        then — only if Google knows no such calendar — on the address book.
        See the module docstring for why that last step exists.
        """
        w = who.strip().lower()
        if not w:
            return []
        cals = self.calendars()
        if w in ("me", "my", "mine", "primary", "self"):
            own = [c for c in cals if c["primary"]]
            if own:
                return own
            # No OAuth, so nothing is flagged primary. The personal feed is
            # what "me" means here, and returning nothing would read as "you
            # have no calendar" when two are configured.
            personal = [c for c in cals
                        if c["name"].lower() in ("personal", "me", "primary")]
            return personal or cals[:1]
        if "@" in w:
            # An address is unambiguous: use it whether or not it is in the
            # list, since freebusy works on addresses that were never shared.
            hit = [c for c in cals if c["id"].lower() == w]
            return hit or [{"id": who.strip(), "name": who.strip(),
                            "primary": False, "access": "unshared",
                            "writable": False, "source": "address"}]
        named = [c for c in cals
                 if w in c["name"].lower() or w in c["id"].lower()]
        return named or self._from_contacts(who)

    def _from_contacts(self, who: str) -> list[dict]:
        """Google has never heard of this person; the address book has.

        Marked `unshared`, which is the honest access level: an address out of
        the CRM says where to reach someone, not that they let this account
        read their day. Callers route it to `freebusy()` on the strength of
        that flag, so a contact never gets rendered as if their titles were
        visible.
        """
        from . import contacts as contacts_mod

        out = []
        for person in contacts_mod.lookup(who, self.s):
            for addr in person.emails:
                out.append({"id": addr, "name": person.name, "primary": False,
                            "access": "unshared", "writable": False,
                            "source": "crm"})
        return out

    # ---- reading ---------------------------------------------------------
    def events(self, *, calendar_id: str = "primary",
               start: dt.date | dt.datetime | None = None,
               end: dt.date | dt.datetime | None = None,
               day: dt.date | None = None,
               limit: int = 50, query: str | None = None) -> list[GEvent]:
        """Events in a window, recurrences already expanded.

        `singleEvents=true` is not optional: without it a weekly standup comes
        back once, as a rule, and "what's on today" silently misses it.
        """
        tz = self.s.tz
        if day is not None:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
            end = start + dt.timedelta(days=1)
        if start is None:
            start = dt.datetime.now(tz).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        if end is None:
            end = start + dt.timedelta(days=1)
        start, end = _as_aware(start, tz), _as_aware(end, tz)

        if calendar_id.startswith("ical:"):
            return self._feed_events(calendar_id[5:], start, end, limit)

        data = self._api("GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events",
                         params={
                             "timeMin": start.isoformat(),
                             "timeMax": end.isoformat(),
                             "singleEvents": "true",
                             "orderBy": "startTime",
                             "maxResults": limit,
                             "q": query,
                         })
        out = []
        for e in data.get("items", []):
            if e.get("status") == "cancelled":
                continue
            when, all_day = _parse_when(e["start"], tz)
            until = None
            if "end" in e:
                until, _ = _parse_when(e["end"], tz)
            out.append(GEvent(
                id=e.get("id", ""), calendar_id=calendar_id,
                summary=e.get("summary", "(no title)"),
                start=when, end=until, all_day=all_day,
                location=e.get("location", ""),
                description=e.get("description", ""),
                organizer=(e.get("organizer") or {}).get("email", ""),
                attendees=[a.get("email", "") for a in e.get("attendees", [])],
                status=e.get("status", "confirmed"),
                link=e.get("htmlLink", ""),
            ))
        return out

    def _feed_events(self, name: str, start: dt.datetime, end: dt.datetime,
                     limit: int) -> list[GEvent]:
        """Read one iCal feed over a date range.

        Delegates the parsing to `daily_calendar.py`, which has handled
        RRULE/EXDATE/VTIMEZONE correctly since June — recurrence expansion is
        the kind of code you write once and never want two of. The feed is
        fetched once and replayed per day, because that module works a day at
        a time and re-downloading the same ICS seven times to answer "this
        week" is the sort of thing nobody notices until the feed is slow.
        """
        from .agenda import _load_script
        from . import calendar as cal_mod

        feed = next((f for f in ical_feeds(self.s) if f.name == name), None)
        if feed is None:
            raise GoogleCalendarError(f"no iCal feed named {name!r}")
        mod = _load_script(self.s)
        if mod is None:
            raise GoogleCalendarError("daily_calendar.py is missing from the "
                                      "vault root — it does the ICS parsing")
        try:
            text = mod.read_source(feed.url)
        except Exception as exc:
            raise GoogleCalendarError(
                f"could not fetch the {name} feed: {exc}") from exc

        out: list[GEvent] = []
        day = start.date()
        while day < end.date() and len(out) < limit:
            try:
                rows = mod.collect_day_events(text, day)
            except Exception:
                rows = []
            for (_k, all_day, summary, location, start_s, end_s, uid,
                 _cal) in rows:
                if all_day:
                    out.append(GEvent(uid, f"ical:{name}", summary, day,
                                      None, True, location or ""))
                    continue
                s0 = cal_mod._parse_time(start_s)
                s1 = cal_mod._parse_time(end_s) if end_s else None
                if s0 is None:
                    continue
                out.append(GEvent(
                    uid, f"ical:{name}", summary,
                    dt.datetime.combine(day, s0, tzinfo=self.s.tz),
                    dt.datetime.combine(day, s1, tzinfo=self.s.tz) if s1 else None,
                    False, location or ""))
            day += dt.timedelta(days=1)
        return out[:limit]

    def freebusy(self, ids: list[str], start: dt.datetime,
                 end: dt.datetime) -> dict[str, list[tuple]]:
        """Busy blocks for people whose calendars are not shared.

        The only thing Google will return for someone who has not shared with
        this account — times, no titles. Anyone unreachable comes back as an
        empty list *with* an error recorded, because "free all day" and "I
        cannot see this person" must not render the same way.
        """
        tz = self.s.tz
        body = {
            "timeMin": _as_aware(start, tz).isoformat(),
            "timeMax": _as_aware(end, tz).isoformat(),
            "items": [{"id": i} for i in ids],
        }
        data = self._api("POST", "/freeBusy", body=body)
        out = {}
        for cid, node in (data.get("calendars") or {}).items():
            if node.get("errors"):
                out[cid] = [("error", node["errors"][0].get("reason", "unknown"))]
                continue
            blocks = []
            for b in node.get("busy", []):
                blocks.append((
                    dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")
                                              ).astimezone(tz),
                    dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")
                                              ).astimezone(tz),
                ))
            out[cid] = blocks
        return out

    # ---- writing ---------------------------------------------------------
    def add_event(self, summary: str, *, start: dt.datetime | dt.date,
                  end: dt.datetime | dt.date | None = None,
                  calendar_id: str = "primary", location: str = "",
                  description: str = "", attendees: list[str] | None = None,
                  all_day: bool = False, notify: bool = True) -> GEvent:
        """Create an event. `attendees` is how something lands on someone
        else's calendar without write access to it."""
        tz = self.s.tz
        if not summary.strip():
            raise GoogleCalendarError("an event needs a title")
        if calendar_id.startswith("ical:"):
            # Worth being exact about, because it is the one thing the
            # existing setup cannot be extended to do. A secret iCal address
            # is a subscription URL; there is no endpoint behind it to POST to.
            raise NotConfigured(
                f"{calendar_id[5:]!r} is an iCal feed, and iCal feeds are "
                "read-only — there is no way to create an event through one. "
                "Writing needs the Calendar API: run "
                "`python3 -m v.cli gcal auth` once to connect it. "
                "Reading, including other people's feeds, keeps working "
                "without that.")
        if all_day:
            d = start if isinstance(start, dt.date) and not isinstance(
                start, dt.datetime) else start.date()
            finish = end if isinstance(end, dt.date) else d + dt.timedelta(days=1)
            when = {"start": {"date": d.isoformat()},
                    "end": {"date": finish.isoformat()}}
        else:
            s0 = _as_aware(start, tz)
            s1 = _as_aware(end, tz) if end else s0 + dt.timedelta(hours=1)
            if s1 <= s0:
                raise GoogleCalendarError(
                    f"the event ends before it starts ({s0:%H:%M} → {s1:%H:%M})")
            when = {"start": {"dateTime": s0.isoformat(),
                              "timeZone": self.s.timezone},
                    "end": {"dateTime": s1.isoformat(),
                            "timeZone": self.s.timezone}}
        body = {"summary": summary.strip(), **when}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a.strip()} for a in attendees
                                 if a.strip()]
        created = self._api(
            "POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events",
            params={"sendUpdates": "all" if (notify and attendees) else "none"},
            body=body)
        when_v, is_all_day = _parse_when(created["start"], tz)
        until, _ = _parse_when(created["end"], tz) if "end" in created else (None, False)
        return GEvent(
            id=created.get("id", ""), calendar_id=calendar_id,
            summary=created.get("summary", summary), start=when_v, end=until,
            all_day=is_all_day, location=created.get("location", ""),
            attendees=[a.get("email", "") for a in created.get("attendees", [])],
            link=created.get("htmlLink", ""))

    def update_event(self, event_id: str, *, calendar_id: str = "primary",
                     **fields) -> dict:
        """Patch, not put: PUT replaces the whole event, so changing a title
        would silently drop the attendees and the location."""
        body = {k: v for k, v in fields.items() if v is not None}
        if not body:
            raise GoogleCalendarError("nothing to update")
        return self._api(
            "PATCH",
            f"/calendars/{urllib.parse.quote(calendar_id)}/"
            f"events/{urllib.parse.quote(event_id)}", body=body)

    def delete_event(self, event_id: str, *, calendar_id: str = "primary",
                     notify: bool = True) -> None:
        self._api("DELETE",
                  f"/calendars/{urllib.parse.quote(calendar_id)}/"
                  f"events/{urllib.parse.quote(event_id)}",
                  params={"sendUpdates": "all" if notify else "none"})


def _as_aware(value, tz) -> dt.datetime:
    """A naive datetime here means local time, and local time is the pinned
    zone — not whatever TZ the process inherited. That distinction is the whole
    reason `config.apply_timezone` exists."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    return dt.datetime.combine(value, dt.time.min, tzinfo=tz)
