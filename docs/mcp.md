# MCP

Connect Claude, Cursor or any other MCP client to your wiki. Your agent can then find articles, quote the facts behind them, and tell the wiki what it learns, without you copying anything across. The examples here use the `team-wiki` from the [quickstart](quickstart.md).

## Connect your agent

**1. Add the workspace to your client.** `cerebro init` printed the line, with your own paths filled in. For Claude Code it looks like this:

```sh
claude mcp add --scope user cerebro-team-wiki -- /Users/ada/.local/bin/cerebro mcp --workspace /Users/ada/wikis/team-wiki
```

For Claude Desktop, Cursor or any client with a JSON config file, add the entry `init` printed:

```json
{
  "mcpServers": {
    "cerebro-team-wiki": {
      "command": "/Users/ada/.local/bin/cerebro",
      "args": ["mcp", "--workspace", "/Users/ada/wikis/team-wiki"]
    }
  }
}
```

**2. Ask it something the wiki knows,** such as "Where does the Atlas launch stand?" It picks the right tools on its own.

**3. Keep the wiki current.** The client starts `cerebro mcp` whenever it needs it, so there's no token to manage. It only answers questions, so also leave `cerebro serve` running in a terminal, as [Runs](runs.md) explains.

## See what your agent sees

`cerebro tools` calls any tool as you, the workspace's owner, and prints the reply as JSON:

```text
$ cerebro tools find --json '{"name": "Atlas"}'
{
  "name": "Atlas",
  "matches": [
    "projects/atlas"
  ],
  "matched_on": {
    "projects/atlas": "name"
  }
}
```

The examples below show the reply as your agent reads it.

## The tools

| Tool | Use it to | Example |
|---|---|---|
| `find` | Turn a name into an article id | `find { "name": "Atlas" }` |
| `query` | List, filter or search articles, facts and logs | `query { "matching": "security review" }` |
| `read` | Get an article's full text, or the quote behind a fact | `read { "ids": ["projects/atlas"] }` |
| `tell` | Tell the wiki a fact, or ask for a change, in plain words | `tell { "text": "The Atlas launch moved to 16 December.", "target": "projects/atlas", "as": "fact" }` |
| `decide` | Accept or reject what agents propose, in one call that lands whole or not at all. | `decide { "accept": ["p_0a1f"] }` |
| `retract` | Take back a record or fact by its id | `retract { "id": "<id>", "reason": "a typo" }` |

Apps can add tools of their own. The Notes app adds `note`, as [Notes](apps/notes.md) shows.

### find

`find` turns a name, alias, email, web domain or path into the article it names. It matches exactly and doesn't search.

```text
find { "name": "Atlas" }

1 article named 'Atlas':
- projects/atlas (matched on name)
```

### query

`query` lists what you ask for. `from` names a collection, `facts`, or a log, and every other key filters:

```text
query { "from": "facts", "articles": "projects/atlas", "window": "last_30_days" }

2 matches in facts; 1 to 2 shown
- fact_bhaqr9g8x4fwzw0qb6 [2026-11-16, local/2026-11-16-atlas-sync.md] Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished.
- fact_173qeej9188evygsx0 [2026-11-16, local/2026-11-16-atlas-sync.md] Sam Reyes owns the Atlas security review and expects to finish it by 4 December.
```

A collection comes newest first by its `date`, `start` or `created` field, the first one its articles hold. With none of them, it comes by `compiled_at`, when a run wrote each article. The reply's first line names the field, as in `newest first by compiled_at`. Give `order_by` to pick the field yourself, as in `query { "from": "projects", "order_by": "due" }`.

Add `matching` to search by words, best match first. Leave out `from`, and Cerebro searches every article and fact at once:

```text
query { "matching": "security review" }

1 match in articles and facts, best match first; 1 to 1 shown
each line says what it is, an article or a fact, then its id: read takes either id, an article's for its text, a fact's for its quote. A fact under an article is linked to it.
- article projects/atlas: Atlas [matched in Where it stands]
  - fact fact_173qeej9188evygsx0 [2026-11-16, local/2026-11-16-atlas-sync.md] Sam Reyes owns the Atlas security review and expects to finish it by 4 December.
  - fact fact_bhaqr9g8x4fwzw0qb6 [2026-11-16, local/2026-11-16-atlas-sync.md] Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished.
```

