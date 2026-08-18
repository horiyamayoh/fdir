# Security Policy

FDIR treats documents, archives, parsers, renderers, OCR engines, adapters, and external references as untrusted. Security reports are handled separately from ordinary public issues.

## Supported versions

FDIR does not yet have a production-qualified release. During development:

| Version | Security support |
|---|---|
| Current `main` and active release-candidate branches | Supported for coordinated fixes |
| Historical development commits | Best effort only |
| Unqualified prototypes, forks, or modified builds | Not covered by an FDIR production claim |

After the first production release, this table will be replaced with an explicit supported-release window and requalification policy.

## Report a vulnerability privately

Use GitHub's private vulnerability reporting route for this repository:

https://github.com/horiyamayoh/fdir/security/advisories/new

Do **not** open a public issue, discussion, pull request, or fixture containing exploit details. If GitHub does not expose the private reporting form to your account, contact the repository owner through a private GitHub channel and reference this policy without publishing the vulnerability details.

Include, where safe:

- affected revision, release, platform, and configuration;
- affected format/capability/profile tuple;
- impact and realistic attacker prerequisites;
- minimal reproduction steps or a sanitized reproducer;
- whether the input contains confidential or personal data;
- observed logs, diagnostics, crash information, and resource behavior;
- suggested mitigation, if known;
- your preferred disclosure and credit details.

Do not send live credentials, unrelated personal data, or proprietary documents. Use the smallest sanitized evidence capable of demonstrating the problem.

## What happens next

Maintainers aim to acknowledge a complete report within five business days and provide an initial triage update within ten business days. Complex parser, sandbox, or supply-chain reports may require more investigation. These are targets rather than contractual service levels.

The project will:

1. preserve the report privately and limit access;
2. reproduce and classify the issue against the documented threat model and claim scope;
3. create a private remediation and regression-test plan;
4. determine affected versions, dependencies, capability tuples, and qualification evidence;
5. coordinate release, advisory, credit, and public disclosure timing with the reporter when practical;
6. requalify affected claims before describing them as fixed and production-ready.

A fix is not complete merely because a crash disappears. Required evidence may include malformed-input tests, accounting/status behavior, sandbox/resource checks, dependency updates, and release-claim changes.

## Disclosure expectations

Please allow a reasonable remediation period before public disclosure. Maintainers will not ask a reporter to hide an unresolved issue indefinitely. If active exploitation or broad ecosystem exposure changes the risk, both parties should coordinate an accelerated disclosure and mitigation plan.

## Security boundaries

The presence of this policy does not imply that every adapter, parser, platform, or capability is already production-qualified. The authoritative support and residual-risk boundary is the released claim manifest and its qualification reports. Unsupported, partial, policy-blocked, and indeterminate states must remain visible.
