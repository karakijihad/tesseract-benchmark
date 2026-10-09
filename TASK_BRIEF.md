# First benchmark task: Evidence Desk

This is the contestant-facing task. The monitor may keep hidden acceptance tests outside the contestant workspace.

## Task brief

You are starting in a fresh project with a small set of supplied evidence records and a methodology document. Build a local command-line application named Evidence Desk.

The application must:

1. Read the supplied evidence records from JSON.
2. Let a user filter records by status, severity, and category.
3. Apply all supplied filters together when more than one filter is present.
4. Produce Markdown and JSON reports.
5. Keep every record's source URL visible in the report. Do not invent or replace a source URL.
6. Sort results deterministically by priority and then by record ID.
7. Give a useful error and non-zero exit status for malformed input.
8. Work offline after dependencies are installed. Do not use an external service, hosted database, language model, or runtime network request.
9. Include tests for filtering, sorting, malformed input, empty results, and source URL preservation.
10. Include a short README with installation, usage, test, and verification commands.

Before implementing, inspect every supplied file. Read the methodology document and use it as the authority for the record fields and priority rules.

Research the official Python documentation for at least two implementation choices. Record the source title, exact URL, access date, and the design takeaway in `RESEARCH_NOTES.md`. The research must affect or explain a real implementation choice. Do not cite a search result without opening the source.

Use Python 3.12 and standard library modules unless a dependency is clearly justified. If you add a dependency, explain why and ensure the project still follows the offline rule at runtime.

Run the tests and the documented verification commands before finishing. Inspect the generated reports. In your final response, state what you changed, which commands you ran, what passed, what remains uncertain, and the estimated cost if your environment exposes it.

You are the whole system for this task. You may use your own sub-agents, workers, or parallel sessions the way you normally would, and everything they do counts as your work and your cost. Do not get help from outside your own system: no human, and no other contestant. Do not send the task to an external coding service that is not part of your normal setup.

## Starter material

The monitor will provide:

- `data/findings.json`, containing records with stable IDs, status, severity, category, title, summary, priority inputs, and source URLs.
- `reference/methodology.md`, containing the authoritative field definitions, validation rules, and priority calculation.
- A minimal Python project structure with no implementation.

The starter material will not contain the hidden acceptance tests.

## Minimum interface

The exact module layout is the contestant's choice. The project must expose a documented command that supports equivalent behavior to:

```text
python -m evidence_desk --input data/findings.json --format markdown --output report.md
python -m evidence_desk --input data/findings.json --status open --severity high --category maintenance --format json --output report.json
```

The validator may call the documented command with different valid input combinations.

## Required evidence before completion

The final project must contain:

- A working implementation.
- Tests.
- README.md.
- RESEARCH_NOTES.md with two opened official sources.
- At least one Markdown report and one JSON report produced by the documented commands.
- No unapproved external runtime service.
