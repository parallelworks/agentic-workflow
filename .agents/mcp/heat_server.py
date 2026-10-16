#!/usr/bin/env python3
"""heat_server.py — a lightweight, stdlib-only MCP server. One process serves
the tools its --tools flag names (default: run_heat,poll_heat), so each agent's
own server instance exposes only that agent's tools:

  site-runner-mcp (default):

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
  evaluator-mcp (--tools evaluate_run):
  evaluate_run(run_id)    for a run either run agent made: polls once to confirm the
                          job left the queue, runs the unmodified fetch.yaml and
                          validate.yaml, checks the record against the run's own
                          target (ranks, mode, nodes, convergence) and always writes
                          results/evaluator-mcp-<target>-<NNNNNN>/verdict.json.

Launch (stdio transport; this is what each agent's mcpServers entry runs):
    python3 -I .agents/mcp/heat_server.py                        # site-runner-mcp
    python3 -I .agents/mcp/heat_server.py --tools evaluate_run   # evaluator-mcp
Debug (no MCP):
    python3 -I .agents/mcp/heat_server.py --stage-only <target> <grid> [tol] [max_iter]
        stage only, no submit; prints the staged inputs
    python3 -I .agents/mcp/heat_server.py --evaluate <run_id>
        runs evaluate_run once (this DOES run the poll/fetch/validate workflows)

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
SERVER_VERSION = "0.6.1"
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
RESULTS = os.path.join(REPO, "results")

DEFAULT_TOL = "1e-3"
DEFAULT_L2_TOLERANCE = "1e-6"  # validate.yaml's pass tolerance; NOT the solver's tol
DEFAULT_MAX_ITER = 50000
SUBMIT_WAIT_SECONDS = 540  # stays under the agent's 10m MCP timeout
POLL_BUDGET_SECONDS = 420  # one poll_heat call; leaves headroom under 10m
POLL_INTERVAL_SECONDS = 30
RUN_ID_RE = re.compile(r"^%s-[A-Za-z0-9._-]+-\d{6}$" % re.escape(AGENT_NAME))
TERMINAL = ("completed", "error", "canceled", "cancelled", "failed")

RUN_INSTRUCTIONS = (
    "Call run_heat once with a target name from sites/sites.yaml and a grid size; it "
    "stages, builds and submits the job and returns a run_id. Then call poll_heat with "
    "that run_id until it returns done=true."
)
EVAL_INSTRUCTIONS = (
    "Call evaluate_run once per run id a run agent reported. It fetches, validates and "
    "audits the run and saves its verdict.json; overall PASS needs a passing validation "
    "and no integrity_flags."
)


COMPARE_INSTRUCTIONS = (
    "Call list_runs to see the runs, then compare_runs once per pair of a site-runner-mcp "
    "run and a site-runner run; it writes comparison.json and report.md. Then call "
    "write_summary once with a short summary."
)


def _instructions():
    served = [t["name"] for t in TOOLS]
    return " ".join(([RUN_INSTRUCTIONS] if "run_heat" in served else [])
                    + ([EVAL_INSTRUCTIONS] if "evaluate_run" in served else [])
                    + ([COMPARE_INSTRUCTIONS] if "compare_runs" in served else []))

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

EVAL_TOOL = {
    "name": "evaluate_run",
    "title": "Fetch, validate and audit one completed heat run",
    "description": (
        "Evaluate ONE run that a run agent reported (pass its run_id, e.g. "
        "site-runner-mcp-gce-cpu-000001 or site-runner-gce-cpu-000001). Does everything "
        "for you: confirms the job has left the queue, fetches the solver's run record, "
        "validates the result on the cluster against the reference, checks the run "
        "against its target (ranks, mode, nodes, convergence), and saves "
        "results/<evaluator_id>/verdict.json. overall is PASS only if validation passed "
        "AND there are no integrity_flags. Call it once per run."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "A run id reported by site-runner-mcp or site-runner"},
            "l2_tolerance": {"type": "string",
                             "description": "Relative-L2 pass tolerance vs the reference (optional, "
                                            "default 1e-6). NOT the solver's convergence tol."},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["evaluated", "not_ready", "rejected", "failed"]},
            "evaluator_id": {"type": ["string", "null"]},
            "run_id": {"type": "string"},
            "overall": {"type": ["string", "null"], "enum": ["PASS", "FAIL", "NOT EVALUATED", None]},
            "record": {"type": ["object", "null"]},
            "verdict": {"type": ["object", "null"]},
            "integrity_flags": {"type": "array", "items": {"type": "string"}},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "run_id", "overall"],
    },
}

LIST_TOOL = {
    "name": "list_runs",
    "title": "List the run agents' runs and who evaluated them",
    "description": (
        "Read-only. Lists every site-runner-mcp and site-runner run in runs/ (optionally "
        "for one target) with its grid, commit, job_id, poll count and the evaluator ids "
        "that evaluated it. Use it to find pairs for compare_runs."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"target": {"type": "string", "description": "Only runs of this target (optional)"}},
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"status": {"type": "string"}, "runs": {"type": "array"}},
        "required": ["status", "runs"],
    },
}

COMPARE_TOOL = {
    "name": "compare_runs",
    "title": "Compare one site-runner-mcp run with one site-runner run",
    "description": (
        "Read-only facts for ONE pair of runs of the same target: input parity (the only "
        "expected difference is remote_dir), whether each run's directives match the "
        "target, poll counts, cost per agent session (from hook digests), and both "
        "evaluators' NEWEST verdict for each run with flags and PASS/FAIL compared. "
        "Claims a new results/compare-agents-<target>-<NNNNNN>/ and writes "
        "comparison.json and report.md (all tables) there; then call write_summary."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "mcp_run_id": {"type": "string", "description": "A site-runner-mcp run id"},
            "shell_run_id": {"type": "string", "description": "A site-runner run id"},
        },
        "required": ["mcp_run_id", "shell_run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["compared", "rejected"]},
            "report_dir": {"type": ["string", "null"]},
            "comparable": {"type": "boolean"},
            "parity": {"type": "object"},
            "runs": {"type": "object"},
            "evaluations": {"type": "array"},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

SUMMARY_TOOL = {
    "name": "write_summary",
    "title": "Add the summary to a comparison report",
    "description": (
        "Adds your short summary (plain markdown, at most 4000 characters) to the end of "
        "the report.md that compare_runs wrote. Pass the report_dir compare_runs returned. "
        "Works once per report; the tables above it are not changed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "report_dir": {"type": "string", "description": "report_dir from compare_runs, e.g. results/compare-agents-gce-cpu-000001"},
            "summary": {"type": "string", "description": "A short summary of the comparison (markdown)"},
        },
        "required": ["report_dir", "summary"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["written", "rejected"]},
                       "report": {"type": ["string", "null"]}, "error": {"type": ["string", "null"]}},
        "required": ["status"],
    },
}

ALL_TOOLS = {"run_heat": RUN_TOOL, "poll_heat": POLL_TOOL, "evaluate_run": EVAL_TOOL,
             "list_runs": LIST_TOOL, "compare_runs": COMPARE_TOOL, "write_summary": SUMMARY_TOOL}
# Which tools this process serves, from --tools (see __main__). The default keeps
# the server exactly as site-runner-mcp has always launched it.
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


def allocate_dir(prefix, target, base=RUNS):
    """First free <prefix>-<target>-<NNNNNN> under `base`, claimed atomically with mkdir."""
    os.makedirs(base, exist_ok=True)
    for n in range(1, 1000000):
        dir_id = "%s-%s-%06d" % (prefix, target, n)
        try:
            os.mkdir(os.path.join(base, dir_id))
            return dir_id
        except FileExistsError:
            continue
    raise RuntimeError("no free id for %s on target %r" % (prefix, target))


def stage(t, grid, tol, max_iter):
    """Stage ./runs/<id>/ exactly as AGENTS.md prescribes; return (run_id, inputs)."""
    run_id = allocate_dir(AGENT_NAME, t["name"])
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


def _json_line(text, key):
    """The last line of a workflow log that is a JSON object containing `key`."""
    found = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and '"%s"' % key in line:
            try:
                found = json.loads(line)
            except ValueError:
                pass
    return found


def run_workflow(dir_id, op, name, budget=120):
    """Run the staged runs/<dir_id>/<op>.yaml as one logged workflow named `name`
    and return (workflow status, log text of its `op` job, slug, error)."""
    rel = "runs/%s" % dir_id
    rc, out, err = _pw(["workflows", "run", "-i", "%s/%s.inputs.json" % (rel, op), "--name", name,
                        "-o", "json", "%s/%s.yaml" % (rel, op)], timeout=120)
    slug = _find_slug(out)
    if not slug:
        return None, "", None, ("pw workflows run (%s) returned no run slug: " % op
                                + (err or out).strip()[-500:])
    view = _wait_terminal(slug, budget)
    _, lout, _ = _pw(["workflows", "runs", "logs", "--job", op, "--tail", "20", slug], timeout=60)
    return view.get("status"), lout, slug, None


def poll_once(run_id, n):
    """One logged poll.yaml run named <id>-poll-<n>; returns (state, raw, slug, error)."""
    return _poll(run_id, "%s-poll-%d" % (run_id, n))


def _poll(dir_id, name):
    status, lout, slug, error = run_workflow(dir_id, "poll", name)
    if error:
        return None, None, None, error
    if status != "completed":
        return None, None, slug, "poll workflow %s ended %s" % (slug, status or "unknown")
    found = _json_line(lout, "state")
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
# evaluate_run (evaluator-mcp)
# --------------------------------------------------------------------------
EVAL_AGENT = "evaluator-mcp"  # evaluator id prefix: evaluator-mcp-<target>-<NNNNNN>
RUN_AGENTS = ("site-runner-mcp", "site-runner")  # longest first: prefixes overlap
REMOTE_DIR_RE = re.compile(r"^pw-heat/[A-Za-z0-9._-]+$")
# Nodes a target's directives request — AUDIT ONLY. The run tooling never parses
# directives (AGENTS.md); this reads them after the fact to compare with the
# record's distinct_hosts, and only these exact forms count. Anything else
# leaves nodes_requested null and the node check "not checked".
NODE_PATTERNS = (
    re.compile(r"^#SBATCH\s+(?:--nodes[= ]|-N\s*)(\d+)\s*$"),
    re.compile(r"^#PBS\s+-l\s+(?:.*[:,])?(?:select|nodes)=(\d+)\b"),
)


def _b64_text(b64):
    try:
        return base64.b64decode(str(b64 or ""), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def _same_directives(a, b):
    """Directive blocks are the same if they differ at most in trailing
    whitespace: run.yaml echoes a newline after the block, so a block missing its
    final newline yields the same job script (seen on site-runner-gce-cpu-000003)."""
    return a is not None and b is not None and a.rstrip() == b.rstrip()


def _split_run_id(run_id):
    """(run agent, target) for <run-agent>-<target>-<NNNNNN>, else (None, None)."""
    m = re.match(r"^(.+)-\d{6}$", run_id)
    if not m:
        return None, None
    for agent in RUN_AGENTS:
        if m.group(1).startswith(agent + "-"):
            return agent, m.group(1)[len(agent) + 1:]
    return None, None


def _nodes_requested(directives):
    for line in directives.splitlines():
        for pat in NODE_PATTERNS:
            m = pat.match(line.strip())
            if m:
                return int(m.group(1))
    return None


def _integrity(inputs, directives, record):
    """Check the record against the run's own target; return (flags, checks)."""
    flags, checks = [], {}
    try:
        want = int(str(inputs.get("mpi_ranks")).strip())
    except ValueError:
        want = None
    got = record.get("ranks")
    checks["ranks"] = {"expected": want, "actual": got, "ok": want is not None and got == want}
    if not checks["ranks"]["ok"]:
        flags.append("RANKS_MISMATCH: ran %s ranks, target mpi_ranks is %s" % (got, want))
    checks["mode"] = {"expected": inputs.get("mode"), "actual": record.get("mode"),
                      "ok": record.get("mode") == inputs.get("mode")}
    if not checks["mode"]["ok"]:
        flags.append("MODE_MISMATCH: ran %s, target mode is %s" % (record.get("mode"), inputs.get("mode")))
    nodes = _nodes_requested(directives)
    hosts = record.get("distinct_hosts")
    checks["nodes"] = {"expected": nodes, "actual": hosts,
                       "ok": None if nodes is None else hosts == nodes}
    if nodes is not None and hosts != nodes:
        flags.append("MULTI_NODE_MISMATCH: used %s hosts, directives request %s nodes" % (hosts, nodes))
    try:
        cap = int(str(inputs.get("max_iter")).strip())
    except ValueError:
        cap = None
    iters = record.get("iterations")
    converged = record.get("converged") is True and (cap is None or not isinstance(iters, int) or iters < cap)
    checks["converged"] = {"converged": record.get("converged"), "iterations": iters, "max_iter": cap,
                           "ok": converged}
    if not converged:
        flags.append("NOT_CONVERGED: converged=%s after %s of %s iterations"
                     % (record.get("converged"), iters, cap))
    # Text comparison, not parsing: did the job get the target's directives at all?
    sent = _b64_text(inputs.get("directives_b64"))
    sent_ok = _same_directives(sent, directives)
    checks["directives"] = {"ok": sent_ok}
    if sent_ok and sent != directives:
        checks["directives"]["note"] = "differs from the target only in trailing whitespace"
    if not sent_ok:
        flags.append("DIRECTIVES_MISMATCH: the run's directives_b64 is not the base64 of the "
                     "target's directives in sites.yaml, so the scheduler got other directives")
    if not inputs.get("commit"):
        flags.append("NO_COMMIT: the run's run.inputs.json has no commit")
    return flags, checks


