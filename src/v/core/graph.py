"""graph.py — the vault as a shape, so a surface can point at where V is.

The window can already say *what* V is doing: `vault_read`, `vault_search`,
one line per call. What it could not say is *where*, and in a vault the where
is the interesting half. "Reading a note" is the same sentence every time;
"reading the note three hops from today, in Work, the one everything links
into" is a different fact each time, and it is the one a person actually wants
while they wait.

So: nodes are notes, edges are wikilinks, and a run lights the notes it
touches as it touches them. Nothing here animates and nothing here draws —
this module answers "what is the shape" and "where is this note in it", and
`ui.py` turns that into particles, as it does for everything else.

Three things are worth knowing about the design.

**The layout is computed once and then frozen.** Re-running a force
simulation per frame would be the obvious thing and it is wrong twice over:
it costs a relaxation pass thirty times a second, and it *boils* — every node
drifting under its neighbours' pressure, so the graph you looked at a second
ago is not the graph in front of you. Positions are solved once per link-set,
cached to disk, and reused until the links themselves change. What animates is
which nodes are lit, never where they are. The same discipline `aperture()`
already follows: stable geometry, moving energy.

**Positions are deterministic.** The seed is a hash of the note's path, not
`random`, so the same vault lays out the same way on every machine and after
every rebuild. That matters more than it sounds: a graph that reshuffles on
each restart can never be *learned*, and the whole value of a picture of your
own vault is that after a week you recognise the corner Gym lives in without
reading a single label.

**Stdlib only, and no numpy.** `pyproject.toml` ships with an empty
`dependencies` list on purpose and a test asserts the free paths stay free, so
the relaxation below is plain Python. It is affordable because it is bucketed:
repulsion is computed against a neighbourhood grid rather than against all n,
which turns the per-iteration cost from n² into roughly n·k. On a vault of a
thousand notes that is a second or two, once, ever.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass, field

from .config import Settings, settings as default_settings

# Relaxation constants. Fruchterman-Reingold, with the two adjustments that a
# vault (as opposed to a random graph) actually needs.
_ITERATIONS = 260
# A neighbourhood is twenty nodes and is re-solved every time the focus moves,
# which during a run is every tool call. It converges in far fewer passes than
# a vault does, and at this size the passes cost microseconds.
_LOCAL_ITERATIONS = 90
# The bucket grid is sized to the ideal edge length, so repulsion only ever
# looks at the 3×3 block around a node. Anything further away contributes a
# force too small to move a cell on a terminal grid — this is not an
# approximation anyone can see, it is one nobody can.
_BUCKET_SPAN = 2.0
# Isolated notes — no links in or out — are most of a vault. Left to pure
# repulsion they inflate into a uniform disc and squash the linked core into
# the middle few cells, which is the one thing the picture must not do. They
# get their own weaker repulsion so they settle as a halo instead.
_LONE_DAMP = 0.35


@dataclass(slots=True)
class Node:
    """One note, at rest.

    `x`/`y` are in layout space: centred on the origin and scaled so the
    furthest node sits at radius 1. Not cells and not pixels — the same
    positions have to serve a 28-column side pane and an 80-column canvas, and
    the only way that works is if neither number is baked in here.
    """

    rel: str
    title: str = ""
    folder: str = ""
    deg: int = 0
    x: float = 0.0
    y: float = 0.0


@dataclass(slots=True)
class View:
    """A slice of the graph, ready to be drawn, with its own extents.

    A view carries bounds rather than letting the renderer measure them,
    because the two callers want different framing from the same data: the
    full canvas wants the whole vault at its true aspect, while the side pane
    wants a neighbourhood filling the box regardless of how tight a cluster it
    happens to be. Both are a choice about the camera, and the camera belongs
    with the selection that made it.
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    x0: float = -1.0
    y0: float = -1.0
    x1: float = 1.0
    y1: float = 1.0

    def unit(self, node: Node) -> tuple[float, float]:
        """A node's position as 0→1 across the view. Clamped, never wrapped."""
        dx = self.x1 - self.x0 or 1.0
        dy = self.y1 - self.y0 or 1.0
        return (min(1.0, max(0.0, (node.x - self.x0) / dx)),
                min(1.0, max(0.0, (node.y - self.y0) / dy)))


