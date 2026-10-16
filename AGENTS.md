# Project: HPC Fleet Workshop

This workspace drives a fleet of HPC clusters (Slurm and PBS sites) from
`pw code`. The workload is a 2D steady-state heat solver (MPI, optional GPU) in
`app/heat/`. Everything the agents need to know about the fleet lives in
`sites/sites.yaml`.

---

## ⚠️ Security Policy & Access Restrictions

**STRICT INSTRUCTION FOR ALL AGENTS:** Under no circumstances may any agent
read, access, log, parse, echo, or transmit sensitive files or credentials.
Off-limits without exception:

- Secret and credential files: `.env`, `.env.*`, `*.pem`, `*.key`, SSH private
  keys (e.g. `~/.ssh/id_*`), and Parallel Works credentials (e.g. under
  `~/.pw/` or wherever `pw auth` stores tokens).
- Secret-bearing environment variables (API keys, tokens, passwords). Do not
  print the environment (`env`, `printenv`) or expand such variables into
  commands, job scripts, or output.
- Log files (`*.log`, `.logs/`) that may contain captured secrets.

Never embed a credential, token, or private key into a rendered job script, a
`--dump` field file, a run record, or any report. If a task appears to require a
secret, stop and report what is needed rather than reading or reproducing it.
This policy reinforces `pw code`'s tool/permission guardrails; where a tool would
let an agent reach a secret, this instruction forbids it anyway.

---

## Delegation

The main agent coordinates a team of custom agents defined in
`.agents/agents/`. `pw code` discovers them automatically and routes by each
definition's `description` — so the authoritative statement of what each agent
does lives in its definition file, not here (kept in one place on purpose).
Delegate to the right specialist rather than doing its job yourself; steer
explicitly when useful ("evaluate these runs").

MAIN AGENT — ALWAYS DELEGATE CLUSTER WORK. Check your own tool list first:
- If you do NOT have the Task tool, this rule is not for you. You are a custom
  agent (for example `site-runner` or `evaluator`, possibly launched with
  `pw code --agent <name>`): do the job yourself, exactly as your own
  instructions and the procedure below say. Never refuse or redirect a job
  because of this rule.
- If you DO have the Task tool and no custom agent definition of your own (the
  general main session), never stage, submit, poll, fetch, validate, or compare
  runs yourself, and never run `pw workflows run`. Hand every such request to the
  matching custom agent with Task, one Task per job. When the user names an agent
  ("use site-runner", "use the evaluator"), delegate to exactly that agent, even
  though the procedure below would let you do the work yourself. Put the agent's
  name in `subagent_type`, exactly (e.g. `subagent_type: "evaluator"`); never use
  `"default"` or a general-purpose agent for cluster work, and never rely on the
  name appearing only in the prompt. When a user requests multiple agents, make
  one Task to each requested agent. If you are a general subagent handed a
  request that names an agent, pass it on unchanged to exactly that agent: the
  routing defaults below never replace an agent the request names. The names
  `site-runner`, `site-runner-mcp`, `evaluator`, `evaluator-mcp`,
  `compare-agents`, `campaign-orchestrator` and `capacity-analyst` are agents,
  not procedures. Call Task with `background: false` unless you have other
  independent work to do meanwhile, so you wait for the agent's result and report
  it: in a one-shot `pw code -p` run, a background agent finishes after the
  session has returned and its result reaches no one. If the named agent is not
  listed, stop and say so rather than doing the job yourself.
  The Task prompt passes the user's request as given: the target and the grid,
  plus run ids for an evaluation. Do not invent settings ("default grid size")
  or restate the agent's procedure; the agent has its own instructions. Do not
  ask for extra output such as file contents: ask for the agent's report, which
  names its run id or verdict path. If the
  user gave no grid, use 128 (the only grid with a reference field, and the
  default in `workflows/run.inputs.json`) and say so in the prompt and your
  reply.
  REPORTING: report only results an agent has returned to you. While any Task
  is still outstanding, say which ones are, never "all finished". Before sending
  an agent back to repeat or continue the same work, check its evidence (for a
  poll, `pw workflows runs logs <slug>` of its polls) and tell the user what you
  found (reading logs is read-only and allowed). Do not start work the user did
  not ask for (for example evaluating finished runs): offer it and wait for an
  answer.
