"""Fail-closed, reproducible integration gate for the Document Form IR design.

The gate deliberately has no third-party dependencies.  It runs the two
authoritative commands first, then checks the machine-readable release
inventory and the product boundary that those commands cannot fully express.
The only successful result is a zero exit status together with a JSON summary
whose command logs can be replayed from the repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable

try:
    from qualification_evidence import validate_source_feature_closure
except ImportError:  # pragma: no cover
    from tools.qualification_evidence import validate_source_feature_closure

try:
    from github_issue_state import (
        AUDIT_ISSUE_NUMBERS,
        IssueStateError,
        REPOSITORY as GITHUB_REPOSITORY,
        derive_release_boundary,
        evidence_close_time_blockers,
        load_snapshot,
        resolve_issue_state,
        validate_snapshot,
    )
except ImportError:  # pragma: no cover
    from tools.github_issue_state import (
        AUDIT_ISSUE_NUMBERS,
        IssueStateError,
        REPOSITORY as GITHUB_REPOSITORY,
        derive_release_boundary,
        evidence_close_time_blockers,
        load_snapshot,
        resolve_issue_state,
        validate_snapshot,
    )

try:
    from release_attestation import AttestationError, load_and_validate_attestation, validate_attestation
except ImportError:  # pragma: no cover
    from tools.release_attestation import AttestationError, load_and_validate_attestation, validate_attestation


ROOT = Path(__file__).resolve().parents[1]

REQUIREMENTS_PATH = ROOT / "machine" / "requirements.json"
ACCEPTANCE_PATH = ROOT / "machine" / "acceptance-tests.json"
ISSUE_PLAN_PATH = ROOT / "machine" / "issue-plan.json"
GITHUB_ISSUE_MAP_PATH = ROOT / "machine" / "github-issue-map.json"
PHASE2_ISSUE_PLAN_PATH = ROOT / "machine" / "phase2-issue-plan.json"
CAPABILITY_PROFILE_PATH = ROOT / "machine" / "capability-profile.json"
REFERENCE_REGISTRY_PATH = ROOT / "machine" / "reference-registry.json"
EXTENSION_REGISTRY_PATH = ROOT / "machine" / "extension-registry.json"
CANONICALIZATION_PATH = ROOT / "machine" / "canonicalization.json"
QUERY_CONTRACT_PATH = ROOT / "machine" / "query-contract.json"
RELEASE_CLAIM_MANIFEST_PATH = ROOT / "machine" / "release-claim-manifest.json"
AUDIT_RECOVERY_PATH = ROOT / "machine" / "audit-recovery-plan.json"
QUALIFICATION_CONTRACT_PATH = ROOT / "machine" / "qualification-contract.json"
INDEPENDENT_CORPUS_MANIFEST_PATH = ROOT / "e2e" / "corpus" / "manifest.json"
STRICT_COMPLETION_CONTRACT_PATH = ROOT / "machine" / "strict-completion-contract.json"
TRACEABILITY_PATH = ROOT / "machine" / "traceability.json"
SCHEMA_PATH = ROOT / "schemas" / "document-form-ir.schema.json"
EXAMPLES_PATH = ROOT / "examples"
CLEAN_ROOM_REPLAY_PATH = ROOT / "e2e" / ".run" / "clean-room-replay.json"
AUDIT_RECOVERY_ISSUES = tuple(range(87, 106)) + tuple(range(108, 114))
LIVE_ISSUES = AUDIT_RECOVERY_ISSUES
QUALIFICATION_ISSUES = tuple(range(88, 106))
BARRIER_ISSUES = tuple(range(108, 114))
UMBRELLA_ISSUE = 87
FINAL_QUALIFICATION_ISSUE = 105
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIRECT_QUERY_INDEX_SCHEMA = "fdir/document-form-index"
DIRECT_QUERY_INDEX_VERSION = "1.3.0"
INDEPENDENT_QUERY_INDEX_SCHEMA = "fdir/independent-sqlite-index"
INDEPENDENT_QUERY_INDEX_VERSION = "1.1.0"

# These are the assertions emitted by the generic bundle builder.  They are
# useful integrity checks, but they do not prove that a requirement was
# exercised.  A release bundle must contain at least one assertion and one
# test case from the issue-specific qualification runner as well.
GENERIC_BUNDLE_ASSERTIONS = {
    "qualification-command-exits-zero",
    "declared-output-files-bound",
    "source-sha-is-current-head",
}
GENERIC_BUNDLE_ORACLES = {
    "the declared qualification command exits with code zero and all declared outputs are bound",
    "the evidence report is generated from and bound to the current commit SHA",
}
PLACEHOLDER_RE = re.compile(
    r"(?:<[^>\r\n]+>|\b(?:todo|tbd|fixme|placeholder|replace[-_ ]?me|not[-_ ]?implemented|unresolved)\b)",
    re.IGNORECASE,
)

QUALIFICATION_COMMANDS = {
    "python tools/validate_design.py",
    "python tools/run_acceptance.py --all",
    "python tools/run_e2e.py --all",
    "python tools/run_e2e.py --all --json",
    "python tools/mutation_qualification.py --json",
    "python tools/query_qualification.py",
    "python tools/independent_corpus.py --json",
    "python tools/strict_completion_gate.py",
    "python tools/release_gate.py",
}

EXPECTED_REQUIREMENTS = 134
EXPECTED_FAMILIES = 16
EXPECTED_CASES = 134
EXPECTED_LEAF_ISSUES = 20
EXPECTED_UMBRELLA_ISSUE = 47
EXPECTED_LEAF_ISSUE_RANGE = range(48, 68)
EXPECTED_E2E_ISSUE = 68

REQUIRED_EXAMPLES = {
    "callout.json",
    "cell-formula.json",
    "markdown-authoring.json",
    "partial-conversion.json",
    "pdf-observation.json",
    "style-resolution.json",
}

NORMATIVE_DOCUMENTS = (
    "README.md",
    "docs/01-product-definition.md",
    "docs/02-architecture.md",
    "docs/03-logical-model.md",
    "docs/04-format-mapping.md",
    "docs/05-serialization-and-extensions.md",
    "docs/06-interfaces-and-implementation.md",
    "docs/07-verification-and-issues.md",
    "docs/08-review-and-reset.md",
)

REQUIRED_SCHEMA_FIELDS = {
    "schema",
    "documentId",
    "sourceFormat",
    "rootNodeId",
    "nodes",
    "conversion",
}

# These are product-model concepts, not words that are forbidden in a
# boundary document.  The key scan therefore applies to the schema and JSON
# examples, while the documentation scan below requires explicit boundary
# language instead of rejecting the documented non-goals themselves.
FORBIDDEN_PRODUCT_KEY_WORDS = (
    "semantic",
    "predicate",
    "assertion",
    "equivalence",
    "rawbyte",
    "sourcebyte",
    "sourcestore",
    "sourcearchive",
    "contentaddressedsource",
    "evidencestore",
    "forensic",
    "accounting",
    "census",
    "lineage",
)

FORBIDDEN_PRODUCT_VALUE_PATTERNS = (
    re.compile(r"\bsemantic\s+(?:ir|equivalence|predicate|assertion|interpretation)\b", re.I),
    re.compile(r"\b(?:raw|source)[ -]?bytes?\b", re.I),
    re.compile(r"\b(?:content[- ]addressed\s+)?source\s+(?:store|archive)\b", re.I),
    re.compile(r"\bforensic(?:\s+(?:evidence|archive|accounting))?\b", re.I),
    re.compile(r"\b(?:accounting\s+(?:closure|item|census)|byte\s+census)\b", re.I),
    re.compile(r"\b(?:lineage\s+certificate|cross[- ]revision\s+lineage)\b", re.I),
    re.compile(r"\bpredicate\b", re.I),
)


class GateError(Exception):
    """A deterministic, user-facing release-gate failure."""

    def __init__(self, message: str, code: str = "GATE_ERROR"):
        self.code = code
        self.detail = message
        super().__init__(message)


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def require(condition: bool, message: str, code: str = "GATE_ERROR") -> None:
    if not condition:
        raise GateError(message, code)


def git_output(*arguments: str) -> str:
    """Return a Git value or fail closed with a stable diagnostic."""

    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise GateError(f"cannot execute git {' '.join(arguments)}: {exc}") from exc
    require(result.returncode == 0, f"git {' '.join(arguments)} failed: {(result.stdout + result.stderr).strip()}")
    return result.stdout.strip()


def current_head() -> str:
    head = git_output("rev-parse", "HEAD")
    require(SOURCE_SHA_RE.fullmatch(head) is not None, f"git HEAD is not a 40-character lowercase SHA: {head!r}")
    return head


def working_tree_status() -> list[str]:
    output = git_output("status", "--porcelain=v1", "--untracked-files=all")
    return [line for line in output.splitlines() if line.strip()]


def check_clean_tree() -> dict[str, int | bool]:
    """Reject release qualification from any non-clean checkout.

    Generated products live under the ignored ``e2e/.run`` directory and are
    therefore intentionally absent from this status.  Tracked edits and
    untracked files elsewhere are evidence drift, not a harmless local detail.
    """

    status = working_tree_status()
    require(not status, "release qualification requires a clean working tree: " + "; ".join(status[:8]))
    return {"dirty_tree": False, "status_entries": 0}


def check_ci_binding(*, require_actions: bool = False) -> dict[str, str | bool]:
    """Bind an Actions run to the exact checkout that the gate inspected.

    Local metadata is useful for development diagnostics, but it is never a
    release authority.  Final release callers must opt into the stricter
    GitHub Actions-only branch.
    """

    head = current_head()
    actions = os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
    if not actions:
        require(not require_actions, "final release qualification requires GitHub Actions provenance", "CI_PROVIDER_REQUIRED")
        return {"provider": "local", "source_sha": head, "actions": False}

    declared_sha = os.environ.get("GITHUB_SHA", "")
    require(SOURCE_SHA_RE.fullmatch(declared_sha) is not None, "GITHUB_SHA is not a 40-character lowercase SHA")
    require(declared_sha == head, f"GitHub Actions SHA {declared_sha} does not match checkout HEAD {head}")
    require(os.environ.get("GITHUB_REPOSITORY") == "horiyamayoh/fdir", "GitHub Actions repository is not horiyamayoh/fdir")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
        require(os.environ.get(name), f"GitHub Actions identity is missing {name}")
    return {"provider": "github-actions", "source_sha": head, "actions": True}


def contains_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PLACEHOLDER_RE.search(value) is not None


def require_no_placeholder(value: Any, label: str) -> None:
    require(not contains_placeholder(value), f"placeholder value is not release evidence: {label}")


def safe_repository_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    parts = PurePosixPath(value).parts
    return bool(parts) and "." not in parts and ".." not in parts


def validate_mutation_report(report: dict[str, Any]) -> dict[str, int]:
    """Require a non-empty, case-level mutation result with zero survivors."""

    require(report.get("schema") == "fdir/mutation-qualification-report", "mutation report schema is invalid")
    require(report.get("version") == "1.0.0", "mutation report version is invalid")
    cases = report.get("cases")
    require(isinstance(cases, list) and cases, "mutation report has no executable cases")
    total = report.get("total")
    killed = report.get("killed")
    survivors = report.get("survivors")
    require(isinstance(total, int) and not isinstance(total, bool) and total > 0, "mutation report total must be a positive integer")
    require(total == len(cases), "mutation report total does not match its case inventory")
    require(isinstance(killed, int) and not isinstance(killed, bool), "mutation report killed count is invalid")
    require(isinstance(survivors, list) and survivors == [], "mutation report contains surviving mutations")
    killed_cases = 0
    for case in cases:
        require(isinstance(case, dict), "mutation report contains a malformed case")
        require(isinstance(case.get("mutation"), str) and case.get("mutation"), "mutation case has no stable id")
        require(isinstance(case.get("class"), str) and case.get("class"), "mutation case has no class")
        require(case.get("status") in {"killed"}, f"mutation case did not get killed: {case.get('mutation')}")
        killed_cases += 1
    require(killed == killed_cases == total, "mutation report killed/total counts are inconsistent")
    require(report.get("mutationScore") == 1.0, "mutation report score is not exactly 1.0")
    coverage = report.get("coverage")
    require(isinstance(coverage, dict) and coverage, "mutation report has no mutation-class coverage")
    return {"mutation_cases": total}


def fetch_live_audit_issue_state() -> dict[str, Any]:
    """Compatibility wrapper returning a verified live snapshot.

    Older callers used this name directly.  Keep it as a thin adapter so no
    caller can accidentally regain the old static-JSON authority.
    """

    try:
        source_sha = current_head()
        resolved = resolve_issue_state(source_sha=source_sha)
        snapshot = resolved["snapshot"]
        return {"repository": snapshot["repository"], "issues": snapshot["issues"], "snapshot": snapshot, "authority": resolved["authority"]}
    except IssueStateError as exc:
        raise GateError(exc.detail, exc.code) from exc


def check_source_closure_report(report: dict[str, Any], label: str) -> None:
    """Recompute source closure from each report's emitted IR document."""

    cases = list(report.get("cases", []))
    cases.extend(item for item in report.get("negativeChecks", []) if isinstance(item, dict))
    require(cases, f"{label} has no source closure cases")
    for case in cases:
        require(isinstance(case, dict), f"{label} contains a malformed source closure case")
        document_path = case.get("documentPath") or case.get("output")
        require(isinstance(document_path, str) and Path(document_path).is_file(), f"{label} case has no emitted IR document path")
        document = load_json(Path(document_path))
        closure = validate_source_feature_closure(document, case)
        require(closure.get("status") == "passed" and closure.get("mismatches") == [], f"{label} source closure failed: {json.dumps(closure.get('mismatches', []), ensure_ascii=False)}")
        reported = case.get("sourceClosure")
        require(isinstance(reported, dict) and reported.get("status") == "passed" and reported.get("mismatches") == [], f"{label} did not report a passed source closure")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateError(f"missing JSON artifact: {relative(path)}") from exc
    except UnicodeDecodeError as exc:
        raise GateError(f"invalid UTF-8 in {relative(path)}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid JSON in {relative(path)}: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc


def compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def find_product_boundary_leaks(value: Any, label: str, *, scan_values: bool) -> list[str]:
    leaks: list[str] = []

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                compact = compact_key(key_text)
                if any(word in compact for word in FORBIDDEN_PRODUCT_KEY_WORDS):
                    leaks.append(f"{label}{''.join(f'[{part!r}]' for part in path)}[{key_text!r}]")
                visit(child, path + (key_text,))
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, path + (str(index),))
            return
        if scan_values and isinstance(node, str):
            if any(pattern.search(node) for pattern in FORBIDDEN_PRODUCT_VALUE_PATTERNS):
                leaks.append(f"{label}{''.join(f'[{part!r}]' for part in path)}")

    visit(value, ())
    return leaks


