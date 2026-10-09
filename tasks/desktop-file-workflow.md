# Example: desktop file workflow

## Reference mode

`supplied`

## End goal

Organize a mixed local document folder and create an auditable summary without losing or altering source files.

## Contestant brief

Inspect the supplied folder, classify each file using the rules, create the required destination structure, copy or move only what the rules permit, and write an audit report with filenames, hashes, and exceptions. Preserve source content. Verify the final tree.

## Minimal reference

Supply the folder, classification rules, and required output report format. The validator keeps expected hashes outside the workspace.

## Validator

- Expected files are in the correct locations.
- Source hashes are preserved where copying is required.
- No files outside the rules were changed or deleted.
- The audit report is complete and consistent with the final tree.
- Exceptions are reported instead of silently discarded.

## What it measures

Computer use, file reasoning, safe side effects, careful classification, auditability, and completion evidence.
