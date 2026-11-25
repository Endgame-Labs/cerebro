#!/usr/bin/env python3
"""Build the docs diagrams: each spec under `src/` becomes `<name>.dark.svg` and `<name>.light.svg` beside it.

A spec is plain Python data, one dict named `DIAGRAM`: a title, items laid on a grid, and arrows. Nothing is placed
by guesswork. Each container (the page, or a group) has columns of equal width; an item names its row, its column
and how many columns it spans. A row is as tall as its tallest item. The generator turns that into coordinates and
draws them in one fixed style.

The build refuses, with one line per problem, a label that would not fit its box, two labels that would overlap,
an arrow label that would sit on a line or a box, and an arrow that would cross a box or a label. Widths are
estimated from a table of character widths, the widest of several common system fonts, with a margin on top.

    python3 docs/diagrams/build.py            # write every SVG
    python3 docs/diagrams/build.py stack      # write one diagram's two SVGs
    python3 docs/diagrams/build.py --check    # fail if a committed SVG differs from what the specs make

Standard library only.
"""

from __future__ import annotations

import argparse
import itertools
import runpy
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"

# -- the look --------------------------------------------------------------------------------------------------------

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
THEMES = {
    "dark": {
        "page": "#0b0b0b", "ink": "#f0efec", "muted": "#c3c2b8", "edge": "#3a3a37", "faint": "#262626",
        "tint": "#24282d", "product_fill": "#10151c", "product": "#4d85de",
    },
    "light": {
        "page": "#ffffff", "ink": "#1f2328", "muted": "#59636e", "edge": "#b4bcc5", "faint": "#d5dae0",
        "tint": "#f2f4f7", "product_fill": "#f5f8fe", "product": "#2f6fdb",
    },
}
TITLE_SIZE, NAME_SIZE, LINE_SIZE = 15, 13, 11.5
TITLE_X, TITLE_Y, TOP, BOTTOM = 24, 28, 48, 24
RADIUS, STROKE, PRODUCT_STROKE = 8, 1.25, 2
TEXT_PAD = 16            # a box's text sits this far in from its left edge
END_PAD = 12             # and must end at least this far from its right edge
LINE_STEP = 16           # baseline to baseline inside a box
HEAD = (7.5, 3.75)       # an arrow head's length and half width
HEADER = 32              # a group's label band
PRODUCT_HEADER = 36      # and the product's own, a little taller
LABEL_GAP = 8            # an arrow label's distance from its line
MAX_BOXES = 14           # more than this is two diagrams

# Advance widths per 1000 units of font size, for ASCII 32 to 126: at each character the widest of Liberation Sans
# (Arial's metrics), Inter (close to San Francisco), DejaVu Sans and FreeSans, measured in Chromium.
_CHARS = "".join(chr(c) for c in range(32, 127))
_REGULAR = [
    318, 401, 466, 838, 642, 982, 780, 300, 391, 391, 501, 838, 318, 460, 318, 361, 637, 637, 637, 637, 646, 637, 637,
    637, 637, 637, 337, 337, 838, 838, 838, 557, 1016, 690, 687, 731, 771, 667, 611, 778, 752, 295, 571, 674, 566, 904,
    754, 788, 667, 788, 723, 667, 646, 745, 690, 989, 686, 679, 686, 391, 361, 391, 838, 557, 500, 613, 635, 572, 635,
    616, 371, 635, 634, 278, 278, 580, 278, 975, 634, 612, 635, 635, 412, 528, 393, 634, 592, 819, 592, 592, 553, 637,
    337, 637, 838,
]
_BOLD = [
    349, 457, 523, 838, 696, 1005, 873, 326, 458, 458, 540, 838, 380, 466, 380, 379, 696, 696, 696, 696, 696, 696, 696,
    696, 696, 696, 400, 400, 838, 838, 838, 611, 1000, 774, 763, 737, 831, 684, 684, 821, 837, 373, 580, 775, 638, 996,
    837, 851, 733, 851, 771, 721, 683, 813, 774, 1104, 771, 725, 726, 458, 379, 458, 838, 557, 500, 675, 716, 593, 716,
    679, 436, 716, 712, 343, 343, 666, 343, 1042, 712, 688, 716, 716, 494, 596, 479, 712, 652, 924, 646, 652, 583, 712,
    366, 712, 838,
]
WIDTHS = {400: dict(zip(_CHARS, _REGULAR, strict=True)), 600: dict(zip(_CHARS, _BOLD, strict=True))}
MARGIN = 1.05            # on top of the widest font
ASCENT, DESCENT = 0.8, 0.25


