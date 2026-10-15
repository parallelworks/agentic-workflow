#!/usr/bin/env python3
"""agent_digest.py — write a REDACTED digest of a run agent's pw code session into
the run's staging dir, so compare-agents can compare agents without reading the
pw code session store.

Runs as a pw code hook (configured in .agents/settings.json; needs `/hooks trust`):
  SubagentStop  payload gives agent_type + agent_transcript_path (delegated agents)
  SessionEnd    payload gives transcript_path (e.g. `pw code --agent site-runner-mcp`)
and reads the hook payload JSON on stdin. Also usable by hand to backfill:
  python3 -I .agents/hooks/agent_digest.py --transcript <session.jsonl> [--agent NAME]
It lives under .agents/ because hook trust covers only the hook COMMAND, not this
file; pw code makes every agent edit under .agents/ ask a human first.

Writes runs/<run_id>/agent-digest.json for every run the session CREATED:
  - site-runner-mcp: a run_heat result carrying the run_id
  - site-runner:     a Bash `pw workflows run ... --name <id> ... run.yaml` command
Sessions that merely mention a run id (evaluator, compare-agents, the main agent)
produce nothing.

REDACTION: the digest keeps token usage, model, turn count, and the sequence of
tool calls. It never copies message text or reasoning. Tool arguments are kept
only for the heat MCP tools and for allowlisted workflow-related Bash commands;
every other argument is replaced by "<redacted>". Tool results are reduced to an
ok/error flag (plus status/state for the heat tools).

As a hook it must never disturb the session: it prints nothing to stdout and
always exits 0 (SubagentStop is blockable, and stdout could be read as a verdict).
"""
import datetime
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNS = os.path.join(REPO, "runs")
RUN_ID = re.compile(r"\b(site-runner(?:-mcp)?-[A-Za-z0-9._-]+?-\d{6})\b")
BASH_KEEP = ("pw workflows ", "python3 scripts/_site_query.py", "git remote get-url",
             "git rev-parse", "mkdir -p runs/", "cp workflows/", "cp ./workflows/")
PATH_KEYS = ("file_path", "path", "pattern")


def _args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except ValueError:
        return {}


def _redact(tool, args):
    if tool.startswith("mcp__heat__"):
        return args
    if tool == "Bash":
        cmd = str(args.get("command", ""))
        return {"command": cmd} if cmd.startswith(BASH_KEEP) else {"command": "<redacted>"}
    if tool in ("ReadFile", "WriteFile", "EditFile", "GlobSearch", "GrepSearch"):
        kept = {k: args[k] for k in PATH_KEYS if isinstance(args.get(k), str)
                and not args[k].startswith(("/", "~"))}
        return kept or {"args": "<redacted>"}
    return {"args": "<redacted>"}


def _result_info(tool, content):
    text = content if isinstance(content, str) else json.dumps(content)
    info = {"ok": not (text.startswith("Error") or "doesn't want to proceed" in text)}
    if tool.startswith("mcp__heat__"):
        try:
            res = json.loads(text)
            info.update({k: res.get(k) for k in ("status", "run_id", "state", "done") if k in res})
            info["ok"] = res.get("status") in ("submitted", "polled")
        except ValueError:
            pass
    return info


def digest(transcript, agent_type=None, event=None):
    usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    other_usage = {}
    models, calls, pending = set(), [], {}
    turns, session_id, created = 0, None, {}

    with open(transcript) as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "session":
                session_id = (e.get("session") or {}).get("id") or session_id
            elif t == "usage":
                u = e.get("usage") or {}
                tok = u.get("tokens") or {}
                if u.get("model"):
                    models.add(u["model"])
                bucket = usage if u.get("purpose", "conversation") == "conversation" \
                    else other_usage.setdefault(u.get("purpose"), {"requests": 0, "total_tokens": 0})
                bucket["requests"] += 1
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if k in bucket:
                        bucket[k] += int(tok.get(k) or 0)
            elif t == "message":
                m = e.get("message") or {}
                if m.get("role") == "assistant":
                    turns += 1
                    for c in m.get("tool_calls") or []:
                        f = c.get("function") or c
                        name = str(f.get("name") or "?")
                        args = _args(f.get("arguments"))
                        call = {"n": len(calls) + 1, "ts": e.get("timestamp"),
                                "tool": name, "args": _redact(name, args)}
                        calls.append(call)
                        pending[c.get("id")] = call
                        cmd = str(args.get("command", ""))
                        if name == "Bash" and cmd.startswith("pw workflows run") and "run.yaml" in cmd:
                            m_id = re.search(r"--name\s+(\S+)", cmd)
                            if m_id and RUN_ID.fullmatch(m_id.group(1)):
                                created[m_id.group(1)] = "site-runner"
                elif m.get("role") == "tool":
                    call = pending.pop(m.get("tool_call_id"), None)
                    if call:
                        call.update(_result_info(call["tool"], m.get("content")))
                        if call["tool"] == "mcp__heat__run_heat" and call.get("run_id"):
                            created[call["run_id"]] = "site-runner-mcp"

    return {
        "captured_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hook_event": event,
        "session_id": session_id,
        "agent_type_from_hook": agent_type,
        "models": sorted(models),
        "turns": turns,
        "usage": usage,
        "usage_other_purposes": other_usage,
        "tool_call_count": len(calls),
        "failed_tool_calls": sum(1 for c in calls if c.get("ok") is False),
        "tool_calls": calls,
    }, created


def write(transcript, agent_type=None, event=None):
    d, created = digest(transcript, agent_type, event)
    written = []
    for run_id, agent in created.items():
        rdir = os.path.join(RUNS, run_id)
        if not os.path.isdir(rdir):
            continue
        out = dict(d, run_id=run_id, agent=agent)
        with open(os.path.join(rdir, "agent-digest.json"), "w") as fh:
            json.dump(out, fh, indent=2)
            fh.write("\n")
        written.append(os.path.relpath(os.path.join(rdir, "agent-digest.json"), REPO))
    return written


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--transcript":
        agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv else None
        for p in write(sys.argv[2], agent, "manual"):
            print(p)
        return 0
    try:  # hook mode: silent, never fails the session
        payload = json.load(sys.stdin)
        path = payload.get("agent_transcript_path") or payload.get("transcript_path")
        written = []
        if path and os.path.isfile(path):
            written = write(path, payload.get("agent_type"), payload.get("hook_event_name"))
        # Breadcrumb: proves the hook fired. Key names only, never payload values.
        os.makedirs(os.path.join(RUNS, "_mcp"), exist_ok=True)
        with open(os.path.join(RUNS, "_mcp", "hooks.jsonl"), "a") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event": payload.get("hook_event_name"), "agent_type": payload.get("agent_type"),
                "keys": sorted(payload), "transcript_found": bool(path and os.path.isfile(path)),
                "written": written}) + "\n")
    except Exception as e:
        sys.stderr.write("agent_digest: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