def check_design_catalog() -> dict[str, int]:
    requirements = load_json(REQUIREMENTS_PATH)
    acceptance = load_json(ACCEPTANCE_PATH)
    require(isinstance(requirements, dict), "requirements root must be an object")
    require(isinstance(acceptance, dict), "acceptance-tests root must be an object")

    requirement_entries = requirements.get("requirements")
    family_entries = acceptance.get("families")
    require(isinstance(requirement_entries, list), "requirements must be an array")
    require(isinstance(family_entries, list), "acceptance families must be an array")
    require(len(requirement_entries) == EXPECTED_REQUIREMENTS, f"expected {EXPECTED_REQUIREMENTS} requirements, got {len(requirement_entries)}")
    require(len(family_entries) == EXPECTED_FAMILIES, f"expected {EXPECTED_FAMILIES} acceptance families, got {len(family_entries)}")

    requirement_ids: set[str] = set()
    for requirement in requirement_entries:
        require(isinstance(requirement, dict), "requirement entry is not an object")
        requirement_id = requirement.get("id")
        require(isinstance(requirement_id, str) and requirement_id, "requirement has no id")
        require(requirement_id not in requirement_ids, f"duplicate requirement id: {requirement_id}")
        requirement_ids.add(requirement_id)

    expected_cases: list[str] = []
    family_ids: set[str] = set()
    for family in family_entries:
        require(isinstance(family, dict), "acceptance family entry is not an object")
        family_id = family.get("id")
        count = family.get("count")
        prefix = family.get("requirementPrefix")
        require(isinstance(family_id, str) and family_id, "acceptance family has no id")
        require(family_id not in family_ids, f"duplicate acceptance family id: {family_id}")
        family_ids.add(family_id)
        require(isinstance(count, int) and count > 0, f"invalid acceptance count: {family_id}")
        require(isinstance(prefix, str) and prefix, f"acceptance family has no requirement prefix: {family_id}")
        matching = [entry for entry in requirement_entries if isinstance(entry, dict) and str(entry.get("id", "")).startswith(prefix)]
        require(len(matching) == count, f"acceptance family count mismatch: {family_id} expected {count} got {len(matching)}")
        expected_cases.extend(f"{family_id}-{number:03d}" for number in range(1, count + 1))

    require(len(expected_cases) == EXPECTED_CASES, f"expected {EXPECTED_CASES} acceptance cases, got {len(expected_cases)}")
    require(len(set(expected_cases)) == EXPECTED_CASES, "acceptance case IDs are not unique")

    actual_cases: list[str] = []
    for requirement in requirement_entries:
        tests = requirement.get("acceptanceTests") if isinstance(requirement, dict) else None
        requirement_id = requirement.get("id") if isinstance(requirement, dict) else "<unknown>"
        require(isinstance(tests, list) and len(tests) == 1, f"requirement must map to exactly one acceptance case: {requirement_id}")
        case_id = tests[0]
        require(isinstance(case_id, str) and case_id, f"requirement has invalid acceptance case: {requirement_id}")
        actual_cases.append(case_id)

    require(len(actual_cases) == EXPECTED_CASES, f"expected {EXPECTED_CASES} mapped acceptance cases, got {len(actual_cases)}")
    require(len(set(actual_cases)) == EXPECTED_CASES, "acceptance cases are mapped more than once")
    missing = sorted(set(expected_cases) - set(actual_cases))
    extra = sorted(set(actual_cases) - set(expected_cases))
    require(not missing and not extra, f"acceptance catalog mismatch: missing={missing} extra={extra}")

    return {
        "requirements": len(requirement_entries),
        "acceptance_families": len(family_entries),
        "acceptance_cases": len(expected_cases),
    }


def check_issue_plan() -> dict[str, int]:
    issue_plan = load_json(ISSUE_PLAN_PATH)
    github_map = load_json(GITHUB_ISSUE_MAP_PATH)
    require(isinstance(issue_plan, dict), "issue-plan root must be an object")
    require(isinstance(github_map, dict), "github issue map root must be an object")

    entries = issue_plan.get("issues")
    require(isinstance(entries, list), "issue plan issues must be an array")
    issue_by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        require(isinstance(entry, dict), "issue plan entry is not an object")
        issue_id = entry.get("id")
        require(isinstance(issue_id, str) and issue_id, "issue plan entry has no id")
        require(issue_id not in issue_by_id, f"duplicate issue-plan id: {issue_id}")
        issue_by_id[issue_id] = entry

    umbrella = issue_by_id.get("DFIR-I-000")
    require(umbrella is not None and umbrella.get("kind") == "umbrella", "issue plan umbrella DFIR-I-000 is missing")
    expected_leaf_ids = {f"DFIR-I-{number:03d}" for number in range(1, EXPECTED_LEAF_ISSUES + 1)}
    actual_leaf_ids = set(issue_by_id) - {"DFIR-I-000"}
    require(actual_leaf_ids == expected_leaf_ids, "issue plan leaf IDs are not exactly DFIR-I-001 through DFIR-I-020")
    require(len(actual_leaf_ids) == EXPECTED_LEAF_ISSUES, f"expected {EXPECTED_LEAF_ISSUES} leaf issues, got {len(actual_leaf_ids)}")

    require(github_map.get("repository") == "horiyamayoh/fdir", "github issue map points to the wrong repository")
    mapped_umbrella = github_map.get("umbrella")
    require(isinstance(mapped_umbrella, dict), "github issue map umbrella is missing")
    require(mapped_umbrella.get("key") == "DFIR-I-000", "github issue map umbrella key is wrong")
    require(mapped_umbrella.get("issueNumber") == EXPECTED_UMBRELLA_ISSUE, "github issue map umbrella must be issue #47")

    mapped_leaves = github_map.get("issues")
    require(isinstance(mapped_leaves, list), "github issue map issues must be an array")
    mapped_by_key: dict[str, dict[str, Any]] = {}
    for entry in mapped_leaves:
        require(isinstance(entry, dict), "github issue map entry is not an object")
        key = entry.get("key")
        number = entry.get("issueNumber")
        require(isinstance(key, str) and key, "github issue map entry has no key")
        require(key not in mapped_by_key, f"duplicate github issue map key: {key}")
        mapped_by_key[key] = entry
        require(isinstance(number, int), f"github issue map has invalid number: {key}")

    require(set(mapped_by_key) == expected_leaf_ids, "github issue map does not cover exactly the 20 leaf issues")
    expected_numbers = set(EXPECTED_LEAF_ISSUE_RANGE)
    actual_numbers = {entry["issueNumber"] for entry in mapped_by_key.values()}
    require(actual_numbers == expected_numbers, "github issue map does not cover issues #48 through #67")
    for number in EXPECTED_LEAF_ISSUE_RANGE:
        key = f"DFIR-I-{number - 47:03d}"
        require(mapped_by_key[key]["issueNumber"] == number, f"github issue mapping is not contiguous at #{number}")
    blocker = github_map.get("releaseBlocker")
    require(isinstance(blocker, dict), "github issue map has no E2E release blocker")
    require(blocker.get("issueNumber") == EXPECTED_E2E_ISSUE, "github issue map E2E blocker must be issue #68")
    require(blocker.get("key") == "DFIR-E2E-001", "github issue map E2E blocker key is wrong")

    return {
        "issue_plan_entries": len(entries),
        "leaf_issues": len(actual_leaf_ids),
        "umbrella_issue": EXPECTED_UMBRELLA_ISSUE,
        "first_leaf_issue": min(expected_numbers),
        "last_leaf_issue": max(expected_numbers),
        "e2e_issue": EXPECTED_E2E_ISSUE,
    }


