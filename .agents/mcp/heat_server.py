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
  orchestrator-mcp (--tools list_runs,load_campaign):
  load_campaign([campaign])  read-only: reads campaigns/<file>, checks its targets
                          against sites.yaml and returns the target x grid run list
                          (at most MAX_CAMPAIGN_RUNS) with tol and max_iter; with no
                          campaign, lists the campaign files. list_runs is shared
                          with compare-agents.
  site-prep-mcp (--tools copy_buildcache,run_spack,spack_status), not part of a
  campaign; site preparation is run per target when a human asks:
  copy_buildcache(target, uri)  runs workflows/buildcache.yaml: on the cluster,
                          `pw buckets cp` a pw://<user>/<bucket>/<name>.tar.gz into
                          $HOME and `tar -xzf` it. Waits for it (a few minutes).
  run_spack(target)       STARTS workflows/spack.yaml, which calls spack-builder
                          (parallelworks/workflows@canary) as a subworkflow, with
                          scheduler, partition, directives and build_gpu from the
                          target. Returns at start; a build can take hours.
  spack_status(run_id)    read-only: `pw workflows runs view` of that build, waiting
                          a few minutes for it to finish. Never starts anything.
  ray-launcher-mcp (--tools launch_ray,ray_status,stop_ray), run when a human asks:
  launch_ray([walltime])  STARTS workflows/ray.yaml (a local copy of ray-cluster's
                          general.yaml): Ray head + dashboard on the PW workspace, one
                          worker row per cpu target (see ray_worker_row). Waits up to
                          ~9 min for the endpoint; the cluster then stays up.
  ray_status([run_id])    read-only: the cluster's workflow state and endpoint.
  stop_ray(run_id)        `pw endpoints delete ray-cluster-<slug>` (ray-cluster's
                          documented teardown), then waits for the endpoint to go.
  ray-runner-mcp (--tools new_canvas,render_tiles,render_status,ray_status):
  new_canvas(tiles, ...)  runs workflows/fractal.yaml action=init: an EMPTY N x N
                          canvas of app/fractal on the cluster's dashboard.
  render_tiles(canvas_id, count|all|rect)  STARTS fractal.yaml action=add (a Ray
                          Job on the head); returns once it is submitted.
  render_status(run_id)   read-only: waits a few minutes for a render's result line,
                          or (canvas id) runs action=status for the canvas progress.

Launch (stdio transport; this is what each agent's mcpServers entry runs):
    python3 -I .agents/mcp/heat_server.py                        # site-runner-mcp
    python3 -I .agents/mcp/heat_server.py --tools evaluate_run   # evaluator-mcp
    python3 -I .agents/mcp/heat_server.py --tools list_runs,load_campaign   # orchestrator-mcp
    python3 -I .agents/mcp/heat_server.py --tools copy_buildcache,run_spack,spack_status   # site-prep-mcp
    python3 -I .agents/mcp/heat_server.py --tools launch_ray,ray_status,stop_ray   # ray-launcher-mcp
    python3 -I .agents/mcp/heat_server.py --tools new_canvas,render_tiles,render_status,ray_status   # ray-runner-mcp
Debug (no MCP):
    python3 -I .agents/mcp/heat_server.py --stage-only <target> <grid> [tol] [max_iter]
        stage only, no submit; prints the staged inputs
    python3 -I .agents/mcp/heat_server.py --evaluate <run_id>
        runs evaluate_run once (this DOES run the poll/fetch/validate workflows)
    python3 -I .agents/mcp/heat_server.py --spack-inputs <target>
        prints the spack.yaml inputs run_spack would stage; stages and runs nothing
    python3 -I .agents/mcp/heat_server.py --ray-inputs [walltime]
        prints the ray.yaml worker rows launch_ray would stage; stages and runs nothing

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
(plus read-only `pw workflows runs view|logs` to read a run's outcome, read-only
`pw endpoints list`, and `pw endpoints delete ray-cluster-*` for stop_ray) — see
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
SERVER_VERSION = "0.10.0"
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
CAMPAIGNS = os.path.join(REPO, "campaigns")
# load_campaign refuses a campaign that expands to more runs than this, so one
# orchestrator call cannot fan out an unbounded number of cluster jobs.
MAX_CAMPAIGN_RUNS = 21

DEFAULT_TOL = "1e-3"
DEFAULT_L2_TOLERANCE = "1e-6"  # validate.yaml's pass tolerance; NOT the solver's tol
DEFAULT_MAX_ITER = 50000
SUBMIT_WAIT_SECONDS = 540  # stays under the agent's 10m MCP timeout
POLL_BUDGET_SECONDS = 420  # one poll_heat call; leaves headroom under 10m
POLL_INTERVAL_SECONDS = 30
RUN_ID_RE = re.compile(r"^%s-[A-Za-z0-9._-]+-\d{6}$" % re.escape(AGENT_NAME))
TERMINAL = ("completed", "error", "canceled", "cancelled", "failed")

# Model-facing text names tools as pw code exposes them: mcp__<server>__<tool>,
# with the server registered as "heat" in both MCP agents' mcpServers. A small
# model called the bare name (evaluate_run) when its prompt used it.
RUN_INSTRUCTIONS = (
    "Call mcp__heat__run_heat once with a target name from sites/sites.yaml and a grid size; it "
    "stages, builds and submits the job and returns a run_id. Then call mcp__heat__poll_heat with "
    "that run_id until it returns done=true."
)
EVAL_INSTRUCTIONS = (
    "Call mcp__heat__evaluate_run once per run id a run agent reported. It fetches, validates and "
    "audits the run and saves its verdict.json; overall PASS needs a passing validation "
    "and no integrity_flags."
)


COMPARE_INSTRUCTIONS = (
    "Call list_runs to see the runs, then compare_runs once per pair of a site-runner-mcp "
    "run and a site-runner run; it writes comparison.json and report.md. Then call "
    "write_summary once with a short summary."
)


CAMPAIGN_INSTRUCTIONS = (
    "Call mcp__heat__load_campaign once with the campaign file name; dispatch each entry of its "
    "`runs` to the site-runner-mcp agent, passing target, grid, tol and max_iter as given."
)


PREP_INSTRUCTIONS = (
    "Site preparation, one target at a time. To copy a build cache, call "
    "mcp__heat__copy_buildcache once with the target and the bucket uri. To build the Spack "
    "stack, call mcp__heat__run_spack once with the target, then mcp__heat__spack_status with the "
    "run_id it returned. mcp__heat__run_spack always starts a NEW build; only "
    "mcp__heat__spack_status checks one."
)


def _instructions():
    served = [t["name"] for t in TOOLS]
    if "load_campaign" in served:
        # orchestrator-mcp: its server also serves the subagents' tools (pw code only
        # lets a subagent use tools its parent's session has), but the orchestrator
        # itself must delegate, so it never gets the run/eval instructions.
        return CAMPAIGN_INSTRUCTIONS + (
            " mcp__heat__run_heat, mcp__heat__poll_heat and mcp__heat__evaluate_run are served "
            "only so your site-runner-mcp and evaluator-mcp subagents can use them; never call "
            "them yourself.")
    return " ".join(([RUN_INSTRUCTIONS] if "run_heat" in served else [])
                    + ([EVAL_INSTRUCTIONS] if "evaluate_run" in served else [])
                    + ([COMPARE_INSTRUCTIONS] if "compare_runs" in served else [])
                    + ([CAMPAIGN_INSTRUCTIONS] if "load_campaign" in served else [])
                    + ([PREP_INSTRUCTIONS] if set(served) & {"copy_buildcache", "run_spack",
                                                             "spack_status"} else [])
                    + ([RAY_LAUNCH_INSTRUCTIONS] if "launch_ray" in served else [])
                    + ([RAY_RUN_INSTRUCTIONS] if "render_tiles" in served else []))

RUN_TOOL = {
    "name": "run_heat",
    "title": "Run the heat solver on a target",
    "description": (
        "Submit the heat job for ONE target (a name from sites/sites.yaml) at grid "
        "size N. Does everything for you: clones the repo on the cluster, builds, "
        "and submits. Returns run_id and job_id. Returns right after submit; it does "
        "NOT wait for the job — call mcp__heat__poll_heat with the run_id next. "
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
        "Check the scheduler state of a job submitted by mcp__heat__run_heat. Pass the run_id "
        "that mcp__heat__run_heat returned. Polls for up to a few minutes and returns as soon "
        "as the job has left the queue (state DONE, done true). If done is false, "
        "call mcp__heat__poll_heat again with the same run_id. done=true means only that the "
        "job left the queue, NOT that it succeeded; the evaluator checks the result."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "The run_id returned by mcp__heat__run_heat, e.g. site-runner-mcp-gce-cpu-000001"},
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
            "next": {"type": ["string", "null"]},
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

CAMPAIGN_TOOL = {
    "name": "load_campaign",
    "title": "Load a campaign and expand it into runs",
    "description": (
        "Read-only. Reads ONE campaign file from campaigns/ and returns its solver "
        "settings (tol, max_iter), grid sizes and targets, each target checked against "
        "sites/sites.yaml (mode, scheduler, mpi_ranks), and `runs`: the target x grid "
        "list to dispatch, one site-runner-mcp task per entry. `skipped` lists unknown "
        "targets and grids smaller than a target's mpi_ranks, with the reason. Call "
        "with no campaign to list the campaign files."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "campaign": {"type": "string",
                         "description": "A campaign file in campaigns/, e.g. grid-sweep.yaml (optional)"},
        },
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["loaded", "listed", "rejected"]},
            "campaigns": {"type": "array"},
            "solver": {"type": "object"},
            "grids": {"type": "array"},
            "targets": {"type": "array"},
            "runs": {"type": "array"},
            "skipped": {"type": "array"},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

COPY_CACHE_TOOL = {
    "name": "copy_buildcache",
    "title": "Copy a Spack build cache from a bucket to a target's cluster",
    "description": (
        "Copy ONE Spack build cache archive from a PW bucket into $HOME on the cluster of ONE "
        "target (a name from sites/sites.yaml) and unpack it there with tar -xzf, through the "
        "buildcache.yaml workflow. uri must be pw://<user>/<bucket>/<name>.tar.gz. Waits for "
        "the copy to finish (a few minutes) and returns status copied or failed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target name, e.g. gce-cpu"},
            "uri": {"type": "string",
                    "description": "Bucket object, e.g. pw://myuser/mybucket/spack-buildcache.tar.gz"},
        },
        "required": ["target", "uri"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["copied", "rejected", "failed"]},
            "run_id": {"type": ["string", "null"]},
            "target": {"type": "string"},
            "workflow_run": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "target"],
    },
}

