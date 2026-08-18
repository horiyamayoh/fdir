#!/usr/bin/env python3
"""Fail-closed validation for the FDIR implementation and dependency boundary."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

RECEIPT_SCHEMA = "fdir/implementation-policy-validation/1"
POLICY_PATH = "machine/implementation-policy.yaml"
SCHEMA_PATH = "machine/dependency-manifest.schema.json"
CATALOG_PATH = "machine/dependency-catalog.yaml"
ADR_PATH = "machine/adrs/0004-implementation-boundary-and-evidence-lanes.md"
ASSESSMENTS_PATH = "references/dependency-candidate-assessments.md"
ISSUE_TEMPLATE_PATH = ".github/ISSUE_TEMPLATE/dependency.yml"
HANDOFF_PATH = "release/development-handoff.md"

EXPECTED_LANES = (
    "native-substrate-census",
    "semantic-helper",
    "renderer-observation",
    "ocr-inference-observation",
    "storage-codec",
)
EXPECTED_KINDS = (
    "rust-crate",
    "python-package",
    "native-library",
    "parser",
    "renderer",
    "ocr-engine",
    "evaluator",
    "codec",
    "worker",
    "model",
    "resource",
)
EXPECTED_BOUNDARIES = (
    "trusted-core",
    "in-process",
    "isolated-worker",
    "external-service-forbidden",
)
EXPECTED_NETWORK_POLICIES = ("deny", "allowlisted", "required")
EXPECTED_QUALIFICATION_STATES = (
    "candidate",
    "admitted-unqualified",
    "adapter-qualified",
    "production-qualified",
    "rejected",
)
EXPECTED_REQUIRED_FIELDS = (
    "schema",
    "id",
    "name",
    "kind",
    "version",
    "features",
    "implementationLanguage",
    "evidenceLanes",
    "inputKinds",
    "outputKinds",
    "normalizations",
    "unavailableSourceDistinctions",
    "unsafeCode",
    "ffi",
    "nativeCode",
    "receivesUntrustedDocumentBytes",
    "processBoundary",
    "license",
    "advisorySnapshot",
    "determinism",
    "networkPolicy",
    "resourceCharacteristics",
    "qualificationState",
    "ownerIssue",
)
DOCUMENT_WORKER_KINDS = {
    "parser",
    "renderer",
    "ocr-engine",
    "evaluator",
    "worker",
    "python-package",
    "native-library",
}
POLICY_REQUIREMENT_IDS = {
    "FDIR-AUTH-003",
    "FDIR-EVID-001",
    "FDIR-EVID-003",
    "FDIR-ACCT-002",
    "FDIR-COMP-002",
    "FDIR-ID-001",
    "FDIR-VAL-001",
    "FDIR-CLAIM-001",
    "FDIR-PKG-001",
}
STALE_AUTHORITY_PATTERNS = (
    re.compile(r"canonical-cbor-and-identity", re.IGNORECASE),
    re.compile(r"canonical\s+CBOR\s+and\s+identity", re.IGNORECASE),
)
FLOATING_VERSIONS = {
    "*",
    "current",
    "dev",
    "development",
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "stable",
    "tip",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def duplicates(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    duplicate_values: set[Any] = set()
    for value in values:
        if value in seen:
            duplicate_values.add(value)
        else:
            seen.add(value)
    return sorted(duplicate_values, key=str)


def require_exact_keys(
    value: Any,
    required: set[str],
    optional: set[str],
    label: str,
    failures: list[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        failures.append(f"{label} must be an object")
        return {}
    keys = set(value)
    for key in sorted(required - keys):
        failures.append(f"missing required field: {label}.{key}")
    for key in sorted(keys - required - optional):
        failures.append(f"unknown field: {label}.{key}")
    return value


def require_string(
    value: Any,
    label: str,
    failures: list[str],
    *,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str):
        failures.append(f"{label} must be a string")
        return ""
    if nonempty and not value.strip():
        failures.append(f"{label} must be non-empty")
    return value


def require_bool(value: Any, label: str, failures: list[str]) -> bool:
    if not isinstance(value, bool):
        failures.append(f"{label} must be a boolean")
        return False
    return value


def require_string_list(
    value: Any,
    label: str,
    failures: list[str],
    *,
    nonempty: bool = False,
    unique: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        failures.append(f"{label} must be a list")
        return []
    if nonempty and not value:
        failures.append(f"{label} must be non-empty")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            failures.append(f"{label}[{index}] must be a string")
        elif not item.strip():
            failures.append(f"{label}[{index}] must be non-empty")
        else:
            result.append(item)
    if unique:
        for item in duplicates(result):
            failures.append(f"duplicate value: {label}={item}")
    return result


def exact_version(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    lowered = normalized.lower()
    if not normalized or normalized != value or lowered in FLOATING_VERSIONS:
        return False
    if re.search(r"\s|[\*\^~<>|,]", normalized):
        return False
    if re.search(r"(?:^|[.\-_])x(?:$|[.\-_])", lowered):
        return False
    if ".." in normalized or lowered.startswith(("branch:", "ref:refs/heads/")):
        return False
    return True


def valid_exception(value: Any, label: str, failures: list[str]) -> bool:
    before = len(failures)
    entry = require_exact_keys(
        value,
        {"adr", "threatAnalysis", "boundedInputContract", "qualificationEvidence"},
        set(),
        label,
        failures,
    )
    require_string(entry.get("adr"), f"{label}.adr", failures)
    require_string(entry.get("threatAnalysis"), f"{label}.threatAnalysis", failures)
    require_string(entry.get("boundedInputContract"), f"{label}.boundedInputContract", failures)
    require_string_list(
        entry.get("qualificationEvidence"),
        f"{label}.qualificationEvidence",
        failures,
        nonempty=True,
        unique=True,
    )
    return len(failures) == before


def validate_manifest(
    manifest: Any,
    schema: dict[str, Any],
    policy: dict[str, Any],
    label: str,
    *,
    root: Path | None = None,
) -> list[str]:
    failures: list[str] = []
    properties = schema.get("properties")
    allowed = set(properties) if isinstance(properties, dict) else set()
    required = set(schema.get("required", []))
    entry = require_exact_keys(
        manifest,
        required,
        allowed - required,
        label,
        failures,
    )

    if entry.get("schema") != "fdir/dependency-manifest/1":
        failures.append(f"{label}.schema must be fdir/dependency-manifest/1")
    identifier = require_string(entry.get("id"), f"{label}.id", failures)
    if identifier and not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", identifier):
        failures.append(f"{label}.id has an invalid identifier spelling")
    require_string(entry.get("name"), f"{label}.name", failures)

    kind = entry.get("kind")
    if kind not in EXPECTED_KINDS:
        failures.append(f"{label}.kind is unknown: {kind!r}")
    version = entry.get("version")
    if not exact_version(version):
        failures.append(f"{label}.version must be an exact immutable version or build identity")
    require_string_list(entry.get("features"), f"{label}.features", failures, unique=True)
    language = require_string(
        entry.get("implementationLanguage"),
        f"{label}.implementationLanguage",
        failures,
    )

    lanes = require_string_list(
        entry.get("evidenceLanes"),
        f"{label}.evidenceLanes",
        failures,
        nonempty=True,
        unique=True,
    )
    for lane in lanes:
        if lane not in EXPECTED_LANES:
            failures.append(f"{label}.evidenceLanes contains unknown lane: {lane}")

    for field in (
        "inputKinds",
        "outputKinds",
        "normalizations",
        "unavailableSourceDistinctions",
    ):
        require_string_list(entry.get(field), f"{label}.{field}", failures, unique=True)

    unsafe_code = require_bool(entry.get("unsafeCode"), f"{label}.unsafeCode", failures)
    ffi = require_bool(entry.get("ffi"), f"{label}.ffi", failures)
    native_code = require_bool(entry.get("nativeCode"), f"{label}.nativeCode", failures)
    untrusted = require_bool(
        entry.get("receivesUntrustedDocumentBytes"),
        f"{label}.receivesUntrustedDocumentBytes",
        failures,
    )
    boundary = entry.get("processBoundary")
    if boundary not in EXPECTED_BOUNDARIES:
        failures.append(f"{label}.processBoundary is unknown: {boundary!r}")

    native_authority = entry.get("nativeAuthority", False)
    independent_census = entry.get("independentCensus", False)
    if "nativeAuthority" in entry:
        require_bool(native_authority, f"{label}.nativeAuthority", failures)
    if "independentCensus" in entry:
        require_bool(independent_census, f"{label}.independentCensus", failures)
    if native_authority and "native-substrate-census" not in lanes:
        failures.append(f"{label} claims native authority without native-substrate-census lane")
    if independent_census and "native-substrate-census" not in lanes:
        failures.append(f"{label} claims independent census without native-substrate-census lane")
    if "semantic-helper" in lanes and native_authority:
        failures.append(f"{label} semantic-helper output cannot claim native authority")
    if "semantic-helper" in lanes and independent_census:
        failures.append(f"{label} semantic-helper output cannot claim independent census")
    if "renderer-observation" in lanes and native_authority:
        failures.append(f"{label} renderer observation cannot claim native authority")
    if "ocr-inference-observation" in lanes and native_authority:
        failures.append(f"{label} OCR/inference observation cannot claim native authority")

    exception_failures: list[str] = []
    exception_ok = False
    if "inProcessException" in entry:
        exception_ok = valid_exception(
            entry.get("inProcessException"),
            f"{label}.inProcessException",
            exception_failures,
        )
        failures.extend(exception_failures)

    unsafe_untrusted = untrusted and (unsafe_code or ffi or native_code)
    non_rust_untrusted_worker = (
        untrusted
        and language.strip().lower() != "rust"
        and kind in DOCUMENT_WORKER_KINDS
    )
    isolation_required = unsafe_untrusted or non_rust_untrusted_worker
    if isolation_required and boundary != "isolated-worker":
        if not (boundary == "in-process" and exception_ok):
            reason = "unsafe/FFI/native" if unsafe_untrusted else "non-Rust document worker"
            failures.append(f"{label} {reason} receiving untrusted bytes must be isolated")
    if boundary == "trusted-core" and untrusted:
        failures.append(f"{label} trusted-core dependency cannot receive untrusted document bytes")
    if "inProcessException" in entry and boundary != "in-process":
        failures.append(f"{label}.inProcessException is permitted only for an in-process boundary")

    if exception_ok and root is not None:
        exception = entry["inProcessException"]
        references = [
            ("adr", exception["adr"]),
            ("threatAnalysis", exception["threatAnalysis"]),
            ("boundedInputContract", exception["boundedInputContract"]),
        ]
        references.extend(
            ("qualificationEvidence", relative)
            for relative in exception["qualificationEvidence"]
        )
        for field, relative in references:
            path = root / relative
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                failures.append(f"{label}.inProcessException.{field} escapes the repository: {relative}")
                continue
            if not path.is_file():
                failures.append(
                    f"{label}.inProcessException.{field} references missing evidence: {relative}"
                )

    license_entry = require_exact_keys(
        entry.get("license"),
        {"spdx", "reviewStatus"},
        set(),
        f"{label}.license",
        failures,
    )
    require_string(license_entry.get("spdx"), f"{label}.license.spdx", failures)
    if license_entry.get("reviewStatus") not in {"pending", "accepted", "rejected"}:
        failures.append(f"{label}.license.reviewStatus is invalid")

    advisory = require_exact_keys(
        entry.get("advisorySnapshot"),
        {"asOf", "source", "status"},
        set(),
        f"{label}.advisorySnapshot",
        failures,
    )
    as_of = require_string(advisory.get("asOf"), f"{label}.advisorySnapshot.asOf", failures)
    if as_of:
        try:
            dt.date.fromisoformat(as_of)
        except ValueError:
            failures.append(f"{label}.advisorySnapshot.asOf must be an ISO date")
    require_string(advisory.get("source"), f"{label}.advisorySnapshot.source", failures)
    if advisory.get("status") not in {"pending", "clear", "findings-open", "rejected"}:
        failures.append(f"{label}.advisorySnapshot.status is invalid")

    determinism = require_exact_keys(
        entry.get("determinism"),
        {"claim", "nondeterminismRecorded"},
        set(),
        f"{label}.determinism",
        failures,
    )
    if determinism.get("claim") not in {
        "deterministic",
        "bounded-nondeterministic",
        "unknown",
    }:
        failures.append(f"{label}.determinism.claim is invalid")
    recorded = require_bool(
        determinism.get("nondeterminismRecorded"),
        f"{label}.determinism.nondeterminismRecorded",
        failures,
    )
    if determinism.get("claim") == "bounded-nondeterministic" and not recorded:
        failures.append(f"{label} bounded nondeterminism must be recorded")

    network_policy = entry.get("networkPolicy")
    if network_policy not in EXPECTED_NETWORK_POLICIES:
        failures.append(f"{label}.networkPolicy is invalid")
    if network_policy != "deny" and boundary != "isolated-worker":
        failures.append(f"{label} non-deny network policy requires an isolated-worker boundary")

    resources = require_exact_keys(
        entry.get("resourceCharacteristics"),
        {"cpu", "memory", "temporaryStorage", "output"},
        set(),
        f"{label}.resourceCharacteristics",
        failures,
    )
    for field in ("cpu", "memory", "temporaryStorage", "output"):
        require_string(resources.get(field), f"{label}.resourceCharacteristics.{field}", failures)

    qualification = entry.get("qualificationState")
    if qualification not in EXPECTED_QUALIFICATION_STATES:
        failures.append(f"{label}.qualificationState is invalid")
    owner_issue = entry.get("ownerIssue")
    if not isinstance(owner_issue, int) or isinstance(owner_issue, bool) or owner_issue <= 0:
        failures.append(f"{label}.ownerIssue must be a positive issue number")

    if qualification in {"candidate", "rejected"}:
        failures.append(f"{label} is not admissible in the dependency catalog: {qualification}")
    if qualification == "production-qualified":
        if license_entry.get("reviewStatus") != "accepted":
            failures.append(f"{label} production-qualified dependency lacks accepted license review")
        if advisory.get("status") != "clear":
            failures.append(f"{label} production-qualified dependency lacks a clear advisory snapshot")
        if determinism.get("claim") == "unknown":
            failures.append(f"{label} production-qualified dependency has unknown determinism")

    admission = policy.get("dependencyAdmission", {})
    required_from_policy = admission.get("requiredManifestFields")
    if isinstance(required_from_policy, list):
        for field in required_from_policy:
            if field not in entry:
                failures.append(f"missing required policy field: {label}.{field}")
    return sorted_unique(failures)


def validate_policy_model(policy: Any, root: Path, *, paths: bool) -> list[str]:
    failures: list[str] = []
    entry = require_exact_keys(
        policy,
        {
            "schema",
            "releaseLine",
            "policyRevision",
            "status",
            "decisionIssue",
            "decisionPullRequest",
            "decisionAdr",
            "semanticBaselineImpact",
            "canonicalIdentity",
            "productBoundary",
            "evidenceLanes",
            "isolation",
            "dependencyAdmission",
            "issueHandoff",
            "claims",
        },
        set(),
        "implementationPolicy",
        failures,
    )
    if entry.get("schema") != "fdir/implementation-policy/1":
        failures.append("implementation policy schema mismatch")
    if entry.get("releaseLine") != "2.1.x":
        failures.append("implementation policy release line must be 2.1.x")
    if entry.get("policyRevision") != 1:
        failures.append("implementation policy revision must be 1")
    if entry.get("status") != "frozen":
        failures.append("implementation policy must be frozen")
    if entry.get("decisionIssue") != 32:
        failures.append("implementation policy decision owner must be Issue #32")
    if entry.get("decisionPullRequest") != 39:
        failures.append("implementation policy decision pull request must be #39")
    if entry.get("decisionAdr") != ADR_PATH:
        failures.append(f"implementation policy ADR must be {ADR_PATH}")
    if entry.get("semanticBaselineImpact") != "none":
        failures.append("implementation policy must not change the FDIR 2.1 semantic baseline")

    canonical = require_exact_keys(
        entry.get("canonicalIdentity"),
        {
            "encoding",
            "fdirVersion",
            "authoritySpecification",
            "oracle",
            "vector",
            "forbiddenAuthoritySubstitutions",
            "normativeChangeRequiredForReplacement",
        },
        set(),
        "implementationPolicy.canonicalIdentity",
        failures,
    )
    if canonical.get("encoding") != "canonical-json":
        failures.append("canonical identity authority must be canonical-json")
    if canonical.get("fdirVersion") != "2.1.0":
        failures.append("canonical identity FDIR version must be 2.1.0")
    if canonical.get("authoritySpecification") != "spec/05-canonicalization-and-identity.md":
        failures.append("canonical identity specification path mismatch")
    if canonical.get("oracle") != "tools/canonical_json.py":
        failures.append("canonical identity oracle path mismatch")
    if canonical.get("vector") != "fixtures/canonical/vector.json":
        failures.append("canonical identity vector path mismatch")
    if canonical.get("forbiddenAuthoritySubstitutions") != ["canonical-cbor"]:
        failures.append("canonical authority substitution policy must forbid canonical-cbor")
    if canonical.get("normativeChangeRequiredForReplacement") is not True:
        failures.append("canonical authority replacement must require a normative change")

    boundary = require_exact_keys(
        entry.get("productBoundary"),
        {
            "referenceProductLanguage",
            "rustFirstResponsibilities",
            "verificationOracle",
            "adapterProtocolLanguageNeutral",
            "implementationLanguageGrantsAuthority",
        },
        set(),
        "implementationPolicy.productBoundary",
        failures,
    )
    if boundary.get("referenceProductLanguage") != "rust":
        failures.append("reference product language must be Rust")
    responsibilities = require_string_list(
        boundary.get("rustFirstResponsibilities"),
        "implementationPolicy.productBoundary.rustFirstResponsibilities",
        failures,
        nonempty=True,
        unique=True,
    )
    required_responsibilities = {
        "generated-domain-types",
        "neutral-logical-kernel",
        "canonical-json-and-digests",
        "snapshot-and-content-addressed-storage",
        "adapter-protocol-and-sdk",
        "coordinator-cli-and-public-api",
        "first-party-adapters",
    }
    if not required_responsibilities.issubset(set(responsibilities)):
        failures.append("Rust-first responsibility set is incomplete")
    oracle = require_exact_keys(
        boundary.get("verificationOracle"),
        {
            "implementationLanguage",
            "runtime",
            "thirdPartyPackages",
            "productRuntimeDependency",
            "roles",
            "rewriteForLanguageUniformityForbidden",
        },
        set(),
        "implementationPolicy.productBoundary.verificationOracle",
        failures,
    )
    if oracle.get("implementationLanguage") != "python" or oracle.get("runtime") != "CPython":
        failures.append("verification oracle must remain CPython")
    if oracle.get("thirdPartyPackages") != []:
        failures.append("verification oracle must remain standard-library-only")
    if oracle.get("productRuntimeDependency") is not False:
        failures.append("verification oracle cannot become an undeclared product runtime dependency")
    require_string_list(
        oracle.get("roles"),
        "implementationPolicy.productBoundary.verificationOracle.roles",
        failures,
        nonempty=True,
        unique=True,
    )
    if oracle.get("rewriteForLanguageUniformityForbidden") is not True:
        failures.append("language-uniformity rewrite prohibition is missing")
    if boundary.get("adapterProtocolLanguageNeutral") is not True:
        failures.append("adapter protocol must remain implementation-language-neutral")
    if boundary.get("implementationLanguageGrantsAuthority") is not False:
        failures.append("implementation language must not grant authority")

    lane_entries = entry.get("evidenceLanes")
    if not isinstance(lane_entries, list):
        failures.append("implementationPolicy.evidenceLanes must be a list")
        lane_entries = []
    lane_ids = [item.get("id") for item in lane_entries if isinstance(item, dict)]
    if tuple(lane_ids) != EXPECTED_LANES:
        failures.append(f"evidence lane set/order mismatch: {lane_ids!r}")
    for item in lane_entries:
        if not isinstance(item, dict):
            failures.append("evidence lane entry must be an object")
            continue
        lane_id = item.get("id")
        lane = require_exact_keys(
            item,
            {
                "id",
                "purpose",
                "maySatisfyNativeEvidence",
                "maySatisfyIndependentCensus",
                "mayEmitSemanticCandidates",
                "mayRewriteSourceAuthority",
            },
            set(),
            f"implementationPolicy.evidenceLanes[{lane_id}]",
            failures,
        )
        require_string(lane.get("purpose"), f"evidence lane {lane_id}.purpose", failures)
        for field in (
            "maySatisfyNativeEvidence",
            "maySatisfyIndependentCensus",
            "mayEmitSemanticCandidates",
            "mayRewriteSourceAuthority",
        ):
            require_bool(lane.get(field), f"evidence lane {lane_id}.{field}", failures)
        if lane.get("mayRewriteSourceAuthority") is not False:
            failures.append(f"evidence lane {lane_id} may not rewrite source authority")
        if lane_id == "native-substrate-census":
            if lane.get("maySatisfyNativeEvidence") is not True:
                failures.append("native substrate lane must be able to satisfy native evidence")
            if lane.get("maySatisfyIndependentCensus") is not True:
                failures.append("native substrate lane must be able to satisfy independent census")
        elif lane.get("maySatisfyNativeEvidence") is not False:
            failures.append(f"non-native lane {lane_id} cannot satisfy native evidence")
        if lane_id != "native-substrate-census" and lane.get("maySatisfyIndependentCensus") is not False:
            failures.append(f"non-native lane {lane_id} cannot satisfy independent census")

    isolation = require_exact_keys(
        entry.get("isolation"),
        {
            "defaultNetworkPolicy",
            "opaqueArtifactOrObjectHandlesRequired",
            "ambientCredentialsForbidden",
            "arbitraryHostPathsForbidden",
            "arbitraryCommandExecutionForbidden",
            "isolatedWorkerRequiredWhen",
            "inProcessExceptionRequirements",
            "boundaryOwnerIssue",
            "securityQualificationOwnerIssue",
        },
        set(),
        "implementationPolicy.isolation",
        failures,
    )
    expected_isolation_values = {
        "defaultNetworkPolicy": "deny",
        "opaqueArtifactOrObjectHandlesRequired": True,
        "ambientCredentialsForbidden": True,
        "arbitraryHostPathsForbidden": True,
        "arbitraryCommandExecutionForbidden": True,
        "boundaryOwnerIssue": 12,
        "securityQualificationOwnerIssue": 23,
    }
    for field, expected in expected_isolation_values.items():
        if isolation.get(field) != expected:
            failures.append(f"implementation isolation policy mismatch: {field}")
    required_when = isolation.get("isolatedWorkerRequiredWhen")
    if required_when != {
        "nonRustReceivesUntrustedDocumentBytes": True,
        "unsafeOrFfiOrNativeReceivesUntrustedDocumentBytes": True,
    }:
        failures.append("isolated-worker trigger policy mismatch")
    if isolation.get("inProcessExceptionRequirements") != [
        "accepted-dedicated-adr",
        "threat-analysis",
        "bounded-input-contract",
        "exact-build-qualification-evidence",
    ]:
        failures.append("in-process exception evidence set mismatch")

    admission = require_exact_keys(
        entry.get("dependencyAdmission"),
        {
            "manifestSchema",
            "catalog",
            "candidateAssessments",
            "issueTemplate",
            "floatingVersionsForbidden",
            "undeclaredDependenciesForbidden",
            "unqualifiedProductionDependencyForbidden",
            "parserUpgradeMayShrinkNativeInventory",
            "requiredManifestFields",
            "admissionOwnerIssue",
            "conformanceOwnerIssue",
        },
        set(),
        "implementationPolicy.dependencyAdmission",
        failures,
    )
    expected_paths = {
        "manifestSchema": SCHEMA_PATH,
        "catalog": CATALOG_PATH,
        "candidateAssessments": ASSESSMENTS_PATH,
        "issueTemplate": ISSUE_TEMPLATE_PATH,
    }
    for field, expected in expected_paths.items():
        if admission.get(field) != expected:
            failures.append(f"dependency admission path mismatch: {field}")
    expected_flags = {
        "floatingVersionsForbidden": True,
        "undeclaredDependenciesForbidden": True,
        "unqualifiedProductionDependencyForbidden": True,
        "parserUpgradeMayShrinkNativeInventory": False,
        "admissionOwnerIssue": 32,
        "conformanceOwnerIssue": 33,
    }
    for field, expected in expected_flags.items():
        if admission.get(field) != expected:
            failures.append(f"dependency admission policy mismatch: {field}")
    if tuple(admission.get("requiredManifestFields", [])) != EXPECTED_REQUIRED_FIELDS:
        failures.append("dependency required-manifest field set/order mismatch")

    handoff = require_exact_keys(
        entry.get("issueHandoff"),
        {
            "umbrellaIssue",
            "roadmapIssue",
            "foundationDecisionIssue",
            "firstProductImplementationIssue",
            "prerequisiteIssues",
            "qualityCommand",
            "policyCommand",
            "handoffDocument",
        },
        set(),
        "implementationPolicy.issueHandoff",
        failures,
    )
    expected_handoff = {
        "umbrellaIssue": 1,
        "roadmapIssue": 4,
        "foundationDecisionIssue": 32,
        "firstProductImplementationIssue": 7,
        "prerequisiteIssues": [2, 3, 5, 6, 32],
        "qualityCommand": "python3 tools/quality.py --mode full --cache-policy off .",
        "policyCommand": "python3 tools/validate_implementation_policy.py --check --self-test --json .",
        "handoffDocument": HANDOFF_PATH,
    }
    for field, expected in expected_handoff.items():
        if handoff.get(field) != expected:
            failures.append(f"issue handoff policy mismatch: {field}")

    claims = require_exact_keys(
        entry.get("claims"),
        {"productCapabilityAdded", "productionReady", "qualificationState"},
        set(),
        "implementationPolicy.claims",
        failures,
    )
    if claims.get("productCapabilityAdded") is not False:
        failures.append("foundation policy cannot add a product capability claim")
    if claims.get("productionReady") is not False:
        failures.append("foundation policy cannot claim production readiness")
    if claims.get("qualificationState") != "development-unqualified":
        failures.append("foundation qualification state must be development-unqualified")

    if paths:
        path_values = [
            entry.get("decisionAdr"),
            canonical.get("authoritySpecification"),
            canonical.get("oracle"),
            canonical.get("vector"),
            admission.get("manifestSchema"),
            admission.get("catalog"),
            admission.get("candidateAssessments"),
            admission.get("issueTemplate"),
            handoff.get("handoffDocument"),
        ]
        for relative in path_values:
            if isinstance(relative, str) and not (root / relative).is_file():
                failures.append(f"implementation policy references missing file: {relative}")
    return sorted_unique(failures)


def validate_schema(schema: Any, policy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not isinstance(schema, dict):
        return ["dependency manifest schema must be an object"]
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        failures.append("dependency manifest schema dialect mismatch")
    if schema.get("$id") != "urn:fdir:dependency-manifest:1":
        failures.append("dependency manifest schema identifier mismatch")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        failures.append("dependency manifest schema must be a closed object")
    required = schema.get("required")
    expected_required = policy.get("dependencyAdmission", {}).get("requiredManifestFields")
    if required != expected_required or tuple(required or []) != EXPECTED_REQUIRED_FIELDS:
        failures.append("dependency manifest schema required fields do not match policy")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return sorted_unique([*failures, "dependency manifest schema properties are missing"])
    if properties.get("schema", {}).get("const") != "fdir/dependency-manifest/1":
        failures.append("dependency manifest record schema const mismatch")
    if tuple(properties.get("kind", {}).get("enum", [])) != EXPECTED_KINDS:
        failures.append("dependency manifest kind enumeration mismatch")
    lane_enum = properties.get("evidenceLanes", {}).get("items", {}).get("enum", [])
    if tuple(lane_enum) != EXPECTED_LANES:
        failures.append("dependency manifest evidence-lane enumeration mismatch")
    if tuple(properties.get("processBoundary", {}).get("enum", [])) != EXPECTED_BOUNDARIES:
        failures.append("dependency manifest process-boundary enumeration mismatch")
    if tuple(properties.get("networkPolicy", {}).get("enum", [])) != EXPECTED_NETWORK_POLICIES:
        failures.append("dependency manifest network-policy enumeration mismatch")
    if tuple(properties.get("qualificationState", {}).get("enum", [])) != EXPECTED_QUALIFICATION_STATES:
        failures.append("dependency manifest qualification-state enumeration mismatch")
    for field in EXPECTED_REQUIRED_FIELDS:
        if field not in properties:
            failures.append(f"dependency manifest schema lacks property: {field}")
    for field, nested_required in {
        "inProcessException": {"adr", "threatAnalysis", "boundedInputContract", "qualificationEvidence"},
        "license": {"spdx", "reviewStatus"},
        "advisorySnapshot": {"asOf", "source", "status"},
        "determinism": {"claim", "nondeterminismRecorded"},
        "resourceCharacteristics": {"cpu", "memory", "temporaryStorage", "output"},
    }.items():
        nested = properties.get(field, {})
        if nested.get("type") != "object" or nested.get("additionalProperties") is not False:
            failures.append(f"dependency manifest schema must close object: {field}")
        if set(nested.get("required", [])) != nested_required:
            failures.append(f"dependency manifest schema nested required fields mismatch: {field}")
    return sorted_unique(failures)


def validate_catalog(
    catalog: Any,
    schema: dict[str, Any],
    policy: dict[str, Any],
    *,
    root: Path | None = None,
) -> list[str]:
    failures: list[str] = []
    entry = require_exact_keys(
        catalog,
        {
            "schema",
            "releaseLine",
            "policyRevision",
            "state",
            "dependencies",
            "candidateAssessmentPath",
            "admissionRule",
            "productionCapabilityClaim",
        },
        set(),
        "dependencyCatalog",
        failures,
    )
    if entry.get("schema") != "fdir/dependency-catalog/1":
        failures.append("dependency catalog schema mismatch")
    if entry.get("releaseLine") != "2.1.x":
        failures.append("dependency catalog release line mismatch")
    if entry.get("policyRevision") != policy.get("policyRevision"):
        failures.append("dependency catalog policy revision mismatch")
    require_string(entry.get("state"), "dependencyCatalog.state", failures)
    if entry.get("candidateAssessmentPath") != ASSESSMENTS_PATH:
        failures.append("dependency catalog candidate-assessment path mismatch")
    require_string(entry.get("admissionRule"), "dependencyCatalog.admissionRule", failures)
    if entry.get("productionCapabilityClaim") is not False:
        failures.append("dependency catalog cannot create a production capability claim")
    dependencies = entry.get("dependencies")
    if not isinstance(dependencies, list):
        failures.append("dependencyCatalog.dependencies must be a list")
        dependencies = []
    ids: list[str] = []
    for index, dependency in enumerate(dependencies):
        label = f"dependencyCatalog.dependencies[{index}]"
        failures.extend(
            validate_manifest(dependency, schema, policy, label, root=root)
        )
        if isinstance(dependency, dict) and isinstance(dependency.get("id"), str):
            ids.append(dependency["id"])
    for identifier in duplicates(ids):
        failures.append(f"duplicate dependency catalog identifier: {identifier}")
    if not dependencies and entry.get("state") != "foundation-no-runtime-dependencies-admitted":
        failures.append("empty dependency catalog must declare the foundation-no-runtime-dependencies-admitted state")
    return sorted_unique(failures)


def validate_repository_alignment(root: Path) -> list[str]:
    failures: list[str] = []
    try:
        claim = load_json(root / "release/claim-manifest.yaml")
        trace = load_json(root / "release/traceability.yaml")
    except (OSError, json.JSONDecodeError) as error:
        return [f"cannot load release alignment authority: {error}"]

    functions = claim.get("releaseFunctions", [])
    function_ids = {
        item.get("id")
        for item in functions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if "canonical-json-and-identity" not in function_ids:
        failures.append("release function registry lacks canonical-json-and-identity")
    if "canonical-cbor-and-identity" in function_ids:
        failures.append("release function registry contains stale canonical-cbor-and-identity")
    policy_function = next(
        (
            item
            for item in functions
            if isinstance(item, dict)
            and item.get("id") == "implementation-boundary-and-dependency-governance"
        ),
        None,
    )
    if not isinstance(policy_function, dict):
        failures.append("release function registry lacks implementation-boundary-and-dependency-governance")
    else:
        if policy_function.get("ownerIssues") != [32]:
            failures.append("implementation-boundary release function must be owned by Issue #32")
        if policy_function.get("state") != "implemented-unqualified":
            failures.append("implementation-boundary release function must be implemented-unqualified")
        if policy_function.get("productionReady") is not False:
            failures.append("implementation-boundary release function cannot be productionReady")
        required = set(policy_function.get("requiredRequirementIds", []))
        if not POLICY_REQUIREMENT_IDS.issubset(required):
            failures.append("implementation-boundary release function requirement mapping is incomplete")

    issue_registry = claim.get("issueRegistry", [])
    issues = {
        item.get("issue"): item
        for item in issue_registry
        if isinstance(item, dict) and isinstance(item.get("issue"), int)
    }
    for issue in range(32, 39):
        if issue not in issues:
            failures.append(f"release issue registry lacks Issue #{issue}")
    issue9 = issues.get(9, {})
    if "canonical JSON" not in str(issue9.get("role", "")):
        failures.append("Issue #9 release role must name canonical JSON")

    adr_entry = next(
        (
            item
            for item in trace.get("adrs", [])
            if isinstance(item, dict) and item.get("path") == ADR_PATH
        ),
        None,
    )
    if not isinstance(adr_entry, dict):
        failures.append("release traceability lacks ADR 0004")
    else:
        if 32 not in adr_entry.get("ownerIssues", []):
            failures.append("ADR 0004 traceability must be owned by Issue #32")
        if set(adr_entry.get("requirementIds", [])) != POLICY_REQUIREMENT_IDS:
            failures.append("ADR 0004 requirement mapping is incomplete or excessive")

    traced_requirements = {
        item.get("requirementId"): item
        for item in trace.get("requirements", [])
        if isinstance(item, dict) and isinstance(item.get("requirementId"), str)
    }
    for requirement_id in POLICY_REQUIREMENT_IDS:
        item = traced_requirements.get(requirement_id)
        if not isinstance(item, dict) or ADR_PATH not in item.get("adrPaths", []):
            failures.append(f"release traceability does not map ADR 0004 to {requirement_id}")

    artifacts = {
        item.get("path"): item
        for item in trace.get("releaseArtifacts", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    expected_artifacts = {
        POLICY_PATH: {32},
        SCHEMA_PATH: {32, 33},
        CATALOG_PATH: {32, 33},
        "tools/validate_implementation_policy.py": {32, 33},
        HANDOFF_PATH: {7, 32},
    }
    for path, expected_owners in expected_artifacts.items():
        item = artifacts.get(path)
        if not isinstance(item, dict):
            failures.append(f"release artifact registry lacks {path}")
            continue
        if not expected_owners.issubset(set(item.get("ownerIssues", []))):
            failures.append(f"release artifact owner mapping is incomplete: {path}")

    scan_paths = (
        "release/claim-manifest.yaml",
        "release/development-handoff.md",
        "README.md",
        "DEVELOPMENT.md",
        "CONTRIBUTING.md",
        "quality/README.md",
        "matrices/release-claims.md",
        "matrices/release-traceability.md",
        "matrices/release-traceability.csv",
    )
    for relative in scan_paths:
        path = root / relative
        if not path.is_file():
            failures.append(f"missing policy-alignment document: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in STALE_AUTHORITY_PATTERNS:
            if pattern.search(text):
                failures.append(f"stale canonical-CBOR authority wording: {relative}")
                break
    return sorted_unique(failures)


def validate_document_contracts(root: Path) -> list[str]:
    failures: list[str] = []
    token_sets = {
        ADR_PATH: (
            "Status:** Accepted",
            "Issue #32",
            "canonical JSON",
            "native-substrate-census",
            "semantic-helper",
            "isolated-worker",
            "Issue #7",
            "Issue #33",
        ),
        ASSESSMENTS_PATH: (
            "No product runtime dependency is admitted",
            "native-substrate-census",
            "semantic-helper",
            "renderer-observation",
            "ocr-inference-observation",
            "storage-codec",
            "Issue #33",
        ),
        ISSUE_TEMPLATE_PATH: (
            "Exact version",
            "Evidence lanes",
            "Process boundary",
            "Normalization",
            "No production capability claim",
        ),
        HANDOFF_PATH: (
            "Issue #7",
            "Issue #32",
            "quality.py --mode full",
            "validate_implementation_policy.py",
            "No product capability",
            "Definition of Done",
        ),
    }
    for relative, tokens in token_sets.items():
        path = root / relative
        if not path.is_file():
            failures.append(f"missing implementation-policy document: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for token in tokens:
            if token not in text:
                failures.append(f"implementation-policy document lacks token: {relative} -> {token}")
    return sorted_unique(failures)


def load_models(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        load_json(root / POLICY_PATH),
        load_json(root / SCHEMA_PATH),
        load_json(root / CATALOG_PATH),
    )


def validate_models(
    root: Path,
    policy: dict[str, Any],
    schema: dict[str, Any],
    catalog: dict[str, Any],
    *,
    paths: bool,
    alignment: bool,
) -> list[str]:
    failures: list[str] = []
    failures.extend(validate_policy_model(policy, root, paths=paths))
    failures.extend(validate_schema(schema, policy))
    failures.extend(validate_catalog(catalog, schema, policy, root=root if paths else None))
    if paths:
        failures.extend(validate_document_contracts(root))
    if alignment:
        failures.extend(validate_repository_alignment(root))
    return sorted_unique(failures)


def validate_repository(root: Path) -> list[str]:
    required = (
        POLICY_PATH,
        SCHEMA_PATH,
        CATALOG_PATH,
        ADR_PATH,
        ASSESSMENTS_PATH,
        ISSUE_TEMPLATE_PATH,
        HANDOFF_PATH,
        "release/claim-manifest.yaml",
        "release/traceability.yaml",
    )
    missing = [relative for relative in required if not (root / relative).is_file()]
    if missing:
        return [f"missing implementation-policy input: {relative}" for relative in missing]
    try:
        policy, schema, catalog = load_models(root)
        return validate_models(
            root,
            policy,
            schema,
            catalog,
            paths=True,
            alignment=True,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return [f"cannot validate implementation policy: {error}"]


def valid_test_manifest() -> dict[str, Any]:
    return {
        "schema": "fdir/dependency-manifest/1",
        "id": "self-test-parser",
        "name": "Self-test parser",
        "kind": "parser",
        "version": "1.2.3",
        "features": ["strict"],
        "implementationLanguage": "Rust",
        "evidenceLanes": ["native-substrate-census"],
        "inputKinds": ["application/x-fdir-self-test"],
        "outputKinds": ["native-inventory"],
        "normalizations": [],
        "unavailableSourceDistinctions": [],
        "unsafeCode": False,
        "ffi": False,
        "nativeCode": False,
        "receivesUntrustedDocumentBytes": True,
        "processBoundary": "isolated-worker",
        "nativeAuthority": True,
        "independentCensus": True,
        "license": {"spdx": "Apache-2.0", "reviewStatus": "accepted"},
        "advisorySnapshot": {
            "asOf": "2026-08-19",
            "source": "self-test",
            "status": "clear",
        },
        "determinism": {
            "claim": "deterministic",
            "nondeterminismRecorded": False,
        },
        "networkPolicy": "deny",
        "resourceCharacteristics": {
            "cpu": "bounded by worker policy",
            "memory": "bounded by worker policy",
            "temporaryStorage": "bounded by worker policy",
            "output": "bounded by worker policy",
        },
        "qualificationState": "admitted-unqualified",
        "ownerIssue": 33,
    }


def self_tests(root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        source_policy, source_schema, source_catalog = load_models(root)
    except (OSError, json.JSONDecodeError) as error:
        return [f"self-test setup failed: {error}"], []
    cases: list[dict[str, Any]] = []
    failures: list[str] = []

    def expect(
        name: str,
        mutate: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], None],
        fragment: str,
    ) -> None:
        policy = copy.deepcopy(source_policy)
        schema = copy.deepcopy(source_schema)
        catalog = copy.deepcopy(source_catalog)
        catalog["state"] = "admitted-dependencies"
        catalog["dependencies"] = [valid_test_manifest()]
        mutate(policy, schema, catalog)
        result = validate_models(
            root,
            policy,
            schema,
            catalog,
            paths=False,
            alignment=False,
        )
        detected = any(fragment.lower() in item.lower() for item in result)
        cases.append(
            {
                "id": name,
                "expectedFragment": fragment,
                "status": "detected" if detected else "missed",
                "diagnostics": result,
            }
        )
        if not detected:
            failures.append(f"implementation-policy self-test missed {name}: {fragment}")

    expect(
        "unknown-evidence-lane",
        lambda _policy, _schema, catalog: catalog["dependencies"][0]["evidenceLanes"].append(
            "imaginary-authority"
        ),
        "unknown lane",
    )
    expect(
        "floating-version",
        lambda _policy, _schema, catalog: catalog["dependencies"][0].update(
            {"version": "latest"}
        ),
        "exact immutable version",
    )
    expect(
        "semantic-helper-native-authority",
        lambda _policy, _schema, catalog: catalog["dependencies"][0].update(
            {
                "evidenceLanes": ["semantic-helper"],
                "nativeAuthority": True,
                "independentCensus": True,
            }
        ),
        "semantic-helper output cannot claim native authority",
    )
    expect(
        "unsafe-untrusted-in-process",
        lambda _policy, _schema, catalog: catalog["dependencies"][0].update(
            {"unsafeCode": True, "processBoundary": "in-process"}
        ),
        "unsafe/FFI/native",
    )
    expect(
        "non-rust-untrusted-in-process",
        lambda _policy, _schema, catalog: catalog["dependencies"][0].update(
            {
                "implementationLanguage": "Python",
                "unsafeCode": False,
                "processBoundary": "in-process",
            }
        ),
        "non-Rust document worker",
    )
    expect(
        "missing-manifest-field",
        lambda _policy, _schema, catalog: catalog["dependencies"][0].pop("license"),
        "missing required field",
    )
    expect(
        "canonical-authority-drift",
        lambda policy, _schema, _catalog: policy["canonicalIdentity"].update(
            {"encoding": "canonical-cbor"}
        ),
        "canonical identity authority must be canonical-json",
    )
    expect(
        "decision-owner-drift",
        lambda policy, _schema, _catalog: policy.update({"decisionIssue": 999}),
        "Issue #32",
    )
    expect(
        "foundation-production-claim",
        lambda policy, _schema, _catalog: policy["claims"].update(
            {"productionReady": True}
        ),
        "cannot claim production readiness",
    )
    return sorted_unique(failures), cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate repository policy, catalog, documents, and release alignment",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="prove that representative policy violations fail closed",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON receipt")
    arguments = parser.parse_args(argv)
    root = Path(arguments.root).resolve()
    run_check = arguments.check or not arguments.self_test

    policy_failures = validate_repository(root) if run_check else []
    self_test_failures: list[str] = []
    self_test_cases: list[dict[str, Any]] = []
    if arguments.self_test:
        self_test_failures, self_test_cases = self_tests(root)
    failures = sorted_unique([*policy_failures, *self_test_failures])
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "passed" if not failures else "failed",
        "policyRevision": 1,
        "decisionIssue": 32,
        "checkedRepository": run_check,
        "selfTested": arguments.self_test,
        "failureCount": len(failures),
        "failures": failures,
        "selfTests": self_test_cases,
    }
    if arguments.json:
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    elif failures:
        print("FDIR implementation policy validation failed:")
        for failure in failures:
            print(f"- {failure}")
    else:
        print(
            "FDIR implementation policy validation passed: "
            f"self-tests={len(self_test_cases)}, decision=#32"
        )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
