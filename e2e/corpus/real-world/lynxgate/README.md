# LynxGate real-input corpus

This directory contains four committed ZIP archives used as a practical
DOCX/XLSX input corpus for FDIR. The archives are source inputs, not generated
fixtures and not release-qualification evidence.

The source manifest is [`manifest.json`](manifest.json). It pins each archive
and each DOCX/XLSX entry by SHA-256. The manifest currently describes 161
documents: 60 DOCX and 101 XLSX.

## Re-run

From the repository root, run the public conversion boundary through the
benchmark runner:

```text
python tools/run_real_input_benchmark.py run --manifest e2e/corpus/real-world/lynxgate/manifest.json --out e2e/results/lynxgate/<run-id>
```

The runner extracts inputs only into the ignored `e2e/.run/` workspace. It
streams one compact case record at a time and preserves the input, IR, and
evidence digests in the tracked report directory supplied by `--out`.

`conversionStatus` retains the adapter's vocabulary (`complete`,
`complete-with-warnings`, `partial`, or `failed`). A run is executable only
when every case has an explicit, schema-valid result and no infrastructure
error occurs. Partial conversion and warnings remain visible; they are not
promoted to complete. The known DOCX XML text-budget rejection is recorded as
an expected limit outcome, and the XML limit is not relaxed for this corpus.

Full per-case IR is deliberately not committed. The result directory contains
the all-case compact JSONL report plus deterministic representative IR/evidence
files for review.

Publication of these archives and representative outputs assumes that the
input owner has cleared the documents and extracted text for repository
publication.
