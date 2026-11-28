# Test kitchen

A kitchen wiki for Juniper Table, a made-up 40-seat neighborhood restaurant. It is written for a new line cook or sous chef in their first weeks. From recipe cards, staff meeting notes, a health inspection, and supplier invoices, Cerebro writes one article per dish, one per supplier, and an allergen sheet for the pass. A cook can mark a dish 86'd from any MCP client, and the dish's article says so on the next pass.

The restaurant, its people, and its suppliers are all fictional.

## What's in the folder

```text
test-kitchen/
├── settings.yaml, workspace.md, profile.md, AGENTS.md
├── recipes/                 # 6 recipe cards, one per dish
├── invoices/2026-fall.jsonl # 18 invoice lines from 6 companies
├── local/                   # 4 staff meetings, 2 inspection reports, 2 prep notes, 3 notes that are noise
├── sources/                 # recipes.yaml, invoices.yaml, local.yaml
├── facts/kinds.py           # Allergen, Substitution, PriceChange, Other
├── apps/kitchen/app.py      # the eighty_six tool
├── extensions.py            # loads apps/kitchen
├── agents/
│   ├── dishes/              # curator.md, article.md
│   ├── suppliers/           # curator.md, article.md
│   └── kitchen/allergens.md # one article that reads every dish
└── evals/kitchen.yaml       # five questions a new cook asks
```

## Run it

From the Cerebro repository:

```sh
uv tool install .
cerebro trust --new-id examples/test-kitchen
cd examples/test-kitchen
cerebro secrets set fireworks
cerebro secrets set typesafe
cerebro serve --once --yes
```

Then read `articles/`, or ask a connected agent "what can a guest with a sesame allergy eat?"

## Mark a dish 86'd

The kitchen app adds one MCP tool, `eighty_six`, beside `find`, `query`, and `read`. Call it from Claude, Cursor, or the command line:

```text
$ cerebro tools eighty_six --json '{"item": "Charred Carrots", "reason": "out of rainbow carrots until the Ridgeback delivery on Tuesday morning"}'
{
  "record": "eighty_six_09d075a0410e",
  "at": "2026-11-27T02:49:35Z",
  "item": "Charred Carrots"
}
```

It keeps the cook's words as a record, with no model call. `query { "from": "eighty_six" }` lists every one. On the next pass the record becomes facts, and the dish's article opens with a Tonight part that cites them.

## What it shows

| Feature | Where |
|---|---|
| Curators that admit on their own, and leave noise out with a reason | `agents/dishes/curator.md`, `agents/suppliers/curator.md` |
| One agent writing every article of a collection, and setting fields | `agents/dishes/article.md` sets `station` and `menu_price` |
| A single article that reads a whole collection | `agents/kitchen/allergens.md` |
| Kinds of fact, filtered with `classes:` | `facts/kinds.py`, used in `agents/kitchen/allergens.md` |
| An app with an `@mcp_tool` that keeps records of its own kind | `apps/kitchen/app.py`, loaded from `extensions.py` |
| Rows of a JSON Lines file as records | `sources/invoices.yaml` |
| A files source that lands each file on one collection | `sources/recipes.yaml` |
| An eval suite | `evals/kitchen.yaml` |

## What to look for

- **A fact a later record corrects.** Cascade Meats billed short rib at $11.40 a pound, then sent a credit memo for $10.90. The Cascade Meats article gives $10.90 as the price now.
- **A plan that moves.** The switch from halibut to rockfish was set for November 1, then moved to November 4, since the supplier had no rockfish until its boat landed on November 3.
- **A recipe card a meeting overruled.** The risotto card says chicken stock. A staff meeting found that out, and the risotto article says when it became vegetarian again.
- **Noise left out.** Family meal, a schedule swap, a linen service, a fridge repair, and a sales pitch reach no article of their own.

## An article it wrote

This is `articles/dishes/charred-carrots.md`, copied whole from a real run. The Tonight part comes from the `eighty_six` call above. The pepita dukkah comes from a staff meeting, not the recipe card.

````markdown
---
id: dishes/charred-carrots
title: Charred Carrots
identity:
  dish:
    - Charred Carrots
menu_price: 16
station: plancha
compiled_at: '2026-11-27T02:54:37Z'
---

## Tonight

Charred Carrots is 86'd: we are out of rainbow carrots until the Ridgeback delivery on Tuesday morning [^fact_01m8det6r3cv0mfkps]. The cook's 86 record is dated Friday, November 27, 2026 [^fact_01m8det6r3cv0mfkps]. Ridgeback delivers Tuesdays and Fridays before 10am [^fact_01m8dekxnx4q6525cw].

## The dish

Charred Carrots is Ridgeback Farm rainbow carrots, halved lengthwise, blanched 3 minutes, then charred on the plancha to order [^fact_01m8denm1ey9wm243f]. The fall menu launch priced it at $16 [^fact_01m8demhrnrjkv6j3c]. The hazelnut dukkah is the standard, and Rosa decided on October 14, 2026 to keep a second, nut-free dukkah for the dish [^fact_01m8demnrncbct9at2] [^fact_01m8dempyv7745nyzj]. Dee Okafor wanted the carrots made nut-free for good, with a pumpkin seed dukkah, because nut allergies come up two or three times a week [^fact_01m8dempzk6tpj216p] [^fact_01m8demq20x6910kab], but Rosa Delgado decided to keep the hazelnut dukkah as the standard and keep the pepita version as a second dukkah [^fact_01m8demnrncbct9at2].

