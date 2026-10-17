#!/usr/bin/env python3
"""session_summary.py — one pw code session's cost and tool-call health.

Usage: session_summary.py <session-id | result.json from `pw code -o json`> [-v]

Prints iterations and tokens (from the -o json result when given, else summed
from the transcript), the models used, the number of tool calls, malformed tool
names (gpt-oss garbling such as `Bash<|channel|>commentary`), denied calls and
failed calls. With -v, lists every call as ok / DENIED / ERR with its argument
and the start of its result, then the final message. Reads only *.jsonl
transcripts, never *.log files.
"""
import json
import os
import sys

D = os.path.expanduser("~/.local/state/pw/code-sessions")


def main():
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv
    if not args:
        print(__doc__.strip())
        return 2
    res = {}
    if args[0].endswith(".json"):
        res = json.load(open(args[0]))
        sid = res["session_id"]
    else:
        sid = args[0]

    calls, out, models, iters, tokens, final = [], {}, set(), 0, 0, ""
    for line in open(os.path.join(D, sid + ".jsonl")):
        e = json.loads(line)
        if e.get("type") == "usage":
            u = e.get("usage") or {}
            if u.get("model"):
                models.add(u["model"])
            if u.get("purpose") == "conversation":
                iters += 1
                tokens += (u.get("tokens") or {}).get("total_tokens", 0)
        if e.get("type") != "message":
            continue
        m = e["message"]
        if m.get("role") == "assistant" and m.get("content"):
            final = str(m["content"])
        for c in m.get("tool_calls") or []:
            f = c.get("function") or c
            try:
                a = json.loads(f.get("arguments") or "{}")
            except ValueError:
                a = {"raw": f.get("arguments")}
            if not isinstance(a, dict):
                a = {"raw": a}
            shown = a.get("command") or a.get("path") or a.get("raw") or a
            calls.append((c.get("id"), f.get("name") or "?", str(shown)[:110]))
        if m.get("role") == "tool":
            out[m.get("tool_call_id")] = str(m.get("content"))

    def state(i):
        r = out.get(i, "")
        return "DENIED" if "doesn't want to proceed" in r else ("ERR" if r.startswith("Error") else "ok")

    print("session", sid,
          "iterations", res.get("iterations", iters),
          "tokens", (res.get("usage") or {}).get("total_tokens", tokens))
    print("models", sorted(models), "| calls", len(calls),
          "| malformed names", sum(1 for _, n, _ in calls if "<|" in n),
          "| denied", sum(1 for i, _, _ in calls if state(i) == "DENIED"),
          "| errors", sum(1 for i, _, _ in calls if state(i) == "ERR"))
    if verbose:
        for i, n, a in calls:
            s = state(i)
            r = "" if s == "DENIED" else out.get(i, "")[:70].replace("\n", " ")
            print(" ", s, "|", n[:32], "|", a.replace("\n", " "), "|", r)
        print("--- final message ---")
        print((res.get("message") or final)[:1800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
