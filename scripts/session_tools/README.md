# session_tools: read `pw code` session transcripts

Small stdlib-only Python 3.9 scripts for a HUMAN (or a Claude Code session
helping one) to check what `pw code` agents actually did. They were used to
find out whether the main session delegated, to which agent, what each
subagent ran, and what it cost, and to compare that with what the agents
reported. They are not for `pw code` agents: per AGENTS.md, no agent reads the
session store.

Transcripts live in `~/.local/state/pw/code-sessions/`:
`<session_id>.jsonl` for a session, and `<session_id>/subagents/agent-task-N.jsonl`
for each delegated subagent. Only these `.jsonl` files are read; never point the
tools at `*.log` files (secrets policy). A session id looks like
`20261016-234917-3d6aec1e`; `pw code -o json` prints it as `session_id`.

| Script | Use it to | Example |
|---|---|---|
| `scan_sessions.py [prefix]` | list sessions: model, prompts, tool counts, every Task call (`subagent_type`, `background`), tokens | `python3 -I scripts/session_tools/scan_sessions.py 20261016` |
| `show_session.py <sid or path> [width]` | read one session or subagent transcript in time order: prompts, reasoning, tool calls and results, truncated to `width` chars | `python3 -I scripts/session_tools/show_session.py 20261016-234917-3d6aec1e 600` |
| `subagents.py <sid>` | summarize each subagent of a session: calls by tool, `mkdir` claims, `_site_query.py` uses, poll runs and states read, files written, unexpected commands, failed or denied calls, tokens | `python3 -I scripts/session_tools/subagents.py 20261016-234917-3d6aec1e` |
| `session_summary.py <sid or -o json file> [-v]` | one session's cost and tool-call health: iterations, tokens, calls, malformed tool names, denied and failed calls; `-v` lists every call | `python3 -I scripts/session_tools/session_summary.py 20261016-234544-50940be6 -v` |

Run them with `python3 -I -B` (isolated mode, no `__pycache__`). Agent claims
should then be checked against ground truth: scheduler accounting (`sacct`),
`pw workflows runs logs <slug>`, the staged `runs/<id>/` files and the
`results/<id>/verdict.json` files.
