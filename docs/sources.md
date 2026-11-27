# Sources

Point Cerebro at the places your knowledge lives: a folder of notes, GitHub, Slack, Gmail, a calendar, a database or any MCP server. Cerebro only reads from a source, and never writes to it.

## Add a folder of files

Your workspace already reads `local/`. Here's how to add a second folder, `meetings/`, to the `team-wiki` from the [quickstart](quickstart.md).

**1. Make the source:**

```text
$ cerebro sources new meetings
wrote    sources/meetings.yaml
next     put files in meetings, then cerebro sources check meetings
```

It writes `sources/meetings.yaml`:

```yaml
# Local files, one record per file, its body the text. A relative glob is relative to the workspace.
kind: files
select:
  - glob: "meetings/**/*.md"
    extract: facts
```

**2. Add a file,** such as `meetings/2026-11-18-atlas-review.md`:

```text
Project: Atlas
Date: 2026-11-18

The Atlas launch review is on Friday.
```

The lines at the top give the file a date and say what it's about. Cerebro reads them and leaves them out of the text.

**3. Check what it will read.** A check reads the source and keeps nothing:

```text
$ cerebro sources check meetings
listed    meetings: 1 record from all of its history
record    meetings/2026-11-18-atlas-review.md  facts  for the next pass
waiting   1 record waits for the next pass: cerebro serve --once or cerebro distill --source meetings takes it in
```

**4. Run a pass** with `cerebro serve --once`. The new facts reach the Atlas article.

## Connect a service

Here's how to read GitHub. Every service with a login works the same way.

**1. Write the source file,** `sources/code.yaml`:

```yaml
kind: github
repos: [example-org/atlas]
```

**2. Connect it.** `cerebro connect` tells you where to make a token, checks it with one read, and keeps it outside the folder:

```sh
cerebro connect code
```

For GitHub, Linear and Slack, it then lists what you can read, such as your repositories, so you can tick the ones you want. Off a terminal it asks nothing, so pass the token in with `cerebro connect code --stdin < github-token.txt`.

**3. Check it** with `cerebro sources check code`. Until the login is kept, the check, and every pass, says what to run:

```text
failed    sources/code: SecretNotFound: sources/code.yaml needs a login, and none is kept as github: run cerebro connect code
```

A source that fails never stops the pass. The other sources and agents go on.

## The kinds

