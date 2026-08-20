# Repository quality policy

`tools/quality.py` is the single repository-level quality entry point for local development, pull requests, main-branch integration, and future release qualification. It uses only the Python standard library and reads its pinned prerequisite policy from [`toolchain.json`](toolchain.json).

## Prerequisites and canonical command

The supported local runtime is CPython 3.12 or 3.13. CI uses the `.python-version` and `quality/toolchain.json` pin for CPython 3.12 on the pinned `ubuntu-24.04` runner image. No third-party Python package is required.

The canonical integration command is:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

The command writes a deterministic JSON receipt and returns a nonzero status whenever a required gate fails, raises unexpectedly, discovers no tests, encounters stale generated output, or cannot establish the requested cache or release state. Full discovery includes the Issue #12 adapter-protocol vectors and mock non-Rust process harness; their receipt contract is recorded in [`adapter-protocol.json`](adapter-protocol.json).

## Modes

| Mode | Purpose | Included authority | Release meaning |
|---|---|---|---|
| `fast` | Bounded developer feedback | Toolchain, text format, Python lint, documentation links, implementation/dependency policy, generated-contract parity, schema contracts, positive/negative fixtures, requirement traceability, baseline validation, and claim discipline | Never certifies a release |
| `full` | Required integration evidence | Every `fast` gate plus generated traceability, release-scope traceability, all discovered Python tests, CI policy, and repository policy | Produces durable integration evidence, not a production claim |
| `release` | Future release-candidate evidence | Every `full` gate plus fail-closed release qualification | Passes only after every declared release tuple is explicitly qualified and production-ready |

The current repository is intentionally `development-unqualified`, so `release` mode is expected to fail at `release-qualification`. That failure is evidence that fast or full validation cannot be relabeled as release certification.

For ordinary edit loops, run:

```bash
python3 tools/quality.py --mode fast --cache-policy off .
```

## Cache policies

The cache policy is explicit and never skips authoritative gates:

- `off` ignores cache state and executes every gate from source.
- `read-write` executes every gate, then writes `.validation/quality-cache.json` only after a complete pass.
- `read-only` requires a matching cache schema, quality version, mode, source digest, gate plan, and result digest; it still executes every gate and compares the fresh result digest with the cached digest.

A missing, malformed, wrong-version, wrong-mode, or stale read-only cache fails closed. Receipts and cache files are excluded from the source digest, so clean, repeated, and warm-cache executions evaluate the same authoritative tree.

## Machine-readable receipts

Default receipt paths are:

- `reports/quality/fast.json` for `fast`;
- `reports/quality/full.json` for `full`;
- `reports/quality/release.json` for `release`;
- `reports/quality/failure-demonstration.json` for intentional failure demonstrations.

A receipt records the command, mode, cache policy, source digest and file count, Git revision and dirty state when available, toolchain, ordered gate plan, normalized command output, per-gate diagnostics, and authoritative gate-result digest. Full and release receipts declare durable evidence. Receipt and cache directories are ignored by Git; CI uploads them as a 90-day artifact bound to the exact workflow revision.

A caller may select another receipt path with `--receipt`. An absolute path outside the repository is permitted for external evidence collectors.

## Required checks

Every pull request and protected integration into `main` must pass the exact GitHub Actions check `quality / full` from [`.github/workflows/baseline.yml`](../.github/workflows/baseline.yml). Repository branch protection or rulesets should require that exact check name and should not permit skipped, neutral, or cancelled runs to count as success.

The `quality / full` job performs, in order. Its full run includes the focused policy command `python3 tools/validate_implementation_policy.py --check --self-test --json .` as an authoritative gate:

1. a clean `full` run with cache policy `off`;
2. the intentional failure demonstration suite;
3. a fresh `read-write` full run;
4. a `read-only` full run that proves result equivalence;
5. an always-run upload of receipts and cache metadata.

A future release additionally requires a successful `release` run and the qualification evidence, security/resource evidence, corpus identity, packaging evidence, and approvals owned by the release roadmap. The required integration check alone never establishes a production capability claim.

## Intentional failure evidence

Run every major negative demonstration with:

```bash
python3 tools/quality.py --self-test-gates .
```

The suite makes isolated repository copies and proves rejection of generated-contract drift, invalid schema SQL, invalid positive examples, unregistered negative fixtures, orphan requirements, formatting and lint defects, broken documentation links, unsafe unisolated dependency admission, failing and empty test suites, false production claims, stale read-only caches, unpinned CI actions, missing required-check policy, and an unqualified release. The source tree is not mutated.

See [DEVELOPMENT.md](../DEVELOPMENT.md) for authority and build rules and [CONTRIBUTING.md](../CONTRIBUTING.md) for pull-request evidence requirements.
