#!/usr/bin/env python3
"""heat_server.py — a lightweight, stdlib-only MCP server exposing two tools
for the site-runner-mcp agent:

  run_heat(target, grid)  stage + build + submit in one typed call. It performs the
                          run-invocation chain that site-runner builds by hand in
                          shell: resolve the target (via _site_query.py),
                          stage ./runs/<id>/, and submit `pw workflows run` against
                          the unmodified workflows/run.yaml. Returns right after
                          submit; it never waits for the job.
  poll_heat(run_id)       runs the unmodified workflows/poll.yaml (one logged
                          workflow run per poll, spaced 30s apart) until the job
                          has left the queue (DONE; success is NOT checked) or a
                          few minutes pass, and returns the state.

Launch (stdio transport; this is what the agent's mcpServers entry runs):
    python3 -I .agents/mcp/heat_server.py
Debug (no MCP, no submit — stage only, print the staged inputs):
    python3 -I .agents/mcp/heat_server.py --stage-only <target> <grid> [tol] [max_iter]

It lives under .agents/ because pw code makes every agent edit there ask a human
first (fails in -p), so no agent can rewrite the server or its _site_query.py.

Protocol: hand-rolled JSON-RPC 2.0 over newline-delimited stdio, Python 3.9
stdlib only (the host has no Python >= 3.10, which the MCP SDK requires).
DUAL-ERA per the MCP spec (verified against modelcontextprotocol.io, 2026-10-13):
  - legacy  (<= 2025-11-25): initialize -> notifications/initialized -> requests
  - modern  (2026-07-28):    no handshake; every request carries
                             params._meta["io.modelcontextprotocol/protocolVersion"];
                             server/discover MUST be implemented.
Every message received and sent is traced to runs/_mcp/heat-<UTC>-<pid>.jsonl and
stderr (never stdout, which carries only MCP messages). Any accommodation of
pw code behavior the spec does not describe is marked with a "spec says X; pw
code does Y" comment.

Constraints enforced in code: the ONLY cluster action is `pw workflows run`
(plus read-only `pw workflows runs view|logs` to read a run's outcome) — see
_pw(). No ssh, no scp, no environment dumps, no secret files.
"""
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

SERVER_NAME = "heat"
SERVER_VERSION = "0.3.0"
AGENT_NAME = "site-runner-mcp"  # run-id prefix: site-runner-mcp-<target>-<NNNNNN>

MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
META_VERSION = "io.modelcontextprotocol/protocolVersion"

HERE = os.path.dirname(os.path.abspath(__file__))  # <repo>/.agents/mcp (pw code-protected)
REPO = os.path.dirname(os.path.dirname(HERE))
SITES = os.path.join(REPO, "sites", "sites.yaml")
# The server only ever EXECUTES the protected copy next to it: agents cannot edit
# files under .agents/ without a human approving. The shared copy in scripts/ (the
# one site-runner runs) is only compared against, never executed, here.
SITE_QUERY = os.path.join(HERE, "_site_query.py")
SHARED_SITE_QUERY = os.path.join(REPO, "scripts", "_site_query.py")
WORKFLOWS = os.path.join(REPO, "workflows")
RUNS = os.path.join(REPO, "runs")

DEFAULT_TOL = "1e-3"
DEFAULT_MAX_ITER = 50000
SUBMIT_WAIT_SECONDS = 540  # stays under the agent's 10m MCP timeout
POLL_BUDGET_SECONDS = 420  # one poll_heat call; leaves headroom under 10m
POLL_INTERVAL_SECONDS = 30
RUN_ID_RE = re.compile(r"^%s-[A-Za-z0-9._-]+-\d{6}$" % re.escape(AGENT_NAME))
TERMINAL = ("completed", "error", "canceled", "cancelled", "failed")

INSTRUCTIONS = (
    "Call run_heat once with a target name from sites/sites.yaml and a grid size; it "
    "stages, builds and submits the job and returns a run_id. Then call poll_heat with "
    "that run_id until it returns done=true."
)