def check_traceability() -> dict[str, int]:
    traceability = load_json(TRACEABILITY_PATH)
    require(isinstance(traceability, dict), "traceability root must be an object")
    authoritative = traceability.get("authoritativeInputs")
    require(isinstance(authoritative, list), "traceability authoritativeInputs must be an array")
    required_inputs = {
        "machine/requirements.json",
        "machine/acceptance-tests.json",
        "machine/issue-plan.json",
        "machine/github-issue-map.json",
        "schemas/document-form-ir.schema.json",
    }
    require(required_inputs.issubset(set(authoritative)), "traceability omits a release authority")
    derivation = traceability.get("derivation")
    require(isinstance(derivation, dict), "traceability derivation is missing")
    for key in ("requirementToIssue", "requirementToAcceptance", "acceptanceFamilyExpansion"):
        require(isinstance(derivation.get(key), str) and derivation[key], f"traceability derivation is missing: {key}")
    return {"authoritative_inputs": len(authoritative), "derivation_rules": len(derivation)}


def check_phase2_contracts() -> dict[str, int]:
    plan = load_json(PHASE2_ISSUE_PLAN_PATH)
    require(isinstance(plan, dict), "phase2 issue plan root must be an object")
    policy = plan.get("policy")
    entries = plan.get("issues")
    require(isinstance(policy, dict) and isinstance(entries, list), "phase2 issue plan is incomplete")
    numbers = [entry.get("issueNumber") for entry in entries if isinstance(entry, dict)]
    expected_numbers = list(range(69, 85)) + [86]
    require(numbers == expected_numbers, "phase2 issue plan must cover issues #69 through #84 and #86 in order")
    require(policy.get("activeIssueNumbers") == expected_numbers, "phase2 active issue numbers are incomplete")
    required_commands = policy.get("requiredCommands")
    require(isinstance(required_commands, list) and "python tools/mutation_qualification.py --json" in required_commands and "python tools/query_qualification.py" in required_commands and "python tools/independent_corpus.py --json" in required_commands and "python tools/strict_completion_gate.py" in required_commands and "python tools/release_gate.py" in required_commands, "phase2 qualification commands are incomplete")

    capability = load_json(CAPABILITY_PROFILE_PATH)
    profiles = capability.get("profiles") if isinstance(capability, dict) else None
    require(isinstance(profiles, list) and {item.get("format") for item in profiles if isinstance(item, dict)} == {"docx", "xlsx", "pdf", "markdown"}, "capability profiles do not cover all formats")
    for profile in profiles:
        require(isinstance(profile, dict) and isinstance(profile.get("id"), str) and isinstance(profile.get("supportedFeatures"), list), "capability profile is malformed")
        require(profile.get("disposition") == "preserve-or-diagnose" and profile.get("unknownConstructPolicy") == "partial-with-diagnostic", "capability profile policy is not fail-closed")
    status_contract = capability.get("statusContract") if isinstance(capability, dict) else None
    require(isinstance(status_contract, dict) and status_contract.get("normalizedIsComplete") is True and "unavailable" in status_contract.get("observationOnlyStatuses", []), "capability status contract is incomplete")

    reference = load_json(REFERENCE_REGISTRY_PATH)
    references = reference.get("references") if isinstance(reference, dict) else None
    require(isinstance(references, list) and references, "reference registry is empty")
    owners = [item.get("owner") for item in references if isinstance(item, dict)]
    require(len(owners) == len(set(owners)), "reference registry contains duplicate owners")

    canonicalization = load_json(CANONICALIZATION_PATH)
    require(isinstance(canonicalization.get("entityCollections"), dict), "canonicalization registry lacks entity collections")
    require(canonicalization.get("defaultDigestProjection") == "source-map-excluded", "canonicalization default projection is wrong")
    require({item.get("name") for item in canonicalization.get("projections", []) if isinstance(item, dict)} >= {"full", "content", "source-map-excluded"}, "canonicalization projections are incomplete")

    query_contract = load_json(QUERY_CONTRACT_PATH)
    operations = query_contract.get("operations") if isinstance(query_contract, dict) else None
    require(isinstance(operations, list) and {"list-entities", "get-entity", "rebuild-index", "get-text"}.issubset(set(operations)), "typed query contract is incomplete")
    index_contract = query_contract.get("index") if isinstance(query_contract, dict) else None
    require(isinstance(index_contract, dict), "query index contract is missing", "QUERY_CONTRACT_INDEX_MISSING")
    require(index_contract.get("schema") == INDEPENDENT_QUERY_INDEX_SCHEMA, "query contract independent index schema is stale or missing", "QUERY_CONTRACT_INDEX_SCHEMA")
    require(index_contract.get("version") == INDEPENDENT_QUERY_INDEX_VERSION, "query contract independent index version is stale", "QUERY_CONTRACT_INDEX_VERSION")
    require(index_contract.get("authority") == "non-authoritative deterministic projection", "query contract independent index authority is not explicit", "QUERY_CONTRACT_INDEX_AUTHORITY")
    required_index_fields = index_contract.get("requiredFields")
    require(isinstance(required_index_fields, list) and {"schema", "source", "bindings", "integrity", "databaseSha256"}.issubset(set(required_index_fields)), "query contract independent index fields are incomplete", "QUERY_CONTRACT_INDEX_FIELDS")

    # There are deliberately two query layers.  ``query_ir`` is the direct,
    # in-memory representation used for authoritative query semantics;
    # ``independent_index`` is the rebuildable SQLite projection declared by
    # machine/query-contract.json.  Do not compare their schema names or one
    # layer will silently invalidate the other.
    try:
        from query_ir import INDEX_SCHEMA as direct_schema, INDEX_VERSION as direct_version
        from independent_index import INDEX_SCHEMA as independent_schema, INDEX_VERSION as independent_version
    except ImportError:  # pragma: no cover
        from tools.query_ir import INDEX_SCHEMA as direct_schema, INDEX_VERSION as direct_version
        from tools.independent_index import INDEX_SCHEMA as independent_schema, INDEX_VERSION as independent_version
    require(direct_schema == DIRECT_QUERY_INDEX_SCHEMA and direct_version == DIRECT_QUERY_INDEX_VERSION, "direct query layer schema/version is stale", "QUERY_DIRECT_LAYER_SCHEMA")
    require(independent_schema == INDEPENDENT_QUERY_INDEX_SCHEMA and independent_version == INDEPENDENT_QUERY_INDEX_VERSION, "independent SQLite query layer schema/version is stale", "QUERY_INDEPENDENT_LAYER_SCHEMA")

    try:
        from extension_registry import validate_registry_integrity
    except ImportError:  # pragma: no cover
        from tools.extension_registry import validate_registry_integrity
    extension_details = validate_registry_integrity()
    return {"phase2_issues": len(numbers), "capability_profiles": len(profiles), "references": len(references), "extension_entries": extension_details["entries"], "canonical_entity_collections": len(canonicalization["entityCollections"]), "query_operations": len(operations), "query_layers": 2}


