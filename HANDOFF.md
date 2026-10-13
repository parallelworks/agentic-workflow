# HANDOFF: `run_heat` MCP server and `site-runner-mcp` agent

## How to use this document

You are a Claude Code (or `pw code`) session being asked to extend this
repository. **Do not start writing code from this document alone.** First read
the "Required reading" section, then produce a written PLAN and get it approved,
then implement, then run the verification in "Acceptance." This document tells
you *what* to build and *why*, and it deliberately flags where its own author
was uncertain — treat those flags as instructions to verify against primary
sources, not as settled fact.

This work is **exploratory**. The goal is to make a comparison possible, not to
hit a fixed performance target. Nothing here should be treated as a benchmark to
pass; the deliverable is a working alternative and the means to compare it.

---

## Background and goal

This repository drives a fleet of HPC clusters (Slurm/PBS "targets") from
`pw code` using a small set of custom agents and platform-logged workflows. The
current run path works like this: an agent (`site-runner`) reads a target from
`sites/sites.yaml`, derives all the fields needed for a job, base64-encodes the
scheduler directives, stages a per-run inputs file, and invokes
`pw workflows run` to execute a merged stage+build+submit workflow (`run.yaml`)
on the cluster. Every one of those steps is currently done by the model
generating shell commands.

**Observed problem (the motivation, not a test):** constructing that
`pw workflows run` invocation correctly — the exact flags, the inputs JSON, the
base64 directives, the derived fields — is a long chain of exact-syntax steps.
More capable models do it reliably; less capable models often malform the
invocation. The *hypothesis* is that moving this fragile chain out of the model
and behind a single structured tool call will make the run step more robust,
especially for smaller models, by replacing free-form shell generation with one
typed tool invocation. **This is a hypothesis to explore, not a goal to certify.**

**What to build:** an agent-scoped MCP server exposing a single tool,
`run_heat(target, grid)`, that performs the entire run-invocation chain in
deterministic code, and a new agent, `site-runner-mcp`, that uses it. The
existing `site-runner` (which builds the invocation via shell) stays in place,
unchanged, so the two can be compared directly and repeatedly. Also build a
separate `compare-agents` agent whose job is that comparison — testing the
*agent* hypothesis (model-to-model change) as a distinct concern from
`evaluator`'s *application* hypothesis (site-to-site change). See the
Comparability section for its full scope and rationale.

---

## Required reading (read these before planning)

Read the **committed** versions of these files in this repository; they are the
source of truth, not any description in this handoff:

- `AGENTS.md` — project instructions, the run procedure, the constraints.
- `.agents/agents/site-runner.md` — the agent you are cloning-and-modifying.
- `.agents/agents/campaign-orchestrator.md` — delegates to run agents; must learn
  about the new one.
- `.agents/agents/evaluator.md` and `.agents/agents/capacity-analyst.md` — for
  context on the roster; likely unchanged. Note `evaluator`'s scope specifically,
  because the new `compare-agents` agent must be scoped to NOT overlap it
  (`evaluator` compares the application across clusters; `compare-agents` compares
  the run agents across models — see the Comparability section).
- `workflows/run.yaml` and `workflows/run.inputs.json` — the exact inputs the run
  workflow expects. The MCP tool must produce exactly these inputs.
- `scripts/_site_query.py` — the shared `sites.yaml` parser. **The MCP server
  must reuse this** (see "Single source of truth" below).
- `sites/sites.yaml` — target definitions and the field shapes.

Read this `pw code` documentation (verify against the live docs; they change):

- MCP Servers: https://parallelworks.com/docs/ai/code/mcp
- Custom Agents (esp. "MCP Servers for an Agent"):
  https://parallelworks.com/docs/ai/code/custom-agents
- Permissions: https://parallelworks.com/docs/ai/code/permissions
- Settings (precedence, Workspace Trust):
  https://parallelworks.com/docs/ai/code/settings

For **authoring** the MCP server (the protocol/SDK implementation itself), the
`pw code` docs do NOT cover this — it is standard Model Context Protocol. Consult
the current MCP SDK documentation at https://modelcontextprotocol.io and verify
the current API before writing server code. Do not rely on remembered SDK
details; the protocol and SDKs evolve.

---

## Confirmed design decisions

These were decided by the repository owner. Implement them as given.

