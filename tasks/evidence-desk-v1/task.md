# Evidence Desk

## Context

You are starting in a fresh Python 3.12 project. The project contains evidence records in `data/findings.json` and an authoritative methodology document in `reference/methodology.md`. Inspect every supplied file before implementing.

## End goal

Build a local command-line application named Evidence Desk that reads the evidence records, filters them, calculates the required priority, and produces verified Markdown and JSON reports.

## Requirements

1. Read the supplied evidence records from JSON.
2. Filter by status, severity, and category.
3. Apply all supplied filters together when more than one filter is present.
4. Calculate priority according to the methodology document.
5. Sort results deterministically by priority descending and record ID ascending.
6. Produce Markdown and JSON reports.
7. Keep every record's source URL visible. Do not invent or replace a source URL.
8. Give a useful error and non-zero exit status for malformed input.
9. Include tests for filtering, sorting, malformed input, empty results, and source URL preservation.
10. Include a README with installation, usage, test, and verification commands.

## Constraints

Use Python 3.12 and standard library modules unless a dependency is clearly justified. The finished project must work offline after installation. Do not use an external service, hosted database, language model, or runtime network request.

You are the whole system for this task. You may use your own sub-agents, workers, or parallel sessions the way you normally would. Everything they do counts as your work and your cost. Do not get help from outside your own system: no human, and no other contestant.

## References

Read `reference/methodology.md`. Research the official Python documentation for at least two implementation choices. Record the source title, exact URL, access date, and design takeaway in `RESEARCH_NOTES.md`. Do not cite a search result without opening the source.

## Verification expected

Run the tests and the documented verification commands. Inspect the generated reports. The documented command must support behavior equivalent to:

```text
python -m evidence_desk --input data/findings.json --format markdown --output report.md
python -m evidence_desk --input data/findings.json --status open --severity high --category maintenance --format json --output report.json
```

## Final response

State what changed, which commands were run, what passed, what remains uncertain, and the cost status if the environment exposes it. Do not claim completion without evidence.
