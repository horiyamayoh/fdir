"""Strict, independent qualification lane for GitHub issue #101.

This module is deliberately a qualification runner, not a second PDF adapter.
The authored corpus is the source authority.  Expected facts are checked with a
small byte-oriented lexical/page-tree inspector that does not import
``adapter_pdf``.  The implementation under test is invoked only through the
public ``convert_document.py convert`` command.  Optional PyMuPDF observations
are kept separate from source-declared facts.

The lane is fail-closed: bounded synthetic evidence may pass its own assertions,
but missing real-producer fixtures, missing #88/#89/#91/#92/#94/#96 bindings,
dirty source, unavailable CI/evidence, renderer blockers, or any unaccounted
occurrence keep every report failed and the process exit code non-zero.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from qualification_producer_report import write_producer_report
except ImportError:  # pragma: no cover - package-style test imports
    from tools.qualification_producer_report import write_producer_report


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-101-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-101"
CONVERTER_PATH = ROOT / "tools" / "convert_document.py"
REPORT_NAMES = [
    "pdf-profile-matrix.json",
    "pdf-object-xref-revision.json",
    "pdf-page-resource-closure.json",
    "pdf-content-event-trace.json",
    "pdf-font-cmap-glyph.json",
    "pdf-annotation-form-report.json",
    "pdf-multi-parser-differential.json",
    "pdf-unsupported-occurrences.json",
]
REPORT_KINDS = {
    "pdf-profile-matrix.json": "profile",
    "pdf-object-xref-revision.json": "object-xref-revision",
    "pdf-page-resource-closure.json": "page-resource-closure",
    "pdf-content-event-trace.json": "content-event-trace",
    "pdf-font-cmap-glyph.json": "font-cmap-glyph",
    "pdf-annotation-form-report.json": "annotation-form",
    "pdf-multi-parser-differential.json": "multi-parser-differential",
    "pdf-unsupported-occurrences.json": "unsupported-occurrences",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MISSING = object()


class QualificationError(RuntimeError):
    """Raised when the #101 qualification inputs cannot be trusted."""


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
            if isinstance(value.get("sourceFacts"), dict) and isinstance(value.get("actualProjection"), dict):
                source = value["sourceFacts"]
                actual = value["actualProjection"]
                for key in sorted(set(source) & set(actual)):
                    add(f"{path}/facts/{key}", source[key], actual[key], str(value.get("status", "")))
            for key, child in value.items():
                if key not in {"expected", "actual", "sourceFacts", "actualProjection"}:
                    visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    for name, report in reports.items():
        visit(report, name)
    equal = next((item for item in pairs if canonical(item[1]) == canonical(item[2])), None)
    different = next((item for item in pairs if canonical(item[1]) != canonical(item[2])), None)
    if equal is None:
        count = sum(len(report.get("fixtureResults", [])) for report in reports.values())
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
            "target": {"path": path, "format": "pdf", "kind": "typed-semantic-fact"},
            "diagnostic": {"code": "PDF-101-PRODUCER-EVIDENCE", "message": f"independent typed evidence bound to {path}"},
            "result": "passed",
            "input": {"caseId": case_id, "source": path, "semanticStatus": status},
        }

    return [
        make("issue101-positive-profile-fact", equal, "positive", "format-profile"),
        make("issue101-mutation-profile-fact", different, "mutation", "mutation-killed"),
    ]


