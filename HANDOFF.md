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
