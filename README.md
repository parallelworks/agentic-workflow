# pw code HPC Fleet Workshop

A hands-on workshop showing how a fleet of custom agents in
[`pw code`](https://parallelworks.com/docs/ai/code/custom-agents) work together
to drive simulation and modeling across a fleet of HPC clusters — where a
"site" is a Slurm or PBS cluster reached over `pw ssh`.

Agents work in terms of **targets** — a target is one specific way to run on a
cluster (its scheduler directives, CPU/GPU mode, and rank count). One cluster can
have several targets, so a machine with both CPU and GPU nodes is just two
targets sharing one `pw ssh` name, and nothing is auto-selected.

Participants clone this repo, point it at their clusters, and use `pw code` to:

1. **Orchestrate** a simulation campaign across sites.
2. **Analyze portability** by running the same workload on heterogeneous
   clusters and comparing the results.
3. **Support software builds** per site.
4. **Perform capacity analysis** to decide where a CPU or GPU workload should
   run.

The workload is a small, self-contained **2D steady-state heat solver** (MPI,
with an optional GPU build). It's deliberately simple to explain but real enough
to make portability and capacity questions concrete.

## What's an agent here, exactly?

In `pw code`, a *custom agent* is a markdown file (`.agents/agents/*.md`) with
YAML frontmatter and a specialization prompt. There is no orchestrator program —
coordination happens *inside* `pw code`: the main agent reads each definition's
`description` and delegates work to the right subagent. This repo ships four:

| Agent | Role |
|-------|------|
| `campaign-orchestrator` | The orchestrator: expands a sweep, dispatches runs to site-runner, hands completed runs to the evaluator. |
| `site-runner` | Runs the workload on ONE target — stages, builds, submits (one workflow), then polls to done. The cross-site primitive. |
| `evaluator` | After runs finish, fetches + validates on every cluster and analyzes portability (reproducing the solution) and performance (run time) together. |
| `capacity-analyst` | Read-only; recommends which target a job should run as. |

## How agents reach clusters

**One channel: platform-logged workflows.** Every cluster operation — stage
(git clone), build, submit, poll, fetch, validate, capacity — runs through
`pw workflows run` against a template in `./workflows/`. A workflow job with an
`ssh:` block executes its steps on the cluster, and every invocation is logged
on the platform. There is no `scp` and no `pw ssh <command>`; agents never move
files by hand and never touch SSH keys (which keeps them clear of the secrets
policy, and sidesteps a ProxyCommand `scp` bug seen inside containers).

Source reaches the cluster by the **stage workflow git-cloning this repository**
there and checking out the exact commit the agent is running from — delivering
`heat.c`, `Makefile`, `validate.py`, and the reference field together, with the
commit SHA as provenance. Results **stay on the cluster**: a validate workflow
runs `validate.py` there against the delivered reference and returns only the
verdict (relative L2, pass/fail) in the run record.

Each run is staged under `./runs/<agent>-<target>-<NNNNNN>/`, and that same id
is the workflow `--name` and the remote working directory `$HOME/pw-heat/<id>/`, so
a platform run traces back to local artifacts and to a cluster directory.

The git-clone approach assumes a **public repository** (no credentials on the
cluster). The legacy `./scripts/` wrappers (which used `pw ssh` directly) are
kept for comparison but are no longer used.

## Repository tour

```
AGENTS.md                    project instructions pw code reads every session
.agents/
  agents/*.md                the four custom agent definitions
app/heat/
  heat.c                     the MPI heat solver (equal 1-D slabs, halos over MPI)
  Makefile                   cpu + gpu (OpenACC) build targets
  validate.py                compares a run's field to the reference (L2 norm)
  reference/heat_128.ref     golden reference field for grid 128
sites/
  sites.yaml                 THE fleet registry (list of targets) — edit first
  slurm/ , pbs/              LEGACY submit templates + env.sh per scheduler
scripts/
  submit.sh poll.sh fetch.sh capacity.sh   LEGACY pw ssh wrappers (kept for
                             comparison; agents no longer use these)
workflows/
  run/poll/fetch/validate/capacity .yaml + .inputs.json
                             platform-logged workflow templates (the layer
                             agents use for every cluster operation). run =
                             stage+build+submit merged; validate runs on cluster.
runs/                        per-invocation staging (like plan-mode's ./plans);
                             each run: <agent>-<target>-<NNNNNN>/ (gitignored)
campaigns/grid-sweep.yaml    an example campaign
WORKSHOP.md                  facilitator guide: three exercises, timed
```

## Prerequisites

- The **PW CLI** installed and authenticated (`pw auth whoami` works). You can
  run from anywhere authenticated — a laptop, a container, a cluster.
- `pw workflows run` available to you, and permission to run workflows against
  your clusters. Validate a template with `pw workflows run --dry-run` before a
  real run.
- **This repository pushed to a public git remote**, with `git` available on the
  clusters. The stage workflow clones it there; agents autodetect the URL and
  commit from your working copy.
- At least one Slurm or PBS cluster, referenced by its PW URI
  (`pw://<user>/<cluster>`). Two heterogeneous clusters (one CPU, one GPU) make
  the portability and capacity exercises richer, but one works (see the fallback
  ladder).
- An MPI toolchain on each cluster (any MPI + C compiler for CPU; the NVIDIA HPC
  SDK for the GPU build). The build runs on the cluster via a workflow.

## Quick start

1. **Edit `sites/sites.yaml`** to describe your actual **targets**. A target is
   one way to run on a cluster: its `ssh_name` is the cluster's PW URI
   (`pw://<user>/<cluster>`), and its `directives` block holds the literal
   #SBATCH/#PBS lines that cluster needs (partition, QoS, account, walltime,
   node/GPU request — verbatim, the tooling never parses them). Give each
   cluster a separate cpu-mode and gpu-mode target if you use both node types.
   This is the single most important setup step.
2. **Sanity-check the solver locally** (optional, needs MPI):
   ```
   make -C app/heat cpu
   mpirun -np 4 app/heat/heat --grid 128 --tol 1e-3 --max-iter 50000
   ```
   You should see a one-line JSON record with `"converged":true`.
3. **Dry-run a workflow** to validate it against your platform:
   ```
   pw workflows run --dry-run workflows/capacity.yaml
   ```
4. **Launch `pw code`** in this workspace and approve the project definitions.
   The agents will stage runs under `./runs/` and drive clusters through
   `pw workflows run` (see `AGENTS.md` for the full procedure).
5. **Work through `WORKSHOP.md`** — three exercises across the four agents.

## The design in one paragraph

The solver splits the global grid into **equal horizontal slabs**, one per MPI
rank, and exchanges halos **through MPI** every iteration. Every rank runs the
same hardware in a given run (all CPU, or all GPU), so equal slabs finish
together. The rank count is **explicit** — each target's `mpi_ranks` — never
derived from the scheduler allocation, so a target can deliberately use a
fraction of a node. GPU is used only when a target's `mode` is gpu; nothing is
auto-selected from hardware. This keeps today's runs simple (single-node GPU,
multi-node CPU) while leaving a clean path to multi-node multi-GPU later; the
seams are documented in `heat.c` and `WORKSHOP.md`.

## References for the numerics

The Jacobi iteration and its discretization are standard; the code cites:

- R. J. LeVeque, *Finite Difference Methods for Ordinary and Partial
  Differential Equations*, SIAM, 2007.
- Y. Saad, *Iterative Methods for Sparse Linear Systems*, 2nd ed., SIAM, 2003.
- G. H. Golub and C. F. Van Loan, *Matrix Computations*, 4th ed., JHU Press,
  2013.