Only `pw code --agent <name>` guarantees which agent runs; naming an agent in a
prompt is a request the main session should honor.

TWO RUN AGENTS, ONE DEFAULT. Both do the same work (same `run.yaml`, same
inputs resolved through `scripts/_site_query.py`) and differ only in HOW the run
is invoked:
- `site-runner-mcp` — the DEFAULT. Uses two MCP tools (`.agents/mcp/heat_server.py`,
  scoped to this agent only): `run_heat` stages, builds and submits in code;
  `poll_heat` runs `poll.yaml` until the job is DONE. No shell at all.
- `site-runner` — the comparison baseline; builds the `pw workflows run` invocation
  in shell. Use it ONLY when the user names it explicitly.
ROUTING RULE (main session only): send any run request to `site-runner-mcp` unless `site-runner` is
named. One intentional difference: `run_heat` rejects `grid < mpi_ranks` up front
(no job submitted); the shell path would submit a job that MPI_Aborts.

TWO EVALUATORS, ONE DEFAULT, the same way. Both run the same `fetch.yaml` and
`validate.yaml` against the run agent's remote dir:
- `evaluator-mcp` — the DEFAULT. One MCP tool, `evaluate_run` (same server,
  launched with `--tools evaluate_run` so this agent sees only that tool), polls
  once to confirm the job left the queue, fetches, validates, audits the run
  against its target in code, and always writes the verdict.json. No shell.
- `evaluator` — the comparison baseline; builds the invocations in shell. Use it
  ONLY when the user names it explicitly.
ROUTING RULE (main session only): send any evaluation request to `evaluator-mcp` unless `evaluator`
is named. Intentional differences: `evaluate_run` refuses to fetch a job still in
the queue, and also flags non-convergence and a run whose `directives_b64` is not
the target's directives.

TWO HYPOTHESES, TWO AGENTS. The evaluators test the APPLICATION across a
site-to-site change (portability, performance). `compare-agents` tests the
MCP-enabled AGENTS against their shell siblings across a model-to-model change
(parity of work, actions, results, cost). It runs post-hoc, after all model switching is done, and is kept separate
from `campaign-orchestrator`, which needs one model for a whole campaign.

CLUSTER STATE BETWEEN AGENTS. The run agent builds its own binary as part of its
run (stage+build+submit in one workflow); the solver compiles in seconds, so
that is cheap and self-contained. The evaluator does NOT run its own job — it
reads the results the run agent already produced, so when the evaluator fetches
and validates it uses the RUN AGENT's run id and remote dir
(`$HOME/pw-heat/<run-agent>-<target>-<NNNNNN>/`), where the field dump waits
under `app/heat/out/`. That is the one intended cross-agent read: the evaluator
consumes the run agent's output, it does not rebuild or resubmit. No agent reuses
another's built binary to run a new job.

## How agents run things on a cluster (READ THIS)

**One channel reaches a cluster: `pw workflows run`.** Every operation — run
(git clone + build + submit, merged), poll, fetch, validate, capacity — runs as
a platform-logged workflow from the `./workflows/` templates. There is no `scp`
and no `pw ssh <command>`: agents never move files by hand and never touch SSH
keys. Source arrives on the cluster inside the run workflow (its first step git-
clones the repo); results stay on the cluster and the validate workflow returns
only the verdict.
This keeps every cluster action logged and keeps agents clear of the secrets
policy.

### Getting source onto the cluster (git clone, no file transfer)

The solver source, Makefile, validate.py, and the reference field all reach the
cluster together when the run workflow's first step clones this repository there.
The agent autodetects what to clone from its own working copy:

- URL:    `git remote get-url origin`
- commit: `git rev-parse HEAD`

Both are workflow inputs, so the user may override URL/branch/commit when needed.
The run workflow clones the URL and checks out the exact commit, so the cluster
runs precisely the code the agent introspected (provenance by SHA).

ASSUMPTION: the repository is public, so the clone needs no credentials on the
cluster. A private repo would reintroduce credential handling and is out of
scope for this workshop.

### The run identifier and the ./runs staging area

