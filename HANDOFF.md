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
     untested.
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
3. **Questions for the pw developers:**
   - The vLLM job's 2-hour time limit means the endpoint id changes.
   - gpt-oss sometimes ends a turn empty, and interactive mode doesn't prompt it
     to continue (`-p` runs do).
   - The tool-name garbling in vLLM's harmony parser.
   - Task accepts `subagent_type: "default"` for a request naming an agent.
   - Is there a guaranteed way to delegate other than `--agent`?
   - A stopped subagent fires no hook.
   - One-shot `-p --agent` runs still fire no hook as of pw v7.131, so every
     digest has to be backfilled by hand.
   - Is there a timeout and retry setting for model requests? A cold local
     model's first request takes about 12 minutes, and requests time out after
     about 5 minutes.
   - Can a session be capped on calls to tools that don't exist? Small local
     models keep calling invented tools after being told to stop.
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
   - Hooks re-trust (waiting on a colleague).
   - A future agent that fills in `sites.yaml` by inspecting existing clusters.
