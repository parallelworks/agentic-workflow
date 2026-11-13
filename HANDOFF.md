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
     Fallback: run `render.py add` directly in the workflow step, still on the
     head host;
   - inside a Slurm worker, `os.sched_getaffinity` sees the `--cpus-per-task`
     cores (ray-cluster's `srun --ntasks=N` step is not `--exact`), so the per-node
     pool is the target's cores per node;
   - `stop_ray` leaves no `ray-worker-*` job behind (check with the capacity
     workflow).

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