def evaluate_run(args):
    """Returns (structured_result, is_error, eval_id_or_None)."""
    run_id = str(args.get("run_id", "")).strip()
    tol = str(args.get("l2_tolerance", DEFAULT_L2_TOLERANCE)).strip()
    base = {"status": "rejected", "evaluator_id": None, "run_id": run_id, "overall": None,
            "error": None}
    agent, target = _split_run_id(run_id)
    rdir = os.path.join(RUNS, run_id)
    if agent is None or not os.path.isfile(os.path.join(rdir, "run.inputs.json")):
        base["error"] = ("unknown run_id %r: pass a run id reported by a run agent, e.g. "
                         "site-runner-mcp-gce-cpu-000001 or site-runner-gce-cpu-000001 "
                         "(runs/<run_id>/run.inputs.json must exist)" % run_id)
        return base, True, None
    try:
        float(tol)
    except ValueError:
        base["error"] = "l2_tolerance must be a number such as 1e-6, got %r" % tol
        return base, True, None
    try:
        with open(os.path.join(rdir, "run.inputs.json")) as fh:
            inputs = json.load(fh)
        with open(os.path.join(rdir, "poll.inputs.json")) as fh:
            poll_inputs = json.load(fh)
    except (OSError, ValueError) as e:
        base["error"] = "cannot read the run's staged inputs: %s" % e
        return base, True, None
    remote_dir, cluster = str(inputs.get("remote_dir", "")), str(inputs.get("cluster", ""))
    if not REMOTE_DIR_RE.match(remote_dir) or not cluster.startswith("pw://"):
        base["error"] = ("the run's run.inputs.json has an unusable remote_dir %r or cluster %r; "
                         "nothing was run" % (remote_dir, cluster))
        return base, True, None
    if not str(poll_inputs.get("job_id", "")).strip():
        base["error"] = "the run has no scheduler job_id in poll.inputs.json; it was never submitted"
        return base, True, None
    try:
        if _site(target, "name") != target:
            raise Rejected("target %r (from the run id) is not in sites/sites.yaml" % target)
        directives = _site(target, "directives")
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None

    eval_id = allocate_dir(EVAL_AGENT, target)
    edir = os.path.join(RUNS, eval_id)
    for op in ("poll", "fetch", "validate"):
        shutil.copyfile(os.path.join(WORKFLOWS, op + ".yaml"), os.path.join(edir, op + ".yaml"))
    staged = {
        "poll": {"cluster": cluster, "scheduler": poll_inputs.get("scheduler") or inputs.get("scheduler"),
                 "job_id": str(poll_inputs["job_id"])},
        "fetch": {"cluster": cluster, "remote_dir": remote_dir},
        "validate": {"cluster": cluster, "remote_dir": remote_dir, "grid": str(inputs.get("grid")),
                     "tol": tol},
    }
    for op, data in staged.items():
        with open(os.path.join(edir, op + ".inputs.json"), "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")

    out = {"status": None, "evaluator_id": eval_id, "run_id": run_id, "run_agent": agent,
           "target": target, "grid": _maybe_int(inputs.get("grid")), "commit": inputs.get("commit"),
           "mpi_ranks": _maybe_int(inputs.get("mpi_ranks")), "mode": inputs.get("mode"),
           "job_id": staged["poll"]["job_id"], "l2_tolerance": tol, "directives": directives,
           "poll_workflow": None, "fetch_workflow": None, "validate_workflow": None,
           "record": None, "verdict": None, "integrity_flags": [], "checks": {},
           "overall": None, "error": None}
    try:
        _evaluate(eval_id, out)
    except Exception as e:  # pw missing, timeout...
        out.update(status="failed", overall="FAIL", error=str(e))
    os.makedirs(os.path.join(RESULTS, eval_id), exist_ok=True)
    with open(os.path.join(RESULTS, eval_id, "verdict.json"), "w") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    return out, out["status"] != "evaluated", eval_id


