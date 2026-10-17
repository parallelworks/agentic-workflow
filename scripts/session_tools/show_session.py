#!/usr/bin/env python3
"""show_session.py — print one pw code transcript in time order.

Usage: show_session.py <session-id | path/to/transcript.jsonl> [width]

Prints user prompts, model reasoning (THINK), assistant messages, tool calls
(CALL) and tool results (TOOL), each truncated to `width` characters (default
400). Pass a session id for a main session, or a path such as
~/.local/state/pw/code-sessions/<sid>/subagents/agent-task-5.jsonl for a
subagent. Reads only *.jsonl transcripts, never *.log files.
"""
import json
import os
import sys

D = os.path.expanduser("~/.local/state/pw/code-sessions")


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    arg = sys.argv[1]
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    path = arg if arg.endswith(".jsonl") else os.path.join(D, arg + ".jsonl")
    for line in open(os.path.expanduser(path)):
        e = json.loads(line)
        if e.get("type") != "message":
            continue
        m, ts = e["message"], e["timestamp"][11:19]
        if m.get("reasoning_content"):
            print(ts, "THINK:", m["reasoning_content"][:width].replace("\n", " "))
        if m.get("content"):
            print(ts, (m.get("role") or "?").upper() + ":", str(m["content"])[:width].replace("\n", " "))
        for c in m.get("tool_calls") or []:
            f = c.get("function") or c
            print(ts, "CALL", f.get("name"), (f.get("arguments") or "")[:width].replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
