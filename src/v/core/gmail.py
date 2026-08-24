"""Gmail — the fourth egress point, after `shells.py`, `llm.py` and `gcal.py`.

Same reasoning as `gcal.py`: no `google-api-python-client` (~40MB of
transitive dependencies to avoid writing a couple hundred lines of JSON over
HTTPS), same loopback OAuth flow, same `state/` token storage, gitignored,
never in the vault.

**Deliberately a separate token from Calendar**, even though both can share
one OAuth client (`GCAL_CLIENT_ID`/`GCAL_CLIENT_SECRET`, same Google Cloud
project — no second app to register). Gmail's scope is a materially bigger
grant than Calendar's, so it gets its own consent (`cli gmail auth`) and its
own file (`gmail_token.json`), rather than folding onto a token someone might
reasonably assume is calendar-only.

**Read, search and draft cost nothing to call — send is gated.** `draft()`
only ever creates a Gmail draft; nothing leaves the account until `send()` is
called with that draft's id, and the system prompt in `identity.py` tells the
model never to call `gmail_send` in the same turn it calls `gmail_draft` — show
the draft, wait for Parsa's next message, send only on confirmation. Compare
`vault_places_search`'s "never save without confirmation" rule and
`gcal.add_event`'s much lower bar (a calendar slot is trivially undoable;
a sent email is not).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.mime.text import MIMEText

from .config import Settings, settings as default_settings
from .gcal import REDIRECT_PORT, REDIRECT_URI, AUTH_URL, TOKEN_URL

API = "https://gmail.googleapis.com/gmail/v1/users/me"

# `gmail.modify` is read + search + labels, without the destructive
# `gmail.settings.*` scopes or full `mail.google.com`. `gmail.send` is kept
# separate and explicit rather than relying on `modify` implying it (it
# doesn't — Google requires it named).
SCOPE = ("https://www.googleapis.com/auth/gmail.modify "
         "https://www.googleapis.com/auth/gmail.send")

TIMEOUT = 20


class GmailError(RuntimeError):
    pass


class NotConfigured(GmailError):
    """No OAuth client. Distinct from NotAuthorised: the fix is different."""


class NotAuthorised(GmailError):
    """Client is set up but nobody has consented yet, or the grant is gone."""


# --------------------------------------------------------------------------
@dataclass
class MessageSummary:
    id: str
    thread_id: str
    from_: str
    subject: str
    snippet: str
    date: dt.datetime | None
    unread: bool

    def render(self) -> str:
        who = self.from_.split("<")[0].strip().strip('"') or self.from_
        when = self.date.strftime("%Y-%m-%d %H:%M") if self.date else ""
        mark = "●" if self.unread else "○"
        return f"{mark} {when}  {who} — {self.subject or '(no subject)'}"


@dataclass
class Message:
    id: str
    thread_id: str
    from_: str
    to: str
    subject: str
    date: dt.datetime | None
    body: str
    labels: list[str] = field(default_factory=list)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_date(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _walk_parts(payload: dict) -> tuple[str, str]:
    """Depth-first search for the first `text/plain` and `text/html` parts.
    A multipart message nests arbitrarily (alternative inside related inside
    mixed, for an inline image); recursing is the only correct way to find
    the body rather than assuming a fixed shape."""
    plain, html = "", ""
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data:
        text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace")
        if mime == "text/plain":
            plain = text
        elif mime == "text/html":
            html = text
    for part in payload.get("parts") or []:
        p, h = _walk_parts(part)
        plain = plain or p
        html = html or h
    return plain, html


def _strip_html(html: str) -> str:
    """A rough tag strip for the case where a message has no plain-text
    part at all — good enough for a model to read, not a renderer."""
    import re
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------
class Gmail:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    # ---- where the secrets live -------------------------------------
    @property
    def _client_path(self):
        # Shared with gcal.py deliberately — same OAuth client, same Google
        # Cloud project, one file registered once.
        return self.s.state_dir / "gcal_client.json"

    @property
    def _token_path(self):
        return self.s.state_dir / "gmail_token.json"

    def _client(self) -> tuple[str, str]:
        cid = os.environ.get("GCAL_CLIENT_ID")
        secret = os.environ.get("GCAL_CLIENT_SECRET")
        if cid and secret:
            return cid, secret
        if not self._client_path.exists():
            raise NotConfigured(
                "no OAuth client. If Calendar is already connected this is "
                "the same client — nothing to create. Otherwise: "
                "https://console.cloud.google.com/apis/credentials — "
                "'Create credentials' → 'OAuth client ID' → type 'Desktop app' "
                "— then save it as:\n"
                f"  {self._client_path}\n"
                '  {"client_id": "...", "client_secret": "..."}\n'
                "The Gmail API must be enabled for the project.")
        try:
            raw = json.loads(self._client_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotConfigured(f"{self._client_path} is not valid JSON: {exc}")
        raw = raw.get("installed", raw.get("web", raw))
        try:
            return raw["client_id"], raw["client_secret"]
        except KeyError as exc:
            raise NotConfigured(f"{self._client_path} has no {exc.args[0]}") from exc

    def configured(self) -> bool:
        try:
            self._client()
            return True
        except NotConfigured:
            return False

    def authorised(self) -> bool:
        return self._token_path.exists()

    def account(self) -> str:
        try:
            return self._read_token().get("account", "")
        except GmailError:
            return ""

    # ---- tokens -------------------------------------------------------
    def _read_token(self) -> dict:
        if not self._token_path.exists():
            raise NotAuthorised(
                "not connected to Gmail yet — run "
                "`python3 -m v.cli gmail auth`")
        try:
            return json.loads(self._token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NotAuthorised(f"token file is corrupt: {exc}") from exc

    def _write_token(self, data: dict) -> None:
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(self._token_path, 0o600)

    def _access_token(self) -> str:
        tok = self._read_token()
        expires = tok.get("expires_at", 0)
        if tok.get("access_token") and expires - 60 > dt.datetime.now().timestamp():
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise NotAuthorised(
                "the access token expired and there is no refresh token — "
                "run `python3 -m v.cli gmail auth` again")
        cid, secret = self._client()
        fresh = self._post_form(TOKEN_URL, {
            "client_id": cid, "client_secret": secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
        })
        tok["access_token"] = fresh["access_token"]
        tok["expires_at"] = (dt.datetime.now().timestamp()
                             + int(fresh.get("expires_in", 3600)))
        if fresh.get("refresh_token"):
            tok["refresh_token"] = fresh["refresh_token"]
        self._write_token(tok)
        return tok["access_token"]

    # ---- the consent flow ----------------------------------------------
    def authorize(self, *, open_browser: bool = True, on_url=None) -> str:
        """Run the loopback OAuth flow and store the grant. Returns the account.

        Same loopback dance as `gcal.GoogleCalendar.authorize` — duplicated
        rather than shared, so each egress point stays independently
        readable end to end without following a helper into another file.
        """
        import http.server
        import secrets
        import threading
        import webbrowser

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

            def log_message(self, *a):
                pass

        try:
            server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        except OSError as exc:
            raise GmailError(
                f"port {REDIRECT_PORT} is busy, and the OAuth client is "
                f"registered against exactly that port: {exc}") from exc

        threading.Thread(target=server.serve_forever, daemon=True).start()
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        try:
            if not done.wait(timeout=300):
                raise GmailError("timed out waiting for consent")
        finally:
            server.shutdown()

        if "code" not in result:
            err = result.get("error", "")
            raise GmailError(
                f"consent failed: {result.get('error_description') or err or result}")
        if result.get("state") != state:
            raise GmailError("state mismatch — refusing the callback")

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
        try:
            profile = self._api("GET", "/profile")
            data["account"] = profile.get("emailAddress", "")
            self._write_token(data)
            return data["account"]
        except GmailError:
            # Consent and the grant are already saved — losing the account
            # label is cosmetic, not a reason to make Parsa re-authorise.
            return ""

    # ---- transport ------------------------------------------------------
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
                raise NotAuthorised(f"Google rejected the grant: {body}") from exc
            raise GmailError(f"token request failed: {body}") from exc
        except urllib.error.URLError as exc:
            raise GmailError(f"cannot reach Google: {exc.reason}") from exc

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
                raise GmailError(
                    f"forbidden — the Gmail API may not be enabled for the "
                    f"project, or the scope wasn't granted: {detail}") from exc
            if exc.code == 404:
                raise GmailError(f"no such message or draft: {detail}") from exc
            raise GmailError(f"{method} {path} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise GmailError(f"cannot reach Google: {exc.reason}") from exc

    # ---- reading --------------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[MessageSummary]:
        """Gmail's own search syntax (`is:unread`, `from:`, `newer_than:1d`,
        …) passed straight through — no query DSL of our own, since the
        model already knows Gmail's."""
        data = self._api("GET", "/messages",
                         params={"q": query, "maxResults": limit})
        out = []
        for item in data.get("messages", []) or []:
            out.append(self._summary(item["id"]))
        return out

    # ---- for the proactive loop ------------------------------------------
    @property
    def _seen_path(self):
        return self.s.state_dir / "gmail_seen.json"

    def unseen(self, query: str, *, limit: int = 10) -> list[MessageSummary]:
        """`search()`, filtered to ids `scheduler.py`'s gmail-check job
        hasn't already surfaced, and marks the returned ones seen. Own state
        file (`gmail_seen.json`), same reason `brag.py` owns `brag.json`
        rather than the scheduler tracking what each job already pushed —
        the dedup rule belongs to the domain, not the loop that happens to
        call it on a timer."""
        hits = self.search(query, limit=limit)
        try:
            seen = set(json.loads(self._seen_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            seen = set()
        new = [h for h in hits if h.id not in seen]
        if new:
            seen |= {h.id for h in new}
            # Capped rather than left to grow forever — only the last 500
            # ids are worth keeping, since anything older has long since
            # fallen out of `newer_than:1d`'s own window anyway.
            self.s.state_dir.mkdir(parents=True, exist_ok=True)
            self._seen_path.write_text(
                json.dumps(sorted(seen)[-500:]), encoding="utf-8")
        return new

    def _summary(self, message_id: str) -> MessageSummary:
        m = self._api("GET", f"/messages/{message_id}",
                      params={"format": "metadata",
                              "metadataHeaders": ["From", "Subject", "Date"]})
        headers = (m.get("payload") or {}).get("headers", [])
        return MessageSummary(
            id=m["id"], thread_id=m.get("threadId", ""),
            from_=_header(headers, "From"), subject=_header(headers, "Subject"),
            snippet=m.get("snippet", ""),
            date=_parse_date(_header(headers, "Date")),
            unread="UNREAD" in (m.get("labelIds") or []))

    def read(self, message_id: str) -> Message:
        m = self._api("GET", f"/messages/{message_id}", params={"format": "full"})
        headers = (m.get("payload") or {}).get("headers", [])
        plain, html = _walk_parts(m.get("payload") or {})
        body = plain or _strip_html(html) or m.get("snippet", "")
        return Message(
            id=m["id"], thread_id=m.get("threadId", ""),
            from_=_header(headers, "From"), to=_header(headers, "To"),
            subject=_header(headers, "Subject"),
            date=_parse_date(_header(headers, "Date")),
            body=body, labels=m.get("labelIds") or [])

    # ---- writing: draft, then send only on confirmation ------------------
    def draft(self, to: str, subject: str, body: str, *,
              thread_id: str | None = None) -> str:
        """Creates a Gmail draft and returns its id. Nothing is sent — see
        the module docstring for why `send()` is a separate, gated call."""
        msg = MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message: dict = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        created = self._api("POST", "/drafts", body={"message": message})
        return created["id"]

    def send(self, draft_id: str) -> str:
        """Sends a previously-created draft. Takes a draft id, never raw
        `to`/`subject`/`body` — compose-and-send in one call does not exist
        here, so the model cannot reach it even by accident."""
        sent = self._api("POST", "/drafts/send", body={"id": draft_id})
        return sent.get("id", "")
