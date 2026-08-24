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
explicitly when useful ("use the portability-analyst to compare these runs").

NO SHARED CLUSTER STATE BETWEEN AGENTS. Each agent works in its own run-scoped
remote directory (`$HOME/pw-heat/<agent>-<target>-<NNNNNN>/`) and never reuses
another agent's built binary. site-runner builds its own binary as part of its
run; build-engineer builds only to DIAGNOSE the toolchain and hands off nothing.
The solver compiles in seconds, so this duplication is deliberate — it keeps
agents decoupled and avoids any "did the other agent's build land where I look?"
coupling. Do not add a cross-agent binary handoff.

## How agents run things on a cluster (READ THIS)

**One channel reaches a cluster: `pw workflows run`.** Every operation — stage
(git clone), build, submit, poll, fetch-status, validate, capacity — runs as a
platform-logged workflow from the `./workflows/` templates. There is no `scp`
and no `pw ssh <command>`: agents never move files by hand and never touch SSH
keys. Source arrives on the cluster by the stage workflow cloning the repo;
results stay on the cluster and a validate workflow returns only the verdict.
This keeps every cluster action logged and keeps agents clear of the secrets
policy.

### Getting source onto the cluster (git clone, no file transfer)

The solver source, Makefile, validate.py, and the reference field all reach the
cluster together when the stage workflow clones this repository there. The agent
autodetects what to clone from its own working copy:

- URL:    `git remote get-url origin`
- commit: `git rev-parse HEAD`

Both are workflow inputs, so the user may override URL/branch/commit when needed.
The stage workflow clones the URL and checks out the exact commit, so the cluster
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

1. Resolve the id and `mkdir -p ./runs/<id>/`; copy in the op template(s).
2. Derive from the target: full URI (`ssh_name`) for the `cluster` input,
   `scheduler`, `mode`, `mpi_ranks`, `mpi_launch`, env_load commands, and — for
   submit — the base64 of the `directives` block. Produce the base64 with
   `_site_query.py sites/sites.yaml <target> directives | base64 -w0` (this reads
   the verbatim block; base64 keeps it a single safe token). Get the env_load
   value (verbatim setup commands joined with &&, no prefixing) with
   `_site_query.py sites/sites.yaml <target> env_load_joined`. Autodetect the
   repo URL (`git remote get-url origin`) and commit (`git rev-parse HEAD`).
3. Fill `./runs/<id>/<op>.inputs.json`.
4. STAGE the source: run `stage.yaml` (git clone + checkout the commit into
   `$HOME/pw-heat/<id>/`). Then `build.yaml`, then `submit.yaml`.
5. `pw workflows run -i runs/<id>/<op>.inputs.json --name <id> runs/<id>/<op>.yaml`
   (append a distinct suffix like `<id>-poll` for repeated polls).
6. RESULTS: after the job completes, run `validate.yaml` on the cluster. It runs
   validate.py against the delivered reference and returns the verdict (relative
   L2, pass/fail) in the run record. The field file stays on the cluster. Save
   the returned verdict JSON to `./results/<id>/verdict.json`.

Keep `./runs/` artifacts; they are the audit trail. (`./scripts/` still exists
for comparison with the pre-workflow approach, but agents no longer use it.)

## Targets, not clusters

Agents reference **targets**, not clusters. A target (defined in
`sites/sites.yaml`) is one specific way to run on a cluster. One physical cluster
(one `pw ssh` name, the target's `ssh_name`) can have several targets — e.g. a
cpu-mode target and a gpu-mode target — with completely different scheduler
directives. Each target carries a VERBATIM block of #SBATCH/#PBS lines that the
tooling injects untouched, plus an explicit `mpi_ranks` and `mode`.

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

## Fallback ladder (when capacity or network is tight)

multi-node CPU → single-node CPU (many ranks) → single-node GPU (one rank per
GPU) → single GPU (one rank, no halo exchange). Every rung is the same solver
with a smaller launch and still produces a reference-comparable result.
