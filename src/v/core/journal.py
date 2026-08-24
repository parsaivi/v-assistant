"""V writes itself down, in the vault, where Parsa already reads.

Everything V knows about itself lives in `19 - Assistant/V/` as ordinary notes:
what it did today, what it decided and why, what it can actually do, what it's
working toward, and what it remembers. Not a dashboard — notes, with
frontmatter, that Dataview can query and Obsidian can link like anything else.

The reason this exists rather than a log file: an assistant whose behaviour you
can't inspect is one you end up guessing about. When V routes something oddly,
starts costing more, or forgets a preference, the answer should be a note you
can open — not a `--verbose` flag and a JSONL file under `state/`.

Writes go through `Vault`, so every one is append-only and a `[bot]` commit,
same as any other write V makes. Three of the five files are regenerated
wholesale (skills, goals, memory) and say so at the top; the daily log and the
decision log are append-only, because their value is that nothing rewrites
history.
"""

from __future__ import annotations

import datetime as dt

from .config import Settings, settings as default_settings
from .vaultfs import Vault

HOME = "19 - Assistant/V"


class Journal:
    def __init__(self, settings: Settings | None = None,
                 vault: Vault | None = None):
        self.s = settings or default_settings
        self.vault = vault or Vault(self.s)

    # ---- paths --------------------------------------------------------
    def log_path(self, day: dt.date) -> str:
        return f"{HOME}/Logs/{day.isoformat()}.md"

    @property
    def decisions_path(self) -> str:
        return f"{HOME}/Decisions.md"

    @property
    def skills_path(self) -> str:
        return f"{HOME}/Skills.md"

    @property
    def goals_path(self) -> str:
        return f"{HOME}/Goals.md"

    @property
    def memory_path(self) -> str:
        return f"{HOME}/Memory.md"

    @property
    def readme_path(self) -> str:
        return f"{HOME}/README.md"

    # ---- daily log ----------------------------------------------------
    def log(self, what: str, *, route: str = "", cost: float = 0.0,
            now: dt.datetime | None = None) -> None:
        """One line per thing V did that was worth doing.

        Not every message — the JSONL under `state/logs/` already has all of
        those, and a vault note that logs 200 lookups a day is a note nobody
        opens twice. This is for writes, escalations, and things V learned.
        """
        now = now or dt.datetime.now()
        rel = self.log_path(now.date())
        if not self.vault.exists(rel):
            self._create_log(rel, now.date())
        bits = [f"- `{now.strftime('%H:%M')}` {what}"]
        if route:
            bits.append(f"·  `{route}`")
        if cost:
            bits.append(f"·  ≈${cost:.4f}")
        try:
            self.vault.append(rel, " ".join(bits), heading="Activity",
                              summary=f"V log {now.date()}")
        except Exception:
            pass  # a failed self-log must never take down the turn it describes

    def _create_log(self, rel: str, day: dt.date) -> None:
        meta = {
            "type": "v-log",
            "date": day.isoformat(),
            "week": f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}",
            "month": day.strftime("%Y-%m"),
            "tags": ["assistant", "v-log"],
        }
        body = (f"\n# V — {day.strftime('%A, %d %B %Y')}\n\n"
                "What I did today. Appended as it happens; nothing here is "
                "rewritten.\n\n## Activity\n\n")
        self.vault.create(rel, meta, body, summary=f"V log for {day}")

    # ---- sessions -----------------------------------------------------
    def session(self, recap) -> bool:
        """File a finished conversation in the day's log.

        The recap arrives already rendered — this layer only decides where it
        goes. That split is what keeps quitting V free: `core/session.py`
        builds the Markdown out of the turns it recorded, so no model is asked
        to summarise anything and the write costs a commit and nothing else.

        It goes under `## Sessions` in the same daily log as everything else V
        did, so one note answers "what happened on Sunday" — activity, and the
        conversations that caused it. Returns whether it landed; the raw
        transcript under `state/sessions/` is the fallback if it didn't.
        """
        rel = self.log_path(recap.day)
        try:
            if not self.vault.exists(rel):
                self._create_log(rel, recap.day)
            # Leading blank line, not trailing: `append` rstrips what it is
            # given, so a trailing one is discarded and the next session's
            # `###` ends up welded to the last line of this one.
            self.vault.append(rel, "\n" + recap.markdown, heading="Sessions",
                              summary=f"V session {recap.session.id} "
                                      f"({recap.n} turn"
                                      f"{'s' if recap.n != 1 else ''})")
            return True
        except Exception:
            # Same rule as the daily log: V failing to write itself down must
            # not be the thing that takes down the exit path.
            return False

    # ---- decisions ----------------------------------------------------
    def decide(self, title: str, choice: str, *, because: str,
               alternatives: str = "", now: dt.datetime | None = None) -> None:
        """Append a decision. Append-only on purpose — the point of a decision
        log is that you can see the one that turned out badly."""
        now = now or dt.datetime.now()
        if not self.vault.exists(self.decisions_path):
            self._create_decisions()
        entry = [
            f"\n### {now.date()} — {title}",
            "",
            f"**Chose:** {choice}",
            "",
            f"**Because:** {because}",
        ]
        if alternatives:
            entry += ["", f"**Instead of:** {alternatives}"]
        entry.append("")
        try:
            self.vault.append(self.decisions_path, "\n".join(entry),
                              summary=f"V decision: {title}")
        except Exception:
            pass

    def _create_decisions(self) -> None:
        meta = {"type": "v-decisions", "tags": ["assistant", "decisions"]}
        body = ("\n# V — Decision Log\n\n"
                "Choices I made about my own behaviour, and why. Append-only: "
                "a decision log you can edit is a story, not a record.\n\n"
                "Each entry says what I chose, the reason, and what I rejected. "
                "If I'm behaving in a way that surprises you, the explanation "
                "is usually here or in [[19 - Assistant/V/Memory|Memory]].\n")
        self.vault.create(self.decisions_path, meta, body,
                          summary="V decision log")

    # ---- generated pages ----------------------------------------------
    def write_skills(self, capabilities: list[dict]) -> None:
        """What V can actually do, generated from the code rather than claimed.

        Hand-written capability lists rot the moment a feature lands or breaks.
        This one is built from the live router/query/tool surfaces, so if
        something disappears from the code it disappears from the page.
        """
        lines = [
            "---", "type: v-skills",
            f'updated: "{dt.date.today().isoformat()}"',
            "tags:", "  - assistant", "  - skills", "---", "",
            "# What V Can Do", "",
            "_Generated from the live code on every run — if it's listed here "
            "the capability exists, and if a capability breaks it stops being "
            "listed._", "",
        ]
        for group in capabilities:
            lines.append(f"## {group['name']}")
            lines.append("")
            if group.get("note"):
                lines += [f"_{group['note']}_", ""]
            for item in group["items"]:
                cost = item.get("cost", "free")
                badge = "**free**" if cost == "free" else f"_{cost}_"
                lines.append(f"- {item['what']} — {badge}")
                if item.get("say"):
                    lines.append(f"    - e.g. *\"{item['say']}\"*")
            lines.append("")
        self._replace(self.skills_path, "\n".join(lines), "V skills")

    def write_goals(self, goals: list[dict]) -> None:
        lines = [
            "---", "type: v-goals",
            f'updated: "{dt.date.today().isoformat()}"',
            "tags:", "  - assistant", "  - goals", "---", "",
            "# What V Is For", "",
            "_The standing goals I hold myself to. These shape what I do when "
            "nobody asked — the morning brief, nudges, what I bother you "
            "about._", "",
        ]
        for g in goals:
            lines.append(f"## {g['goal']}")
            lines.append("")
            lines.append(g["why"])
            lines.append("")
            if g.get("measure"):
                lines.append(f"**How I'd know I'm failing:** {g['measure']}")
                lines.append("")
        self._replace(self.goals_path, "\n".join(lines), "V goals")

    def write_memory(self, markdown: str) -> None:
        self._replace(self.memory_path, markdown, "V memory")

    def write_readme(self) -> None:
        lines = [
            "---", "type: v-home", "tags:", "  - assistant", "---", "",
            "# V", "",
            "The assistant over this vault. Local by default, and it only "
            "reaches for a model when the answer genuinely needs one.", "",
            "| | |", "|---|---|",
            "| [[19 - Assistant/V/Skills\\|Skills]] | what I can do, generated "
            "from the code |",
            "| [[19 - Assistant/V/Goals\\|Goals]] | what I'm for, and how I'd "
            "know I'm failing |",
            "| [[19 - Assistant/V/Memory\\|Memory]] | what I've learned from "
            "you correcting me |",
            "| [[19 - Assistant/V/Decisions\\|Decisions]] | choices about my "
            "own behaviour, and why |",
            "| `Logs/` | what I did, day by day |",
            "",
            "## Talking to me", "",
            "```bash",
            "python3 -m v.cli repl        # conversation",
            "python3 -m v.cli brief       # the morning brief",
            "python3 -m v.cli memory      # what I remember",
            "python3 -m v.cli shell       # which model tier I'm on",
            "```", "",
            "## Teaching me", "",
            "I learn from corrections, not from chat. These stick:", "",
            "- *\"remember that X\"* — a fact worth keeping",
            "- *\"always X\" / \"never X\" / \"from now on X\"* — how you want "
            "me to work",
            "- *\"no, actually X\"* — a correction; it supersedes whatever I "
            "believed",
            "- *\"/forget 4\"* — drop memory #4",
            "",
            "Everything I remember is in [[19 - Assistant/V/Memory|Memory]], "
            "with the reason I believe it and how strongly. Nothing is hidden "
            "in a state file.",
            "",
        ]
        self._replace(self.readme_path, "\n".join(lines), "V readme")

    # ---- housekeeping --------------------------------------------------
    def _replace(self, rel: str, content: str, summary: str) -> None:
        """Regenerated pages are the one place a rewrite is correct — the file
        is a projection of state V owns, not something Parsa wrote."""
        try:
            if self.vault.exists(rel):
                self.vault.rewrite(rel, content, allow_rewrite=True,
                                   summary=summary)
            else:
                self.vault.path(rel).parent.mkdir(parents=True, exist_ok=True)
                self.vault.path(rel).write_text(content, encoding="utf-8")
                self.vault.rewrite(rel, content, allow_rewrite=True,
                                   summary=summary)
        except Exception:
            pass

    def refresh_all(self, *, capabilities: list[dict], goals: list[dict],
                    memory_markdown: str) -> None:
        self.write_readme()
        self.write_skills(capabilities)
        self.write_goals(goals)
        self.write_memory(memory_markdown)
