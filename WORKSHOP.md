# Facilitator Guide

A ~2-hour workshop. Three exercises across the four agents, each driven by asking
`pw code` in plain language and letting it delegate to the custom agents. Times
are guidance for a group that has `pw code` working and `sites.yaml` filled in.

## Before the session (facilitator checklist)

Do this in advance — it is where workshops actually go wrong:

- [ ] The **PW CLI** is authenticated for every participant (`pw auth whoami`),
      and each can run `pw workflows run` against the clusters.
- [ ] `pw workflows run --dry-run workflows/capacity.yaml` validates for each
      participant. Do a real `capacity.yaml` run against one cluster in advance —
      it's read-only and confirms the ssh/compute-clusters wiring end to end.
- [ ] This repo is **pushed to a public git remote** and `git` is available on
      the clusters (the stage workflow clones it there).
- [ ] `sites/sites.yaml` defines your real **targets**: each target's `ssh_name`
      as a PW URI (`pw://<user>/<cluster>`), scheduler, mode, explicit
      `mpi_ranks`, and its verbatim `directives` block. The submit workflow
      injects these untouched, so a bad directive line fails exactly as it would
      by hand. Give clusters with both node types separate cpu and gpu targets.
- [ ] The CPU build compiles on each site: `make -C app/heat cpu`.
- [ ] On GPU sites, the GPU build compiles: `make -C app/heat gpu` (NVIDIA HPC
      SDK). If it doesn't, that's fine — site-runner's run workflow surfaces the
      build failure in its `=== BUILD ===` phase, but you want to know in advance.