RUN_TOOL = {
    "name": "run_heat",
    "title": "Run the heat solver on a target",
    "description": (
        "Submit the heat job for ONE target (a name from sites/sites.yaml) at grid "
        "size N. Does everything for you: clones the repo on the cluster, builds, "
        "and submits. Returns run_id and job_id. Returns right after submit; it does "
        "NOT wait for the job — call poll_heat with the run_id next. "
        "grid must be >= the target's mpi_ranks."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target name, e.g. bigcluster-cpu"},
            "grid": {"type": "integer", "minimum": 1, "description": "Global grid size N (NxN), e.g. 128"},
            "tol": {"type": "string", "description": "Convergence tolerance (optional, default 1e-3)"},
            "max_iter": {"type": "integer", "minimum": 1, "description": "Max iterations (optional, default 50000)"},
        },
        "required": ["target", "grid"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["submitted", "rejected", "failed"]},
            "run_id": {"type": ["string", "null"]},
            "target": {"type": "string"},
            "job_id": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "target"],
    },
}

POLL_TOOL = {
    "name": "poll_heat",
    "title": "Check whether a submitted heat job has left the queue",
    "description": (
        "Check the scheduler state of a job submitted by run_heat. Pass the run_id "
        "that run_heat returned. Polls for up to a few minutes and returns as soon "
        "as the job has left the queue (state DONE, done true). If done is false, "
        "call poll_heat again with the same run_id. done=true means only that the "
        "job left the queue, NOT that it succeeded; the evaluator checks the result."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "The run_id returned by run_heat, e.g. site-runner-mcp-gce-cpu-000001"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["polled", "rejected", "failed"]},
            "run_id": {"type": "string"},
            "job_id": {"type": ["string", "null"]},
            "state": {"type": ["string", "null"], "enum": ["PENDING", "RUNNING", "DONE", "UNKNOWN", None]},
            "done": {"type": "boolean"},
            "left_queue": {"type": "boolean"},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "run_id", "done"],
    },
}

TOOLS = [RUN_TOOL, POLL_TOOL]


# --------------------------------------------------------------------------
# Tracing (stderr + a per-process JSONL file; never stdout)
# --------------------------------------------------------------------------
def _now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Trace:
    def __init__(self):
        self.path = None
        try:
            d = os.path.join(RUNS, "_mcp")
            os.makedirs(d, exist_ok=True)
            stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            self.path = os.path.join(d, "heat-%s-%d.jsonl" % (stamp, os.getpid()))
        except OSError:
            pass

    def write(self, direction, msg, extra_path=None):
        line = json.dumps({"ts": _now(), "dir": direction, "msg": msg}, separators=(",", ":"))
        sys.stderr.write("[heat-mcp] " + line + "\n")
        sys.stderr.flush()
        for p in (self.path, extra_path):
            if p:
                try:
                    with open(p, "a") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass


TRACE = None


