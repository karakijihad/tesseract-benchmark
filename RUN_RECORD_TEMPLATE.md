# Run record template

Use one copy per contestant and run ID. The runner should generate machine-readable JSON with this shape as the canonical record. Provider-specific details belong under `raw_provider_usage`; they must not replace the normalized fields.

```yaml
run_id: "YYYYMMDD-task-contestant-seq"
task_id: "evidence-desk-v1"
contestant: "tesseract | claude-code | codex"
monitor: "claude-code-monitor"
track: "native-system"
started_at: ""
ended_at: ""
time_limit_minutes: 0
runtime:
  os: ""
  python: ""
  node: ""
  git: ""
  contestant_version: ""
  monitor_version: ""
prompt:
  delivery_mode: "stdin | file | argument | environment | interactive"
  delivery_verified: false
  acknowledgement: ""
isolation:
  clean_workspace: false
  separate_process: false
  contestant_root_isolated: false
  task_metadata_hidden: false
  global_run_dir_hidden: false
  hidden_tests_outside_workspace: false
  validator_unreachable: false
  other_runs_readable: false
network:
  research_allowed: true
  runtime_network_allowed: false
  urls_observed: []
protocol:
  sub_agents_used: null
  outside_help_detected: false
  outside_help_confirmed: false
  protocol_violation: "none | outside_help"
usage:
  # Every total below includes the calls made by the contestant's sub-agents.
  collection_status: "exact | partial | unavailable"
  input_tokens: null
  output_tokens: null
  total_tokens: null
  model_calls: null
  tool_calls: null
  failed_tool_calls: null
  retries: null
  active_turns: null
  wall_clock_seconds: null
  context:
    measurement_status: "exact | estimated | unavailable"
    max_context_tokens: null
    peak_context_tokens: null
    final_context_tokens: null
    by_turn: []
  cache:
    measurement_status: "exact | estimated | unavailable"
    provider_reported: false
    cache_read_tokens: null
    cache_write_tokens: null
    cache_hit_requests: null
    cache_miss_requests: null
    cache_savings_tokens: null
    cache_savings_cost_usd: null
    pricing_basis: ""
    notes: []
  raw_provider_usage: {}
cost:
  status: "exact | estimated | unavailable"
  estimated_cost_usd: null
  currency: "USD"
  basis: ""
validation:
  required_files: false
  dependencies_ok: false
  tests_passed: false
  hidden_tests_passed: false
  markdown_output_ok: false
  json_output_ok: false
  filters_ok: false
  sorting_ok: false
  malformed_input_ok: false
  empty_results_ok: false
  source_urls_preserved: false
  readme_commands_ok: false
  runtime_offline_ok: false
scores:
  completion_score: 0
  quality_score: 0
  clean_run: false
  notes: []
artifacts:
  workspace: ""
  transcript: ""
  events: ""
  usage: ""
  diff: ""
  validator_result: ""
  final_report: ""
```

## Field rules

- `null` means the adapter did not provide defensible evidence. It is not zero.
- `sub_agents_used` is the number of sub-agents, workers, lanes or parallel sessions the contestant started. `0` means none were used. `null` means the adapter could not tell.
- Every usage total includes the calls made by those sub-agents. Using sub-agents is not a violation.
- `outside_help_detected` and `outside_help_confirmed` cover a human, the monitor, or another contestant doing any of the work. Only a confirmed finding changes `protocol_violation` and `clean_run`.
- `measurement_status` says whether the value is exact, estimated, or unavailable.
- `cache_savings_tokens` is the primary cache comparison. `cache_savings_cost_usd` is secondary and requires the pricing basis to be recorded.
- `by_turn` may contain provider-specific turn identifiers, but the normalized context values must remain comparable across adapters.
- The validator result remains separate from contestant claims. A final message never overrides it.