def _maybe_int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return v


def _evaluate(eval_id, out):
    """Precondition poll, fetch, validate and integrity checks; fills `out`."""
    state, raw, slug, error = _poll(eval_id, eval_id + "-poll")
    out["poll_workflow"] = slug
    if error or state != "DONE":
        out.update(status="not_ready" if not error else "failed", overall="NOT EVALUATED",
                   error=error or ("the job has not left the queue (state %s, %s); nothing was "
                                   "fetched or validated. Evaluate it later." % (state, raw)))
        return
    status, lout, slug, error = run_workflow(eval_id, "fetch", eval_id + "-fetch")
    out["fetch_workflow"] = slug
    record = _json_line(lout, "grid") or _json_line(lout, "ok")
    if error or status != "completed" or not record or "ranks" not in record:
        out.update(status="failed", overall="FAIL", record=record,
                   error=error or "fetch workflow %s ended %s without a run record%s"
                   % (slug, status, ": %s" % record["error"] if record and record.get("error") else ""))
        return
    out["record"] = record
    status, lout, slug, error = run_workflow(eval_id, "validate", eval_id + "-validate")
    out["validate_workflow"] = slug
    verdict = _json_line(lout, "ok")
    out["verdict"] = verdict
    if error or not verdict:
        # validate.py exits non-zero on a failed comparison, so the workflow
        # ends "error" yet still printed a verdict; only a missing verdict fails.
        out.update(status="failed", overall="FAIL",
                   error=error or "validate workflow %s ended %s without a verdict" % (slug, status))
        return
    with open(os.path.join(RUNS, out["run_id"], "run.inputs.json")) as fh:
        inputs = json.load(fh)
    flags, checks = _integrity(inputs, out["directives"], record)
    out.update(status="evaluated", integrity_flags=flags, checks=checks,
               overall="PASS" if verdict.get("ok") is True and not flags else "FAIL")