# --------------------------------------------------------------------------
# Subprocess helpers
# --------------------------------------------------------------------------
def _pw(argv, timeout):
    """The ONLY way this server touches the platform. Allowlisted verbs:
    `workflows run` (the submission) and read-only `workflows runs view|logs`.
    Anything else — pw ssh, scp, other verbs — is refused by construction."""
    allowed = argv[:2] == ["workflows", "run"] or (
        argv[:2] == ["workflows", "runs"] and len(argv) > 2 and argv[2] in ("view", "logs")
    )
    if not allowed:
        raise RuntimeError("refused non-allowlisted pw command: pw %s" % " ".join(argv[:3]))
    pw = shutil.which("pw")
    if not pw:
        raise RuntimeError("the `pw` CLI was not found on PATH")
    p = subprocess.run([pw] + argv, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _check_site_query():
    """Fail closed unless the protected copy is byte-identical to scripts/'s copy.
    Catches tampering with the unprotected copy and keeps target resolution
    identical for both run agents. A legitimate change to scripts/_site_query.py
    must be re-copied into .agents/mcp/ by a human."""
    try:
        with open(SITE_QUERY, "rb") as a, open(SHARED_SITE_QUERY, "rb") as b:
            if a.read() == b.read():
                return
    except OSError as e:
        raise Rejected("cannot read _site_query.py for the parity check: %s" % e)
    raise Rejected("scripts/_site_query.py differs from the protected copy "
                   ".agents/mcp/_site_query.py; nothing was staged. A human must "
                   "review the change and re-copy it before run_heat can resolve targets.")


def _site(target, field):
    """Resolve one target field through the protected copy of the shared parser,
    run as a subprocess with the same `python3` the shell path uses, so
    PyYAML-vs-fallback parsing is identical for site-runner and site-runner-mcp."""
    _check_site_query()
    py = shutil.which("python3") or sys.executable
    p = subprocess.run([py, SITE_QUERY, SITES, target, field], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError("_site_query.py failed for %s/%s" % (target, field))
    return p.stdout


def _git(*args):
    p = subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def _target_names():
    """Target names, for the unknown-target error message only (resolution
    itself always goes through _site_query.py)."""
    names = []
    try:
        with open(SITES) as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("- name:"):
                    names.append(s.split(":", 1)[1].strip())
    except OSError:
        pass
    return names


# --------------------------------------------------------------------------
# run_heat
# --------------------------------------------------------------------------
class Rejected(Exception):
    pass


def _as_int(name, value):
    # Tolerate digit strings ("128") as well as JSON integers: small models
    # often quote numbers. Anything else is a clear validation error.
    if isinstance(value, bool):
        raise Rejected("%s must be an integer, got %r" % (name, value))
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise Rejected("%s must be an integer, got %r" % (name, value))


def resolve_and_validate(args):
    """Return (resolved target fields, grid, tol, max_iter) or raise Rejected."""
    target = args.get("target")
    if not isinstance(target, str) or not target.strip():
        raise Rejected("target is required (a name from sites/sites.yaml)")
    target = target.strip()
    grid = _as_int("grid", args.get("grid"))
    max_iter = _as_int("max_iter", args.get("max_iter", DEFAULT_MAX_ITER))
    tol = str(args.get("tol", DEFAULT_TOL)).strip()
    try:
        float(tol)
    except ValueError:
        raise Rejected("tol must be a number such as 1e-3, got %r" % tol)
    if grid < 1 or max_iter < 1:
        raise Rejected("grid and max_iter must be >= 1")

    if _site(target, "name") != target:
        raise Rejected("unknown target %r; valid targets: %s"
                       % (target, ", ".join(_target_names()) or "(none found)"))
    t = {f: _site(target, f) for f in
         ("ssh_name", "scheduler", "mode", "mpi_ranks", "mpi_launch", "env_load_joined", "directives")}
    t["name"] = target
    try:
        ranks = int(t["mpi_ranks"])
    except ValueError:
        raise Rejected("target %r has a non-integer mpi_ranks %r in sites.yaml" % (target, t["mpi_ranks"]))
    # Guardrail the shell path does not enforce up front: grid < ranks makes the
    # solver MPI_Abort on every rank, so refuse before launching a doomed job.
    if grid < ranks:
        raise Rejected("grid %d is smaller than target %r mpi_ranks %d; the solver would "
                       "MPI_Abort on every rank. Use grid >= %d. Nothing was submitted."
                       % (grid, target, ranks, ranks))
    return t, grid, tol, max_iter


def allocate_run_dir(target):
    """First free <agent>-<target>-<NNNNNN>, claimed atomically with mkdir."""
    os.makedirs(RUNS, exist_ok=True)
    for n in range(1, 1000000):
        run_id = "%s-%s-%06d" % (AGENT_NAME, target, n)
        try:
            os.mkdir(os.path.join(RUNS, run_id))
            return run_id
        except FileExistsError:
            continue
    raise RuntimeError("no free run id for target %r" % target)


def stage(t, grid, tol, max_iter):
    """Stage ./runs/<id>/ exactly as AGENTS.md prescribes; return (run_id, inputs)."""
    run_id = allocate_run_dir(t["name"])
    rdir = os.path.join(RUNS, run_id)
    for name in ("run.yaml", "poll.yaml"):
        shutil.copyfile(os.path.join(WORKFLOWS, name), os.path.join(rdir, name))
    inputs = {
        "cluster": t["ssh_name"],
        "remote_dir": "pw-heat/%s" % run_id,
        "repo_url": _git("remote", "get-url", "origin"),
        "commit": _git("rev-parse", "HEAD"),
        "scheduler": t["scheduler"],
        "mode": t["mode"],
        "mpi_ranks": t["mpi_ranks"],
        "grid": str(grid),
        "tol": tol,
        "max_iter": str(max_iter),
        "mpi_launch": t["mpi_launch"],
        "env_load": t["env_load_joined"],
        # Same bytes as `_site_query.py ... directives | base64 -w0`.
        "directives_b64": base64.b64encode(t["directives"].encode()).decode(),
    }
    with open(os.path.join(rdir, "run.inputs.json"), "w") as fh:
        json.dump(inputs, fh, indent=2)
        fh.write("\n")
    return run_id, inputs


def _find_slug(text):
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for key in ("slug", "runSlug"):
                if isinstance(obj.get(key), str):
                    return obj[key]
            for v in obj.values():
                if isinstance(v, dict) and isinstance(v.get("slug"), str):
                    return v["slug"]
    except ValueError:
        pass
    m = re.search(r'"slug"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def _phase(step_name):
    head = step_name.split()[0].upper() if step_name else ""
    return head if head in ("STAGE", "BUILD", "SUBMIT") else None


def submit(run_id, inputs):
    """Run the workflow; wait only for the submit step (not the job); return a
    dict of submission facts."""
    rel = "runs/%s" % run_id
    argv = ["workflows", "run", "-i", rel + "/run.inputs.json", "--name", run_id,
            "-o", "json", rel + "/run.yaml"]
    record = {"run_id": run_id, "argv": ["pw"] + argv, "started": _now()}
    rc, out, err = _pw(argv, timeout=SUBMIT_WAIT_SECONDS)
    slug = _find_slug(out)
    record.update(exit_code=rc, workflow_run=slug)
    if not slug:
        record.update(status="failed", error=("pw workflows run did not return a run slug: "
                                              + (err or out).strip()[-1500:]))
        return record

    # `pw workflows run` returns as soon as the run is created (status "running"),
    # so wait for the workflow — not the scheduler job — to finish.
    view = _wait_terminal(slug, SUBMIT_WAIT_SECONDS)
    record["workflow_status"] = view.get("status")

    if view.get("status") != "completed":
        failed_step = None
        for job in (view.get("executedJobs") or {}).values():
            for step in job.get("steps", []):
                if step.get("status") == "error":
                    failed_step = step.get("name")
        _, lout, _ = _pw(["workflows", "runs", "logs", "--failed", "--tail", "20", slug], timeout=60)
        record.update(status="failed", failed_phase=_phase(failed_step),
                      error="workflow %s ended %s; failed step log tail:\n%s"
                            % (slug, view.get("status") or "unknown (timed out waiting)",
                               lout.strip()[-2500:]))
        return record

    _, lout, _ = _pw(["workflows", "runs", "logs", "--job", "run", "--tail", "5", slug], timeout=60)
    m = re.search(r"SUBMITTED scheduler=\S+ job_id=(\S+)", lout)
    if not m:
        record.update(status="failed", failed_phase="SUBMIT",
                      error="workflow completed but no SUBMITTED job_id line was found in its log")
        return record
    record.update(status="submitted", job_id=m.group(1))
    return record


def run_heat(args):
    """Returns (structured_result, is_error, run_id_or_None)."""
    target = str(args.get("target", "")).strip()
    base = {"status": "rejected", "run_id": None, "target": target, "job_id": None,
            "next": None, "error": None}
    try:
        t, grid, tol, max_iter = resolve_and_validate(args)
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None

    run_id, inputs = stage(t, grid, tol, max_iter)
    rdir = os.path.join(RUNS, run_id)
    try:
        rec = submit(run_id, inputs)
    except Exception as e:  # pw missing, timeout, refused verb...
        rec = {"run_id": run_id, "status": "failed", "error": str(e)}
    rec["finished"] = _now()
    with open(os.path.join(rdir, "submit.json"), "w") as fh:
        json.dump(rec, fh, indent=2)
        fh.write("\n")

    result = {
        "status": rec["status"], "run_id": run_id, "target": t["name"],
        "cluster": inputs["cluster"], "scheduler": inputs["scheduler"], "mode": inputs["mode"],
        "mpi_ranks": inputs["mpi_ranks"], "grid": inputs["grid"], "commit": inputs["commit"],
        "remote_dir": "$HOME/" + inputs["remote_dir"],
        "workflow_run": rec.get("workflow_run"), "job_id": rec.get("job_id"),
        "next": None, "error": rec.get("error"), "failed_phase": rec.get("failed_phase"),
    }
    if rec["status"] == "submitted":
        with open(os.path.join(rdir, "poll.inputs.json"), "w") as fh:
            json.dump({"cluster": inputs["cluster"], "scheduler": inputs["scheduler"],
                       "job_id": rec["job_id"]}, fh, indent=2)
            fh.write("\n")
        result["next"] = "Call poll_heat with run_id %s until it returns done=true." % run_id
    return result, rec["status"] != "submitted", run_id


def _wait_terminal(slug, budget):
    """Wait for a workflow run (not the scheduler job) to finish; return its view."""
    deadline = time.time() + budget
    view = {}
    while True:
        vrc, vout, _ = _pw(["workflows", "runs", "view", slug, "-o", "json"], timeout=60)
        try:
            view = json.loads(vout) if vrc == 0 else {}
        except ValueError:
            view = {}
        if view.get("status") in TERMINAL or time.time() > deadline:
            return view
        time.sleep(2)


def poll_once(run_id, n):
    """One logged poll.yaml run named <id>-poll-<n>; returns (state, raw, slug, error)."""
    rel = "runs/%s" % run_id
    name = "%s-poll-%d" % (run_id, n)
    rc, out, err = _pw(["workflows", "run", "-i", rel + "/poll.inputs.json", "--name", name,
                        "-o", "json", rel + "/poll.yaml"], timeout=120)
    slug = _find_slug(out)
    if not slug:
        return None, None, None, "pw workflows run (poll) returned no run slug: " + (err or out).strip()[-500:]
    view = _wait_terminal(slug, 120)
    if view.get("status") != "completed":
        return None, None, slug, "poll workflow %s ended %s" % (slug, view.get("status") or "unknown")
    _, lout, _ = _pw(["workflows", "runs", "logs", "--job", "poll", "--tail", "5", slug], timeout=60)
    found = None
    for line in lout.splitlines():
        line = line.strip()
        if line.startswith("{") and '"state"' in line:
            try:
                found = json.loads(line)
            except ValueError:
                pass
    if not found:
        return None, None, slug, "poll workflow %s printed no state line" % slug
    return found.get("state"), found.get("raw"), slug, None


def poll_heat(args):
    """Returns (structured_result, is_error, run_id_or_None)."""
    run_id = str(args.get("run_id", "")).strip()
    base = {"status": "rejected", "run_id": run_id, "job_id": None, "state": None,
            "done": False, "next": None, "error": None}
    rdir = os.path.join(RUNS, run_id)
    if not RUN_ID_RE.match(run_id) or not os.path.isfile(os.path.join(rdir, "poll.inputs.json")):
        base["error"] = ("unknown run_id %r: pass the run_id that run_heat returned for a "
                         "submitted job (e.g. %s-gce-cpu-000001)" % (run_id, AGENT_NAME))
        return base, True, None
    with open(os.path.join(rdir, "poll.inputs.json")) as fh:
        base["job_id"] = json.load(fh).get("job_id")
    log_path = os.path.join(rdir, "polls.jsonl")
    try:
        with open(log_path) as fh:
            n = sum(1 for _ in fh)
    except OSError:
        n = 0

    deadline = time.time() + POLL_BUDGET_SECONDS
    polls = 0
    while True:
        n += 1
        polls += 1
        try:
            state, raw, slug, error = poll_once(run_id, n)
        except Exception as e:
            state, raw, slug, error = None, None, None, str(e)
        with open(log_path, "a") as fh:
            fh.write(json.dumps({"ts": _now(), "n": n, "workflow_run": slug, "state": state,
                                 "raw": raw, "error": error}, separators=(",", ":")) + "\n")
        if error:
            base.update(status="failed", error=error)
            return base, True, run_id
        if state in ("DONE", "UNKNOWN") or time.time() + POLL_INTERVAL_SECONDS > deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    base.update(status="polled", state=state, done=(state == "DONE"), polls_this_call=polls, total_polls=n)
    if state == "DONE":
        # poll.yaml prints DONE for any job no longer in the queue, FAILED ones
        # included (seen with job 10), so say exactly that and no more.
        base["left_queue"] = True
        base["next"] = ("The job has left the queue. That does NOT mean it succeeded; "
                        "the evaluator checks the result. Report that the job left the "
                        "queue and stop; do not poll again.")
    elif state == "UNKNOWN":
        base["next"] = "The scheduler reported an unexpected state (%s). Report it and stop." % raw
    else:
        base["next"] = ("The job is still %s (%s). Call poll_heat again with run_id %s."
                        % (state, raw, run_id))
    return base, False, run_id


# --------------------------------------------------------------------------
# JSON-RPC / MCP protocol
# --------------------------------------------------------------------------
class RpcError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code, self.message, self.data = code, message, data


def _modern_version(params):
    meta = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(meta, dict) and META_VERSION in meta:
        return meta[META_VERSION]
    return None


def handle(method, params):
    """Dispatch one request. Returns (result, run_id_for_trace)."""
    modern = _modern_version(params)
    if modern is not None and modern not in MODERN_VERSIONS:
        raise RpcError(-32022, "Unsupported protocol version",
                       {"supported": MODERN_VERSIONS + LEGACY_VERSIONS, "requested": modern})

    def done(result):
        # Modern (2026-07-28) results carry resultType; legacy results do not.
        if modern is not None:
            result["resultType"] = "complete"
        return result

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}, None
    if method == "server/discover":
        return done({"supportedVersions": MODERN_VERSIONS + LEGACY_VERSIONS,
                     "capabilities": {"tools": {}},
                     "_meta": {"io.modelcontextprotocol/serverInfo":
                               {"name": SERVER_NAME, "version": SERVER_VERSION}},
                     "instructions": INSTRUCTIONS}), None
    if method == "ping":
        return done({}), None
    if method == "tools/list":
        return done({"tools": TOOLS}), None
    if method == "tools/call":
        name = params.get("name")
        tool = {"run_heat": run_heat, "poll_heat": poll_heat}.get(name)
        if tool is None:
            raise RpcError(-32602, "Unknown tool: %s" % name)
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments must be an object")
        result, is_error, run_id = tool(args)
        return done({"content": [{"type": "text", "text": json.dumps(result)}],
                     "structuredContent": result, "isError": is_error}), run_id
    raise RpcError(-32601, "Method not found: %s" % method)


def _send(msg, extra_path=None):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    TRACE.write("out", msg, extra_path)


def serve():
    global TRACE
    TRACE = Trace()
    # Read raw bytes and decode as UTF-8 ourselves, independent of the locale.
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            TRACE.write("in", {"unparseable": line[:2000]})
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        TRACE.write("in", msg)
        if not isinstance(msg, dict) or "method" not in msg:
            # Batches and stray responses are not part of MCP over stdio.
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                continue
            _send({"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                   "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        if "id" not in msg:
            continue  # notifications (initialized, cancelled, ...) need no reply
        params = msg.get("params") or {}
        run_path = None
        try:
            result, run_id = handle(msg["method"], params)
            if run_id:
                # Copy this tools/call request into the run's own trace; the
                # reply follows via _send(reply, run_path).
                run_path = os.path.join(RUNS, run_id, "rpc.jsonl")
                try:
                    with open(run_path, "a") as fh:
                        fh.write(json.dumps({"ts": _now(), "dir": "in", "msg": msg},
                                            separators=(",", ":")) + "\n")
                except OSError:
                    pass
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except RpcError as e:
            err = {"code": e.code, "message": e.message}
            if e.data is not None:
                err["data"] = e.data
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": err}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32603, "message": "Internal error: %s" % e}}
        _send(reply, run_path)
    # stdin EOF: the spec's graceful-shutdown signal.
    return 0


def stage_only(argv):
    args = {"target": argv[0], "grid": argv[1]}
    if len(argv) > 2:
        args["tol"] = argv[2]
    if len(argv) > 3:
        args["max_iter"] = argv[3]
    try:
        t, grid, tol, max_iter = resolve_and_validate(args)
    except Rejected as e:
        print(json.dumps({"status": "rejected", "error": str(e)}, indent=2))
        return 1
    run_id, inputs = stage(t, grid, tol, max_iter)
    print(json.dumps({"run_id": run_id, "inputs": inputs}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--stage-only":
        sys.exit(stage_only(sys.argv[2:]))
    sys.exit(serve())