@dataclass
class Graph:
    """Every note and every resolved link, laid out."""

    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    adj: dict[str, set[str]] = field(default_factory=dict)
    # Basename → the notes that could be meant by it. Obsidian resolves a bare
    # `[[Home]]` this way and so must this, or two thirds of the links in a
    # vault that uses short wikilinks simply do not exist.
    _by_stem: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    # ---- finding a note ------------------------------------------------
    def resolve(self, candidate: str) -> str | None:
        """Turn something path-shaped into a note that exists, or nothing.

        The candidates arrive from `shells.ShellEvent`, which recognises the
        shape of a vault path and deliberately checks nothing else — so this
        is where a path becomes a note. Three ways, in order of confidence:
        the exact rel, the same path with an extension put back, and finally
        the basename, which is how a wikilink usually names its target.

        An ambiguous basename resolves to the first match in sorted order.
        Guessing is not ideal, but the alternative is dropping the link — and
        a graph missing its ambiguous edges is a *misleading* picture, where
        one with an occasional edge on the wrong twin is merely an imprecise
        one.
        """
        if not candidate:
            return None
        cand = candidate.strip().split("#")[0].split("|")[0].strip()
        if not cand:
            return None
        if cand in self.nodes:
            return cand
        if cand + ".md" in self.nodes:
            return cand + ".md"
        stem = os.path.basename(cand)
        if stem.endswith(".md"):
            stem = stem[:-3]
        hits = self._by_stem.get(stem.lower())
        return hits[0] if hits else None

    # ---- carving a piece out of it -------------------------------------
    def neighbourhood(self, focus: list[str], hops: int = 1,
                      limit: int = 22) -> View:
        """The notes around a focus, and the links among them.

        Breadth-first from every focused note at once, so a search that lit
        eight notes gives one connected picture rather than eight islands.
        Truncation is by hop first and degree second: if a note has forty
        neighbours, the ones worth drawing at this size are the ones that are
        themselves well connected, because those are the notes you will
        recognise.

        Then it is laid out *again*, over just these nodes. Keeping the global
        coordinates would be the cheaper thing and it does not work: twenty
        notes that sit inside one cluster of the whole vault occupy a corner
        of it, so the neighbourhood arrives as a bundle of near-parallel lines
        with everything overlapping. The relaxation is seeded from the global
        positions, so the piece keeps the orientation it has in the big
        picture — Work still opens to the same side — and then spreads to fill
        the pane. This is Obsidian's local-graph behaviour and it is the same
        reasoning: a neighbourhood is a diagram of six notes, not a crop of a
        photograph.
        """
        picked: list[str] = []
        seen: set[str] = set()
        frontier = [r for r in focus if r in self.nodes]
        for r in frontier:
            if r not in seen:
                seen.add(r)
                picked.append(r)
        for _ in range(max(0, hops)):
            nxt: list[str] = []
            for rel in frontier:
                for other in sorted(self.adj.get(rel, ()),
                                    key=lambda o: (-self.nodes[o].deg, o)):
                    if other not in seen:
                        seen.add(other)
                        picked.append(other)
                        nxt.append(other)
            frontier = nxt
            if len(picked) >= limit:
                break
        picked = picked[:limit]
        keep = set(picked)
        edges = [(a, b) for a, b in self.edges if a in keep and b in keep]
        sub = {r: {o for o in self.adj.get(r, ()) if o in keep} for r in picked}
        seeds = {r: (self.nodes[r].x, self.nodes[r].y) for r in picked}
        pos = _relax(picked, edges, sub, seeds=seeds,
                     iterations=_LOCAL_ITERATIONS)
        # Copies, not the graph's own nodes: two views of the same note must
        # not be able to move each other, and the whole-vault view is holding
        # the originals.
        nodes = [Node(rel=r, title=self.nodes[r].title,
                      folder=self.nodes[r].folder, deg=self.nodes[r].deg,
                      x=pos[r][0], y=pos[r][1]) for r in picked]
        return _framed(nodes, edges, pad=0.30)

    def whole(self) -> View:
        """Every note, at the vault's own proportions."""
        nodes = list(self.nodes.values())
        return _framed(nodes, self.edges, pad=0.20, square=True)