# --------------------------------------------------------------------------
# list_runs / compare_runs (compare-agents) — read-only joins over local files
# --------------------------------------------------------------------------
COMPARE_AGENT = "compare-agents"
EVAL_AGENTS = ("evaluator-mcp", "evaluator")  # longest first: prefixes overlap
MCP_SIBLING = {"site-runner-mcp": "site-runner", "evaluator-mcp": "evaluator"}
EXPECTED_DIFF = ("remote_dir",)  # each run has its own remote dir


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _split_id(dir_id, agents):
    m = re.match(r"^(.+)-(\d{6})$", dir_id)
    if not m:
        return None, None, None
    for agent in agents:
        if m.group(1).startswith(agent + "-"):
            return agent, m.group(1)[len(agent) + 1:], int(m.group(2))
    return None, None, None


def _sessions():
    """session_id -> ids whose agent-digest.json came from that session. One
    session (an evaluator, typically) can create several ids; its cost must be
    counted once."""
    out = {}
    for name in sorted(os.listdir(RUNS)) if os.path.isdir(RUNS) else []:
        d = _read_json(os.path.join(RUNS, name, "agent-digest.json"))
        if d and d.get("session_id"):
            out.setdefault(d["session_id"], set()).add(name)
    return out


def _cost(dir_id, sessions):
    """Session cost from runs/<id>/agent-digest.json, or None if there is none."""
    d = _read_json(os.path.join(RUNS, dir_id, "agent-digest.json"))
    if not d:
        return None
    calls = d.get("tool_calls") or []
    ids = sorted(sessions.get(d.get("session_id"), {dir_id}))
    tokens = (d.get("usage") or {}).get("total_tokens")
    return {"source": "hook digest", "session_id": d.get("session_id"), "models": d.get("models"),
            "turns": d.get("turns"), "total_tokens": tokens,
            "tool_calls": d.get("tool_call_count"), "failed_tool_calls": d.get("failed_tool_calls"),
            "malformed_tool_names": sum(1 for c in calls if "<|" in str(c.get("tool"))),
            "ids_created_by_session": ids,
            "tokens_per_id": (tokens // len(ids)) if isinstance(tokens, int) and ids else None}


def _run_facts(run_id, sessions, target_directives=None):
    agent, target, _ = _split_id(run_id, RUN_AGENTS)
    rdir = os.path.join(RUNS, run_id)
    inputs = _read_json(os.path.join(rdir, "run.inputs.json")) or {}
    poll = _read_json(os.path.join(rdir, "poll.inputs.json")) or {}
    facts = {"run_id": run_id, "agent": agent, "target": target, "grid": inputs.get("grid"),
             "commit": inputs.get("commit"), "mpi_ranks": inputs.get("mpi_ranks"),
             "job_id": str(poll.get("job_id") or "") or None, "cost": _cost(run_id, sessions)}
    if target_directives is not None:
        facts["directives_match_target"] = _same_directives(
            _b64_text(inputs.get("directives_b64")), target_directives)
    if agent == "site-runner-mcp":
        submit = _read_json(os.path.join(rdir, "submit.json")) or {}
        facts["workflow_run"] = submit.get("workflow_run")
        try:
            with open(os.path.join(rdir, "polls.jsonl")) as fh:
                polls = [json.loads(l) for l in fh if l.strip()]
        except (OSError, ValueError):
            polls = []
        facts["polls"] = {"source": "polls.jsonl", "count": len(polls),
                          "last_state": polls[-1].get("state") if polls else None}
    else:
        d = _read_json(os.path.join(rdir, "agent-digest.json")) or {}
        cmds = [str((c.get("args") or {}).get("command", "")) for c in d.get("tool_calls") or []]
        n = sum(1 for c in cmds if c.startswith("pw workflows run ") and "poll.yaml" in c)
        facts["polls"] = {"source": "hook digest" if d else "unavailable", "count": n if d else None,
                          "last_state": None}
    return facts


def _evaluations(sessions):
    """run_id -> evaluator agent -> list of evaluations, oldest first."""
    out = {}
    names = sorted(os.listdir(RESULTS)) if os.path.isdir(RESULTS) else []
    for name in names:
        agent, target, n = _split_id(name, EVAL_AGENTS)
        v = _read_json(os.path.join(RESULTS, name, "verdict.json")) if agent else None
        if not v or not v.get("run_id"):
            continue
        verdict, record = v.get("verdict") or {}, v.get("record") or {}
        if "integrity_flags" not in v:
            overall = "UNKNOWN (written before integrity checks existed)"
        elif agent == "evaluator-mcp":
            overall = v.get("overall")
        else:  # the evaluator's file has no overall; apply the same rule
            overall = "PASS" if verdict.get("ok") is True and not v["integrity_flags"] else "FAIL"
        out.setdefault(v["run_id"], {}).setdefault(agent, []).append({
            "evaluator_id": name, "n": n, "status": v.get("status"), "overall": overall,
            "integrity_flags": [str(f).split(":")[0].strip() for f in v.get("integrity_flags") or []],
            "verdict": verdict, "record": record, "cost": _cost(name, sessions)})
    for agents in out.values():
        for evs in agents.values():
            evs.sort(key=lambda e: e["n"])
    return out


def _eval_pair(run_id, evals):
    """Newest evaluation per evaluator for one run, and how the two compare."""
    by_agent = evals.get(run_id, {})
    pair = {"run_id": run_id}
    for agent in EVAL_AGENTS:
        evs = by_agent.get(agent) or []
        newest = dict(evs[-1]) if evs else None
        if newest:
            newest.pop("n")
            newest["superseded"] = [e["evaluator_id"] for e in evs[:-1]]
        pair[agent] = newest
    a, b = pair["evaluator-mcp"], pair["evaluator"]
    if a and b:
        pair["record_equal"] = a["record"] == b["record"]
        pair["verdict_equal"] = a["verdict"] == b["verdict"]
        pair["flags_only_mcp"] = sorted(set(a["integrity_flags"]) - set(b["integrity_flags"]))
        pair["flags_only_shell"] = sorted(set(b["integrity_flags"]) - set(a["integrity_flags"]))
        pair["overall_agree"] = a["overall"] == b["overall"]
    else:
        pair["missing"] = [ag for ag in EVAL_AGENTS if not pair[ag]]
    for agent in EVAL_AGENTS:  # bulky; the pair already compares them
        if pair[agent]:
            pair[agent].pop("record")
    return pair


def list_runs(args):
    """Returns (structured_result, is_error, None)."""
    target = str(args.get("target", "")).strip() or None
    sessions = _sessions()
    evals = _evaluations(sessions)
    runs = []
    for name in sorted(os.listdir(RUNS)) if os.path.isdir(RUNS) else []:
        agent, t, _ = _split_id(name, RUN_AGENTS)
        if not agent or (target and t != target) or not os.path.isfile(
                os.path.join(RUNS, name, "run.inputs.json")):
            continue
        f = _run_facts(name, sessions)
        f["evaluated_by"] = {ag: [e["evaluator_id"] for e in evs]
                             for ag, evs in evals.get(name, {}).items()}
        f.pop("cost")
        runs.append(f)
    return {"status": "listed", "target": target, "runs": runs,
            "next": "Pick a site-runner-mcp run and a site-runner run with the same target, grid "
                    "and commit, and call compare_runs with them."}, False, None


def compare_runs(args):
    """Returns (structured_result, is_error, None)."""
    mcp_id = str(args.get("mcp_run_id", "")).strip()
    shell_id = str(args.get("shell_run_id", "")).strip()
    base = {"status": "rejected", "report_dir": None, "error": None}
    ids = {"site-runner-mcp": mcp_id, "site-runner": shell_id}
    for want, rid in ids.items():
        agent, target, _ = _split_id(rid, RUN_AGENTS)
        if agent != want or not os.path.isfile(os.path.join(RUNS, rid, "run.inputs.json")):
            base["error"] = ("%r is not a %s run id with runs/<id>/run.inputs.json; call list_runs "
                             "to see the runs" % (rid, want))
            return base, True, None
    t_mcp, t_shell = _split_id(mcp_id, RUN_AGENTS)[1], _split_id(shell_id, RUN_AGENTS)[1]
    if t_mcp != t_shell:
        base["error"] = "the runs are on different targets (%s, %s); pair runs of one target" % (t_mcp, t_shell)
        return base, True, None
    try:
        target_directives = _site(t_mcp, "directives")
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None

    sessions = _sessions()
    a = _read_json(os.path.join(RUNS, mcp_id, "run.inputs.json"))
    b = _read_json(os.path.join(RUNS, shell_id, "run.inputs.json"))
    diffs, type_only, space_only = {}, {}, {}
    for k in sorted(set(a) | set(b)):
        if k in EXPECTED_DIFF or a.get(k) == b.get(k):
            continue
        if k == "directives_b64" and _same_directives(_b64_text(a.get(k)), _b64_text(b.get(k))):
            space_only[k] = "decoded blocks differ only in trailing whitespace"
        elif str(a.get(k)) == str(b.get(k)):
            type_only[k] = [a.get(k), b.get(k)]  # e.g. 4 vs "4": same value
        else:
            diffs[k] = ["<%d chars>" % len(str(a.get(k))) if k == "directives_b64" else a.get(k),
                        "<%d chars>" % len(str(b.get(k))) if k == "directives_b64" else b.get(k)]
    evals = _evaluations(sessions)
    report_dir = allocate_dir(COMPARE_AGENT, t_mcp, base=RESULTS)
    out = {
        "status": "compared", "target": t_mcp, "grid": a.get("grid"),
        "report_dir": "results/%s" % report_dir,
        "comparable": a.get("commit") == b.get("commit") and str(a.get("grid")) == str(b.get("grid")),
        "parity": {"ok": not diffs, "differences": diffs, "type_only_differences": type_only,
                   "whitespace_only_differences": space_only,
                   "expected_differences": list(EXPECTED_DIFF)},
        "runs": {"site-runner-mcp": _run_facts(mcp_id, sessions, target_directives),
                 "site-runner": _run_facts(shell_id, sessions, target_directives)},
        "evaluations": [_eval_pair(mcp_id, evals), _eval_pair(shell_id, evals)],
        "intentional_differences": [
            "run_heat rejects grid < mpi_ranks before submitting; site-runner does not check",
            "site-runner-mcp polls through poll_heat (server-side spacing); site-runner through Bash",
            "evaluate_run confirms the job left the queue before fetching, and also checks "
            "convergence and directives parity"],
        "next": "report.md and comparison.json are written in report_dir. Read the facts, then "
                "call write_summary once with report_dir and a short summary.",
    }
    with open(os.path.join(RESULTS, report_dir, "comparison.json"), "w") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(RESULTS, report_dir, "report.md"), "w") as fh:
        fh.write(_render_report(out))
    return out, False, None


SUMMARY_MARK = "<!-- summary: compare-agents fills this in with write_summary -->"
SUMMARY_MAX = 4000


def _cell(v):
    if v is None or v == "" or v == []:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "NO"
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    return str(v).replace("|", "/").replace("\n", " ")


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _render_report(c):
    """Deterministic report.md: every table comes from `c` (= comparison.json)."""
    m, s = c["runs"]["site-runner-mcp"], c["runs"]["site-runner"]

    def cost(r, k):
        return (r.get("cost") or {}).get(k) if r.get("cost") else "no digest"

    out = ["# Agent comparison: %s, grid %s" % (c["target"], c["grid"]), "",
           "Tables generated by `compare_runs` (heat server %s) at %s from "
           "`comparison.json` in this directory; the summary at the end is the "
           "compare-agents model's." % (SERVER_VERSION, _now()[:19] + "Z"), "",
           "## Run pair: site-runner-mcp vs site-runner", "",
           _table(["", "site-runner-mcp", "site-runner", "source"], [
               ["run id", m["run_id"], s["run_id"], "artifact"],
               ["commit", (m["commit"] or "")[:12], (s["commit"] or "")[:12], "run.inputs.json"],
               ["scheduler job id", m["job_id"], s["job_id"], "poll.inputs.json"],
               ["directives match target", m.get("directives_match_target"),
                s.get("directives_match_target"), "run.inputs.json vs sites.yaml"],
               ["polls", m["polls"]["count"], s["polls"]["count"],
                "%s / %s" % (m["polls"]["source"], s["polls"]["source"])],
               ["last poll state", m["polls"]["last_state"], s["polls"]["last_state"] or "not recorded",
                "polls.jsonl"],
               ["turns", cost(m, "turns"), cost(s, "turns"), "hook digest"],
               ["tokens", cost(m, "total_tokens"), cost(s, "total_tokens"), "hook digest"],
               ["tool calls (failed)", "%s (%s)" % (cost(m, "tool_calls"), cost(m, "failed_tool_calls")),
                "%s (%s)" % (cost(s, "tool_calls"), cost(s, "failed_tool_calls")), "hook digest"],
               ["malformed tool names", cost(m, "malformed_tool_names"), cost(s, "malformed_tool_names"),
                "hook digest"],
               ["model", cost(m, "models"), cost(s, "models"), "hook digest"],
           ]), ""]
    p = c["parity"]
    out.append("**Comparable** (same commit and grid): %s. **Input parity**: %s."
               % (_cell(c["comparable"]), "ok" if p["ok"] else "FAILED"))
    if p["differences"]:
        out.append("Differing inputs: %s." % ", ".join(
            "`%s` (%s vs %s)" % (k, _cell(v[0]), _cell(v[1])) for k, v in sorted(p["differences"].items())))
    if p["type_only_differences"]:
        out.append("Same value, different JSON type: %s." % ", ".join(
            "`%s` (%r vs %r)" % (k, v[0], v[1]) for k, v in sorted(p["type_only_differences"].items())))
    if p.get("whitespace_only_differences"):
        out.append("Same text, different trailing whitespace: %s." % ", ".join(
            "`%s`" % k for k in sorted(p["whitespace_only_differences"])))
    out.append("Expected to differ: %s." % ", ".join("`%s`" % k for k in p["expected_differences"]))
    out += ["", "## Evaluator pairs: evaluator-mcp vs evaluator", ""]
    for e in c["evaluations"]:
        a, b = e.get("evaluator-mcp"), e.get("evaluator")

        def ev(x, k):
            return x.get(k) if x else "not evaluated"

        def ecost(x, k):
            if not x:
                return "not evaluated"
            return (x.get("cost") or {}).get(k) if x.get("cost") else "no digest"

        def nids(x):
            ids = ecost(x, "ids_created_by_session")
            return len(ids) if isinstance(ids, list) else ids

        out += ["### Run `%s`" % e["run_id"], "",
                _table(["", "evaluator-mcp", "evaluator", "source"], [
                    ["evaluator id (newest)", ev(a, "evaluator_id"), ev(b, "evaluator_id"), "results/"],
                    ["superseded", ev(a, "superseded"), ev(b, "superseded"), "results/"],
                    ["overall", ev(a, "overall"), ev(b, "overall"), "verdict.json"],
                    ["integrity flags", ev(a, "integrity_flags") or "none",
                     ev(b, "integrity_flags") or "none", "verdict.json"],
                    ["relative L2", (a or {}).get("verdict", {}).get("relative_l2") if a else "not evaluated",
                     (b or {}).get("verdict", {}).get("relative_l2") if b else "not evaluated", "verdict.json"],
                    ["tokens for this run", ecost(a, "tokens_per_id"), ecost(b, "tokens_per_id"),
                     "hook digest, session / runs evaluated"],
                    ["session tokens", ecost(a, "total_tokens"), ecost(b, "total_tokens"), "hook digest"],
                    ["session turns", ecost(a, "turns"), ecost(b, "turns"), "hook digest"],
                    ["runs evaluated in session", nids(a), nids(b), "hook digest"],
                ]), ""]
        if a and b:
            out.append("Record equal: %s. Verdict equal: %s. Overall agrees: %s. Flags only "
                       "evaluator-mcp raised: %s. Flags only the evaluator raised: %s."
                       % (_cell(e["record_equal"]), _cell(e["verdict_equal"]), _cell(e["overall_agree"]),
                          _cell(e["flags_only_mcp"]) if e["flags_only_mcp"] else "none",
                          _cell(e["flags_only_shell"]) if e["flags_only_shell"] else "none"))
        else:
            out.append("Not paired: %s has not evaluated this run." % " and ".join(e["missing"]))
        out.append("")
    out += ["## Intentional differences", ""] + ["- " + d for d in c["intentional_differences"]]
    out += ["", "## Summary", "", SUMMARY_MARK, ""]
    return "\n".join(out)


def write_summary(args):
    """Fill the summary placeholder of a report compare_runs wrote. Returns
    (structured_result, is_error, None)."""
    rel = str(args.get("report_dir", "")).strip().rstrip("/")
    summary = str(args.get("summary", "")).strip()
    base = {"status": "rejected", "report_dir": rel, "error": None}
    name = rel[len("results/"):] if rel.startswith("results/") else rel
    if not re.match(r"^%s-[A-Za-z0-9._-]+-\d{6}$" % re.escape(COMPARE_AGENT), name):
        base["error"] = "report_dir must be the report_dir compare_runs returned, e.g. results/compare-agents-gce-cpu-000001"
        return base, True, None
    if not summary or len(summary) > SUMMARY_MAX:
        base["error"] = "summary must be non-empty plain markdown of at most %d characters" % SUMMARY_MAX
        return base, True, None
    path = os.path.join(RESULTS, name, "report.md")
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        base["error"] = "no report.md in %s; call compare_runs first" % rel
        return base, True, None
    if text.count(SUMMARY_MARK) != 1:
        base["error"] = "this report already has a summary; it is written once"
        return base, True, None
    with open(path, "w") as fh:
        fh.write(text.replace(SUMMARY_MARK, summary))
    return {"status": "written", "report_dir": rel, "report": rel + "/report.md", "error": None}, False, None


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
                "instructions": _instructions()}, None
    if method == "server/discover":
        return done({"supportedVersions": MODERN_VERSIONS + LEGACY_VERSIONS,
                     "capabilities": {"tools": {}},
                     "_meta": {"io.modelcontextprotocol/serverInfo":
                               {"name": SERVER_NAME, "version": SERVER_VERSION}},
                     "instructions": _instructions()}), None
    if method == "ping":
        return done({}), None
    if method == "tools/list":
        return done({"tools": TOOLS}), None
    if method == "tools/call":
        name = params.get("name")
        served = [t["name"] for t in TOOLS]
        tool = {"run_heat": run_heat, "poll_heat": poll_heat, "evaluate_run": evaluate_run,
                "list_runs": list_runs, "compare_runs": compare_runs,
                "write_summary": write_summary}.get(name) if name in served else None
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


