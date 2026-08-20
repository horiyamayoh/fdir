"""Run the four bounded, format-specific qualification profiles.

Every case begins with a generated source document, then goes through the
public converter and the independent source oracle.  The report names the
qualified lanes and the residual policy so a green run cannot be confused
with a claim of complete external-format conformance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

try:
    from convert_document import convert_path
    from generate_e2e_fixtures import write_fixtures
    from independent_oracle import compare_source_to_document, source_oracle
    from ir_validation import validate_document
except ImportError:  # pragma: no cover
    from tools.convert_document import convert_path
    from tools.generate_e2e_fixtures import write_fixtures
    from tools.independent_oracle import compare_source_to_document, source_oracle
    from tools.ir_validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "machine" / "format-qualified-profiles.json"


class FormatQualificationFailure(AssertionError):
    pass


def _all_strings(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {_all_strings(child)}" for key, child in value.items())
    if isinstance(value, list):
        return " ".join(_all_strings(child) for child in value)
    return str(value)


def _run_profile(profile: dict[str, Any], fixture_paths: dict[str, Path], workspace: Path) -> dict[str, Any]:
    format_name = str(profile["format"])
    valid_path = fixture_paths[format_name]
    unsupported_path = fixture_paths[f"unsupported_{format_name}"]
    document, evidence = convert_path(valid_path, format_name)
    validate_document(document)
    if document.get("conversion", {}).get("status") not in {"complete", "partial"}:
        raise FormatQualificationFailure(f"{format_name} valid profile failed")
    actual_kinds = sorted({str(item.get("kind")) for item in document.get("nodes", []) if isinstance(item, dict)})
    missing_kinds = sorted(set(profile["requiredNodeKinds"]) - set(actual_kinds))
    if missing_kinds:
        raise FormatQualificationFailure(f"{format_name} missing qualified node kinds: {missing_kinds}")
    oracle_report = compare_source_to_document(source_oracle(valid_path, format_name), document, tuple(profile.get("expectedTokens", [])))
    non_preserved = [item for item in document.get("conversion", {}).get("features", []) if isinstance(item, dict) and item.get("status") in {"approximated", "ambiguous", "unsupported", "omitted-by-policy", "failed"}]
    missing_diagnostics = [item.get("feature") for item in non_preserved if not item.get("diagnosticIds")]
    if missing_diagnostics:
        raise FormatQualificationFailure(f"{format_name} non-preserved features lack diagnostics: {missing_diagnostics}")

    unsupported, unsupported_evidence = convert_path(unsupported_path, format_name)
    validate_document(unsupported)
    unsupported_features = [item for item in unsupported.get("conversion", {}).get("features", []) if isinstance(item, dict) and item.get("status") == "unsupported"]
    if unsupported.get("conversion", {}).get("status") != "partial" or not unsupported_features or not unsupported.get("diagnostics"):
        raise FormatQualificationFailure(f"{format_name} unsupported profile did not fail closed")
    return {
        "issueNumber": profile["issueNumber"],
        "profileId": profile["id"],
        "format": format_name,
        "status": "passed",
        "claimMode": "bounded-qualified-profile",
        "source": {"path": str(valid_path), "sha256": evidence.get("input", {}).get("sha256"), "consumed": evidence.get("input", {}).get("consumed")},
        "validConversionStatus": document.get("conversion", {}).get("status"),
        "unsupportedConversionStatus": unsupported.get("conversion", {}).get("status"),
        "nodeKinds": actual_kinds,
        "qualifiedLanes": profile["requiredLanes"],
        "oracle": oracle_report,
        "unsupportedFeatureCount": len(unsupported_features),
        "diagnosticCount": len(document.get("diagnostics", [])) + len(unsupported.get("diagnostics", [])),
        "residualPolicy": profile["residualPolicy"],
        "residuals": [item.get("feature") for item in non_preserved],
    }


def run(format_filter: str | None = None) -> dict[str, Any]:
    catalog = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profiles = catalog.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 4:
        raise FormatQualificationFailure("format profile catalog must contain exactly four profiles")
    if format_filter is not None:
        profiles = [profile for profile in profiles if profile.get("format") == format_filter]
        if len(profiles) != 1:
            raise FormatQualificationFailure(f"unknown format profile: {format_filter}")
    workspace = ROOT / "e2e" / ".run" / f"format-qualification-{os.getpid()}"
    workspace.mkdir(parents=True, exist_ok=True)
    fixture_paths = write_fixtures(workspace / "fixtures")
    reports = [_run_profile(profile, fixture_paths, workspace) for profile in profiles]
    return {
        "schema": "fdir/format-qualification-report",
        "version": "1.0.0",
        "status": "passed",
        "claimMode": "bounded-qualified-profile",
        "profiles": reports,
        "issueNumbers": [report["issueNumber"] for report in reports],
        "formats": [report["format"] for report in reports],
        "residuals": [
            {"issueNumber": report["issueNumber"], "format": report["format"], "policy": report["residualPolicy"], "features": report["residuals"]}
            for report in reports
        ],
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["docx", "xlsx", "pdf", "markdown"])
    args = parser.parse_args()
    try:
        report = run(args.format)
    except Exception as exc:
        report = {"schema": "fdir/format-qualification-report", "version": "1.0.0", "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