- [ ] A **two-node** CPU job actually launches on two nodes on at least one site.
      Test the MPI launch flag (`srun --mpi=pmix` or the site's mpirun/PMI). A
      wrong PMI flag is the most common reason a "2-node" job silently runs on
      one.
- [ ] Regenerate the reference field if you change the grid default:
      `mpirun -np 1 app/heat/heat --grid 128 --tol 1e-3 --max-iter 50000
       --dump app/heat/reference/heat_128.ref`

## Opening (10 min)

Frame the three ideas participants need:

1. **A custom agent is a markdown file, not a program.** Open
   `.agents/agents/site-runner.md`. Point out the frontmatter (tools,
   permissionMode) and the body (the specialization prompt). Orchestration is
   the main agent delegating by `description`.
2. **The cross-site primitive is `pw workflows run`**, not raw ssh. Every
   operation on a cluster (stage, build, submit, poll, fetch, validate,
   capacity) runs as a platform-logged workflow from `./workflows/`, staged
   per-invocation under `./runs/<agent>-<target>-<NNNNNN>/`. Source arrives by
   git clone; results are validated on the cluster and only the verdict returns.
   No scp, no SSH keys. `site-runner` is the only agent that drives a full job;
   everyone else delegates to it. The point: auditability — each cluster action
   is a traceable run.
3. **Agents target "targets," not clusters.** Open `sites/sites.yaml`. A target
   is one way to run on a cluster: an `ssh_name`, a `mode` (cpu/gpu), an
   explicit `mpi_ranks`, and a VERBATIM `directives` block the tool injects
   untouched. One cluster can have several targets (e.g. `bigcluster-cpu` and
   `bigcluster-gpu` share `ssh_name: bigcluster`). Nothing is inferred: GPU runs
   only via a gpu-mode target, ranks are exactly `mpi_ranks`, and the scheduler
   directives are yours to own. This is what lets messy per-cluster QoS/account/
   queue options live in one place without the tool trying to be clever.

---

## Exercise 1 — Capacity analysis: which target? (20 min)

**Goal:** decide placement from real signals, not guesses.

Ask `pw code`:

> "Ask the capacity-analyst which target I should use for a GPU run, and which
> for a CPU job right now."

What to watch for:
- The analyst lists candidate targets from `sites.yaml`, groups those sharing a
  cluster URI (same cluster, different ways to run), snapshots each cluster's
  queue with the `capacity.yaml` workflow, and recommends a target whose mode
  matches the work and whose cluster is least loaded.
- It matches mode to the request — never steers a CPU job to a GPU target just
  because GPUs exist.
- It's READ-ONLY: it recommends, it doesn't submit.
- It does NOT compute GPU-memory fit — the tooling never parses directives or
  infers hardware, so whether a problem fits is the user's call. Teaching point:
  explicit control means explicit responsibility.

---

## Exercise 2 — Orchestrate a campaign across the fleet (30 min)

**Goal:** run the same study across the fleet with one request.

Ask `pw code`:

> "Use the campaign-orchestrator to run `campaigns/grid-sweep.yaml`."

What to watch for:
- The orchestrator expands the sweep and delegates each run to `site-runner`,
  issuing independent runs in the same turn so targets run in parallel.
- Each `site-runner` run is ONE workflow (`run.yaml`) that git-clones the repo on
  the cluster, builds the solver, and submits — then polls to DONE. Watch the
  `=== STAGE ===`, `=== BUILD ===`, `=== SUBMIT ===` phase markers in the run log;
  if a run fails, they show exactly which phase broke.
- Any campaign entry naming a target not in `sites.yaml` is skipped with a
  reason. Every run uses exactly the ranks and directives its target declares.

Talking point: the campaign file is the sweep; the orchestrator is the
dispatcher; `site-runner` is the one agent that touches a cluster. Adding a
target or a grid size is a data change, not a code change.

---

## Exercise 3 — Evaluate: portability AND performance (30 min)

**Goal:** once the runs are done, prove (or disprove) that the clusters reproduce
the same solution, and compare their run times — in one evaluation.

Ask `pw code`:

> "Now have the evaluator fetch and validate all the completed runs, and report
> portability and performance."

What to watch for:
- The evaluator waits for `site-runner` to confirm runs are DONE, then does the
  fetch and validate itself — on every run, reading each one from *site-runner's*
  remote directory where the field dump lives.
- Validation runs `validate.py` ON the cluster against the reference the run
  workflow delivered; only the verdict (relative L2, pass/fail) comes back. The
  field never leaves the cluster.
- **Portability**: a small nonzero L2 across different compilers or CPU-vs-GPU is
  expected; a large L2 is a real bug (broken halo exchange, wrong stencil). The
  evaluator also confirms each run actually converged (didn't just hit the cap).
- **Performance**: wall times compared across targets and CPU vs GPU, descriptive.
- **Integrity**: multi-node honesty (`distinct_hosts` vs what the directives
  requested) and provenance (same commit SHA across a comparison).

Key teaching moment: this is why we chose the heat solver — "same converged field
within tolerance" is a checkable, quantitative portability claim, and the
evaluator reports correctness and performance together rather than in isolation.
Show the negative control if time allows — a run that hit the iteration cap
without converging is flagged, not silently accepted.

---

## Closing: the fallback ladder and honesty checks (10 min)

Two ideas worth landing:

**Fallback ladder.** When network or node availability is bad, turn the knob
down — the same solver runs at every rung:

```
multi-node CPU  ->  single-node CPU (many ranks)  ->  single-node GPU (1 rank/GPU)  ->  single GPU (1 rank)
```

Every rung produces a reference-comparable result, so a fallback run is still a
valid workshop result.

**Multi-node honesty.** "MPI ran with N ranks" does not prove N nodes. The
solver's rank→host roll-call (`distinct_hosts`, `hosts[]`) is the ground truth,
and the evaluator flags any run that asked for more nodes than it used. A
deliberate one-node run is valid; a *silent* collapse to one node is the bug.

---

## Appendix: the future multi-node multi-GPU seams

This repo runs single-node GPU and multi-node CPU today, by design. It
generalizes to multi-node multi-GPU later **without restructuring**, because
multi-node multi-GPU is just "more equal slabs." The seams (all in
`app/heat/heat.c`, marked `SEAM 1/2/3`):

- **SEAM 1 — halos always go through MPI.** Never a device-to-device peer copy.
  This is what makes the jump a *build* change (link a GPU-aware MPI), not a
  rewrite. Peer copies don't cross node boundaries; do not introduce them.
- **SEAM 2 — one rank per worker.** CPU: one rank per core. GPU: one rank per
  GPU, bound by `rank % num_devices`. Multi-node multi-GPU is nodes × GPUs ranks
  with the same decomposition and kernel.
- **SEAM 3 — GPU-aware halo buffers.** With a CUDA-aware MPI, send/recv buffers
  can be device pointers passed straight to MPI (ideally GPUDirect RDMA).

The migration later reads: keep the solver source; rebuild against a GPU-aware
MPI; change submit templates from one node to K with per-node GPU binding; widen
the roll-call assertion from devices-on-one-node to devices-across-nodes;
re-tune tile size for the network. In `sites.yaml`, that's editing a gpu
target's `directives` block to request multiple nodes with per-node GPU binding,
raising its `mpi_ranks`, and ensuring its `env_load` brings up a CUDA-aware MPI — all
values in one target, no schema change.

Why it's deferred (all real, none about code readiness): the software stack
deepens (GPU-aware MPI built against the right CUDA + fabric), performance — not
correctness — becomes the trap (the interconnect must keep up with GPUs that
compute halos faster than the network moves them), and multi-node multi-GPU
allocations are scarce and expensive.
