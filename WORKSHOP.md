# Facilitator Guide

A ~2-hour workshop. Four exercises, one per capability, each driven by asking
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
      SDK). If it doesn't, that's fine — Exercise 3 is partly about discovering
      exactly that, but you want to know in advance.
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

## Exercise 1 — Orchestrate a campaign across sites (25 min)

**Goal:** run the same study across the fleet with one request.

Ask `pw code`:

> "Use the campaign-orchestrator to run `campaigns/grid-sweep.yaml`."

What to watch for:
- The orchestrator expands the sweep and delegates each run to `site-runner`,
  issuing independent runs in the same turn so sites run in parallel.
- Any campaign entry naming a target that isn't in `sites.yaml` is skipped with
  a reason, rather than guessed at. Every run uses exactly the ranks and
  directives its target declares.
- The summary table: one row per run with site, mode, grid, ranks, iterations,
  wall time, converged, distinct_hosts.

Talking point: the campaign file is the sweep; the agent is the dispatcher.
Adding a site or a grid size is a data change, not a code change.

---

## Exercise 2 — Portability by comparing results (30 min)

**Goal:** prove (or disprove) that the same problem gives the same answer on
different clusters.

Ask `pw code`:

> "Have the portability-analyst run grid 128 on the cpu targets of both
> clusters and compare the results against the reference."

What to watch for:
- The analyst holds the grid FIXED, delegates one run per site to `site-runner`,
  then validates each field with `app/heat/validate.py` against
  `reference/heat_128.ref`.
- The verdict is a **relative L2 difference**, not a vibe. A small nonzero L2
  across different compilers or CPU-vs-GPU is expected; a large L2 is a real
  bug (broken halo exchange, wrong stencil).
- The table separates **correctness** (match the reference?) from
  **performance** (wall times), and the analyst does NOT turn a speed difference
  into a placement recommendation — that's Exercise 4.

Key teaching moment: this is why we chose the heat solver over a
verification-only workload. "Same converged field within tolerance" is a
checkable, quantitative portability claim. Show the negative control if you have
time — corrupt a field and re-validate to see it fail.

---

## Exercise 3 — Software builds per site (20 min)

**Goal:** get the solver building on heterogeneous toolchains and surface the
differences.

Ask `pw code`:

> "Use the build-engineer to build the solver on both sites and report what
> toolchain each one has."

What to watch for:
- The CPU build should succeed on both. The GPU build is attempted only on GPU
  sites; on the CPU-only site its absence is a finding, not a failure.
- Expected real-world findings the agent should surface: the OpenACC build needs
  `nvc` (NVIDIA HPC SDK); older PGI `pgcc` pragmas may need updates; a two-node
  launch needs the right PMI flag.

Talking point: the build-engineer turns "it didn't compile" into a specific,
actionable finding (which flag, which pragma, which module), which is exactly
what you want an agent doing across a fleet you don't hand-tune. Note it is
purely diagnostic — it reports the toolchain and hands off no binary. When you
actually run a job, site-runner builds its own (the solver compiles in seconds),
so build-engineer is for answering "does this build here?" without submitting.

---

## Exercise 4 — Capacity analysis: where should this run? (25 min)

**Goal:** decide placement from real signals, not guesses.

Ask `pw code` two questions:

> "Ask the capacity-analyst which target I should use for a GPU run."

> "And which target should a CPU job go to right now?"

What to watch for:
- The analyst lists the candidate targets from `sites.yaml`, groups those that
  share an `ssh_name` (same cluster, different ways to run), snapshots each
  cluster's queue with the `capacity.yaml` workflow, and recommends a target whose
  mode matches the work and whose cluster is least loaded.
- It matches mode to the request — it never steers a CPU job to a GPU target
  just because GPUs exist.
- It's READ-ONLY: it recommends, it doesn't submit. If participants want the job
  run, that goes back to `site-runner` / `campaign-orchestrator`.
- It offers the **fallback ladder** when capacity is tight.
- It does NOT compute GPU-memory fit — the tool no longer parses directives or
  infers hardware, so whether a problem fits the GPUs a target requested is the
  user's call. A good teaching point: this is the cost of the flexibility you
  asked for — explicit control means explicit responsibility.

Optional capstone: chain it. "Recommend a site, run it there, then have the
results-reviewer audit the run." This shows the full loop — decide, run, verify —
across four agents.

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
and `results-reviewer` flags any run that asked for more nodes than it used. A
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