class SpecError(Exception):
    """A spec the generator refuses: the message is one line that names the culprit."""


def text_width(text: str, size: float, weight: int = 400) -> float:
    table = WIDTHS[weight]
    return sum(table.get(ch, 1000) for ch in text) * size / 1000 * MARGIN


# -- geometry --------------------------------------------------------------------------------------------------------

@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def r(self) -> float:
        return self.x + self.w

    @property
    def b(self) -> float:
        return self.y + self.h

    def grow(self, d: float) -> Rect:
        return Rect(self.x - d, self.y - d, self.w + 2 * d, self.h + 2 * d)

    def meets(self, o: Rect) -> bool:
        return self.x < o.r and o.x < self.r and self.y < o.b and o.y < self.b

    def holds(self, o: Rect) -> bool:
        return self.x <= o.x and self.y <= o.y and o.r <= self.r and o.b <= self.b


@dataclass
class Text:
    x: float
    y: float
    s: str
    size: float
    weight: int
    color: str
    anchor: str = "start"
    owner: str = ""

    @property
    def rect(self) -> Rect:
        w = text_width(self.s, self.size, self.weight)
        x = self.x - (w if self.anchor == "end" else w / 2 if self.anchor == "middle" else 0)
        return Rect(x, self.y - ASCENT * self.size, w, (ASCENT + DESCENT) * self.size)


@dataclass
class Item:
    id: str
    kind: str                       # box or group
    spec: dict
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    children: list[Item] = field(default_factory=list)
    depth: int = 0


def segment_rect(a: tuple[float, float], b: tuple[float, float], half: float = STROKE / 2) -> Rect:
    x0, x1 = sorted((a[0], b[0]))
    y0, y1 = sorted((a[1], b[1]))
    return Rect(x0 - half, y0 - half, x1 - x0 + 2 * half, y1 - y0 + 2 * half)


# -- layout ----------------------------------------------------------------------------------------------------------

def _lines(spec: dict) -> list[str]:
    return list(spec.get("lines", []))


def box_height(spec: dict) -> float:
    n = 1 + len(_lines(spec)) if spec.get("title") else len(_lines(spec))
    return 24 + LINE_STEP * max(n, 1)


def _grid(container: dict, w: float) -> tuple[float, float, int, float]:
    cols, gutter, pad = container.get("cols", 1), container.get("gutter", 16), container.get("pad", 16)
    return pad, (w - 2 * pad - (cols - 1) * gutter) / cols, cols, gutter


def _cell(spec: dict, x: float, colw: float, gutter: float) -> tuple[float, float]:
    out = spec.get("outset", 0)
    span = spec.get("span", 1)
    return x + spec.get("col", 0) * (colw + gutter) - out, span * colw + (span - 1) * gutter + 2 * out


def _rows(container: dict, w: float, where: str) -> tuple[list[float], list[float]]:
    """Each row's height and the gap above it, for a container of width `w`."""
    _, colw, cols, gutter = _grid(container, w)
    items = container.get("items", [])
    count = 1 + max((it.get("row", 0) for it in items), default=-1)
    heights = [float(h) for h in [container.get("heights", {}).get(r, 0) for r in range(count)]]
    taken: dict[int, list[tuple[int, int, str]]] = {}
    for it in items:
        name = it.get("box") or it.get("group")
        col, span = it.get("col", 0), it.get("span", 1)
        if col < 0 or span < 1 or col + span > cols:
            raise SpecError(f"{where}: {name} takes columns {col} to {col + span - 1}, and its container has {cols}")
        row = it.get("row", 0)
        for c0, c1, other in taken.get(row, []):
            if col < c1 and c0 < col + span:
                raise SpecError(f"{where}: {name} and {other} share row {row} and a column")
        taken.setdefault(row, []).append((col, col + span, name))
        _, cw = _cell(it, 0, colw, gutter)
        need = group_height(it, cw, where) if "group" in it else box_height(it)
        if "height" in it and it["height"] < need:
            raise SpecError(f"{where}: {name} is {it['height']}px tall and its text needs {need}px")
        heights[row] = max(heights[row], it.get("height", need))
    for r, h in enumerate(heights):
        if h == 0:
            raise SpecError(f"{where}: row {r} holds nothing: give it a height in heights, or remove it")
    gaps = [0.0] + [float(container.get("gaps", {}).get(r, container.get("gap", 16))) for r in range(1, count)]
    return heights, gaps


