# HANDOFF: open items

What is still open after the MCP-enabled agents (`site-runner-mcp`,
`evaluator-mcp`, `compare-agents`) were built and compared with their shell
siblings. Test results are in `results/agent-summary.md`.

1. **`scheduler: none` gaps:**
   - gpu mode is untested.
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
   - `agent_digest.py` marks `load_campaign` (`loaded`) and `list_runs`
     (`listed`) results as failed calls; add those statuses to its ok list.
   - Hooks re-trust (waiting on a colleague).
   - A future agent that fills in `sites.yaml` by inspecting existing clusters.