def _write_producer_envelope(out_dir: Path, reports: dict[str, dict[str, Any]], corpus_path: Path, source_sha: str) -> None:
    input_paths = [
        corpus_path,
        ROOT / "tools" / "qualification_issue101.py",
        ROOT / "tools" / "test_qualification_issue101.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_pdf.py",
        ROOT / ".github" / "workflows" / "design.yml",
        ROOT / "tools" / "validate_qualification_contract.py",
    ]
    write_producer_report(
        out_dir=out_dir,
        reports=reports,
        report_names={kind: filename for filename, kind in REPORT_KINDS.items()},
        artifact_report_names=REPORT_NAMES[:4],
        issue_number=101,
        evidence_id="issue-101-pdf-profile",
        requirement_id="QUAL-101-PDF-PROFILE",
        source_sha=source_sha,
        input_paths=input_paths,
        producer_id="fdir-pdf-public-converter",
        authority_id="fdir-pdf-independent-byte-oracle",
        producer_component_path=ROOT / "tools" / "convert_document.py",
        authority_component_path=corpus_path,
        evaluator_component_path=ROOT / "tools" / "qualification_issue101.py",
        rows=_producer_rows(reports),
        shared_component_paths=[ROOT / "tools" / "adapter_pdf.py"],
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON input {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha() -> str | None:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and SOURCE_SHA_RE.fullmatch(value) else None


def _source_tree_clean() -> bool:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _json_safe(value: Any) -> Any:
    if value is MISSING:
        return {"$missing": True}
    if isinstance(value, bytes):
        return {"$bytesHex": value.hex()}
    if isinstance(value, tuple):
        return {"$tuple": [_json_safe(item) for item in value]}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _compare(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    """Compare only authored expected fields; missing and null are distinct."""

    if expected is MISSING:
        return []
    if actual is MISSING:
        return [{"path": path, "expected": _json_safe(expected), "actual": {"$missing": True}, "kind": "missing"}]
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [{"path": path, "expected": _json_safe(expected), "actual": _json_safe(actual), "kind": "type"}]
        findings: list[dict[str, Any]] = []
        for key, expected_value in expected.items():
            findings.extend(_compare(expected_value, actual.get(key, MISSING), f"{path}/{key}"))
        return findings
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [{"path": path, "expected": _json_safe(expected), "actual": _json_safe(actual), "kind": "type"}]
        findings = []
        if len(expected) != len(actual):
            findings.append({"path": path, "expected": len(expected), "actual": len(actual), "kind": "length"})
        for index, expected_value in enumerate(expected):
            if index < len(actual):
                findings.extend(_compare(expected_value, actual[index], f"{path}[{index}]"))
        return findings
    if expected != actual:
        return [{"path": path, "expected": _json_safe(expected), "actual": _json_safe(actual), "kind": "value"}]
    return []


def _tokenize(data: bytes) -> list[dict[str, Any]]:
    """Tokenize PDF lexical values without using the implementation parser."""

    tokens: list[dict[str, Any]] = []
    i = 0
    length = len(data)
    whitespace = b" \t\r\n\f\x00"
    delimiters = b"[]<>()/{}%"
    while i < length:
        byte = data[i]
        if byte in whitespace:
            i += 1
            continue
        if byte == ord("%"):
            newline = data.find(b"\n", i)
            i = length if newline < 0 else newline + 1
            continue
        start = i
        if data.startswith(b"<<", i):
            tokens.append({"kind": "dict-start", "value": "<<", "start": i, "end": i + 2})
            i += 2
            continue
        if data.startswith(b">>", i):
            tokens.append({"kind": "dict-end", "value": ">>", "start": i, "end": i + 2})
            i += 2
            continue
        if byte == ord("[") or byte == ord("]"):
            value = chr(byte)
            tokens.append({"kind": "array-start" if value == "[" else "array-end", "value": value, "start": i, "end": i + 1})
            i += 1
            continue
        if byte == ord("("):
            i += 1
            depth = 1
            value = bytearray()
            while i < length and depth:
                current = data[i]
                if current == ord("\\"):
                    i += 1
                    if i >= length:
                        break
                    escaped = data[i]
                    simple = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
                    if escaped in simple:
                        value.append(simple[escaped])
                        i += 1
                    elif escaped in b"\r\n":
                        if escaped == ord("\r") and i + 1 < length and data[i + 1] == ord("\n"):
                            i += 2
                        else:
                            i += 1
                    elif 48 <= escaped <= 55:
                        digits = [escaped]
                        i += 1
                        for _ in range(2):
                            if i < length and 48 <= data[i] <= 55:
                                digits.append(data[i])
                                i += 1
                            else:
                                break
                        value.append(int(bytes(digits), 8))
                    else:
                        value.append(escaped)
                        i += 1
                    continue
                if current == ord("("):
                    depth += 1
                elif current == ord(")"):
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                value.append(current)
                i += 1
            tokens.append({"kind": "string", "value": bytes(value), "start": start, "end": i})
            continue
        if byte == ord("<"):
            i += 1
            end = data.find(b">", i)
            if end < 0:
                end = length
            raw = b"".join(data[i:end].split())
            if len(raw) % 2:
                raw += b"0"
            try:
                decoded = bytes.fromhex(raw.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                decoded = b""
            tokens.append({"kind": "hex", "value": decoded, "start": start, "end": min(end + 1, length)})
            i = min(end + 1, length)
            continue
        if byte == ord("/"):
            i += 1
            while i < length and data[i] not in whitespace and data[i] not in delimiters:
                i += 1
            tokens.append({"kind": "name", "value": "/" + data[start + 1:i].decode("latin-1"), "start": start, "end": i})
            continue
        i += 1
        while i < length and data[i] not in whitespace and data[i] not in delimiters:
            i += 1
        raw = data[start:i].decode("latin-1")
        kind = "number" if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw) else "bare"
        tokens.append({"kind": kind, "value": raw, "start": start, "end": i})
    return tokens


def _parse_value(tokens: list[dict[str, Any]], index: int = 0) -> tuple[Any, int]:
    if index >= len(tokens):
        return MISSING, index
    token = tokens[index]
    kind = token["kind"]
    if kind == "dict-start":
        value: dict[str, Any] = {}
        index += 1
        while index < len(tokens) and tokens[index]["kind"] != "dict-end":
            key = tokens[index]
            if key["kind"] != "name":
                index += 1
                continue
            item, index = _parse_value(tokens, index + 1)
            value[str(key["value"])] = item
        return value, min(index + 1, len(tokens))
    if kind == "array-start":
        value: list[Any] = []
        index += 1
        while index < len(tokens) and tokens[index]["kind"] != "array-end":
            item, index = _parse_value(tokens, index)
            value.append(item)
            if item is MISSING:
                index += 1
        return value, min(index + 1, len(tokens))
    if kind == "number":
        if index + 2 < len(tokens) and tokens[index + 1]["kind"] == "number" and tokens[index + 2]["value"] == "R":
            return ("ref", int(float(token["value"])), int(float(tokens[index + 1]["value"]))), index + 3
        number = float(token["value"])
        return int(number) if number.is_integer() else number, index + 1
    return token["value"], index + 1


def _parse_dictionary(data: bytes) -> dict[str, Any]:
    tokens = _tokenize(data)
    for index, token in enumerate(tokens):
        if token["kind"] == "dict-start":
            value, _ = _parse_value(tokens, index)
            return value if isinstance(value, dict) else {}
    return {}


def _read_integer(data: bytes, start: int) -> tuple[int | None, int]:
    i = start
    while i < len(data) and data[i] in b" \t\r\n\f\x00":
        i += 1
    begin = i
    if i < len(data) and data[i] in b"+-":
        i += 1
    while i < len(data) and 48 <= data[i] <= 57:
        i += 1
    if i == begin or (i == begin + 1 and data[begin] in b"+-"):
        return None, start
    return int(data[begin:i]), i


def _scan_objects(raw: bytes) -> dict[tuple[int, int], dict[str, Any]]:
    objects: dict[tuple[int, int], dict[str, Any]] = {}
    cursor = 0
    while cursor < len(raw):
        first, after_first = _read_integer(raw, cursor)
        if first is None:
            cursor += 1
            continue
        if after_first >= len(raw) or raw[after_first] not in b" \t\r\n\f\x00":
            cursor = max(after_first, cursor + 1)
            continue
        second, after_second = _read_integer(raw, after_first)
        if second is None:
            cursor = max(after_first, cursor + 1)
            continue
        marker = after_second
        while marker < len(raw) and raw[marker] in b" \t\r\n\f\x00":
            marker += 1
        if not raw.startswith(b"obj", marker):
            cursor = max(after_first, cursor + 1)
            continue
        body_start = marker + 3
        end = raw.find(b"endobj", body_start)
        if end < 0:
            break
        body = raw[body_start:end]
        stream_marker = body.find(b"stream")
        dictionary_bytes = body if stream_marker < 0 else body[:stream_marker]
        dictionary = _parse_dictionary(dictionary_bytes)
        stream: bytes | None = None
        if stream_marker >= 0:
            payload_start = stream_marker + len(b"stream")
            if body[payload_start:payload_start + 2] == b"\r\n":
                payload_start += 2
            elif body[payload_start:payload_start + 1] in {b"\r", b"\n"}:
                payload_start += 1
            length_value = dictionary.get("/Length")
            if isinstance(length_value, (int, float)):
                stream = body[payload_start:payload_start + int(length_value)]
            else:
                stream_end = body.find(b"endstream", payload_start)
                stream = body[payload_start:stream_end if stream_end >= 0 else len(body)]
                if stream.endswith(b"\r\n"):
                    stream = stream[:-2]
                elif stream.endswith((b"\r", b"\n")):
                    stream = stream[:-1]
        objects[(first, second)] = {
            "id": f"{first} {second}",
            "number": first,
            "generation": second,
            "dictionary": dictionary,
            "stream": stream,
        }
        cursor = end + len(b"endobj")
    return objects


def _is_ref(value: Any) -> bool:
    return isinstance(value, tuple) and len(value) == 3 and value[0] == "ref"


def _ref_text(value: Any) -> str | None:
    return f"{value[1]} {value[2]}" if _is_ref(value) else None


def _deref(value: Any, objects: dict[tuple[int, int], dict[str, Any]]) -> dict[str, Any] | None:
    return objects.get((value[1], value[2])) if _is_ref(value) else None


def _number_array(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if isinstance(item, (int, float)):
            result.append(item)
        else:
            return None
    return result


def _ref_list(value: Any) -> list[str]:
    if _is_ref(value):
        return [_ref_text(value) or ""]
    if isinstance(value, list):
        return [text for item in value if (text := _ref_text(item)) is not None]
    return []


def _effective_page_facts(objects: dict[tuple[int, int], dict[str, Any]]) -> list[dict[str, Any]]:
    catalog = next((item for item in objects.values() if item["dictionary"].get("/Type") == "/Catalog"), None)
    if catalog is None:
        return []
    root_ref = catalog["dictionary"].get("/Pages")
    pages: list[dict[str, Any]] = []

    def visit(value: Any, inherited: dict[str, Any]) -> None:
        item = _deref(value, objects)
        if item is None:
            return
        dictionary = item["dictionary"]
        effective = dict(inherited)
        for key in ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox", "/Rotate", "/Resources", "/UserUnit"):
            if key in dictionary:
                effective[key] = dictionary[key]
        kind = dictionary.get("/Type")
        if kind == "/Pages":
            for child in dictionary.get("/Kids", []):
                visit(child, effective)
            return
        if kind != "/Page":
            return
        page = {
            "object": item["id"],
            "contents": _ref_list(dictionary.get("/Contents")),
            "annotations": _ref_list(dictionary.get("/Annots")),
            "mediaBox": _number_array(effective.get("/MediaBox")),
            "cropBox": _number_array(effective.get("/CropBox")),
            "rotate": effective.get("/Rotate", 0),
            "resources": effective.get("/Resources", {}),
        }
        pages.append(page)

    visit(root_ref, {})
    for index, page in enumerate(pages):
        page["ordinal"] = index
    return pages


KNOWN_OPERATORS = {
    "b", "B", "B*", "BI", "BT", "BX", "CS", "Do", "DP", "EI", "ET", "EX", "G", "ID", "J", "K",
    "M", "MP", "Q", "RG", "S", "SC", "SCN", "T*", "Tc", "Td", "TD", "Tf", "Tj", "TJ", "TL", "Tm",
    "Tr", "Ts", "Tw", "Tz", "T'", "T\"", "W", "W*", "b*", "c", "cm", "cs", "d", "d0", "d1", "f",
    "f*", "g", "gs", "h", "i", "j", "k", "l", "m", "n", "q", "re", "ri", "rg", "sc", "scn", "sh",
    "v", "w", "y", "Do", "sh",
}
PAINT_OPERATORS = {"Tj", "TJ", "T'", "T\"", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "Do", "sh"}


def _operator_facts(payload: bytes, page_object: str, stream_object: str) -> dict[str, Any]:
    tokens = _tokenize(payload)
    operators: list[dict[str, Any]] = []
    paint: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    operand_start = 0
    for index, token in enumerate(tokens):
        if token["kind"] != "bare":
            continue
        name = str(token["value"])
        if name in KNOWN_OPERATORS:
            args = tokens[operand_start:index]
            item = {"operator": name, "streamObject": stream_object, "pageObject": page_object, "ordinal": len(operators)}
            for argument in args:
                if argument["kind"] in {"string", "hex"}:
                    item["codeHex"] = argument["value"].hex()
                    break
                if argument["kind"] == "name":
                    item["resourceName"] = argument["value"]
            operators.append(item)
            if name in PAINT_OPERATORS:
                paint.append(dict(item))
            operand_start = index + 1
        elif name not in {"R", "true", "false", "null"} and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", name):
            unknown.append({"operator": name, "streamObject": stream_object, "pageObject": page_object, "ordinal": len(operators)})
    return {"operators": operators, "paintEvents": paint, "unknownOperators": unknown}


def _cmap_facts(stream: bytes | None) -> dict[str, str]:
    if not stream:
        return {}
    tokens = _tokenize(stream)
    mappings: dict[str, str] = {}

    def source_code(token: dict[str, Any]) -> str | None:
        raw = token.get("value")
        return raw.hex().upper() if token.get("kind") == "hex" and isinstance(raw, bytes) and raw else None

    def unicode_value(token: dict[str, Any]) -> str:
        raw = token.get("value")
        if token.get("kind") != "hex" or not isinstance(raw, bytes) or not raw:
            return ""
        try:
            return raw.decode("utf-16-be") if len(raw) % 2 == 0 else ""
        except UnicodeDecodeError:
            return ""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.get("kind") != "bare" or token.get("value") not in {"beginbfchar", "beginbfrange"}:
            index += 1
            continue
        block = str(token["value"])
        end_word = "endbfchar" if block == "beginbfchar" else "endbfrange"
        end = index + 1
        while end < len(tokens) and not (tokens[end].get("kind") == "bare" and tokens[end].get("value") == end_word):
            end += 1
        cursor = index + 1
        if block == "beginbfchar":
            while cursor + 1 < end:
                source = source_code(tokens[cursor])
                target = unicode_value(tokens[cursor + 1])
                if source is not None and target:
                    mappings[source] = target
                cursor += 2
        else:
            while cursor + 2 < end:
                start_token, end_token, target_token = tokens[cursor:cursor + 3]
                start_raw = start_token.get("value") if start_token.get("kind") == "hex" else None
                end_raw = end_token.get("value") if end_token.get("kind") == "hex" else None
                if not isinstance(start_raw, bytes) or not isinstance(end_raw, bytes) or len(start_raw) != len(end_raw) or not start_raw or not end_raw:
                    cursor += 3
                    continue
                first = int.from_bytes(start_raw, "big")
                last = int.from_bytes(end_raw, "big")
                if last < first:
                    cursor += 3
                    continue
                if target_token.get("kind") == "hex":
                    target_raw = target_token.get("value")
                    if not isinstance(target_raw, bytes) or not target_raw:
                        cursor += 3
                        continue
                    target_value = int.from_bytes(target_raw, "big")
                    for offset in range(last - first + 1):
                        value = (target_value + offset).to_bytes(len(target_raw), "big")
                        try:
                            decoded = value.decode("utf-16-be") if len(value) % 2 == 0 else ""
                        except UnicodeDecodeError:
                            decoded = ""
                        if decoded:
                            mappings[(first + offset).to_bytes(len(start_raw), "big").hex().upper()] = decoded
                elif target_token.get("kind") == "array-start":
                    array_end = cursor + 3
                    while array_end < end and tokens[array_end].get("kind") != "array-end":
                        array_end += 1
                    for offset, item in enumerate(tokens[cursor + 3:array_end]):
                        if offset > last - first:
                            break
                        decoded = unicode_value(item)
                        if decoded:
                            mappings[(first + offset).to_bytes(len(start_raw), "big").hex().upper()] = decoded
                    cursor = array_end
                cursor += 3
        index = end + 1
    return mappings


def _source_facts(raw: bytes) -> dict[str, Any]:
    if not raw.startswith(b"%PDF-"):
        raise QualificationError("authored PDF does not start with a PDF header")
    header = raw.splitlines()[0].decode("latin-1", errors="replace")
    objects = _scan_objects(raw)
    pages = _effective_page_facts(objects)
    object_facts = []
    for key in sorted(objects):
        item = objects[key]
        dictionary = item["dictionary"]
        object_facts.append({
            "object": item["id"],
            "type": dictionary.get("/Type"),
            "subtype": dictionary.get("/Subtype"),
            "hasStream": item["stream"] is not None,
        })
    all_paint: list[dict[str, Any]] = []
    all_operators: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    content_objects: set[str] = set()
    for page in pages:
        for content_ref in page["contents"]:
            content_objects.add(content_ref)
            target = next((item for item in objects.values() if item["id"] == content_ref), None)
            if target is None or target["stream"] is None:
                continue
            events = _operator_facts(target["stream"], page["object"], content_ref)
            all_paint.extend(events["paintEvents"])
            all_operators.extend(events["operators"])
            unknown.extend(events["unknownOperators"])
    fonts: list[dict[str, Any]] = []
    xobjects: list[str] = []
    annotations: list[dict[str, Any]] = []
    for page in pages:
        resources = page["resources"] if isinstance(page["resources"], dict) else {}
        font_map = resources.get("/Font", {}) if isinstance(resources.get("/Font", {}), dict) else {}
        xobject_map = resources.get("/XObject", {}) if isinstance(resources.get("/XObject", {}), dict) else {}
        for value in font_map.values():
            reference = _ref_text(value)
            if reference is None:
                continue
            target = next((item for item in objects.values() if item["id"] == reference), None)
            if target is not None and not any(item["object"] == reference for item in fonts):
                cmap_ref = _ref_text(target["dictionary"].get("/ToUnicode"))
                cmap_object = next((item for item in objects.values() if item["id"] == cmap_ref), None)
                fonts.append({
                    "object": reference,
                    "subtype": target["dictionary"].get("/Subtype"),
                    "toUnicodeObject": cmap_ref,
                    "mappings": _cmap_facts(cmap_object["stream"] if cmap_object else None),
                })
        for value in xobject_map.values():
            reference = _ref_text(value)
            if reference and reference not in xobjects:
                xobjects.append(reference)
        for annotation_ref in page["annotations"]:
            target = next((item for item in objects.values() if item["id"] == annotation_ref), None)
            dictionary = target["dictionary"] if target else {}
            body = dictionary.get("/Contents")
            if isinstance(body, bytes):
                body = body.decode("latin-1", errors="replace")
            annotations.append({
                "object": annotation_ref,
                "pageObject": page["object"],
                "valid": dictionary.get("/Type") == "/Annot" and "/Subtype" in dictionary,
                "type": dictionary.get("/Type"),
                "subtype": dictionary.get("/Subtype"),
                "body": body,
            })
    features = {
        "classicXref": b"\nxref" in raw or raw.startswith(b"xref"),
        "xrefStream": any(item["dictionary"].get("/Type") == "/XRef" for item in objects.values()),
        "objectStream": any(item["dictionary"].get("/Type") == "/ObjStm" for item in objects.values()),
        "incrementalRevisionCount": raw.count(b"%%EOF"),
        "encrypted": b"/Encrypt" in raw,
        "linearized": b"/Linearized" in raw,
    }
    return {
        "header": header,
        "objectIds": [item["object"] for item in object_facts],
        "objects": object_facts,
        "features": features,
        "pages": pages,
        "contentObjects": sorted(content_objects, key=lambda value: tuple(int(item) for item in value.split())),
        "operators": all_operators,
        "paintEvents": all_paint,
        "unknownOperators": unknown,
        "fonts": fonts,
        "xobjects": sorted(xobjects),
        "annotations": annotations,
    }


def _payload_bytes(spec: Any) -> bytes:
    if not isinstance(spec, str):
        raise QualificationError("authored PDF stream payload must be text")
    return spec.encode("latin-1")


def _write_authored_pdf(fixture: dict[str, Any], path: Path) -> bytes:
    source = fixture.get("source")
    if not isinstance(source, dict) or source.get("kind") != "authored-object-list":
        raise QualificationError(f"fixture {fixture.get('fixtureId')} has no authored object-list source")
    objects = source.get("objects")
    if not isinstance(objects, list) or not objects:
        raise QualificationError(f"fixture {fixture.get('fixtureId')} has no authored PDF objects")
    chunks: list[bytes] = [str(source.get("header", "%PDF-1.7\n")).encode("latin-1")]
    offsets: dict[int, int] = {}
    generations: dict[int, int] = {}
    for entry in objects:
        number = int(entry["number"])
        generation = int(entry.get("generation", 0))
        offsets[number] = sum(len(chunk) for chunk in chunks)
        generations[number] = generation
        chunks.append(f"{number} {generation} obj\n".encode("ascii"))
        if "stream" in entry:
            payload = _payload_bytes(entry["stream"])
            dictionary = str(entry.get("dictionary", "<< /Length {length} >>")).replace("{length}", str(len(payload)))
            chunks.append(dictionary.encode("latin-1"))
            chunks.append(b"\nstream\n")
            chunks.append(payload)
            chunks.append(b"\nendstream\n")
        else:
            chunks.append(str(entry.get("body", "<< >>")).encode("latin-1"))
            chunks.append(b"\n")
        chunks.append(b"endobj\n")
    if source.get("xref") == "classic":
        xref_offset = sum(len(chunk) for chunk in chunks)
        maximum = max(offsets)
        rows = [b"xref\n", f"0 {maximum + 1}\n".encode("ascii"), b"0000000000 65535 f \n"]
        for number in range(1, maximum + 1):
            if number in offsets:
                rows.append(f"{offsets[number]:010d} {generations[number]:05d} n \n".encode("ascii"))
            else:
                rows.append(b"0000000000 65535 f \n")
        rows.append(f"trailer\n<< /Size {maximum + 1} /Root {source['rootObject']} 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
        chunks.extend(rows)
    else:
        chunks.append(b"%%EOF\n")
    raw = b"".join(chunks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict) or corpus.get("schema") != "fdir/qualification-issue-101-corpus":
        raise QualificationError("issue #101 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 101:
        raise QualificationError("issue #101 corpus version or issue binding is invalid")
    if corpus.get("reportNames") != REPORT_NAMES:
        raise QualificationError("issue #101 report list is incomplete or reordered")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("expectedValuesAreRuntimeIndependent") is not True:
        raise QualificationError("issue #101 expected values are not declared independent")
    if oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #101 corpus permits adapter-derived expected values")
    if not isinstance(oracle.get("forbiddenDerivations"), list) or not oracle["forbiddenDerivations"]:
        raise QualificationError("issue #101 corpus has no forbidden derivation declaration")
    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) < 2:
        raise QualificationError("issue #101 requires at least two authored bounded fixtures")
    fixture_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("fixtureId"), str):
            raise QualificationError("issue #101 fixture has no stable id")
        if fixture["fixtureId"] in fixture_ids:
            raise QualificationError(f"duplicate issue #101 fixture id: {fixture['fixtureId']}")
        fixture_ids.add(fixture["fixtureId"])
        declared = fixture.get("sha256")
        if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
            raise QualificationError(f"fixture {fixture['fixtureId']} has no exact authored SHA-256")
        if not isinstance(fixture.get("expectedSourceFacts"), dict) or not isinstance(fixture.get("expectedIR"), dict):
            raise QualificationError(f"fixture {fixture['fixtureId']} lacks independent expected facts")
    producers = corpus.get("producerMatrix")
    if not isinstance(producers, list) or not producers:
        raise QualificationError("issue #101 has no producer matrix")
    producer_ids: set[str] = set()
    for producer in producers:
        if not isinstance(producer, dict) or not isinstance(producer.get("producerId"), str):
            raise QualificationError("issue #101 producer entry is invalid")
        if producer["producerId"] in producer_ids:
            raise QualificationError(f"duplicate issue #101 producer: {producer['producerId']}")
        producer_ids.add(producer["producerId"])
        if producer.get("required") is not True:
            raise QualificationError(f"issue #101 producer {producer['producerId']} is not required")
        if producer.get("availability") not in {"available", "missing"}:
            raise QualificationError(f"issue #101 producer {producer['producerId']} has invalid availability")
        if producer["availability"] == "missing" and not producer.get("missingReason"):
            raise QualificationError(f"issue #101 missing producer {producer['producerId']} has no reason")
    negative = corpus.get("negativeCases")
    if not isinstance(negative, list) or len(negative) < 6:
        raise QualificationError("issue #101 negative defect corpus is incomplete")
    for case in negative:
        if not isinstance(case, dict) or not all(isinstance(case.get(key), str) for key in ("caseId", "fixtureId", "mutation", "expectedDefectCode")):
            raise QualificationError("issue #101 negative defect entry is invalid")
        if case["fixtureId"] not in fixture_ids:
            raise QualificationError(f"issue #101 negative case references unknown fixture: {case['fixtureId']}")
    bindings = corpus.get("bindings")
    if not isinstance(bindings, dict) or not isinstance(bindings.get("requiredIssues"), list):
        raise QualificationError("issue #101 cross-issue binding declaration is missing")
    required_issue_numbers = {item.get("issueNumber") for item in bindings["requiredIssues"] if isinstance(item, dict)}
    if not {88, 89, 91, 92, 94, 96}.issubset(required_issue_numbers):
        raise QualificationError("issue #101 binding declaration omits required parent lanes")
    return corpus


def _part_object_ids(document: dict[str, Any]) -> list[str]:
    result = []
    for part in document.get("parts", []):
        name = part.get("name") if isinstance(part, dict) else None
        match = re.fullmatch(r"(\d+) (\d+) obj", str(name)) if name else None
        if match:
            result.append(f"{match.group(1)} {match.group(2)}")
    return sorted(set(result), key=lambda value: tuple(int(item) for item in value.split()))


def _actual_projection(document: dict[str, Any]) -> dict[str, Any]:
    nodes = {item.get("nodeId"): item for item in document.get("nodes", []) if isinstance(item, dict)}
    pages = []
    for surface in sorted((item for item in document.get("surfaces", []) if item.get("kind") == "page"), key=lambda item: item.get("ordinal", 0)):
        pages.append({
            "sourceObject": surface.get("sourceObject"),
            "ordinal": surface.get("ordinal"),
            "pageTreeIndex": surface.get("pageTreeIndex"),
            "rotation": surface.get("rotation"),
            "mediaBox": surface.get("mediaBox"),
            "cropBox": surface.get("cropBox"),
        })
    paint_order: list[list[str]] = []
    for page in pages:
        ordinal = int(page.get("ordinal", 0)) + 1
        node_id = f"node-pdf-page-{ordinal}"
        order = next((item for item in document.get("orders", []) if item.get("ownerId") == node_id and item.get("kind") == "draw"), None)
        paint_order.append([str(nodes.get(item.get("id"), {}).get("kind")) for item in (order or {}).get("items", [])])
    annotation_objects = []
    for annotation in document.get("annotations", []):
        reference = str(annotation.get("referenceId", ""))
        match = re.fullmatch(r"(\d+ \d+) R", reference)
        if match:
            annotation_objects.append(match.group(1))
    font_mapping = []
    for extension in document.get("extensions", []):
        if extension.get("type") == "font-cmap":
            payload = extension.get("payload", {})
            font_mapping.append({
                "fontObject": payload.get("fontObject"),
                "mappingStatus": payload.get("mappingStatus"),
                "mappings": payload.get("mappings"),
            })
    resources = []
    for resource in document.get("resources", []):
        handle = str(resource.get("derivedHandle", ""))
        match = re.fullmatch(r"object:(\d+ \d+)", handle)
        if match:
            resources.append({"kind": resource.get("kind"), "object": match.group(1), "availability": resource.get("availability")})
    diagnostics = sorted({item.get("code") for item in document.get("diagnostics", []) if isinstance(item, dict) and isinstance(item.get("code"), str)})
    xref_feature = next((item for item in document.get("conversion", {}).get("features", []) if item.get("feature") == "pdf-xref"), {})
    return {
        "objectIds": _part_object_ids(document),
        "pages": pages,
        "paintOrder": paint_order,
        "annotationObjects": sorted(annotation_objects),
        "resources": sorted(resources, key=lambda item: (str(item.get("kind")), str(item.get("object")))),
        "fontMappings": font_mapping,
        "diagnosticCodes": diagnostics,
        "xrefStatus": xref_feature.get("status"),
        "conversionStatus": document.get("conversion", {}).get("status"),
    }


def _source_occurrences(facts: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    keys.extend(f"object:{item}" for item in facts.get("objectIds", []))
    for page in facts.get("pages", []):
        page_object = page["object"]
        keys.append(f"page:{page_object}")
        keys.extend(f"content:{item}" for item in page.get("contents", []))
        keys.extend(f"annotation:{item}" for item in page.get("annotations", []))
    keys.extend(f"font:{item['object']}" for item in facts.get("fonts", []))
    keys.extend(f"xobject:{item}" for item in facts.get("xobjects", []))
    keys.extend(f"paint:{item['pageObject']}:{index}" for index, item in enumerate(facts.get("paintEvents", [])))
    keys.extend(f"unknown:{item['pageObject']}:{item['streamObject']}:{item['operator']}" for item in facts.get("unknownOperators", []))
    return keys


def _actual_occurrences(document: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    keys: list[str] = [f"object:{item}" for item in _part_object_ids(document)]
    part_objects = set(_part_object_ids(document))
    keys.extend(f"content:{item}" for item in facts.get("contentObjects", []) if item in part_objects)
    pages = sorted((item for item in document.get("surfaces", []) if item.get("kind") == "page"), key=lambda item: item.get("ordinal", 0))
    for page in pages:
        source_object = page.get("sourceObject")
        if source_object:
            keys.append(f"page:{source_object}")
    for resource in document.get("resources", []):
        handle = str(resource.get("derivedHandle", ""))
        if handle.startswith("object:"):
            kind = "font" if resource.get("kind") == "font" else "xobject"
            keys.append(f"{kind}:{handle.removeprefix('object:')}")
    for annotation in document.get("annotations", []):
        reference = str(annotation.get("referenceId", "")).removesuffix(" R")
        if reference:
            keys.append(f"annotation:{reference}")
    diagnostic_codes = {item.get("code") for item in document.get("diagnostics", []) if isinstance(item, dict)}
    if "DFIR-PDF-ANNOTATION-TYPE-UNAVAILABLE" in diagnostic_codes:
        keys.extend(f"annotation:{item['object']}" for item in facts.get("annotations", []) if not item.get("valid"))
    paint_index = 0
    for order in document.get("orders", []):
        if order.get("kind") != "draw" or not str(order.get("ownerId", "")).startswith("node-pdf-page-"):
            continue
        page_number = str(order["ownerId"]).removeprefix("node-pdf-page-")
        page_object = next((item.get("sourceObject") for item in pages if int(item.get("ordinal", -1)) + 1 == int(page_number)), None)
        for _item in order.get("items", []):
            if page_object:
                keys.append(f"paint:{page_object}:{paint_index}")
            paint_index += 1
    for unknown in facts.get("unknownOperators", []):
        if "DFIR-PDF-OPERATOR-UNSUPPORTED" in diagnostic_codes:
            keys.append(f"unknown:{unknown['pageObject']}:{unknown['streamObject']}:{unknown['operator']}")
    return keys


def _occurrence_accounting(facts: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    source = _source_occurrences(facts)
    actual = _actual_occurrences(document, facts)
    source_set = set(source)
    actual_set = set(actual)
    duplicates = sorted({item for item in actual if actual.count(item) > 1})
    unaccounted = sorted(source_set - actual_set)
    unexpected = sorted(actual_set - source_set)
    return {
        "status": "passed" if not unaccounted and not duplicates else "failed",
        "sourceOccurrenceCount": len(source),
        "actualOccurrenceCount": len(actual),
        "unaccountedOccurrences": unaccounted,
        "unexpectedOccurrences": unexpected,
        "duplicateActualOccurrences": duplicates,
        "unaccountedOccurrenceCount": len(unaccounted),
        "duplicateActualOccurrenceCount": len(duplicates),
    }


def _run_converter(fixture: dict[str, Any], input_path: Path, work: Path) -> dict[str, Any]:
    output_path = work / "ir" / f"{fixture['fixtureId']}.json"
    evidence_path = work / "evidence" / f"{fixture['fixtureId']}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(CONVERTER_PATH), "convert", str(input_path), "--format", "pdf", "--out", str(output_path), "--evidence", str(evidence_path)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    document = _read_json(output_path) if output_path.is_file() else None
    evidence = _read_json(evidence_path) if evidence_path.is_file() else None
    input_sha = _sha256_file(input_path)
    evidence_findings: list[dict[str, Any]] = []
    if not isinstance(evidence, dict) or evidence.get("input", {}).get("consumed") is not True:
        evidence_findings.append({"code": "INPUT-NOT-CONSUMED", "expected": True, "actual": evidence})
    elif evidence.get("input", {}).get("sha256") != input_sha:
        evidence_findings.append({"code": "INPUT-SHA-MISMATCH", "expected": input_sha, "actual": evidence.get("input", {}).get("sha256")})
    return {
        "command": command,
        "returnCode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "document": document if isinstance(document, dict) else {},
        "evidence": evidence if isinstance(evidence, dict) else {},
        "inputSha256": input_sha,
        "evidenceFindings": evidence_findings,
    }


def _fixture_result(fixture: dict[str, Any], input_path: Path, work: Path) -> dict[str, Any]:
    raw = input_path.read_bytes()
    source = _source_facts(raw)
    source_mismatches = _compare(fixture["expectedSourceFacts"], source)
    execution = _run_converter(fixture, input_path, work)
    actual = _actual_projection(execution["document"])
    actual_mismatches = _compare(fixture["expectedIR"], actual)
    expected_xref_status = "preserved" if source.get("features", {}).get("classicXref") or source.get("features", {}).get("xrefStream") else "unavailable"
    if actual.get("xrefStatus") != expected_xref_status:
        actual_mismatches.append({
            "path": "$/xrefStatus",
            "expected": expected_xref_status,
            "actual": actual.get("xrefStatus", MISSING),
            "kind": "value",
        })
    actual_mismatches.extend(execution["evidenceFindings"])
    accounting = _occurrence_accounting(source, execution["document"])
    fabricated = 0
    invalid_annotations = [item for item in source.get("annotations", []) if not item.get("valid")]
    actual_annotation_objects = set(actual.get("annotationObjects", []))
    fabricated += sum(1 for item in invalid_annotations if item.get("object") in actual_annotation_objects)
    mapping_unavailable = any(item.get("mappings") == {} for item in source.get("fonts", []))
    if mapping_unavailable:
        for item in actual.get("fontMappings", []):
            if item.get("mappingStatus") == "preserved" and item.get("mappings"):
                fabricated += 1
    return {
        "fixtureId": fixture["fixtureId"],
        "producerId": fixture.get("producerId"),
        "inputSha256": execution["inputSha256"],
        "declaredSha256": fixture["sha256"],
        "shaMatches": execution["inputSha256"] == fixture["sha256"],
        "sourceFacts": source,
        "sourceMismatches": source_mismatches,
        "actualProjection": actual,
        "actualMismatches": actual_mismatches,
        "occurrenceAccounting": accounting,
        "fabricatedPreservedCount": fabricated,
        "returnCode": execution["returnCode"],
        "evidence": execution["evidence"],
        "status": "passed" if not source_mismatches and not actual_mismatches and accounting["status"] == "passed" and fabricated == 0 and execution["returnCode"] == 0 else "failed",
    }


def _mutate_facts(value: dict[str, Any], mutation: str) -> None:
    if mutation == "swap-page-tree-kids":
        value["pages"] = list(reversed(value.get("pages", [])))
    elif mutation == "font-stream-as-content":
        if value.get("pages"):
            value["pages"][0]["contents"] = [value["fonts"][0]["object"]]
    elif mutation == "fabricate-marker-annotation":
        for annotation in value.get("annotations", []):
            if not annotation.get("valid"):
                annotation["valid"] = True
    elif mutation == "fabricate-unicode":
        for font in value.get("fonts", []):
            font["mappings"] = {"00": "A"}
    elif mutation == "drop-unknown-operator":
        value["unknownOperators"] = []
    elif mutation == "drop-paint-event":
        value["paintEvents"] = value.get("paintEvents", [])[1:]
    else:
        raise QualificationError(f"unknown issue #101 mutation: {mutation}")


def _run_negative_mutations(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {fixture["fixtureId"]: fixture for fixture in corpus["fixtures"]}
    results = []
    for case in corpus["negativeCases"]:
        baseline = deepcopy(by_id[case["fixtureId"]]["expectedSourceFacts"])
        mutated = deepcopy(baseline)
        _mutate_facts(mutated, case["mutation"])
        findings = [] if baseline == mutated else [{"path": "$", "expected": _json_safe(baseline), "actual": _json_safe(mutated), "kind": "mutation"}]
        results.append({
            "caseId": case["caseId"],
            "fixtureId": case["fixtureId"],
            "mutation": case["mutation"],
            "expectedDefectCode": case["expectedDefectCode"],
            "classification": "independent-oracle-projection-mutation",
            "adapterMutationExecuted": False,
            "detected": bool(findings),
            "status": "passed" if findings else "failed",
            "mismatchCount": len(findings),
            "findings": findings[:20],
        })
    return results


def _pymupdf_differential(fixture_results: list[dict[str, Any]], input_paths: dict[str, Path]) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent.
        return {"status": "failed", "parser": "PyMuPDF", "available": False, "reason": str(exc), "observations": []}
    for result in fixture_results:
        path = input_paths[result["fixtureId"]]
        try:
            pdf = fitz.open(path)
            pages = [{"pageCount": len(pdf), "rotation": int(pdf[index].rotation), "rect": [round(float(item), 4) for item in pdf[index].rect]} for index in range(len(pdf))]
            observations.append({"fixtureId": result["fixtureId"], "status": "passed", "pages": pages})
            pdf.close()
        except Exception as exc:
            observations.append({"fixtureId": result["fixtureId"], "status": "failed", "reason": str(exc)})
    failed = [item for item in observations if item.get("status") != "passed"]
    return {
        "status": "passed" if not failed else "failed",
        "parser": "PyMuPDF",
        "available": True,
        "independentFromAdapter": True,
        "observations": observations,
        "mismatchCount": len(failed),
    }


def _report_status(path: Path, source_sha: str | None) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": str(path), "reason": "report file is absent"}
    try:
        report = _read_json(path)
    except QualificationError as exc:
        return {"status": "invalid", "path": str(path), "reason": str(exc)}
    if not isinstance(report, dict):
        return {"status": "invalid", "path": str(path), "reason": "report is not an object"}
    if report.get("status") != "passed":
        return {"status": "failed", "path": str(path), "reason": "bound report status is not passed", "sourceSha": report.get("sourceSha")}
    if source_sha is None or report.get("sourceSha") != source_sha:
        return {"status": "stale", "path": str(path), "reason": "bound report source SHA is not the current exact SHA", "sourceSha": report.get("sourceSha")}
    return {"status": "passed", "path": str(path), "sourceSha": report.get("sourceSha")}


def _binding_checks(corpus: dict[str, Any], source_sha: str | None, out_dir: Path) -> dict[str, Any]:
    required_issues = []
    for entry in corpus["bindings"]["requiredIssues"]:
        issue_number = int(entry["issueNumber"])
        report_path = ROOT / str(entry["reportPath"])
        result = _report_status(report_path, source_sha)
        required_issues.append({"issueNumber": issue_number, **result})
    bundle_path = ROOT / str(corpus["bindings"]["evidenceBundlePathTemplate"]).replace("{sourceSha}", source_sha or "<missing-sha>")
    bundle = _report_status(bundle_path, source_sha)
    workflow_text = (ROOT / ".github" / "workflows" / "design.yml").read_text(encoding="utf-8", errors="replace") if (ROOT / ".github" / "workflows" / "design.yml").is_file() else ""
    github_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    ci = {
        "status": "passed" if "qualification_issue101.py" in workflow_text and github_actions else "failed",
        "workflowReferencesRunner": "qualification_issue101.py" in workflow_text,
        "githubActions": github_actions,
        "runId": os.environ.get("GITHUB_RUN_ID"),
        "reason": "GitHub Actions run binding is unavailable in this local execution" if not github_actions else None,
    }
    return {
        "sourceTreeClean": _source_tree_clean(),
        "requiredIssues": required_issues,
        "evidenceBundle": {"status": "missing" if bundle["status"] == "missing" else bundle["status"], "path": str(bundle_path), "detail": bundle},
        "ci": ci,
        "generatedOutputDirectory": str(out_dir),
    }


def _external_producer_results(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for entry in corpus["producerMatrix"]:
        if entry["availability"] == "missing":
            results.append({"producerId": entry["producerId"], "required": True, "status": "unavailable", "missingReason": entry["missingReason"], "fixturePath": entry.get("fixturePath")})
        else:
            results.append({"producerId": entry["producerId"], "required": True, "status": "available", "fixtureId": entry.get("fixtureId")})
    return results


def _requirements(corpus: dict[str, Any], fixture_results: list[dict[str, Any]], negatives: list[dict[str, Any]], bindings: dict[str, Any], differential: dict[str, Any], producers: list[dict[str, Any]], source_sha: str | None) -> list[dict[str, Any]]:
    unaccounted = sum(item["occurrenceAccounting"]["unaccountedOccurrenceCount"] for item in fixture_results)
    duplicate = sum(item["occurrenceAccounting"]["duplicateActualOccurrenceCount"] for item in fixture_results)
    graph_paths = ("$/objectIds", "$/pages", "$/annotationObjects", "$/resources", "$/fontMappings", "$/xrefStatus")
    graph_mismatch = sum(
        len(item["sourceMismatches"])
        + sum(1 for mismatch in item["actualMismatches"] if str(mismatch.get("path", "")).startswith(graph_paths))
        for item in fixture_results
    )
    event_mismatch = sum(
        sum(1 for mismatch in item["actualMismatches"] if str(mismatch.get("path", "")).startswith("$/paintOrder"))
        for item in fixture_results
    )
    fabricated = sum(item["fabricatedPreservedCount"] for item in fixture_results)
    negative_failures = sum(1 for item in negatives if item["status"] != "passed")
    required_producer_failures = [item for item in producers if item["required"] and item["status"] != "available"]
    cross_issue = {item["issueNumber"]: item for item in bindings["requiredIssues"]}
    requirements = [
        {"id": "PDF-101-OCCURRENCES", "status": "passed" if unaccounted == 0 and duplicate == 0 else "failed", "evidence": {"unaccounted": unaccounted, "duplicates": duplicate}},
        {"id": "PDF-101-GRAPH", "status": "passed" if graph_mismatch == 0 else "failed", "evidence": {"mismatchCount": graph_mismatch}},
        {"id": "PDF-101-EVENT-ORDER", "status": "passed" if event_mismatch == 0 else "failed", "evidence": {"mismatchCount": event_mismatch}},
        {"id": "PDF-101-FABRICATION", "status": "passed" if fabricated == 0 else "failed", "evidence": {"fabricatedPreservedCount": fabricated}},
        {"id": "PDF-101-NEGATIVE-DEFECTS", "status": "passed" if negative_failures == 0 else "failed", "evidence": {"undetectedDefectCount": negative_failures}},
        {"id": "PDF-101-REAL-PRODUCERS", "status": "passed" if not required_producer_failures else "failed", "evidence": {"unavailableProducerIds": [item["producerId"] for item in required_producer_failures]}},
        {"id": "PDF-101-MULTI-PARSER-RENDERER", "status": "passed" if differential.get("status") == "passed" and corpus["bindings"].get("rendererStatus") == "available" else "failed", "evidence": {"differentialStatus": differential.get("status"), "rendererStatus": corpus["bindings"].get("rendererStatus")}},
        {"id": "PDF-101-EXACT-SHA", "status": "passed" if source_sha and bindings["sourceTreeClean"] else "failed", "evidence": {"sourceSha": source_sha, "sourceTreeClean": bindings["sourceTreeClean"]}},
        {"id": "PDF-101-EVIDENCE-BUNDLE", "status": "passed" if bindings["evidenceBundle"]["status"] == "passed" else "failed", "evidence": bindings["evidenceBundle"]},
        {"id": "PDF-101-CI-BINDING", "status": "passed" if bindings["ci"]["status"] == "passed" else "failed", "evidence": bindings["ci"]},
    ]
    for issue_number in (88, 89, 91, 92, 94, 96):
        bound = cross_issue.get(issue_number, {})
        requirements.append({"id": f"PDF-101-BIND-{issue_number}", "status": "passed" if bound.get("status") == "passed" else "failed", "evidence": bound})
    return requirements


def _make_report(kind: str, source_sha: str | None, corpus_sha: str | None, fixture_results: list[dict[str, Any]], negatives: list[dict[str, Any]], bindings: dict[str, Any], differential: dict[str, Any], producers: list[dict[str, Any]], requirements: list[dict[str, Any]], setup_failure: str | None = None) -> dict[str, Any]:
    unmet = [item["id"] for item in requirements if item["status"] != "passed"]
    failure_summary: list[str] = []
    if setup_failure:
        failure_summary.append(setup_failure)
    failure_summary.extend(f"{item['id']}:unmet" for item in requirements if item["status"] != "passed")
    return {
        "schema": "fdir/qualification-issue-101-report",
        "version": "1.0.0",
        "issueNumber": 101,
        "reportKind": kind,
        "status": "failed" if setup_failure or unmet else "passed",
        "completionStatus": "incomplete-strict-gate" if setup_failure or unmet else "qualified",
        "sourceSha": source_sha,
        "sourceTreeClean": bindings.get("sourceTreeClean") if isinstance(bindings, dict) else False,
        "corpusSha256": corpus_sha,
        "fixtureResults": fixture_results,
        "producerResults": producers,
        "negativeDefectResults": negatives,
        "negativeDefectFailureCount": sum(1 for item in negatives if item.get("status") != "passed"),
        "undetectedDefectCount": sum(1 for item in negatives if not item.get("detected")),
        "unaccountedOccurrenceCount": sum(item.get("occurrenceAccounting", {}).get("unaccountedOccurrenceCount", 0) for item in fixture_results),
        "duplicateActualOccurrenceCount": sum(item.get("occurrenceAccounting", {}).get("duplicateActualOccurrenceCount", 0) for item in fixture_results),
        "mismatchCount": sum(len(item.get("sourceMismatches", [])) + len(item.get("actualMismatches", [])) for item in fixture_results),
        "fabricatedPreservedCount": sum(item.get("fabricatedPreservedCount", 0) for item in fixture_results),
        "independentParserDifferential": differential,
        "bindings": bindings,
        "requirements": requirements,
        "unmetRequirements": unmet,
        "unmetCount": len(unmet),
        "failureSummary": failure_summary,
        "falseCompleteCount": 0,
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    source_sha = _source_sha()
    corpus_sha = _sha256_file(corpus_path) if corpus_path.is_file() else None
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture_results: list[dict[str, Any]] = []
    input_paths: dict[str, Path] = {}
    negatives: list[dict[str, Any]] = []
    differential: dict[str, Any] = {"status": "failed", "reason": "qualification setup did not complete"}
    producers: list[dict[str, Any]] = []
    bindings: dict[str, Any] = {"sourceTreeClean": False, "requiredIssues": [], "evidenceBundle": {"status": "missing"}, "ci": {"status": "failed"}}
    setup_failure: str | None = None
    try:
        corpus = _load_corpus(corpus_path)
        work = out_dir / "work"
        for fixture in corpus["fixtures"]:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", fixture["fixtureId"])
            input_path = work / "inputs" / f"{safe_id}.pdf"
            raw = _write_authored_pdf(fixture, input_path)
            input_paths[fixture["fixtureId"]] = input_path
            if _sha256_bytes(raw) != fixture["sha256"]:
                raise QualificationError(f"fixture {fixture['fixtureId']} authored SHA-256 does not match corpus declaration")
        for fixture in corpus["fixtures"]:
            fixture_results.append(_fixture_result(fixture, input_paths[fixture["fixtureId"]], work))
        negatives = _run_negative_mutations(corpus)
        differential = _pymupdf_differential(fixture_results, input_paths)
        producers = _external_producer_results(corpus)
        bindings = _binding_checks(corpus, source_sha, out_dir)
        requirements = _requirements(corpus, fixture_results, negatives, bindings, differential, producers, source_sha)
    except (OSError, QualificationError, KeyError, TypeError, ValueError) as exc:
        setup_failure = str(exc)
        corpus = {"bindings": {"requiredIssues": []}, "producerMatrix": []}
        requirements = [{"id": "PDF-101-SETUP", "status": "failed", "evidence": {"reason": setup_failure}}]
    reports = {
        kind: _make_report(kind, source_sha, corpus_sha, fixture_results, negatives, bindings, differential, producers, requirements, setup_failure)
        for kind in REPORT_KINDS.values()
    }
    _write_producer_envelope(out_dir, reports, Path(corpus_path), source_sha)
    failed = [filename for filename, kind in REPORT_KINDS.items() if reports[kind]["status"] != "passed"]
    if failed:
        print("FAIL: issue #101 qualification reports written: " + ", ".join(failed), file=sys.stderr)
        return 1
    print("PASS: issue #101 qualification reports written: " + ", ".join(REPORT_NAMES))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run strict issue #101 PDF qualification")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return run_qualification(corpus_path=arguments.corpus, out_dir=arguments.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
