# Example: browser workflow

## Reference mode

`supplied`

## End goal

Complete a multi-step workflow in a local mock web application and leave the correct backend state.

## Contestant brief

Use the local web application to find the records matching the supplied criteria, inspect their details, update the permitted record, and create the required confirmation note. Verify the final state before reporting completion. Do not modify records outside the requested scope.

## Minimal reference

Supply the local application URL, user goal, permitted fields, and confirmation rules. The application and backend are local and reset for every contestant.

## Validator

- Backend state contains the expected update.
- Records outside the requested scope remain unchanged.
- The confirmation note has the required content.
- The agent reached the correct final page or state.
- The report cites actual verification rather than only saying that the page looked correct.

## What it measures

Browser navigation, page understanding, form interaction, state tracking, safe side effects, visual and DOM reasoning, and verification.