def _head(spec: dict) -> float:
    """How far a group's first row sits below its top edge."""
    if not spec.get("label"):
        return spec.get("pad_bottom", 16)
    return PRODUCT_HEADER if spec.get("style") == "product" else HEADER


def group_height(spec: dict, w: float, where: str) -> float:
    heights, gaps = _rows(spec, w, where)
    return _head(spec) + sum(heights) + sum(gaps) + spec.get("pad_bottom", 16)


def place(container: dict, x: float, y: float, w: float, depth: int, where: str, out: list[Item]) -> list[Item]:
    pad, colw, _, gutter = _grid(container, w)
    heights, gaps = _rows(container, w, where)
    tops, at = [], y
    for h, g in zip(heights, gaps, strict=True):
        at += g
        tops.append(at)
        at += h
    placed = []
    for it in container.get("items", []):
        row = it.get("row", 0)
        ix, iw = _cell(it, x + pad, colw, gutter)
        rect = Rect(ix, tops[row], iw, heights[row])
        if "group" in it:
            item = Item(it["group"], "group", it, rect, depth=depth)
            item.children = place(it, ix, rect.y + _head(it), iw, depth + 1, where, out)
        else:
            item = Item(it["box"], "box", it, rect, depth=depth)
        out.append(item)
        placed.append(item)
    return placed


# -- arrows ----------------------------------------------------------------------------------------------------------

Point = tuple[float, float]


def _along(lo: float, size: float, frac: float | None, default: float) -> float:
    return lo + size * frac if frac is not None else default


def route(spec: dict, a: Rect, b: Rect, where: str) -> list[Point]:
    """The corner points of an arrow from `a` to `b`, as its spec's `route` says."""
    how = spec.get("route", "straight")
    start, end = spec.get("start"), spec.get("end")
    name = f"{where}: arrow {spec['from']} to {spec['to']}"
    if how == "straight":
        if b.y >= a.b or b.b <= a.y:                       # one above the other
            lo, hi = max(a.x, b.x), min(a.r, b.r)
            if hi - lo < 16:
                raise SpecError(f"{name}: the boxes do not line up across, so give it a route")
            narrow = a if a.w <= b.w else b
            mid = narrow.x + narrow.w / 2 if lo + 12 <= narrow.x + narrow.w / 2 <= hi - 12 else (lo + hi) / 2
            x = _along(a.x, a.w, start, mid) if end is None else b.x + b.w * end
            if not lo <= x <= hi:
                raise SpecError(f"{name}: its line at x={x:g} misses one of the two boxes")
            return [(x, a.b), (x, b.y)] if b.y >= a.b else [(x, a.y), (x, b.b)]
        if b.x >= a.r or b.r <= a.x:                       # side by side
            lo, hi = max(a.y, b.y), min(a.b, b.b)
            if hi - lo < 16:
                raise SpecError(f"{name}: the boxes do not line up down, so give it a route")
            narrow = a if a.h <= b.h else b
            mid = narrow.y + narrow.h / 2 if lo + 12 <= narrow.y + narrow.h / 2 <= hi - 12 else (lo + hi) / 2
            y = _along(a.y, a.h, start, mid) if end is None else b.y + b.h * end
            if not lo <= y <= hi:
                raise SpecError(f"{name}: its line at y={y:g} misses one of the two boxes")
            return [(a.r, y), (b.x, y)] if b.x >= a.r else [(a.x, y), (b.r, y)]
        raise SpecError(f"{name}: the boxes overlap")
    sx = _along(a.x, a.w, start, a.x + a.w / 2)
    ex = _along(b.x, b.w, end, b.x + b.w / 2)
    if how in ("under", "over"):
        depth = spec.get("depth", 14)
        if how == "under":
            y = max(a.b, b.b) + depth
            return [(sx, a.b), (sx, y), (ex, y), (ex, b.b)]
        y = min(a.y, b.y) - depth
        return [(sx, a.y), (sx, y), (ex, y), (ex, b.y)]
    if how == "vhv":                                       # down or up, across in the gap, then on into b
        if b.y >= a.b:
            y = _along(a.b, b.y - a.b, spec.get("turn"), (a.b + b.y) / 2)
            return [(sx, a.b), (sx, y), (ex, y), (ex, b.y)]
        if b.b <= a.y:
            y = _along(b.b, a.y - b.b, spec.get("turn"), (a.y + b.b) / 2)
            return [(sx, a.y), (sx, y), (ex, y), (ex, b.b)]
        raise SpecError(f"{name}: vhv needs one box above the other")
    if how == "hv":                                        # out of a's side, then down or up into b
        sy = _along(a.y, a.h, start, a.y + a.h / 2)
        side = a.r if ex >= a.r else a.x
        if not (ex >= a.r or ex <= a.x):
            raise SpecError(f"{name}: hv needs b's end point beside a")
        return [(side, sy), (ex, sy), (ex, b.y if b.y >= sy else b.b)]
    if how == "vh":                                        # out of a's top or bottom, then across into b's side
        ey = _along(b.y, b.h, end, b.y + b.h / 2)
        sx = _along(a.x, a.w, start, a.x + a.w / 2)
        top = a.b if ey >= a.b else a.y
        return [(sx, top), (sx, ey), (b.x if b.x >= sx else b.r, ey)]
    raise SpecError(f"{name}: route {how} is not one of straight, under, over, vhv, hv, vh")


