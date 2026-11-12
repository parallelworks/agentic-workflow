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

Writes runs/<id>/agent-digest.json for every run or evaluation the session CREATED:
  - site-runner-mcp: a run_heat result carrying the run_id
  - site-runner:     a Bash `pw workflows run ... --name <id> ... run.yaml` command
  - evaluator-mcp:   an evaluate_run result carrying the evaluator_id
  - evaluator:       a Bash `pw workflows run ... --name <eval-id>-fetch|-validate ...`
  - site-prep-mcp:   a run_spack or copy_buildcache result carrying the run_id
A tool result stored as a list of text parts (claude-code/opus) is read as its
joined text, the same as a plain string result.
Sessions that merely mention an id (compare-agents, the main agent) produce
nothing. One session can create several ids; each gets the same SESSION-level
digest (same session_id), so sum costs per session_id, not per id.

REDACTION: the digest keeps token usage, model, turn count, and the sequence of
tool calls. It never copies message text or reasoning. Tool arguments are kept
only for the heat MCP tools, for allowlisted workflow-related Bash commands, and
for Task's `subagent_type` and `background` (its prompt stays redacted); every
other argument is replaced by "<redacted>". Tool results are reduced to an
ok/error flag (plus status/state for the heat tools; a heat call fails only on
status rejected, failed or not_ready). The only result text kept is an error pw
code itself writes (`Error: unknown tool "..."`, `Error executing Task: ...`),
truncated to 300 characters, so a refused delegation can be diagnosed without
reading the session store.

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
PREP_ID = re.compile(r"site-prep-mcp-[A-Za-z0-9._-]+?-\d{6}")
EVAL_STEP = re.compile(r"^(evaluator-[A-Za-z0-9._-]+?-\d{6})-(?:fetch|validate)$")
BASH_KEEP = ("pw workflows ", "python3 scripts/_site_query.py", "git remote get-url",
             "git rev-parse", "mkdir runs/", "mkdir -p runs/", "cp workflows/",
             "cp ./workflows/", "ls runs", "ls results")
PATH_KEYS = ("file_path", "path", "pattern")
# Heat tool statuses that mean the call did NOT do its job. Every other status
# (submitted, polled, evaluated, listed, loaded, compared, written, ...) is a
# success, so a new read-only tool is not miscounted as a failure.
HEAT_FAILED = ("rejected", "failed", "not_ready")
SUBAGENT_TYPE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Errors pw code itself writes about a call (never tool output or message text);
# only these are copied, truncated, into the digest.
UNKNOWN_TOOL_ERROR = re.compile(r'^Error: unknown tool "[^"\n]{1,200}"$')
TASK_ERROR = re.compile(r'^Error executing Task: [^\n]+$')
ERROR_CHARS = 300
MCP_ERROR_PREFIX = "MCP tool error: "
# Session and agent ids pw code puts in hook payloads (e.g. 20261111-132318-cc93370d,
# agent-task-9). Only values of this shape are copied into hooks.jsonl.
HOOK_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
HOOK_REASON = re.compile(r"^[a-z_-]{1,40}$")


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
    if tool == "Task":
        # Which agent was asked and how; the prompt and description stay redacted.
        st = args.get("subagent_type")
        kept = {"subagent_type": st if isinstance(st, str) and SUBAGENT_TYPE.match(st)
                else ("<redacted>" if st is not None else None)}
        if isinstance(args.get("background"), bool):
            kept["background"] = args["background"]
        return kept
    return {"args": "<redacted>"}


def _content_text(content):
    """A tool result's text. Some providers (seen with claude-code/opus) store it as
    a list of parts, [{"type": "text", "text": "..."}], instead of a string; join
    the text parts so the result parses the same either way."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
            isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
            for p in content):
        return "".join(p["text"] for p in content)
    return json.dumps(content)


def _result_info(tool, content):
    text = _content_text(content)
    info = {"ok": not (text.startswith("Error") or "doesn't want to proceed" in text)}
    # Only whole single-line errors in pw's own wording; a subagent report that
    # merely starts like one has newlines or more text and is never copied.
    if UNKNOWN_TOOL_ERROR.match(text) or (tool == "Task" and TASK_ERROR.match(text)
                                          and len(text) <= 2 * ERROR_CHARS):
        info["error"] = text[:ERROR_CHARS]
    if tool.startswith("mcp__heat__"):
        # pw code prefixes a result the server marked isError with this text; the
        # JSON after it still carries status and run_id.
        if text.startswith(MCP_ERROR_PREFIX):
            info["ok"] = False
            text = text[len(MCP_ERROR_PREFIX):]
        try:
            res = json.loads(text)
            info.update({k: res.get(k) for k in ("status", "run_id", "state", "done",
                                                 "evaluator_id", "overall") if k in res})
            if "status" in res:
                info["ok"] = res["status"] not in HEAT_FAILED
        except (ValueError, AttributeError):
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
                        if name == "Bash" and cmd.startswith("pw workflows run"):
                            m_id = re.search(r"--name\s+(\S+)", cmd)
                            m_ev = EVAL_STEP.match(m_id.group(1)) if m_id else None
                            if m_ev and not m_ev.group(1).startswith("evaluator-mcp-"):
                                created[m_ev.group(1)] = "evaluator"
                elif m.get("role") == "tool":
                    call = pending.pop(m.get("tool_call_id"), None)
                    if call:
                        call.update(_result_info(call["tool"], m.get("content")))
                        if call["tool"] == "mcp__heat__run_heat" and call.get("run_id"):
                            created[call["run_id"]] = "site-runner-mcp"
                        if call["tool"] == "mcp__heat__evaluate_run" and call.get("evaluator_id"):
                            created[call["evaluator_id"]] = "evaluator-mcp"
                        if call["tool"] in ("mcp__heat__run_spack", "mcp__heat__copy_buildcache") \
                                and PREP_ID.fullmatch(str(call.get("run_id") or "")):
                            created[call["run_id"]] = "site-prep-mcp"

    return {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        out = dict(d, run_id=run_id, agent=agent, ids_created_by_session=sorted(created))
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
        # Breadcrumb: proves the hook fired. Key names, plus the session and agent
        # ids when they have the expected shape; no other payload values. pw code
        # fires SessionEnd only for interactive sessions, so a SessionEnd line with
        # its session_id marks that session (and every digest from it) interactive.
        ids = {k: payload[k] for k in ("session_id", "agent_id")
               if isinstance(payload.get(k), str) and HOOK_ID.match(payload[k])}
        # SessionEnd's reason (how the session ended, e.g. clear or exit), kept
        # only when it is a short lowercase word.
        if isinstance(payload.get("reason"), str) and HOOK_REASON.match(payload["reason"]):
            ids["reason"] = payload["reason"]
        os.makedirs(os.path.join(RUNS, "_mcp"), exist_ok=True)
        with open(os.path.join(RUNS, "_mcp", "hooks.jsonl"), "a") as fh:
            fh.write(json.dumps(dict({
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event": payload.get("hook_event_name"), "agent_type": payload.get("agent_type")},
                **ids, keys=sorted(payload), transcript_found=bool(path and os.path.isfile(path)),
                written=written)) + "\n")
    except Exception as e:
        sys.stderr.write("agent_digest: %s\n" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
