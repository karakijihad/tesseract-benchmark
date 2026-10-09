# Evidence Desk

A command line tool that reads evidence records from JSON, filters them, calculates a priority and writes a Markdown or JSON report.

## Install

It uses only the Python 3.12 standard library, so there is nothing to install. Run it from this folder.

## Usage

```text
python -m evidence_desk --input data/findings.json --format markdown --output report.md
python -m evidence_desk --input data/findings.json --status open --severity high --category maintenance --format json --output report.json
```

Filters (`--status`, `--severity`, `--category`) are combined with AND. Results are sorted by priority, highest first, then by record ID. A filter that matches nothing writes an empty report and exits with status 0. Bad input exits with status 2 and explains the problem.

## Test

```text
python -m unittest discover -s tests
```

## Verify

Run the two usage commands above and open the reports. Every record shows its original source URL.
