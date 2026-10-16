# Agent usage log

One row per `pw code` agent session, kept so that the run-to-run variability
in turns and tokens for the same agent, test and model can be tracked over
many test runs. This is a log, not a benchmark.

How to maintain this file:

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
| site-runner | 2026-10-15 gce-cpu grid 128 rerun attempt 1, before the ls runs/ prompt fix | gpt-oss-120b | 10 | 103634 | no job: GlobSearch could not see runs/; own printf and cat base64 pipelines denied; stopped to ask for approval |
| site-runner-mcp | 2026-10-15 gce-cpu grid 128 rerun run site-runner-mcp-gce-cpu-000006 | gpt-oss-120b | 7 | 41587 | killed by 1700 s timeout, cluster fault: job 15 NODE_FAIL then FAILED exit 122, node stuck COMPLETING so polls read RUNNING; turns and tokens summed from session usage |
| site-runner | 2026-10-15 gce-cpu grid 128 rerun attempt 2, after the ls runs/ prompt fix | gpt-oss-120b | 3 | 25166 | no job: its ls runs/... 2>/dev/null pipe to wc -l was denied (wc not allowed); stopped after one denial to ask for approval |
| site-runner-mcp | 2026-10-15 gce-cpu grid 128 rerun run site-runner-mcp-gce-cpu-000007 | gpt-oss-120b | 3 | 20956 | job 16 COMPLETED on 2 nodes; cold start, about 3 min from submit to start; agent reported the job left the queue |
| evaluator | 2026-10-15 after verdict.json prompt fix, validate site-runner-mcp-gce-cpu-000007 and site-runner-gce-cpu-000001 | gpt-oss-120b | 32 | 367332 | both pass, relative L2 0, 2 hosts; verdict.json written for both as evaluator-gce-cpu-000002 and -000003, values match workflow logs; noted different commits but did not flag them |
| site-runner | 2026-10-15 gce-cpu grid 128 rerun attempt 3, after the denied-command rule | gpt-oss-120b | 21 | 234869 | no job: recovered from 2 denials (WriteFile then base64 -w0, directives correct), but used GlobSearch not ls, reused id 000001, overwrote the baseline's local files (restored), STAGE refused the existing remote dir; reported the failure honestly |
| site-runner | 2026-10-15 gce-cpu grid 128 rerun attempt 4, no GlobSearch and strict mkdir claim, run site-runner-gce-cpu-000000 | gpt-oss-120b | 41 | 674572 | job 17 COMPLETED but invalid: ls and mkdir claim worked, printf denied, then hand-typed base64 garbled the directives, so job ran on default gpu partition, 1 node, 1 rank; agent reported DONE without noticing |
| evaluator | 2026-10-15 same commit d494fe1, validate site-runner-mcp-gce-cpu-000007 and site-runner-gce-cpu-000000 | gpt-oss-120b | 37 | 490237 | both pass, relative L2 0; verdict.json written for both as evaluator-gce-cpu-000004 and -000006 (mkdir claim skipped taken 000005); MISSED integrity: shell run used 1 rank on 1 host vs mpi_ranks 4 on 2 nodes; 1 malformed tool name and 1 invented EditFile, both recovered |
| evaluator | 2026-10-15 after integrity-check prompt fix, validate site-runner-mcp-gce-cpu-000007 and site-runner-gce-cpu-000000 | gpt-oss-120b | 40 | 446142 | correct: 000007 PASS; 000000 FAIL with RANKS_MISMATCH and MULTI_NODE_MISMATCH despite relative L2 0; verdict.json with integrity_flags as evaluator-gce-cpu-000007 and -000008; GlobSearch picked taken 000001 but strict mkdir refused it, then ls; 1 invented EditFile, recovered |
| evaluator-mcp | 2026-10-15 first agent run, evaluate site-runner-mcp-gce-cpu-000007 and site-runner-gce-cpu-000000 | gpt-oss-120b | 3 | 24755 | correct: 000007 PASS; 000000 FAIL with RANKS_MISMATCH, MULTI_NODE_MISMATCH and DIRECTIVES_MISMATCH; verdict.json as evaluator-mcp-gce-cpu-000001 and -000002; same outcome as the fixed evaluator; model endpoint vllm-rag-00067 |
| compare-agents | 2026-10-15 run pair site-runner-mcp-gce-cpu-000007 vs site-runner-gce-cpu-000000 plus both evaluator pairs | gpt-oss-120b | 24 | 336688 | run pair correct (directives_b64 differs, same commit, 20k vs 674k tokens); evaluator pairs WRONG: paired the oldest pre-integrity-fix evaluator verdicts (000004, 000006) so said the shell evaluator passed 000000; omitted shell evaluator cost; overwrote results/compare-agents-gce-cpu-000001 again |
| compare-agents | 2026-10-15 rerun after newest-verdict and strict-claim prompt fix, same pairs | gpt-oss-120b | 31 | 509578 | claimed results/compare-agents-gce-cpu-000002 correctly (no overwrite); evaluator pairs still WRONG: never read the newest evaluator verdict 000008, paired old 000006 with both runs, so again said the shell evaluator passed 000000; wrote "none" for shell evaluator cost |
| compare-agents | 2026-10-15 first run with list_runs and compare_runs tools, site-runner-mcp-gce-cpu-000007 vs site-runner-gce-cpu-000000 plus both evaluator pairs | gpt-oss-120b | 3 | 30248 | mostly correct: right newest verdicts, new report dir compare-agents-gce-cpu-000001, figures match comparison.json; 3 write-up slips: flag columns show one-side-only flags, says not comparable though comparable is true, repeats evaluator session totals instead of tokens_per_id |
| compare-agents | 2026-10-15 compare_runs writes report.md tables and write_summary adds the summary, same pair | gpt-oss-120b | 3 | 29783 | tables correct by construction in results/compare-agents-gce-cpu-000002; summary correct on parity, flags and verdicts, but invents "about 2k tokens per ID" for the MCP run (tool says 20276) and lists 2 of 3 intentional differences |
| site-runner-mcp | 2026-10-15 gce-cpu grid 128 run site-runner-mcp-gce-cpu-000009, mcp first, warm nodes | gpt-oss-120b | 3 | 23564 | job 22 COMPLETED on 2 nodes in partition cpu; warm start, 1 s from submit to start; agent reported the job left the queue |
| site-runner | 2026-10-15 gce-cpu grid 128 after mcp, warm nodes, attempt 1 | gpt-oss-120b | 3 | 25535 | no job: 2 denials (ls with 2>/dev/null and a shell while-loop with printf), then STOPPED: repeated denied commands one denial early (rule says 3); reported honestly |
| site-runner | 2026-10-15 gce-cpu grid 128 after mcp, warm nodes, attempt 2, run site-runner-gce-cpu-000002 | gpt-oss-120b | 44 | 668208 | STOPPED by human while still polling: printf denied, then hand-typed directives_b64 garbled, job 25 ran on default gpu partition, 1 node, FAILED exit 2 in 2 s; read each poll log before its state line, so never saw DONE (8 polls, 1 typo in a path); turns and tokens summed from session usage |
| default | 2026-10-15 interactive pw code session by the human ("use the site-runner agent"), main agent did the work itself without delegating, run site-runner-gce-cpu-000003 | gpt-oss-120b | 27 | 426750 | job 28 COMPLETED on 2 nodes in partition cpu, warm start; directives correct but missing the trailing newline; interactive approvals (printf pipe allowed); not a like-for-like site-runner -p test; called the job "completed successfully" before any evaluation |
| evaluator-mcp | 2026-10-15 evaluate site-runner-mcp-gce-cpu-000009 and site-runner-gce-cpu-000003 | gpt-oss-120b | 3 | 25073 | 000009 PASS; 000003 FAIL on DIRECTIVES_MISMATCH only, a false positive: directives lacked only the trailing newline, which run.yaml adds anyway (tool fixed to ignore trailing whitespace) |
| evaluator-mcp | 2026-10-15 re-evaluate site-runner-gce-cpu-000003 after the trailing-whitespace fix | gpt-oss-120b | 2 | 15864 | 000003 PASS as evaluator-mcp-gce-cpu-000005, superseding -000004 |
| evaluator | 2026-10-15 evaluate site-runner-mcp-gce-cpu-000009 and site-runner-gce-cpu-000003 | gpt-oss-120b | 39 | 482817 | both PASS, ranks 4, 2 hosts, no flags; verdict.json as evaluator-gce-cpu-000009 and -000010; 3 denials (cat, mkdir -p results/, mkdir results/) and 1 invented EditFile, all recovered |
| compare-agents | 2026-10-15 site-runner-mcp-gce-cpu-000009 vs site-runner-gce-cpu-000003, server 0.6.0 | gpt-oss-120b | 3 | 29377 | tables reproduced a tool false positive (directives_b64 differed only by a trailing newline, reported as parity FAILED and directives NO); report moved aside after compare_runs was fixed in 0.6.1 |
| compare-agents | 2026-10-15 same pair after server 0.6.1 trailing-whitespace fix | gpt-oss-120b | 3 | 29362 | tables correct in results/compare-agents-gce-cpu-000003: parity ok, whitespace-only directives difference, both evaluators PASS both runs; summary wrongly says evaluator costs are similar (12k-15k vs 241k per run) and omits that the shell-side run came from the main agent |
| default | 2026-10-15 delegation test after the AGENTS.md always-delegate rule, prompt names evaluator-mcp, no --agent | gpt-oss-20b | 2 | 23266 | delegated: one Task call with subagent_type evaluator-mcp and no work of its own; chose background true, so the -p session returned before the subagent finished |
| evaluator-mcp | 2026-10-15 delegated by the 20b main session as background Task task-3, evaluate site-runner-mcp-gce-cpu-000009 | gpt-oss-20b | 2 | 15916 | 000009 PASS, no flags, as evaluator-mcp-gce-cpu-000006 (supersedes -000003, same outcome); kept running in the pw code daemon after the -p session returned; SubagentStop hook fired and wrote the digest; figures from the hook digest |
| site-runner-mcp | 2026-10-15 gce-cpu grid 128 run site-runner-mcp-gce-cpu-000010, mcp first, warm nodes, endpoint vllm-rag-00069 | gpt-oss-120b | 3 | 24331 | job 32 COMPLETED on 2 nodes in partition cpu; warm start, 0 s from submit to start; agent reported the job left the queue |
| site-runner | 2026-10-15 gce-cpu grid 128 after mcp, warm nodes, with the new AGENTS.md always-delegate rule | gpt-oss-120b | 1 | 9018 | no job, no tool calls: refused, saying the job must be run by the site-runner agent; the rule's exception for --agent sessions was not understood |
| site-runner | 2026-10-15 gce-cpu grid 128 after mcp, warm nodes, after the delegation rule was keyed to having the Task tool, run site-runner-gce-cpu-000004 | gpt-oss-120b | 28 | 436630 | job 33 COMPLETED on 2 nodes in partition cpu, warm start; printf and echo denied, then WriteFile plus base64 -w0 on the file gave correct directives; ls and mkdir claim worked; read DONE from the poll; left temp_directives.txt in the repo root (deleted) |
| evaluator-mcp | 2026-10-15 evaluate site-runner-mcp-gce-cpu-000010 and site-runner-gce-cpu-000004 | gpt-oss-120b | 3 | 25878 | both PASS, no flags, as evaluator-mcp-gce-cpu-000007 and -000008 |
| evaluator | 2026-10-15 evaluate site-runner-mcp-gce-cpu-000010 and site-runner-gce-cpu-000004 | gpt-oss-120b | 35 | 444523 | both PASS, no flags, as evaluator-gce-cpu-000011 and -000012; 1 denial (mkdir -p results/), recovered |
| compare-agents | 2026-10-15 site-runner-mcp-gce-cpu-000010 vs site-runner-gce-cpu-000004 plus both evaluator pairs | gpt-oss-120b | 3 | 31047 | tables correct in results/compare-agents-gce-cpu-000004: comparable, parity ok, directives match on both, all four evaluations PASS with no flags; summary correct apart from a duplicated heading and evaluator figures given per session (24872 vs 443631) without the per-run split |
| default | 2026-10-15 delegation test on 120b, prompt names evaluator-mcp, no --agent, rule keyed to having the Task tool plus background false | gpt-oss-120b | 2 | 39614 | delegated correctly: one Task call to evaluator-mcp with background false, waited, and reported the verdict path and PASS itself |
| evaluator-mcp | 2026-10-15 delegated by the 120b main session as a foreground Task, evaluate site-runner-gce-cpu-000004 | gpt-oss-120b | 2 | 16204 | PASS, no flags, as evaluator-mcp-gce-cpu-000009 (supersedes -000008, same outcome); SubagentStop hook wrote the digest; figures from the hook digest |
