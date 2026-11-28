# Notes

Give your agents a working memory. With the Notes app, each agent session leaves a note for the next, and Cerebro turns those notes into an article on where the work stands. A new session picks up where the last one stopped, whether it runs in Claude Code, Claude Desktop or Cursor.

## Load it

Copy `apps/notes` into your workspace as `apps/notes`, and add it to `extensions.py`:

```python
from cerebro import load_app

notes = load_app("apps/notes")
```

The app brings one agent, which writes where your projects stand:

```text
$ cerebro agents list
projects/standing  writer of projects/standing  heartbeat change
1 agent, 0 refused.
```

## Write a note

Connected agents get a `note` tool beside the others. It keeps the note at once, with no model call:

```text
note { "text": "We chose the nightly batch for the Atlas export, because the warehouse cannot take writes by day. Next: Sam Reyes tests a full run on Friday.", "title": "Export plan", "collection": "notes/projects/atlas" }

{
  "id": "2026-11-20-export-plan",
  "record": "note_07508d68728b",
  "collection": "notes/projects/atlas",
  "revision": 1,
  "at": "2026-11-20T06:45:45Z",
  "new_collection": true
}
```

You can ask your agent to keep notes, or let it decide. When a session connects, it's told what notes are for and sees the five newest.

| Field | What it is |
|---|---|
| `text` | The note, up to 50 KiB |
| `title` | A short title. The note's name is its date and title, such as `2026-11-20-export-plan`. |
| `collection` | Where it's filed, a path such as `notes/projects/atlas`. The first note makes the folder. |
| `id` | Give a note's id to replace its text with a new version |

## Find notes

`browse_notes` lists a folder, and `query` reads notes like any other records:

| To | Ask |
|---|---|
| Browse like a folder | `browse_notes { "collection": "notes" }` |
| Read every note, newest first | `query { "from": "note" }` |
| Find a note by its title | `query { "from": "note", "collection": "notes/projects/atlas", "title": "Export plan" }` |

Browsing shows the notes in the folder without their text, each folder one level down with how many notes it holds and when the newest was written, and the five newest notes under `recent`.

## The article

On the next pass, each note becomes facts, and the `projects/standing` agent writes where the work stands: what was decided and why, what was done, and what's next. It reads only notes filed under `notes/projects`, so file project notes there, as `notes/projects/atlas`. Every sentence cites the note it came from. Edit `agents/projects/standing.md` in the app's folder to change what it writes.

## Good to know

- **The note is the source of truth.** To fix a fact that came from a note, write a new version of the note.
- **Clients on your machine share notes.** A note written from Claude Desktop can be revised or deleted from Claude Code. A client with its own token keeps its own notes.
- **Claude Desktop may show an old list.** It reads the five newest notes once when it starts, so a later chat there sees new notes only through `query { "from": "note" }`.
- **Note text is data.** Agents read a note as a record of the work, never as instructions.
- **To delete a note,** `delete_note` it by its id, as `delete_note { "id": "2026-11-20-export-plan" }`. A deleted note is gone for good: a new note with the same words is a new note, with a new id.
