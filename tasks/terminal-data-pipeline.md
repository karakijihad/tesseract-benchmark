# Example: terminal data pipeline

## Reference mode

`mixed`

## End goal

Create a reproducible command that transforms messy input data into a validated output dataset and summary.

## Contestant brief

Inspect the input files and transformation specification. Build a command that normalizes the records, handles malformed rows according to the specification, writes the required output formats, and produces a summary report. Run the command twice and demonstrate that the result is deterministic and idempotent.

## Minimal reference

Supply input data and the basic transformation specification. Require the agent to research one format or standard detail that the specification leaves open and record the decision.

## Validator

- Hidden input fixtures produce the expected output.
- Output schema and ordering are correct.
- Malformed rows follow the declared policy.
- A second run produces the same result.
- The command exits non-zero for unrecoverable input.
- The research note and source are present.

## What it measures

Terminal fluency, data reasoning, edge-case handling, research under incomplete requirements, reproducibility, and careful error handling.
