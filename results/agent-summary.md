# Agent usage log

One row per `pw code` agent session, kept so that the run-to-run variability
in turns and tokens for the same agent, test and model can be tracked over
many test runs. This is a log, not a benchmark.

How Claude (or anyone) maintains this file:

- APPEND one row after every agent session in a test, including failed or
  abandoned attempts. Never edit or delete earlier rows; a correction gets a
  new row that says what it corrects.
- Exactly 6 columns, in this order: agent | test | llm | turns | tokens | result.
  - `agent`: the exact pw code agent name (`site-runner`, `site-runner-mcp`,
    `evaluator`, `compare-agents`, ...), or `default` for the main session.
  - `test`: `YYYY-MM-DD` and a short descriptor, e.g. the target, grid,
    attempt number and run id.
  - `llm`: the short model name, e.g. `gpt-oss-120b`.
  - `turns`: `iterations` from `pw code -p ... -o json`, as a bare integer.
  - `tokens`: `usage.total_tokens` from the same JSON, as a bare integer
    (no commas, no `k`).
  - `result`: a short outcome, e.g. the scheduler job id and its final state.
- Never put a `|` character inside a cell.

Querying. A plain `grep site-runner` also matches `site-runner-mcp`, so anchor
on the agent column:

    grep '^| site-runner |' results/agent-summary.md
    grep '^| site-runner-mcp |' results/agent-summary.md | awk -F' *[|] *' '{print $5, $6}'   # turns tokens
    awk -F' *[|] *' '$2=="site-runner-mcp" && $4=="gpt-oss-120b" {n++; t+=$6} END {print n, t/n}' results/agent-summary.md

With `-F' *[|] *'` the fields are $2 agent, $3 test, $4 llm, $5 turns,
$6 tokens, $7 result.

| agent | test | llm | turns | tokens | result |
|---|---|---|---|---|---|
| site-runner | 2026-10-14 gce-cpu grid 128 clean slate attempt 1 | gpt-oss-120b | 22 | 290312 | FAILED: job 11, garbled base64 directives ran on 1 node, invented tol 1e-6 and max_iter 10000 |
| site-runner | 2026-10-14 gce-cpu grid 128 clean slate attempt 2 run site-runner-gce-cpu-000001 | gpt-oss-120b | 46 | 883633 | job 12 COMPLETED on 2 nodes, converged in 22161 iterations; agent stopped while its last poll read PENDING |
| site-runner-mcp | 2026-10-14 gce-cpu grid 128 clean slate run site-runner-mcp-gce-cpu-000005 | gpt-oss-120b | 3 | 20506 | job 13 COMPLETED; agent reported the job left the queue |
| evaluator | 2026-10-14 validate site-runner-gce-cpu-000001 and site-runner-mcp-gce-cpu-000005 | gpt-oss-120b | 29 | 335003 | both pass, relative L2 0, 2 hosts; no verdict.json saved |
| compare-agents | 2026-10-14 site-runner-mcp-gce-cpu-000005 vs site-runner-gce-cpu-000001 | gpt-oss-120b | 8 | 84290 | report written; wrongly says the shell run's last poll reached DONE |