Every workflow invocation gets a unique id: `<agent-name>-<target-name>-<NNNNNN>`
where NNNNNN is a 6-digit zero-padded integer (first free number per
agent+target). That single id is used three ways for end-to-end traceability:
the local staging dir `./runs/<id>/`, the `--name <id>` of the workflow run (so
the platform run traces back to the artifacts), and the remote working directory
`$HOME/pw-heat/<id>/` on the cluster. Note the `remote_dir` workflow input is a
RELATIVE path (`pw-heat/<id>`, no `~` and no leading `/`); the workflow steps
anchor it to `$HOME` themselves. Do not put `~` in `remote_dir` — a literal tilde
is not expanded and becomes a directory named `~`.

Before running a workflow, STAGE it: create `./runs/<id>/`, copy the relevant
`./workflows/<op>.yaml` and `<op>.inputs.json` into it, and fill in every
`<...>` placeholder in the JSON from the target's fields in `sites/sites.yaml`.
This mirrors how plan-mode keeps planning under `./plans/` — all workflow
artifacts are staged and inspectable, anticipating a future `run-mode`. Never
run a workflow straight from `./workflows/` with ad-hoc inputs; stage first so
there is a durable record of exactly what was run.

### The end-to-end run procedure (a cluster job)

This procedure is for the custom agents named below: if you are site-runner or
the evaluator, it is YOUR procedure; do it. The general main session delegates
instead (see "MAIN AGENT — ALWAYS DELEGATE CLUSTER WORK"). The run agent does the RUN and POLL; the evaluator does the FETCH and VALIDATE
afterward. Both stage local artifacts under `./runs/<id>/` with their own id.

site-runner (the shell path), for a target + grid:

1. Resolve the id `site-runner-<target>-<NNNNNN>` (first free number from
   `ls runs/`) and claim it with `mkdir runs/<id>` (no `-p`, so a taken id fails
   instead of being silently reused); copy in `run.yaml` and `poll.yaml` (+ their inputs).
2. Derive from the target: full URI (`ssh_name`) for the `cluster` input,
   `scheduler`, `mode`, `mpi_ranks`, `mpi_launch`, env_load, and the base64 of
   the `directives` block. Produce the base64 with
   `_site_query.py sites/sites.yaml <target> directives | base64 -w0`, and the
   env_load with `_site_query.py sites/sites.yaml <target> env_load_joined`.
   Autodetect repo URL (`git remote get-url origin`) and commit
   (`git rev-parse HEAD`). Set `remote_dir` to `pw-heat/<id>` (RELATIVE; the
   workflow anchors it to `$HOME`).
3. Fill `./runs/<id>/run.inputs.json`, then RUN (stage+build+submit in one):
   `pw workflows run -i runs/<id>/run.inputs.json --name <id> runs/<id>/run.yaml`.
   Capture the scheduler `job_id`.
4. POLL: `pw workflows run -i runs/<id>/poll.inputs.json --name <id>-poll-N
   runs/<id>/poll.yaml` until state is DONE.

MCP PATH: site-runner-mcp's `run_heat` call performs steps 1–3 in code (id
`site-runner-mcp-<target>-<NNNNNN>`, same staging layout, plus `submit.json` and
`rpc.jsonl` as its action record); `poll_heat` performs step 4 (same `poll.yaml`,
one logged run per poll, recorded in `polls.jsonl`). Note `pw workflows run`
returns as soon as a run is created; a poll's state line is only visible via
`pw workflows runs logs <slug>`.

the evaluator, once the run agent reports a run is DONE, for each such run:

