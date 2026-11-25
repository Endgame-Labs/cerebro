# Diagrams

The diagrams for Cerebro's README and docs. Each is a small spec under `src/`, and `build.py` turns it into two SVG
files beside this README: `<name>.dark.svg` for a dark page and `<name>.light.svg` for a light one. Both are
committed. Edit the spec, never the SVG.

| Diagram | What it shows |
| --- | --- |
| `stack` | Cerebro is the stack, and Factoid is its facts layer |
| `tell` | What happens to something a person tells the wiki, and why a correction or a withdrawal holds |

## Build

```sh
python3 docs/diagrams/build.py            # write every SVG
python3 docs/diagrams/build.py stack      # write one diagram's two SVGs
python3 docs/diagrams/build.py --check    # fail if a committed SVG differs from what the specs make
```

`build.py` needs the standard library and nothing else. It refuses, with one line per problem, a label that would
not fit its box, two labels that would overlap, an arrow label that would sit on a line or a box, and an arrow that
would cross a box or a label. Label widths come from a table of the widest of several common system fonts, with a
margin, so a label that passes fits on any of them.

## Show one on GitHub

GitHub picks the file that matches the reader's theme. In the repository's top-level README:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/stack.dark.svg">
  <img alt="Cerebro is the stack. Factoid is its facts layer." src="docs/diagrams/stack.light.svg">
</picture>
```

From a file in `docs/`, the paths start at `diagrams/`. Use the diagram's title as its `alt` text.

## Add a diagram

1. Copy a spec in `src/` to `src/<name>.py` and change its title, items and arrows.
2. Run `python3 docs/diagrams/build.py <name>` until it builds with no complaint.
3. Look at both SVGs, then commit the spec and the two files together.

## Writing a spec

A spec is one dict named `DIAGRAM`. Nothing is placed by guesswork: you say where each thing goes on a grid.

- **The page and each group are a grid.** `cols` columns of equal width, `gutter` pixels between them, and `pad`
  pixels inside the left and right edges. The page is 760 wide. Rows stack top to bottom, `gap` apart (16 unless
  given), and `gaps` sets the gap above one row, as `{2: 40}`.
- **An item takes a cell.** `row`, `col`, and `span` columns. `outset` lets it reach that many pixels past its cell on
  each side. A row is as tall as its tallest item, and every item in it takes that height.
- **A box** is `{"box": id, "title": ..., "lines": [...]}`: a bold name and muted lines under it. `style` is
  `dashed` for what is the builder's to add, or `plain` for a row of plain words with a faint outline.
- **A group** is `{"group": id, "label": ..., "items": [...]}` with its own grid. Its label sits at the top left, or
  `label_dx` pixels further right to clear an arrow. `style: "product"` draws the one blue outline around Cerebro
  itself. `pad_bottom` sets the room under its last row.
- **An arrow** is `{"from": id, "to": id, "label": ...}`, between boxes or groups. Two boxes one above the other, or
  side by side, get a straight line through the narrower one's middle. `start` or `end` moves it, as a fraction
  across the first or the second. A `route` bends it: `under` or `over` runs below or above both (`depth` pixels),
  `vhv` turns in the gap between rows (`turn`), `hv` leaves a side and turns into a top or bottom, and `vh` leaves a
  top or bottom and turns into a side. A label goes beside the longest segment, or segment `seg`, at `pos` along it,
  on the `side` given: `right` or `left` of a vertical line, `above` or `below` a horizontal one.

A diagram holds at most 14 boxes. More than that is two diagrams.
