# The stack: who reaches the wiki, its core, its context layer, and where data comes from.
# Three columns on the page. Cerebro's own outline reaches 16px past them on each side, as the systems row does.

DIAGRAM = {
    "title": "Cerebro is the stack. Factoid is its facts layer.",
    "cols": 3, "gutter": 16, "pad": 40, "gap": 24,
    "items": [
        {"box": "agents", "row": 0, "col": 0, "span": 2,
         "title": "Outside agents", "lines": ["Claude, Cursor, or Codex, over MCP"]},
        {"box": "you", "row": 0, "col": 2, "title": "You"},

        {"group": "cerebro", "row": 1, "col": 0, "span": 3, "outset": 16, "style": "product",
         "label": "Cerebro: a wiki your agents write", "label_dx": 136,
         "cols": 3, "gutter": 16, "pad": 16, "gaps": {2: 40}, "pad_bottom": 28,
         "items": [
             {"group": "core", "row": 0, "col": 0, "span": 3, "label": "Core",
              "cols": 3, "gutter": 16, "pad": 0,
              "items": [
                  {"box": "mcp", "row": 0, "col": 0, "title": "MCP surface", "lines": ["Six tools over every kind"]},
                  {"box": "human", "row": 0, "col": 1, "title": "Human input",
                   "lines": ["tell, decide, retract, review"]},
                  {"box": "cli", "row": 0, "col": 2, "title": "CLI", "lines": ["Run Cerebro from a terminal"]},
                  {"box": "models", "row": 1, "col": 0, "title": "Model calls and budget",
                   "lines": ["A cap for each layer"]},
                  {"box": "store", "row": 1, "col": 1, "title": "Store", "lines": ["SQLite to start, hooks for more"]},
                  {"box": "search", "row": 1, "col": 2, "title": "Search", "lines": ["BM25, re-ranked if set"]},
              ]},
             {"group": "context", "row": 1, "col": 0, "span": 3, "label": "Context layer",
              "cols": 2, "gutter": 72, "pad": 16,
              "items": [
                  {"box": "facts", "row": 0, "col": 0, "title": "Facts",
                   "lines": ["by Factoid", "(also stands alone)"]},
                  {"box": "articles", "row": 0, "col": 1, "title": "Articles",
                   "lines": ["by context agents", "Curators admit them"]},
              ]},
             {"box": "connectors", "row": 2, "col": 0, "title": "Connectors", "lines": ["Core: hold the logins"]},
             {"box": "notes", "row": 2, "col": 1, "style": "dashed", "title": "Apps: Notes", "lines": ["Yours to add"]},
         ]},

        {"box": "systems", "row": 2, "col": 0, "span": 3, "outset": 16, "style": "plain",
         "title": "Your systems: CRM, call recorder, mailbox, folders"},
    ],
    "arrows": [
        {"from": "agents", "to": "mcp"},
        {"from": "you", "to": "cli"},
        {"from": "facts", "to": "articles", "label": "evidence"},
        {"from": "connectors", "to": "facts", "label": "documents"},
        {"from": "notes", "to": "facts", "label": "documents", "side": "left", "end": 0.89},
        {"from": "connectors", "to": "articles", "label": "direct evidence",
         "route": "under", "start": 0.9, "end": 0.3, "seg": 2, "pos": 0.78},
        {"from": "systems", "to": "connectors", "label": "read only", "end": 0.5, "pos": 0.23},
    ],
}
