# Evidence Desk methodology

## Required fields

Every record must contain:

- `id`: non-empty stable string.
- `status`: `open` or `closed`.
- `severity`: `high`, `medium`, or `low`.
- `category`: non-empty string.
- `title`: non-empty string.
- `summary`: non-empty string.
- `source_url`: non-empty absolute URL string.
- `severity_weight`: integer where high is 3, medium is 2, and low is 1.

## Priority

Calculate:

```text
priority = severity_weight
if status is open, add 1
if category is maintenance, add 1
```

Sort by priority descending, then by `id` ascending.

## Invalid data

A malformed JSON document or a record missing a required field is an error. The command must exit non-zero and explain the problem. A valid filter that matches no records is not an error. It should produce an empty result with a clear message.

## Source handling

The application may display or copy a source URL, but it must not invent, rewrite, or silently remove one.
