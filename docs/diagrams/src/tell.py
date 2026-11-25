# What happens to something a person tells the wiki, as `records.tell` and `compile` do it.
# A tell is kept whole as a record, then becomes facts, proposals, both or neither, each citing the record: with no
# `as`, the decision model sorts it sentence by sentence, and what it cannot place comes back asking for `as`,
# `kind` and `target`. Facts come out of Factoid. Only a run writes the article's text, and front matter has two
# writers, the article's curator when it admits it and its agents when they run: a tell writes none.
# Three columns. The person sits above Cerebro's outline.

DIAGRAM = {
    "title": "What you tell is kept whole, then becomes facts or a proposal citing it.",
    "cols": 3, "gutter": 40, "pad": 40, "gap": 40,
    "items": [
        {"box": "you", "row": 0, "col": 1, "title": "A person", "lines": ["tell { text, target }"]},

        {"group": "cerebro", "row": 1, "col": 0, "span": 3, "outset": 16, "style": "product",
         "label": "Cerebro",
         "cols": 3, "gutter": 40, "pad": 16, "gap": 40,
         "items": [
             {"box": "record", "row": 0, "col": 1, "title": "A first-party record",
              "lines": ["kept whole: it has", "no other copy"]},

             {"box": "facts", "row": 1, "col": 0, "title": "Facts",
              "lines": ["drawn by Factoid from", "the record, citing it"]},
             {"box": "proposal", "row": 1, "col": 2, "title": "A proposal",
              "lines": ["citing the record: an", "instruction applies now,", "a sorted admit waits"]},

             {"box": "run", "row": 2, "col": 0, "span": 3, "title": "The agent's next run",
              "lines": ["reads the new facts as evidence, and the proposals left for it"]},

             {"box": "article", "row": 3, "col": 0, "span": 3, "title": "The article",
              "lines": ["its text: only a run writes it",
                        "its front matter: its curator at an admit, and its agents"]},
         ]},
    ],
    "arrows": [
        {"from": "you", "to": "record"},
        {"from": "record", "to": "facts", "route": "hv", "label": "what it states", "seg": 0},
        {"from": "record", "to": "proposal", "route": "hv", "label": "what it asks", "seg": 0},
        {"from": "facts", "to": "run"},
        {"from": "proposal", "to": "run"},
        {"from": "run", "to": "article", "label": "writes its text and fields"},
    ],
}