| Kind | What it reads | Login |
|---|---|---|
| `files` | Text and markdown files, or each row of a CSV, JSON Lines or JSON file | None |
| `calendar` | Meetings from an iCal feed that include someone from outside | The feed's secret address |
| `github` | Issues, pull requests and releases of the `repos:` you list | A token |
| `gmail` | Your mail, read only, over IMAP | Your address and an app password |
| `gong` | Recorded calls, with transcripts | An access key and secret |
| `granola` | Meeting notes and transcripts | A token, or a browser sign-in |
| `linear` | Issues, projects and updates of the `teams:` you list | A token, or a browser sign-in |
| `salesforce` | Accounts, contacts, deals and activity, as the [gtm template](templates/gtm.md#connect-salesforce) shows | The Salesforce CLI's login, from `sf org login web` |
| `slack` | Messages in the `channels:` you list and belong to, never direct messages | A user token |
| `database` | A SQLite file or a Postgres database, for agents to query | None for SQLite, a connection string for Postgres |
| `mcp` | Any MCP server, through tools you name | Optional: a token or a browser sign-in |

A source is one file, `sources/<name>.yaml`. The file's name is the source's name. To stop reading a source, delete its file.

## Common settings

| Key | What it does | Taken by |
|---|---|---|
| `window:` | How far back to read, as `window: { behind: 30d }`. Most kinds read 90 days by default. | Every kind but `database` |
| `freshness:` | How often `cerebro serve` checks the source, as `freshness: 1h`. 15 minutes by default. | Every kind but `database` |
| `extract:` | `facts` turns each record into facts. `records` reads none of its text, only the fields a source maps. Either way each field becomes one fact, such as "The stage of Acme renewal is Negotiation.": the same value again costs nothing, and a changed value replaces the old fact. No pass writes a field onto an article: its curator and its agents do, from those facts. | Every kind but `database` |
| `into:` | The collection each record's article belongs to, as `into: things` | Each `select:` entry of `files` and `mcp`, and the top of `calendar`, `gong`, `granola` and `mcp` |
| `owner:` | Whose source it is: `me`, the default, or a colleague's email | `calendar`, `github`, `gmail`, `granola`, `linear` and `slack` |

GitHub, Gmail, Linear and Slack records take no `into:`. Their facts reach every article they mention.

A file under `into:` belongs to the article its curator admits from it, or to the one its `identity:` names. Its header lines, such as `Account: Acme`, link its facts to Acme and put no other file on its article.

A key a kind doesn't take stops the file from loading, with a line that names it and says where it goes:

```text
refused   sources/code.yaml line 3: into: a github record names no collection, it distills into facts that cite it: delete into
```

## Read rows from a spreadsheet

A `files` source can make one record from each row of a CSV, JSON Lines or JSON file. Each key is a template, and `{row.<column>}` reads a column:

```yaml
kind: files
window: { behind: 90d }
select:
  - glob: "exports/usage/*.csv"
    rows: csv             # one record per row, and jsonl or json work the same way
    id: "{row.thing_id}"
    at: "{row.day}"
    into: things
    identity: { thing_id: "{row.thing_id}" }
    fields: { seats_active: "{row.seats_active}" }
  - glob: "notes/**/*.md"  # one record per file
```

A row gives facts from its `fields:` alone, since rows take `extract: records` by default. Whole files take `extract: facts`. Set `extract: facts` on the entry to read each row's text too, as one `column: value` line per column, with a column that has no value left out.

A field's name becomes the words of its fact, so `seats_active` 42 reads as "The seats active of A1 is 42." The fact names the row by its `name:`, `title:` or `subject:` field, else by the title of the one article it binds, else by its id, as A1 here. So name each field in words, and put a unit in the value, as `disk: "{row.disk_gb} GB"`.

Cerebro keeps [some article fields](agents.md#article-fields) itself, so an agent can't set `id`, `title`, `status`, `compiled_at`, `identity`, `keys` or `in_source` from your facts. For a value you want an agent to set on the article, pick another name, as `state: "{row.status}"` instead of `status: "{row.status}"`.

Give each row an `id:` of its own. Without one, a row is known by its line, and a new row above it makes every row below look new.

## Read from any MCP server

An `mcp` source reaches a server by `url:` or `command:`, and names two of its tools. `list` gives the ids of what changed, and `get` gives one record:

```yaml
kind: mcp
url: https://warehouse.example/mcp
extract: records
into: things
read_only: [execute_sql]   # the server does not mark its SQL tool, and this one only reads
binding:
  list:
    tool: execute_sql
    args: { query: "select id from rows" }
    id: "$.rows[*].id"
  get:
    tool: execute_sql
    args: { query: "select * from rows where id = '{id}'" }
    revision: "$.rows[0].revision"
    at: "$.rows[0].at"
    text: "$.rows[0].notes"
    url: "https://warehouse.example/rows/{id}"
    identity: { email: "$.rows[0].email" }
    fields: { title: "$.rows[0].title" }
```

Cerebro refuses a tool the server marks as one that writes. It also refuses one the server doesn't mark as read only, unless you list it under `read_only:`.

## Query a database

A `database` source lets an agent run read-only SQL with a `sql` evidence line, as [Agents](agents.md#evidence) shows. It adds no records of its own.

For SQLite, name the file:

```yaml
kind: database
url: sqlite:data/team.sqlite
```

For Postgres, keep the connection string in the secrets store, never in the file:

```yaml
kind: database
url: postgres
auth: { keychain: warehouse-dsn }
```

```sh
cerebro secrets set warehouse-dsn
```

The value is a connection string, such as `postgresql://cerebro_reader:<password>@db.example:5432/team?sslmode=require`. Postgres needs one extra package, so install with `uv tool install '.[postgres]'`.

Cerebro runs only a single `SELECT`, read only, and stops at 500 rows or 10 seconds. Connect as a login that can only read the tables you want, and never as a superuser, which Cerebro refuses:

```sql
create role cerebro_reader login nosuperuser;
alter role cerebro_reader set default_transaction_read_only = on;
grant connect on database team to cerebro_reader;
grant usage on schema reporting to cerebro_reader;
grant select on reporting.things to cerebro_reader;
```

## Good to know

- **Under `cerebro serve`, a source is checked every 15 minutes,** or as its `freshness:` says. `cerebro serve --once` and `cerebro distill` check every source now.
- **Cerebro keeps no copy of a record's text** from a connected service. It asks the source again when it needs it.
- **A call keeps who said each line.** Each quote from a `gong` call names its speaker, as `Jane Doe`, so a query's `speaker:` finds what one person said.
- **Other kinds name no speaker.** A line such as `Account: Acme` in a file or a note is never read as someone speaking.
