#!/usr/bin/env python3
"""scan_sessions.py — list pw code sessions: model, prompts, tool counts, Task calls.

Usage: scan_sessions.py [session-id prefix, e.g. 20261016]

For each main-session transcript in ~/.local/state/pw/code-sessions/ whose id
starts with the prefix (all if omitted): the model, the first user prompts, how
many times each tool was called, every Task call with its arguments (so you can
see subagent_type and background), the number of subagent transcripts, and the
conversation tokens. Reads only *.jsonl transcripts, never *.log files.
"""
import collections
import glob
import json
import os
import sys

D = os.path.expanduser("~/.local/state/pw/code-sessions")


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    for p in sorted(glob.glob(os.path.join(D, prefix + "*.jsonl"))):
        sid = os.path.basename(p)[:-6]
        users, tools, tasks, model, tokens = [], collections.Counter(), [], "", 0
        for line in open(p):
            e = json.loads(line)
            if e.get("type") == "session":
                model = e["session"].get("model", "")
            if e.get("type") == "usage" and e["usage"].get("purpose") == "conversation":
                tokens += (e["usage"].get("tokens") or {}).get("total_tokens", 0)
            if e.get("type") != "message":
                continue
            m = e["message"]
            if m.get("role") == "user":
                users.append(str(m.get("content"))[:150].replace("\n", " "))
            for c in m.get("tool_calls") or []:
                f = c.get("function") or c
                name = f.get("name") or "?"
                tools[name[:30]] += 1
                if name.lower().startswith("task"):
                    tasks.append((name, (f.get("arguments") or "")[:300].replace("\n", " ")))
        subs = len(glob.glob(os.path.join(D, sid, "subagents", "*.jsonl")))
        print("====", sid, model.split(":")[-1], "prompts=%d subagents=%d tokens=%d"
              % (len(users), subs, tokens))
        for u in users[:4]:
            print("   U:", u)
        print("   tools:", dict(tools))
        for t in tasks:
            print("   TASK:", t)


if __name__ == "__main__":
    main()
