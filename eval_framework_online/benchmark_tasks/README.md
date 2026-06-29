# Benchmark Tasks

This directory is the single entrypoint for benchmark task resources used by `unified_safety_eval`.

Expected layout:

```text
benchmark_tasks/
  agentdojo/
  agentharm/
  agentlab/
  agent_safetybench/
  asb/
  lps_bench/
  vendor/
    toolsafe/
    purplellama/
```

Use these folders for released task data, task environments, grading functions, official evaluator helpers, and benchmark-local tool implementations. Do not add generated run outputs, logs, model responses, or temporary caches here.

The default YAML config points to this layout. If a benchmark's upstream release uses a different file structure, either normalize it into this directory or override the matching `benchmarks.<name>.*` path in `unified_safety_eval/config.yaml`.