def evaluate_only(run_id):
    """CLI debug: run evaluate_run once without MCP (it DOES run the workflows)."""
    result, is_error, _ = evaluate_run({"run_id": run_id})
    print(json.dumps(result, indent=2))
    return 1 if is_error else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "--stage-only":
        sys.exit(stage_only(argv[1:]))
    if len(argv) == 2 and argv[0] == "--evaluate":
        sys.exit(evaluate_only(argv[1]))
    if len(argv) in (1, 2) and argv[0] == "--list":
        print(json.dumps(list_runs({"target": argv[1]} if len(argv) == 2 else {})[0], indent=2))
        sys.exit(0)
    if len(argv) == 3 and argv[0] == "--compare":
        res, err, _ = compare_runs({"mcp_run_id": argv[1], "shell_run_id": argv[2]})
        print(json.dumps(res, indent=2))
        sys.exit(1 if err else 0)
    if len(argv) == 2 and argv[0] == "--tools":
        names = [n.strip() for n in argv[1].split(",") if n.strip()]
        unknown = [n for n in names if n not in ALL_TOOLS]
        if unknown or not names:
            sys.stderr.write("unknown tool(s) %s; choose from %s\n" % (unknown, ", ".join(ALL_TOOLS)))
            sys.exit(2)
        TOOLS = [ALL_TOOLS[n] for n in names]
    elif argv:
        sys.stderr.write("usage: heat_server.py [--tools a,b] | --stage-only <target> <grid> "
                         "[tol] [max_iter] | --evaluate <run_id> | --list [target] | "
                         "--compare <mcp_run_id> <shell_run_id>\n")
        sys.exit(2)
    sys.exit(serve())