RUN_SPACK_TOOL = {
    "name": "run_spack",
    "title": "Start a Spack stack build on a target's cluster",
    "description": (
        "START the Spack stack build (the spack-builder workflow, through spack.yaml) on the "
        "cluster of ONE target (a name from sites/sites.yaml). Scheduler, partition, "
        "directives and GPU build come from the target. Returns run_id right after the "
        "build starts; it does NOT wait. A build takes minutes with a matching build cache "
        "and hours without one. Call mcp__heat__spack_status with the run_id next. Every "
        "call starts a NEW build: never call it to check on a build."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Target name, e.g. gce-cpu"},
        },
        "required": ["target"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["started", "rejected", "failed"]},
            "run_id": {"type": ["string", "null"]},
            "target": {"type": "string"},
            "workflow_run": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "target"],
    },
}

SPACK_STATUS_TOOL = {
    "name": "spack_status",
    "title": "Check a Spack stack build",
    "description": (
        "Read-only: report the state of a build that mcp__heat__run_spack started. Pass the "
        "run_id it returned. Waits up to a few minutes and returns as soon as the build has "
        "finished (done true; succeeded says whether it worked). If done is false the "
        "build is still running. Never starts anything."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string",
                       "description": "The run_id mcp__heat__run_spack returned, e.g. site-prep-mcp-gce-cpu-000002"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["checked", "rejected", "failed"]},
            "run_id": {"type": "string"},
            "workflow_run": {"type": ["string", "null"]},
            "state": {"type": ["string", "null"]},
            "done": {"type": "boolean"},
            "succeeded": {"type": "boolean"},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "run_id", "done"],
    },
}

ALL_TOOLS = {"run_heat": RUN_TOOL, "poll_heat": POLL_TOOL, "evaluate_run": EVAL_TOOL,
             "list_runs": LIST_TOOL, "compare_runs": COMPARE_TOOL, "write_summary": SUMMARY_TOOL,
             "load_campaign": CAMPAIGN_TOOL, "copy_buildcache": COPY_CACHE_TOOL,
             "run_spack": RUN_SPACK_TOOL, "spack_status": SPACK_STATUS_TOOL}
# Which tools this process serves, from --tools (see __main__). The default keeps
# the server exactly as site-runner-mcp has always launched it.
TOOLS = [RUN_TOOL, POLL_TOOL]


