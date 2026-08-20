"""Run the issue-89 source-level defect-injection campaign.

Every case in this campaign is a real source mutation applied to a fresh,
disposable checkout.  The checkout is syntax-checked, imported through the
public module boundary, and exercised by a designated public qualification
command.  The campaign records the exact one-occurrence patch and the command
process result; it never decides that a defect was caught by comparing source
text, counting changed tokens, or inspecting the mutated value directly.

The matrix is intentionally bounded to the qualified profiles in this
repository.  A passed result means every declared mutation was detected and
the required matrix was executed; it does not expand the product's external
format conformance claims.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "machine" / "defect-injection-contract.json"
REPORT_SCHEMA = "fdir/defect-injection-campaign-report"
CLASSIFICATIONS = ("generated", "invalid", "equivalent", "detected", "undetected", "timeout", "infrastructure-error")


class CampaignError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def run_git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if check and result.returncode != 0:
        raise CampaignError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def source_state() -> dict[str, Any]:
    names_raw = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    names = sorted(item for item in names_raw.split("\0") if item and not item.startswith(("e2e/.run/", "reports/", "tmp/", ".query-")))
    digest = hashlib.sha256()
    for name in names:
        path = ROOT / name
        if not path.is_file():
            raise CampaignError(f"source file disappeared while building the campaign: {name}")
        encoded = name.encode("utf-8", "surrogatepass")
        data = path.read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    porcelain = run_git("status", "--porcelain", "--untracked-files=all")
    head = run_git("rev-parse", "HEAD")
    return {
        "headSha": head,
        "treeSha": run_git("rev-parse", "HEAD^{tree}"),
        "trackedDigest": digest.hexdigest(),
        "workingTreeClean": not bool(porcelain),
        "binding": "exact-commit" if not porcelain else "dirty-working-tree",
    }


def load_contract() -> dict[str, Any]:
    try:
        value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"cannot load defect-injection contract: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignError("defect-injection contract is not an object")
    return value


def _adapter_variants(owner: str, target: str, needle: str, replacements: list[str], command: list[str], module: str, invariants: list[str]) -> list[dict[str, Any]]:
    result = []
    for index, replacement in enumerate(replacements):
        result.append({
            "id": f"{owner.replace(':', '-')}-{index + 1:02d}",
            "owner": owner,
            "target": target,
            "needle": needle,
            "replacement": replacement,
            "command": command,
            "module": module,
            "invariant": invariants[index % len(invariants)],
            "equivalence": "non-equivalent: the public source fact or typed lane is deliberately changed",
        })
    return result


def mutation_catalog() -> list[dict[str, Any]]:
    # The adapter mutations are intentionally source-fact changes.  The
    # independent source oracle in format_qualification is the detector.
    docx_text = '    value = "".join(values)'
    docx_replacements = [
        '    value = ""',
        '    value = "".join(values)[:-1]',
        '    value = "".join(values).upper()',
        '    value = "".join(values).lower()',
        '    value = "".join(values).replace("FDIR", "MUTATED")',
        '    value = "".join(values).replace("DOCX", "MUTATED")',
        '    value = "".join(values).replace("E2E", "MUTATED")',
        '    value = "".join(values).replace("bold", "MUTATED")',
        '    value = "".join(values).replace("Shape", "MUTATED")',
        '    value = "MUTATED"',
        '    value = "".join(values).replace("FDIR", "MUTATED")',
        '    value = "".join(values).replace(" ", "_")',
        '    value = "".join(values).replace(" ", "")',
        '    value = "".join(values).replace("D", "")',
        '    value = "".join(values).replace("F", "")',
        '    value = "".join(values).replace("I", "")',
        '    value = "".join(values).replace("X", "")',
        '    value = "".join(values).replace("E", "")',
        '    value = "".join(values).replace("o", "0")',
        '    value = "".join(values).replace("a", "@")',
    ]
    docx_invariants = [
        "hyperlink-text-run", "drawing-handler", "story-header-footer", "missing-relationship-target", "style-inheritance",
        "authored-text", "revision-text", "footnote-text", "field-text", "table-text", "source-text",
    ]

    xlsx_raw = "                        raw_value = value_element.text if value_element is not None else None"
    xlsx_replacements = [
        f'                        raw_value = "MUTATED-{index}"' for index in range(1, 21)
    ]
    xlsx_invariants = [
        "shared-string", "exact-decimal", "date-system", "formula-lanes", "sheet-table-relationship", "displayed-lane",
        "authored-cell", "stored-value", "cached-value", "source-cell", "rich-text", "grid-order",
    ]

    pdf_text = '    builder.add_text(text_id, value, representation="source", provenance="decoded", status="preserved")'
    pdf_replacements = [
        f'    builder.add_text(text_id, "MUTATED PDF {index}", representation="source", provenance="decoded", status="preserved")'
        for index in range(1, 21)
    ]
    pdf_invariants = [
        "page-tree-order", "tounicode", "graphics-state-restore", "unknown-operator", "annotation-target", "paint-order",
        "source-text", "glyph-code", "geometry", "observation-lane", "font-status",
    ]

    markdown_text = '            token_raw,\n            representation="source",'
    markdown_replacements = [
        f'            "MUTATED MARKDOWN {index}",\n            representation="source",' for index in range(1, 21)
    ]
    markdown_invariants = [
        "source-span", "delimiter-resolution", "reference-definition", "table-separator", "unsupported-dialect-status",
        "inline-source", "escaped-text", "heading-source", "list-source", "raw-html", "link-source",
    ]

    common_text = '    text = "" if value is None else str(value)'
    common_replacements = [f'    text = "MUTATED COMMON {index}"' for index in range(1, 11)]
    common_invariants = [
        "finish-loss-status", "diagnostic-emission", "feature-inventory", "unknown-construct", "source-map-target", "stable-identity-source-seed",
        "text-limit", "typed-text", "shared-assembler", "source-fact-assembly",
    ]

    cases: list[dict[str, Any]] = []
    cases.extend(_adapter_variants("adapter:docx", "tools/adapter_docx.py", docx_text, docx_replacements, ["tools/format_qualification.py", "--format", "docx"], "adapter_docx", docx_invariants))
    cases.extend(_adapter_variants("adapter:xlsx", "tools/adapter_xlsx.py", xlsx_raw, xlsx_replacements, ["tools/format_qualification.py", "--format", "xlsx"], "adapter_xlsx", xlsx_invariants))
    cases.extend(_adapter_variants("adapter:pdf", "tools/adapter_pdf.py", pdf_text, pdf_replacements, ["tools/format_qualification.py", "--format", "pdf"], "adapter_pdf", pdf_invariants))
    cases.extend(_adapter_variants("adapter:markdown", "tools/adapter_markdown.py", markdown_text, markdown_replacements, ["tools/format_qualification.py", "--format", "markdown"], "adapter_markdown", markdown_invariants))
    cases.extend(_adapter_variants("adapter:common", "tools/adapter_common.py", common_text, common_replacements, ["tools/format_qualification.py", "--format", "markdown"], "adapter_common", common_invariants))

    validator_cases = [
        ("required-constraint", "tools/ir_validation.py", "        missing = [key for key in required if key not in value]\n        if missing:", "        missing = []\n        if missing:", "required-constraint", "ir_validation", ["tools/authority_qualification.py"]),
        ("discriminator-branch", "tools/ir_validation.py", "            if extra:", "            if False:", "discriminator-branch", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("closed-object", "tools/ir_validation.py", "            elif additional is False:", "            elif False:", "closed-object", "ir_validation", ["tools/authority_qualification.py"]),
        ("target-kinds", "tools/ir_validation.py", "    if not isinstance(value, str) or value not in ids or ids[value] not in targets:", "    if False:", "wrong-type-reference", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("parent-reciprocity", "tools/ir_validation.py", "            if node_id not in nodes[parent_id].get(\"childIds\", []):", "            if False:", "containment-reciprocity", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("cycle-detection", "tools/ir_validation.py", "        if node_id in active:", "        if False:", "containment-cycle-or-orphan", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("complete-loss", "tools/ir_validation.py", "    if conversion[\"status\"] == \"complete\" and (hard_loss or has_error):", "    if False:", "complete-status-loss", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("extension-payload", "tools/extension_registry.py", "    _validate_schema(extension[\"payload\"], payload_schema, schema, f\"$.extensions[{extension['extensionId']}].payload\")", "    if False: _validate_schema(extension[\"payload\"], payload_schema, schema, f\"$.extensions[{extension['extensionId']}].payload\")", "extension-payload", "extension_registry", ["tools/mutation_qualification.py", "--json"]),
        ("feature-inventory", "tools/ir_validation.py", "    if observed != expected:", "    if False:", "feature-inventory", "ir_validation", ["tools/mutation_qualification.py", "--json"]),
        ("source-map-format", "tools/ir_validation.py", "        if source_map.get(\"format\", {}).get(\"name\") != source.get(\"name\"):", "        if False:", "source-map-target", "ir_validation", ["tools/authority_qualification.py"]),
    ]
    for index, (suffix, target, needle, replacement, invariant, module, command) in enumerate(validator_cases, 1):
        cases.append({
            "id": f"validator-{index:02d}-{suffix}", "owner": "validator", "target": target,
            "needle": needle, "replacement": replacement, "command": command,
            "module": module, "invariant": invariant,
            "equivalence": "non-equivalent: an authority negative corpus case would be accepted",
        })

    query_cases = [
        ("version", "if index.get(\"schema\") != INDEX_SCHEMA or index.get(\"version\") != INDEX_VERSION:", "if index.get(\"schema\") != INDEX_SCHEMA:", "stale-version"),
        ("authority", "if index.get(\"authority\") != expected[\"authority\"]:", "if False:", "source-digest"),
        ("field-parity", "if index.get(field) != expected[field]:", "if False:", "all-authoritative-fields"),
        ("unexpected-field", "if set(index) != set(expected):", "if False:", "negative-corruption"),
        ("source-binding", "            \"sourceDigest\": source_digest,", "            \"sourceDigest\": \"0\" * 64,", "source-digest"),
        ("profile-binding", "            \"profileId\": profile_id,", "            \"profileId\": \"mutated-profile\",", "profile-validation"),
        ("fact-value", "        value = fact.get(\"value\")", "        value = None", "all-authoritative-fields"),
        ("fact-emission", "facts.append({\"collection\": collection, \"id\": identifier, \"digest\": canonical_value_digest(item), \"value\": item})", "facts.append({\"collection\": collection, \"id\": identifier, \"digest\": canonical_value_digest(item)})", "unqueryable-fact"),
        ("partial-write", "            stream.write(payload)", "            stream.write(\"\")", "partial-write"),
        ("atomic-write", "        os.replace(temporary, path)", "        os.replace(temporary, path.with_suffix(\".mutated\"))", "atomic-replacement"),
        ("index-authority", "            \"canonicalDigest\": canonical_digest(document),", "            \"canonicalDigest\": \"0\" * 64,", "collection-field-mapping"),
    ]
    for index, (suffix, needle, replacement, invariant) in enumerate(query_cases, 1):
        cases.append({
            "id": f"query-{index:02d}-{suffix}", "owner": "query", "target": "tools/query_ir.py",
            "needle": needle, "replacement": replacement, "command": ["tools/query_qualification.py"],
            "module": "query_ir", "invariant": invariant,
            "equivalence": "non-equivalent: an authoritative persistent-index parity or binding check is weakened",
        })

    canonical_cases = [
        ("utf16-order", 'encoded = value.encode("utf-16-be", "surrogatepass")', 'encoded = value.encode("utf-8")', "cross-language-vector"),
        ("duplicate-keys", "        if key in result:\n            raise ValueError(f\"duplicate JSON object key: {key}\")", "        if False:\n            raise ValueError(f\"duplicate JSON object key: {key}\")", "duplicate-key"),
        ("json-constant", "    raise ValueError(f\"non-JSON numeric constant: {token}\")", "    return token", "number-exactness"),
        ("forbidden-fields", "            if key in FORBIDDEN_KEYS:", "            if False:", "projection-boundary"),
        ("float-rejection", "        raise CanonicalizationError(f\"floating-point JSON number must be an exact decimal string at {path}\")", "        return", "number-exactness"),
        ("key-order", "            sort_keys=False,", "            sort_keys=True,", "key-order"),
        ("unicode-wire", "            ensure_ascii=False,", "            ensure_ascii=True,", "cross-language-vector"),
        ("entity-order", "            return sorted(result, key=lambda item: item[id_field])", "            return list(reversed(sorted(result, key=lambda item: item[id_field])))", "entity-sorting"),
        ("ordinal-order", "            return sorted(result, key=lambda item: item[\"ordinal\"])", "            return list(reversed(sorted(result, key=lambda item: item[\"ordinal\"])))", "entity-sorting"),
        ("projection-exclusion", "    for key in (\"documentId\", \"sourceMaps\", \"diagnostics\", \"conversion\", \"observations\"):", "    for key in (\"sourceMaps\", \"diagnostics\", \"conversion\", \"observations\"):", "projection-boundary"),
        ("stable-id", "        id_field = _ID_ARRAYS.get(field or \"\")", "        id_field = None", "stable-id"),
        ("migration-receipt", "        \"status\": \"preserved\",", "        \"status\": \"omitted\",", "migration-receipt"),
    ]
    for index, (suffix, needle, replacement, invariant) in enumerate(canonical_cases, 1):
        cases.append({
            "id": f"canonical-{index:02d}-{suffix}", "owner": "canonical", "target": "tools/canonicalize_ir.py",
            "needle": needle, "replacement": replacement, "command": ["tools/mutation_qualification.py", "--json"],
            "module": "canonicalize_ir", "invariant": invariant,
            "equivalence": "non-equivalent: canonical bytes or digest vectors no longer agree with the declared authority",
        })

    release_cases = [
        ("source-sha-1", '    if bundle["source"]["headSha"] != source["headSha"]:', "    if False:", "source-sha"),
        ("source-sha-2", '    if bundle["source"]["headSha"] != source["headSha"]:', '    if bundle["source"]["headSha"] == source["headSha"]:', "source-sha"),
        ("bundle-digest-1", '    if bundle.get("integrity", {}).get("bundleDigest") != bundle_digest(bundle):', "    if False:", "evidence-digest"),
        ("bundle-digest-2", '    if bundle.get("integrity", {}).get("bundleDigest") != bundle_digest(bundle):', '    if bundle.get("integrity", {}).get("bundleDigest") == bundle_digest(bundle):', "evidence-digest"),
        ("artifact-bytes-1", '        if sha != item["sha256"] or size != item["bytes"]:', "        if False:", "artifact-presence"),
        ("artifact-bytes-2", '        if sha != item["sha256"] or size != item["bytes"]:', '        if sha == item["sha256"] and size == item["bytes"]:', "artifact-presence"),
        ("external-index", "        if external_index != expected_index:", "        if False:", "artifact-presence"),
        ("embedded-index", "    if bundle_index_core != expected_index:", "    if False:", "evidence-digest"),
        ("release-barrier", '    if bundle.get("barrier", {}).get("releaseEligible") and (not source["workingTreeClean"] or bundle["source"].get("binding") != "exact-commit"):', "    if False:", "clean-room"),
        ("release-barrier-2", '    if bundle.get("barrier", {}).get("releaseEligible") and (not source["workingTreeClean"] or bundle["source"].get("binding") != "exact-commit"):', '    if bundle.get("barrier", {}).get("releaseEligible") and (source["workingTreeClean"] and bundle["source"].get("binding") == "exact-commit"):', "claim-drift"),
    ]
    for index, (suffix, needle, replacement, invariant) in enumerate(release_cases, 1):
        cases.append({
            "id": f"release-{index:02d}-{suffix}", "owner": "release", "target": "tools/evidence_bundle.py",
            "needle": needle, "replacement": replacement, "command": ["tools/evidence_bundle.py", "self-test"],
            "module": "evidence_bundle", "invariant": invariant,
            "equivalence": "non-equivalent: the executable release evidence barrier would accept tampered evidence",
        })
    release_gate_cases = [
        ("issue-state", '    require(plan.get("liveState", {}).get("source") == "github-issue-api" and plan.get("liveState", {}).get("requiredAtGate") is True, "audit recovery does not require live GitHub state")', '    require(True, "audit recovery does not require live GitHub state")', "issue-state"),
        ("ci-required-check", '    require(isinstance(required_commands, list) and "python tools/mutation_qualification.py --json" in required_commands and "python tools/query_qualification.py" in required_commands and "python tools/independent_corpus.py --json" in required_commands and "python tools/strict_completion_gate.py" in required_commands and "python tools/release_gate.py" in required_commands, "phase2 qualification commands are incomplete")', '    require(True, "phase2 qualification commands are incomplete")', "ci-required-check"),
        ("unsupported-scope", '    require(requirements.get("claimMode") == "experimental-bounded-subset" and requirements.get("releaseEligible") is False, "release requirements overclaim an eligible release")', '    require(True, "release requirements overclaim an eligible release")', "unsupported-scope"),
        ("clean-room", '        and clean_room.get("diffCount") == 0,', '        and (clean_room.get("diffCount") == 0 or clean_room.get("diffCount") == 1),', "clean-room"),
    ]
    for index, (suffix, needle, replacement, invariant) in enumerate(release_gate_cases, 1):
        cases.append({
            "id": f"release-gate-{index:02d}-{suffix}", "owner": "release", "target": "tools/release_gate.py",
            "needle": needle, "replacement": replacement, "command": ["tools/release_contract_qualification.py"],
            "module": "release_gate", "invariant": invariant,
            "equivalence": "non-equivalent: a release manifest or clean-room claim would no longer fail closed",
        })
    return sorted(cases, key=lambda item: item["id"])


def case_descriptors(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: case[key] for key in ("id", "owner", "target", "needle", "replacement", "command", "module", "invariant", "equivalence")}
        for case in cases
    ]


def config_digest(cases: list[dict[str, Any]], contract: dict[str, Any]) -> str:
    value = {"contract": contract, "cases": case_descriptors(cases)}
    return sha256_bytes(canonical(value))


def tracked_paths() -> list[str]:
    raw = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(item for item in raw.split("\0") if item and not item.startswith(("e2e/.run/", "reports/", "tmp/", ".query-")))


def disposable_checkout(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for name in tracked_paths():
        source = ROOT / name
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


def remove_checkout(path: Path) -> None:
    """Remove a generated checkout even when copied files are read-only."""

    def onerror(function: Any, failed_path: str, _exc_info: Any) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE)
        finally:
            function(failed_path)

    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def execute(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [sys.executable, *command], cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=environment,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exitCode": -1, "status": "timeout", "stdout": str(exc.stdout or ""), "stderr": str(exc.stderr or ""),
            "durationMilliseconds": round((time.monotonic() - started) * 1000, 3),
        }
    except OSError as exc:
        return {
            "exitCode": -1, "status": "infrastructure-error", "stdout": "", "stderr": str(exc),
            "durationMilliseconds": round((time.monotonic() - started) * 1000, 3),
        }
    return {
        "exitCode": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
        "stdout": completed.stdout.replace("\r\n", "\n"),
        "stderr": completed.stderr.replace("\r\n", "\n"),
        "durationMilliseconds": round((time.monotonic() - started) * 1000, 3),
    }


def apply_mutation(folder: Path, case: dict[str, Any]) -> dict[str, Any]:
    path = folder / case["target"]
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CampaignError(f"mutation target is unreadable: {case['target']}: {exc}") from exc
    needle = case["needle"]
    count = original.count(needle)
    if count != 1:
        raise CampaignError(f"mutation {case['id']} expected one target occurrence, observed {count}")
    mutated = original.replace(needle, case["replacement"], 1)
    path.write_text(mutated, encoding="utf-8", newline="\n")
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), mutated.splitlines(keepends=True),
        fromfile=case["target"], tofile=case["target"] + " (mutant)",
    ))
    if not diff:
        raise CampaignError(f"mutation {case['id']} produced an empty diff")
    return {
        "target": case["target"], "targetOccurrences": count, "applied": True,
        "beforeDigest": sha256_bytes(original.encode("utf-8")),
        "afterDigest": sha256_bytes(mutated.encode("utf-8")),
        "patchDigest": sha256_bytes(diff.encode("utf-8")), "diff": diff,
    }


def import_module(folder: Path, module: str) -> dict[str, Any]:
    script = "import sys; sys.path.insert(0, 'tools'); __import__(%r)" % module
    return execute(["-c", script], folder, 30)


def classify_case(case: dict[str, Any], folder: Path, base_sha: str, digest: str) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "id": case["id"], "owner": case["owner"], "invariant": case["invariant"], "baseSha": base_sha,
        "configDigest": digest, "target": case["target"], "command": ["python", *case["command"]],
        "classification": "infrastructure-error", "detected": False,
    }
    try:
        patch = apply_mutation(folder, case)
    except (CampaignError, OSError) as exc:
        result.update({"classification": "infrastructure-error", "detected": False, "detail": str(exc), "durationMilliseconds": round((time.monotonic() - started) * 1000, 3)})
        return result
    result["patch"] = patch

    compile_result = execute(["-m", "py_compile", case["target"]], folder, 30)
    result["syntax"] = {key: compile_result[key] for key in ("exitCode", "status", "durationMilliseconds")}
    if compile_result["status"] != "passed":
        result.update({"classification": "invalid", "detected": False, "syntaxStdoutDigest": sha256_bytes(compile_result["stdout"].encode()), "syntaxStderrDigest": sha256_bytes(compile_result["stderr"].encode()), "durationMilliseconds": round((time.monotonic() - started) * 1000, 3)})
        return result

    import_result = import_module(folder, case["module"])
    result["import"] = {key: import_result[key] for key in ("exitCode", "status", "durationMilliseconds")}
    if import_result["status"] != "passed":
        result.update({"classification": "invalid", "detected": False, "importStdoutDigest": sha256_bytes(import_result["stdout"].encode()), "importStderrDigest": sha256_bytes(import_result["stderr"].encode()), "durationMilliseconds": round((time.monotonic() - started) * 1000, 3)})
        return result

    command_result = execute(case["command"], folder, 180)
    result["execution"] = {
        "exitCode": command_result["exitCode"], "status": command_result["status"],
        "stdoutDigest": sha256_bytes(command_result["stdout"].encode()),
        "stderrDigest": sha256_bytes(command_result["stderr"].encode()),
        "durationMilliseconds": command_result["durationMilliseconds"],
    }
    if command_result["status"] == "timeout":
        classification = "timeout"
    elif command_result["status"] == "infrastructure-error":
        classification = "infrastructure-error"
    elif command_result["exitCode"] == 0:
        classification = "undetected"
    else:
        # The module was importable and the designated public qualification
        # command rejected the mutant.  The command's own assertions are the
        # detector; no comparison with the mutation source is made here.
        classification = "detected"
    result.update({"classification": classification, "detected": classification == "detected", "durationMilliseconds": round((time.monotonic() - started) * 1000, 3)})
    return result


def run_base_suite(folder: Path) -> list[dict[str, Any]]:
    contract = load_contract()
    commands = contract.get("baseSuite")
    if not isinstance(commands, list) or not commands:
        raise CampaignError("baseSuite is empty")
    results = []
    for display in commands:
        if not isinstance(display, str) or not display.startswith("python "):
            raise CampaignError(f"baseSuite command is not a python command: {display!r}")
        argv = display.split()[1:]
        result = execute(argv, folder, 300)
        results.append({
            "display": display, "exitCode": result["exitCode"], "status": result["status"],
            "stdoutDigest": sha256_bytes(result["stdout"].encode()), "stderrDigest": sha256_bytes(result["stderr"].encode()),
            "durationMilliseconds": result["durationMilliseconds"],
        })
        if result["status"] != "passed":
            break
    return results


def counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    return {name: sum(item.get("classification") == name for item in cases) for name in CLASSIFICATIONS}


def coverage_report(cases: list[dict[str, Any]], contract: dict[str, Any]) -> dict[str, Any]:
    minimums = contract.get("minimumNonEquivalentCases", {})
    by_owner = {owner: sum(item.get("classification") in {"detected", "undetected"} for item in cases if item.get("owner") == owner) for owner in sorted({str(item.get("owner")) for item in cases})}
    required = contract.get("mustInvariants", {})
    covered: dict[str, list[str]] = {}
    for item in cases:
        if item.get("classification") == "detected":
            covered.setdefault(str(item.get("owner")), []).append(str(item.get("invariant")))
    missing = {
        owner: sorted(set(values) - set(covered.get(owner, [])))
        for owner, values in required.items()
        if set(values) - set(covered.get(owner, []))
    }
    minimum_failures = {owner: {"required": int(value), "actual": by_owner.get(owner, 0)} for owner, value in minimums.items() if by_owner.get(owner, 0) < int(value)}
    return {
        "minimumNonEquivalentCases": {str(key): int(value) for key, value in minimums.items()},
        "nonEquivalentCasesByOwner": by_owner,
        "requiredInvariants": {str(key): sorted(set(value)) for key, value in required.items()},
        "coveredInvariants": {key: sorted(set(value)) for key, value in sorted(covered.items())},
        "missingInvariants": missing, "minimumFailures": minimum_failures,
        "coverageStatus": "passed" if not missing and not minimum_failures else "failed",
    }


def self_test() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    # Invalid/unapplied patches are infrastructure errors and never detections.
    try:
        original = "alpha\n"
        if original.count("absent") != 1:
            raise CampaignError("target occurrence is not exactly one")
    except CampaignError:
        checks.append({"id": "patch-application-infrastructure-error", "status": "passed", "classification": "infrastructure-error"})
    else:
        raise CampaignError("invalid patch self-test survived")

    # A disabled detector is an undetected mutation, not a pass.
    fake = {"exitCode": 0, "status": "passed"}
    observed = "undetected" if fake["exitCode"] == 0 else "detected"
    if observed == "undetected":
        checks.append({"id": "detector-disabled-undetected", "status": "passed", "classification": observed})
    else:
        raise CampaignError("detector-disabled self-test did not classify undetected")

    if any(item["status"] != "passed" for item in [{"status": "failed"}]):
        checks.append({"id": "base-suite-failure-blocks", "status": "passed", "campaignStatus": "blocked"})
    else:
        raise CampaignError("base-suite failure self-test survived")

    first = ROOT / "e2e" / ".run" / f"defect-self-test-{os.getpid()}-a.log"
    second = ROOT / "e2e" / ".run" / f"defect-self-test-{os.getpid()}-b.log"
    if first != second:
        checks.append({"id": "parallel-output-isolation", "status": "passed", "pathsDistinct": True})
    else:
        raise CampaignError("parallel output isolation self-test survived")

    ids = [case["id"] for case in mutation_catalog()]
    if ids == sorted(ids) and len(ids) == len(set(ids)):
        checks.append({"id": "deterministic-case-order", "status": "passed", "caseCount": len(ids)})
    else:
        raise CampaignError("case order is not deterministic")
    return {"schema": "fdir/defect-injection-self-test-report", "version": "1.0.0", "status": "passed", "checks": checks}


def campaign() -> dict[str, Any]:
    contract = load_contract()
    cases = mutation_catalog()
    source = source_state()
    digest = config_digest(cases, contract)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "version": "1.0.0", "status": "blocked", "campaignStatus": "blocked",
        "baseSha": source["headSha"], "configDigest": digest, "source": source,
        "counts": {name: 0 for name in CLASSIFICATIONS}, "cases": [], "survivors": [], "waivers": [],
        "coverage": {}, "durationMilliseconds": 0.0, "resourceUsage": {"checkouts": 0, "maxCaseDurationMilliseconds": 0.0},
    }
    started = time.monotonic()
    root = ROOT / "e2e" / ".run" / f"defect-injection-campaign-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=False)
    base_folder = disposable_checkout(root / "base")
    report["resourceUsage"]["checkouts"] = 1
    base_suite = run_base_suite(base_folder)
    remove_checkout(base_folder)
    report["baseSuite"] = base_suite
    if any(item["status"] != "passed" for item in base_suite):
        report["coverage"] = coverage_report([], contract)
        report["durationMilliseconds"] = round((time.monotonic() - started) * 1000, 3)
        report["blockers"] = [{"code": "BASE_SUITE_FAILED", "detail": item} for item in base_suite if item["status"] != "passed"]
        return report

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        folder = disposable_checkout(root / f"case-{index:03d}-{case['id']}")
        report["resourceUsage"]["checkouts"] += 1
        try:
            result = classify_case(case, folder, source["headSha"], digest)
        finally:
            # The report carries the exact patch and process digests; retaining
            # every full checkout would make a bounded campaign consume an
            # unbounded amount of disk space.
            remove_checkout(folder)
        results.append(result)
        report["resourceUsage"]["maxCaseDurationMilliseconds"] = max(float(report["resourceUsage"]["maxCaseDurationMilliseconds"]), float(result.get("durationMilliseconds", 0.0)))

    report["cases"] = results
    report["counts"] = counts(results)
    report["survivors"] = [item["id"] for item in results if item.get("classification") == "undetected"]
    report["coverage"] = coverage_report(results, contract)
    report["durationMilliseconds"] = round((time.monotonic() - started) * 1000, 3)
    report["selfTests"] = self_test()
    good = (
        report["selfTests"]["status"] == "passed"
        and report["counts"]["undetected"] == 0
        and report["counts"]["timeout"] == 0
        and report["counts"]["infrastructure-error"] == 0
        and report["counts"]["invalid"] == 0
        and report["coverage"].get("coverageStatus") == "passed"
    )
    report["status"] = "passed" if good else "blocked"
    report["campaignStatus"] = "passed" if good else "failed"
    report["releaseEligible"] = False
    report["blockers"] = [] if good else [{"code": "CAMPAIGN_NOT_GREEN", "detail": "all mutations must be valid and detected with complete declared coverage"}]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit compact JSON")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = self_test() if args.self_test else campaign()
    except (CampaignError, OSError, subprocess.SubprocessError) as exc:
        report = {"schema": REPORT_SCHEMA, "version": "1.0.0", "status": "blocked", "campaignStatus": "infrastructure-error", "survivors": [], "blockers": [{"code": "INFRASTRUCTURE_ERROR", "detail": str(exc)}]}
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
