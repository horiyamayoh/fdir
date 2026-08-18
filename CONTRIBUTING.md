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
- Never force-push shared history without explicit project-owner approval and a recorded reason.

## 5. Validation evidence

Until issue #6 installs the complete repository quality runner, the mandatory baseline check is:

```bash
python3 tools/validate_baseline.py .
```

Run every additional check relevant to the changed responsibility. Record exact commands, versions, result summaries, and any intentionally unavailable checks in the issue or pull request. A check that did not run is not a pass.

For generated contracts, validation must include byte-for-byte regeneration parity. For implementation changes, formatting, linting, unit tests, integration tests, negative cases, and applicable qualification fixtures become mandatory as the corresponding infrastructure lands.

## 6. Pull requests

A pull request must include:

- the owning issue;
- what changed and why;
- authority and production-claim impact;
- changed/owned paths;
- exact validation commands and results;
- incomplete, unsupported, intentionally deferred, or follow-up work;
- migration or compatibility impact when applicable.

Review focuses first on semantic authority, failure-state honesty, evidence/accounting closure, deterministic behavior, and security/resource boundaries—not only on a happy-path output.

## 7. Security-sensitive contributions

Do not disclose suspected vulnerabilities, exploit documents, credentials, or sensitive customer documents in public issues or pull requests. Follow `SECURITY.md`.

## 8. Contribution licensing

By intentionally submitting a contribution for inclusion in FDIR, you agree that it is provided under the Apache License, Version 2.0, as described in `LICENSE`, unless an explicit written agreement says otherwise.
