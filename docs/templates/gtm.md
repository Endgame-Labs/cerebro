# The gtm template

Start a sales team's wiki: a briefing for each account, each person and each open deal, a prep before each call and a write-up after, and a weekly pipeline report. It works from your notes alone, and gets richer as you connect your CRM, calendar and call recorder.

```sh
cerebro init --template gtm sales-wiki --name "Ada Park" --email ada@example.com --org "Example Co" \
  --domain example.com --daily-usd 20 --timezone UTC
```

```text
workspace ~/wikis/sales-wiki
wrote     settings.yaml, workspace.md, profile.md, AGENTS.md, 1 file under sources/, 31 files under agents/, 1 file under evals/, 1 file under facts/
...
next      put notes as .md files in local/, then cerebro serve --once --workspace ~/wikis/sales-wiki
next      to read a source that needs a login, write sources/<name>.yaml, then cerebro connect <name> --workspace ~/wikis/sales-wiki
```

Your domain matters here. `--domain` writes it into `workspace.md` as your own, which is how the agents tell your team from your customers. Leave it out, and `init` asks for it at a terminal:

```markdown
---
name: sales-wiki
identity:
  name: Example Co
  domains: [example.com]   # your own email domains: people at one are your side
---
```

If your team uses more than one domain, pass `--domain` once for each, or add them to `domains:` later.

## Start from your notes

**1. Give each note an `Account:` line.** The accounts curator looks for them to find your accounts. Give each attendee an email address, so an address at your own domain marks one of your people:

```text
local/2026-11-18-tidewater-intro.md

Account: Tidewater Freight
Date: 2026-11-18
Attendees: Nessa Torvik <nessa@tidewater.example>, Sam Okoye <sam@example.com>

Intro call. Nessa runs operations and wants to cut the hours her team spends on shipment exceptions.
Next step: a demo for her team on 2 December.
```

**2. Put playbooks and policies under `local/knowledge/`.** The knowledge curator proposes a topic article for each one that says how your team works.

**3. Run the first pass.** Nothing is written until you agree to the list of accounts it proposes:

```sh
cerebro serve --once
```

The list opens as a tree: each account with its people and calls under it, the ones worth your time first and ticked. Change what you like and approve, and the pass writes those articles. The rest wait for you in `cerebro review`. A curator that found nothing to read the first time, such as the risk radar's, is asked again once the sources are read, and may show you one more, shorter list in the same run.

**4. Check who is proposed, and run a second pass for the rest.** An attendee whose address is at an account's domain, such as `nessa@tidewater.example` on a Tidewater Freight note, is proposed in the first list. An attendee with no address, or one at a personal mailbox such as `gmail.com`, waits until a fact from your notes places them at an account. Those facts come only after you approve the first list, so the next `cerebro serve --once` proposes them, and they wait for you in `cerebro review`.

**5. Ask your agent** about an account your notes name, such as "Where does Tidewater Freight stand, and what's the next step?"

## What it writes

| Collection | Writes |
|---|---|
| `accounts` | A briefing per account: overview, deals, stakeholders and work under way |
| `people` | An article per person: who they are and recent activity |
| `opportunities` | Which deals the wiki keeps |
| `open-pipeline` | A brief per open deal: where it stands, what moves it, what's in the way |
| `risk-radar` | A brief per account closing soon: sponsor, competitor, approval path, timing and budget |
| `calls` | An article per call: a prep before it, and a write-up once it has happened |
| `knowledge` | An article per topic from your playbooks and policies |
| `digests` | One article that lists all the others |
| `reports` | A weekly pipeline report |

Each collection is a folder under `agents/`. Delete a folder you don't need, and that collection is gone.

People, calls, deals and briefs list their account in `linked`, and a call lists its attendees there too. So `query { "from": "people", "linked": "accounts/tidewater-freight" }` gives an account's people, and `read` of an account counts its people, calls and deals.

## What runs on its own

After the first run, the calls and people curators add new articles on their own, for accounts already in your wiki:

```yaml
authority: { admit: true, archive: false, merge: false, split: false }
```

Every other curator proposes, and you decide in `cerebro review`, with its reason. Archives, merges and splits wait for you from every curator:

```yaml
authority: { admit: false, archive: false, merge: false, split: false }
```

To let a curator do one of these on its own, set it to `true` in that curator's file. The pipeline report runs once a week, and every other agent runs when its evidence changes.

## Connect Salesforce

The template's account and deal agents work best with your CRM. Cerebro reads Salesforce through the Salesforce CLI, `sf`, so install it first. If you have it already, run `sf update`. Then log in and connect:

```sh
sf org login web --alias work
cerebro connect salesforce
```

Connect asks the CLI for a token, checks it with your org, and writes `sources/salesforce.yaml` with `org: work`. Cerebro keeps no token. It asks the CLI for one each time it reads.

Connect uses the CLI's default org, or the one org it is logged in to. If you are logged in to several and none is the default, connect lists them and asks which:

```text
The Salesforce CLI is logged in to several orgs:
  1  work  (you@example.com)
  2  sandbox  (you@example.com.sandbox)
Connect which? [1-2]:
```

Type its number or alias. To skip the question, or off a terminal, name the org: `cerebro connect salesforce --org work`.

To read more than the defaults, write `sources/salesforce.yaml` yourself. This file reads what the template's agents need. Its `org:` is the alias you logged in with.

```yaml
# Your CRM, read only: each account, contact, user and deal the curators admit, and the
# tasks, events and emails logged on it. Log in with the Salesforce CLI: sf org login web --alias work
kind: salesforce
org: work
window: { behind: 90d }
opportunities:
  default_currency: USD   # a deal whose record names no currency; change it if the org's one currency is not USD
objects:                  # what lands where; a file that names objects reads only those, so all three are here
  Account:
    into: accounts
    identity: { salesforce_id: Id, name: Name, aliases: Website }
    fields: { owner: OwnerId, industry: Industry, website: Website }
  Contact:
    into: people
    identity: { salesforce_id: Id, name: Name, email: Email }
    fields: { account: AccountId, title: Title }
  User:
    into: people
    identity: { salesforce_id: Id, name: Name, email: Email }
    fields: { title: Title }
```

Each mapped field becomes a fact, and the template's writers keep the front matter its lookups read: the opportunities curator gives a deal's stage, amount, close date and owner as it admits it, the opportunities agent keeps them current from the facts, and the account overview sets an account's close date from its next open deal. Then run `cerebro serve --once`. Until the CLI has a login, the source fails with a line that says what to run.

Calendar, Gmail, Gong, Granola and Slack sources add meetings, email and calls. [Sources](../sources.md) shows how to connect each.

A meeting on your calendar, its recording and your Granola notes of it share one `calls` article. A recording that names its calendar event lands there on its own. The calls curator puts one that names no event on the meeting that starts at about the same time and shares an outside attendee.

## Check it with evals

The template writes `evals/example.yaml`, two sample questions about a made-up account. Swap in an account and a person your notes name, then run:

```sh
cerebro eval evals/example.yaml
```

[Command line](../cli.md#check-answers-with-eval) explains what a suite holds.
