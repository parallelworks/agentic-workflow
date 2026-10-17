#!/usr/bin/env python3
"""subagents.py — summarize every subagent a pw code session delegated to.

Usage: subagents.py <session-id>

For each <sid>/subagents/agent-task-N.jsonl: start and end time, the task
prompt, calls per tool, `mkdir` claims, `_site_query.py` uses, poll workflow
runs and the job states read from tool results, files written, Bash commands
outside the documented forms, failed / denied / "File exists" calls, and
conversation tokens. Reads only *.jsonl transcripts, never *.log files.
"""
import collections
import glob
import json
import os
import re
import sys

D = os.path.expanduser("~/.local/state/pw/code-sessions")
DOCUMENTED = re.compile(r"(ls|mkdir runs/|cp workflows/|python3 scripts/_site_query|"
                        r"git (remote|rev-parse)|pw workflows run|pw workflows runs|sleep|base64)")


def task_number(path):
    m = re.search(r"task-(\d+)", path)
    return int(m.group(1)) if m else 0


def summarize(path):
    calls, results, first, last, task, tokens = [], {}, None, None, "", 0
    for line in open(path):
        e = json.loads(line)
        if e.get("type") == "usage" and e["usage"].get("purpose") == "conversation":
            tokens += (e["usage"].get("tokens") or {}).get("total_tokens", 0)
        if e.get("type") != "message":
            continue
        m, ts = e["message"], e["timestamp"][11:19]
        first, last = first or ts, ts
        if m.get("role") == "user" and not task:
            task = str(m["content"]).replace("[Task from the main session]", "").strip()[:110]
        for c in m.get("tool_calls") or []:
            f = c.get("function") or c
            try:
                a = json.loads(f.get("arguments") or "{}")
            except ValueError:
                a = {}
            calls.append((c.get("id"), f.get("name"), a if isinstance(a, dict) else {}))
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = str(m.get("content"))

    names = collections.Counter(n for _, n, _ in calls)
    cmds = [a.get("command", "") for _, n, a in calls if n == "Bash"]
    states = []
    for i, _, _ in calls:
        states += re.findall(r'"state":"(\w+)"', results.get(i, ""))
    bad = [(n, (a.get("command") or a.get("path") or "")[:80], results.get(i, "")[:90].replace("\n", " "))
           for i, n, a in calls
           if results.get(i, "").startswith("Error") or "doesn't want to proceed" in results.get(i, "")
           or "No such file" in results.get(i, "") or "File exists" in results.get(i, "")]
    print("##", os.path.basename(path)[:-6], first, "-", last, "| tokens", tokens, "|", task)
    print("   calls", len(calls), dict(names))
    print("   mkdir", [c for c in cmds if c.startswith("mkdir")],
          "| site_query", sum("_site_query.py" in c for c in cmds),
          "| poll runs", sum(1 for c in cmds if "poll.yaml" in c and "workflows run " in c),
          "| states read", states)
    print("   writes", [a.get("path") for _, n, a in calls if n in ("WriteFile", "EditFile")])
    other = [c[:90].replace("\n", " ") for c in cmds if not DOCUMENTED.match(c)]
    if other:
        print("   other cmds", other)
    for b in bad:
        print("   BAD", b)


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    paths = sorted(glob.glob(os.path.join(D, sys.argv[1], "subagents", "*.jsonl")), key=task_number)
    if not paths:
        print("no subagent transcripts for session %s" % sys.argv[1])
    for p in paths:
        summarize(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