5. Stage `./runs/evaluator-<target>-<NNNNNN>/` with `fetch.yaml` + `validate.yaml`
   (one evaluator id per evaluated run; NNNNNN is the first free number from
   `ls runs/`, unrelated to the run id's number).
   IMPORTANT: set `remote_dir` to the RUN AGENT's dir — `pw-heat/<run-id>`, e.g.
   `pw-heat/site-runner-mcp-<target>-<NNNNNN>` — because that is where the field
   dump and run record live. The evaluator reads the run agent's output; it does
   not rebuild.
6. FETCH: run `fetch.yaml` (`--name evaluator-...-fetch`) for the JSON run record.
   VALIDATE: run `validate.yaml` (`--name evaluator-...-validate`); it runs
   validate.py on the cluster against the delivered reference and returns the
   verdict (relative L2, pass/fail). ALWAYS save `./results/<evaluator-id>/verdict.json`
   holding the run id, target, grid, the commit from the run's `run.inputs.json`
   (the solver's record has none), both workflow slugs, and the fetch record and
   validate verdict verbatim, plus `integrity_flags`. compare-agents reads these
   files. The validate `tol` (default 1e-6) is the relative-L2 pass tolerance,
   not the solver's tol. A run passes only if it ALSO matches its target: record
   `ranks` == the run's `mpi_ranks`, same `mode`, and `distinct_hosts` == the
   nodes its directives request. The solution is identical at any rank count, so
   a job whose directives were ignored still validates; only this check catches it.

MCP PATH: evaluator-mcp's `evaluate_run(run_id)` performs steps 5–6 in code (id
`evaluator-mcp-<target>-<NNNNNN>`, same staging plus `poll.yaml`), checks ranks,
mode, nodes (exact `--nodes=N`/`-N N`/`select=N` lines only, for the audit),
convergence and directives parity, and writes `results/<id>/verdict.json` with
`status`, `overall` (PASS/FAIL/NOT EVALUATED), `integrity_flags` and `checks`.

Keep `./runs/` artifacts; they are the audit trail. (`./scripts/` still exists
for comparison with the pre-workflow approach, but agents no longer use it.)

SETUP FOR THE MCP AGENTS: approve Workspace Trust at the first `pw code` launch,
or `site-runner-mcp`, `evaluator-mcp` and their servers do not load (`-p` runs
silently skip unapproved config). Pre-approve their tools for one-shot runs:
`--allowedTools mcp__heat__run_heat --allowedTools mcp__heat__poll_heat` and
`--allowedTools mcp__heat__evaluate_run`. compare-agents uses three tools from the
same server (`--tools list_runs,compare_runs,write_summary`): `compare_runs`
pairs runs with each evaluator's NEWEST verdict and session costs in code, claims
its report directory, and writes comparison.json and every table of report.md;
the model only adds a summary with `write_summary`. Pre-approve `--allowedTools
mcp__heat__list_runs --allowedTools mcp__heat__compare_runs --allowedTools
mcp__heat__write_summary --allowedTools 'Bash(ls:*)' --allowedTools 'Bash(pw
workflows runs:*)'`.
SESSION DIGESTS: `SubagentStop`/`SessionEnd` hooks in `.agents/settings.json`
run `.agents/hooks/agent_digest.py`, which writes a redacted
`runs/<id>/agent-digest.json` (tokens, model, turns, tool-call sequence; no
message text) for each run or evaluation a session created (one evaluator
session usually creates several ids, all with the same session digest). Enable once with `/hooks trust`.
compare-agents reads these digests; no agent reads the pw code session store.
SETUP FOR THE SHELL AGENT: `-p` runs auto-deny unapproved shell commands, so a
one-shot site-runner run needs its commands pre-approved on the command line
(`--allowedTools` is per launch; nothing is stored):
  pw code -p "Use site-runner to run the heat job on target <t> with grid <g>." \
    --agent site-runner -o json \
    --allowedTools 'Bash(pw workflows run:*)' --allowedTools 'Bash(pw workflows runs:*)' \
    --allowedTools 'Bash(python3 scripts/_site_query.py:*)' \
    --allowedTools 'Bash(git remote get-url:*)' --allowedTools 'Bash(git rev-parse:*)' \
    --allowedTools 'Bash(mkdir runs/*)' \
    --allowedTools 'Bash(cp workflows/run.yaml:*)' --allowedTools 'Bash(cp workflows/poll.yaml:*)' \
    --allowedTools 'Bash(cp workflows/run.inputs.json:*)' --allowedTools 'Bash(cp workflows/poll.inputs.json:*)' \
    --allowedTools 'Bash(ls:*)' --allowedTools 'Bash(base64:*)' --allowedTools 'Bash(sleep:*)'
A rule's fixed part must end on a WHOLE WORD (tested on pw v7.122):
`Bash(mkdir -p runs/:*)` does NOT match `mkdir -p runs/x`, while
`Bash(cp workflows/poll.yaml:*)` matches. A glob rule is the alternative:
`Bash(mkdir runs/*)` allows `mkdir runs/x` but denies `mkdir -p runs/x`, which
keeps the id claim strict (a prefix rule `Bash(mkdir:*)` would also allow `-p`). Name whole template files rather than
allowing all of `cp` (which could copy credentials into the workspace). Every part
of a `&&`/`|` compound must match a rule. `ls` is REQUIRED, not a convenience:
GlobSearch skips git-ignored paths, so it cannot see `runs/` or `results/`, and
`ls runs/` is the only way to find the first free run number (site-runner and the
evaluator have no GlobSearch for this reason). The evaluator needs the same
`mkdir runs/*` rule. `sleep` lets it
space polls (poll_heat waits server-side); `pw workflows runs` lets it read a
poll's state line from the run logs.
Hooks as of pw v7.122: interactive sessions fire SessionEnd; one-shot `-p` runs
fire SubagentStop for a DELEGATED agent (so a run requested through the main
session gets its digest) but never SessionEnd (so `--agent` runs do not). After a
`-p --agent` run the HUMAN backfills the digest with `python3 -I .agents/hooks/agent_digest.py --transcript
~/.local/state/pw/code-sessions/<session_id>.jsonl` (session_id is in `-o json`).
`runs/_mcp/hooks.jsonl` gets one line each time a hook actually fires.

## Targets, not clusters

Agents reference **targets**, not clusters. A target (defined in
`sites/sites.yaml`) is one specific way to run on a cluster. One physical cluster
(one `pw ssh` name, the target's `ssh_name`) can have several targets — e.g. a
cpu-mode target and a gpu-mode target — with completely different scheduler
directives. Each target carries a VERBATIM block of #SBATCH/#PBS lines that the
tooling injects untouched, plus an explicit `mpi_ranks` and `mode`.

`scheduler: none` is a target with NO batch system: `run.yaml` runs the job in
the foreground on the node the target's `ssh_name` reaches (the login/head
node), under the target's `timeout` (default 00:10:00), with `<mpi_launch> -np
<mpi_ranks>`. Its directives are REQUIRED but are `#` comment lines only (e.g.
`# NO SCHEDULER - directive placeholder`). Everything else is the same: the run
prints `SUBMITTED scheduler=none job_id=<host>:<pid>` once launched, `poll.yaml`
reports RUNNING until the process exits and then DONE, and fetch/validate are
unchanged. cpu and gpu modes and any `mpi_ranks` are allowed (a cloud head node
may be yours alone, with GPUs). A none run uses exactly 1 host. Launcher flags
are the target's: OpenMPI counts physical cores as slots, so a target that
needs more ranks puts `--use-hwthread-cpus` or `--oversubscribe` in `mpi_launch`. For such a target
the run agent adds `timeout` to `run.inputs.json` from
`python3 scripts/_site_query.py sites/sites.yaml <target> timeout` (only when
that prints a value).