def head(points: list[Point]) -> tuple[list[Point], list[Point]]:
    """The line, shortened to the head's base, and the head's three corners."""
    (x0, y0), (x1, y1) = points[-2], points[-1]
    length = abs(x1 - x0) + abs(y1 - y0)
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    long, half = HEAD
    bx, by = x1 - dx * long, y1 - dy * long
    tri = [(x1, y1), (bx - dy * half, by + dx * half), (bx + dy * half, by - dx * half)]
    return points[:-1] + [(bx, by)], tri


def arrow_label(spec: dict, points: list[Point], muted: str) -> Text | None:
    if not spec.get("label"):
        return None
    segs = list(itertools.pairwise(points))
    i = spec.get("seg", max(range(len(segs)), key=lambda k: abs(segs[k][1][0] - segs[k][0][0])
                            + abs(segs[k][1][1] - segs[k][0][1])))
    (x0, y0), (x1, y1) = segs[i]
    pos = spec.get("pos", 0.5)
    if x0 == x1:                                           # vertical: beside the line
        side = spec.get("side", "right")
        y = y0 + (y1 - y0) * pos + LINE_SIZE * 0.35
        x = x0 + LABEL_GAP if side == "right" else x0 - LABEL_GAP
        return Text(round(x), round(y), spec["label"], LINE_SIZE, 400, muted, "start" if side == "right" else "end")
    side = spec.get("side", "above")
    x = x0 + (x1 - x0) * pos
    y = y0 - LABEL_GAP - DESCENT * LINE_SIZE if side == "above" else y0 + LABEL_GAP + ASCENT * LINE_SIZE
    return Text(round(x), round(y), spec["label"], LINE_SIZE, 400, muted, "middle")


# -- the drawing -----------------------------------------------------------------------------------------------------

@dataclass
class Drawing:
    name: str
    width: float
    height: float
    title: str
    items: list[Item]
    texts: list[Text]
    arrows: list[tuple[dict, list[Point], list[Point], list[Point]]]   # spec, full path, line, head
    labels: list[Text]


def _box_texts(item: Item, theme: dict) -> list[Text]:
    s, r = item.spec, item.rect
    plain = s.get("style") == "plain"
    rows = ([(s["title"], NAME_SIZE, 400 if plain else 600, theme["ink"])] if s.get("title") else []) \
        + [(line, LINE_SIZE, 400, theme["muted"]) for line in _lines(s)]
    first = r.y + r.h / 2 - LINE_STEP * (len(rows) - 1) / 2 + 4
    return [Text(r.x + TEXT_PAD, first + LINE_STEP * k, t, size, weight, color, owner=item.id)
            for k, (t, size, weight, color) in enumerate(rows)]


