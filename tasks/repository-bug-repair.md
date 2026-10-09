# Example: repository bug repair

## Reference mode

`supplied`

## End goal

Fix a real defect in an unfamiliar small repository without breaking existing behavior.

## Contestant brief

Read the repository instructions and issue report. Reproduce the failure, identify the cause, implement the smallest safe fix, add a regression test, and run the full test suite. Do not rewrite unrelated modules. Report the original failure, the fix, the tests run, and any remaining uncertainty.

## Minimal reference

Supply the repository, issue report, visible tests, and runtime command. Keep the hidden regression tests outside the contestant workspace.

## Validator

- Hidden regression tests pass.
- Existing tests pass.
- A regression test was added or the existing test was meaningfully strengthened.
- The diff stays within the relevant scope.
- The final report matches the observed test output.

## What it measures

Repository inspection, debugging, hypothesis testing, code quality, regression prevention, scope control, and evidence-based reporting.
