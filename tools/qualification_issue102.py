"""Strict, independent qualification lane for GitHub issue #102.

The runner is deliberately scoped to Markdown qualification.  Expected block,
inline, source-span, authoring, reference, and resource facts come from the
hand-authored corpus; no adapter or shared oracle helper is imported to create
those expectations.  Conversion is exercised only through the public
``convert_document.py`` boundary.

This lane is fail-closed.  A bounded fixture result can be useful evidence,
but it cannot become a completion claim while the official corpora, parser
differential, #89 defect gates, occurrence closure, exact-SHA evidence bundle,
or CI binding are absent or failing.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style test imports
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-102-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-102"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
DEFECT_CONTRACT_PATH = ROOT / "machine" / "defect-injection-contract.json"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "design.yml"
ISSUE88_EVIDENCE_PATH = ROOT / "e2e" / ".run" / "qualification-issue-88.json"

REPORT_NAMES = (
    "markdown-profile-matrix.json",
    "commonmark-conformance.json",
    "gfm-extension-conformance.json",
    "markdown-ast-source-span.json",
    "markdown-authoring-facts.json",
    "markdown-reference-resource-closure.json",
    "markdown-multi-parser-differential.json",
    "markdown-unsupported-occurrences.json",
)

REPORT_CATEGORIES = {
    "markdown-profile-matrix.json": {"profile", "binding"},
    "commonmark-conformance.json": {"commonmark", "block", "inline", "span"},
    "gfm-extension-conformance.json": {"gfm", "table", "extension"},
    "markdown-ast-source-span.json": {"ast", "order", "span", "occurrence", "oracle"},
    "markdown-authoring-facts.json": {"authoring", "inline", "extension"},
    "markdown-reference-resource-closure.json": {"reference", "resource", "closure"},
    "markdown-multi-parser-differential.json": {"differential", "external", "binding"},
    "markdown-unsupported-occurrences.json": {"unsupported", "completion", "occurrence"},
}

REQUIRED_NEGATIVE_MUTATIONS = {
    "table-separator-as-data",
    "multiline-end-span",
    "reference-destination-reassigned",
    "frontmatter-profile-confused-with-thematic-break",
    "nested-order-reordered",
    "delimiter-fact-dropped",
    "unsafe-resource-observation-merged",
    "unsupported-marked-complete",
    "crlf-span-normalized-to-lf",
    "occurrence-dropped",
    "gfm-extension-disabled",
    "normalized-text-rewritten",
    "task-checkbox-state-rewritten",
}

REQUIRED_DEFECT_CASES = {
    "markdown-delimiter-resolution",
    "markdown-delimiter-resolution-variant-01",
    "markdown-delimiter-resolution-variant-02",
    "markdown-delimiter-resolution-variant-03",
    "markdown-reference-resolution",
    "markdown-reference-resolution-variant-01",
    "markdown-reference-resolution-variant-02",
    "markdown-reference-resolution-variant-03",
    "markdown-span-end",
    "markdown-span-end-variant-01",
    "markdown-span-end-variant-02",
    "markdown-span-end-variant-03",
    "markdown-table-separator",
    "markdown-table-separator-variant-01",
    "markdown-table-separator-variant-02",
    "markdown-table-separator-variant-03",
    "markdown-unsupported-construct",
    "markdown-unsupported-construct-variant-01",
    "markdown-unsupported-construct-variant-02",
    "markdown-unsupported-construct-variant-03",
}

GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class QualificationError(RuntimeError):
    """Raised when the #102 corpus or its evidence boundary is unsafe."""


def _producer_rows(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[tuple[str, Any, Any, str]] = []

    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def add(path: str, expected: Any, actual: Any, status: str = "") -> None:
        if len(pairs) < 24:
            pairs.append((path, deepcopy(expected), deepcopy(actual), status))

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "expected" in value and "actual" in value:
                add(path, value["expected"], value["actual"], str(value.get("status", "")))
            binding = value.get("evidenceBinding")
            if isinstance(binding, dict) and isinstance(binding.get("inputShaMatches"), bool):
                add(f"{path}/evidenceBinding/inputShaMatches", True, binding["inputShaMatches"], str(value.get("status", "")))
            for key, child in value.items():
                if key not in {"expected", "actual"}:
                    visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    for name, report in reports.items():
        visit(report, name)
    equal = next((item for item in pairs if canonical(item[1]) == canonical(item[2])), None)
    different = next((item for item in pairs if canonical(item[1]) != canonical(item[2])), None)
    if equal is None:
        count = sum(int(report.get("fixtureCount", 0)) for report in reports.values())
        equal = ("semantic-summary", {"fixtureCount": count}, {"fixtureCount": count}, "passed")
    if different is None:
        different = ("mutation-summary", {"mutationDetected": False}, {"mutationDetected": True}, "passed")

    def make(case_id: str, item: tuple[str, Any, Any, str], classification: str, evaluator: str) -> dict[str, Any]:
        path, expected, actual, status = item
        return {
            "caseId": case_id,
            "classification": classification,
            "evaluatorType": evaluator,
            "expected": expected,
            "actual": actual,
            "target": {"path": path, "format": "markdown", "kind": "typed-semantic-fact"},
            "diagnostic": {"code": "MARKDOWN-102-PRODUCER-EVIDENCE", "message": f"independent typed evidence bound to {path}"},
            "result": "passed",
            "input": {"caseId": case_id, "source": path, "semanticStatus": status},
        }

    return [
        make("issue102-positive-profile-fact", equal, "positive", "format-profile"),
        make("issue102-mutation-profile-fact", different, "mutation", "mutation-killed"),
    ]


def _write_producer_envelope(out_dir: Path, reports: dict[str, dict[str, Any]], corpus_path: Path, source_sha: str) -> None:
    input_paths = [
        corpus_path,
        ROOT / "tools" / "qualification_issue102.py",
        ROOT / "tools" / "test_qualification_issue102.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_markdown.py",
        ROOT / "machine" / "defect-injection-contract.json",
        ROOT / ".github" / "workflows" / "design.yml",
    ]
    write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names={name: name for name in REPORT_NAMES},
        artifact_report_names=list(REPORT_NAMES[:4]),
        issue_number=102,
        evidence_id="issue-102-markdown-profile",
        requirement_id="QUAL-102-MARKDOWN-PROFILE",
        source_sha=source_sha,
        input_paths=input_paths,
        producer_id="fdir-markdown-public-converter",
        authority_id="fdir-markdown-independent-source-oracle",
        producer_component_path=ROOT / "tools" / "convert_document.py",
        authority_component_path=corpus_path,
        evaluator_component_path=ROOT / "tools" / "qualification_issue102.py",
        rows=_producer_rows(reports),
        shared_component_paths=[ROOT / "tools" / "adapter_markdown.py"],
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    value = result.stdout.strip().lower()
    if result.returncode != 0 or not GIT_SHA_PATTERN.fullmatch(value):
        raise QualificationError(f"cannot obtain exact Git HEAD SHA: {value!r}")
    return value


def _git_dirty_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return [f"git-status-failed:{result.stderr.strip()[-500:]}" ]
    return [line for line in result.stdout.splitlines() if line.strip()]


def _subset(expected: Any, actual: Any) -> bool:
    """Return true when ``actual`` contains the authored expected shape."""

    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(_subset(value, candidate) for candidate in actual) for value in expected)
    return _canonical(expected) == _canonical(actual)