1. **Return-after-submit.** `run_heat` submits the job and returns immediately
   with a structured result (including the run id and the scheduler job id). It
   does NOT block until the job finishes. Polling stays a separate step, exactly
   as it is today (the existing `poll` workflow / the agent's poll behavior is
   unchanged). Do not build polling into `run_heat`.

2. **Agent-scoped MCP server.** The server is attached to the new
   `site-runner-mcp` agent via that agent's own `mcpServers` list in its
   definition (see the Custom Agents doc, "MCP Servers for an Agent"). It must
   NOT be configured session-wide. Only `site-runner-mcp` should see the
   `run_heat` tool. Consider limiting that agent's `tools` to essentially just
   its own server's tool (plus whatever minimum it needs), so its job is as
   legible as possible.

3. **Two parallel agents, kept side by side.**
   - `site-runner` — the existing agent, UNCHANGED. Builds the `pw workflows run`
     invocation via shell/`Bash`. Kept as the comparison baseline.
   - `site-runner-mcp` — NEW. Same job, same result, but invokes the run through
     the `run_heat` MCP tool instead of constructing shell commands. This is the
     variable under test.
   Both must produce equivalent runs (same workflow, same inputs, same cluster
   behavior) so that any difference in behavior, reliability, or cost is
   attributable to the invocation mechanism, not to doing different work.

4. **Implement the MCP server in Python.** Python reuses `scripts/_site_query.py`
   most directly (import or subprocess), keeping the single-source-of-truth
   requirement simple. Use a current, maintained MCP server library for Python
   (verify the current SDK/API against https://modelcontextprotocol.io before
   writing code — do not rely on remembered API details).

5. **`site-runner-mcp` is the DEFAULT run agent.** If a user request does not
   name which run agent to use, `site-runner-mcp` should be chosen.
   `site-runner` should be used only when explicitly named (for comparison).
   NOTE: `pw code` selects delegates by matching the request against agent
   `description` fields; there is no documented "default agent" flag. Achieving
   "default" therefore likely means engineering the `description` fields and
   `AGENTS.md`/orchestrator guidance so a generic run request matches
   `site-runner-mcp`, while `site-runner`'s description marks it as an
   explicit-only comparison baseline. **Treat the exact mechanism as a design
   question to solve during planning** — confirm against the Custom Agents and
   Subagents docs how delegate selection actually works, and implement "default"
   in whatever way those docs support. Do not assume a flag exists.

---

## Single source of truth: reuse `_site_query.py`

The MCP server MUST derive target fields by reusing `scripts/_site_query.py`,
not by reimplementing `sites.yaml` parsing. This is a hard requirement, for two
reasons:

1. One source of truth for parsing avoids drift between the two run paths.
2. **Comparability.** Both `site-runner` and `site-runner-mcp` must resolve
   targets through the *same* logic, so that a comparison between them isolates
   the invocation mechanism as the only variable. If the MCP server parsed
   `sites.yaml` differently, differences in outcome could not be cleanly
   attributed.

Reuse can be by invoking `_site_query.py` as a subprocess (the same way the
shell path calls it) or by importing it as a module — planner's choice — but the
*logic* must be the shared script, not a copy. If `_site_query.py` needs a small,
backward-compatible extension to be importable/reusable cleanly, that is
acceptable; changing its existing behavior is not.

---

## Constraints that must hold (non-negotiable)

These are existing repository invariants. The MCP server must preserve them; it
should, if anything, enforce them more strongly than the prose-instructed shell
path does.

- **Everything that happens on a compute target is mediated by
  `pw workflows run`.** The server's only means of acting on a cluster is to
  invoke `pw workflows run` against the existing `run.yaml`. No new cluster-
  access path is introduced.
- **No `pw ssh` and no `scp`.** The server must not shell to a cluster directly
  and must not move files by scp. (Source reaches the cluster via the git clone
  already inside `run.yaml`; results stay on the cluster.) The server must never
  read or handle SSH keys or other secrets.
- **Directives are passed verbatim, base64-encoded**, exactly as the current
  path does (via `_site_query.py ... directives | base64`). The server does not
  parse or interpret them.
- **`mpi_ranks` is explicit and never inferred**; the solver runs exactly the
  target's declared ranks.
- **Enforce `grid >= mpi_ranks` in the server, deterministically.** A grid
  smaller than the rank count causes the solver to `MPI_Abort` on every rank.
  The server must check this before submitting and return a clear, structured
  error instead of launching a doomed job. (This is a guardrail the shell path
  does not currently enforce up front; moving it into the server is a side
  benefit and should be documented as an intentional difference — but note it in
  the comparison writeup, since it is a behavioral difference between the two
  agents.)