def check_release_claims() -> dict[str, int]:
    manifest = load_json(RELEASE_CLAIM_MANIFEST_PATH)
    require(manifest.get("schema") == "fdir/document-form-release-claim-manifest", "release claim manifest schema is missing")
    release = manifest.get("release")
    require(isinstance(release, dict) and release.get("policy") == "fail-closed", "release claim policy is not fail-closed")
    require(release.get("releaseBlocked") in {True, False}, "release claim has no boolean releaseBlocked state")
    require(release.get("status") in {"release-blocked", "release-ready"}, "release claim has an invalid release status")
    claim_policy = release.get("claimPolicy")
    require(isinstance(claim_policy, dict), "release claim policy details are missing")
    for policy in (
        "implementedSurfaceIsNotQualifiedEvidence",
        "closedStateIsNotEvidence",
        "fileExistenceIsNotEvidence",
        "fieldOrEnumPresenceIsNotEvidence",
        "commandExitOnlyIsNotEvidence",
        "releaseClaimsRequireAllRecoveryChildren",
    ):
        require(claim_policy.get(policy) is True, f"release claim policy is weak: {policy}")
    binding = release.get("qualificationBinding")
    require(isinstance(binding, dict), "release claim qualification binding is missing")
    if release.get("releaseBlocked") is True:
        require(binding.get("status") in {"blocked", "pending", "not-qualified"},
                "blocked release cannot publish a passed qualification binding")
    else:
        require(binding.get("status") == "passed", "release-ready claim has no passed qualification binding")
        require(binding.get("manifestPath") == "qualification/<source-sha>/manifest.json",
                "release-ready claim qualification path is not source-SHA templated")
        require(binding.get("sourceShaPolicy") == "exact-bundle-manifest",
                "release-ready claim qualification SHA policy is not exact-bundle-manifest")
    claims = manifest.get("issueClaims")
    plan = load_json(PHASE2_ISSUE_PLAN_PATH)
    plan_numbers = {entry.get("issueNumber") for entry in plan.get("issues", []) if isinstance(entry, dict)}
    require(isinstance(claims, list), "release claim issue claims are missing")
    claim_numbers = {claim.get("issueNumber") for claim in claims if isinstance(claim, dict)}
    require(claim_numbers == plan_numbers - {69}, "release claims do not cover every phase2 child issue")
    for claim in claims:
        require(isinstance(claim, dict) and isinstance(claim.get("claim"), str) and claim.get("claim"), "release claim is malformed")
        evidence_commands = claim.get("evidenceCommands")
        evidence_paths = claim.get("evidencePaths")
        require(isinstance(evidence_commands, list) and evidence_commands, f"release claim has no evidence commands: #{claim.get('issueNumber')}")
        require(isinstance(evidence_paths, list) and evidence_paths, f"release claim has no evidence paths: #{claim.get('issueNumber')}")
        for command in evidence_commands:
            require(isinstance(command, str) and command in QUALIFICATION_COMMANDS,
                    f"release claim uses an unapproved or command-exit-only command: {command!r}")
            require_no_placeholder(command, f"issue claim command #{claim.get('issueNumber')}")
        for evidence_path in evidence_paths:
            require(safe_repository_path(evidence_path), f"release claim evidence path is unsafe: {evidence_path!r}")
            require_no_placeholder(evidence_path, f"issue claim path #{claim.get('issueNumber')}")
            require((ROOT / evidence_path).is_file(), f"release claim evidence path is missing: {evidence_path}")
    capability_claims = manifest.get("capabilityClaims")
    capability = load_json(CAPABILITY_PROFILE_PATH)
    profile_ids = {profile.get("id") for profile in capability.get("profiles", []) if isinstance(profile, dict)}
    require(isinstance(capability_claims, list) and {item.get("profileId") for item in capability_claims if isinstance(item, dict)} == profile_ids, "capability claims do not cover every profile")
    corpus = load_json(INDEPENDENT_CORPUS_MANIFEST_PATH)
    require(corpus.get("independent") is True and len(corpus.get("cases", [])) >= 4, "independent corpus is incomplete")
    required_formats = {"docx", "xlsx", "pdf", "markdown"}
    require({case.get("format") for case in corpus.get("cases", []) if isinstance(case, dict)} == required_formats, "independent corpus format matrix is incomplete")
    all_corpus_cases = [case for case in corpus.get("cases", []) + corpus.get("negativeCases", []) if isinstance(case, dict)]
    require({case.get("caseClass") for case in all_corpus_cases} >= {"positive", "malformed", "unsupported"}, "independent corpus lacks a positive/malformed/unsupported matrix")
    require({case.get("format") for case in all_corpus_cases} == required_formats, "independent negative corpus format matrix is incomplete")
    require(isinstance(corpus.get("negativeChecks"), list) and any(item.get("id") == "resource-limit" for item in corpus["negativeChecks"] if isinstance(item, dict)), "independent resource-limit evidence is missing")
    independent_evidence = manifest.get("independentEvidence")
    require(isinstance(independent_evidence, dict) and independent_evidence.get("runner") == "tools/independent_corpus.py", "independent corpus runner is not claimed")
    require(independent_evidence.get("releaseEligible") is False, "independent evidence must not be release-eligible without the bundle")
    require(set(independent_evidence.get("requiredRecoveryIssues", [])) == set(QUALIFICATION_ISSUES) - {89},
            "independent evidence recovery scope is incomplete")
    strict_contract = load_json(STRICT_COMPLETION_CONTRACT_PATH)
    require(strict_contract.get("schema") == "fdir/document-form-strict-completion-contract", "strict completion contract is missing")
    require(strict_contract.get("closurePolicy", {}).get("closedStateIsNotEvidence") is True and strict_contract.get("closurePolicy", {}).get("fileExistenceIsNotEvidence") is True, "strict completion closure policy is weak")
    strict_claim = manifest.get("strictCompletionContract")
    require(isinstance(strict_claim, dict) and strict_claim.get("path") == "machine/strict-completion-contract.json" and strict_claim.get("gate") == "tools/strict_completion_gate.py" and strict_claim.get("requiredReportStatus") == "passed", "release claim does not bind the strict completion gate")
    strict_issue_evidence = strict_contract.get("issueEvidence", {})
    require(set(strict_issue_evidence) == {str(number) for number in strict_contract.get("scope", {}).get("phase2Issues", [])}, "strict issue evidence does not cover the declared phase2 scope")
    return {"child_claims": len(claims), "capability_claims": len(capability_claims), "independent_positive_cases": len(corpus["cases"]), "independent_negative_cases": len(corpus.get("negativeCases", [])), "strict_issue_bindings": len(strict_issue_evidence)}


def check_recovery_scope_contract(contract: Any) -> dict[str, Any]:
    """Validate the split live/report scope and its non-duplicating barriers."""

    require(isinstance(contract, dict), "qualification contract is not an object", "QUALIFICATION_SCOPE")
    require(contract.get("targetIssueNumbers") == list(LIVE_ISSUES), "qualification target scope is not #87-#105 and #108-#113", "QUALIFICATION_SCOPE")
    require(contract.get("recoveryChildIssueNumbers") == list(QUALIFICATION_ISSUES), "qualification child scope is not #88-#105", "QUALIFICATION_SCOPE")
    require(contract.get("barrierIssueNumbers") == [UMBRELLA_ISSUE, *BARRIER_ISSUES], "qualification barrier issue scope is invalid", "QUALIFICATION_SCOPE")
    ci_policy = contract.get("ciPolicy")
    require(isinstance(ci_policy, dict) and ci_policy.get("allowedProviders") == ["github-actions"], "qualification contract permits a non-GitHub provider", "CI_PROVIDER_REQUIRED")
    coverage = contract.get("barrierCoverage")
    require(isinstance(coverage, dict), "qualification barrierCoverage is missing", "BARRIER_COVERAGE_SCOPE")
    expected_coverage = {
        "issue-88-qualification-contract": list(BARRIER_ISSUES),
        "issue-105-release-quality": [UMBRELLA_ISSUE, *BARRIER_ISSUES],
    }
    require(set(coverage) == set(expected_coverage), "barrierCoverage must be owned only by #88 and #105", "BARRIER_COVERAGE_SCOPE")
    for evidence_id, issue_numbers in expected_coverage.items():
        record = coverage.get(evidence_id)
        require(isinstance(record, dict), f"barrierCoverage record is missing: {evidence_id}", "BARRIER_COVERAGE_SCOPE")
        require(record.get("issueNumbers") == issue_numbers, f"barrierCoverage is invalid for {evidence_id}", "BARRIER_COVERAGE_SCOPE")
        require(isinstance(record.get("role"), str) and record.get("role"), f"barrierCoverage role is missing: {evidence_id}", "BARRIER_COVERAGE_SCOPE")
    return {"liveIssues": len(LIVE_ISSUES), "qualificationIssues": len(QUALIFICATION_ISSUES), "barrierIssues": len(BARRIER_ISSUES)}


def _validate_live_issue_scope(snapshot: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("issues"), list):
        raise GateError("verified GitHub issue state has no issue list", "ISSUE_STATE_SCOPE")
    target_issues = tuple(AUDIT_RECOVERY_ISSUES)
    raw_numbers = [item.get("issueNumber") for item in snapshot["issues"] if isinstance(item, dict)]
    require(raw_numbers == list(target_issues), "verified GitHub issue state must be ordered as #87-#105 and #108-#113", "ISSUE_STATE_SCOPE")
    live_by_number = {
        int(item["issueNumber"]): item
        for item in snapshot["issues"]
        if isinstance(item, dict) and isinstance(item.get("issueNumber"), int)
    }
    require(set(live_by_number) == set(target_issues), "verified GitHub audit state is incomplete", "ISSUE_STATE_SCOPE")
    return live_by_number


def _require_umbrella_closed_last(live_by_number: dict[int, dict[str, Any]]) -> None:
    """Require #87 to be the final closure in the live issue snapshot."""

    umbrella = live_by_number.get(UMBRELLA_ISSUE, {}).get("closedAt")
    require(isinstance(umbrella, str), "umbrella issue #87 has no close timestamp", "UMBRELLA_NOT_LAST")
    try:
        from github_issue_state import parse_datetime
    except ImportError:  # pragma: no cover
        from tools.github_issue_state import parse_datetime
    umbrella_time = parse_datetime(umbrella, field="issue #87.closedAt")
    for issue_number, issue in live_by_number.items():
        if issue_number == UMBRELLA_ISSUE:
            continue
        require(isinstance(issue.get("closedAt"), str), f"issue #{issue_number} has no close timestamp", "UMBRELLA_NOT_LAST")
        child_time = parse_datetime(issue.get("closedAt"), field=f"issue #{issue_number}.closedAt")
        require(umbrella_time > child_time, f"umbrella issue #87 was not closed after issue #{issue_number}", "UMBRELLA_NOT_LAST")