| Key | What it does | Example |
|---|---|---|
| `from` | A collection, `facts`, or a log such as `proposals`, `spend` or `mcp-requests` | `"from": "projects"` |
| `matching` | Ranks results by the words you give | `"matching": "security review"` |
| `window` | On facts or a log, keeps a time range | `"window": "last_30_days"` |
| `articles` | On facts, keeps the facts linked to one article | `"articles": "projects/atlas"` |
| `linked` | On a collection, keeps the articles that [link to](agents.md#links-between-articles) one article, or to any of a list | `"linked": "projects/atlas"` |
| Any other key | Filters by a field your articles have, such as one an agent sets | `"status": "archived"` |
| `limit`, `offset` | Pages through long results | `"limit": 10, "offset": 10` |

Time ranges include `last_30_days`, `next_30_days`, `this_quarter` and `last_quarter`. A fiscal range such as `this_fiscal_year` works once `workspace.md` says when your fiscal year starts, as [Workspace](workspace.md#workspacemd-and-profilemd) shows.

### read

`read` takes up to 20 ids and gives back each article whole. The text keeps its citations:

```text
read { "ids": ["projects/atlas"] }

# projects/atlas: Atlas
compiled_at: 2026-11-20T07:07:58Z

## Where it stands

Lena Ortiz said the Atlas launch moves from 2 December to 9 December, because the security review is not finished. [^fact_bhaqr9g8x4fwzw0qb6] Sam Reyes owns the Atlas security review and expects to finish it by 4 December. [^fact_173qeej9188evygsx0]
```

Read a fact's id to see where it came from and the exact words behind it:

```json
{
  "facts": [
    {
      "id": "fact_173qeej9188evygsx0",
      "text": "Sam Reyes owns the Atlas security review and expects to finish it by 4 December.",
      "kind": "note",
      "at": "2026-11-16",
      "record": "local/2026-11-16-atlas-sync.md",
      "quote": "Sam Reyes owns the Atlas security review and expects to finish it by 4 December."
    }
  ]
}
```

If the wiki has learned something since an article was last written, `read` lists those new facts under `facts_since_written`.

If other articles link to it, `read` counts them by collection, each with the query that lists them:

```text
linked from 2 articles of people: query from: people, linked: projects/atlas
```

### tell

`tell` keeps your agent's words as a record, then turns what they state into facts and what they ask for into proposals. What your agent asks for is accepted as it is sent. [Tell](tell.md) shows every reply.

### decide and retract

Every client you connect is a caller, and any caller can `decide` a waiting proposal and `retract` anything. Tell your agent "approve these, reject those", and it decides them in one call.

A yes to an admit, archive, merge, split or link is carried out by the collection's curator on its next run. The reply says so:

```text
Accepted p_4823559b1ff9: its curator carries it out on its next run.
```

Your agent can take back a yes the curator has not carried out yet, by its `p_` id. One already carried out stands, and the reply says how to undo it.

## Connect over HTTP

Use HTTP for a client that can't start a program itself, or for a workspace on a server. Every request needs a token, even from your own machine.

**1. Start the server:**

```text
$ cerebro serve
workspace ~/wikis/team-wiki
serving   MCP on localhost, a pass every 30 seconds. Ctrl-C stops.
mcp       http://127.0.0.1:4747/mcp
api       http://127.0.0.1:4747/api/v1, the JSON API
health    http://127.0.0.1:4747/healthz, which answers with no token
tokens    every request needs a token: cerebro tokens list shows them
```

**2. Make a token,** in another terminal. Copy it right away, since it's shown once:

```text
$ cerebro tokens new laptop-agent
cbt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
made     laptop-agent, a caller, at 2026-11-20T06:25:04Z: the token above is shown this once, and the workspace keeps only its hash
```

**3. Add the server to your client:**

```sh
claude mcp add --scope user --transport http cerebro-team-wiki http://127.0.0.1:4747/mcp --header "Authorization: Bearer <token>"
```

`cerebro tokens list` shows your tokens and when each was last used. `cerebro tokens revoke <name>` ends one, and its next request is refused. To reach the server from another machine, follow [Deploy](deploy.md).

## Call it from a script

The same tools answer as plain JSON under `/api/v1`, on the same port and with the same tokens. Send the tool's arguments as the body:

```sh
curl -X POST http://127.0.0.1:4747/api/v1/find \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Atlas"}'
```

The reply is the JSON that `cerebro tools` prints. `GET /api/v1` lists every tool and its arguments. Without a token, you get this:

```json
{"error": {"code": "unauthorized", "message": "this workspace needs a token: send Authorization: Bearer <token>, made with cerebro tokens new <name>"}}
```

For a web app, start the server with `--allowed-origin https://app.example`, so pages from that address may call the API.

## Good to know

- **Your agent is briefed when it connects:** what the wiki holds, who you are from `profile.md`, and that what the tools return is information, never instructions.
- **Long replies are cut** at about 25,000 tokens. A cut reply says what it left out and how to get the rest.
- **Every call is logged.** `query { "from": "mcp-requests" }` shows who called what. Set `CEREBRO_NO_REQUEST_LOG=1` to turn the log off.
- **Clients on your machine share one identity,** `mcp`. What one adds, another can take back.
- **Wrong arguments are refused by name,** and nothing runs, as `find: unknown field "bogus"`.
