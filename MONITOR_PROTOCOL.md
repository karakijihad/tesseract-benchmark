# Monitor protocol

## Monitor role

The monitor controls the experiment, not the solution. It may create folders, start and stop contestants, capture evidence, run the independent validator, calculate the score, and clean up processes.

It must not:

- Explain the task beyond the frozen brief.
- Suggest an implementation.
- Repair contestant files.
- Answer a contestant's design question.
- Show one contestant another contestant's work.
- Ask a model or agent outside the contestant's own system to review or complete a contestant's work.
- Count its own work as contestant work.

## Contestant roles

The contestants are:

- `tesseract`: a clean TESSERACT run.
- `claude-code`: a separate Claude Code process, not the monitor process.
- `codex`: a separate Codex process.

Each contestant has its own directory, transcript, process tree, and usage record.

Each contestant is the whole system. It may use its own sub-agents, workers, lanes, or parallel sessions the way it normally would, and the monitor does not discourage or block that. Everything those sub-agents do belongs to the contestant's run.

## Isolation

Before each run, the monitor must:

1. Copy the frozen starter project into a new run directory.
2. Remove prior build outputs and temporary files.
3. Record runtime versions and relevant environment metadata.
4. Confirm that the contestant cannot read another run directory.
5. Confirm that the validator and hidden tests are outside the contestant directory.
6. Start the contestant with the exact same task brief.

## Observation

The monitor captures, where available:

- Prompt and final response.
- Tool calls and tool results.
- Process creation and termination.
- URLs used for research.
- File changes.
- Test, build, and server output.
- Token or usage counts.
- Wall-clock timestamps.
- Sub-agents started by the contestant, and the calls they made.
- Any sign of outside help.

A monitor event must identify its source as `monitor`, `contestant`, or `validator`.

## Sub-agent accounting and outside help

The monitor counts the contestant's sub-agents and adds every call they make to the contestant's totals: tokens, cost, model calls, tool calls, and wall-clock time. The run record reports the sub-agent count. Using sub-agents is not a violation and is not penalized as such. It shows up in the contestant's numbers.

The monitor checks for outside help, which is the only protocol violation in this area: a human, the monitor, or another contestant doing any of the work. Ordinary local commands remain allowed. The monitor should flag, not hide, any uncertain event and record the reason for the final decision.

A confirmed finding of outside help makes the run non-clean. The validator may still calculate the artifact's technical score, but the headline result must say `protocol_violation: outside_help` and must not present the result as an independent agent score.

## Time limit

The pilot should use a fixed time limit chosen before the first run. The monitor stops the contestant at the limit, lets already-running ordinary validation commands finish only if safe, and records the result as incomplete if required acceptance work is missing.

The monitor must not extend one contestant's time because it is behind another contestant.

## Research policy

Public research is allowed for this task. The monitor records the URLs used when the environment exposes them. The finished project must not make runtime network calls. Source claims are checked after the run.

## Validation sequence

After the contestant stops:

1. Check required files.
2. Create a clean validation environment.
3. Install dependencies from the recorded project files.
4. Run the project's tests.
5. Run hidden acceptance tests.
6. Run the documented command with valid filters.
7. Run malformed and empty-result cases.
8. Inspect source URL preservation and deterministic ordering.
9. Check the README commands.
10. Check that no runtime network service is required.
11. Save machine-readable validation results.

The validator must never import or execute the contestant's untrusted helper scripts beyond the commands necessary to test the project. The exact sandbox policy belongs in the runner implementation.

## Cost policy

Every run records:

- `cost_status`: `exact`, `estimated`, or `unavailable`.
- `estimated_cost_usd`, if a defensible value exists.
- `cost_basis`.
- Input and output usage, if exposed.
- Wall-clock duration.
- Active turns.
- Tool calls.
- Failed calls and retries.
- Sub-agents used.

All of these include the work done by the contestant's sub-agents.

Subscription access with no per-task billing must not be reported as an exact zero-cost API run.

## Completion rule

A contestant is complete only when the artifact passes the validator and the final report accurately describes the evidence. A final message that says it is done without passing checks is not a completion.