# --------------------------------------------------------------------------
# Tracing (stderr + a per-process JSONL file; never stdout)
# --------------------------------------------------------------------------
def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Trace:
    def __init__(self):
        self.path = None
        try:
            d = os.path.join(RUNS, "_mcp")
            os.makedirs(d, exist_ok=True)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    `workflows run` (the submission), read-only `workflows runs view|logs` and
    `endpoints list`, and `endpoints delete ray-cluster-<slug>` (stop_ray only: the
    documented ray-cluster teardown). Anything else — pw ssh, scp, other verbs — is
    refused by construction."""
    allowed = argv[:2] == ["workflows", "run"] or (
        argv[:2] == ["workflows", "runs"] and len(argv) > 2 and argv[2] in ("view", "logs")
    ) or argv[:2] == ["endpoints", "list"] or (
        argv[:2] == ["endpoints", "delete"] and len(argv) == 3 and bool(ENDPOINT_RE.match(argv[2]))
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
                   "review the change and re-copy it before mcp__heat__run_heat can resolve targets.")


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
         ("ssh_name", "scheduler", "mode", "mpi_ranks", "mpi_launch", "env_load_joined", "directives",
          "timeout")}
    t["name"] = target
    if t["scheduler"] == "none":
        # Same checks run.yaml makes, made before anything is staged or launched.
        lines = t["directives"].rstrip("\n").split("\n")
        if not t["directives"].strip() or any(not ln.startswith("#") for ln in lines):
            raise Rejected("target %r has scheduler none, so its directives must be '#' comment "
                           "lines only (e.g. '# NO SCHEDULER - directive placeholder'). Nothing "
                           "was submitted." % target)
        if t["timeout"] and not re.match(r"^\d+(:\d+){0,2}$", t["timeout"]):
            raise Rejected("target %r has an invalid timeout %r; use seconds or [[HH:]MM:]SS. "
                           "Nothing was submitted." % (target, t["timeout"]))
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
    if t["scheduler"] == "none" and t["timeout"]:
        inputs["timeout"] = t["timeout"]
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
    # so wait for the workflow — not the scheduler job — to finish. With scheduler
    # none the workflow runs the job itself in the foreground, so instead stop
    # waiting once its log shows the SUBMITTED line (the job was launched).
    if inputs.get("scheduler") == "none":
        view, launched = _wait_submitted(slug, SUBMIT_WAIT_SECONDS)
        if launched:
            record.update(workflow_status=view.get("status"), status="submitted",
                          job_id=launched)
            return record
    else:
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
        result["next"] = "Call mcp__heat__poll_heat with run_id %s until it returns done=true." % run_id
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


def _wait_submitted(slug, budget):
    """For scheduler none: wait until the run's log shows the SUBMITTED line
    (return its view and the job_id) or the run ends without one (job_id None)."""
    deadline = time.time() + budget
    while True:
        vrc, vout, _ = _pw(["workflows", "runs", "view", slug, "-o", "json"], timeout=60)
        try:
            view = json.loads(vout) if vrc == 0 else {}
        except ValueError:
            view = {}
        _, lout, _ = _pw(["workflows", "runs", "logs", "--job", "run", slug], timeout=60)
        m = re.search(r"SUBMITTED scheduler=\S+ job_id=(\S+)", lout)
        if m:
            return view, m.group(1)
        if view.get("status") in TERMINAL or time.time() > deadline:
            return view, None
        time.sleep(3)


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
        base["error"] = ("unknown run_id %r: pass the run_id that mcp__heat__run_heat returned for a "
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
        base["next"] = ("The job is still %s (%s). Call mcp__heat__poll_heat again with run_id %s."
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
    # scheduler none runs on the one node the workflow reached: expect 1 host.
    nodes = 1 if inputs.get("scheduler") == "none" else _nodes_requested(directives)
    hosts = record.get("distinct_hosts")
    checks["nodes"] = {"expected": nodes, "actual": hosts,
                       "ok": None if nodes is None else hosts == nodes}
    if nodes is not None and hosts != nodes:
        flags.append("MULTI_NODE_MISMATCH: used %s hosts, %s" % (
            hosts, "scheduler none runs on 1 node" if inputs.get("scheduler") == "none"
            else "directives request %s nodes" % nodes))
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
    """Returns (structured_result, is_error, eval_id_or_None). Adds `next`, the
    instruction the model sees with the result (not saved in verdict.json):
    small models ignored the prompt's stop rules and went hunting for runs and
    files with tools they do not have, so every result says plainly to stop."""
    result, is_error, eval_id = _evaluate_run(args)
    if result["status"] == "rejected" and (result.get("error") or "").startswith("unknown run_id"):
        result["next"] = ("This run id does not exist as given; nothing was evaluated. Report the "
                          "id exactly as you received it, with this error, and stop working on it: "
                          "do not call any tool for it again, and do not search for, correct, or "
                          "re-pad the id. A human checks it.")
    elif result["status"] == "rejected":
        result["next"] = ("Nothing was evaluated. Report this error verbatim for this run id and "
                          "do not call any tool for it again.")
    else:
        result["next"] = ("This run id has its one result; never evaluate it again. When every run "
                          "id you were given has a result, make no more tool calls of any kind and "
                          "write your report from these results.")
    return result, is_error, eval_id


def _evaluate_run(args):
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
        base["error"] = ("the run has no scheduler job_id in poll.inputs.json (the run agent never recorded one), "
                         "so the job cannot be confirmed out of the queue; nothing was evaluated")
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


# --------------------------------------------------------------------------
# load_campaign (orchestrator-mcp) — read-only expansion of campaigns/<file>
# --------------------------------------------------------------------------
def _campaign_files():
    if not os.path.isdir(CAMPAIGNS):
        return []
    return sorted(n for n in os.listdir(CAMPAIGNS)
                  if n.endswith((".yaml", ".yml")) and os.path.isfile(os.path.join(CAMPAIGNS, n)))


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    return v


def _parse_campaign_fallback(text):
    """Minimal parser for the documented campaign shape (nested maps, `- item`
    lists, inline [a, b] lists, scalars as strings), used only when PyYAML is
    missing. Not a general YAML parser."""
    root = {}
    stack = [(-1, root)]  # (indent, container)
    pending = None        # (indent, parent dict, key) of a `key:` with no value yet
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        if s.startswith("- "):
            if pending and indent >= pending[0]:
                lst = []
                pending[1][pending[2]] = lst
                stack.append((pending[0], lst))
                pending = None
            if isinstance(stack[-1][1], list):
                stack[-1][1].append(_scalar(s[2:]))
            continue
        if pending:
            child = {}
            pending[1][pending[2]] = child
            stack.append((pending[0], child))
            pending = None
        if ":" not in s or not isinstance(stack[-1][1], dict):
            continue
        k, v = s.split(":", 1)
        if v.strip():
            stack[-1][1][k.strip()] = _scalar(v)
        else:
            pending = (indent, stack[-1][1], k.strip())
    return root


def _read_campaign(path):
    with open(path) as fh:
        text = fh.read()
    try:
        import yaml  # system site-packages; the server itself stays stdlib-only
        return yaml.safe_load(text)
    except ImportError:
        return _parse_campaign_fallback(text)


def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def load_campaign(args):
    """Returns (structured_result, is_error, None). Reads only campaigns/ and
    sites/sites.yaml (through the protected _site_query.py); writes nothing."""
    name = args.get("campaign")
    if name is None or (isinstance(name, str) and not name.strip()):
        return {"status": "listed", "campaigns": _campaign_files(),
                "next": "Call load_campaign again with one of these file names."}, False, None
    try:
        if not isinstance(name, str):
            raise Rejected("campaign must be a file name such as grid-sweep.yaml, got %r" % (name,))
        name = name.strip()
        while name.startswith("./"):
            name = name[2:]
        if name.startswith("campaigns/"):
            name = name[len("campaigns/"):]
        if not name.endswith((".yaml", ".yml")):
            name += ".yaml"
        path = os.path.realpath(os.path.join(CAMPAIGNS, name))
        if os.path.dirname(path) != os.path.realpath(CAMPAIGNS) or not os.path.isfile(path):
            raise Rejected("no campaign %r in campaigns/; available: %s"
                           % (args.get("campaign"), ", ".join(_campaign_files()) or "(none)"))
        try:
            data = _read_campaign(path)
        except Exception as e:  # yaml.YAMLError, OSError
            raise Rejected("cannot parse campaigns/%s: %s" % (name, e))
        if not isinstance(data, dict):
            raise Rejected("campaigns/%s is not a mapping" % name)
        solver = data.get("solver") or {}
        axes = data.get("axes") or {}
        if not isinstance(solver, dict) or not isinstance(axes, dict):
            raise Rejected("campaigns/%s: `solver` and `axes` must be mappings" % name)

        defaults_used = []
        if "tol" in solver:
            tol = str(solver["tol"]).strip()
        else:
            tol, defaults_used = DEFAULT_TOL, defaults_used + ["tol"]
        try:
            float(tol)
        except ValueError:
            raise Rejected("solver.tol must be a number such as 1e-3, got %r" % tol)
        if "max_iter" in solver:
            max_iter = _as_int("solver.max_iter", solver["max_iter"])
        else:
            max_iter, defaults_used = DEFAULT_MAX_ITER, defaults_used + ["max_iter"]
        if max_iter < 1:
            raise Rejected("solver.max_iter must be >= 1")

        grids = []
        for g in _as_list(axes.get("grid")):
            g = _as_int("axes.grid entry", g)
            if g < 1:
                raise Rejected("axes.grid entries must be >= 1, got %d" % g)
            if g not in grids:
                grids.append(g)
        names = []
        for t in _as_list(axes.get("target")):
            if not isinstance(t, str) or not t.strip():
                raise Rejected("axes.target entries must be target names, got %r" % (t,))
            if t.strip() not in names:
                names.append(t.strip())
        if not grids or not names:
            raise Rejected("campaigns/%s needs at least one axes.grid and one axes.target" % name)

        targets, runs, skipped = [], [], []
        for t in names:
            if _site(t, "name") != t:  # also runs the _site_query.py parity check
                targets.append({"name": t, "known": False})
                skipped.append({"target": t, "grid": None,
                                "reason": "target is not in sites/sites.yaml"})
                continue
            info = {"name": t, "known": True, "mode": _site(t, "mode"),
                    "scheduler": _site(t, "scheduler"), "mpi_ranks": _maybe_int(_site(t, "mpi_ranks"))}
            targets.append(info)
            for g in grids:
                ranks = info["mpi_ranks"]
                if isinstance(ranks, int) and g < ranks:
                    skipped.append({"target": t, "grid": g,
                                    "reason": "grid %d < mpi_ranks %d; run_heat would reject it"
                                              % (g, ranks)})
                    continue
                runs.append({"target": t, "grid": g, "tol": tol, "max_iter": max_iter})
        if len(runs) > MAX_CAMPAIGN_RUNS:
            raise Rejected("campaigns/%s expands to %d runs, more than the limit of %d; a human "
                           "must split it. Nothing was dispatched."
                           % (name, len(runs), MAX_CAMPAIGN_RUNS))
    except Rejected as e:
        return {"status": "rejected", "error": str(e)}, True, None
    return {"status": "loaded", "campaign": "campaigns/" + name,
            "name": data.get("name"), "description": data.get("description"),
            "solver": {"tol": tol, "max_iter": max_iter, "defaults_used": defaults_used},
            "grids": grids, "targets": targets, "runs": runs, "skipped": skipped,
            "next": "Every entry of `runs` is a NEW job: dispatch ALL of them now to the "
                    "site-runner-mcp agent (one Task per entry) with its target, grid, tol and "
                    "max_iter, even if earlier runs of the same target and grid exist. Report "
                    "every `skipped` entry with its reason."}, False, None


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
# copy_buildcache / run_spack / spack_status (site-prep-mcp)
# --------------------------------------------------------------------------
PREP_AGENT = "site-prep-mcp"  # id prefix: site-prep-mcp-<target>-<NNNNNN>
PREP_ID_RE = re.compile(r"^%s-[A-Za-z0-9._-]+-\d{6}$" % re.escape(PREP_AGENT))
# pw://<user>/<bucket>/[<dir>/...]<name>.tar.gz; the charset also keeps the value
# safe inside the single quotes buildcache.yaml puts it in.
BUCKET_URI_RE = re.compile(r"^pw://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.tar\.gz$")
COPY_BUDGET_SECONDS = 540      # stays under the agent's 10m MCP timeout
SPACK_START_SECONDS = 20       # long enough to catch a run rejected at start
SPACK_STATUS_BUDGET_SECONDS = 420
SPACK_STATUS_INTERVAL_SECONDS = 30
SBATCH_PARTITION_RE = re.compile(r"^#SBATCH\s+(?:--partition[= ]\s*|-p\s*)(\S+)\s*$")
SBATCH_TIME_RE = re.compile(r"^#SBATCH\s+(?:--time[= ]\s*|-t\s*)(\S+)\s*$")


def _prep_target(args):
    """Resolve the target fields site-prep needs, or raise Rejected."""
    target = args.get("target")
    if not isinstance(target, str) or not target.strip():
        raise Rejected("target is required (a name from sites/sites.yaml)")
    target = target.strip()
    if _site(target, "name") != target:
        raise Rejected("unknown target %r; valid targets: %s"
                       % (target, ", ".join(_target_names()) or "(none found)"))
    t = {f: _site(target, f) for f in ("ssh_name", "scheduler", "mode", "directives")}
    t["name"] = target
    if t["scheduler"] not in ("slurm", "pbs", "none"):
        raise Rejected("target %r has scheduler %r; expected slurm, pbs or none" % (target, t["scheduler"]))
    if t["mode"] not in ("cpu", "gpu"):
        raise Rejected("target %r has mode %r; expected cpu or gpu" % (target, t["mode"]))
    return t


def spack_inputs(t):
    """spack.yaml inputs for a resolved target. A slurm target's --partition and
    --time lines become spack-builder's partition and time fields (so they are not
    requested twice); every other directive line is passed on verbatim."""
    inputs = {"cluster": t["ssh_name"], "scheduler_type": t["scheduler"],
              "scheduler": t["scheduler"] != "none", "build_gpu": t["mode"] == "gpu"}
    if t["scheduler"] == "none":
        return inputs
    lines, partition, walltime = [], "", ""
    for line in t["directives"].splitlines():
        s = line.strip()
        mp = SBATCH_PARTITION_RE.match(s) if t["scheduler"] == "slurm" else None
        mt = SBATCH_TIME_RE.match(s) if t["scheduler"] == "slurm" else None
        if mp:
            partition = mp.group(1)
        elif mt:
            walltime = mt.group(1)
        elif s:
            lines.append(line)
    inputs["directives"] = "\n".join(lines)
    if t["scheduler"] == "slurm":
        inputs["partition"] = partition
        if walltime:
            inputs["time"] = walltime
    return inputs


def _stage_prep(t, op, inputs):
    run_id = allocate_dir(PREP_AGENT, t["name"])
    rdir = os.path.join(RUNS, run_id)
    shutil.copyfile(os.path.join(WORKFLOWS, op + ".yaml"), os.path.join(rdir, op + ".yaml"))
    with open(os.path.join(rdir, op + ".inputs.json"), "w") as fh:
        json.dump(inputs, fh, indent=2)
        fh.write("\n")
    return run_id, rdir


def _start(run_id, op):
    """`pw workflows run` the staged op; returns (record, slug)."""
    rel = "runs/%s" % run_id
    argv = ["workflows", "run", "-i", "%s/%s.inputs.json" % (rel, op), "--name", run_id,
            "-o", "json", "%s/%s.yaml" % (rel, op)]
    record = {"run_id": run_id, "op": op, "argv": ["pw"] + argv, "started": _now()}
    rc, out, err = _pw(argv, timeout=120)
    slug = _find_slug(out)
    record.update(exit_code=rc, workflow_run=slug)
    if not slug:
        record.update(status="failed", error=("pw workflows run did not return a run slug: "
                                              + (err or out).strip()[-1500:]))
    return record, slug


def _view(slug):
    rc, out, _ = _pw(["workflows", "runs", "view", slug, "-o", "json"], timeout=60)
    try:
        return json.loads(out) if rc == 0 else {}
    except ValueError:
        return {}


def _failed_tail(slug):
    _, lout, _ = _pw(["workflows", "runs", "logs", "--failed", "--tail", "20", slug], timeout=60)
    return lout.strip()[-2500:]


def _write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")


def copy_buildcache(args):
    """Returns (structured_result, is_error, run_id_or_None)."""
    target = str(args.get("target", "")).strip()
    base = {"status": "rejected", "run_id": None, "target": target, "workflow_run": None,
            "next": None, "error": None}
    uri = args.get("uri")
    try:
        t = _prep_target(args)
        if (not isinstance(uri, str) or not BUCKET_URI_RE.match(uri.strip())
                or any(seg.strip(".") == "" for seg in uri.strip()[5:].split("/"))):
            raise Rejected("uri must be a bucket object ending in .tar.gz, in the form "
                           "pw://<user>/<bucket>/<name>.tar.gz (letters, digits, . _ - only; no . or .. "
                           "segments); got %r. "
                           "Nothing was copied." % (uri,))
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    uri = uri.strip()
    run_id, rdir = _stage_prep(t, "buildcache", {"cluster": t["ssh_name"], "uri": uri})
    base.update(run_id=run_id, cluster=t["ssh_name"], uri=uri)
    try:
        rec, slug = _start(run_id, "buildcache")
        if slug:
            view = _wait_terminal(slug, COPY_BUDGET_SECONDS)
            rec["workflow_status"] = view.get("status")
            if view.get("status") == "completed":
                _, lout, _ = _pw(["workflows", "runs", "logs", "--job", "copy", "--tail", "20", slug],
                                 timeout=60)
                found = _json_line(lout, "status")
                if found and found.get("status") == "copied":
                    rec.update(status="copied", copy=found)
                else:
                    rec.update(status="failed", error="workflow %s completed but printed no copied line"
                                                      % slug)
            elif view.get("status") in TERMINAL:
                rec.update(status="failed", error="workflow %s ended %s; failed step log tail:\n%s"
                                                  % (slug, view.get("status"), _failed_tail(slug)))
            else:
                rec.update(status="failed",
                           error="workflow %s was still %s after %d s; check it with "
                                 "`pw workflows runs view %s`" % (slug, view.get("status") or "unknown",
                                                                  COPY_BUDGET_SECONDS, slug))
    except Exception as e:  # pw missing, timeout, refused verb...
        rec = {"run_id": run_id, "op": "buildcache", "status": "failed", "error": str(e)}
    rec["finished"] = _now()
    _write_json(os.path.join(rdir, "prep.json"), rec)
    base.update(status=rec["status"], workflow_run=rec.get("workflow_run"), error=rec.get("error"))
    if rec["status"] == "copied":
        base["copy"] = rec["copy"]
        base["next"] = ("The build cache is unpacked in $HOME on the cluster. Report the run id and "
                        "the copy line and stop.")
    else:
        base["next"] = "Report the error verbatim and stop."
    return base, rec["status"] != "copied", run_id


def run_spack(args):
    """Returns (structured_result, is_error, run_id_or_None)."""
    target = str(args.get("target", "")).strip()
    base = {"status": "rejected", "run_id": None, "target": target, "workflow_run": None,
            "next": None, "error": None}
    try:
        t = _prep_target(args)
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    inputs = spack_inputs(t)
    run_id, rdir = _stage_prep(t, "spack", inputs)
    base.update(run_id=run_id, cluster=t["ssh_name"], scheduler=t["scheduler"], mode=t["mode"],
                build_gpu=inputs["build_gpu"])
    try:
        rec, slug = _start(run_id, "spack")
        if slug:
            time.sleep(SPACK_START_SECONDS)
            view = _view(slug)
            rec["workflow_status"] = view.get("status")
            if view.get("status") in TERMINAL and view.get("status") != "completed":
                rec.update(status="failed", error="workflow %s ended %s; failed step log tail:\n%s"
                                                  % (slug, view.get("status"), _failed_tail(slug)))
            else:
                rec["status"] = "started"
    except Exception as e:
        rec = {"run_id": run_id, "op": "spack", "status": "failed", "error": str(e)}
    rec["finished"] = _now()
    _write_json(os.path.join(rdir, "submit.json"), rec)
    base.update(status=rec["status"], workflow_run=rec.get("workflow_run"), error=rec.get("error"))
    if rec["status"] == "started":
        base["next"] = ("The build has started but is NOT finished; do not report yet. Your next "
                        "call must be mcp__heat__spack_status with run_id %s. Never call "
                        "mcp__heat__run_spack again for this build." % run_id)
    else:
        base["next"] = "Report the error verbatim and stop."
    return base, rec["status"] != "started", run_id


def spack_status(args):
    """Returns (structured_result, is_error, run_id_or_None). Read-only: only
    `pw workflows runs view|logs` on the run that run_spack started."""
    run_id = str(args.get("run_id", "")).strip()
    base = {"status": "rejected", "run_id": run_id, "workflow_run": None, "state": None,
            "done": False, "succeeded": False, "next": None, "error": None}
    rdir = os.path.join(RUNS, run_id)
    rec = _read_json(os.path.join(rdir, "submit.json")) if PREP_ID_RE.match(run_id) else None
    slug = rec.get("workflow_run") if isinstance(rec, dict) and rec.get("op") == "spack" else None
    if not slug:
        base["error"] = ("unknown run_id %r: pass the run_id that mcp__heat__run_spack returned for a "
                         "started build (e.g. %s-gce-cpu-000002)" % (run_id, PREP_AGENT))
        return base, True, None
    base["workflow_run"] = slug
    log_path = os.path.join(rdir, "statuses.jsonl")
    deadline = time.time() + SPACK_STATUS_BUDGET_SECONDS
    checks = 0
    try:
        while True:
            checks += 1
            view = _view(slug)
            state = view.get("status")
            jobs = {name: job.get("status") for name, job in (view.get("executedJobs") or {}).items()
                    if isinstance(job, dict)}
            with open(log_path, "a") as fh:
                fh.write(json.dumps({"ts": _now(), "state": state, "jobs": jobs},
                                    separators=(",", ":")) + "\n")
            if state in TERMINAL or time.time() + SPACK_STATUS_INTERVAL_SECONDS > deadline:
                break
            time.sleep(SPACK_STATUS_INTERVAL_SECONDS)
    except Exception as e:
        base.update(status="failed", error=str(e))
        return base, True, run_id
    done = state in TERMINAL
    base.update(status="checked", state=state, jobs=jobs, done=done,
                succeeded=(state == "completed"), checks_this_call=checks)
    if state == "completed":
        base["next"] = ("The build succeeded. Report it and stop. spack-builder leaves a placeholder "
                        "endpoint named spack-builder-<run slug> running; tell the user it can be "
                        "deleted with `pw endpoints delete`.")
    elif done:
        base["error"] = "workflow %s ended %s; failed step log tail:\n%s" % (slug, state, _failed_tail(slug))
        base["next"] = "The build failed. Report the error verbatim and stop."
    else:
        base["next"] = ("The build is still %s. Call mcp__heat__spack_status again with run_id %s, "
                        "or stop if your instructions say so." % (state or "unknown", run_id))
    return base, False, run_id


# --------------------------------------------------------------------------
# Ray fleet: ray-launcher-mcp (launch_ray, ray_status, stop_ray) and
# ray-runner-mcp (new_canvas, render_tiles, render_status, ray_status).
# One long-lived Ray cluster spans every cpu target (workflows/ray.yaml, head on
# the PW workspace); the fractal app (app/fractal/) pushes tiles into it through
# workflows/fractal.yaml. Ids: ray-launcher-mcp-fleet-<NNNNNN> and
# ray-runner-mcp-fleet-<NNNNNN> (the cluster spans the fleet, not one target).
# --------------------------------------------------------------------------
RAY_LAUNCHER = "ray-launcher-mcp"
RAY_RUNNER = "ray-runner-mcp"
FLEET = "fleet"
RAY_LAUNCH_ID_RE = re.compile(r"^%s-%s-\d{6}$" % (re.escape(RAY_LAUNCHER), FLEET))
RAY_RUN_ID_RE = re.compile(r"^%s-%s-\d{6}$" % (re.escape(RAY_RUNNER), FLEET))
ENDPOINT_RE = re.compile(r"^ray-cluster-[A-Za-z0-9._-]+$")
WALLTIME_RE = re.compile(r"^(\d+-)?\d{1,3}:\d{2}:\d{2}$")
RAY_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DEFAULT_RAY_VERSION = "2.40.0"
RAY_LAUNCH_BUDGET_SECONDS = 540   # stays under the agent's 10m MCP timeout
RAY_STATUS_BUDGET_SECONDS = 420
RAY_STATUS_INTERVAL_SECONDS = 15
STOP_BUDGET_SECONDS = 180
CANVAS_BUDGET_SECONDS = 300
RENDER_START_BUDGET_SECONDS = 120
RENDER_STATUS_BUDGET_SECONDS = 420
RENDER_STATUS_INTERVAL_SECONDS = 20
FRACTAL_LIMITS = {"tiles": (1, 256), "tile_px": (8, 4096), "max_iter": (1, 1000000)}
FRACTAL_DEFAULTS = {"tile_px": 128, "max_iter": 256, "palette": "electric"}
FRACTAL_PALETTES = ("electric", "fire", "ocean", "cosmic")
FRACTAL_ORDERS = ("random", "row", "center", "spiral")
RECT_RE = re.compile(r"^\d+,\d+,\d+,\d+$")

_SB = r"^#SBATCH\s+"
SBATCH_NODES_RE = re.compile(_SB + r"(?:--nodes[= ]\s*|-N\s*)(\d+)\s*$")
SBATCH_NTPN_RE = re.compile(_SB + r"--ntasks-per-node[= ]\s*(\d+)\s*$")
SBATCH_CPT_RE = re.compile(_SB + r"(?:--cpus-per-task[= ]\s*|-c\s*)(\d+)\s*$")
# Lines upstream's worker job writes itself (--nodes/--ntasks/--job-name/--output/
# --error/--export) or that describe heat's MPI task layout: not passed to Ray rows.
SBATCH_RAY_DROP_RE = re.compile(_SB + r"(?:--ntasks(?:[= ]|$)|-n\s*\d|--job-name\b|-J\s|--output\b|"
                                      r"-o\s|--error\b|-e\s|--export\b)")
PBS_QUEUE_RE = re.compile(r"^#PBS\s+-q\s+(\S+)\s*$")
PBS_WALLTIME_RE = re.compile(r"^#PBS\s+-l\s+walltime=(\S+)\s*$")
PBS_SELECT_RE = re.compile(r"^#PBS\s+-l\s+select=(\S+)\s*$")
PBS_RAY_DROP_RE = re.compile(r"^#PBS\s+(?:-N\b|-j\b|-o\b|-e\b|-V\b)")

RAY_LAUNCH_INSTRUCTIONS = (
    "Ray fleet cluster. To start it, call mcp__heat__launch_ray once; it builds one worker row "
    "per cpu target of sites/sites.yaml and returns a run_id. If it returns starting, call "
    "mcp__heat__ray_status with that run_id until it is ready or failed. Only stop a cluster "
    "with mcp__heat__stop_ray when the user asks for that."
)
RAY_RUN_INSTRUCTIONS = (
    "Fractal tiles on the running Ray cluster. mcp__heat__new_canvas shows an empty N x N "
    "canvas on the dashboard and returns its canvas_id. mcp__heat__render_tiles renders more "
    "tiles into a canvas (count, all, or rect) and returns a run_id at once. "
    "mcp__heat__render_status with that run_id waits for the render and reports it."
)

LAUNCH_RAY_TOOL = {
    "name": "launch_ray",
    "title": "Start the Ray cluster across the fleet",
    "description": (
        "START a long-lived Ray cluster: head and live dashboard on the PW workspace, one worker "
        "row per cpu target of sites/sites.yaml (slurm/pbs rows from the target's directives, "
        "scheduler none targets as SSH workers on the login node). Waits up to about 9 minutes "
        "for the cluster to be ready. Returns ready (with the dashboard URL), starting (call "
        "mcp__heat__ray_status next), rejected or failed. Refused while another Ray cluster "
        "endpoint is running. The cluster stays up until mcp__heat__stop_ray or the workers' "
        "walltime ends."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "walltime": {"type": "string",
                         "description": "Worker walltime HH:MM:SS or D-HH:MM:SS for every slurm/pbs "
                                        "row (default: each target's own walltime)"},
            "ray_version": {"type": "string", "description": "Ray version, default 2.40.0"},
        },
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ready", "starting", "rejected", "failed"]},
            "run_id": {"type": ["string", "null"]},
            "workflow_run": {"type": ["string", "null"]},
            "endpoint": {"type": ["string", "null"]},
            "dashboard_url": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

RAY_STATUS_TOOL = {
    "name": "ray_status",
    "title": "State of a Ray fleet cluster",
    "description": (
        "Read-only: the state of a Ray cluster mcp__heat__launch_ray started (default: the most "
        "recent one). While it is starting, waits up to a few minutes for it. Returns status "
        "ready, starting, failed or stopped, with the endpoint and dashboard URL. Never starts "
        "or stops anything."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string",
                       "description": "e.g. ray-launcher-mcp-fleet-000001 (default: the most recent)"},
        },
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ready", "starting", "failed", "stopped", "rejected"]},
            "run_id": {"type": ["string", "null"]},
            "live": {"type": "boolean"},
            "endpoint": {"type": ["string", "null"]},
            "dashboard_url": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

STOP_RAY_TOOL = {
    "name": "stop_ray",
    "title": "Stop a Ray fleet cluster",
    "description": (
        "STOP the Ray cluster that mcp__heat__launch_ray started with this run_id: deletes its "
        "endpoint (pw endpoints delete ray-cluster-<slug>), which stops the dashboard, cancels "
        "every worker job and stops Ray. Only when the user asks. Returns stopped or failed."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "e.g. ray-launcher-mcp-fleet-000001"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["stopped", "rejected", "failed"]},
            "run_id": {"type": "string"},
            "endpoint": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "run_id"],
    },
}

NEW_CANVAS_TOOL = {
    "name": "new_canvas",
    "title": "Show an empty fractal canvas on the Ray dashboard",
    "description": (
        "Create a new N x N fractal canvas on the running Ray cluster and show it EMPTY on the "
        "dashboard (replacing the canvas shown before). Nothing is rendered yet: call "
        "mcp__heat__render_tiles with the returned canvas_id. tiles is the tiles per side "
        "(1-256, total tiles = tiles^2); tile_px the pixels per tile side (8-4096, default 128); "
        "max_iter the iterations per pixel (default 256). Work per tile grows with "
        "tile_px^2 x max_iter."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tiles": {"type": "integer", "description": "Tiles per side, e.g. 10 for 10 x 10"},
            "tile_px": {"type": "integer", "description": "Pixels per tile side (default 128)"},
            "max_iter": {"type": "integer", "description": "Iterations per pixel (default 256)"},
            "palette": {"type": "string", "enum": list(FRACTAL_PALETTES)},
            "cluster_run_id": {"type": "string",
                               "description": "The launch run id (default: the most recent ready cluster)"},
        },
        "required": ["tiles"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["canvas_ready", "rejected", "failed"]},
            "canvas_id": {"type": ["string", "null"]},
            "total_tiles": {"type": ["integer", "null"]},
            "dashboard_url": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

RENDER_TILES_TOOL = {
    "name": "render_tiles",
    "title": "Render more tiles into a fractal canvas",
    "description": (
        "Render more tiles of a canvas (canvas_id from mcp__heat__new_canvas) on the Ray cluster; "
        "they appear on the dashboard as they finish. Give exactly one of: count (that many tiles "
        "not yet rendered), all (every remaining tile), or rect \"x0,y0,x1,y1\" (a block of tile "
        "coordinates). order picks which free tiles go first (random, row, center, spiral). "
        "Returns a run_id as soon as the render is running; call mcp__heat__render_status with "
        "it. Every call starts a NEW render."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "canvas_id": {"type": "string", "description": "e.g. ray-runner-mcp-fleet-000001"},
            "count": {"type": "integer", "description": "Number of tiles to render"},
            "all": {"type": "boolean", "description": "Render every tile not yet rendered"},
            "rect": {"type": "string", "description": "x0,y0,x1,y1 tile block (inclusive)"},
            "order": {"type": "string", "enum": list(FRACTAL_ORDERS)},
        },
        "required": ["canvas_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                       "enum": ["submitted", "added", "partial", "failed", "rejected"]},
            "run_id": {"type": ["string", "null"]},
            "canvas_id": {"type": ["string", "null"]},
            "dashboard_url": {"type": ["string", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status"],
    },
}

RENDER_STATUS_TOOL = {
    "name": "render_status",
    "title": "Check a fractal render or a canvas",
    "description": (
        "Read-only. With a run_id from mcp__heat__render_tiles: waits up to a few minutes for that "
        "render and returns done, and when done the rendered/failed counts, tiles per second, "
        "tiles per site and the canvas progress. With a canvas_id: the canvas progress (tiles "
        "done of total, per site). Never renders anything."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string",
                       "description": "A render run id or a canvas id (ray-runner-mcp-fleet-NNNNNN)"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["checked", "rejected", "failed"]},
            "run_id": {"type": "string"},
            "done": {"type": "boolean"},
            "result": {"type": ["object", "null"]},
            "next": {"type": ["string", "null"]},
            "error": {"type": ["string", "null"]},
        },
        "required": ["status", "run_id", "done"],
    },
}


# ---- worker rows from sites.yaml -----------------------------------------
def ray_worker_row(t, walltime=None):
    """One ray-cluster worker row for a resolved cpu target; returns (row, notes).
    The target's directives are mapped, never edited: slurm --partition/--time/--nodes
    and pbs -q/-l walltime/-l select become the row's fields; --ntasks-per-node x
    --cpus-per-task becomes --cpus-per-task (ray-cluster runs ONE task per node, so
    that keeps the target's cores per node); lines ray-cluster's worker job writes
    itself are left out; everything else passes verbatim."""
    notes = []
    if t["scheduler"] == "none":
        return {"resource": t["ssh_name"], "scheduler": False}, ["SSH worker on the login node"]
    lines = []
    if t["scheduler"] == "slurm":
        partition, tm, nodes, ntpn, cpt = "", "", 1, None, None
        for line in t["directives"].splitlines():
            s = line.strip()
            if not s:
                continue
            for rx, key in ((SBATCH_PARTITION_RE, "p"), (SBATCH_TIME_RE, "t"), (SBATCH_NODES_RE, "n"),
                            (SBATCH_NTPN_RE, "k"), (SBATCH_CPT_RE, "c")):
                m = rx.match(s)
                if m:
                    break
            if m:
                v = m.group(1)
                if key == "p":
                    partition = v
                elif key == "t":
                    tm = v
                elif key == "n":
                    nodes = int(v)
                elif key == "k":
                    ntpn = int(v)
                else:
                    cpt = int(v)
                continue
            if SBATCH_RAY_DROP_RE.match(s):
                notes.append("left out %r (set by ray-cluster)" % s)
                continue
            lines.append(s)
        if ntpn or cpt:
            per_node = (ntpn or 1) * (cpt or 1)
            lines.append("#SBATCH --cpus-per-task=%d" % per_node)
            notes.append("one Ray worker per node with %d cpus (the target's cores per node)" % per_node)
        row = {"resource": t["ssh_name"], "scheduler": True,
               "slurm": {"partition": partition, "nodes": nodes, "gres": "",
                         "time": walltime or tm or "01:00:00", "scheduler_directives": "\n".join(lines)}}
        return row, notes
    queue, wall, select, nodes = "", "", "", 1
    for line in t["directives"].splitlines():
        s = line.strip()
        if not s:
            continue
        mq, mw, ms = PBS_QUEUE_RE.match(s), PBS_WALLTIME_RE.match(s), PBS_SELECT_RE.match(s)
        if mq:
            queue = mq.group(1)
        elif mw:
            wall = mw.group(1)
        elif ms:
            select = ms.group(1)
            # mpiprocs=K lists a node K times in PBS_NODEFILE, while ray-cluster starts
            # one worker per node index; keep ncpus (the cores), drop mpiprocs.
            if re.search(r":mpiprocs=\d+", select):
                select = re.sub(r":mpiprocs=\d+", "", select)
                notes.append("dropped mpiprocs from select (one Ray worker per node)")
            mn = re.match(r"^(\d+)", select)
            nodes = int(mn.group(1)) if mn else 1
        elif PBS_RAY_DROP_RE.match(s):
            notes.append("left out %r (set by ray-cluster)" % s)
        else:
            lines.append(s)
    row = {"resource": t["ssh_name"], "scheduler": True,
           "pbs": {"queue": queue, "nodes": nodes, "select": select,
                   "walltime": walltime or wall or "01:00:00", "scheduler_directives": "\n".join(lines)}}
    return row, notes


def fleet_worker_rows(walltime=None):
    """(rows, used, skipped): one row per cpu target (identical rows once)."""
    rows, used, skipped, seen = [], [], [], set()
    for name in _target_names():
        try:
            t = _prep_target({"target": name})
        except Rejected as e:
            skipped.append({"target": name, "reason": str(e)})
            continue
        if t["mode"] != "cpu":
            skipped.append({"target": name, "reason": "mode %s (Ray workers use cpu targets)" % t["mode"]})
            continue
        row, notes = ray_worker_row(t, walltime)
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            skipped.append({"target": name, "reason": "same worker row as an earlier target"})
            continue
        seen.add(key)
        rows.append(row)
        used.append({"target": name, "row": len(rows) - 1, "resource": t["ssh_name"],
                     "scheduler": t["scheduler"], "notes": notes})
    return rows, used, skipped


# ---- endpoints ------------------------------------------------------------
def _endpoints():
    """{name: url} of the ray-cluster-* endpoints (read-only `pw endpoints list`).
    Tries -o json first, then the tab-separated table ray-cluster itself parses
    (name in column 1, url in column 3)."""
    eps = {}
    rc, out, _ = _pw(["endpoints", "list", "-o", "json"], timeout=60)
    if rc == 0:
        try:
            data = json.loads(out)
            items = data if isinstance(data, list) else (data.get("endpoints") or data.get("items") or [])
            for e in items:
                if isinstance(e, dict) and isinstance(e.get("name"), str):
                    eps[e["name"]] = e.get("url") or e.get("URL") or e.get("link") or ""
            return {k: v for k, v in eps.items() if k.startswith("ray-cluster-")}
        except ValueError:
            pass
    rc, out, err = _pw(["endpoints", "list"], timeout=60)
    if rc != 0:
        raise RuntimeError("pw endpoints list failed: %s" % (err or out).strip()[-500:])
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if cols and cols[0].startswith("ray-cluster-"):
            eps[cols[0]] = cols[2] if len(cols) > 2 else ""
    return eps


def _find_endpoint(slug, before, eps):
    """The endpoint of the ray.yaml run `slug`: ray-cluster-<slug>, else the one
    new ray-cluster-* endpoint since launch, else the name its log prints."""
    if "ray-cluster-%s" % slug in eps:
        return "ray-cluster-%s" % slug
    new = [n for n in eps if n not in before]
    if len(new) == 1:
        return new[0]
    _, lout, _ = _pw(["workflows", "runs", "logs", slug], timeout=60)
    m = re.findall(r"(ray-cluster-[A-Za-z0-9._-]+)", lout)
    for name in reversed(m):
        if ENDPOINT_RE.match(name) and name != "ray-cluster-":
            return name
    return None


def _launch_ids():
    try:
        names = os.listdir(RUNS)
    except OSError:
        return []
    return sorted(n for n in names if RAY_LAUNCH_ID_RE.match(n))


def _cluster_record(run_id):
    rec = _read_json(os.path.join(RUNS, run_id, "cluster.json")) if RAY_LAUNCH_ID_RE.match(run_id or "") else None
    return rec if isinstance(rec, dict) else None


def _cluster_state(run_id, wait=False):
    """Refresh runs/<run_id>/cluster.json from the workflow run and the endpoint
    list; while starting and `wait`, keep checking up to RAY_STATUS_BUDGET_SECONDS."""
    rec = _cluster_record(run_id)
    if not rec or not rec.get("workflow_run"):
        raise Rejected("unknown cluster run_id %r: pass the run_id mcp__heat__launch_ray returned "
                       "(e.g. %s-%s-000001)" % (run_id, RAY_LAUNCHER, FLEET))
    slug = rec["workflow_run"]
    deadline = time.time() + (RAY_STATUS_BUDGET_SECONDS if wait else 0)
    while True:
        view = _view(slug)
        state = view.get("status")
        eps = _endpoints()
        if not rec.get("endpoint"):
            rec["endpoint"] = _find_endpoint(slug, set(rec.get("endpoints_before") or []), eps)
        live = bool(rec.get("endpoint")) and rec["endpoint"] in eps
        if live and eps.get(rec["endpoint"]):
            rec["dashboard_url"] = eps[rec["endpoint"]]
        if rec.get("status") == "stopped" and not live:
            status = "stopped"
        elif state in TERMINAL and state != "completed":
            status = "failed"
        elif state == "completed":
            status = "ready" if live else "stopped"
        else:
            status = "starting"
        if status != "starting" or time.time() + RAY_STATUS_INTERVAL_SECONDS > deadline:
            break
        time.sleep(RAY_STATUS_INTERVAL_SECONDS)
    rec.update(status=status, workflow_status=state, live=live, checked=_now())
    if status == "failed" and not rec.get("error"):
        rec["error"] = "workflow %s ended %s; failed step log tail:\n%s" % (slug, state, _failed_tail(slug))
    if status == "ready" and not rec.get("ready_at"):
        rec["ready_at"] = _now()
    _write_json(os.path.join(RUNS, run_id, "cluster.json"), rec)
    return rec


def _cluster_result(rec, base_status=None):
    out = {"status": base_status or rec.get("status"), "run_id": rec.get("run_id"),
           "workflow_run": rec.get("workflow_run"), "workflow_status": rec.get("workflow_status"),
           "live": bool(rec.get("live")), "endpoint": rec.get("endpoint"),
           "dashboard_url": rec.get("dashboard_url"), "workers": rec.get("workers"),
           "walltime": rec.get("walltime"), "next": None, "error": rec.get("error")}
    return out


def ray_inputs(rows, version):
    """ray.inputs.json for workflows/ray.yaml, the local copy of ray-cluster's general.yaml
    run as the TOP-LEVEL workflow (as a subworkflow its app/ checkout lands where its
    start script does not look). name ray-cluster makes the endpoint ray-cluster-<slug>."""
    return {"head": {"resource": "workspace"}, "workers": rows,
            "ray_settings": {"name": "ray-cluster", "ray_version": version},
            "workload_settings": {"workload_type": "cluster_only", "run_user_script": False}}


def launch_ray(args):
    """Returns (structured_result, is_error, run_id_or_None)."""
    base = {"status": "rejected", "run_id": None, "workflow_run": None, "endpoint": None,
            "dashboard_url": None, "next": None, "error": None}
    walltime = args.get("walltime")
    version = str(args.get("ray_version") or DEFAULT_RAY_VERSION).strip()
    try:
        if walltime is not None:
            walltime = str(walltime).strip()
            if not WALLTIME_RE.match(walltime):
                raise Rejected("walltime must be HH:MM:SS or D-HH:MM:SS, got %r" % walltime)
        if not RAY_VERSION_RE.match(version):
            raise Rejected("ray_version must look like 2.40.0, got %r" % version)
        rows, used, skipped = fleet_worker_rows(walltime)
        if not rows:
            raise Rejected("no cpu target in sites/sites.yaml to use as a Ray worker")
        eps = _endpoints()
        if eps:
            owners = {}
            for rid in _launch_ids():
                r = _cluster_record(rid) or {}
                if r.get("endpoint") in eps:
                    owners[r["endpoint"]] = rid
            raise Rejected("a Ray cluster endpoint is already running (%s). Only one Ray head runs on "
                           "the workspace: use it, or stop it first (mcp__heat__stop_ray with its "
                           "run_id) if the user asks. Nothing was launched."
                           % ", ".join("%s from %s" % (n, owners.get(n, "outside this repo")) for n in eps))
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    except Exception as e:  # pw missing, sites.yaml unreadable...
        base.update(status="failed", error=str(e))
        return base, True, None

    run_id = allocate_dir(RAY_LAUNCHER, FLEET)
    rdir = os.path.join(RUNS, run_id)
    shutil.copyfile(os.path.join(WORKFLOWS, "ray.yaml"), os.path.join(rdir, "ray.yaml"))
    inputs = ray_inputs(rows, version)
    _write_json(os.path.join(rdir, "ray.inputs.json"), inputs)
    _write_json(os.path.join(rdir, "workers.json"), {"workers": used, "skipped": skipped})
    rec = {"run_id": run_id, "op": "ray", "started": _now(), "walltime": walltime,
           "ray_version": version, "workers": used, "skipped": skipped, "endpoints_before": sorted(eps)}
    try:
        srec, slug = _start(run_id, "ray")
        rec.update(argv=srec.get("argv"), workflow_run=slug)
        if not slug:
            rec.update(status="failed", error=srec.get("error"))
    except Exception as e:
        slug = None
        rec.update(status="failed", error=str(e))
    _write_json(os.path.join(rdir, "cluster.json"), rec)
    if slug:
        try:
            deadline = time.time() + RAY_LAUNCH_BUDGET_SECONDS
            while True:
                rec = _cluster_state(run_id)
                if rec["status"] != "starting" or time.time() + RAY_STATUS_INTERVAL_SECONDS > deadline:
                    break
                time.sleep(RAY_STATUS_INTERVAL_SECONDS)
        except Exception as e:
            rec = _cluster_record(run_id) or rec
            rec.update(status="failed", error=str(e))
            _write_json(os.path.join(rdir, "cluster.json"), rec)
    result = _cluster_result(rec)
    result["skipped"] = skipped
    if rec["status"] == "ready":
        result["next"] = ("The cluster is ready and keeps running. Report the run_id, the endpoint and "
                          "the dashboard_url, and stop. Do not stop the cluster unless the user asks.")
    elif rec["status"] == "starting":
        result["next"] = ("The cluster is still starting. Call mcp__heat__ray_status with run_id %s. "
                          "Never call mcp__heat__launch_ray again for it." % run_id)
    else:
        result["next"] = "Report the error verbatim and stop."
    return result, rec["status"] not in ("ready", "starting"), run_id


def ray_status(args):
    run_id = str(args.get("run_id") or "").strip()
    base = {"status": "rejected", "run_id": run_id or None, "live": False, "endpoint": None,
            "dashboard_url": None, "next": None, "error": None}
    if not run_id:
        ids = _launch_ids()
        if not ids:
            base["error"] = "no Ray cluster has been launched from this repo (no %s-* run)" % RAY_LAUNCHER
            return base, True, None
        run_id = ids[-1]
    try:
        rec = _cluster_state(run_id, wait=True)
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    except Exception as e:
        base.update(status="failed", run_id=run_id, error=str(e))
        return base, True, run_id
    result = _cluster_result(rec)
    result["next"] = {
        "ready": "The cluster is ready. Report it and stop.",
        "starting": "Still starting. Call mcp__heat__ray_status again with run_id %s, or stop if your "
                    "instructions say so." % run_id,
        "stopped": "The cluster is not running (stopped, or its endpoint is gone). Report it.",
        "failed": "The launch failed. Report the error verbatim and stop.",
    }[rec["status"]]
    return result, False, run_id


def stop_ray(args):
    run_id = str(args.get("run_id") or "").strip()
    base = {"status": "rejected", "run_id": run_id, "endpoint": None, "next": None, "error": None}
    rec = _cluster_record(run_id)
    if not rec or not rec.get("workflow_run"):
        base["error"] = ("unknown cluster run_id %r: pass the run_id mcp__heat__launch_ray returned"
                         % run_id)
        return base, True, None
    try:
        rec = _cluster_state(run_id)
        ep = rec.get("endpoint")
        base["endpoint"] = ep
        if not ep or not ENDPOINT_RE.match(ep):
            raise Rejected("cluster %s has no known ray-cluster endpoint; nothing was stopped" % run_id)
        if not rec.get("live"):
            rec.update(status="stopped", stopped=rec.get("stopped") or _now())
            _write_json(os.path.join(RUNS, run_id, "cluster.json"), rec)
            base.update(status="stopped", next="The endpoint was already gone; nothing to stop. Report it.")
            return base, False, run_id
        rc, out, err = _pw(["endpoints", "delete", ep], timeout=120)
        rec["stop"] = {"argv": ["pw", "endpoints", "delete", ep], "exit_code": rc, "at": _now(),
                       "output": (out or err).strip()[-500:]}
        deadline = time.time() + STOP_BUDGET_SECONDS
        while ep in _endpoints():
            if time.time() > deadline:
                raise RuntimeError("endpoint %s is still listed %d s after pw endpoints delete (exit %d): %s"
                                   % (ep, STOP_BUDGET_SECONDS, rc, (err or out).strip()[-300:]))
            time.sleep(10)
        rec.update(status="stopped", live=False, stopped=_now())
        _write_json(os.path.join(RUNS, run_id, "cluster.json"), rec)
    except Rejected as e:
        base["error"] = str(e)
        return base, True, run_id
    except Exception as e:
        base.update(status="failed", error=str(e), next="Report the error verbatim and stop.")
        return base, True, run_id
    base.update(status="stopped", next=("The endpoint is deleted; ray-cluster's teardown cancels the "
                                        "worker jobs and stops Ray. Report it and stop."))
    return base, False, run_id


# ---- fractal canvases and renders ----------------------------------------
def _live_cluster(cluster_run_id=None):
    """(launch run id, cluster record) of a READY cluster, or raise Rejected."""
    cid = str(cluster_run_id or "").strip()
    if not cid:
        ids = _launch_ids()
        if not ids:
            raise Rejected("no Ray cluster has been launched; ask ray-launcher-mcp to launch one")
        cid = ids[-1]
    rec = _cluster_state(cid)
    if rec["status"] != "ready":
        raise Rejected("Ray cluster %s is %s, not ready; ask ray-launcher-mcp about it. Nothing was "
                       "rendered." % (cid, rec["status"]))
    return cid, rec


def _fractal_inputs(run_id, action, canvas_id, endpoint, **fields):
    inputs = {"action": action, "run_id": run_id, "canvas_id": canvas_id, "endpoint": endpoint,
              "repo_url": _git("remote", "get-url", "origin"), "commit": _git("rev-parse", "HEAD")}
    for key in ("tiles", "tile_px", "max_iter", "palette", "count", "all", "rect", "order"):
        v = fields.get(key)
        inputs[key] = "" if v is None else (("true" if v else "") if isinstance(v, bool) else str(v))
    return inputs


def _start_fractal(dir_id, inputs_name, name):
    """`pw workflows run` runs/<dir_id>/fractal.yaml with the named inputs file."""
    rel = "runs/%s" % dir_id
    argv = ["workflows", "run", "-i", "%s/%s" % (rel, inputs_name), "--name", name, "-o", "json",
            "%s/fractal.yaml" % rel]
    rc, out, err = _pw(argv, timeout=120)
    slug = _find_slug(out)
    rec = {"argv": ["pw"] + argv, "exit_code": rc, "workflow_run": slug, "started": _now()}
    if not slug:
        rec["error"] = "pw workflows run did not return a run slug: " + (err or out).strip()[-1500:]
    return rec, slug


def _fractal_log(slug, tail=80):
    _, lout, _ = _pw(["workflows", "runs", "logs", "--job", "fractal", "--tail", str(tail), slug], timeout=60)
    return lout


def _stage_fractal(inputs_maker):
    run_id = allocate_dir(RAY_RUNNER, FLEET)
    rdir = os.path.join(RUNS, run_id)
    shutil.copyfile(os.path.join(WORKFLOWS, "fractal.yaml"), os.path.join(rdir, "fractal.yaml"))
    inputs = inputs_maker(run_id)
    _write_json(os.path.join(rdir, "fractal.inputs.json"), inputs)
    return run_id, rdir, inputs


def _canvas_record(canvas_id):
    rec = _read_json(os.path.join(RUNS, canvas_id, "canvas.json")) if RAY_RUN_ID_RE.match(canvas_id or "") else None
    return rec if isinstance(rec, dict) and rec.get("status") == "canvas_ready" else None


def new_canvas(args):
    base = {"status": "rejected", "canvas_id": None, "total_tiles": None, "dashboard_url": None,
            "next": None, "error": None}
    try:
        vals = {}
        for key in ("tiles", "tile_px", "max_iter"):
            if key == "tiles" or args.get(key) is not None:
                v = _as_int(key, args.get(key))
                lo, hi = FRACTAL_LIMITS[key]
                if not lo <= v <= hi:
                    raise Rejected("%s must be %d..%d, got %d" % (key, lo, hi, v))
                vals[key] = v
            else:
                vals[key] = FRACTAL_DEFAULTS[key]
        palette = str(args.get("palette") or FRACTAL_DEFAULTS["palette"]).strip()
        if palette not in FRACTAL_PALETTES:
            raise Rejected("palette must be one of %s" % ", ".join(FRACTAL_PALETTES))
        cid, crec = _live_cluster(args.get("cluster_run_id"))
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    except Exception as e:
        base.update(status="failed", error=str(e))
        return base, True, None
    run_id, rdir, inputs = _stage_fractal(lambda rid: _fractal_inputs(
        rid, "init", rid, crec["endpoint"], palette=palette, **vals))
    rec = {"run_id": run_id, "op": "init", "cluster_run_id": cid, "endpoint": crec["endpoint"],
           "dashboard_url": crec.get("dashboard_url"), "tiles": vals["tiles"], "tile_px": vals["tile_px"],
           "max_iter": vals["max_iter"], "palette": palette, "commit": inputs["commit"]}
    try:
        srec, slug = _start_fractal(run_id, "fractal.inputs.json", run_id)
        rec.update(srec)
        if slug:
            view = _wait_terminal(slug, CANVAS_BUDGET_SECONDS)
            rec["workflow_status"] = view.get("status")
            found = _json_line(_fractal_log(slug), "status") if view.get("status") == "completed" else None
            if found and found.get("status") == "canvas_ready":
                rec.update(status="canvas_ready", canvas=found)
            elif view.get("status") in TERMINAL:
                rec.update(status="failed", error="workflow %s ended %s; log tail:\n%s"
                           % (slug, view.get("status"), _failed_tail(slug) or _fractal_log(slug, 20)))
            else:
                rec.update(status="failed", error="workflow %s was still %s after %d s"
                           % (slug, view.get("status") or "unknown", CANVAS_BUDGET_SECONDS))
        else:
            rec["status"] = "failed"
    except Exception as e:
        rec.update(status="failed", error=str(e))
    rec["finished"] = _now()
    _write_json(os.path.join(rdir, "canvas.json"), rec)
    base.update(status=rec["status"], workflow_run=rec.get("workflow_run"), error=rec.get("error"),
                dashboard_url=rec.get("dashboard_url"), cluster_run_id=cid)
    if rec["status"] == "canvas_ready":
        base.update(canvas_id=run_id, total_tiles=vals["tiles"] ** 2, tiles=vals["tiles"],
                    tile_px=vals["tile_px"], max_iter=vals["max_iter"], palette=palette,
                    next=("The canvas is empty on the dashboard. To render tiles, call "
                          "mcp__heat__render_tiles with canvas_id %s." % run_id))
    else:
        base["next"] = "Report the error verbatim and stop."
    return base, rec["status"] != "canvas_ready", run_id


def render_tiles(args):
    canvas_id = str(args.get("canvas_id") or "").strip()
    base = {"status": "rejected", "run_id": None, "canvas_id": canvas_id, "dashboard_url": None,
            "next": None, "error": None}
    try:
        crec = _canvas_record(canvas_id)
        if not crec:
            raise Rejected("unknown canvas_id %r: pass the canvas_id mcp__heat__new_canvas returned "
                           "(e.g. %s-%s-000001)" % (canvas_id, RAY_RUNNER, FLEET))
        total = crec["tiles"] ** 2
        count = args.get("count")
        all_ = args.get("all") is True
        rect = args.get("rect")
        given = [k for k, v in (("count", count), ("all", all_ or None), ("rect", rect)) if v not in (None, "")]
        if len(given) != 1:
            raise Rejected("give exactly one of count, all or rect (got %s)" % (", ".join(given) or "none"))
        if count is not None:
            count = _as_int("count", count)
            if not 1 <= count <= total:
                raise Rejected("count must be 1..%d (the canvas has %d tiles)" % (total, total))
        if rect is not None:
            rect = str(rect).replace(" ", "")
            if not RECT_RE.match(rect) or any(int(v) >= crec["tiles"] for v in rect.split(",")):
                raise Rejected("rect must be x0,y0,x1,y1 with every value 0..%d" % (crec["tiles"] - 1))
        order = str(args.get("order") or "random").strip()
        if order not in FRACTAL_ORDERS:
            raise Rejected("order must be one of %s" % ", ".join(FRACTAL_ORDERS))
        cid, clu = _live_cluster(crec.get("cluster_run_id"))
        if clu.get("endpoint") != crec.get("endpoint"):
            raise Rejected("canvas %s belongs to cluster %s, which is no longer running; create a new "
                           "canvas" % (canvas_id, crec.get("cluster_run_id")))
    except Rejected as e:
        base["error"] = str(e)
        return base, True, None
    except Exception as e:
        base.update(status="failed", error=str(e))
        return base, True, None
    run_id, rdir, inputs = _stage_fractal(lambda rid: _fractal_inputs(
        rid, "add", canvas_id, crec["endpoint"], count=count, all=all_, rect=rect, order=order))
    rec = {"run_id": run_id, "op": "add", "canvas_id": canvas_id, "cluster_run_id": cid,
           "endpoint": crec["endpoint"], "count": count, "all": all_, "rect": rect, "order": order,
           "commit": inputs["commit"]}
    base.update(run_id=run_id, dashboard_url=clu.get("dashboard_url"))
    try:
        srec, slug = _start_fractal(run_id, "fractal.inputs.json", run_id)
        rec.update(srec)
        if slug:
            deadline = time.time() + RENDER_START_BUDGET_SECONDS
            while True:
                view = _view(slug)
                lout = _fractal_log(slug)
                found = _json_line(lout, "submission")
                if view.get("status") in TERMINAL:
                    if found:
                        rec.update(status=found.get("status", "failed"), result=found)
                    else:
                        rec.update(status="failed", error="workflow %s ended %s; log tail:\n%s"
                                   % (slug, view.get("status"), _failed_tail(slug) or lout[-2000:]))
                    break
                if "SUBMITTED ray_job=" in lout:
                    rec["status"] = "submitted"
                    break
                if time.time() > deadline:
                    rec["status"] = "submitted"
                    rec["note"] = "the workflow is still staging after %d s" % RENDER_START_BUDGET_SECONDS
                    break
                time.sleep(5)
            rec["workflow_status"] = view.get("status")
        else:
            rec["status"] = "failed"
    except Exception as e:
        rec.update(status="failed", error=str(e))
    rec["finished"] = _now()
    _write_json(os.path.join(rdir, "submit.json"), rec)
    base.update(status=rec["status"], workflow_run=rec.get("workflow_run"), error=rec.get("error"),
                result=rec.get("result"))
    if rec["status"] == "submitted":
        base["next"] = ("The render is running; tiles appear on the dashboard as they finish. Call "
                        "mcp__heat__render_status with run_id %s. Never call mcp__heat__render_tiles "
                        "again for this request." % run_id)
    elif rec["status"] in ("added", "partial"):
        base["next"] = "The render already finished. Report its result and stop."
    else:
        base["next"] = "Report the error verbatim and stop."
    return base, rec["status"] not in ("submitted", "added", "partial"), run_id


def render_status(args):
    run_id = str(args.get("run_id") or "").strip()
    base = {"status": "rejected", "run_id": run_id, "done": False, "result": None, "next": None, "error": None}
    rdir = os.path.join(RUNS, run_id)
    sub = _read_json(os.path.join(rdir, "submit.json")) if RAY_RUN_ID_RE.match(run_id) else None
    canvas = _canvas_record(run_id)
    if isinstance(sub, dict) and sub.get("op") == "add" and sub.get("workflow_run"):
        slug = sub["workflow_run"]
        log_path = os.path.join(rdir, "statuses.jsonl")
        deadline = time.time() + RENDER_STATUS_BUDGET_SECONDS
        try:
            while True:
                view = _view(slug)
                state = view.get("status")
                lout = _fractal_log(slug)
                found = _json_line(lout, "submission")
                prog = [l.strip() for l in lout.splitlines() if re.search(r"\d+/\d+ tiles \(", l)]
                with open(log_path, "a") as fh:
                    fh.write(json.dumps({"ts": _now(), "state": state,
                                         "progress": prog[-1] if prog else None}, separators=(",", ":")) + "\n")
                if state in TERMINAL or found or time.time() + RENDER_STATUS_INTERVAL_SECONDS > deadline:
                    break
                time.sleep(RENDER_STATUS_INTERVAL_SECONDS)
        except Exception as e:
            base.update(status="failed", error=str(e))
            return base, True, run_id
        done = bool(found) or state in TERMINAL
        base.update(status="checked", done=done, workflow_run=slug, workflow_status=state, result=found,
                    progress=prog[-1] if prog else None, canvas_id=sub.get("canvas_id"))
        if found:
            sub["result"] = found
            sub["final_state"] = state
            _write_json(os.path.join(rdir, "submit.json"), sub)
            base["next"] = "The render finished. Report its result and stop."
        elif done:
            base["error"] = "workflow %s ended %s without a result line; log tail:\n%s" % (
                slug, state, _failed_tail(slug) or lout[-2000:])
            base["next"] = "The render failed. Report the error verbatim and stop."
        else:
            base["next"] = ("Still rendering (%s). Call mcp__heat__render_status again with run_id %s, or "
                            "stop if your instructions say so." % (base["progress"] or "starting", run_id))
        return base, bool(done and not found), run_id
    if canvas:
        # A canvas id: run fractal.yaml action=status, staged in the canvas's own dir.
        try:
            _, clu = _live_cluster(canvas.get("cluster_run_id"))
            n = 1
            while os.path.exists(os.path.join(rdir, "status-%d.inputs.json" % n)):
                n += 1
            name = "%s-status-%d" % (run_id, n)
            _write_json(os.path.join(rdir, "status-%d.inputs.json" % n),
                        _fractal_inputs(name, "status", run_id, canvas["endpoint"]))
            srec, slug = _start_fractal(run_id, "status-%d.inputs.json" % n, name)
            if not slug:
                raise RuntimeError(srec.get("error"))
            view = _wait_terminal(slug, CANVAS_BUDGET_SECONDS)
            found = _json_line(_fractal_log(slug), "canvas")
            if not found:
                raise RuntimeError("status workflow %s ended %s without a result line; log tail:\n%s"
                                   % (slug, view.get("status"), _failed_tail(slug)))
        except Rejected as e:
            base["error"] = str(e)
            return base, True, run_id
        except Exception as e:
            base.update(status="failed", error=str(e))
            return base, True, run_id
        base.update(status="checked", done=True, result=found, workflow_run=slug, canvas_id=run_id,
                    next="Report the canvas progress and stop.")
        return base, False, run_id
    base["error"] = ("unknown run_id %r: pass a run_id mcp__heat__render_tiles returned or a canvas_id "
                     "from mcp__heat__new_canvas" % run_id)
    return base, True, None


ALL_TOOLS.update({"launch_ray": LAUNCH_RAY_TOOL, "ray_status": RAY_STATUS_TOOL, "stop_ray": STOP_RAY_TOOL,
                  "new_canvas": NEW_CANVAS_TOOL, "render_tiles": RENDER_TILES_TOOL,
                  "render_status": RENDER_STATUS_TOOL})


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
                "write_summary": write_summary,
                "load_campaign": load_campaign, "copy_buildcache": copy_buildcache,
                "run_spack": run_spack, "spack_status": spack_status,
                "launch_ray": launch_ray, "ray_status": ray_status, "stop_ray": stop_ray,
                "new_canvas": new_canvas, "render_tiles": render_tiles,
                "render_status": render_status}.get(name) if name in served else None
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
    if len(argv) in (1, 2) and argv[0] == "--campaign":
        res, err, _ = load_campaign({"campaign": argv[1]} if len(argv) == 2 else {})
        print(json.dumps(res, indent=2))
        sys.exit(1 if err else 0)
    if len(argv) == 2 and argv[0] == "--spack-inputs":
        try:
            print(json.dumps(spack_inputs(_prep_target({"target": argv[1]})), indent=2))
        except Rejected as e:
            print(json.dumps({"status": "rejected", "error": str(e)}, indent=2))
            sys.exit(1)
        sys.exit(0)
    if len(argv) in (1, 2) and argv[0] == "--ray-inputs":
        rows, used, skipped = fleet_worker_rows(argv[1] if len(argv) == 2 else None)
        print(json.dumps({"workers": rows, "targets": used, "skipped": skipped}, indent=2))
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
                         "[tol] [max_iter] | --evaluate <run_id> | --list [target] | --campaign [file] | "
                         "--spack-inputs <target> | --ray-inputs [walltime] | "
                         "--compare <mcp_run_id> <shell_run_id>\n")
        sys.exit(2)
    sys.exit(serve())