def check_audit_recovery_release_boundary(
    *,
    issue_state: dict[str, Any] | None = None,
    issue_snapshot: Path | None = None,
    evidence_times: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Derive the recovery barrier from verified GitHub state.

    ``machine/audit-recovery-plan.json`` and the release claim manifest are
    checked for shape and contradiction only.  Their status fields never
    override the live response or a verified snapshot.
    """

    recovery = load_json(AUDIT_RECOVERY_PATH)
    require(isinstance(recovery, dict), "audit recovery plan root is not an object", "RECOVERY_PLAN_INVALID")
    require(recovery.get("schema") == "fdir/audit-recovery-plan", "audit recovery plan schema is missing", "RECOVERY_PLAN_INVALID")
    require(recovery.get("umbrellaIssue") == 87, "audit recovery plan is not bound to issue #87", "RECOVERY_PLAN_INVALID")
    require(recovery.get("repository") == GITHUB_REPOSITORY, "audit recovery plan repository is wrong", "RECOVERY_PLAN_REPOSITORY")
    children = recovery.get("children")
    require(isinstance(children, list), "audit recovery children are missing", "RECOVERY_PLAN_INVALID")
    child_by_number = {child.get("issueNumber"): child for child in children if isinstance(child, dict)}
    required_children = set(QUALIFICATION_ISSUES)
    require(set(child_by_number) == required_children, "audit recovery plan does not cover #88-#105 exactly", "RECOVERY_PLAN_SCOPE")

    source_sha = current_head()
    try:
        if issue_state is None:
            resolved = resolve_issue_state(source_sha=source_sha, snapshot_path=issue_snapshot)
            snapshot = resolved["snapshot"]
            authority = resolved["authority"]
        else:
            snapshot = issue_state.get("snapshot") if isinstance(issue_state.get("snapshot"), dict) else issue_state
            validate_snapshot(snapshot, expected_repository=GITHUB_REPOSITORY, expected_source_sha=source_sha)
            authority = issue_state.get("authority", "verified-snapshot") if isinstance(issue_state, dict) else "verified-snapshot"
    except IssueStateError as exc:
        raise GateError(exc.detail, exc.code) from exc

    boundary = derive_release_boundary(snapshot)
    live_by_number = _validate_live_issue_scope(snapshot)

    # A stale committed completion flag is itself a release failure.  It is
    # never used to make an open issue pass, but it must be visible as a
    # contradiction rather than silently ignored.
    if recovery.get("releaseBlocked") is not boundary.get("releaseBlocked"):
        raise GateError("committed audit recovery releaseBlocked contradicts verified GitHub state", "STATIC_RELEASE_STATE_CONTRADICTION")
    for issue_number, child in child_by_number.items():
        live_issue = live_by_number[issue_number]
        live_complete = live_issue.get("state") == "closed" and live_issue.get("stateReason") == "completed" and live_issue.get("closedAt") is not None
        if child.get("status") == "completed" and not live_complete:
            raise GateError(f"committed completion status cannot override live issue #{issue_number}", "STATIC_COMPLETION_CONTRADICTION")
        if live_complete and child.get("status") not in {"completed", "closed"}:
            raise GateError(f"closed live issue #{issue_number} has no completed recovery projection", "STATIC_COMPLETION_CONTRADICTION")

    qualification = recovery.get("qualificationEvidence")
    require(isinstance(qualification, dict), "audit recovery plan has no qualification evidence binding", "RECOVERY_QUALIFICATION_BINDING")
    require(qualification.get("manifestPath") == "qualification/<source-sha>/manifest.json", "audit recovery qualification manifest path is not source-SHA templated", "RECOVERY_QUALIFICATION_BINDING")
    contract = load_json(QUALIFICATION_CONTRACT_PATH)
    check_recovery_scope_contract(contract)
    expected_evidence = set(contract.get("scope", {}).get("requiredEvidenceIds", []))
    require(set(qualification.get("requiredEvidenceIds", [])) == expected_evidence, "audit recovery qualification evidence IDs do not match the contract", "RECOVERY_QUALIFICATION_BINDING")

    claims = load_json(RELEASE_CLAIM_MANIFEST_PATH)
    release = claims.get("release") if isinstance(claims, dict) else None
    require(isinstance(release, dict), "release claim manifest has no release state", "RELEASE_CLAIM_INVALID")
    expected_status = "release-blocked" if boundary.get("releaseBlocked") else "release-ready"
    if release.get("releaseBlocked") is not boundary.get("releaseBlocked") or release.get("status") != expected_status:
        raise GateError("release claim manifest state contradicts the verified issue boundary", "STATIC_MANIFEST_CONTRADICTION")
    binding = release.get("qualificationBinding")
    require(isinstance(binding, dict), "release claim manifest has no qualification binding", "RELEASE_CLAIM_INVALID")
    if boundary.get("releaseBlocked"):
        if qualification.get("status") not in {"blocked", "pending", "not-qualified"} or binding.get("status") not in {"blocked", "pending", "not-qualified"}:
            raise GateError("blocked live recovery cannot use passed static qualification claims", "STATIC_MANIFEST_CONTRADICTION")
    else:
        require(qualification.get("status") == "passed", "closed audit plan has no passed qualification evidence binding", "RECOVERY_QUALIFICATION_BINDING")
        require(binding.get("status") == "passed", "release claim manifest has no passed qualification binding", "RELEASE_CLAIM_INVALID")
        require(binding.get("manifestPath") == "qualification/<source-sha>/manifest.json", "release claim qualification path is not source-SHA templated", "RELEASE_CLAIM_INVALID")
        require(set(binding.get("requiredEvidenceIds", [])) == expected_evidence, "release claim qualification evidence IDs do not match the contract", "RELEASE_CLAIM_INVALID")
        for policy_name in ("issueClaimsPolicy", "capabilityClaimsPolicy"):
            policy = claims.get(policy_name)
            if isinstance(policy, dict) and policy.get("releaseEligible") is not True:
                raise GateError(f"release-ready claim has {policy_name}.releaseEligible=false", "STATIC_MANIFEST_CONTRADICTION")
        independent = claims.get("independentEvidence")
        if isinstance(independent, dict) and independent.get("releaseEligible") is not True:
            raise GateError("release-ready claim has independent evidence marked not-qualified", "STATIC_MANIFEST_CONTRADICTION")

    close_blockers = evidence_close_time_blockers(snapshot, evidence_times or {})
    if close_blockers:
        raise GateError(json.dumps(close_blockers, ensure_ascii=False, sort_keys=True), "GITHUB_ISSUE_CLOSED_BEFORE_EVIDENCE")
    if not boundary.get("releaseBlocked"):
        _require_umbrella_closed_last(live_by_number)
    return {
        "recovery_children": len(QUALIFICATION_ISSUES),
        "umbrella_issue": UMBRELLA_ISSUE,
        "live_issue_numbers": list(LIVE_ISSUES),
        "qualification_issue_numbers": list(QUALIFICATION_ISSUES),
        "barrier_issue_numbers": list(BARRIER_ISSUES),
        "live_open_issues": len(boundary.get("openIssues", [])),
        "releaseBlocked": boundary.get("releaseBlocked"),
        "status": boundary.get("status"),
        "authority": authority,
        "snapshotDigest": snapshot.get("snapshotDigest"),
        "retrievedAt": snapshot.get("retrievedAt"),
        "blockingIssues": boundary.get("blockingIssues", []),
    }


def check_schema() -> dict[str, int]:
    schema = load_json(SCHEMA_PATH)
    require(isinstance(schema, dict), "IR schema root must be an object")
    require(schema.get("type") == "object", "IR schema root must be an object type")
    require(schema.get("additionalProperties") is False, "IR schema root must be closed")
    required = schema.get("required")
    properties = schema.get("properties")
    defs = schema.get("$defs")
    require(isinstance(required, list), "IR schema required must be an array")
    require(REQUIRED_SCHEMA_FIELDS.issubset(set(required)), "IR schema is missing a required core field")
    require(isinstance(properties, dict), "IR schema properties are missing")
    require(REQUIRED_SCHEMA_FIELDS.issubset(set(properties)), "IR schema properties omit a required core field")
    require(isinstance(defs, dict) and defs, "IR schema definitions are missing")
    require("extension" in defs, "IR schema has no extension definition")
    require("criticality" in json.dumps(schema, ensure_ascii=False), "IR schema has no extension criticality")

    open_definitions: list[str] = []
    for name, definition in defs.items():
        if isinstance(definition, dict) and "properties" in definition and definition.get("additionalProperties") is not False:
            open_definitions.append(str(name))
    require(not open_definitions, "IR schema has open typed definitions: " + ", ".join(sorted(open_definitions)))

    leaks = find_product_boundary_leaks(schema, "schema", scan_values=False)
    require(not leaks, "product-boundary concept leaked into schema: " + ", ".join(leaks[:5]))
    for token in (
        "sourceBytes",
        "sourceByteStore",
        "contentAddressedSource",
        "RecordAssertion",
        "EquivalenceCertificate",
        "LineageCertificate",
        "AccountingItem",
        "semanticEquivalence",
    ):
        require(token not in json.dumps(schema, ensure_ascii=False), f"forbidden schema token: {token}")

    return {"required_core_fields": len(REQUIRED_SCHEMA_FIELDS), "typed_definitions": len(defs)}


def check_examples() -> dict[str, int]:
    require(EXAMPLES_PATH.is_dir(), "examples directory is missing")
    paths = sorted(EXAMPLES_PATH.glob("*.json"))
    require(len(paths) >= 6, f"expected at least 6 JSON examples, got {len(paths)}")
    actual_names = {path.name for path in paths}
    missing_examples = sorted(REQUIRED_EXAMPLES - actual_names)
    require(not missing_examples, "required examples are missing: " + ", ".join(missing_examples))
    formats: set[str] = set()
    for path in paths:
        data = load_json(path)
        require(isinstance(data, dict), f"example is not an object: {path.name}")
        schema_ref = data.get("schema")
        source_format = data.get("sourceFormat")
        require(isinstance(schema_ref, dict) and schema_ref.get("name") == "fdir/document-form", f"example has wrong schema: {path.name}")
        require(isinstance(data.get("documentId"), str) and data["documentId"], f"example has no documentId: {path.name}")
        require(isinstance(source_format, dict) and isinstance(source_format.get("name"), str), f"example has no source format: {path.name}")
        require(isinstance(data.get("nodes"), list) and data["nodes"], f"example has no nodes: {path.name}")
        conversion = data.get("conversion")
        require(isinstance(conversion, dict) and isinstance(conversion.get("status"), str), f"example has no conversion report: {path.name}")
        formats.add(source_format["name"])
        leaks = find_product_boundary_leaks(data, f"example:{path.name}", scan_values=True)
        require(not leaks, "product-boundary concept leaked into example: " + ", ".join(leaks[:5]))

    require({"docx", "xlsx", "pdf", "markdown"}.issubset(formats), "examples do not cover DOCX, XLSX, PDF, and Markdown")
    return {"json_examples": len(paths), "source_formats": len(formats)}


def check_real_input_e2e_assets() -> dict[str, int]:
    """Ensure the release contains a real-input path for every required format."""

    required = {
        "tools/adapter_common.py",
        "tools/adapter_docx.py",
        "tools/adapter_xlsx.py",
        "tools/adapter_pdf.py",
        "tools/adapter_markdown.py",
        "tools/ir_validation.py",
        "tools/convert_document.py",
        "tools/generate_e2e_fixtures.py",
        "tools/run_e2e.py",
        "tools/clean_room_replay.py",
        "e2e/README.md",
        "e2e/fixtures/README.md",
    }
    missing = sorted(relative(ROOT / item) for item in required if not (ROOT / item).is_file())
    require(not missing, "real-input E2E assets are missing: " + ", ".join(missing))
    generator = (ROOT / "tools" / "generate_e2e_fixtures.py").read_text(encoding="utf-8")
    runner = (ROOT / "tools" / "run_e2e.py").read_text(encoding="utf-8")
    for phrase in ("write_zip", "pdf_bytes", "MARKDOWN"):
        require(phrase in generator, f"E2E fixture generator lacks real {phrase} implementation")
    for phrase in ("real-input", "evidence", "validate", "canonical", "query", "malformed", "unsupported", "resource-limit", "consumed"):
        require(phrase.casefold() in runner.casefold(), f"E2E runner lacks required check: {phrase}")
    return {"required_assets": len(required), "formats": 4, "e2e_issue": EXPECTED_E2E_ISSUE}


def check_clean_room_replay() -> dict[str, int | str]:
    """Require two successful, same-SHA clean-room E2E runs with no diff."""

    check_clean_tree()
    report = load_json(CLEAN_ROOM_REPLAY_PATH)
    require(report.get("schema") == "fdir/clean-room-replay-report", "clean-room replay report schema is missing")
    require(report.get("version") == "1.1.0", "clean-room replay report version is invalid")
    expected_sha = current_head()
    require(report.get("sourceSha") == expected_sha, "clean-room replay source SHA does not match HEAD")
    runs = report.get("runs")
    require(isinstance(runs, list) and len(runs) == 2, "clean-room replay must contain exactly two runs")
    for run in runs:
        require(isinstance(run, dict) and run.get("status") == "passed" and run.get("returnCode") == 0 and run.get("timedOut") is False, "clean-room replay contains a non-passing run")
        require(isinstance(run.get("reportDigest"), str) and re.fullmatch(r"[0-9a-f]{64}", run["reportDigest"]), "clean-room run report digest is invalid")
        artifact_digest = run.get("artifactDigest")
        require(isinstance(artifact_digest, str) and SHA256_RE.fullmatch(artifact_digest), "clean-room deterministic artifact digest is invalid")
        artifact_files = run.get("artifactFiles")
        require(isinstance(artifact_files, list) and artifact_files, "clean-room deterministic artifact inventory is missing")
        for artifact in artifact_files:
            require(isinstance(artifact, dict) and safe_repository_path(artifact.get("path")), "clean-room artifact path is unsafe")
            require(isinstance(artifact.get("sha256"), str) and SHA256_RE.fullmatch(artifact["sha256"]), "clean-room artifact SHA-256 is invalid")
    comparison = report.get("comparison")
    require(isinstance(comparison, dict) and comparison.get("status") == "passed" and comparison.get("differenceCount") == 0 and comparison.get("differences") == [], "clean-room replay has an unexpected deterministic diff")
    diff_digest = comparison.get("diffDigest")
    require(isinstance(diff_digest, str) and SHA256_RE.fullmatch(diff_digest), "clean-room diff digest is invalid")
    require(isinstance(comparison.get("scope"), list) and comparison["scope"], "clean-room comparison scope is missing")
    require(report.get("status") == "passed", "clean-room replay report is not passed")
    return {"runs": len(runs), "difference_count": int(comparison["differenceCount"]), "diff_digest": diff_digest}


def check_documents() -> dict[str, int]:
    texts: list[str] = []
    for document in NORMATIVE_DOCUMENTS:
        path = ROOT / document
        require(path.is_file(), f"missing normative document: {document}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise GateError(f"document is not UTF-8: {document}") from exc
        require(content.strip(), f"normative document is empty: {document}")
        texts.append(content)

    all_text = "\n".join(texts)
    required_phrases = (
        "Parser / Adapter",
        "Document Form IR",
        "Semantic IR",
        "source map",
        "property bag",
    )
    for phrase in required_phrases:
        require(phrase.casefold() in all_text.casefold(), f"documentation is missing boundary phrase: {phrase}")

    boundary_terms = (
        r"semantic\s+IR",
        r"raw[- ]byte",
        r"source[- ]byte",
        r"(?:source|raw\s+byte)\s+store",
        r"forensic",
        r"accounting",
        r"lineage",
    )
    for term in boundary_terms:
        require(re.search(term, all_text, re.I), f"documentation is missing boundary term: {term}")

    # A boundary document may name an excluded concept, but it must also state
    # the direction or exclusion.  This prevents a positive product claim from
    # being hidden behind otherwise adequate documentation coverage.
    boundary_markers = (
        "downstream",
        "out of scope",
        "outside",
        "not ",
        "no ",
        "without",
        "excluded",
        "removed",
        "範囲外",
        "除外",
        "廃棄",
    )
    marked_lines = [line.casefold() for line in all_text.splitlines() if any(re.search(term, line, re.I) for term in boundary_terms)]
    require(any(any(marker in line for marker in boundary_markers) for line in marked_lines), "boundary terms are not accompanied by an exclusion or downstream marker")

    return {"normative_documents": len(NORMATIVE_DOCUMENTS), "boundary_marked_lines": len(marked_lines)}


def run_command(name: str, display_command: str, argv: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "command": display_command,
        "cwd": ".",
    }
    try:
        child_environment = os.environ.copy()
        # Runtime reports contain absolute paths under the Japanese workspace.
        # The Windows console default is often CP932, while this gate decodes
        # captured JSON as UTF-8; force the child Python process to use the
        # same encoding so paths cannot be silently replaced with U+FFFD.
        child_environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, *argv],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        result.update({"return_code": 124, "stdout": stdout.replace("\r\n", "\n"), "stderr": (stderr + "\ncommand timed out after 300 seconds").replace("\r\n", "\n"), "timed_out": True})
        return result
    except OSError as exc:
        result.update({"return_code": None, "stdout": "", "stderr": str(exc), "timed_out": False})
        return result

    result.update(
        {
            "return_code": completed.returncode,
            "stdout": completed.stdout.replace("\r\n", "\n"),
            "stderr": completed.stderr.replace("\r\n", "\n"),
            "timed_out": False,
        }
    )
    return result


def check_runtime_evidence(commands: list[dict[str, Any]]) -> dict[str, int]:
    """Validate the content of the qualification reports produced this run."""

    by_name = {item.get("name"): item for item in commands if isinstance(item, dict)}

    def report(name: str) -> dict[str, Any]:
        item = by_name.get(name)
        require(isinstance(item, dict) and item.get("return_code") == 0, f"runtime command did not pass: {name}")
        try:
            value = json.loads(str(item.get("stdout", "")))
        except json.JSONDecodeError as exc:
            raise GateError(f"runtime report is not JSON: {name}: {exc}") from exc
        require(isinstance(value, dict), f"runtime report is not an object: {name}")
        return value

    mutation = report("mutation_qualification")
    corpus = report("independent_corpus")
    query = report("query_qualification")
    e2e = report("real_input_e2e")
    strict = report("strict_completion")
    mutation_details = validate_mutation_report(mutation)
    require(corpus.get("schema") == "fdir/independent-fidelity-corpus-report" and corpus.get("version") == "1.0.0", "independent corpus report schema is invalid")
    corpus_cases = corpus.get("cases")
    require(corpus.get("status") == "passed" and isinstance(corpus_cases, list) and len(corpus_cases) >= 4, "independent corpus report is incomplete")
    require({case.get("format") for case in corpus_cases if isinstance(case, dict)} == {"docx", "xlsx", "pdf", "markdown"}, "independent corpus report format matrix is incomplete")
    negative_checks = corpus.get("negativeChecks")
    require(isinstance(negative_checks, list) and any(item.get("id") == "resource-limit" for item in negative_checks if isinstance(item, dict)), "independent corpus resource-limit evidence is missing")
    check_source_closure_report(corpus, "independent corpus")
    require(query.get("schema") == "fdir/query-qualification-report" and query.get("version") == "1.3.0", "query report schema is invalid")
    require(query.get("status") == "passed" and query.get("parity", {}).get("status") == "passed" and query.get("unqueryableFacts") == [], "query report is not fully green")
    require(isinstance(query.get("operations"), list) and query["operations"], "query report has no executed operations")
    require(e2e.get("schema") == "fdir/e2e-report" and e2e.get("version") == "1.0.0", "real-input E2E report schema is invalid")
    require(e2e.get("status") == "passed" and set(e2e.get("formats", [])) == {"docx", "xlsx", "pdf", "markdown"}, "real-input E2E report is not fully green")
    e2e_cases = e2e.get("cases")
    require(isinstance(e2e_cases, list) and len(e2e_cases) == 16, "real-input E2E case inventory is incomplete")
    require(all(isinstance(case, dict) and isinstance(case.get("id"), str) and case.get("id") for case in e2e_cases), "real-input E2E case inventory is malformed")
    check_source_closure_report(e2e, "real-input E2E")
    require(strict.get("schema") == "fdir/strict-completion-gate-report", "strict completion report schema is invalid")
    require(strict.get("status") == "passed" and strict.get("blockers") == [], "strict completion report is not fully green")
    return {
        **mutation_details,
        "independent_cases": len(corpus_cases),
        "independent_negative_checks": len(negative_checks),
        "query_sources": len(query.get("sources", [])),
        "e2e_cases": len(e2e_cases),
        "strict_issues": len(strict.get("issues", [])),
    }


def bundle_output_path(report: dict[str, Any], basename: str) -> str:
    matches = sorted(
        str(item.get("path"))
        for item in report.get("outputs", [])
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and PurePosixPath(item["path"]).name == basename
    )
    require(len(matches) == 1, f"evidence report {report.get('evidenceId')} must bind exactly one {basename}: {matches}")
    return matches[0]


def load_bundle_output(bundle_root: Path, report: dict[str, Any], basename: str) -> dict[str, Any]:
    path = bundle_output_path(report, basename)
    target = bundle_root / Path(*PurePosixPath(path).parts)
    value = load_json(target)
    require(isinstance(value, dict), f"bundle output is not a JSON object: {path}")
    return value


def semantic_assertion_count(value: dict[str, Any]) -> int:
    assertions = value.get("assertions")
    require(isinstance(assertions, list) and assertions, "evidence output has no assertions")
    semantic = 0
    for assertion in assertions:
        require(isinstance(assertion, dict), "evidence output contains a malformed assertion")
        assertion_id = assertion.get("assertionId", assertion.get("id"))
        if assertion_id not in GENERIC_BUNDLE_ASSERTIONS:
            semantic += 1
        require(assertion.get("status") == "passed", f"evidence output assertion is not passed: {assertion_id}")
    require(semantic > 0, "evidence output contains command-exit-only assertions")
    return semantic


def check_qualification_bundle(bundle_manifest: Path, *, require_actions: bool = False) -> dict[str, int]:
    """Check the semantic and identity properties beyond the generic bundle schema."""

    manifest_path = bundle_manifest.resolve()
    bundle_root = manifest_path.parent
    manifest = load_json(manifest_path)
    require(isinstance(manifest, dict), "qualification bundle manifest is not an object")
    source_sha = manifest.get("sourceSha")
    require(isinstance(source_sha, str) and SOURCE_SHA_RE.fullmatch(source_sha), "qualification bundle source SHA is invalid")
    require(source_sha == current_head(), "qualification bundle source SHA does not match HEAD")
    require(manifest.get("dirtyTree") is False, "qualification bundle is not bound to a clean tree")

    contract = load_json(QUALIFICATION_CONTRACT_PATH)
    check_recovery_scope_contract(contract)
    scope = contract.get("scope") if isinstance(contract, dict) else None
    required_ids = set(scope.get("requiredEvidenceIds", [])) if isinstance(scope, dict) else set()
    require(required_ids, "qualification contract has no required Evidence IDs")
    require(set(scope.get("issueNumbers", [])) == set(QUALIFICATION_ISSUES), "qualification contract scope is not #88-#105")

    reports_dir = bundle_root / "reports"
    report_paths = sorted(reports_dir.glob("*.json")) if reports_dir.is_dir() else []
    require(report_paths, "qualification bundle has no Evidence reports")
    reports: dict[str, dict[str, Any]] = {}
    covered_issue_numbers: set[int] = set()
    for path in report_paths:
        value = load_json(path)
        require(isinstance(value, dict), f"qualification Evidence report is not an object: {path.name}")
        evidence_id = value.get("evidenceId")
        require(isinstance(evidence_id, str) and evidence_id not in reports, f"qualification Evidence ID is invalid or duplicated: {evidence_id!r}")
        require(evidence_id in required_ids, f"qualification bundle contains an unscoped Evidence ID: {evidence_id}")
        issue_numbers = value.get("issueNumbers")
        require(isinstance(issue_numbers, list) and issue_numbers and all(isinstance(item, int) and not isinstance(item, bool) for item in issue_numbers), f"qualification Evidence issue scope is invalid: {evidence_id}")
        require(len(issue_numbers) == len(set(issue_numbers)), f"qualification Evidence repeats an issue number: {evidence_id}")
        require(set(issue_numbers) <= set(QUALIFICATION_ISSUES), f"qualification Evidence binds a live-only issue: {evidence_id}")
        require(not covered_issue_numbers.intersection(issue_numbers), f"qualification Evidence duplicates report coverage: {evidence_id}")
        covered_issue_numbers.update(issue_numbers)
        require(value.get("sourceSha") == source_sha, f"qualification Evidence source SHA mismatch: {evidence_id}")
        require(value.get("dirtyTree") is False, f"qualification Evidence is dirty: {evidence_id}")
        require(value.get("status") == "passed" and value.get("failureCount") == 0, f"qualification Evidence is not passed: {evidence_id}")
        require_no_placeholder(value.get("generator"), f"Evidence generator {evidence_id}")
        for field in ("command", "inputs", "outputs"):
            entries = value.get(field)
            require(isinstance(entries, list) and entries, f"qualification Evidence has no {field}: {evidence_id}")
            for entry in entries:
                if isinstance(entry, str):
                    require_no_placeholder(entry, f"Evidence {evidence_id} {field}")
                elif isinstance(entry, dict):
                    require_no_placeholder(entry.get("path"), f"Evidence {evidence_id} {field} path")
        reports[evidence_id] = value

    require(set(reports) == required_ids, "qualification bundle Evidence IDs do not exactly match #88-#105")

    # #105 is a Phase-A behavioral candidate only.  The separate final
    # attestation is the release authority; a report that invokes a release
    # gate from inside the bundle is the old circular self-qualification path.
    try:
        try:
            from release_attestation import _candidate_105_receipt
        except ImportError:  # pragma: no cover
            from tools.release_attestation import _candidate_105_receipt
        _candidate_105_receipt(bundle_root, reports["issue-105-release-quality"])
    except Exception as exc:
        raise GateError(str(exc), getattr(exc, "code", "CIRCULAR_105_EVIDENCE")) from exc

    ci_records: list[tuple[Any, ...]] = []
    actions = os.environ.get("GITHUB_ACTIONS", "").casefold() == "true"
    for evidence_id, report in sorted(reports.items()):
        ci = report.get("ci")
        require(isinstance(ci, dict), f"qualification Evidence has no CI binding: {evidence_id}")
        record = tuple(ci.get(field) for field in ("provider", "repository", "sourceSha", "runId", "runUrl", "jobId", "attempt", "status"))
        ci_records.append(record)
        require(ci.get("sourceSha") == source_sha and ci.get("repository") == "horiyamayoh/fdir", f"qualification Evidence CI binding mismatch: {evidence_id}")
        require(ci.get("status") == "completed", f"qualification Evidence CI status is not completed: {evidence_id}")
        if actions or require_actions:
            require(ci.get("provider") == "github-actions", f"qualification Evidence is not bound to GitHub Actions: {evidence_id}")
            require(isinstance(ci.get("runId"), str) and re.fullmatch(r"[1-9][0-9]*", ci["runId"]), f"qualification Evidence run ID is invalid: {evidence_id}")
            require(isinstance(ci.get("runUrl"), str) and re.fullmatch(r"https://github\.com/horiyamayoh/fdir/actions/runs/[1-9][0-9]*", ci["runUrl"]), f"qualification Evidence run URL is invalid: {evidence_id}")
        else:
            require(ci.get("provider") == "local" and isinstance(ci.get("runUrl"), str) and ci["runUrl"].startswith("local://"), f"local qualification Evidence CI binding is invalid: {evidence_id}")
    require(covered_issue_numbers == set(QUALIFICATION_ISSUES), "qualification Evidence reports must cover exactly #88-#105")
    require(len(set(ci_records)) == 1, "qualification bundle mixes CI runs or environments")

    recovery_contract = load_json(ROOT / "machine" / "recovery-report-contract.json")
    required_reports = recovery_contract.get("reports") if isinstance(recovery_contract, dict) else None
    require(isinstance(required_reports, dict), "recovery report contract is missing")
    semantic_count = 0
    named_output_count = 0
    for issue_text, names in sorted(required_reports.items(), key=lambda item: str(item[0])):
        issue_number = int(issue_text)
        issue_reports = [report for report in reports.values() if issue_number in report.get("issueNumbers", [])]
        require(issue_reports, f"qualification bundle has no report for issue #{issue_number}")
        for name in names:
            matches = [report for report in issue_reports if any(PurePosixPath(str(item.get("path"))).name == name for item in report.get("outputs", []) if isinstance(item, dict))]
            require(len(matches) == 1, f"issue #{issue_number} requires exactly one semantic report {name!r}")
            value = load_bundle_output(bundle_root, matches[0], name)
            require(value.get("status") == "passed", f"issue #{issue_number} report {name} is not passed")
            require(value.get("sourceSha") == source_sha, f"issue #{issue_number} report {name} has the wrong source SHA")
            semantic_count += semantic_assertion_count(value)
            named_output_count += 1

    mutation_report = load_bundle_output(bundle_root, reports["issue-89-defect-injection"], "defect-injection-campaign.json")
    require(mutation_report.get("schema") == "fdir/defect-injection-campaign-report", "#89 campaign report schema is invalid")
    require(mutation_report.get("status") == "passed" and mutation_report.get("sourceSha") == source_sha, "#89 defect-injection campaign is not passed and SHA-bound")
    require(mutation_report.get("undetected") == [], "#89 defect-injection campaign has undetected mutations")
    completion = mutation_report.get("completion")
    require(isinstance(completion, dict) and completion.get("must_undetected_zero") is True and completion.get("coverage_complete") is True, "#89 mutation completion is incomplete")
    require(isinstance(mutation_report.get("cases"), list) and mutation_report["cases"], "#89 campaign has no executable mutation cases")

    return {"bundle_evidence": len(reports), "bundle_named_reports": named_output_count, "bundle_semantic_assertions": semantic_count}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the FDIR design and acceptance release gate.")
    parser.add_argument(
        "--mode",
        choices=("smoke", "release"),
        default=None,
        help="smoke is development-only; release requires both a bundle and final attestation",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        metavar="PATH",
        help="also write the deterministic JSON summary to PATH (relative to the repository root)",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        metavar="MANIFEST",
        help="commit-bound qualification bundle manifest; release also requires --attestation",
    )
    parser.add_argument(
        "--attestation",
        type=Path,
        metavar="JSON",
        help="final external release attestation bound to the selected --bundle",
    )
    parser.add_argument(
        "--issue-snapshot",
        type=Path,
        metavar="JSON",
        help="verified GitHub issue-state snapshot to use instead of making live API calls",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    has_bundle = args.bundle is not None
    has_attestation = args.attestation is not None
    has_release_authority = has_bundle and has_attestation
    release_mode = args.mode == "release" or has_bundle or has_attestation
    if not has_release_authority:
        # This is the only intentionally cheap path.  The bundle builder uses
        # it for the #105 Phase-A receipt; it must be a successful command with
        # a blocked result, never a release-ready result.
        if has_bundle and not has_attestation:
            diagnostic_code = "FINAL_ATTESTATION_REQUIRED"
            diagnostic_detail = "release qualification requires --bundle and --attestation; a candidate bundle alone is not release authority"
        elif has_attestation and not has_bundle:
            diagnostic_code = "BUNDLE_REQUIRED"
            diagnostic_detail = "release qualification requires --bundle and --attestation; an attestation without its candidate bundle is diagnostic-only"
        else:
            diagnostic_code = "RELEASE_AUTHORITY_REQUIRED"
            diagnostic_detail = "release qualification requires --bundle and --attestation; this invocation is development smoke only"
        summary = {
            "schema": "fdir/release-gate-summary",
            "version": "1.1.0",
            "status": "blocked",
            "releaseReady": False,
            "mode": "release" if release_mode else "smoke",
            "exit_code": 1,
            "diagnostics": [{
                "code": diagnostic_code,
                "detail": diagnostic_detail,
            }],
            "checks": [],
            "commands": [],
        }
        if args.summary is not None:
            summary_path = args.summary if args.summary.is_absolute() else ROOT / args.summary
            try:
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
            except OSError:
                pass
        rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        stdout_buffer = getattr(sys.stdout, "buffer", None)
        if stdout_buffer is not None:
            stdout_buffer.write(rendered.encode("utf-8"))
        else:  # pragma: no cover
            sys.stdout.write(rendered)
        # Smoke is not a release result and is allowed to be consumed as the
        # explicit blocked #105 receipt.  Any partial authority invocation,
        # and explicit release mode, fail closed.
        return 1 if release_mode else 0
    checks: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []

    attestation_snapshot: dict[str, Any] | None = None
    attestation_validation: dict[str, Any] | None = None
    authority_checks: list[dict[str, Any]] = []
    if args.attestation is not None:
        try:
            attestation_value = load_json(args.attestation)
            expected_attempt: Any = os.environ.get("GITHUB_RUN_ATTEMPT")
            if isinstance(expected_attempt, str) and expected_attempt.isdigit():
                expected_attempt = int(expected_attempt)
            attestation_validation = validate_attestation(
                attestation_value,
                bundle_manifest_path=args.bundle,
                expected_source_sha=current_head(),
                expected_run_id=os.environ.get("GITHUB_RUN_ID") if os.environ.get("GITHUB_ACTIONS", "").casefold() == "true" else None,
                expected_attempt=expected_attempt if os.environ.get("GITHUB_ACTIONS", "").casefold() == "true" else None,
                repo_root=ROOT,
            )
            attestation_snapshot = attestation_validation.get("snapshot")
        except (GateError, AttestationError, IssueStateError) as exc:
            authority_checks.append({"name": "release_attestation", "status": "failed", "error": str(exc), "code": getattr(exc, "code", "ATTESTATION_INVALID")})
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            authority_checks.append({"name": "release_attestation", "status": "failed", "error": f"unexpected {type(exc).__name__}: {exc}", "code": "ATTESTATION_INVALID"})
        else:
            authority_checks.append({"name": "release_attestation", "status": "passed", "details": {"attestationDigest": attestation_validation.get("attestationDigest"), "snapshotDigest": attestation_snapshot.get("snapshotDigest") if isinstance(attestation_snapshot, dict) else None}})
    checks.extend(authority_checks)

    strict_command = ["tools/strict_completion_gate.py"]
    strict_display = "python tools/strict_completion_gate.py"
    if args.bundle is not None:
        strict_command.extend(["--bundle", str(args.bundle)])
        strict_display += f" --bundle {args.bundle}"
    if args.attestation is not None:
        strict_command.extend(["--attestation", str(args.attestation)])
        strict_display += f" --attestation {args.attestation}"

    for name, display, command in (
        ("design_validation", "python tools/validate_design.py", ["tools/validate_design.py"]),
        ("model_contract", "python tools/validate_model_contract.py --check", ["tools/validate_model_contract.py", "--check"]),
        ("qualification_schema", "python tools/validate_qualification_bundle.py --schema-only", ["tools/validate_qualification_bundle.py", "--schema-only"]),
        ("evidence_integrity", "python tools/test_evidence_integrity.py --all", ["tools/test_evidence_integrity.py", "--all"]),
        ("acceptance_all", "python tools/run_acceptance.py --all", ["tools/run_acceptance.py", "--all"]),
        ("real_input_e2e", "python tools/run_e2e.py --all --json", ["tools/run_e2e.py", "--all", "--json"]),
        ("mutation_qualification", "python tools/mutation_qualification.py --json", ["tools/mutation_qualification.py", "--json"]),
        ("query_qualification", "python tools/query_qualification.py", ["tools/query_qualification.py"]),
        ("independent_corpus", "python tools/independent_corpus.py --json", ["tools/independent_corpus.py", "--json"]),
        ("strict_completion", strict_display, strict_command),
    ):
        command_result = run_command(name, display, command)
        commands.append(command_result)
        passed = command_result.get("return_code") == 0
        checks.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "details": {
                    "command": display,
                    "return_code": command_result.get("return_code"),
                },
            }
        )

    try:
        runtime_details = check_runtime_evidence(commands)
    except GateError as exc:
        checks.append({"name": "runtime_evidence", "status": "failed", "error": str(exc), "code": exc.code})
        runtime_details = {"mutation_cases": 0, "independent_cases": 0, "independent_negative_checks": 0, "query_sources": 0, "e2e_cases": 0, "strict_issues": 0}
    else:
        checks.append({"name": "runtime_evidence", "status": "passed", "details": runtime_details})

    check_functions: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("clean_worktree", check_clean_tree),
        ("ci_binding", lambda: check_ci_binding(require_actions=True)),
        ("design_catalog", check_design_catalog),
        ("issue_plan_and_github_map", check_issue_plan),
        ("phase2_contracts", check_phase2_contracts),
        ("release_claims", check_release_claims),
        ("audit_recovery_release_boundary", lambda: check_audit_recovery_release_boundary(issue_state=attestation_snapshot, issue_snapshot=args.issue_snapshot)),
        ("traceability", check_traceability),
        ("schema", check_schema),
        ("examples", check_examples),
        ("real_input_e2e_assets", check_real_input_e2e_assets),
        ("clean_room_replay", check_clean_room_replay),
        ("documents_and_boundaries", check_documents),
    )
    for name, function in check_functions:
        try:
            details = function()
        except GateError as exc:
            checks.append({"name": name, "status": "failed", "error": str(exc), "code": exc.code})
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            checks.append({"name": name, "status": "failed", "error": f"unexpected {type(exc).__name__}: {exc}"})
        else:
            checks.append({"name": name, "status": "passed", "details": details})

    if args.bundle is not None:
        try:
            try:
                from validate_qualification_bundle import validate_bundle
            except ImportError:  # pragma: no cover
                from tools.validate_qualification_bundle import validate_bundle
            bundle_result = validate_bundle(args.bundle, repo_root=ROOT)
            if bundle_result.get("status") != "passed":
                raise GateError(json.dumps(bundle_result.get("diagnostics", []), ensure_ascii=False), "QUALIFICATION_BUNDLE_INVALID")
            bundle_details = check_qualification_bundle(args.bundle, require_actions=True)
        except Exception as exc:
            checks.append({"name": "qualification_bundle", "status": "failed", "error": str(exc), "code": getattr(exc, "code", "QUALIFICATION_BUNDLE_INVALID")})
        else:
            checks.append({"name": "qualification_bundle", "status": "passed", "details": {"manifest": str(args.bundle), **bundle_details}})
    else:
        audit_check = next((item for item in checks if item.get("name") == "audit_recovery_release_boundary"), None)
        audit_details = audit_check.get("details") if isinstance(audit_check, dict) else None
        if isinstance(audit_details, dict) and audit_details.get("live_open_issues") == 0:
            checks.append({"name": "qualification_bundle", "status": "failed", "error": "release-ready gate requires --bundle for exact-SHA Evidence validation"})

    # A candidate bundle is a Phase-A input to the external attestation step;
    # it is not itself final release authority.  Requiring the attestation here
    # prevents a bundle-only invocation from turning a passed candidate into a
    # release-ready summary.
    if args.bundle is not None and args.attestation is None:
        checks.append({
            "name": "final_attestation",
            "status": "failed",
            "code": "FINAL_ATTESTATION_REQUIRED",
            "error": "a candidate bundle requires an external final attestation before release-ready can be emitted",
        })

    passed = all(check["status"] == "passed" for check in checks)
    summary: dict[str, Any] = {
        "schema": "fdir/release-gate-summary",
        "version": "1.1.0",
        "status": "passed" if passed else "failed",
        "releaseReady": bool(passed),
        "mode": "release",
        "exit_code": 0 if passed else 1,
        "reproducibility": {
            "repository_root": ".",
            "python_command": "python",
            "logs_are_normalized": True,
        },
        "counts": {
            "requirements": EXPECTED_REQUIREMENTS,
            "acceptance_families": EXPECTED_FAMILIES,
            "acceptance_cases": EXPECTED_CASES,
            "leaf_issues": EXPECTED_LEAF_ISSUES,
            "umbrella_issue": EXPECTED_UMBRELLA_ISSUE,
            "leaf_issue_first": min(EXPECTED_LEAF_ISSUE_RANGE),
            "leaf_issue_last": max(EXPECTED_LEAF_ISSUE_RANGE),
            "e2e_issue": EXPECTED_E2E_ISSUE,
            "phase2_issues": len(list(range(69, 85)) + [85, 86]),
            "phase2_issue_first": 69,
            "phase2_issue_last": 86,
            "phase2_duplicate_issue": 85,
            **runtime_details,
        },
        "checks": checks,
        "commands": commands,
    }

    if args.summary is not None:
        summary_path = args.summary if args.summary.is_absolute() else ROOT / args.summary
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            summary["status"] = "failed"
            summary["exit_code"] = 1
            summary["checks"].append({"name": "summary_output", "status": "failed", "error": str(exc)})
            passed = False

    rendered_summary = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    # CI is UTF-8, but local Windows shells can still expose a legacy code
    # page.  Writing the JSON bytes directly keeps the summary valid and
    # reproducible regardless of the console locale.
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(rendered_summary.encode("utf-8"))
    else:  # pragma: no cover - useful when main() is called with StringIO
        sys.stdout.write(rendered_summary)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
