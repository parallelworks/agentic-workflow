# `heat` MCP server — run, evaluate and compare tools

`heat_server.py` is a lightweight, **stdlib-only** (Python ≥ 3.9) MCP server with
six tools. Each agent launches its own server process, and the `--tools` flag
decides which tools that process serves: the default (no flag) is `run_heat` and
`poll_heat` for `site-runner-mcp`, exactly as before v0.4.0; `--tools
evaluate_run` serves only `evaluate_run`, for `evaluator-mcp`; `--tools
list_runs,compare_runs,write_summary` serves the three for `compare-agents`. A tool that is not
served is absent from `tools/list` and refused by `tools/call` (`-32602`).

**`run_heat(target, grid, [tol], [max_iter])`** does in code exactly what
`site-runner` does in shell for the run step:
1. Resolve the target via `scripts/_site_query.py`, run as a subprocess with the
   same `python3`.
2. Stage `runs/site-runner-mcp-<target>-<NNNNNN>/`.
3. Run `pw workflows run` against the unmodified `workflows/run.yaml`.

It returns right after the submit step finishes, with the run id and the scheduler
job id. It never waits for the job itself.

For a `scheduler: none` target (no batch system; `run.yaml` runs the job in the
foreground on the login node under the target's `timeout`), `run_heat` first
rejects directives that are not `#` comment lines only and a malformed
`timeout`, adds `timeout` to `run.inputs.json`, and returns as soon as the run's
log shows `SUBMITTED scheduler=none job_id=<host>:<pid>`, while the workflow is
still running the job. `poll_heat` then reports RUNNING until that process
exits, and `evaluate_run` expects exactly 1 host.

**`poll_heat(run_id)`** runs the unmodified `workflows/poll.yaml`, one logged
workflow run per poll named `<run_id>-poll-<N>`, spaced 30 s apart. It reads the
state line from the poll's log and returns as soon as the job has left the queue
(`state: "DONE"`, `done: true`, `left_queue: true`), or after about 7 minutes with
`done: false`, in which case the agent calls it again. `poll.yaml` reports DONE for
any job that is no longer queued, failed ones included, so `poll_heat` (since
v0.3.0) says only "left the queue". Whether the job succeeded is the evaluator's
call.

### Why polling is a tool too
HANDOFF.md originally scoped only the run step. The first real run on `gce-cpu`
(gpt-oss-20b, 2026-10-13) showed that shell polling is the fragile part:
- `pw workflows run` returns as soon as a run is created, so the poll's `{"state":…}`
  line only appears in `pw workflows runs logs <slug>`. The model never saw it.
- Its attempts to recover were blocked (`pw workflows logs`, `sleep 10; …`).
- Three of its calls used malformed tool names (`Bash<|channel|>commentary`).
- It then reported DONE while Slurm still showed the job CONFIGURING.

The repository owner chose to wrap polling as well. As a result `site-runner-mcp`
needs no shell at all.

It is **agent-scoped**: only `site-runner-mcp` sees it, through the inline
`mcpServers` entry in `.agents/agents/site-runner-mcp.md`:

```yaml
mcpServers:
  - heat:
      command: python3
      args: ["-I", ".agents/mcp/heat_server.py"]
      timeout: "10m"   # pw code's default is 30s; clone+build+submit can take longer
```

The tools are exposed to the model as `mcp__heat__run_heat` and `mcp__heat__poll_heat`.

## `evaluate_run` (v0.4.0, for `evaluator-mcp`)

**`evaluate_run(run_id, [l2_tolerance])`** does in code what the `evaluator` does
in shell for one run, for a run id from either run agent:
1. Read `runs/<run_id>/run.inputs.json` and `poll.inputs.json`, take the target
   from the run id, and resolve the target's directives through the protected
   `_site_query.py`. Rejects (nothing staged) an unknown run id, a run with no
   job id, or an unusable `remote_dir`/`cluster`.
2. Claim `runs/evaluator-mcp-<target>-<NNNNNN>/` with an atomic `mkdir` and stage
   the unmodified `poll.yaml`, `fetch.yaml` and `validate.yaml` with their inputs.
3. **Precondition:** one poll (`<eval-id>-poll`). If the job has not left the
   queue, stop with `status: not_ready`, `overall: NOT EVALUATED`; nothing is
   fetched.
4. Fetch (`<eval-id>-fetch`) and validate (`<eval-id>-validate`), reading the JSON
   line from each log. `validate.py` exits non-zero on a failed comparison, so a
   validate workflow that ends `error` but printed a verdict still counts.
5. **Integrity, in code**, against the run's own target: `ranks` vs `mpi_ranks`,
   `mode`, `distinct_hosts` vs the nodes the directives request, convergence
   (`converged` and `iterations < max_iter`), and directives parity (the run's
   `directives_b64` must be the base64 of the target's directives — a byte
   comparison). Each failure is a string in `integrity_flags`.
6. **Always** write `results/<eval-id>/verdict.json` (status, ids, target, grid,
   commit, mpi_ranks, mode, job id, l2_tolerance, directives, workflow slugs,
   record, verdict, integrity_flags, checks, overall, error).

`overall` is PASS only if `verdict.ok` is true and there are no flags.

The node count is read from the directives **for the audit only**, never to launch
anything (AGENTS.md: the run tooling never parses directives). Only exact forms
count: `#SBATCH --nodes=N`, `#SBATCH -N N`, and `#PBS -l …select=N` or `nodes=N`.
Anything else leaves `checks.nodes.ok` null ("not checked"), and the agent is told
to judge it from `directives` and `distinct_hosts`.

Scoped the same way, through `.agents/agents/evaluator-mcp.md`:

```yaml
mcpServers:
  - heat:
      command: python3
      args: ["-I", ".agents/mcp/heat_server.py", "--tools", "evaluate_run"]
      timeout: "10m"
```

## Why hand-rolled
The host has Python 3.9 and no `uv`, and the official MCP Python SDK needs Python
3.10 or newer. A single file with no dependencies ships with the repo and runs
wherever `python3` does.

## Protocol (dual-era)
The protocol was checked against modelcontextprotocol.io on 2026-10-13.
- **Legacy** (2024-11-05 … 2025-11-25): `initialize` (the server echoes the
  client's version if it is listed, otherwise returns 2025-11-25), then
  `notifications/initialized`, then requests.
- **Modern** (2026-07-28): there is no handshake. Each request carries
  `params._meta["io.modelcontextprotocol/protocolVersion"]`. The server answers
  `server/discover`, adds `resultType:"complete"` to modern results, and returns
  `-32022` for unsupported versions.
- Both eras support `ping`, `tools/list`, and `tools/call`.
- Errors:
  - Unknown tool → `-32602`.
  - Unknown method → `-32601`.
  - Bad JSON → `-32700`.
  - Validation and business errors are returned as `isError: true` with a
    structured result.
- Framing is newline-delimited UTF-8 JSON-RPC. stdout carries MCP messages only,
  and the server exits when stdin closes.

## Why it lives under `.agents/`
The server runs as the user and its `_pw()` allowlist is the safety boundary, so
agents must not be able to rewrite it. pw code makes any change under
`<workspace>/.agents/` ask a human first, whether by file tool or shell, in every
mode except `bypass-permissions`; in `-p` runs such a write simply fails. The same
reasoning puts the digest hook in `.agents/hooks/`. Hook trust covers only the
hook command, not the script it runs.

- **`_site_query.py`:** the server executes only `.agents/mcp/_site_query.py`, a
  path fixed relative to the server file.
- **Parity check:** before each target lookup, it checks that this copy is
  byte-identical to `scripts/_site_query.py`, the copy `site-runner` runs. If they
  differ, `run_heat` is rejected and nothing is staged.
  - This catches tampering with the unprotected copy, and keeps target resolution
    identical for both run agents.
  - After a legitimate change to `scripts/_site_query.py`, a human must re-copy
    it: `cp scripts/_site_query.py .agents/mcp/`.
- **`python3 -I`:** both the server and the hook run in isolated mode, which
  ignores `PYTHON*` environment variables and personal site-packages. PyYAML comes
  from system site-packages, so parsing is unchanged.

## Guarantees enforced in code
- **Platform access:** the only platform access is `_pw()`. Its allowlist is
  `pw workflows run` (the submission) plus read-only `pw workflows runs view|logs`,
  used to read the outcome. No `pw ssh`, no scp, no environment dumps, no secret
  files.
- **`grid >= mpi_ranks`:** a smaller grid would make every rank `MPI_Abort`. Such a
  call is rejected before anything is staged or submitted. This is an intentional
  difference from the shell path.
- **Directives:** passed verbatim, base64-encoded. The bytes are identical to
  `_site_query.py … directives | base64 -w0`.
- **Tool scoping:** a process serves only the tools named by `--tools`; calls to
  any other tool are refused.
- **Comparison only reads:** `list_runs`, `compare_runs` and `write_summary` read
  local files (and `_site_query.py`); the only things they write are a new report
  directory with its `comparison.json` and `report.md`, and that report's summary.
- **Evaluation never resubmits:** `evaluate_run` runs only poll, fetch and
  validate against the run agent's existing remote dir.

## Artifacts
| Path | Contents |
|------|----------|
| `runs/<id>/run.yaml`, `run.inputs.json` | Same layout as the shell path |
| `runs/<id>/poll.yaml`, `poll.inputs.json` | Poll staged and pre-filled (cluster, scheduler, job_id) |
| `runs/<id>/submit.json` | Exact `pw` argv, workflow slug, job id, timestamps, status |
| `runs/<id>/rpc.jsonl` | This run's tools/call requests and responses |
| `runs/<id>/polls.jsonl` | One line per poll: N, workflow slug, state, raw scheduler state |
| `runs/<id>/agent-digest.json` | Redacted summary of the agent's session (tokens, turns, tool calls), written by `.agents/hooks/agent_digest.py` from a pw code hook |
| `runs/evaluator-mcp-<t>-<N>/` | Staged poll/fetch/validate yaml + inputs, `rpc.jsonl`, `agent-digest.json` |
| `results/evaluator-mcp-<t>-<N>/verdict.json` | The evaluation (always written, whatever the outcome) |
| `runs/_mcp/heat-<UTC>-<pid>.jsonl` | Every JSON-RPC message in or out for one server process, including the handshake. Also mirrored to stderr |

The trace files use `.jsonl` rather than `.log`. They hold only JSON-RPC messages
and the tool's structured result, never the environment or raw platform output, so
agents such as `compare-agents` may read them.

## Debugging without MCP
```
python3 -I .agents/mcp/heat_server.py --stage-only <target> <grid>   # stage only, no submit
python3 -I .agents/mcp/heat_server.py --evaluate <run_id>            # one evaluate_run; DOES run poll/fetch/validate
python3 -I .agents/mcp/heat_server.py --list [target]                # list_runs
python3 -I .agents/mcp/heat_server.py --compare <mcp_run> <shell_run> # compare_runs; claims a report dir
```

## Using it with pw code
1. Approve Workspace Trust at an interactive `pw code` launch in this repo.
   Agent definitions and their servers are ignored until you approve, and `-p`
   runs skip unapproved config silently. Any change to an agent file asks again.
2. Pre-approve the tools, or `-p` runs auto-deny them:
   `--allowedTools mcp__heat__run_heat --allowedTools mcp__heat__poll_heat`
   (site-runner-mcp) or `--allowedTools mcp__heat__evaluate_run` (evaluator-mcp),
   or add them with `/permissions add`.
3. Enable the session-summary hooks once with `/hooks trust`.
4. Check: `/mcp` under `pw code --agent site-runner-mcp` lists `heat`; the main
   session's `/mcp` does not. Likewise under `--agent evaluator-mcp`, which lists
   only `evaluate_run`.

## Diagnosing a failed run
| Symptom | Cause | Where to look |
|---------|-------|---------------|
| No handshake in the trace | Protocol | `runs/_mcp/*.jsonl` |
| `isError` / `failed` result | Tool logic | The returned result and `submit.json` |
| No tools/call at all | Prompting or description | The pw code session |

## Conformance against pw code
**Source:** the server trace from the error-path gate on 2026-10-13. CLI `pw` v7.122.0;
the client identified itself as `pw-code` 1.0.0. Model `gpt-oss-20b`. Command:
`pw code -p "... bigcluster-cpu with grid 16" --agent site-runner-mcp --allowedTools mcp__heat__run_heat`.

**Observed sequence:** `server/discover` → `tools/list` → `prompts/list` → `tools/call`.
pw code sent no `initialize` and no notifications.

| Check | Observed | Spec (2026-07-28) | Verdict |
|-------|----------|-------------------|---------|
| Era | Modern 2026-07-28 only | — | pw code is a modern client |
| Probe | `server/discover` first | Optional, RECOMMENDED on stdio | Conforms |
| `_meta` on every request | `protocolVersion`, `clientInfo`, and `clientCapabilities` (`{roots:{listChanged:true}}`) on all four requests | MUST | Conforms |
| `initialize` / `initialized` | Never sent | Not part of the modern era | Conforms; the server's legacy branch is untested by pw code |
| `prompts/list` | Sent, although discover advertised only `tools` | Clients are expected to use only advertised capabilities | **Divergence (minor).** The server's `-32601` is the correct reply, and pw code tolerated it silently. No accommodation was needed |
| `tools/call` | `{name, arguments:{grid:16 (integer), target}, _meta}` | Matches CallToolRequest | Conforms |
| Result handling | Accepted `resultType`, `structuredContent`, and `isError:true`; the model got the error text | Clients SHOULD pass tool errors to the model | Conforms |

**Accommodations of pw code quirks in the server code:** none so far.

**Not exercised by pw code:** `ping`, `notifications/cancelled`, timeouts, and
version-mismatch retry.

**Re-check with v0.2.0 (2026-10-14).** Same model and client. The trace sequence is
unchanged (`server/discover` → `tools/list` → `prompts/list` (-32601) → `tools/call`).
- **Error gate (`gce-cpu`, grid 2 < 4 ranks):** rejected in 2 iterations, 11,295
  tokens, nothing staged.
- **Happy path (`gce-cpu`, grid 128):** run `site-runner-mcp-gce-cpu-000002`, Slurm
  job 4. Five polls in one `poll_heat` call: four returned PENDING (CONFIGURING),
  while the nodes warmed up, and the fifth returned DONE. sacct reports COMPLETED
  0:0 on 2 nodes. The model's DONE was truthful.
  - It took 3 iterations, 18,095 tokens, and 2 tool calls with 0 failures.
  - The v0.1 run with Bash polling took 10 iterations, 71,901 tokens, and 9 calls
    with 5 failures, and reported a false DONE.

**Findings outside this server (pw v7.122, gpt-oss-20b only model available):**
- **Hooks in `-p` runs:** `SessionEnd` never fired. `SubagentStop` was later
  confirmed to fire in `-p` for a delegated agent (gpt-oss-120b, 2026-10-14); in the
  20b sessions no delegation ever succeeded, so it had no chance to. This was
  observed although
  `/hooks trust` was recorded for the workspace. An interactive session with the same
  config did fire `SessionEnd` (payload keys `cwd, hook_event_name, permission_mode,
  reason, session_id, transcript_path`), so hooks appear to be off in `-p` mode.
  Back the digest up manually (see AGENTS.md); `runs/_mcp/hooks.jsonl` shows whether
  a hook ever fired.
- **Default routing:** a generic "Run the heat job on target gce-cpu…" with no
  `--agent` picked `subagent_type: "site-runner-mcp"`, which is the correct routing
  decision. The delegation itself failed: every one of the 7 attempts named the tool
  `Task<|channel|>commentary`, a gpt-oss harmony channel tag that pw code does not
  strip.
  - The model then claimed it was submitting. Nothing was submitted.
  - The session took 24 iterations and 491k tokens.
  - Until that is fixed, pass `--agent site-runner-mcp` explicitly.

**Run after moving the server to `.agents/mcp/` (2026-10-14, gpt-oss-20b, `-p`).**

| Agent | Iterations / tokens | Result |
|---|---|---|
| `site-runner-mcp`, grid 2 | 2 / 13k | Rejected cleanly |
| `site-runner-mcp`, grid 128 (run 000003) | 3 / 19.8k | Slurm job 5 COMPLETED. 6 polls, true DONE |
| `site-runner`, attempts 3–4 | 23 / 415k; 53 / 1.12M | See below |
| `evaluator` on both runs | 37 / 434k | Staged into the run agents' dirs, ran no workflow, nothing validated |
| `compare-agents` | 15 / 120k | Empty answer, nothing written |

- **`site-runner` attempt 3** stopped after staging, with empty directives and an
  invented `tol`/`max_iter`.
- **`site-runner` attempt 4** submitted job 6 as `site-runner-gce-cpu-000001`. That
  id reused attempt 3's directory: `GlobSearch` did not list directories.
  - It polled once, then ended without DONE. Job 6 FAILED (exit 2, `converged:false`).
  - The cause was its invented `tol` 1e-6 and `max_iter` 20000.
  - Its `directives_b64` lacks the final newline.
  - 20 of its 52 calls failed.
- **Common failure:** the dominant one in every shell-capable agent is gpt-oss's
  malformed tool names (`X<|channel|>commentary`). `site-runner-mcp`, with two
  typed tools, was not affected in these runs.

**Same tests on gpt-oss-120b (2026-10-14).** Separate vLLM provider, same
`--tool-call-parser openai`. Zero malformed tool names in all 8 sessions.

| Agent | Iterations / tokens | Result |
|---|---|---|
| `site-runner-mcp`, grid 2 | 2 / 13k | Rejected cleanly |
| `site-runner-mcp`, grid 128 (run 000004) | 3 / 20k | Job 9 COMPLETED; evaluator pass, relative L2 0 |
| Main session, no `--agent`, grid 2 | 4 / 91k | One clean `Task` to `site-runner-mcp`. Routing works; on 20b all 7 attempts were malformed |
| `site-runner`, attempt 1 | 12 / 135k | Retyped the directives by hand. Gave up after 3 denials |
| `site-runner`, attempt 2 (run 000002) | 31 / 396k | Submitted job 10, polled with `sleep` gaps, reported DONE. Job FAILED (see below) |
| `evaluator`, attempt 1 | 9 / 77k | Gave up after one denial (`pw compute-clusters list`) |
| `evaluator`, attempt 2 | 34 / 486k | Ran all four fetch/validate workflows with correct verdicts. Saved no `verdict.json` |
| `compare-agents` | 15 / 156k | Wrote report and JSON; tokens and turns match the digests |

- **Run 000002 (`site-runner`, attempt 2):** it copied `tol` 1e-6 and `max_iter`
  20000 from the earlier 20b run's `run.inputs.json`. Job 10 therefore FAILED
  (exit 2, not converged).
  - It never ran `_site_query.py`. The model encoded the directives itself,
    correctly apart from the final newline.
- **`compare-agents` report:**
  - It calls the directives "different", but they differ only by the final newline.
  - It omits the guard note.
  - It made no platform cross-check.
- **DONE does not mean success.** `poll.yaml` reports `DONE` for any job that has
  left the queue, including FAILED ones (job 10). Both run agents' DONE therefore
  means finished, not succeeded. The evaluator is what catches a failure.

**Clean-slate pair on gpt-oss-120b (2026-10-14, `poll_heat` v0.3.0).** The stale
`site-runner` runs were removed first, and the allow list gained the
`run.inputs.json`/`poll.inputs.json` template rules (see AGENTS.md).

| Agent | Iterations / tokens | Result |
|---|---|---|
| `site-runner`, without the inputs-template rules | 22 / 290k | Job 11 FAILED on 1 node (see below) |
| `site-runner` (run 000001) | 46 / 884k | Job 12 COMPLETED, converged in 22161 iterations. Ended while its last poll still read PENDING |
| `site-runner-mcp` (run 000005) | 3 / 20.5k | Job 13 COMPLETED. Reported "the job has left the queue" |
| `evaluator` on both runs | 29 / 335k | Both pass, relative L2 0. Still no `verdict.json` saved |
| `compare-agents` | 8 / 84k | Correct figures. Wrongly says the shell run's final poll reached DONE |

- **Job 11:** the model hand-computed base64 that decoded to garbled `#SARB_` lines,
  which Slurm ignored, so the job ran on 1 node. It also invented `tol` 1e-6 and
  `max_iter` 10000; 1e-6 is `validate.yaml`'s tolerance. The job FAILED.
- **Run 000001 parity:** only `remote_dir` differs from the MCP run, plus
  `mpi_ranks` as a number instead of a string. The directives are byte-identical.
  - It worked the directives through a `WriteFile` + `base64` file, which it
    left in the repo root, not through `_site_query.py`.
- **Tool names:** zero malformed names in all 5 sessions.

**Session record:** `~/.local/state/pw/code-sessions/<session>.jsonl` holds the
messages, tool calls by name, and per-request token `usage`. It has no MCP
protocol-level detail; for that, use the server trace.

## `evaluate_run` checks without pw code (2026-10-15, v0.4.0)

Run through `--evaluate <run_id>` and over stdio, before any agent used it. The
cloud nodes had no capacity, but poll, fetch and validate run on the login node,
so these are live workflow runs. The outputs were moved out of `runs/` and
`results/` afterwards, so the agent's first evaluation is `evaluator-mcp-…-000001`.

| Run | Expected | Result |
|---|---|---|
| `site-runner-mcp-gce-cpu-000003` (job 5) | PASS | evaluated, PASS, no flags |
| `site-runner-mcp-gce-cpu-000007` (job 16) | PASS | evaluated, PASS, no flags |
| `site-runner-gce-cpu-000001` (job 12) | PASS | evaluated, PASS, no flags |
| `site-runner-gce-cpu-000000` (job 17, garbled directives) | FAIL | evaluated, FAIL: RANKS_MISMATCH (1 vs 4), MULTI_NODE_MISMATCH (1 vs 2), DIRECTIVES_MISMATCH |
| `site-runner-mcp-gce-cpu-000006` (job 15, FAILED on the cluster) | FAIL | failed: fetch found no solver JSON record |
| `site-runner-mcp-gce-cpu-000008` (job 18, cancelled) | FAIL | failed: "no solver JSON record found on cluster" |
| throwaway run pointing at a RUNNING job | not ready | not_ready, NOT EVALUATED; nothing fetched |
| unknown run id / `l2_tolerance: "abc"` | rejected | rejected, nothing staged |
| `run_heat` on a `--tools evaluate_run` server, and `evaluate_run` on a default server | refused | `-32602 Unknown tool` both ways |

## `list_runs` and `compare_runs` (v0.5.0, for `compare-agents`)

Added because `compare-agents` twice paired the wrong evaluator verdicts by hand
(2026-10-15, gpt-oss-120b): with six `evaluator` verdicts for three runs it read
some, never opened the newest, and reported that the shell evaluator passed a run
whose newest verdict failed it. The pairing is now code; the model writes the
report.

- **`list_runs([target])`** lists each run (`site-runner-mcp-…`, `site-runner-…`)
  with grid, commit, job id, poll count (MCP: `polls.jsonl`; shell: `poll.yaml`
  invocations in its hook digest) and the evaluator ids that evaluated it.
- **`compare_runs(mcp_run_id, shell_run_id)`** returns, for one pair of runs of
  one target:
  - Input parity: differences other than `remote_dir`, with value-equal type
    differences such as `4` vs `"4"` listed separately.
  - `comparable`: same commit and grid.
  - Whether each run's `directives_b64` matches the target.
  - Poll counts and session cost per run.
  - For each run, each evaluator's NEWEST verdict, with older ones listed as
    `superseded`, `record_equal`, `verdict_equal`, `flags_only_mcp`,
    `flags_only_shell` and `overall_agree`. The `evaluator`'s file has no
    `overall`, so the same rule is applied to it: PASS only if `verdict.ok` and
    no flags. A verdict written before integrity checks existed is `UNKNOWN`.
  - Session cost counted once per `session_id`, with `tokens_per_id` for an
    evaluator session that evaluated several runs.
  
  It claims `results/compare-agents-<target>-<NNNNNN>/` with an atomic `mkdir`
  and saves `comparison.json` there, so a report can no longer overwrite an
  earlier one. Since v0.6.0 it also writes `report.md`: every table, with each
  figure's source, rendered from `comparison.json`.
- **`write_summary(report_dir, summary)`** (v0.6.0) replaces the placeholder at
  the end of that `report.md` with the model's summary (markdown, at most 4000
  characters). It works once per report, only on a `compare-agents-…` directory,
  and never touches the tables. Added after the first tool-based run, in which
  the model rewrote the tables by hand and mislabeled the flag columns, called a
  comparable pair "not comparable", and repeated session totals as per-run costs.

CLI checks (2026-10-15), on `site-runner-mcp-gce-cpu-000007` vs `site-runner-gce-cpu-000000`:
- **Parity and cost:**
  - Parity fails on `directives_b64` only, with the same commit.
  - Only the shell run's directives do not match the target.
  - Costs: 20,276 tokens / 3 turns (MCP) vs 673,862 / 41 (shell).
- **Evaluator pairs:**
  - Both evaluators PASS the MCP run, with equal records and verdicts.
  - Both FAIL the shell run. Only evaluator-mcp raised DIRECTIVES_MISMATCH.
  - Per evaluated run: `evaluator` 222,480 tokens, `evaluator-mcp` 11,869.
- **Superseded verdict:** with a superseded verdict put back temporarily, the
  newest (`-000008`) was used and `-000006` was listed as superseded.
- **Swapped arguments:** rejected, and no directory was claimed.

## Directives comparison ignores trailing whitespace (v0.6.1)

`site-runner-gce-cpu-000003` (job 28) encoded its directives without the final
newline. `run.yaml` writes a newline after the block (`echo ""`), so the job
script Slurm received was line-for-line the target's directives. Even so, v0.6.0's
byte comparison flagged the run `DIRECTIVES_MISMATCH` in `evaluate_run` and as a
parity failure in `compare_runs`. Both now decode the base64 and compare the text
with trailing whitespace stripped. `evaluate_run` notes the case in
`checks.directives.note`, and `compare_runs` lists it under
`whitespace_only_differences`. A genuinely different block, such as the garbled
`site-runner-gce-cpu-000000`, is still flagged.
