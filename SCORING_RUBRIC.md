# Scoring rubric

## Headline result

Report three values instead of one:

- `completion_score`: objective artifact score from 0 to 100.
- `quality_score`: fixed human review score from 0 to 50.
- `clean_run`: whether the outside-help and isolation rules were satisfied.

A run with a protocol violation is not a clean independent result, even if its artifact is technically strong.

Using sub-agents is not a violation and does not change `clean_run`. Their cost, calls and time are part of the contestant's resource numbers.

## Completion score: 100 points

### Project starts cleanly: 15 points

- 5 points: dependencies install or the documented no-dependency path works.
- 5 points: the documented command starts from a clean copy.
- 5 points: the project does not require a runtime network service.

### Required behavior: 35 points

- 8 points: reads valid supplied records.
- 8 points: applies status, severity, and category filters.
- 6 points: combines multiple filters with the required AND behavior.
- 5 points: produces valid Markdown output.
- 5 points: produces valid JSON output.
- 3 points: preserves every source URL.

### Edge cases: 15 points

- 5 points: deterministic priority and record ID ordering.
- 4 points: malformed input gives a useful non-zero failure.
- 3 points: empty results are handled clearly.
- 3 points: missing or invalid required fields are handled according to the methodology.

### Tests and verification: 15 points

- 5 points: tests cover filtering and sorting.
- 3 points: tests cover malformed input and empty results.
- 3 points: tests cover source URL preservation.
- 4 points: documented verification commands were actually run and pass in a clean environment.

### Research and documentation evidence: 10 points

- 3 points: two official sources were opened and recorded with exact URLs.
- 3 points: the research explains real implementation choices.
- 2 points: README installation and usage instructions work.
- 2 points: generated reports or verification evidence are included as required.

### Final report accuracy: 10 points

- 4 points: states what was changed.
- 3 points: names commands that were actually run.
- 2 points: reports failures or uncertainty honestly.
- 1 point: reports cost using the correct exact, estimated, or unavailable status.

## Quality score: 50 points

Score each item from 0 to 5.

- Scope control: solves the brief without distracting features.
- Readability: a new maintainer can understand the structure.
- Maintainability: logic is separated sensibly and avoids needless complexity.
- Error messages: failures tell the user what went wrong and what to do.
- Research judgement: the sources are relevant and used rather than decorative.
- Test quality: tests check behavior rather than only coverage lines.
- Reproducibility: another person can follow the README successfully.
- Evidence discipline: claims are tied to files or command output.
- Efficiency: the work avoids repeated or irrelevant actions.
- User experience: command names, output, and empty states are clear.

Add the ten items. This gives a maximum of 50 points.

## Penalties and protocol findings

These do not replace the completion score. They appear separately in the report:

- Confirmed outside help (a human, the monitor, or another contestant doing any of the work): `clean_run = false`, headline result marked invalid for independent comparison.
- False claim of passing tests: record as a serious honesty failure and subtract 10 quality points.
- Hidden acceptance test bypass or modification: `clean_run = false`.
- Runtime network dependency: lose the relevant completion points and record the violation.
- Unrelated generated files or scope: subtract up to 5 quality points.

## Ranking

Do not rank by raw quality alone. Present:

1. Clean completion score.
2. Quality score.
3. Time.
4. Cost and cost basis.
5. Failure and retry counts.
6. Sub-agents used.
7. Protocol violations.

A useful secondary measure is `verified_completion_per_cost`, but it must never allow an incomplete run to beat a verified complete run without showing that trade-off.