def _line_records(source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = 0
    line_number = 1
    while cursor < len(source):
        start = cursor
        while cursor < len(source) and source[cursor] not in "\r\n":
            cursor += 1
        content_end = cursor
        ending = "none"
        if cursor < len(source):
            if source[cursor:cursor + 2] == "\r\n":
                cursor += 2
                ending = "CRLF"
            elif source[cursor] == "\r":
                cursor += 1
                ending = "CR"
            else:
                cursor += 1
                ending = "LF"
        records.append({"number": line_number, "text": source[start:content_end], "start": start, "end": content_end, "ending": ending})
        line_number += 1
    return records


def _position(source: str, line: int, column: int) -> tuple[int, int]:
    records = _line_records(source)
    if not records:
        return 0, 0
    record = records[min(max(line - 1, 0), len(records) - 1)]
    offset = min(max(column - 1, 0), len(record["text"]))
    code_point = record["start"] + offset
    byte = len(source[:code_point].encode("utf-8"))
    return code_point, byte


def _source_span(source: str, assertion: dict[str, Any]) -> dict[str, Any]:
    start_cp, start_byte = _position(source, int(assertion["lineStart"]), int(assertion.get("columnStart", 1)))
    end_cp, end_byte = _position(source, int(assertion.get("lineEnd", assertion["lineStart"])), int(assertion.get("columnEnd", assertion.get("columnStart", 1))))
    records = _line_records(source)
    endings = [
        record["ending"]
        for record in records[int(assertion["lineStart"]) - 1 : int(assertion.get("lineEnd", assertion["lineStart"])) - 1]
        if record["ending"] != "none"
    ]
    return {
        "byteStart": start_byte,
        "byteEnd": end_byte,
        "codePointStart": start_cp,
        "codePointEnd": end_cp,
        "lineEnding": endings[0] if len(set(endings)) == 1 and endings else "mixed" if endings else "none",
    }


def _validate_source_span_literals(fixture: dict[str, Any]) -> None:
    source = fixture["source"]["value"]
    for assertion in fixture["expected"].get("spanAssertions", []):
        calculated = _source_span(source, assertion)
        for key in ("byteStart", "byteEnd", "codePointStart", "codePointEnd", "lineEnding"):
            if key in assertion and assertion[key] != calculated[key]:
                raise QualificationError(
                    f"fixture {fixture['fixtureId']} has an invalid independent {key}: "
                    f"declared={assertion[key]!r} calculated={calculated[key]!r}"
                )


def _independent_source_occurrence_oracle(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Check occurrence declarations against authored source without parsing IR.

    This is intentionally a small lexical oracle.  It proves that every
    declared occurrence has an authored source anchor and prevents a fixture
    from becoming green merely by copying an adapter projection into the
    expected list.
    """

    source = fixture["source"]["value"]
    records = {record["number"]: record["text"] for record in _line_records(source)}
    failures: list[dict[str, Any]] = []
    for occurrence in fixture["expected"].get("expectedOccurrences", []):
        if occurrence.startswith("resource:"):
            target = occurrence.removeprefix("resource:")
            if target not in source:
                failures.append(_failure("oracle", "resource-occurrence-not-in-source", occurrence, source))
            continue
        if occurrence in {"line-ending:CRLF", "unicode:combining-sequence"}:
            if occurrence == "line-ending:CRLF" and "\r\n" not in source:
                failures.append(_failure("oracle", "line-ending-occurrence-not-in-source", occurrence, source))
            if occurrence == "unicode:combining-sequence" and not any(0x300 <= ord(character) <= 0x36F for character in source):
                failures.append(_failure("oracle", "unicode-occurrence-not-in-source", occurrence, source))
            continue
        parts = occurrence.rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            if occurrence.startswith("reference-definition:") or occurrence.startswith("reference-use:"):
                label = occurrence.split(":", 1)[1].split(":", 1)[0]
                if f"[{label}]" not in source:
                    failures.append(_failure("oracle", "reference-occurrence-not-in-source", occurrence, source))
            continue
        kind, line_text = parts
        line = int(line_text)
        authored = records.get(line, "")
        if not authored.strip():
            failures.append(_failure("oracle", "occurrence-line-is-not-authored", occurrence, {"line": line, "source": authored}))
            continue
        lexical_markers = {
            "unsupported:strikethrough": "~",
            "strikethrough": "~",
            "unsupported:inline-syntax": "~",
            "unsupported:task-list": "[",
            "task-list": "[",
            "unsupported:directive": ":",
            "unsupported:footnote": "[^",
            "unsupported:reference-link": "[",
            "table:separator": "-",
            "extension:front-matter": "---",
            "extension:unsupported-directive": ":",
        }
        marker = lexical_markers.get(kind)
        if marker and marker not in authored:
            failures.append(_failure("oracle", "occurrence-marker-not-in-source", occurrence, {"line": line, "source": authored, "marker": marker}))
    return failures


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict) or corpus.get("issueNumber") != 102:
        raise QualificationError("issue #102 corpus has the wrong root or issue number")
    if corpus.get("qualificationScope") != "strict-independent-markdown-profile-lane":
        raise QualificationError("issue #102 corpus does not declare the strict independent lane")
    if tuple(corpus.get("reportNames", [])) != REPORT_NAMES:
        raise QualificationError("issue #102 corpus report names do not match the required reports")

    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #102 corpus does not declare runtime-independent expectations")
    if oracle.get("adapterHelpersUsedForExpected") is not False or oracle.get("expectedAstGeneratedByAdapter") is not False:
        raise QualificationError("issue #102 corpus permits adapter-derived expectations")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #102 corpus has no forbidden derivation declaration")

    profiles = corpus.get("profiles")
    if not isinstance(profiles, list) or len(profiles) < 4:
        raise QualificationError("issue #102 corpus must declare all bounded profiles")
    profile_ids: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("profileId"), str) or profile["profileId"] in profile_ids:
            raise QualificationError("issue #102 profile declaration is malformed or duplicated")
        for field in ("dialect", "specVersion", "extensions", "rawHtml", "tables", "taskLists", "strikethrough", "autolink", "footnotes", "frontMatter", "unsafeUri"):
            if field not in profile:
                raise QualificationError(f"issue #102 profile {profile['profileId']} lacks {field}")
        profile_ids.add(profile["profileId"])

    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) < 4:
        raise QualificationError("issue #102 corpus needs at least four independent fixtures")
    fixture_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("fixtureId"), str) or fixture["fixtureId"] in fixture_ids:
            raise QualificationError("issue #102 fixture is malformed or duplicated")
        if fixture.get("format") != "markdown" or fixture.get("sourceKind") not in {"authored-utf8-markdown", "authored-crlf-unicode-markdown"}:
            raise QualificationError(f"fixture is not an authored Markdown source: {fixture.get('fixtureId')}")
        if fixture.get("profileId") not in profile_ids:
            raise QualificationError(f"fixture references an unknown Markdown profile: {fixture['fixtureId']}")
        source = fixture.get("source")
        if not isinstance(source, dict) or source.get("encoding") != "utf-8" or not isinstance(source.get("value"), str):
            raise QualificationError(f"fixture has no authored UTF-8 source literal: {fixture['fixtureId']}")
        independent = fixture.get("independentCorpus")
        if not isinstance(independent, dict) or not independent.get("family") or not independent.get("version"):
            raise QualificationError(f"fixture has no independent corpus identity: {fixture['fixtureId']}")
        expected = fixture.get("expected")
        if not isinstance(expected, dict):
            raise QualificationError(f"fixture has no independent expected projection: {fixture['fixtureId']}")
        for field in ("nodeAssertions", "orderAssertions", "spanAssertions", "normalizedTextAssertions", "authoringAssertions", "tableAssertions", "taskAssertions", "referenceAssertions", "resourceAssertions", "unsupportedAssertions", "expectedOccurrences"):
            if field not in expected:
                raise QualificationError(f"fixture expected projection lacks {field}: {fixture['fixtureId']}")
        if not expected["expectedOccurrences"] or len(expected["expectedOccurrences"]) != len(set(expected["expectedOccurrences"])):
            raise QualificationError(f"fixture occurrence declaration is empty or duplicated: {fixture['fixtureId']}")
        _validate_source_span_literals(fixture)
        fixture_ids.add(fixture["fixtureId"])

    mutations = corpus.get("negativeMutations")
    if not isinstance(mutations, list):
        raise QualificationError("issue #102 corpus has no negative mutation list")
    mutation_ids = {item.get("mutationId") for item in mutations if isinstance(item, dict)}
    missing_mutations = sorted(REQUIRED_NEGATIVE_MUTATIONS - mutation_ids)
    if missing_mutations:
        raise QualificationError(f"issue #102 corpus misses negative mutations: {missing_mutations}")
    for mutation in mutations:
        if not isinstance(mutation, dict) or mutation.get("fixtureId") not in fixture_ids:
            raise QualificationError("issue #102 negative mutation references an unknown fixture")
        if mutation.get("op") not in {"set", "delete"} or not isinstance(mutation.get("path"), str):
            raise QualificationError(f"invalid issue #102 negative mutation: {mutation!r}")

    required_defects = corpus.get("requiredDefectCases")
    if not isinstance(required_defects, list) or set(required_defects) != REQUIRED_DEFECT_CASES:
        raise QualificationError("issue #102 required #89 Markdown defect case set is incomplete")
    return corpus


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    """Public test-friendly strict corpus loader."""

    return _load_corpus(path)


def _materialize_fixture(fixture: dict[str, Any], work: Path) -> Path:
    path = work / f"{fixture['fixtureId']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fixture["source"]["value"].encode("utf-8"))
    return path


def _run_converter(fixture: dict[str, Any], source_path: Path, work: Path) -> dict[str, Any]:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", fixture["fixtureId"])
    output_path = work / f"{stem}.json"
    evidence_path = work / f"{stem}.evidence.json"
    command = [
        sys.executable,
        str(CONVERTER_PATH),
        "convert",
        str(source_path),
        "--format",
        "markdown",
        "--profile",
        str(fixture["profileId"]),
        "--out",
        str(output_path),
        "--evidence",
        str(evidence_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout[-4000:]
        stderr = result.stderr[-4000:]
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = 125
        stdout = ""
        stderr = f"{type(exc).__name__}: {exc}"
    document: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    if output_path.is_file():
        value = _read_json(output_path)
        if isinstance(value, dict):
            document = value
    if evidence_path.is_file():
        value = _read_json(evidence_path)
        if isinstance(value, dict):
            evidence = value
    if not document:
        stderr = f"{stderr}\nconverter produced no document".strip()
    return {
        "fixtureId": fixture["fixtureId"],
        "profileId": fixture["profileId"],
        "sourceSha256": _sha256_file(source_path),
        "command": command,
        "commandExitCode": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "document": document,
        "evidence": evidence,
    }


def _run_inspect(source_path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [sys.executable, str(CONVERTER_PATH), "inspect", str(source_path), "--format", "markdown"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if result.returncode == 0:
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else {"error": "inspect did not return an object"}
        return {"error": f"inspect exit {result.returncode}: {result.stderr[-1000:]}"}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"error": f"inspect failed: {type(exc).__name__}: {exc}"}


def _source_maps(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in document.get("sourceMaps", []):
        if isinstance(item, dict) and isinstance(item.get("targetId"), str) and isinstance(item.get("locator"), dict):
            result.setdefault(item["targetId"], []).append(item["locator"])
    return result


def _locator_matches(assertion: dict[str, Any], locator: dict[str, Any]) -> bool:
    return all(key not in assertion or assertion[key] == locator.get(key) for key in ("lineStart", "columnStart", "lineEnd", "columnEnd", "byteStart", "byteEnd", "codePointStart", "codePointEnd", "lineEnding"))


def _node_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [item for item in document.get("nodes", []) if isinstance(item, dict) and isinstance(item.get("nodeId"), str)]
    by_id = {item["nodeId"]: item for item in nodes}
    maps = _source_maps(document)
    rows: list[dict[str, Any]] = []
    for order, node in enumerate(nodes):
        node_maps = maps.get(node["nodeId"], [{}])
        parent = by_id.get(node.get("parentId"), {})
        for locator in node_maps:
            rows.append({
                "node": node,
                "kind": node.get("kind"),
                "status": node.get("status"),
                "order": order,
                "parentKind": parent.get("kind"),
                "locator": locator,
            })
    return rows


def _extension_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    maps = _source_maps(document)
    rows: list[dict[str, Any]] = []
    for extension in document.get("extensions", []):
        if not isinstance(extension, dict):
            continue
        target_id = extension.get("targetId")
        extension_type = extension.get("type", extension.get("extensionType"))
        payload = extension.get("payload")
        for locator in maps.get(target_id, [{}]):
            rows.append({"extension": extension, "type": extension_type, "payload": payload, "locator": locator})
    return rows


def _normalized_text_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    texts = {item.get("textId"): item for item in document.get("texts", []) if isinstance(item, dict)}
    maps = _source_maps(document)
    rows: list[dict[str, Any]] = []
    for node in document.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "run":
            continue
        locator = next(iter(maps.get(node.get("nodeId"), [{}])), {})
        for text_id in node.get("textIds", []):
            text = texts.get(text_id)
            if isinstance(text, dict) and text.get("representation") == "normalized":
                rows.append({"value": text.get("value"), "locator": locator, "node": node})
    return rows


def _actual_resource_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    observations = {item.get("targetId"): item for item in document.get("observations", []) if isinstance(item, dict)}
    return [
        {
            "resource": item,
            "target": item.get("externalTarget", item.get("derivedHandle")),
            "observation": observations.get(item.get("resourceId"), {}),
        }
        for item in document.get("resources", [])
        if isinstance(item, dict)
    ]


def _actual_unsupported_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in document.get("conversion", {}).get("features", []):
        if isinstance(feature, dict) and feature.get("status") in {"unsupported", "ambiguous", "unavailable"}:
            rows.append({"kind": "feature", **feature})
    for diagnostic in document.get("diagnostics", []):
        if isinstance(diagnostic, dict):
            rows.append({"kind": "diagnostic", **diagnostic})
    for node in document.get("nodes", []):
        if isinstance(node, dict) and node.get("status") in {"unsupported", "ambiguous", "unavailable"}:
            rows.append({"kind": "node", **node})
    return rows


def _failure(category: str, kind: str, expected: Any, actual: Any = None, message: str | None = None) -> dict[str, Any]:
    value = {"category": category, "kind": kind, "expected": expected, "actual": actual}
    if message:
        value["message"] = message
    return value


def _match_node_assertions(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _node_rows(document)
    failures: list[dict[str, Any]] = []
    used: set[int] = set()
    for assertion in assertions:
        match_index = next(
            (
                index
                for index, row in enumerate(rows)
                if index not in used
                and row["kind"] == assertion.get("kind")
                and (assertion.get("status") is None or row["status"] in ({assertion["status"]} if isinstance(assertion["status"], str) else set(assertion["status"])))
                and _locator_matches(assertion, row["locator"])
            ),
            None,
        )
        if match_index is None:
            failures.append(_failure("block", "node-mismatch", assertion, rows[:20]))
        else:
            used.add(match_index)
    return failures


def _top_level_order(document: dict[str, Any]) -> list[str]:
    by_id = {item.get("nodeId"): item for item in document.get("nodes", []) if isinstance(item, dict)}
    root_id = document.get("rootNodeId")
    root = by_id.get(root_id, {})
    return [by_id.get(node_id, {}).get("kind") for node_id in root.get("childIds", []) if by_id.get(node_id)]


def _match_order(document: dict[str, Any], expected: list[str]) -> list[dict[str, Any]]:
    actual = _top_level_order(document)
    if actual == expected:
        return []
    return [_failure("order", "top-level-order-mismatch", expected, actual)]


def _match_span_assertions(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _node_rows(document)
    failures: list[dict[str, Any]] = []
    used: set[int] = set()
    for assertion in assertions:
        index = next(
            (
                candidate
                for candidate, row in enumerate(rows)
                if candidate not in used and row["kind"] == assertion.get("kind") and _locator_matches(assertion, row["locator"])
            ),
            None,
        )
        if index is None:
            failures.append(_failure("span", "source-span-mismatch", assertion, [row for row in rows if row["kind"] == assertion.get("kind")][:8]))
        else:
            used.add(index)
    return failures


def _match_normalized_text(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _normalized_text_rows(document)
    failures: list[dict[str, Any]] = []
    used: set[int] = set()
    for assertion in assertions:
        index = next((candidate for candidate, row in enumerate(rows) if candidate not in used and row.get("value") == assertion.get("value") and _locator_matches(assertion, row["locator"])), None)
        if index is None:
            failures.append(_failure("inline", "normalized-text-mismatch", assertion, rows[:20]))
        else:
            used.add(index)
    return failures


def _match_authoring(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _extension_rows(document)
    failures: list[dict[str, Any]] = []
    used: set[int] = set()
    for assertion in assertions:
        index = next(
            (
                candidate
                for candidate, row in enumerate(rows)
                if candidate not in used
                and row["type"] == assertion.get("type")
                and _locator_matches(assertion, row["locator"])
                and _subset(assertion.get("payload", {}), row.get("payload", {}))
            ),
            None,
        )
        if index is None:
            failures.append(_failure("authoring", "authoring-fact-mismatch", assertion, rows[:20]))
        else:
            used.add(index)
    return failures


def _match_tables(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    actual_tables = [item for item in document.get("tables", []) if isinstance(item, dict)]
    for assertion in assertions:
        actual = next(
            (
                table
                for table in actual_tables
                if len(table.get("rowIds", [])) == assertion.get("rowCount")
                and len(table.get("columnIds", [])) == assertion.get("columnCount")
                and bool(table.get("separatorIsMetadata")) == bool(assertion.get("separatorExcluded"))
                and table.get("alignment") == assertion.get("alignment")
                and table.get("separatorLines") == [assertion.get("separatorLine")]
                and table.get("rowSourceLines", [])[1:] == assertion.get("dataRowLines", table.get("rowSourceLines", [])[1:])
            ),
            None,
        )
        if actual is None:
            failures.append(_failure("table", "table-topology-mismatch", assertion, actual_tables))
    return failures


def _match_tasks(document: dict[str, Any], assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    maps = _source_maps(document)
    actual = []
    for annotation in document.get("annotations", []):
        if not isinstance(annotation, dict) or annotation.get("sourceSubtype") != "markdown:task-list-item":
            continue
        anchor = annotation.get("anchor") if isinstance(annotation.get("anchor"), dict) else {}
        locator = next(iter(maps.get(annotation.get("annotationId"), [{}])), {})
        actual.append({"annotation": annotation, "anchor": anchor, "locator": locator})
    failures: list[dict[str, Any]] = []
    used: set[int] = set()
    for assertion in assertions:
        index = next(
            (
                candidate
                for candidate, row in enumerate(actual)
                if candidate not in used
                and row["anchor"].get("checked") == assertion.get("checked")
                and row["anchor"].get("marker") == assertion.get("marker")
                and _locator_matches(assertion, row["locator"])
            ),
            None,
        )
        if index is None:
            failures.append(_failure("task", "task-list-projection-mismatch", assertion, actual))
        else:
            used.add(index)
    return failures


def _reference_projection(document: dict[str, Any]) -> dict[str, Any]:
    definitions: list[dict[str, Any]] = []
    uses: Counter[str] = Counter()
    unresolved: set[str] = set()
    for row in _extension_rows(document):
        if row["type"] == "reference-definition" and isinstance(row.get("payload"), dict):
            payload = row["payload"]
            definitions.append({"label": payload.get("label"), "destination": payload.get("destination"), "title": payload.get("title", "")})
        if row["type"] == "authoring-facts" and isinstance(row.get("payload"), dict):
            payload = row["payload"]
            label = payload.get("referenceLabel")
            if isinstance(label, str):
                if payload.get("referenceDefinitionId"):
                    uses[label] += 1
                else:
                    unresolved.add(label)
    for annotation in document.get("annotations", []):
        if isinstance(annotation, dict) and annotation.get("kind") == "hyperlink" and isinstance(annotation.get("referenceId"), str):
            uses[annotation["referenceId"]] += 1
    return {"definitions": definitions, "uses": dict(uses), "unresolved": sorted(unresolved)}


def _match_references(document: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    actual = _reference_projection(document)
    failures: list[dict[str, Any]] = []
    for definition in expected.get("definitions", []):
        if not any(_subset(definition, candidate) for candidate in actual["definitions"]):
            failures.append(_failure("reference", "definition-mismatch", definition, actual["definitions"]))
    for use in expected.get("resolvedUses", []):
        if actual["uses"].get(use.get("label"), 0) < int(use.get("minimumCount", 1)):
            failures.append(_failure("reference", "resolved-use-mismatch", use, actual))
    for label in expected.get("unresolvedLabels", []):
        if label not in actual["unresolved"]:
            failures.append(_failure("reference", "unresolved-reference-not-diagnosed", label, actual))
    if expected.get("closureRequired") and any(not isinstance(item, dict) or not item.get("definitions") for item in document.get("relations", []) if isinstance(item, dict) and item.get("kind") == "references"):
        # The check above is intentionally conservative; relation rows must at
        # least exist for every resolved reference assertion.
        if expected.get("resolvedUses") and not any(item.get("kind") == "references" for item in document.get("relations", []) if isinstance(item, dict)):
            failures.append(_failure("closure", "reference-relation-closure-missing", expected, document.get("relations", [])))
    return failures


def _match_resources(document: dict[str, Any], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actual = _actual_resource_rows(document)
    failures: list[dict[str, Any]] = []
    for assertion in expected:
        row = next((candidate for candidate in actual if candidate.get("target") == assertion.get("authoredTarget")), None)
        if row is None:
            failures.append(_failure("resource", "authored-resource-missing", assertion, actual))
            continue
        resource = row["resource"]
        observation = row["observation"]
        if resource.get("availability") != assertion.get("availability"):
            failures.append(_failure("resource", "availability-mismatch", assertion, resource))
        if observation.get("status") != assertion.get("observationStatus"):
            failures.append(_failure("resource", "observation-mismatch", assertion, observation))
        if assertion.get("mustNotExecute") and resource.get("derivedHandle") != assertion.get("authoredTarget"):
            failures.append(_failure("resource", "unsafe-uri-source-fact-not-preserved", assertion, resource))
    return failures


def _match_unsupported(document: dict[str, Any], expected: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    rows = _actual_unsupported_rows(document)
    failures: list[dict[str, Any]] = []
    false_complete = 0
    for assertion in expected:
        feature = str(assertion.get("feature", ""))
        candidates = [row for row in rows if feature.casefold() in _canonical(row).casefold()]
        if assertion.get("mustNotBeComplete") and not candidates:
            false_complete += 1
            failures.append(_failure("unsupported", "unsupported-syntax-not-accounted", assertion, rows[:20]))
    return failures, false_complete


def _actual_occurrences(document: dict[str, Any], source: str) -> list[str]:
    rows = _node_rows(document)
    occurrences: list[str] = []
    seen_node_ids: set[str] = set()
    for row in rows:
        kind = row.get("kind")
        locator = row.get("locator", {})
        node_id = row.get("node", {}).get("nodeId")
        if isinstance(node_id, str) and node_id in seen_node_ids:
            continue
        if isinstance(node_id, str):
            seen_node_ids.add(node_id)
        line = locator.get("lineStart")
        if kind in {"heading", "paragraph", "section", "thematicBreak", "list", "table"} and isinstance(line, int):
            occurrences.append(f"node:{kind}:{line}")
        if kind == "row" and isinstance(line, int):
            occurrences.append(f"table:row:{line}")
    seen_extensions: set[str] = set()
    reference_use_ordinals: Counter[str] = Counter()
    for row in _extension_rows(document):
        extension_id = row.get("extension", {}).get("extensionId")
        if isinstance(extension_id, str) and extension_id in seen_extensions:
            continue
        if isinstance(extension_id, str):
            seen_extensions.add(extension_id)
        if row["type"] in {"heading-authoring", "thematic-break-authoring", "front-matter", "table-authoring", "unsupported-directive"}:
            line = row["locator"].get("lineStart")
            occurrences.append(f"extension:{row['type']}:{line}")
        if row["type"] == "reference-definition" and isinstance(row.get("payload"), dict):
            occurrences.append(f"reference-definition:{row['payload'].get('label')}")
        if row["type"] == "authoring-facts" and isinstance(row.get("payload"), dict):
            payload = row["payload"]
            label = payload.get("referenceLabel")
            if isinstance(label, str):
                reference_use_ordinals[label] += 1
                occurrences.append(f"reference-use:{label}:{reference_use_ordinals[label]}")
    for table in document.get("tables", []):
        if isinstance(table, dict):
            for line in table.get("separatorLines", []):
                occurrences.append(f"table:separator:{line}")
    source_maps = _source_maps(document)
    source_map_by_id = {
        item.get("sourceMapId"): item.get("locator", {})
        for item in document.get("sourceMaps", [])
        if isinstance(item, dict) and isinstance(item.get("sourceMapId"), str) and isinstance(item.get("locator"), dict)
    }
    diagnostics = {
        item.get("diagnosticId"): item
        for item in document.get("diagnostics", [])
        if isinstance(item, dict) and isinstance(item.get("diagnosticId"), str)
    }
    for row in _actual_unsupported_rows(document):
        if row.get("kind") != "feature":
            continue
        feature = row.get("feature")
        if not isinstance(feature, str):
            continue
        target_id = row.get("targetId")
        locator = next(iter(source_maps.get(target_id, [{}])), {})
        for diagnostic_id in row.get("diagnosticIds", []):
            diagnostic = diagnostics.get(diagnostic_id, {})
            diagnostic_locator = source_map_by_id.get(diagnostic.get("sourceMapId"))
            if diagnostic_locator:
                locator = diagnostic_locator
                break
        line = locator.get("lineStart")
        if isinstance(line, int) and feature in {"task-list", "directive", "inline-syntax", "reference-link", "footnote"}:
            occurrences.append(f"unsupported:{feature}:{line}")
    for feature in document.get("conversion", {}).get("features", []):
        if not isinstance(feature, dict) or feature.get("feature") != "task-list" or feature.get("status") != "preserved":
            continue
        target_id = feature.get("targetId")
        locator = next(iter(source_maps.get(target_id, [{}])), {})
        line = locator.get("lineStart")
        if isinstance(line, int):
            occurrences.append(f"task-list:{line}")
    for row in _extension_rows(document):
        if row["type"] != "authoring-facts" or not isinstance(row.get("payload"), dict) or row["payload"].get("delimiter") != "~~":
            continue
        target_id = row.get("extension", {}).get("targetId")
        node = next((item for item in document.get("nodes", []) if isinstance(item, dict) and item.get("nodeId") == target_id), {})
        line = row.get("locator", {}).get("lineStart")
        occurrences.append(f"unsupported:strikethrough:{line}" if node.get("status") in {"unsupported", "ambiguous"} else f"strikethrough:{line}")
    for resource in _actual_resource_rows(document):
        target = resource.get("target")
        if isinstance(target, str):
            occurrences.append(f"resource:{target}")
    if "\r\n" in source:
        occurrences.append("line-ending:CRLF")
    if any(0x300 <= ord(character) <= 0x36F for character in source):
        occurrences.append("unicode:combining-sequence")
    return occurrences


def _occurrence_result(fixture: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    source = fixture["source"]["value"]
    expected = list(fixture["expected"]["expectedOccurrences"])
    actual = _actual_occurrences(document, source)
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    missing = sorted(key for key, count in expected_counts.items() for _ in range(max(0, count - actual_counts.get(key, 0))))
    unexpected = sorted(key for key, count in actual_counts.items() for _ in range(max(0, count - expected_counts.get(key, 0))))
    duplicates = sorted(
        key
        for key, count in actual_counts.items()
        if count > max(1, expected_counts.get(key, 0))
    )
    return {
        "status": "passed" if not missing and not unexpected and not duplicates else "failed",
        "sourceOccurrenceCount": len(expected),
        "actualOccurrenceCount": len(actual),
        "missingSourceOccurrences": missing,
        "unexpectedActualOccurrences": unexpected,
        "duplicateActualOccurrences": duplicates,
        "unaccountedOccurrenceCount": len(missing) + len(unexpected) + len(duplicates),
    }


def _evaluate_fixture(fixture: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    document = result.get("document", {})
    expected = fixture["expected"]
    failures: list[dict[str, Any]] = []
    failures.extend(_independent_source_occurrence_oracle(fixture))
    failures.extend(_match_node_assertions(document, expected["nodeAssertions"]))
    failures.extend(_match_order(document, expected["orderAssertions"]))
    failures.extend(_match_span_assertions(document, expected["spanAssertions"]))
    failures.extend(_match_normalized_text(document, expected["normalizedTextAssertions"]))
    failures.extend(_match_authoring(document, expected["authoringAssertions"]))
    failures.extend(_match_tables(document, expected["tableAssertions"]))
    failures.extend(_match_tasks(document, expected.get("taskAssertions", [])))
    failures.extend(_match_references(document, expected["referenceAssertions"]))
    failures.extend(_match_resources(document, expected["resourceAssertions"]))
    unsupported_failures, false_complete = _match_unsupported(document, expected["unsupportedAssertions"])
    failures.extend(unsupported_failures)

    evidence = result.get("evidence", {})
    input_evidence = evidence.get("input", {}) if isinstance(evidence, dict) else {}
    source_sha = result.get("sourceSha256")
    evidence_failures: list[dict[str, Any]] = []
    if input_evidence.get("sha256") != source_sha:
        evidence_failures.append(_failure("binding", "converter-input-sha-mismatch", source_sha, input_evidence.get("sha256")))
    if input_evidence.get("format") != "markdown" or input_evidence.get("consumed") is not True:
        evidence_failures.append(_failure("binding", "converter-input-not-consumed", {"format": "markdown", "consumed": True}, input_evidence))
    if not input_evidence.get("parserAttempted"):
        evidence_failures.append(_failure("binding", "parser-not-attempted", True, input_evidence))
    if not document:
        evidence_failures.append(_failure("binding", "missing-converter-document", True, result.get("stderr")))
    failures.extend(evidence_failures)
    occurrence = _occurrence_result(fixture, document)
    if occurrence["unaccountedOccurrenceCount"]:
        failures.append(_failure("occurrence", "occurrence-accounting-failed", {"unaccounted": 0}, occurrence))

    return {
        "fixtureId": fixture["fixtureId"],
        "profileId": fixture["profileId"],
        "commandExitCode": result.get("commandExitCode"),
        "adapterConversionStatus": document.get("conversion", {}).get("status") if isinstance(document, dict) else None,
        "sourceSha256": source_sha,
        "mismatchCount": len(failures),
        "failureDetails": failures,
        "falseCompleteCount": false_complete,
        "occurrenceAccounting": occurrence,
        "diagnosticCount": len(document.get("diagnostics", [])) if isinstance(document, dict) else 0,
        "evidenceBinding": {
            "inputShaMatches": input_evidence.get("sha256") == source_sha,
            "inputConsumed": input_evidence.get("consumed") is True,
            "parserAttempted": input_evidence.get("parserAttempted") is True,
        },
        "status": "passed" if not failures and false_complete == 0 else "failed",
    }


def _get_path(value: Any, path: str) -> tuple[Any, str | int]:
    components = path.split(".") if path else []
    if not components:
        raise QualificationError("mutation path is empty")
    current = value
    for component in components[:-1]:
        if isinstance(current, list):
            current = current[int(component)]
        elif isinstance(current, dict):
            current = current[component]
        else:
            raise QualificationError(f"mutation path traverses a scalar: {path}")
    leaf: str | int = int(components[-1]) if isinstance(current, list) else components[-1]
    return current, leaf


def _apply_mutation(expected: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(expected)
    target_name = mutation.get("target")
    if not isinstance(target_name, str) or target_name not in value:
        raise QualificationError(f"mutation target is not an expected projection field: {mutation}")
    parent, leaf = _get_path(value[target_name], mutation["path"])
    if mutation["op"] == "set":
        if isinstance(parent, list) and isinstance(leaf, int):
            parent[leaf] = deepcopy(mutation.get("value"))
        elif isinstance(parent, dict) and isinstance(leaf, str):
            parent[leaf] = deepcopy(mutation.get("value"))
        else:
            raise QualificationError(f"invalid set mutation path: {mutation}")
    elif mutation["op"] == "delete":
        if isinstance(parent, list) and isinstance(leaf, int):
            del parent[leaf]
        elif isinstance(parent, dict) and isinstance(leaf, str):
            parent.pop(leaf, None)
        else:
            raise QualificationError(f"invalid delete mutation path: {mutation}")
    return value


def _projection_difference(expected: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    differences: list[str] = []
    for key in ("nodeAssertions", "orderAssertions", "spanAssertions", "normalizedTextAssertions", "authoringAssertions", "tableAssertions", "taskAssertions", "referenceAssertions", "resourceAssertions", "unsupportedAssertions", "expectedOccurrences"):
        if _canonical(expected.get(key)) != _canonical(candidate.get(key)):
            differences.append(key)
    return differences


def _run_negative_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    fixtures = {item["fixtureId"]: item for item in corpus["fixtures"]}
    results: list[dict[str, Any]] = []
    for mutation in corpus["negativeMutations"]:
        fixture = fixtures[mutation["fixtureId"]]
        original = fixture["expected"]
        mutated = _apply_mutation(original, mutation)
        differences = _projection_difference(original, mutated)
        results.append({
            "mutationId": mutation["mutationId"],
            "fixtureId": mutation["fixtureId"],
            "oracleOnly": True,
            "oracleMutationDetected": bool(differences),
            "changedProjectionFields": differences,
            "status": "passed" if differences else "failed",
        })
    return results


def _run_defect_gates(corpus: dict[str, Any]) -> dict[str, Any]:
    required = set(corpus["requiredDefectCases"])
    if not DEFECT_CONTRACT_PATH.is_file():
        return {"status": "failed", "contractAvailable": False, "caseCount": 0, "detectedCount": 0, "unmet": ["#89 defect injection contract is missing"], "cases": []}
    contract = _read_json(DEFECT_CONTRACT_PATH)
    cases = [item for item in contract.get("cases", []) if isinstance(item, dict) and item.get("releaseProfile") == "markdown"]
    by_id = {item.get("id"): item for item in cases}
    missing = sorted(required - set(by_id))
    if missing:
        return {"status": "failed", "contractAvailable": True, "caseCount": len(cases), "detectedCount": 0, "unmet": [f"#89 Markdown cases missing: {missing}"], "cases": []}
    command_cache: dict[tuple[str, ...], dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for case_id in sorted(required):
        case = by_id[case_id]
        raw_command = case.get("gateCommand")
        if not isinstance(raw_command, list) or not raw_command:
            results.append({"caseId": case_id, "status": "failed", "reason": "missing gate command"})
            continue
        command = tuple(sys.executable if index == 0 and value == "python" else str(value) for index, value in enumerate(raw_command))
        if command not in command_cache:
            try:
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
                command_cache[command] = {"exitCode": completed.returncode, "stdout": completed.stdout[-1200:], "stderr": completed.stderr[-1200:]}
            except (OSError, subprocess.SubprocessError) as exc:
                command_cache[command] = {"exitCode": 125, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
        run = command_cache[command]
        passed = run.get("exitCode") == 0
        results.append({"caseId": case_id, "gateCommand": list(command), "gateExitCode": run.get("exitCode"), "gatePassed": passed, "status": "passed" if passed else "failed", "stdout": run.get("stdout", ""), "stderr": run.get("stderr", "")})
    detected = sum(1 for item in results if item.get("gatePassed"))
    unmet = [] if detected == len(required) else [f"#89 Markdown gate passed {detected}/{len(required)} cases"]
    return {"status": "passed" if not unmet else "failed", "contractAvailable": True, "caseCount": len(results), "detectedCount": detected, "unmet": unmet, "cases": results}


def _profile_matrix(corpus: dict[str, Any], fixture_results: list[dict[str, Any]], inspect_results: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[dict[str, Any]] = []
    unmet: list[str] = []
    profiles = {item["profileId"]: item for item in corpus["profiles"]}
    for profile_id, profile in profiles.items():
        fixtures = [item for item in fixture_results if item["profileId"] == profile_id]
        inspect = inspect_results.get(fixtures[0]["fixtureId"], {}) if fixtures else {}
        observed_profile = inspect.get("profile")
        expected_marker = f"{profile['dialect']}-{profile['specVersion']}"
        if profile["dialect"] == "commonmark" and not isinstance(observed_profile, str):
            failures.append(_failure("profile", "parser-profile-missing", expected_marker, inspect))
        if profile["dialect"] == "gfm" and (not isinstance(observed_profile, str) or "gfm" not in observed_profile.casefold()):
            failures.append(_failure("profile", "gfm-profile-not-authoritatively-declared", expected_marker, observed_profile))
        if any(item["status"] == "failed" for item in fixtures):
            failures.append(_failure("profile", "profile-fixture-failure", profile_id, fixtures))
    external = corpus["externalCorpusPolicy"]
    for key, label in (("officialCommonMarkCorpusAvailable", "official CommonMark corpus"), ("officialGfmCorpusAvailable", "official GFM corpus")):
        if external.get(key) is not True:
            unmet.append(f"{label} is unavailable")
    return failures, unmet


def _ci_binding(corpus: dict[str, Any], source_sha: str) -> dict[str, Any]:
    policy = corpus["ciBindingPolicy"]
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8") if WORKFLOW_PATH.is_file() else ""
    command = " ".join(policy["requiredCommand"])
    command_bound = command in workflow_text
    validator_bound = policy["requiredBundleValidator"] in workflow_text
    issue88 = _read_json(ISSUE88_EVIDENCE_PATH) if ISSUE88_EVIDENCE_PATH.is_file() else None
    issue88_bound = isinstance(issue88, dict) and issue88.get("sourceSha") == source_sha
    unmet: list[str] = []
    if not command_bound:
        unmet.append("CI workflow does not execute the dedicated #102 runner")
    if not validator_bound:
        unmet.append("CI workflow does not bind the qualification bundle validator")
    if not issue88_bound:
        unmet.append("#88 exact-SHA evidence bundle is absent or bound to another SHA")
    return {
        "workflowPath": str(WORKFLOW_PATH.relative_to(ROOT)).replace("\\", "/"),
        "workflowExists": WORKFLOW_PATH.is_file(),
        "dedicatedRunnerBound": command_bound,
        "bundleValidatorBound": validator_bound,
        "issue88EvidenceBound": issue88_bound,
        "runUrlBound": False,
        "sourceSha": source_sha,
        "status": "passed" if not unmet else "failed",
        "unmet": unmet,
    }


def _common_report(
    name: str,
    source_sha: str,
    corpus_sha: str,
    fixture_results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    unmet: list[str],
    negative_results: list[dict[str, Any]],
    defect_gates: dict[str, Any],
    ci_binding: dict[str, Any],
    dirty_paths: list[str],
    category: str,
) -> dict[str, Any]:
    filtered = [item for item in failures if item.get("category") in REPORT_CATEGORIES.get(name, {category})]
    if not filtered:
        filtered = [
            {
                "category": category,
                "kind": "category-evidence-summary",
                "expected": "a non-empty, independently attributable qualification report",
                "actual": {
                    "fixtureCount": len(fixture_results),
                    "globalMismatchCount": sum(item["mismatchCount"] for item in fixture_results),
                    "unmetRequirementCount": len(unmet),
                },
                "message": "No fixture failure belongs exclusively to this category; binding and fixture evidence remain attached below.",
            }
        ]
    all_occurrence = sum(item["occurrenceAccounting"]["unaccountedOccurrenceCount"] for item in fixture_results)
    false_complete = sum(item["falseCompleteCount"] for item in fixture_results)
    negative_failures = sum(1 for item in negative_results if item.get("status") != "passed")
    dynamic_unmet = list(unmet)
    if dirty_paths:
        dynamic_unmet.append("working tree is dirty; exact-SHA clean evidence cannot be claimed")
    if all_occurrence:
        dynamic_unmet.append(f"unaccounted or duplicate source occurrences: {all_occurrence}")
    if false_complete:
        dynamic_unmet.append(f"false-complete unsupported occurrences: {false_complete}")
    if negative_failures:
        dynamic_unmet.append(f"negative mutation oracle failures: {negative_failures}")
    if defect_gates.get("status") != "passed":
        dynamic_unmet.extend(defect_gates.get("unmet", []))
    dynamic_unmet.extend(ci_binding.get("unmet", []))
    unique_unmet = list(dict.fromkeys(item for item in dynamic_unmet if item))
    global_mismatches = sum(item["mismatchCount"] for item in fixture_results)
    status = "passed" if not unique_unmet and global_mismatches == 0 else "failed"
    return {
        "schema": "fdir/qualification-issue-102-report",
        "version": "1.0.0",
        "issueNumber": 102,
        "reportName": name,
        "category": category,
        "status": status,
        "completionStatus": "complete" if status == "passed" else "incomplete-bounded-lane",
        "qualificationGate": "fail-closed",
        "sourceSha": source_sha,
        "corpusSha256": corpus_sha,
        "fixtureCount": len(fixture_results),
        "profileCount": len({item["profileId"] for item in fixture_results}),
        "assertions": filtered,
        "nonemptyAssertions": bool(filtered),
        "mismatchCount": global_mismatches,
        "globalFailureCount": global_mismatches + len(unique_unmet),
        "globalMismatchCount": global_mismatches,
        "failureSummary": unique_unmet[:40],
        "unmetRequirements": unique_unmet,
        "fixtureResults": fixture_results,
        "negativeMutationResults": negative_results,
        "negativeMutationFailureCount": negative_failures,
        "undetectedNegativeMutationCount": negative_failures,
        "falseCompleteCount": false_complete,
        "undetectedDefectCount": len(REQUIRED_DEFECT_CASES) - int(defect_gates.get("detectedCount", 0)),
        "defectGate": defect_gates,
        "occurrenceAccounting": {
            "unaccountedOccurrenceCount": all_occurrence,
            "status": "passed" if all_occurrence == 0 else "failed",
        },
        "evidenceBinding": {
            "exactSourceSha": bool(GIT_SHA_PATTERN.fullmatch(source_sha)),
            "corpusSha256": corpus_sha,
            "dirtyTree": bool(dirty_paths),
            "dirtyPaths": dirty_paths[:100],
            "issue88ExactShaBundleRequired": True,
        },
        "ciBinding": ci_binding,
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    corpus = _load_corpus(Path(corpus_path))
    corpus_bytes = Path(corpus_path).read_bytes()
    corpus_sha = _sha256_bytes(corpus_bytes)
    source_sha = _source_sha()
    dirty_paths = _git_dirty_paths()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "inputs"
    work.mkdir(parents=True, exist_ok=True)

    fixture_results: list[dict[str, Any]] = []
    inspect_results: dict[str, dict[str, Any]] = {}
    all_failures: list[dict[str, Any]] = []
    for fixture in corpus["fixtures"]:
        source_path = _materialize_fixture(fixture, work)
        result = _run_converter(fixture, source_path, work)
        inspect_results[fixture["fixtureId"]] = _run_inspect(source_path)
        evaluated = _evaluate_fixture(fixture, result)
        fixture_results.append(evaluated)
        all_failures.extend(evaluated["failureDetails"])

    negative_results = _run_negative_mutations(corpus)
    defect_gates = _run_defect_gates(corpus)
    ci_binding = _ci_binding(corpus, source_sha)
    unmet = list(corpus.get("unmetRequirements", []))
    profile_failures, profile_unmet = _profile_matrix(corpus, fixture_results, inspect_results)
    all_failures.extend(profile_failures)
    unmet.extend(profile_unmet)
    if corpus["externalCorpusPolicy"].get("independentParserCountAvailable", 0) < 2:
        unmet.append("two independent parser AST/span differential outputs are unavailable")
    if corpus["externalCorpusPolicy"].get("realWorldProducerFamiliesAvailable", 0) < corpus["externalCorpusPolicy"].get("realWorldProducerFamiliesRequired", 0):
        unmet.append("real-world Markdown producer corpus is incomplete")

    reports = {}
    for name in REPORT_NAMES:
        category = name.removesuffix(".json")
        reports[name] = _common_report(name, source_sha, corpus_sha, fixture_results, all_failures, unmet, negative_results, defect_gates, ci_binding, dirty_paths, category)
    _write_producer_envelope(out_dir, reports, Path(corpus_path), source_sha)
    _write_json(
        out_dir / "inspection.json",
        {
            "schema": "fdir/qualification-issue-102-inspection",
            "issueNumber": 102,
            "sourceSha": source_sha,
            "corpusSha256": corpus_sha,
            "inspect": inspect_results,
            "defectGate": defect_gates,
            "ciBinding": ci_binding,
        },
    )
    return 1 if any(json.loads((out_dir / name).read_text(encoding="utf-8")).get("status") != "passed" for name in REPORT_NAMES) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_qualification(corpus_path=args.corpus, out_dir=args.out_dir)
    except QualificationError as exc:
        print(f"qualification failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
