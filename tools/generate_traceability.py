#!/usr/bin/env python3
"""Generate deterministic requirement and release traceability projections."""
from __future__ import annotations

import argparse
import csv
import io
import importlib.util
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def import_tool(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def issue_list(values: list[int]) -> str:
    return ", ".join(f"#{value}" for value in values)


def issue_csv(values: list[int]) -> str:
    return ";".join(str(value) for value in values)


def outputs(root: Path) -> dict[str, str]:
    requirements = load(root / "machine/requirements.yaml")["requirements"]
    tests = load(root / "machine/acceptance-tests.yaml")["tests"]
    by_requirement: dict[str, list[str]] = {item["id"]: [] for item in requirements}
    for test in tests:
        for requirement in test["requirements"]:
            by_requirement.setdefault(requirement, []).append(test["id"])

    requirements_md = [
        "# Requirement / acceptance-test traceability",
        "",
        "> Generated; do not edit manually.",
        "",
        "| Requirement | Level | Acceptance tests | Text |",
        "|---|---|---|---|",
    ]
    for item in requirements:
        test_ids = ", ".join(f"`{test}`" for test in sorted(by_requirement[item["id"]]))
        requirements_md.append(
            f"| `{item['id']}` | {item['level']} | {test_ids} | {item['text']} |"
        )
    requirements_md_text = "\n".join(requirements_md) + "\n"

    requirements_csv_buffer = io.StringIO(newline="")
    requirements_csv = csv.writer(requirements_csv_buffer, lineterminator="\n")
    requirements_csv.writerow(["requirement_id", "level", "acceptance_tests", "text"])
    for item in requirements:
        requirements_csv.writerow(
            [
                item["id"],
                item["level"],
                ";".join(sorted(by_requirement[item["id"]])),
                item["text"],
            ]
        )

    claim_manifest = load(root / "release/claim-manifest.yaml")
    claims_md = [
        "# FDIR 2.1 release claims",
        "",
        "> Generated from `release/claim-manifest.yaml`; do not edit manually.",
        "",
        "| Claim tuple | Format | Capability | Profile | State | Implementation owners | Positive evidence | Negative evidence | Ambiguous / partial evidence | Qualification report |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for claim in claim_manifest["formatTuples"]:
        claims_md.append(
            "| "
            f"`{claim['id']}` | {claim['format']} | {claim['capability']} | "
            f"`{claim['profile']}` | `{claim['state']}` | "
            f"{issue_list(claim['implementationOwnerIssues'])} | "
            f"{issue_list(claim['positiveEvidenceOwnerIssues'])} | "
            f"{issue_list(claim['negativeEvidenceOwnerIssues'])} | "
            f"{issue_list(claim['ambiguousPartialEvidenceOwnerIssues'])} | "
            f"{issue_list(claim['qualificationReportOwnerIssues'])} |"
        )
    claims_md_text = "\n".join(claims_md) + "\n"

    traceability = load(root / "release/traceability.yaml")
    release_md = [
        "# End-to-end release traceability",
        "",
        "> Generated from `release/traceability.yaml`; do not edit manually.",
        "",
        "| Requirement | Implementation owners | Verification owners | Qualification owners | Acceptance tests | State |",
        "|---|---|---|---|---|---|",
    ]
    for item in traceability["requirements"]:
        test_ids = ", ".join(f"`{test_id}`" for test_id in item["acceptanceTestIds"])
        release_md.append(
            "| "
            f"`{item['requirementId']}` | {issue_list(item['implementationOwnerIssues'])} | "
            f"{issue_list(item['verificationOwnerIssues'])} | "
            f"{issue_list(item['qualificationOwnerIssues'])} | {test_ids} | "
            f"`{item['completionState']}` |"
        )
    release_md_text = "\n".join(release_md) + "\n"

    release_csv_buffer = io.StringIO(newline="")
    release_csv = csv.writer(release_csv_buffer, lineterminator="\n")
    release_csv.writerow(
        [
            "requirement_id",
            "implementation_owner_issues",
            "verification_owner_issues",
            "qualification_owner_issues",
            "acceptance_test_ids",
            "completion_state",
            "evidence_paths",
        ]
    )
    for item in traceability["requirements"]:
        release_csv.writerow(
            [
                item["requirementId"],
                issue_csv(item["implementationOwnerIssues"]),
                issue_csv(item["verificationOwnerIssues"]),
                issue_csv(item["qualificationOwnerIssues"]),
                ";".join(item["acceptanceTestIds"]),
                item["completionState"],
                ";".join(item["evidencePaths"]),
            ]
        )

    return {
        "matrices/requirements-tests.md": requirements_md_text,
        "matrices/requirements-tests.csv": requirements_csv_buffer.getvalue(),
        "matrices/release-claims.md": claims_md_text,
        "matrices/release-traceability.md": release_md_text,
        "matrices/release-traceability.csv": release_csv_buffer.getvalue(),
    }


def run(root: Path, check: bool) -> int:
    failures = []
    for relative, content in outputs(root).items():
        path = root / relative
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                failures.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if failures:
        print("traceability mismatch:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if check:
        validator = import_tool(
            root / "tools/validate_release_traceability.py",
            "fdir_validate_release_traceability",
        )
        release_failures = validator.validate_repository(root, generated=False)
        self_test_failures = validator.self_tests(root)
        if release_failures or self_test_failures:
            print("release traceability validation failed:")
            for failure in release_failures + self_test_failures:
                print(f"  - {failure}")
            return 1
    print("traceability: ok" if check else "traceability: written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    return run(Path(args.root).resolve(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