## Ground rules

- Reach a target's cluster via `pw ssh <ssh_name>`. There is no inbound-port
  server; the cross-site pattern is submit/poll/fetch over SSH, wrapped in
  `scripts/`.
- `sites/sites.yaml` is the single source of truth. The tooling does NOT parse a
  target's directives, derive ranks from the allocation, or infer hardware.
  Whatever the target says is what runs.
- GPU is used only when a target's `mode` is gpu. Never steer a job to GPU
  because a cluster has GPU nodes — a cluster with both node types simply has
  separate cpu and gpu targets. Pick the target that matches the work.
- The solver runs exactly a target's `mpi_ranks`. It never autofills to the
  node; a target may deliberately use a fraction of its allocation.
- Hold the global grid size FIXED when comparing targets. Comparing runs of
  different sizes proves nothing about portability.
- A deliberate single-node target is valid. Only flag a mismatch between nodes a
  target's directives request and nodes actually used (`distinct_hosts`).
- Never modify `heat.c` to force a build or make results match. Never edit a
  target's directives to "fix" a run. Report obstacles; those are human
  decisions.
- Agents write files only in their own `runs/<id>/` and `results/<id>/`
  directories. No scratch files: never `/tmp` (not shared across nodes and not
  part of the audit trail) and never the repo root. Values from sites.yaml come
  from `scripts/_site_query.py`, piped straight into `base64 -w0` for the
  directives; never retype a target's directives by hand.

## Fallback ladder (when capacity or network is tight)

multi-node CPU → single-node CPU (many ranks) → single-node GPU (one rank per
GPU) → single GPU (one rank, no halo exchange). Every rung is the same solver
with a smaller launch and still produces a reference-comparable result.
