# HANDOFF: open items

What is still open after the MCP-enabled agents (`site-runner-mcp`,
`evaluator-mcp`, `compare-agents`) were built and compared with their shell
siblings. Test results are in `results/agent-summary.md`.

1. **`scheduler: none` gaps:**
   - gpu mode is untested. The `lab-gpu` target is blocked on its toolchain:
     `make gpu` needs `mpicc` wrapping NVIDIA's `nvc` (OpenACC `-acc
     -Minfo=accel`). A `+cuda` OpenMPI built with gcc fails with `gcc: error:
     unrecognized command-line option '-acc'`, and `nvcc` (CUDA Toolkit) does not
     compile OpenACC. Next: install Spack's `nvhpc +mpi`, choosing a release whose
     bundled CUDA the node's GPU driver supports (`nvidia-smi`), and load it in
     the target's `env_load`. Do not fall back to GCC `-fopenacc` without NVPTX
     offload: it builds, runs every `acc` region on the CPU, and still validates.
   - Targets without environment modules use Spack directly in `env_load`:
     `eval "$(<spack-root>/bin/spack load --sh /<hash>)"`. Call `spack` by path
     (the workflow shell never sources `setup-env.sh`), use `"$(...)"` and no
     single quotes (run.yaml writes `env_load` inside single quotes), and pick by
     hash when a spec matches several installs. Under run.yaml's `set -u`, an
     unset variable in `env_load` (e.g. `$MODULEPATH`) silently ends the step;
     write `${VAR:-}`.
   - `capacity.yaml`'s `none` branch has only been run locally, not through a
     workflow.
   - Long jobs relying on `run_heat` returning at the `SUBMITTED` line are
     untested. pw also cuts off a subagent that "stopped making progress for
     15m0s" (Q20), which a campaign with a long job hits: run big grids with
     `site-runner-mcp` directly and evaluate in a separate session.
   - Clusters with several login nodes behind one name are untested; poll
     reports UNKNOWN there by design.
2. **Gaps in the cost logging:**
   - A subagent stopped by the user fires no `SubagentStop`, so its digest goes
     stale.
   - An agent that creates no run id gets no digest.
   - The main session's own cost is never captured.
   - Digests leave out the session-naming request and `cached_tokens`, so a
     digest total is a few hundred tokens below the `pw code -o json` total.

   Possible fix: have the hook also write a per-session digest under
   `runs/_mcp/`.
3. **Questions for the pw developers** (pw code v7.131.0, gpt-oss-20b on vLLM).
   Q7 was asked on 2026-11-09; the answer is pending.

   *Tool calling with gpt-oss on vLLM*
   1. Garbled tool names (`Task<|channel|>commentary`, `Taskjson`, and since
      2026-11-09 MCP names such as `mcp__heat__load_campaign<|channel|>commentary`)
      get `unknown tool`; the rate ranged from 1 in 6 to 13 in 15 calls on one
      endpoint. Could pw accept a known name plus a harmony suffix, or is it a
      vLLM `--tool-call-parser` setting?
   2. Can a session or agent be capped on consecutive calls to tools that don't
      exist? Small models ignore stop rules (13 failed calls in a row once).
   3. gpt-oss sometimes ends a turn empty; `-p` prompts it to continue,
      interactive mode doesn't.
   4. Is there a model request timeout/retry setting? A cold model's first
      request takes about 12 minutes; requests time out after about 5.
   5. The vLLM job's 2-hour limit changes the endpoint id, so saved model ids and
      `-r` resume lines go stale. Is there a stable alias?

   *Delegation and tool restriction*
   6. Task accepts `subagent_type: "default"` for a request naming an agent. Can
      Task be limited to named agents (a `Task(agent-name)` rule) and `default`
      disabled?
   7. Is there a guaranteed way to delegate other than `--agent`? (asked)
   8. Confirm the observed design: a custom agent's tools restrict every subagent
      below it at any depth, including `default` (`unknown tool "Bash"`), and the
      set is the tools the session HAS, not the names in `tools:`. Intended,
      documented and stable?
   9. Subagents use the parent's MCP server process rather than their own
      `mcpServers` entry (unless the parent has no such server), so the parent
      must serve, and can call, its children's tools. Intended? Can a tool be
      reachable by a child but not its parent?
   10. At the `subagentDepth` limit Task fails as `unknown tool "Task"`; could the
       error name the depth limit?
   11. Task needs no approval, even in `-p` without `--allowedTools Task`.
       Intended?

   *Hooks, transcripts and `-p` output*
   12. One-shot `-p --agent` runs still fire no `SessionEnd` (v7.131), so every
       digest is backfilled by hand.
   13. A subagent stopped by the user, or in a cancelled session, fires no
       `SubagentStop`.
   14. Transcripts record `agent_type` and `depth` but not the tools an agent was
       offered. Could they?
   15. `-p` text mode prints progress only to a TTY and never shows successful
       Task calls; `-o json` has no tool-call list. Could it?
   16. Is the `MCP tool error: <json>` prefix on `isError` results a stable
       format?

   *Trust and permissions*
   17. Editing one agent file makes `-p` ignore every workspace config file
       (`unknown agent ...`) until trust is re-approved. Could trust be per file?
   18. Is the subagent tool restriction (Q8) guaranteed under
       `--permission-mode bypass-permissions`? We rely on it.
   19. Are `permissions.deny` rules honoured in bypass mode?

   *Long-running subagents*
   20. On 2026-11-12 (session 20261112-183218-2e4c6896, Opus) pw ended two
       subagents with `[subagent cut off by an error before finishing its task:
       subagent stopped making progress for 15m0s]`: a `site-runner-mcp` still
       calling `poll_heat` as instructed (each call waits up to 7 minutes; its
       last one was 30 s before the cutoff), and one second later the
       `orchestrator-mcp` waiting on it in a foreground Task (its previous Task
       had returned about 17 minutes earlier). Neither fired `SubagentStop`, and
       the orchestrator never reached its evaluation step. What counts as
       progress? Is the 15-minute limit configurable per agent or per session?
       Should a parent waiting on a foreground Task whose child is still working
       count as stalled? Hours-long jobs (grid 2048) need a supported way to wait.
       2026-11-13 (session 20261113-142741-bd20623b): a `site-runner-mcp`
       DELEGATED from the main session by a foreground Task (task-104, depth 1;
       azure-cpu, grid 2048, run site-runner-mcp-azure-cpu-000015) stayed
       active for 4.1 hours (16:00-20:08) through 34 `poll_heat` calls (each up
       to ~7 min), and the main session waiting on that Task was not cut off
       either. So a new tool call evidently counts as progress, the repeated-
       poll pattern works, and a MAIN session may wait on a long foreground
       Task. (An earlier note here called this session standalone; it was
       delegated.) Narrows Q20 to a SUBAGENT parent (orchestrator-mcp, depth 1)
       waiting on a depth-2 child, the 2026-11-12 case.
       `maxTurns` may not be a firm limit: that session's digest records 41
       turns against `maxTurns: 40` (35 tool calls: 1 `run_heat`, 34
       `poll_heat`) and it still finished normally and reported. How pw counts
       turns against `maxTurns`, and whether it ends the session exactly at the
       limit, is unconfirmed. Plan on about 34-38 polls (roughly 4-4.4 hours)
       per session (`.agents/mcp/README.md`, "Long jobs: how polling keeps the
       session alive").
4. **Older backlog:**
   - `write_summary` should reject numbers that aren't in `comparison.json`.
   - The procedure is duplicated between `site-runner.md` and AGENTS.md, and
     the copies are drifting; consolidate.
   - `orchestrator-mcp` now exists alongside `campaign-orchestrator` and ran
     `campaigns/tiny.yaml` end to end on gpt-oss-20b (run
     `site-runner-mcp-laptop-cpu-000010`, PASS as `evaluator-mcp-laptop-cpu-000007`).
     Verified in `-p`, and again under `--permission-mode bypass-permissions`
     (session 20261104-225140-28f02235): its tool list restricts every subagent
     at any depth, including `default` (`unknown tool "Bash"`), and Task needs no
     approval. Open: the orchestrator (or any subagent) could still call
     `run_heat` directly, which only the prompt prevents; `permissions.deny`
     behaviour is untested. Its unknown-tool stop limit is 3 (20b's third Task
     attempt after two garbled ones succeeded in the tiny.yaml run).
   - `agent_digest.py` (2026-11-09): a heat call now fails only on status
     `rejected`/`failed`/`not_ready` (`loaded`, `listed`, `compared`, `written`
     no longer count as failures); Task keeps `subagent_type` and `background`;
     pw's own one-line errors (`Error: unknown tool "..."`, `Error executing
     Task: ...`) are kept, truncated to 300 chars. A heat result pw prefixes
     with `MCP tool error: ` is now parsed (it was recorded as ok with no status
     or run_id, so failed and rejected runs got no digest). Still: a Task whose subagent
     report starts with "Error" counts as failed. Next: a per-session digest
     (item 2), which is the only way an orchestrator session gets a digest.
     2026-11-10: tool results stored as a list of text parts (claude-code/opus)
     are now parsed (before, Opus sessions got no digest and no failure
     detection), and `run_spack`/`copy_buildcache` ids get site-prep-mcp
     digests. Backfilled for laptop-cpu-000021, lab-cpu-000011,
     evaluator-mcp-laptop-cpu-000014, -lab-cpu-000006 and
     site-prep-mcp-gce-cpu-000001.
   - Hooks re-trust (waiting on a colleague).
   - A future agent that fills in `sites.yaml` by inspecting existing clusters.
5. **Ray fleet + fractal app** (branch `add-ray-launcher`, 2026-11-13): new agents
   `ray-launcher-mcp` and `ray-runner-mcp`, heat_server v0.10.0, `workflows/ray.yaml`
   and `workflows/fractal.yaml`, and `app/fractal/`. This was tested offline only: a
   fake `pw`, plus a local Ray cluster shaped like ray-cluster's with the upstream
   dashboard (see `.agents/mcp/README.md`). **Spike before the first agent run:**
   hand-run `launch_ray` (or `pw workflows run` a staged `ray.yaml`) with one slurm
   and one none target, then confirm:
   - DONE 2026-11-13 (run workable-ringtail, a hand-run of the first `ray.yaml`, which
     called ray-cluster as a subworkflow, with lab-cpu + azure-cpu): the list-typed
     `workers` passed through intact and `head.resource: workspace` resolved, but the
     head failed with `start_ray_head.sh: No such file or directory` (status 127)
     and the run looped in `wait_for_endpoint` until the session runner gave up.
     Cause and fix: see "Workflow Writing/Design Feedback" below. `ray.yaml` is now a
     local copy of upstream `general.yaml` run top-level, with upstream's own input
     schema (`ray_settings`, `workload_settings`). CONFIRMED 2026-11-13 by run
     saving-haddock (that copy, run top-level by hand, workers lab-cpu + gce-cpu +
     aws-cpu + oci-cpu): completed, endpoint `ray-cluster-saving-haddock` listed as
     `name<TAB>running<TAB>url` (the format `_endpoints` falls back to), job dir
     `/tmp/runs/saving-haddock/`, Ray head on the workspace at a 172.17.x address,
     and the user saw every worker node join. Note the run completes as soon as the
     FIRST worker CPU joins ("Cluster has 1 CPUs available"); the rest join after,
     so `ready` does not mean every row is up;
   - the endpoint is named `ray-cluster-<our slug>` (confirmed by workable-ringtail's
     `wait_for_endpoint`), and `pw endpoints list` prints name / ... / url columns.
     `_endpoints` tries `-o json` first, then the tab-separated table;
   - `fractal.yaml` on the workspace finds the cluster's job dir. workable-ringtail's
     job dir (`PW_PARENT_JOB_DIR`) was `/tmp/runs/<slug>/` on the workspace, not
     `~/pw/jobs/<slug>/` as upstream's README says for CLI runs, so `fractal.yaml`
     now searches both. Confirm that a later `fractal.yaml` run on the workspace
     sees the same `/tmp` (not a per-run container);
   - `ray job submit` against `127.0.0.1:8265` on the head works, and a Ray Job
     driver reaches workers on remote sites (their 127.0.X.Y tunnel addresses).
     CONFIRMED 2026-11-13 for an SSH worker: run composed-terrapin (`fractal.yaml`
     add, 20 tiles on canvas ray-tests-canvas-000001 of saving-haddock) rendered all
     20 on lab-cpu (`127.0.2.1`, site-2, 20 cores) at 9.9 tiles/s; the Slurm workers
     had reached their 30-min walltime. Still to confirm on Slurm workers. The run
     itself ended `error` although the render succeeded: `fractal.yaml` grepped
     `ray job status` for SUCCEEDED/FAILED, but that command prints only prose
     ("Job '<id>' succeeded") on stderr, so the state read UNKNOWN and the step
     exited 4. Fixed: the state now comes from the Jobs API
     (`JobSubmissionClient.get_job_status`), checked against a local Ray 2.60 job
     that succeeded and one that failed, and on the platform by run humane-bunny
     (next 20 tiles, `RAY_JOB_STATE=SUCCEEDED`, run completed, canvas 40/100).
     (`render_status` reads the JSON result
     line, so an agent would still have reported the render correctly.)
     SLURM PATH CONFIRMED 2026-11-13: cluster capital-quetzal (Slurm workers only,
     gce/aws/oci, 2 nodes x `--cpus-per-task=2`, 02:00:00), canvas slurm-test-000001,
     add run close-feline (commit d38763c): 20/20 tiles, 0 failed, 4.0 s, Ray Job
     SUCCEEDED; 16 tiles on gce (127.0.2.1), 4 on oci (127.0.4.1); a second oci
     node (127.0.4.2) joined mid-run and the driver picked it up. aws's nodes were
     still CONFIGURING (cloud provisioning) when the launch run completed, so no aws
     worker took part.
     Fallback: run `render.py add` directly in the workflow step, still on the
     head host;
   - inside a Slurm worker, `os.sched_getaffinity` sees the `--cpus-per-task`
     cores (ray-cluster's `srun --ntasks=N` step is not `--exact`), so the per-node
     pool is the target's cores per node. MIXED 2026-11-13 (close-feline): the gce
     node saw 2 cores, but both oci nodes saw 1, from the same `--cpus-per-task=2`
     row. NOT the node definition: `scontrol show node` on oci gives CPUTot=2,
     ThreadsPerCore=1, CoresPerSocket=2, Slurm 23.11.9, and the worker job holds
     AllocTRES cpu=2 (hyperthreading is off; the sibling threads are offline).
     The step is not the cause either: `scontrol show step 3.0` on oci gives
     Tasks=2, CPUs=4, TresPerTask=cpu:2 (23.11 passes `--cpus-per-task` on to srun).
     Leading hypothesis now: the binding Slurm applies maps the task's 2 CPUs onto
     logical CPU ids that include the OFFLINE hyperthread siblings, so the online
     part of the mask is 1 CPU. Check inside the running step on an oci node:
     `grep Cpus_allowed_list /proc/<raylet pid>/status`,
     `cat /sys/devices/system/cpu/online` and `lscpu -e`. oci's config (Slurm
     23.11.9): `TaskPlugin=task/cgroup,task/affinity`, `TaskPluginParam` unset,
     `SelectTypeParameters=CR_CPU`, cgroup `ConstrainCores=no`. So the binding is
     task/affinity's `sched_setaffinity` (no cpuset), which is exactly what
     `os.sched_getaffinity` reports. gce's config is the same in every relevant
     setting (23.11.9, same TaskPlugin, CR_CPU, ConstrainCores=no), so the
     difference is the oci hardware (siblings offline), which supports the
     offline-sibling hypothesis. On an oci compute node
     `/sys/devices/system/cpu/online` is `0,2` (CPUs 1 and 3, the siblings, are
     offline). If task/affinity maps the task's 2 CPUs to machine CPUs 0 and 1,
     the mask holds one online CPU. CONFIRMED in effect: the raylet's
     `Cpus_allowed_list` on that node is `0` (CPU 2 unused). The kernel drops
     offline CPUs from an affinity mask, so `0` is what a {0,1} binding leaves;
     `scontrol -d show job <id> | grep CPU_IDs` (expect `CPU_IDs=0-1`) shows
     Slurm's side if it matters. Full write-up for the provisioning team:
     "OCI: Slurm CPU binding with hyperthreading disabled" below. DECISION
     (2026-11-13): nothing is implemented here for it; the team may change the
     oci configuration in general;
   - `stop_ray` leaves no `ray-worker-*` job behind (check with the capacity
     workflow). 2026-11-13: `pw endpoints delete ray-cluster-capital-quetzal` (what
     `stop_ray` runs) removed the endpoint within ~5 s; the user checks the gce,
     aws and oci queues.
   - DONE 2026-11-13 on capital-quetzal, canvas slurm-test-000001:
     - perfect-ibex (`add`, `all: true`): the remaining 80 tiles, 0 failed, Ray Job
       SUCCEEDED, canvas complete with checksum `0ad866f6...aabfa8fd`, IDENTICAL to a
       local single-process render of the same spec. Tiles came from two runs and
       two sites (gce 16, oci 84).
     - The gce node (127.0.2.1) was dropped at the start of that render with
       `RuntimeEnvSetupError: Failed to set up runtime environment` (it had
       rendered close-feline's tiles an hour earlier); the driver carried on with
       the oci nodes as designed. Cause not yet known: the Ray Job's working_dir
       package is set up on every node that runs the job's actors, and that setup
       failed on gce. The node's Ray runtime-env logs
       (`/tmp/ray/session_latest/logs/` on the gce node) would say why; `render.py`
       prints only the first line of the error.
     - secure-dinosaur (`status`) and active-pegasus (`redraw`, 100 tiles posted)
       completed, so `render_status` on a canvas id and the redraw path work.

   - 2026-11-13: `launch_ray` takes an optional `targets` list (cpu targets by name)
     so a launch can leave out targets that are busy or short of quota (e.g. azure-cpu
     during a long heat run): "Use the ray-launcher-mcp agent to start a Ray cluster
     that spans compute targets gce-cpu, aws-cpu, and lab-cpu". Tested offline (fake
     pw and over MCP); not yet run by the agent.

   Open:
   - The worker rows follow the heat targets' footprint. The pwdemo targets give
     2 nodes × 2 cores per cloud cluster, which is small for fractal scaling runs. A
     per-launch override (`cpus_per_node`, `nodes`), or Ray-specific fields in a
     target, would be a human decision.
   - The upstream dashboard's own counters (`total_completed`/`total_planned`, site
     task counts) also count Ray Jobs and actor calls, so they run ahead of the
     tiles. The canvas progress in `render_status` is the reliable number.
   - A submission holds every node's single Ray slot while it runs. A second
     concurrent submission waits for slots (up to `--node-wait`, 600 s) instead of
     sharing them.
   - Follow-ups: GPU worker rows (gpu targets are skipped); a fractal evaluator
     (canvas checksum vs a local reference); non-square canvases (the dashboard
     draws a square `grid_size`).


## OCI: Slurm CPU binding with hyperthreading disabled

Status (2026-11-13): diagnosed, nothing changed. To be discussed with the cluster
provisioning team, who may change the oci configuration in general. No workaround
is implemented in this repo, on purpose.

### Summary

On the pwdemo oci cluster, a Slurm task granted 2 CPUs on a 2-core node can run on
only ONE core. The node's online CPUs are numbered 0 and 2 (hyperthreading is off
because the sibling threads 1 and 3 are offline), but Slurm's CPU binding uses CPUs
0 and 1. CPU 1 is offline, so the task ends up on CPU 0 alone and CPU 2 sits idle.
Anything Slurm binds on these nodes gets half the node.

### Evidence

- Symptom: Ray fractal render close-feline (cluster capital-quetzal, worker rows
  with `--cpus-per-task=2` on 2-node gce, aws and oci clusters). `render.py` sizes
  its per-node process pool with `os.sched_getaffinity(0)`; it saw **2** CPUs on the
  gce node and **1** on each oci node, from identical rows.
- oci node (`scontrol show node pwdemostefan-oci-00018-1-0001`): shape
  VM.Standard.E2.2, `CPUTot=2`, `CPUEfctv=2`, `CoresPerSocket=2`, `ThreadsPerCore=1`,
  Slurm 23.11.9, job holds `AllocTRES=cpu=2`. The node definition is right.
- Worker step (`scontrol show step 3.0`): `Tasks=2`, `CPUs=4`, `TresPerTask=cpu:2`.
  The step does get 2 CPUs per task (23.11 passes `--cpus-per-task` on to `srun`).
- Slurm config (`scontrol show config`), identical on oci and gce in every relevant
  setting: `TaskPlugin=task/cgroup,task/affinity`, `TaskPluginParam` unset,
  `SelectType=select/cons_tres`, `SelectTypeParameters=CR_CPU`, cgroup
  `ConstrainCores=no`. So CPU placement is done only by task/affinity's
  `sched_setaffinity`; there is no cpuset fence.
- oci compute node: `/sys/devices/system/cpu/online` = `0,2`.
- Ray worker (raylet) on that node: `Cpus_allowed_list: 0`.
- Not yet run (would show Slurm's own CPU ids): `scontrol -d show job <id> | grep
  CPU_IDs`, expected `CPU_IDs=0-1`. The kernel drops offline CPUs from an affinity
  mask, so a {0,1} binding leaves exactly `0`.

Since the Slurm configuration is the same on both clusters, the difference is the
hardware layout: gce's online CPUs are 0 and 1, oci's are 0 and 2.

### Impact

- Ray workers on oci use 1 of 2 cores (fractal throughput on oci is about half).
- Heat MPI runs on oci-cpu are bound the same way. If the target places 2 ranks per
  node, both may share CPU 0. Results still validate (the solution does not depend
  on placement), but oci timings in site comparisons are pessimistic.
- Likely affects any job on these nodes that relies on Slurm's default binding.

### Options

1. **Site fix (preferred if the team agrees).** Make Slurm's CPU numbering match the
   online cores: turn hyperthreading off at the shape/hypervisor level, or
   renumber/online CPUs so the usable cores are 0 and 1, or describe the
   topology so slurmd/hwloc binds to online CPUs only. Fixes Ray, heat and every
   other job at once.
2. **`srun --cpu-bind=none` for the Ray worker step** (a change to the upstream
   ray-cluster workflow, `parallelworks/workflows`).
   - Covers: any site where the problem is task/affinity binding to the wrong CPUs
     (offline siblings, odd numbering, firmware quirks), as long as cores are not
     cgroup-constrained. It fits Ray's model (one worker per node, parallelism inside
     the node), where per-task binding adds nothing.
   - Does NOT cover: sites with `ConstrainCores=yes`, where Slurm also builds a
     cgroup cpuset from the same CPU ids, so the wrong-CPU problem remains and
     `--cpu-bind=none` cannot widen it; and heat MPI runs, which are separate
     steps.
   - Risk: on shared nodes (`CR_CPU` lets several jobs share a node) with
     `ConstrainCores=no`, an unbound worker sees every CPU on the node, including
     other jobs' CPUs. An app that sizes its pool from its affinity (as `render.py`
     does) would then oversubscribe the node. It is safe for our worker rows only
     because they take whole nodes (2 of 2 CPUs).
   - To make it general: pair it with app-side sizing from Slurm's allocation
     (`SLURM_CPUS_PER_TASK` or `SLURM_CPUS_ON_NODE`, capped by the CPUs actually
     usable, falling back to `sched_getaffinity` outside Slurm), and log a warning
     when the usable CPUs are fewer than `SLURM_CPUS_PER_TASK`, so a bad binding shows
     in the launch log rather than only as low throughput.
3. **App widens its own affinity** to all online CPUs. Possible on oci because
   cores are not cgroup-constrained, but it overrides the scheduler's decision; only
   acceptable on nodes the job owns. Not recommended.

Related: "Workflow Writing/Design Feedback", item 7 (pass `--cpus-per-task` to the
worker `srun` and print each worker's usable CPU count when it joins).

## Workflow Writing/Design Feedback

Lessons from using `parallelworks/workflows` from this repo, for the workflows
repository's skills. Each one says what happened, what we did here, and what we
suggest upstream.

1. **A workflow that clones its own scripts must find them where the clone lands,
   not under `PW_PARENT_JOB_DIR`.** (ray-cluster `yamls/general.yaml` at
   `8b5acb7`, run workable-ringtail, 2026-11-13.)
   - What happened: our first `workflows/ray.yaml` called `general.yaml` as a
     subworkflow (`uses: github/parallelworks/workflows@canary`, `$yaml: ...`). Its
     `preprocessing` Checkout step cloned `workflows/ray-cluster/app` into the
     directory of the job running that step, which for a subworkflow is
     `/tmp/runs/<slug>/subworkflows/ray/step_0/`. `app/start-template.sh` then set
     `APP_DIR="${PW_PARENT_JOB_DIR}/workflows/ray-cluster/app"`, which is the
     TOP-LEVEL run directory `/tmp/runs/<slug>/`. The head died at once with
     `bash: .../workflows/ray-cluster/app/start_ray_head.sh: No such file or
     directory` (status 127).
   - Why it is easy to miss: run top-level, the job directory and
     `PW_PARENT_JOB_DIR` are the same directory, so the bug only shows when the
     workflow is nested. `general.yaml` itself nests `script_submitter` and
     `wait_for_endpoint` without trouble because those do not clone and then look up
     files by a parent path.
   - What we did: `workflows/ray.yaml` is now a byte-for-byte local copy of
     `general.yaml` (provenance header: repo, path, commit) that we stage under
     `runs/<id>/` and run top-level with `pw workflows run`, as ray-cluster's README
     "Running from the CLI" does. We did not change the workflows repository.
   - Suggestion: resolve scripts relative to the checkout (e.g. pass the checkout
     path as a step output, or `APP_DIR` from the script's own location), use one
     directory variable consistently for both the clone and its readers, and add a
     test that runs each reusable workflow as a subworkflow of a one-step wrapper.
     If a workflow is not meant to be nested, say so in its README.

2. **A failure that ends the service should end the wait for it promptly.**
   - What happened: the head exited with status 127 right after it started (about
     14:25:42), but `wait_for_endpoint` kept printing `Endpoint ... not found yet;
     retrying in 10 seconds` until about 14:29:57, when the session runner job
     noticed and cancelled it. To a user watching the log it looked like an endless
     loop, and the real error was near the end of the `session_runner` output.
   - Suggestion: have the waiter also stop on a marker the start script writes when
     it exits (e.g. `HEAD_FAILED` in the job dir, like the existing
     `DISPATCH_FAILED`), and print the head's exit status in the wait loop, so the
     first error is also the last line the user sees.

3. **Document where the job directory is.** ray-cluster's README says
   `~/pw/jobs/<run-slug>/` for CLI runs; on the PW workspace,
   workable-ringtail's `PW_PARENT_JOB_DIR` was `/tmp/runs/<slug>/`. Tools that find
   a running cluster afterwards (our `fractal.yaml` reads `ENDPOINT_NAME`,
   `RAY_HEAD_IP`, `SESSION_PORT` and `RAY_VENV_DIR` from it) need a stable answer.
   We now search both places. Suggestion: state the rule in the README, or record the
   job dir somewhere discoverable by endpoint name (e.g. in the endpoint's metadata
   or a fixed `~/pw/endpoints/<endpoint-name>` link).

4. **Pinning is only partial when a copied workflow still pulls `@canary`.** Our
   copy is pinned to `8b5acb7`, but its Checkout step clones `app/` from branch
   `canary`, and its `session_runner` and `wait_for_endpoint` jobs use
   `github/parallelworks/workflows@canary`. A later upstream change to `app/` can
   therefore break our older copy of the YAML. Suggestion: let the checkout ref and
   the nested `uses:` refs follow one input (default `canary`), so a caller can pin
   the whole workflow to a commit.

5. **Hidden inputs with defaults are part of the interface.** The endpoint name is
   `${{ inputs.ray_settings.name }}-${PW_RUN_SLUG}`, from a hidden input defaulting
   to `ray-cluster`. Callers that need the endpoint name (to find or delete the
   cluster) depend on that default, so we pass `ray_settings.name: ray-cluster`
   explicitly. Suggestion: document hidden inputs that shape externally visible
   names, or print the endpoint name as a step output early in the run.

6. **Remote worker sites share one fixed work directory across clusters.** On every
   remote site the worker side uses `~/pw/jobs/ray_worker_remote/` (the
   `PW_PARENT_JOB_DIR` default in `teardown.sh`), whatever cluster it belongs to.
   - What happened: cluster capital-quetzal's dispatcher output (in its `cluster_ready`
     log) replayed lines from EARLIER clusters' workers: `JOB 1 ... CANCELLED ... DUE
     TO TIME LIMIT` on aws from 15:29, `JOB 2` on gce and oci from 16:20/16:42, and
     tunnel IPs `127.0.5.x` for oci (whose rows are `127.0.4.x` in this cluster). Its
     `slurm_jobid` file is overwritten by each new cluster's worker job.
   - Why it matters: the log mixes old failures into a healthy run, which misleads
     anyone debugging, and the per-site cleanup reads only the newest `slurm_jobid`
     and also runs `scancel --name="ray-worker-*"`, which cancels the worker jobs of
     EVERY Ray cluster the user has on that site, not just this one.
   - Suggestion: give each cluster its own remote dir (e.g.
     `~/pw/jobs/ray_worker_remote/<endpoint-name>/`), truncate or rotate the worker
     log when a new dispatch starts, and cancel by the recorded job id (or a job name
     that includes the endpoint name) instead of a name wildcard.

7. **`--cpus-per-task` on the batch job may not reach the `srun` step.** The worker
   job runs `srun --ntasks=N` without `--cpus-per-task`, so whether the step (and
   thus Ray's worker and its processes) sees the job's per-task CPUs depends on the
   site's Slurm version and node definition. In close-feline the gce worker saw 2
   cores and the oci workers saw 1 from identical rows, although oci's node and the
   job both have 2 CPUs (see item 5). Suggestion: pass `--cpus-per-task` (or
   `-c "$SLURM_CPUS_PER_TASK"`) to the `srun` explicitly, and print each worker's
   visible CPU count (`os.sched_getaffinity`) when it joins, so such a mismatch is
   visible in the launch log rather than only in a workload's throughput.


8. **A queued worker site is cancelled after a fixed 10 minutes.** In
   `app/dispatch_workers.sh` (at `8b5acb7`) the remote-site script submits the
   worker job, then waits for its nodes to report in: `if [ ${attempt} -gt 300 ]`
   (300 × 2 s) it prints `Timeout waiting for compute nodes after 10min` and exits,
   and its `trap cleanup EXIT` runs `scancel` on the worker job.
   - Why it matters: a site whose partition is busy (e.g. azure-cpu while a long heat
     run holds the partition's whole node quota) or whose cloud nodes provision
     slowly never joins: its worker job is cancelled instead of waiting in the queue,
     and the cluster keeps running without that site. Upstream's own Walltime
     tooltip suggests long-lived `cluster_only` clusters, where a late-joining site
     is the expected case.
   - Suggestion: make the node wait an input (per worker row, or a `0` = wait for
     as long as the job is queued), and report a site that gave up in the dashboard
     and the run log as "not joined (queued too long)", not only as an error line.
