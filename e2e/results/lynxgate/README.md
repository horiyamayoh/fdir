# LynxGate benchmark results

Each run is stored below a name such as `baseline-<source-sha>` or
`final-<source-sha>`. The directory name and `summary.json` bind the result to
the exact converter commit used for the run.

Every run contains:

- `summary.json`: run metadata, environment, counts, status, and representative artifacts.
- `cases.jsonl`: one compact, SHA-bound record for every corpus document.
- `representatives/`: selected full IR and evidence sidecars for review.

Run verification with:

```text
python tools/run_real_input_benchmark.py verify --report e2e/results/lynxgate/<run-id>/summary.json
```

`passed` means the run processed the complete inventory without infrastructure
errors or unexpected conversion failures. It does not mean that every source
document converted to `complete`; `partial`, `complete-with-warnings`, and
declared `expected-limit` outcomes remain explicit in the case records.

These are practical real-input observations. They do not replace the
commit-bound qualification bundle or change the repository's release-blocked
state.
