"""Privacy enforcement, at the only place it can actually work.

`llm.Guard` checks a list of paths *you* chose before sending them somewhere.
That is the right check for a pipeline where the caller assembles the context.
It is the wrong layer the moment a model picks its own reading material: an
agent with Read and Grep never calls it, so the guard silently never runs.

So the enforcement point moves here, to the tool boundary. Every tool that can
return note content filters its own results, which means a private note cannot
reach a model's context regardless of what the model asked for or how it asked.
Private notes stay indexed and stay searchable by the local $0 path — only the
MCP surface is blind to them.
"""

from __future__ import annotations

from dataclasses import replace

from ..core.config import Settings, settings as default_settings


def _strip_link(link: str) -> str:
    """`[[10 - Personal CRM/Ali|Ali]]` -> `10 - Personal CRM/Ali`."""
    s = link.strip().lstrip("!").strip("[]")
    return s.split("|", 1)[0].split("#", 1)[0].strip()


class Blocked(PermissionError):
    """Raised when a tool is asked for something in a private folder."""


class Privacy:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or default_settings

    @property
    def folders(self) -> tuple[str, ...]:
        return self.s.private_folders

    def blocks(self, rel: str) -> bool:
        return self.s.is_private(rel)

    def check(self, rel: str) -> None:
        """Gate a single explicit path. Raises rather than returning empty —
        an explicit ask for a private note deserves an explicit refusal."""
        if self.blocks(rel):
            raise Blocked(
                f"{rel} is in a private folder and is never exposed to a model. "
                f"Private folders: {', '.join(self.folders)}."
            )

    def filter(self, rels):
        """Silently drop private paths from a result set.

        Silent here, loud in `check`: a search that happens to touch a private
        note should return its other hits, not fail. A direct request for one
        should say why it can't.
        """
        return [r for r in rels if not self.blocks(r)]

    def scrub_nudges(self, items):
        """Keep the signal, drop the private specifics.

        A nudge derived from a private folder is still worth surfacing — "4
        people past their contact cadence" is the actionable half. The names
        are not: `10 - Personal CRM/` is private, so the detail line and the
        wikilinks are stripped and the headline survives. Found the hard way:
        the first version of `vault_nudges` cheerfully returned three contacts
        by name.
        """
        out = []
        for n in items:
            leaks = any(self.blocks(_strip_link(l)) for l in n.links) or \
                any(f in (n.detail or "") for f in self.folders)
            if leaks:
                n = replace(n, links=[],
                            detail="(details withheld — private folder)")
            out.append(n)
        return out

    def redacted(self, before: int, after: int) -> str:
        n = before - after
        if n <= 0:
            return ""
        return f"\n\n_({n} result{'s' if n != 1 else ''} withheld — private folders.)_"