## Allergens and substitutions

The dish holds three major allergens: tree nuts from the hazelnut in the hazelnut dukkah [^fact_01m8desmb7w870v0jq], sesame from the white sesame in the dukkah [^fact_01m8desmb7jn4kjtb2], and dairy from the Larkspur Creamery labneh [^fact_01m8desmb7ft7x9h5a].

For a dairy allergy, coconut yogurt goes in place of the labneh [^fact_01m8desmb7fta0f12a]. For a nut or sesame allergy, swap the hazelnut dukkah for the second dukkah, made of pepitas, coriander and cumin, with no nuts and no sesame; it lives in a blue-lid container on garde manger [^fact_01m8dempyv7745nyzj] [^fact_01m8demnrncbct9at2]. The standard hazelnut dukkah is kept in a red-lid container [^fact_01m8den54tk6pnx3fe].

## How we make it

The carrots are halved lengthwise, blanched 3 minutes, then charred on the plancha to order [^fact_01m8denm1ey9wm243f]. Prep blanched carrots ahead: on Friday, November 20, 2026, Luis blanched 2 cases of carrots in the morning [^fact_01m8den54tk6pnx3fe]. The same prep list made 2 quarts of hazelnut dukkah in a red-lid container and 1 quart of pepita dukkah in a blue-lid container [^fact_01m8den54tk6pnx3fe].

## What went wrong

Nut allergies on this dish come up two or three times a week [^fact_01m8dempzk6tpj216p]. Rosa decided to keep the pepita, coriander and cumin dukkah, with no nuts and no sesame, in a blue-lid container on garde manger for anyone with a nut or sesame allergy [^fact_01m8demnrncbct9at2] [^fact_01m8dempyv7745nyzj].

[^fact_01m8det6r3cv0mfkps]: eighty_six_09d075a0410e "Charred Carrots is 86'd: out of rainbow carrots until the Ridgeback delivery on Tuesday morning"

[^fact_01m8dekxnx4q6525cw]: RF-0911-1 "date: 2026-09-11 invoice: 0911 item: Rainbow carrots line: RF-0911-1 note: Delivery Tuesdays and Fridays before 10am qty: 50 supplier: Ridgeback Farm total: 80.00 unit: lb unit_price: 1.60"

[^fact_01m8denm1ey9wm243f]: recipes/charred-carrots.md "Carrots: Ridgeback Farm rainbow carrots, halved lengthwise, blanched 3 minutes, then charred on the plancha to order."

[^fact_01m8demhrnrjkv6j3c]: local/meetings/2026-09-16-fall-menu-launch.md "Prices for launch: short rib $34, halibut $36, risotto $28, carrots $16, little gems $15, budino $11."

[^fact_01m8demnrncbct9at2]: local/meetings/2026-10-14-after-inspection.md "Rosa decided: keep the hazelnut dukkah as the standard, and keep a second dukkah of pepitas, coriander and cumin, with no nuts and no sesame, in a blue-lid container on garde manger for anyone with a nut or sesame allergy."

[^fact_01m8dempyv7745nyzj]: local/meetings/2026-10-14-after-inspection.md "Rosa decided: keep the hazelnut dukkah as the standard, and keep a second dukkah of pepitas, coriander and cumin, with no nuts and no sesame, in a blue-lid container on garde manger for anyone with a nut or sesame allergy."

[^fact_01m8dempzk6tpj216p]: local/meetings/2026-10-14-after-inspection.md "Dukkah: Dee wants the carrots made nut-free for good, with a pumpkin seed dukkah, because nut allergies come up two or three times a week."

[^fact_01m8demq20x6910kab]: local/meetings/2026-10-14-after-inspection.md "Dukkah: Dee wants the carrots made nut-free for good, with a pumpkin seed dukkah, because nut allergies come up two or three times a week."

[^fact_01m8desmb7w870v0jq]: recipes/charred-carrots.md "Allergens: tree nuts (hazelnut) and sesame in the dukkah, and dairy in the labneh."

[^fact_01m8desmb7jn4kjtb2]: recipes/charred-carrots.md "Allergens: tree nuts (hazelnut) and sesame in the dukkah, and dairy in the labneh."

[^fact_01m8desmb7ft7x9h5a]: recipes/charred-carrots.md "Allergens: tree nuts (hazelnut) and sesame in the dukkah, and dairy in the labneh."

[^fact_01m8desmb7fta0f12a]: recipes/charred-carrots.md "Substitutions: for a dairy allergy, coconut yogurt in place of the labneh."

[^fact_01m8den54tk6pnx3fe]: local/prep/2026-11-20-friday-prep.md "Luis, morning: 2 cases of carrots blanched, little gems washed and spun, 2 quarts hazelnut dukkah (red lid), 1 quart pepita dukkah (blue lid), polenta portioned."
````

## The run

One first run on the default models, with the two `eighty_six` calls above made before it:

| Records | Facts | Articles | Cost |
|---|---|---|---|
| 37 (6 recipe cards, 11 notes, 18 invoice lines, 2 eighty_six) | 250 | 11 (6 dishes, 4 suppliers, 1 allergen sheet) | $1.26 |

The suppliers curator proposed its four suppliers one pass after the first run's list, so they waited for `cerebro review` before the second pass wrote them. `cerebro eval evals/kitchen.yaml --no-model` scores 0.93: the risotto article leaves out the vegan version's miso, which the allergen sheet carries.