- **Run identity and layout unchanged.** The server produces the same run-id
  scheme (`site-runner-mcp-<target>-<NNNNNN>` for the new agent), stages under
  `./runs/<id>/`, sets `remote_dir` to the relative `pw-heat/<id>` form, and uses
  the id as the `pw workflows run --name`. Preserve the existing traceability
  contract (local `./runs/<id>/`, `--name`, remote `$HOME/pw-heat/<id>/`).

---

## The `run_heat` tool contract

Design the tool for a *less capable* model: coarse, obvious, few parameters.

- **Name:** `run_heat` (exposed to the model as `mcp__<server>__run_heat`; pick a
  short server name, e.g. `heat`).
- **Inputs:** at minimum `target` (string, must exist in `sites.yaml`) and
  `grid` (integer). Consider whether `tol` and `max_iter` should be optional
  parameters with sensible defaults, or fixed — prefer fewer parameters unless
  there is a clear need. Do not require the model to pass anything it cannot
  trivially know (no derived fields, no base64, no paths).
- **Behavior:** resolve the target via `_site_query.py`; validate
  `grid >= mpi_ranks`; generate the run id; stage `./runs/<id>/` with the filled
  `run.inputs.json` and `run.yaml`; execute `pw workflows run -i ... --name <id>
  ...`; parse the submission result.