def build(name: str, spec: dict, theme: dict) -> Drawing:
    width = spec.get("width", 760)
    items: list[Item] = []
    heights, gaps = _rows(spec, width, name)
    place(spec, 0, TOP, width, 0, name, items)
    height = TOP + sum(heights) + sum(gaps) + BOTTOM
    texts = [Text(TITLE_X, TITLE_Y, spec["title"], TITLE_SIZE, 600, theme["ink"], owner="title")]
    for it in items:
        if it.kind == "group" and it.spec.get("label"):
            texts.append(Text(it.rect.x + TEXT_PAD + it.spec.get("label_dx", 0), it.rect.y + 22, it.spec["label"],
                              NAME_SIZE, 600, theme["ink"], owner=it.id))
        elif it.kind == "box":
            texts += _box_texts(it, theme)
    by_id = {it.id: it for it in items}
    if len(by_id) != len(items):
        raise SpecError(f"{name}: two items share an id")
    arrows, labels = [], []
    for a in spec.get("arrows", []):
        for end in ("from", "to"):
            if a[end] not in by_id:
                raise SpecError(f"{name}: arrow {a['from']} to {a['to']} names {a[end]}, which is no box or group")
        points = route(a, by_id[a["from"]].rect, by_id[a["to"]].rect, name)
        line, tri = head(points)
        arrows.append((a, points, line, tri))
        if (label := arrow_label(a, points, theme["muted"])) is not None:
            label.owner = f"{a['from']}>{a['to']}"
            labels.append(label)
    drawing = Drawing(name, width, height, spec["title"], items, texts, arrows, labels)
    check(drawing)
    return drawing


# -- checks ----------------------------------------------------------------------------------------------------------

def check(d: Drawing) -> None:
    n = d.name
    boxes = [it for it in d.items if it.kind == "box"]
    groups = [it for it in d.items if it.kind == "group"]
    if len(boxes) > MAX_BOXES:
        raise SpecError(f"{n}: {len(boxes)} boxes is more than one screen holds ({MAX_BOXES}): make it two diagrams")
    page = Rect(0, 0, d.width, d.height)
    for t in d.texts:
        if t.owner == "title":
            room = d.width - 2 * TITLE_X
        elif any(g.id == t.owner for g in groups):
            g = next(g for g in groups if g.id == t.owner)
            room = g.rect.r - t.x - END_PAD
        else:
            b = next(b for b in boxes if b.id == t.owner)
            room = b.rect.w - TEXT_PAD - END_PAD
        need = text_width(t.s, t.size, t.weight)
        if need > room:
            raise SpecError(f'{n}: "{t.s}" needs about {need:.0f}px and {t.owner} has {room:.0f}px for it')
    every = d.texts + d.labels
    for t in every:
        if not page.holds(t.rect):
            raise SpecError(f'{n}: "{t.s}" runs off the page')
    for i, t in enumerate(every):
        for u in every[i + 1:]:
            if t.rect.grow(2).meets(u.rect):
                raise SpecError(f'{n}: "{t.s}" and "{u.s}" overlap')
    shapes = []                                            # every drawn line or head, by arrow
    for a, _, line, tri in d.arrows:
        who = f"arrow {a['from']} to {a['to']}"
        for p, q in itertools.pairwise(line):
            shapes.append((who, segment_rect(p, q)))
        xs, ys = [p[0] for p in tri], [p[1] for p in tri]
        shapes.append((who, Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))))
    for who, s in shapes:
        if not page.holds(s):
            raise SpecError(f"{n}: {who} runs off the page")
        for b in boxes:
            if s.meets(b.rect.grow(-1.5)):
                raise SpecError(f"{n}: {who} crosses the box {b.id}")
        for t in d.texts:
            if s.meets(t.rect.grow(2)):
                raise SpecError(f'{n}: {who} runs through "{t.s}"')
    for t in d.labels:
        r = t.rect.grow(3)
        for who, s in shapes:
            if r.meets(s):
                raise SpecError(f'{n}: the label "{t.s}" sits on {who}')
        for b in boxes:
            if r.meets(b.rect):
                raise SpecError(f'{n}: the label "{t.s}" sits on the box {b.id}')
        for g in groups:
            if r.meets(g.rect) and not g.rect.grow(-2).holds(r):
                raise SpecError(f'{n}: the label "{t.s}" sits on the edge of the group {g.id}')


