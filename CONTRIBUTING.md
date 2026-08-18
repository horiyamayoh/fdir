# Contributing to FDIR

FDIR is developed against the frozen FDIR 2.1 normative baseline. Contributions are welcome, but implementation convenience must never weaken information independence, evidence fidelity, exhaustive accounting, explicit non-success states, or production-claim discipline.

## 1. Authority and scope

Before changing behavior, read:

- `README.md` for the product model and authority order;
- `baseline.yaml` for canonical and generated artifact classes;
- `machine/logical-model.yaml` and `tools/generate_contracts.py` for the logical-model authority;
- `machine/requirements.yaml`, `machine/acceptance-tests.yaml`, profiles, capabilities, and ADRs for additional normative obligations;
- issue [#1](https://github.com/horiyamayoh/fdir/issues/1) and roadmap [#4](https://github.com/horiyamayoh/fdir/issues/4) for product completion ownership.

Generated contracts, review documents, indexes, reports, and rendered artifacts are not independent sources of truth. Do not edit a generated contract to make a test pass; change its declared authority and regenerate it.

## 2. Issue-first development

Every non-trivial change starts from an issue. The issue must identify:

1. the parent umbrella, roadmap milestone, or owning issue;
2. a concrete goal;
3. in-scope responsibilities and explicit out-of-scope work;
4. dependencies and owned paths;
5. acceptance criteria that can be verified;
6. expected validation evidence;
7. any effect on normative authority, capability declarations, or production claims.

New normative requirements, unowned responsibilities, qualification gaps, and release blockers must be represented by issues and linked into roadmap #4 before final release work proceeds.

### Dependency and external-worker proposals

Use `.github/ISSUE_TEMPLATE/dependency.yml` for any parser, renderer, OCR engine, evaluator, codec, model, resource, native library, Python package, or Rust crate that may enter the product boundary. The proposal must link its implementation owner and Issue #33, name an exact version/build/features, declare evidence lanes, list every normalization and unavailable source distinction, record unsafe/FFI/native and untrusted-byte facts, select a process boundary, and define license/advisory, determinism, network, and resource evidence.

High-level parser output, rendered pixels, OCR tokens, or inference candidates may not be offered as the sole native evidence or independent census. Admission to `machine/dependency-catalog.yaml` is not a production capability claim. Policy-affecting contributions must run:

```bash
python3 tools/validate_implementation_policy.py --check --self-test --json .
```

### Actionable issue lifecycle

1. **Propose or select the owning issue.** Use the appropriate issue form, link its parent and related work, and define goal, bounded scope, owned paths, dependencies, acceptance criteria, planned evidence, claim impact, and explicit exclusions.
2. **Triage ownership and dependencies.** Confirm that the issue owns the responsibility it intends to change. Record shared seams, blockers, and any newly discovered normative, qualification, or release gap before implementation starts.
3. **Create a focused branch.** Start from the current integration branch after required prerequisites are present. Keep unrelated work and paths owned by other issues out of the branch.
4. **Implement in reviewable commits.** Each commit must be coherent, reference the owning issue, preserve authority boundaries, and keep incomplete or non-success states explicit.
5. **Produce validation evidence.** Run the exact required commands and relevant positive, negative, parity, security, resource, and qualification checks. Record versions, concise results, and unavailable checks; a check that did not run is not a pass.
6. **Submit and review the pull request.** Link the owning issue, map the diff to acceptance criteria and owned paths, state authority and production-claim impact, and link every intentionally deferred item to an owning follow-up issue.
7. **Integrate and close with evidence.** After required review and checks pass, integrate according to repository policy, then post the PR, merge revision, commands, and results to the issue. Close only when every acceptance criterion owned by the issue is evidenced; otherwise keep it open or link a completed replacement that owns the remaining responsibility.

The project-owner exception for a direct `main` commit does not waive issue ownership, bounded scope, validation evidence, or evidence-before-closure requirements.

## 3. Bounded completion

A leaf issue is complete when its declared responsibility is implemented and its own acceptance criteria are evidenced. A leaf issue does not inherit unlimited whole-product qualification work. Whole-product integration and qualification remain owned by their dedicated roadmap milestones.

The following states are never aliases for success:

- partial or incomplete;
- unsupported or not applicable;
- unresolved or indeterminate;
- unreadable or invalid input;
- policy-excluded or credential-blocked;
- resource-limited or cancelled;
- internal failure.

A placeholder, skipped test, empty result, or schema-valid demonstration must not be presented as production-qualified capability.

## 4. Branches, commits, and integration

The project owner currently permits small validated commits directly to `main`. Other contributors should normally use a focused branch and pull request unless the owner explicitly authorizes direct integration.

- Keep each commit internally coherent and small enough to review and revert.
- Reference the owning issue in commit or PR text.
- Do not mix unrelated work or silently rewrite another issue's owned paths.
- Preserve deterministic generated output and commit all required generated artifacts together with the authority change.
- Do not merge unless the exact required check `quality / full` passes for the pull-request head revision.
- Never force-push shared history without explicit project-owner approval and a recorded reason.

## 5. Validation evidence

The mandatory integration check is:

```bash
python3 tools/quality.py --mode full --cache-policy off .
```

For a bounded edit loop, `--mode fast` is permitted, but it does not replace the full command before review or integration and cannot certify a release. The GitHub Actions result named `quality / full` is the required pull-request and main-integration check.

Record the exact command, Python version, mode, cache policy, result, and receipt path in the issue or pull request. A check that did not run, discovered no tests, used a stale cache, or returned skipped/neutral/cancelled is not a pass. Full receipts are written under `reports/quality/` and archived by CI.

For generated contracts, validation includes byte-for-byte regeneration parity and schema-contract checks. For implementation changes, formatting, linting, all discovered unit/integration tests, relevant positive and negative cases, requirement/test traceability, documentation links, implementation/dependency policy conformance, and claim discipline are mandatory.

Use the intentional-failure suite when changing a quality gate or its policy:

```bash
python3 tools/quality.py --self-test-gates .
```

`release` mode is a separate fail-closed qualification boundary. It is expected to fail while any release tuple remains unqualified; a passing full check must never be presented as production qualification. See [`quality/README.md`](quality/README.md) for the mode, cache, receipt, and required-check contract.

## 6. Pull requests

A pull request must include:

- the owning issue;
- what changed and why;
- authority and production-claim impact;
- implementation boundary, dependency manifest, evidence-lane, normalization, and process-isolation impact;
- changed/owned paths;
- exact validation commands and results;
- incomplete, unsupported, intentionally deferred, or follow-up work;
- migration or compatibility impact when applicable.

Review focuses first on semantic authority, failure-state honesty, evidence/accounting closure, deterministic behavior, and security/resource boundaries—not only on a happy-path output.

## 7. Security-sensitive contributions

Do not disclose suspected vulnerabilities, exploit documents, credentials, or sensitive customer documents in public issues or pull requests. Follow `SECURITY.md`.

## 8. Contribution licensing

By intentionally submitting a contribution for inclusion in FDIR, you agree that it is provided under the Apache License, Version 2.0, as described in `LICENSE`, unless an explicit written agreement says otherwise.