- **Return:** a structured, machine-readable result (JSON) including at least:
  the run id, the target, the scheduler job id, submit status (submitted/failed),
  and on failure a clear error message (e.g. the `grid >= mpi_ranks` violation,
  or the platform's error verbatim). This return is what the model reasons about
  next (e.g. to poll). Keep it small and clean — do not dump raw platform logs
  into the return if they are large (recall the repo has a token-cost concern).

---

## Files to change vs. leave untouched

**New:**
- The MCP server program, **in Python** (reuses `_site_query.py` most directly).
  Place it sensibly in the repo (e.g. a `mcp/` directory); document how it is
  launched (the command/args that go in the agent's `mcpServers` entry).
- `.agents/agents/site-runner-mcp.md` — the new run agent, with its own
  `mcpServers` entry and a `tools` list scoped tightly around the MCP tool.
- `.agents/agents/compare-agents.md` — the new comparison agent (see the
  Comparability section for its full scope, rationale, and what it compares). It
  is separate from both `evaluator` and `campaign-orchestrator`, runs post-hoc
  after all model switching is done, and for now compares `site-runner` with
  `site-runner-mcp`.

**Changed:**
- `AGENTS.md` — document that there are now TWO run agents, what each does, how
  they differ (shell-built invocation vs. MCP tool), the `grid >= mpi_ranks`
  enforcement difference, and the default-selection behavior
  (`site-runner-mcp` by default, `site-runner` only when explicitly named). Also
  document the new `compare-agents` agent and the two-hypotheses framing: that
  `evaluator` compares the *application* across a site-to-site change while
  `compare-agents` compares the *run agents* across a model-to-model change, and
  that `compare-agents` runs post-hoc (after model switching) and is deliberately
  separate from `campaign-orchestrator`. Keep it concise and within the file's
  size budget.
- `.agents/agents/campaign-orchestrator.md` — teach it that the default run
  delegate is `site-runner-mcp`, and that `site-runner` exists as an
  explicit-only comparison baseline. The orchestrator should delegate to
  `site-runner-mcp` unless the user asks for `site-runner`.
- Possibly `scripts/_site_query.py` — only if a small, backward-compatible change
  is needed to make it cleanly reusable by the server. Do not change its existing
  outputs.

**Leave untouched:**
- `workflows/run.yaml`, `workflows/run.inputs.json`, and all other workflows.
  The server *calls* `run.yaml`; it does not replace or modify it.
- `sites/sites.yaml`, the heat application, the other agents
  (`evaluator`, `capacity-analyst`), and the existing `site-runner`.

---

## Configuration and permissions (verify against docs)

- Configure the server **agent-scoped**, inside `site-runner-mcp.md`'s
  `mcpServers`, per the Custom Agents doc. Do not add it to
  `.agents/settings.json` session-wide.
- Be aware of the settings **precedence/replacement** rule (highest-precedence
  non-empty map wins wholesale) if any server config lands in a settings file;
  the agent-scoped approach mostly sidesteps this, but confirm.
- Workspace Trust: workspace config (including agent definitions and any MCP
  config) only applies after the user approves it at launch. Document that
  participants must approve on first launch, or `site-runner-mcp`/its server will
  not load.
- Pre-approving the tool (e.g. `--allowedTools mcp__heat__run_heat` or a
  `/permissions add`) avoids a per-call confirmation prompt. Document this;
  decide whether to recommend it for the comparison runs so the two agents face
  comparable friction.

---

## Comparability: the `compare-agents` agent (first-class deliverable)

The reason both run agents exist is to compare them. This comparison is
**scientific** — it tests a distinct hypothesis from the one `evaluator` tests.
Be clear about the two hypotheses, because they define two different agents:

- **`evaluator`** tests the *application* hypothesis: across a **site-to-site**
  change, does the heat simulation reproduce the same solution (portability) and
  how does its run time differ (performance)? Its subject is the workload output
  on the clusters.
- **`compare-agents`** (NEW) tests the *agent* hypothesis: across a
  **model-to-model** change, how do the two run mechanisms —
  `site-runner` (shell-built invocation) and `site-runner-mcp` (MCP tool
  invocation) — differ in their actions, the equivalence of the work they
  produced, and their cost? Its subject is the *output of the run agents
  themselves*, not the simulation.

Both are scientific comparisons; they simply vary different things (the cluster
vs. the model) and examine different outputs (the application vs. the agent).
`compare-agents` must be its **own agent**, separate from both `evaluator` and
`campaign-orchestrator`.

### Why `compare-agents` is separate from `campaign-orchestrator`

The comparison is a **post-hoc** activity that runs *after* all model switching
is complete. The intended workflow is: run a job through `site-runner` under one
model, switch the session's model, run the same job through `site-runner-mcp`
under another model (or the same model at a different time), and only once all
such switching is done, invoke `compare-agents` to analyze the results.

`campaign-orchestrator`, by contrast, coordinates live runs and needs
**continuous access to a single model** for the duration of a campaign;
switching its model mid-campaign would be disruptive. Folding the comparison into
the orchestrator would entangle these two incompatible lifecycles. Keeping
`compare-agents` separate gives it its own lifecycle and model context, cleanly
decoupled from the orchestrator's continuous-model requirement. (This is a design
rationale to preserve, not merely an implementation note.)

### Scope of `compare-agents` (now, and later)

- **Now:** it compares exactly two agents — `site-runner` and `site-runner-mcp` —
  for the same logical job (`target`, `grid`).
- **Later (design for, do not build):** the same agent may compare other
  MCP-enabled agents against their non-MCP-enabled siblings as the pattern
  extends. Write its definition so the *concept* is "compare an MCP-enabled agent
  with its non-MCP sibling," with `site-runner`/`site-runner-mcp` as the first
  such pair — but only that pair is in scope now.

### What `compare-agents` compares

For the same `(target, grid)` run through each agent, it examines:

- **Parity of work.** Did the two agents submit the *same* workflow with the
  *same* inputs? Both resolve targets through the shared `_site_query.py` and
  submit the same `run.yaml`, so the staged `run.inputs.json` (resolved fields,
  base64 directives, `remote_dir`, `--name` scheme) should match. Confirm this,
  and call out any **intentional** difference — notably the up-front
  `grid >= mpi_ranks` guard that lives in the MCP path but not the shell path.
- **Actions taken.** The exact `pw workflows run` command each path produced, and
  the sequence of steps/tool calls each agent took to get there (the shell path's
  many `Bash` steps vs. the MCP path's single `run_heat` call).
- **End results.** The run records and, where relevant, the evaluator's
  scientific verdicts for the resulting runs. `compare-agents` **consumes** the
  evaluator's output where useful; it does not re-run the application comparison.
- **Cost.** Overall **token usage** per agent for the same task, plus tool-call
  and turn counts. See the metric-access caveat below.

### Metric access — verify, do not assume

Comparing **actions**, **parity of staged inputs**, and **end results** is
possible from durable artifacts: the `./runs/<id>/` staging directories (the two
agents' differing id prefixes — `site-runner-...` vs `site-runner-mcp-...` — make
them easy to pair and tell apart), the rendered commands, and the run records /
verdicts. `compare-agents` should base its comparison on these artifacts.

**Token and turn counts are a known unknown.** It is NOT established that an agent
can programmatically read another agent's (or its own) token usage at runtime;
these may be visible only to the human in the `pw code` UI. The receiving session
MUST verify what `pw code` exposes to an agent. If per-agent token/turn metrics
are not machine-readable by an agent, then `compare-agents` compares everything
it can from artifacts and **clearly states that token/turn figures must be
supplied by the human** (or read from wherever the tool surfaces them), rather
than fabricating or estimating them. Do not have the agent invent metrics it
cannot actually obtain.

### Reproducibility

Because both paths resolve targets through the same `_site_query.py` and submit
the same `run.yaml`, repeated runs of the same `(target, grid)` are directly
comparable across agents and across models. Preserve the `./runs/` artifacts as
the durable record that makes repeated comparison possible.

---

## Suggested plan shape (produce this before implementing)

Your PLAN should, at minimum:

1. Summarize the current run path from reading the committed files, and identify
   the exact inputs `run.yaml` requires (so the tool produces them precisely).
2. Decide how the server reuses `_site_query.py` (subprocess vs. import) and note
   any minimal, backward-compatible change needed.
3. Specify the Python MCP server: chosen MCP library, launch command, tool schema
   for `run_heat`, the return shape, and how it enforces the constraints (esp.
   `grid >= mpi_ranks` and `pw workflows run`-only).
4. Specify `site-runner-mcp.md`: `mcpServers` entry, `tools` scoping, `description`
   (written so it is the default match for a generic run request), body prompt.
5. Specify the edits to `AGENTS.md` and `campaign-orchestrator.md`, including how
   "default = `site-runner-mcp`" is actually achieved given how delegate
   selection works.
6. Specify `compare-agents.md`: its scope (compare `site-runner` vs
   `site-runner-mcp` for the same job now; designed to extend to other
   MCP/non-MCP sibling pairs later), why it is separate from
   `campaign-orchestrator` (post-hoc, after model switching; orchestrator needs a
   continuous model), what it compares (parity of staged inputs, actions, end
   results consuming `evaluator`'s verdicts, cost), and how it reads or is given
   token/turn metrics (verify what `pw code` exposes to an agent).
7. List the verification steps (below) you will run.

Get the plan reviewed before implementing.

---

## Acceptance (verification, not benchmarks)

Verify functionally; do not treat any model's success as a fixed goalpost.

- **Config loads.** `site-runner-mcp` appears as an agent; its `run_heat` tool is
  discovered (check `/mcp`) after workspace approval. The server is visible ONLY
  to `site-runner-mcp`, not session-wide.
- **Constraint preservation.** By inspection of the server code and a dry
  run/log: the only cluster action is `pw workflows run` against `run.yaml`; no
  `pw ssh`, no `scp`, no secret/key handling.
- **Parity.** For a given `(target, grid)`, the inputs the MCP path stages match
  what the shell path would stage (same resolved fields via `_site_query.py`,
  same base64 directives, same `remote_dir`/`--name` scheme). Document any
  intentional difference.
- **Guardrail.** `run_heat` with `grid < mpi_ranks` returns a clear structured
  error and does NOT submit.
- **Return-after-submit.** The tool returns promptly with run id + job id and
  does not block on job completion; polling still works as a separate step.
- **Default behavior.** A generic run request (not naming an agent) is handled by
  `site-runner-mcp`; naming `site-runner` explicitly still uses the shell path.
- **Comparability via `compare-agents`.** The `compare-agents` agent exists, is
  separate from both `evaluator` and `campaign-orchestrator`, and for the same
  `(target, grid)` run through each run agent it can pair the two `./runs/`
  artifacts, confirm parity of staged inputs, contrast the actions/commands, and
  incorporate the evaluator's verdicts. It reports token/turn cost where `pw code`
  exposes those to an agent, and otherwise clearly states those figures must be
  supplied rather than fabricating them. Confirm it runs correctly post-hoc
  (after model switching) without requiring the orchestrator.

## Explicitly out of scope for this pass

- Wrapping poll/fetch/validate/capacity as MCP tools. Only the run step is being
  wrapped now. (These may follow later if the approach proves worthwhile.)
- Any change to `run.yaml` or the other workflows.
- Certifying any particular model's success. This is exploratory; the deliverable
  is the working `site-runner-mcp` alternative plus the `compare-agents` agent
  that makes comparing it with `site-runner` (across models) a repeatable,
  first-class capability.