# -- SVG -------------------------------------------------------------------------------------------------------------

def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _rect(r: Rect, fill: str, stroke: str, width: float, dash: str = "") -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{_n(r.x)}" y="{_n(r.y)}" width="{_n(r.w)}" height="{_n(r.h)}" rx="{RADIUS}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{_n(width)}"{extra}/>')


def _text(t: Text) -> str:
    anchor = f' text-anchor="{t.anchor}"' if t.anchor != "start" else ""
    weight = f' font-weight="{t.weight}"' if t.weight != 400 else ""
    return (f'<text x="{_n(t.x)}" y="{_n(t.y)}" font-size="{_n(t.size)}"{weight} fill="{t.color}"{anchor}>'
            f"{_esc(t.s)}</text>")


def svg(d: Drawing, theme: dict) -> str:
    w, h = _n(d.width), _n(d.height)
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-labelledby="{d.name}-title" font-family="{FONT}">'),
           f'<title id="{d.name}-title">{_esc(d.title)}</title>',
           f'<rect width="{w}" height="{h}" fill="{theme["page"]}"/>']
    for g in sorted((it for it in d.items if it.kind == "group"), key=lambda it: it.depth):
        if g.spec.get("style") == "product":
            out.append(_rect(g.rect, theme["product_fill"], theme["product"], PRODUCT_STROKE))
        else:
            out.append(_rect(g.rect, theme["tint"], theme["edge"], STROKE))
    for b in (it for it in d.items if it.kind == "box"):
        style = b.spec.get("style", "solid")
        stroke = theme["faint"] if style == "plain" else theme["edge"]
        out.append(_rect(b.rect, "none", stroke, STROKE, "4 4" if style == "dashed" else ""))
    for _, _, line, tri in d.arrows:
        path = "M" + " L".join(f"{_n(x)} {_n(y)}" for x, y in line)
        out.append(f'<path d="{path}" fill="none" stroke="{theme["edge"]}" stroke-width="{_n(STROKE)}"/>')
        out.append('<path d="M' + " L".join(f"{_n(x)} {_n(y)}" for x, y in tri) + f'Z" fill="{theme["edge"]}"/>')
    out += [_text(t) for t in d.texts + d.labels]
    out.append("</svg>")
    return "\n".join(out) + "\n"


# -- the command -----------------------------------------------------------------------------------------------------

def load(path: Path) -> dict:
    spec = runpy.run_path(str(path)).get("DIAGRAM")
    if not isinstance(spec, dict) or "title" not in spec:
        raise SpecError(f"{path.name}: a spec defines DIAGRAM, a dict with a title")
    return spec


def outputs(name: str) -> dict[Path, str]:
    spec = load(SRC / f"{name}.py")
    return {HERE / f"{name}.{t}.svg": svg(build(name, spec, THEMES[t]), THEMES[t]) for t in THEMES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the docs diagrams from the specs under src/.")
    parser.add_argument("names", nargs="*", help="diagrams to build; all of them when none is given")
    parser.add_argument("--check", action="store_true", help="fail if a committed SVG differs from its spec")
    args = parser.parse_args(argv)
    every = sorted(p.stem for p in SRC.glob("*.py") if not p.stem.startswith("_"))
    names = args.names or every
    try:
        made = {path: text for name in names for path, text in outputs(name).items()}
    except (SpecError, FileNotFoundError) as error:
        print(f"build.py: {error}", file=sys.stderr)
        return 1
    if args.check:
        stale = [p.name for p, text in made.items() if not p.exists() or p.read_text() != text]
        if not args.names:
            stale += [p.name for p in HERE.glob("*.svg") if p not in made]
        if stale:
            verb = "differs" if len(stale) == 1 else "differ"
            print(f"build.py: {', '.join(sorted(stale))} {verb} from the specs: run python3 docs/diagrams/build.py",
                  file=sys.stderr)
            return 1
        return 0
    for path, text in made.items():
        path.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