def _framed(nodes: list[Node], edges: list[tuple[str, str]],
            pad: float = 0.1, square: bool = False) -> View:
    """Fit a camera around a set of nodes.

    `square` keeps the vault's true aspect — the full canvas is a picture of a
    thing and stretching it would be a lie about its shape. A neighbourhood is
    not a picture of anything, it is a diagram of six notes, so it is allowed
    to fill its box.

    The margins are wide because the camera in `ui.graph` sways, breathes and
    tilts inside them. They are the room it moves in: too little and the
    outermost notes leave the frame at the ends of every swing.
    """
    if not nodes:
        return View()
    xs = [n.x for n in nodes]
    ys = [n.y for n in nodes]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    # A single node, or a row of them, has no extent on one axis and would
    # divide by zero on the way to the screen. Give it one.
    if x1 - x0 < 1e-6:
        x0, x1 = x0 - 0.5, x1 + 0.5
    if y1 - y0 < 1e-6:
        y0, y1 = y0 - 0.5, y1 + 0.5
    if square:
        span = max(x1 - x0, y1 - y0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, x1 = cx - span / 2, cx + span / 2
        y0, y1 = cy - span / 2, cy + span / 2
    mx, my = (x1 - x0) * pad, (y1 - y0) * pad
    return View(nodes=nodes, edges=edges,
                x0=x0 - mx, y0=y0 - my, x1=x1 + mx, y1=y1 + my)


# ---------------------------------------------------------------------------
# loading


def load(settings: Settings | None = None,
         conn: sqlite3.Connection | None = None) -> Graph:
    """Read the graph out of the index, laying it out if the links moved.

    Never raises. A vault with no index yet, a database mid-rebuild, an
    unwritable cache — all of them return an empty graph, and an empty graph
    draws as nothing at all. This is decoration on a status pane: there is no
    failure here worth interrupting an answer for.
    """
    s = settings or default_settings
    own = conn is None
    try:
        if conn is None:
            if not s.db_path.exists():
                return Graph()
            conn = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT rel, title, folder FROM notes").fetchall()
        raw = conn.execute("SELECT src, dst FROM links").fetchall()
    except sqlite3.Error:
        return Graph()
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    g = Graph()
    for rel, title, folder in rows:
        g.nodes[rel] = Node(rel=rel, title=title or _stem(rel),
                            folder=folder or "")
        g._by_stem.setdefault(_stem(rel).lower(), []).append(rel)
    for hits in g._by_stem.values():
        hits.sort()

    seen: set[tuple[str, str]] = set()
    for src, dst in raw:
        a, b = g.resolve(src), g.resolve(dst)
        # A link to a note that does not exist is real and common — every
        # weekly note links a month ahead — but it has no position, so there
        # is nothing to draw. Dropped rather than drawn as a phantom: a
        # phantom node in a 28-column pane is indistinguishable from a real
        # one, and a picture that invents notes is worse than one that omits
        # broken links.
        if not a or not b or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        g.edges.append(key)
        g.adj.setdefault(a, set()).add(b)
        g.adj.setdefault(b, set()).add(a)
    for rel, node in g.nodes.items():
        node.deg = len(g.adj.get(rel, ()))

    for rel, (x, y) in _positions(g, s).items():
        node = g.nodes.get(rel)
        if node:
            node.x, node.y = x, y
    return g


def _stem(rel: str) -> str:
    base = os.path.basename(rel)
    return base[:-3] if base.endswith(".md") else base


# ---------------------------------------------------------------------------
# layout


def _signature(rels: list[str], edges: list[tuple[str, str]]) -> str:
    """What the cached layout was computed from.

    The node *set* and the edge set, and nothing else — not mtimes, not the
    body text. Editing a note's prose does not move it in the graph, and a
    cache that invalidated on every save would be recomputed on every save for
    a picture that came out identical.
    """
    h = hashlib.blake2b(digest_size=16)
    for rel in sorted(rels):
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\x00")
    h.update(b"\xff")
    for a, b in sorted(edges):
        h.update(f"{a}\x00{b}\x00".encode("utf-8", "replace"))
    return h.hexdigest()


def _positions(g: Graph, s: Settings) -> dict[str, tuple[float, float]]:
    """Cached positions for this link-set, computing them if it changed."""
    rels = list(g.nodes)
    if not rels:
        return {}
    sig = _signature(rels, g.edges)
    path = s.state_dir / "graph-layout.json"
    try:
        cached = json.loads(path.read_text())
        if cached.get("signature") == sig:
            return {k: (float(v[0]), float(v[1]))
                    for k, v in cached["pos"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pos = _relax(rels, g.edges, g.adj)
    try:
        s.ensure_dirs()
        path.write_text(json.dumps(
            {"signature": sig,
             "pos": {k: [round(x, 5), round(y, 5)] for k, (x, y) in pos.items()}}))
    except OSError:
        pass                                 # a read-only state dir still draws
    return pos


def _seeded(rel: str) -> tuple[float, float]:
    """A note's starting point, from its path.

    On a phyllotactic spiral rather than at a uniform random point, because
    the relaxation below is a local method: it improves whatever it is handed
    and cannot escape a bad start. Uniform random puts clusters of notes on
    top of each other and leaves holes elsewhere, and 260 iterations is not
    enough to undo that. A spiral starts evenly spread, so the forces spend
    their whole budget on structure instead of on untangling the seed.
    """
    h = hashlib.blake2b(rel.encode("utf-8", "replace"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    # The golden angle, which is what makes the spiral fill evenly.
    ang = (n % 100003) * 2.39996323
    rad = math.sqrt((n >> 20) % 100003 / 100003.0)
    return math.cos(ang) * rad, math.sin(ang) * rad


def _relax(rels: list[str], edges: list[tuple[str, str]],
           adj: dict[str, set[str]], *,
           seeds: dict[str, tuple[float, float]] | None = None,
           iterations: int = _ITERATIONS) -> dict[str, tuple[float, float]]:
    """Fruchterman-Reingold with bucketed repulsion. Deterministic.

    `seeds` supplies starting positions — the local layout hands in the global
    ones so a neighbourhood keeps its bearings instead of being re-derived
    from scratch in a random orientation. Without them the start is a hashed
    spiral, which is the same thing on a vault that has no prior layout.
    """
    n = len(rels)
    if not n:
        # A neighbourhood focused on a note that is not in the vault, which
        # happens whenever V reads a file the index has not seen yet. There is
        # nothing to lay out and it is not an error.
        return {}
    idx = {rel: i for i, rel in enumerate(rels)}
    xs = [0.0] * n
    ys = [0.0] * n
    for rel, i in idx.items():
        xs[i], ys[i] = (seeds or {}).get(rel) or _seeded(rel)
    if seeds:
        # Seeded positions arrive from a cluster of the whole graph, so they
        # are all within a few hundredths of each other. Repulsion at that
        # separation is enormous and the first pass would fire them apart at
        # random; spreading them to the working scale first keeps the
        # orientation they came in with.
        cx = sum(xs) / n
        cy = sum(ys) / n
        span = max(max(abs(x - cx) for x in xs),
                   max(abs(y - cy) for y in ys), 1e-6)
        xs = [(x - cx) / span for x in xs]
        ys = [(y - cy) / span for y in ys]
    lone = [not adj.get(rel) for rel in rels]
    pairs = [(idx[a], idx[b]) for a, b in edges if a in idx and b in idx]

    # The ideal edge length for n nodes spread over an area of about 4. Fewer
    # notes means a looser graph, which is right: six nodes at the spacing of
    # six hundred is a picture of nothing.
    k = math.sqrt(4.0 / max(1, n))
    span = max(k * _BUCKET_SPAN, 1e-4)
    temp = 0.28

    for step in range(iterations):
        fx = [0.0] * n
        fy = [0.0] * n

        # Repulsion, against the 3×3 block of buckets around each node. The
        # grid is rebuilt every iteration because the nodes moved; that is one
        # dict of small lists, and cheaper than any scheme for updating it.
        grid: dict[tuple[int, int], list[int]] = {}
        for i in range(n):
            grid.setdefault((int(xs[i] // span), int(ys[i] // span)),
                            []).append(i)
        for (gx, gy), members in grid.items():
            near: list[int] = []
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    near.extend(grid.get((gx + ox, gy + oy), ()))
            for i in members:
                xi, yi = xs[i], ys[i]
                damp_i = _LONE_DAMP if lone[i] else 1.0
                for j in near:
                    if j == i:
                        continue
                    dx = xi - xs[j]
                    dy = yi - ys[j]
                    d2 = dx * dx + dy * dy
                    if d2 < 1e-9:
                        # Exactly coincident. Push apart along a direction
                        # derived from the indices so it stays deterministic —
                        # a random jitter here would make the whole layout
                        # irreproducible for the sake of a rare tie.
                        ang = (i * 7 + j * 13) % 360 * math.pi / 180.0
                        fx[i] += math.cos(ang) * k * 0.01
                        fy[i] += math.sin(ang) * k * 0.01
                        continue
                    push = (k * k) / d2 * damp_i
                    if lone[j]:
                        push *= _LONE_DAMP
                    fx[i] += dx * push
                    fy[i] += dy * push

        # Attraction, along the edges only.
        for i, j in pairs:
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 1e-9:
                continue
            pull = dist / k
            ux, uy = dx / dist, dy / dist
            fx[i] -= ux * pull
            fy[i] -= uy * pull
            fx[j] += ux * pull
            fy[j] += uy * pull

        # A weak pull to the origin, which is not part of the classic method
        # and is here because a disconnected vault has nothing else holding it
        # together — without it the isolated notes drift outward forever and
        # the frame has to zoom out until the linked core is three cells wide.
        for i in range(n):
            fx[i] -= xs[i] * 0.012
            fy[i] -= ys[i] * 0.012

        for i in range(n):
            d = math.sqrt(fx[i] * fx[i] + fy[i] * fy[i])
            if d < 1e-9:
                continue
            step_len = min(d, temp) / d
            xs[i] += fx[i] * step_len
            ys[i] += fy[i] * step_len
        temp *= 0.985 if step < iterations * 0.75 else 0.94

    # Normalise so the furthest node sits at radius 1, and centre on the
    # middle of the extent rather than on the mean: one dense cluster and one
    # far outlier would otherwise pull the centre into the cluster and leave
    # half the canvas empty.
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    scale = max(max(abs(x - cx) for x in xs), max(abs(y - cy) for y in ys), 1e-6)
    return {rel: ((xs[i] - cx) / scale, (ys[i] - cy) / scale)
            for rel, i in idx.items()}
