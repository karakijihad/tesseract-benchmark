# Agent task catalog

## What public evaluations tend to test

The recurring pattern across public agent evaluations is not one universal task. It is a set of environments that expose different failure modes:

| Capability | Common task shape | Typical objective check |
|---|---|---|
| Research and synthesis | Answer a hard question using several documents or web sources | Exact answer, source validity, claim support, citation completeness |
| Software engineering | Fix a real issue in an unfamiliar repository | Hidden tests, patch correctness, regression behavior |
| Terminal work | Complete a realistic command-line workflow | Exit status, output files, checksums, tests, service state |
| Browser work | Navigate realistic websites and complete a multi-step action | URL and page state, backend state, form values, transaction state |
| Desktop work | Use several applications or the file system to complete a workflow | Files, application state, database state, screenshots, UI properties |
| Tool and policy adherence | Call APIs while following user intent and domain rules | Tool trace, final state, confirmation behavior, policy violations |
| Planning and long horizon | Decompose a multi-stage goal, recover from failure, preserve progress | Milestone state, recovery, final artifact, time and action trace |
| Memory and instruction following | Apply supplied preferences or project rules across steps | Required constraints respected, disallowed changes absent |

These categories are reflected in public work such as [SWE-bench](https://www.swebench.com/), [Terminal-Bench](https://www.tbench.ai/), [GAIA](https://huggingface.co/gaia-benchmark), [τ-bench](https://taubench.com/), [BrowserGym](https://github.com/ServiceNow/BrowserGym), [WorkArena](https://servicenow.github.io/WorkArena/), [OSWorld](https://os-world.github.io/), and [Inspect AI](https://inspect.aisi.org.uk/). Anthropic's evaluation guidance also emphasizes checking final URL, page, backend, file, database, and application state instead of trusting a success message: [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).

## What makes a good local task

A useful local task has:

- A short brief and a clear end goal.
- Enough ambiguity to require judgement, but not enough ambiguity to make grading subjective.
- A fresh project or unfamiliar evidence.
- A validator that can inspect the result independently.
- A fixed reference policy.
- A hard boundary around outside help and external side effects.
- At least one opportunity to discover, test, or correct an assumption.
- A final report that can be checked against the run record.

## Recommended local task families

### 1. Evidence Desk CLI

Build a small evidence-processing tool from supplied records and a methodology document. Require official documentation research, tests, reports, and offline operation.

Measures: inspection, document understanding, research, implementation, testing, reporting, and how it uses its own sub-agents when it has them.

### 2. Research source audit

Give the agent a decision question and either a small candidate source set or no sources. Require a claim table, exact URLs, freshness dates, contradiction handling, and a recommendation with uncertainty.

Validator: open URLs, check required claims against source passages, check that every recommendation has evidence, and flag unsupported claims.

Measures: research planning, source quality, synthesis, citation discipline, and resistance to confident invention.

### 3. Repository bug repair

Give the agent an unfamiliar small repository with a failing test or a bug report. Require diagnosis, the smallest fix, a regression test, and a clean test run.

Validator: hidden tests, regression behavior, diff scope, and test output.

Measures: code reading, debugging, hypothesis testing, regression prevention, and scope control. This is the local analogue of SWE-bench.

### 4. Terminal data pipeline

Provide CSV or JSON input, a transformation specification, malformed rows, and expected output rules. Require a reproducible command, validation, and an idempotent result.

Validator: hidden fixtures, output schema, checksums, error handling, and repeat-run equality.

Measures: terminal fluency, data reasoning, edge-case handling, and reproducibility.

### 5. Browser workflow

Run a local mock site. Ask the agent to find records, apply filters, update a stateful object, and produce evidence. Do not use a screenshot alone as the success check.

Validator: backend state, URL state, database or fixture state, and required visible confirmation.

Measures: navigation, form interaction, state tracking, visual interpretation, and recovery from page changes.

### 6. Desktop file workflow

Give the agent a local directory with mixed documents and a clear filing or reporting goal. Require sorting, renaming, extracting a summary, and leaving an audit file.

Validator: exact file tree, hashes, report content, and absence of disallowed changes.

Measures: computer use, file reasoning, careful side effects, and completion evidence.

### 7. Tool and policy task

Provide simulated APIs for a domain such as travel, support, or inventory. Give the agent a user request plus rules about confirmation, refunds, permissions, or irreversible actions.

Validator: tool trace, final state, policy compliance, and whether the agent requested confirmation at the right moment.

Measures: intent understanding, tool selection, policy adherence, safe action, and honesty. This is the local analogue of τ-bench.

### 8. Long-horizon recovery task

Give the agent a multi-stage task with a failing check, a misleading note, and a dependency that must be inspected. Require it to diagnose the failure, update the plan, and finish without hiding the failure.

Validator: milestone artifacts, final tests, recovery trace, and final report accuracy.

Measures: planning, context management, self-correction, persistence, and resistance to blindly repeating a failed step.

### 9. Memory and instruction task

Provide project instructions that include a real boundary, such as do not edit a protected file, preserve a selected format, or use a specific output path. Add a task that tempts the agent to violate that boundary.

Validator: protected files unchanged, required format preserved, and final artifact complete.

Measures: instruction hierarchy, constraint retention, selective memory use, and safe refusal or redirection.

### 10. End-to-end delivery task

Start with a short idea and require a small project, tests, user documentation, and a verified local delivery. Do not allow deployment or external contact unless the task explicitly includes it.

Validator: artifact, tests, documentation, local route, evidence report, time, cost, and scope.

Measures: full agentic harness quality from planning through verified completion.

## Suggested first suite

Use these five before adding larger public benchmarks:

1. Evidence Desk CLI.
2. Research source audit.
3. Repository bug repair.
4. Browser workflow on a local mock site.
5. Long-horizon recovery task.

This covers research, coding, tools, browser interaction, planning, recovery, and reporting without requiring a large external infrastructure stack.
