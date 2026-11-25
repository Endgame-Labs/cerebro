# How-to skills for a coding agent

These are skills for a coding agent, such as Claude Code or Codex, that is working on a workspace. Each one is a `SKILL.md` that walks one task step by step, with the commands that check it.

- [`add-a-source`](add-a-source/SKILL.md): add a folder of files as a source, and check its records are read.
- [`why-is-this-article-stale`](why-is-this-article-stale/SKILL.md): find out why an article was not rewritten, and get it current.
- [`write-a-context-agent`](write-a-context-agent/SKILL.md): add or change a context agent, then check it loads and reads well.

To use one, copy its folder into your coding agent's own skills folder, such as `~/.claude/skills/` for Claude Code.
