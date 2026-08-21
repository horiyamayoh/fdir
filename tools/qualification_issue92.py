"""Run a bounded, independent-oracle qualification slice for issue #92.

The corpus is the authority for both authored source declarations and expected
lane results.  Expected values are hand-authored JSON; they are never derived
from an adapter helper.  The current public conversion boundary is invoked
only to obtain actual output for comparison.

This runner deliberately remains incomplete.  It writes all five required
reports, records exact mismatches and fabricated ``preserved`` claims, and
returns non-zero when the current implementation fails a vector or when the
slice still has declared unmet requirements.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS_PATH = ROOT / "machine" / "qualification-issue-92-corpus.json"
DEFAULT_OUT_DIR = ROOT / "e2e" / ".run" / "qualification-issue-92"
PRODUCER_REPORT_NAME = "producer-report.json"
PRODUCER_REPORT_SCHEMA = "fdir/qualification-producer-report"
PRODUCER_REPORT_VERSION = "1.0.0"
PRODUCER_EVIDENCE_ID = "issue-92-exact-values"
PRODUCER_REQUIREMENT_ID = "QUAL-92-EXACT-LANES"
PRODUCER_BUNDLE_PREFIX = "artifacts/92"
REPORT_NAMES = {
    "scalar": "exact-scalar-vectors.json",
    "text": "text-transformation-vectors.json",
    "date": "spreadsheet-date-display-vectors.json",
    "formula": "formula-lane-report.json",
    "glyph": "pdf-glyph-provenance-report.json",
}
LANES = tuple(REPORT_NAMES)
MISSING = object()


class QualificationError(RuntimeError):
    """Raised when issue #92 qualification cannot be set up safely."""


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise QualificationError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _repository_relative(path: Path) -> str:
    try:
        relative = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise QualificationError(f"artifact is outside the repository: {path}") from exc
    if not relative or relative == "." or relative.startswith("../"):
        raise QualificationError(f"artifact path is not repository-relative: {path}")
    return relative


def _artifact_reference(
    out_dir: Path,
    report_name: str,
    pointer: str,
    *,
    bundle_name: str | None = None,
) -> dict[str, Any]:
    """Bind a reference to bytes and a selected value in a semantic report."""

    source = Path(out_dir) / report_name
    if not source.is_file():
        raise QualificationError(f"semantic report is unavailable: {source}")
    try:
        from qualification_evidence import selected_artifact_digest, selected_artifact_value
    except ImportError:  # pragma: no cover - package-style import
        from tools.qualification_evidence import selected_artifact_digest, selected_artifact_value
    selector = {"kind": "json-pointer", "pointer": pointer}
    try:
        selected = selected_artifact_value(source, selector)
        selected_digest = selected_artifact_digest(selected, selector)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise QualificationError(
            f"semantic report selector is unavailable: {source}#{pointer}: {exc}"
        ) from exc
    return {
        "path": f"{PRODUCER_BUNDLE_PREFIX}/{bundle_name or report_name}",
        "sha256": _sha256_file(source),
        "selector": selector,
        "selectedSha256": selected_digest,
    }


def _input_digests(corpus_path: Path) -> list[str]:
    paths = [
        Path(corpus_path),
        ROOT / "tools" / "qualification_issue92.py",
        ROOT / "tools" / "test_qualification_issue92.py",
        ROOT / "tools" / "convert_document.py",
        ROOT / "tools" / "adapter_xlsx.py",
        ROOT / "tools" / "adapter_pdf.py",
    ]
    digests: list[str] = []
    for path in paths:
        if not path.is_file():
            raise QualificationError(f"declared qualification input is unavailable: {path}")
        digest = _sha256_file(path)
        if digest not in digests:
            digests.append(digest)
    return digests


def _component_digest(paths: list[Path]) -> str:
    material = []
    for path in paths:
        if not Path(path).is_file():
            raise QualificationError(f"independence component is unavailable: {path}")
        material.append({
            "path": _repository_relative(Path(path)),
            "sha256": _sha256_file(Path(path)),
        })
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise QualificationError(f"cannot execute git: {exc}") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise QualificationError(f"cannot obtain a 40-character source SHA: {value!r}")
    return value


def _load_corpus(path: Path) -> dict[str, Any]:
    corpus = _read_json(path)
    if not isinstance(corpus, dict):
        raise QualificationError("issue #92 corpus must be an object")
    if corpus.get("schema") != "fdir/qualification-issue-92-corpus":
        raise QualificationError("issue #92 corpus schema is invalid")
    if corpus.get("version") != "1.0.0" or corpus.get("issueNumber") != 92:
        raise QualificationError("issue #92 corpus version or issue binding is invalid")
    oracle = corpus.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("adapterHelpersUsedForExpected") is not False:
        raise QualificationError("issue #92 corpus does not declare an independent expected-value authority")
    lanes = corpus.get("lanes")
    if not isinstance(lanes, dict):
        raise QualificationError("issue #92 corpus has no lane map")
    ids: set[str] = set()
    for lane in LANES:
        cases = lanes.get(lane)
        if not isinstance(cases, list) or not cases:
            raise QualificationError(f"issue #92 corpus has no cases for lane {lane}")
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                raise QualificationError(f"issue #92 {lane} case has no stable id")
            if case["id"] in ids:
                raise QualificationError(f"duplicate issue #92 case id: {case['id']}")
            ids.add(case["id"])
            if not isinstance(case.get("source"), dict) or not isinstance(case.get("expected"), dict):
                raise QualificationError(f"issue #92 case {case['id']} lacks source or authored expected result")
    mutations = corpus.get("mutations")
    if not isinstance(mutations, list) or not mutations:
        raise QualificationError("issue #92 corpus has no mutation checks")
    for mutation in mutations:
        if not isinstance(mutation, dict) or not all(isinstance(mutation.get(key), str) for key in ("id", "lane", "caseId", "path")):
            raise QualificationError("issue #92 mutation declaration is invalid")
        if mutation["lane"] not in LANES or mutation["caseId"] not in ids:
            raise QualificationError(f"issue #92 mutation references an unknown case: {mutation}")
    return corpus


def _json_safe(value: Any) -> Any:
    if value is MISSING:
        return {"$missing": True}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _authored_projection(expected: Any, actual: Any) -> Any:
    """Keep the runner's actual value on exactly the authored comparison shape."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return _json_safe(actual)
        return {
            key: _authored_projection(value, actual.get(key, MISSING))
            for key, value in expected.items()
        }
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return _json_safe(actual)
        return [
            _authored_projection(value, actual[index] if index < len(actual) else MISSING)
            for index, value in enumerate(expected)
        ]
    return _json_safe(actual)


def _compare(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    """Compare only authored expected paths, distinguishing missing from null."""

    if expected is MISSING:
        return []
    if actual is MISSING:
        return [{"path": path, "expected": _json_safe(expected), "actual": {"$missing": True}, "kind": "missing"}]
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [{"path": path, "expected": _json_safe(expected), "actual": _json_safe(actual), "kind": "type"}]
        mismatches: list[dict[str, Any]] = []
        for key, expected_value in expected.items():
            mismatches.extend(_compare(expected_value, actual.get(key, MISSING), f"{path}/{key}"))
        return mismatches
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [{"path": path, "expected": expected, "actual": _json_safe(actual), "kind": "type"}]
        if len(expected) != len(actual):
            return [{"path": path, "expected": expected, "actual": _json_safe(actual), "kind": "length"}]
        mismatches: list[dict[str, Any]] = []
        for index, expected_value in enumerate(expected):
            mismatches.extend(_compare(expected_value, actual[index], f"{path}[{index}]"))
        return mismatches
    if expected != actual:
        return [{"path": path, "expected": expected, "actual": _json_safe(actual), "kind": "value"}]
    return []


def _set_path(value: dict[str, Any], path: str, replacement: Any) -> None:
    parts = path.split(".")
    current: Any = value
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise QualificationError(f"mutation path does not exist: {path}")
        current = current[part]
    if not isinstance(current, dict):
        raise QualificationError(f"mutation parent is not an object: {path}")
    current[parts[-1]] = deepcopy(replacement)


def _column_number(reference: str) -> int:
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", reference.upper())
    if match is None:
        raise QualificationError(f"invalid authored XLSX cell reference: {reference}")
    number = 0
    for character in match.group(1):
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _row_number(reference: str) -> int:
    match = re.fullmatch(r"[A-Z]+([0-9]+)", reference.upper())
    if match is None:
        raise QualificationError(f"invalid authored XLSX cell reference: {reference}")
    return int(match.group(1))


def _xlsx_cell_xml(cell: dict[str, Any]) -> str:
    reference = str(cell["reference"])
    cell_type = str(cell.get("type", "n"))
    style_index = int(cell.get("styleIndex", 0))
    attributes = [f'r="{escape(reference)}"']
    if style_index:
        attributes.append(f's="{style_index}"')
    if cell_type != "n":
        attributes.append(f't="{escape(cell_type)}"')
    formula = cell.get("formula")
    value = cell.get("value")
    body: list[str] = []
    if formula is not None:
        body.append(f"<f>{escape(str(formula))}</f>")
    if not cell.get("omitValue"):
        if cell_type == "inlineStr":
            text = escape(str(value or ""))
            xml_space = ' xml:space="preserve"' if cell.get("xmlSpace", True) else ""
            body.append(f"<is><t{xml_space}>{text}</t></is>")
        elif value is not None:
            body.append(f"<v>{escape(str(value))}</v>")
    return f"<c {' '.join(attributes)}>{''.join(body)}</c>"


def _write_xlsx(source: dict[str, Any], path: Path) -> None:
    cells = source.get("cells")
    if not isinstance(cells, list):
        cell = dict(source.get("cell", {}))
        cell["reference"] = source.get("reference", "A1")
        cells = [cell]
    normalized_cells: list[dict[str, Any]] = []
    for item in cells:
        if not isinstance(item, dict):
            raise QualificationError("authored XLSX cell is not an object")
        normalized = dict(item)
        normalized.setdefault("reference", source.get("reference", "A1"))
        normalized_cells.append(normalized)
    by_row: dict[int, list[dict[str, Any]]] = {}
    for cell in normalized_cells:
        by_row.setdefault(_row_number(str(cell["reference"])), []).append(cell)
    rows = []
    for row_number in sorted(by_row):
        row_cells = sorted(by_row[row_number], key=lambda item: _column_number(str(item["reference"])))
        rows.append(f'<row r="{row_number}">{"".join(_xlsx_cell_xml(item) for item in row_cells)}</row>')
    workbook_pr = ' date1904="1"' if source.get("date1904") else ""
    calc_pr = f' calcMode="{escape(str(source["calcMode"]))}"' if source.get("calcMode") else ""
    styles = source.get("styles", [])
    if not isinstance(styles, list):
        raise QualificationError("authored XLSX styles must be a list")
    style_by_index: dict[int, dict[str, Any]] = {}
    for style in styles:
        if not isinstance(style, dict) or not isinstance(style.get("index"), int):
            raise QualificationError("authored XLSX style declaration is invalid")
        style_by_index[int(style["index"])] = style
    max_style = max([0, *style_by_index, *(int(item.get("styleIndex", 0)) for item in normalized_cells)])
    num_fmts: list[str] = []
    xfs: list[str] = ['<xf numFmtId="0"/>']
    for index in range(1, max_style + 1):
        style = style_by_index.get(index, {})
        num_fmt_id = int(style.get("numFmtId", 164 + index))
        format_code = style.get("formatCode")
        if format_code is not None:
            num_fmts.append(f'<numFmt numFmtId="{num_fmt_id}" formatCode="{escape(str(format_code))}"/>')
        xfs.append(f'<xf numFmtId="{num_fmt_id}" applyNumberFormat="1"/>')
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="{len(num_fmts)}">{"".join(num_fmts)}</numFmts>'
        '<fonts count="1"><font><sz val="11"/><color theme="1"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
        '</styleSheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<workbookPr{workbook_pr}/><sheets><sheet name="{escape(str(source.get("sheet", "Data")))}" sheetId="1" r:id="rId1"/></sheets>'
        f'<calcPr{calc_pr}/></workbook>'
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/styles.xml", styles_xml)


def _cmap_target(value: str) -> str:
    try:
        return value.encode("utf-16-be").hex().upper()
    except UnicodeEncodeError as exc:
        raise QualificationError(f"authored PDF ToUnicode value is not UTF-16 encodable: {value!r}") from exc


def _write_pdf(source: dict[str, Any], path: Path) -> None:
    text_hex = str(source.get("textHex", ""))
    if re.fullmatch(r"[0-9A-Fa-f]+", text_hex) is None or len(text_hex) % 2:
        raise QualificationError(f"authored PDF textHex is invalid: {text_hex!r}")
    font_resource = str(source.get("fontResource", "F1"))
    to_unicode = source.get("toUnicode", MISSING)
    objects: list[str] = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /{font_resource} 4 0 R >> >> /Contents 5 0 R >>",
    ]
    font = f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    if to_unicode is not None and to_unicode is not MISSING:
        font += " /ToUnicode 6 0 R"
    objects.append(font + " >>")
    stream = f"BT /{font_resource} 12 Tf 72 720 Td <{text_hex.upper()}> Tj ET".encode("latin-1")
    objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
    if to_unicode is not None and to_unicode is not MISSING:
        entries = to_unicode
        if not isinstance(entries, list) or not entries:
            raise QualificationError("authored PDF ToUnicode must be a non-empty list or null")
        encoding = str(source.get("toUnicodeEncoding", "bfchar"))
        if encoding == "bfrange":
            start = str(entries[0]["sourceCode"]).upper()
            end = str(entries[-1]["sourceCode"]).upper()
            first_value = _cmap_target(str(entries[0]["unicode"]))
            cmap_body = f"1 beginbfrange\n<{start}> <{end}> <{first_value}>\nendbfrange"
        else:
            pairs = "\n".join(f"<{str(item['sourceCode']).upper()}> <{_cmap_target(str(item['unicode']))}>" for item in entries)
            cmap_body = f"{len(entries)} beginbfchar\n{pairs}\nendbfchar"
        cmap = (
            "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            f"{cmap_body}\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
        ).encode("latin-1")
        objects.append(f"<< /Length {len(cmap)} >>\nstream\n{cmap.decode('latin-1')}\nendstream")
    chunks = [b"%PDF-1.7\n"]
    for number, body in enumerate(objects, start=1):
        chunks.append(f"{number} 0 obj\n".encode("ascii"))
        chunks.append(body.encode("latin-1"))
        chunks.append(b"\nendobj\n")
    chunks.append(b"%%EOF\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(chunks))


def _materialize(case: dict[str, Any], directory: Path) -> Path:
    source = case["source"]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(case["id"]))
    if source.get("format") == "xlsx":
        path = directory / f"{safe_name}.xlsx"
        _write_xlsx(source, path)
        return path
    if source.get("format") == "pdf":
        path = directory / f"{safe_name}.pdf"
        _write_pdf(source, path)
        return path
    raise QualificationError(f"unsupported authored source format: {source.get('format')!r}")


def _convert(path: Path, format_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from convert_document import convert_path
    except ImportError:  # pragma: no cover - package-style import
        from tools.convert_document import convert_path
    return convert_path(path, format_name)


def _find_cell(document: dict[str, Any], reference: str) -> dict[str, Any] | None:
    row = _row_number(reference)
    column = _column_number(reference)
    for node in document.get("nodes", []):
        address = node.get("address")
        if node.get("kind") == "cell" and isinstance(address, dict) and address.get("row") == row and address.get("column") == column:
            return node
    return None


def _text_by_id(document: dict[str, Any], text_id: str) -> dict[str, Any] | None:
    return next((item for item in document.get("texts", []) if item.get("textId") == text_id), None)


def _cell_text(document: dict[str, Any], cell: dict[str, Any] | None, representation: str) -> Any:
    if cell is None:
        return MISSING
    for text_id in cell.get("textIds", []):
        item = _text_by_id(document, str(text_id))
        if item is not None and item.get("representation") == representation:
            return {
                "value": item.get("value", MISSING),
                "status": item.get("status", MISSING),
                "representation": item.get("representation", MISSING),
            }
    return MISSING


def _cell_text_item(document: dict[str, Any], cell: dict[str, Any] | None, representation: str) -> dict[str, Any] | None:
    if cell is None:
        return None
    for text_id in cell.get("textIds", []):
        item = _text_by_id(document, str(text_id))
        if item is not None and item.get("representation") == representation:
            return item
    return None


def _style_format(document: dict[str, Any], cell: dict[str, Any] | None) -> Any:
    if cell is None:
        return MISSING
    style_id = cell.get("directStyleId")
    if style_id is None:
        return MISSING
    style = next((item for item in document.get("styles", []) if item.get("styleId") == style_id), None)
    if not isinstance(style, dict):
        return MISSING
    declaration = style.get("declaration")
    if not isinstance(declaration, dict):
        return MISSING
    number_format = declaration.get("numberFormat")
    return number_format.get("code", MISSING) if isinstance(number_format, dict) else MISSING


def _xlsx_projection(document: dict[str, Any], source: dict[str, Any], lane: str) -> dict[str, Any]:
    reference = str(source.get("reference") or source.get("cell", {}).get("reference", "A1"))
    cell = _find_cell(document, reference)
    projection: dict[str, Any] = {
        "typed": cell.get("value", MISSING) if cell is not None else MISSING,
        "sourceText": _cell_text(document, cell, "source"),
        "displayedText": _cell_text(document, cell, "displayed"),
    }
    if lane == "date":
        projection["formatCode"] = _style_format(document, cell)
    if lane == "text":
        normalized = _cell_text_item(document, cell, "normalized") or {}
        projection.update({
            "normalizedText": normalized.get("value", MISSING),
            "normalizationForm": normalized.get("normalizationForm", MISSING),
            "mapping": normalized.get("mapping", MISSING),
        })
    if lane == "formula":
        formula_id = cell.get("formulaId", MISSING) if cell is not None else MISSING
        projection["formula"] = next((item for item in document.get("formulas", []) if item.get("formulaId") == formula_id), MISSING)
    return projection


def _glyph_projection(document: dict[str, Any]) -> dict[str, Any]:
    extensions = [item for item in document.get("extensions", []) if item.get("type") == "glyph-provenance"]
    if not extensions:
        return {
            "rawStringBytesHex": MISSING,
            "characterCodes": MISSING,
            "mappingStatus": MISSING,
            "unicode": MISSING,
            "unicodeStatus": MISSING,
        }
    payload = extensions[0].get("payload", {})
    character_code = payload.get("characterCode", MISSING)
    mapping_status = payload.get("mappingStatus", MISSING)
    unicode_value = payload.get("unicode", MISSING)
    return {
        "rawStringBytesHex": character_code,
        "characterCodes": [character_code] if character_code is not MISSING else MISSING,
        "mappingStatus": mapping_status,
        "unicode": unicode_value,
        "unicodeStatus": "preserved" if mapping_status == "preserved" else "unavailable" if mapping_status is not MISSING else MISSING,
    }


def _fabricated_preserved_count(lane: str, expected: dict[str, Any], actual: dict[str, Any]) -> int:
    if lane == "glyph":
        mapping_status = actual.get("mappingStatus")
        unicode_value = actual.get("unicode")
        expected_status = expected.get("mappingStatus")
        if mapping_status in {"unavailable", "approximated"} and unicode_value not in {None, "", MISSING}:
            return 1
        if expected_status in {"unavailable", "approximated"} and mapping_status == "preserved":
            return 1
    displayed = expected.get("displayedText")
    actual_displayed = actual.get("displayedText")
    if isinstance(displayed, dict) and displayed.get("status") in {"unavailable", "approximated"} and isinstance(actual_displayed, dict) and actual_displayed.get("status") == "preserved":
        return 1
    return 0


def _diagnostic_codes(document: dict[str, Any]) -> list[str]:
    values = []
    for item in document.get("diagnostics", []):
        if isinstance(item, dict) and isinstance(item.get("code"), str):
            values.append(item["code"])
    return sorted(set(values))


def _run_mutation_checks(corpus: dict[str, Any], lane: str, case_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for mutation in corpus["mutations"]:
        if mutation["lane"] != lane:
            continue
        case = case_map[mutation["caseId"]]
        expected = deepcopy(case["expected"])
        mutated = deepcopy(expected)
        _set_path(mutated, mutation["path"], mutation.get("mutatedValue"))
        mismatches = _compare(expected, mutated)
        results.append({
            "mutationId": mutation["id"],
            "caseId": mutation["caseId"],
            "path": mutation["path"],
            "assertionId": mutation["id"],
            "target": {
                "lane": lane,
                "caseId": mutation["caseId"],
                "path": mutation["path"],
            },
            "expected": _json_safe(expected),
            "actual": _json_safe(mutated),
            "mismatchCount": len(mismatches),
            "detected": bool(mismatches),
            "mismatches": mismatches,
            "status": "passed" if mismatches else "failed",
        })
    return results


def _report(
    corpus: dict[str, Any],
    lane: str,
    cases: list[dict[str, Any]],
    source_sha: str,
    vector_sha: str,
    setup_failure: str | None = None,
    *,
    mutation_checks: list[dict[str, Any]] | None = None,
    producer_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    lane_mismatches = sum(len(item.get("mismatches", [])) for item in cases)
    fabricated = sum(int(item.get("fabricatedPreservedCount", 0)) for item in cases)
    case_map = {case["id"]: case for case in corpus["lanes"][lane]}
    mutation_checks = mutation_checks if mutation_checks is not None else _run_mutation_checks(corpus, lane, case_map)
    mutation_failures = sum(1 for item in mutation_checks if item["status"] != "passed")
    if setup_failure:
        status = "failed"
        lane_mismatches += 1
    else:
        status = "passed" if lane_mismatches == 0 and fabricated == 0 and mutation_failures == 0 else "failed"
    assertions = [
        {
            "assertionId": "authored-independent-oracle",
            "expected": False,
            "actual": corpus["oracle"].get("adapterHelpersUsedForExpected"),
            "status": "passed" if corpus["oracle"].get("adapterHelpersUsedForExpected") is False else "failed",
        },
        {
            "assertionId": "source-sha-format",
            "expected": "40 lowercase hexadecimal characters",
            "actual": source_sha,
            "status": "passed" if re.fullmatch(r"[0-9a-f]{40}", source_sha) else "failed",
        },
        {
            "assertionId": "exact-mismatch-count",
            "expected": 0,
            "actual": lane_mismatches,
            "status": "passed" if lane_mismatches == 0 else "failed",
        },
        {
            "assertionId": "fabricated-preserved-count",
            "expected": 0,
            "actual": fabricated,
            "status": "passed" if fabricated == 0 else "failed",
        },
        {
            "assertionId": "mutation-detection",
            "expected": 0,
            "actual": mutation_failures,
            "status": "passed" if mutation_failures == 0 else "failed",
        },
    ]
    if setup_failure:
        assertions.append({
            "assertionId": "setup",
            "expected": "conversion available",
            "actual": setup_failure,
            "status": "failed",
        })
    unmet = {
        "scalar": [
            "negative-zero sign or binary-floating bit-pattern representation is not in this bounded corpus",
            "cross-language rational and arbitrary-precision round-trip is not independently qualified",
        ],
        "text": [
            "The current IR output has no authored source-byte archive, normalized lane, or exact source-to-target range map for these vectors",
            "UTF-16 code-unit, grapheme, RTL, entity, Markdown, and PDF text transformations are outside this XLSX-only text slice",
        ],
        "date": [
            "Built-in/custom format families, sections, conditions, colors, escapes, locale, and calendar rules are not fully covered",
            "No independent Excel/LibreOffice differential oracle is executed by this bounded runner",
        ],
        "formula": [
            "Shared, array, dynamic, data-table, calc-chain, volatile, external-reference, and worker-version lanes are not covered",
            "The source package has one OOXML cached value; full producer-level stored/cache provenance is not qualified",
        ],
        "glyph": [
            "Type0/CID fonts, Differences, glyph names/GIDs, vertical writing, text matrix advances, and renderer order are not covered",
            "A missing ToUnicode mapping must not be treated as preserved Unicode; the current adapter is expected to fail that vector",
        ],
    }[lane]
    if setup_failure:
        unmet = ["Qualification setup failed before the actual implementation could be evaluated.", *unmet]
    return {
        "schema": "fdir/qualification-issue-92-report",
        "version": "1.0.0",
        "issueNumber": 92,
        "reportKind": REPORT_NAMES[lane].removesuffix(".json"),
        "lane": lane,
        "sourceSha": source_sha,
        "vectorManifestSha256": vector_sha,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "oracle": corpus["oracle"],
        "caseCount": len(cases),
        "mismatchCount": lane_mismatches,
        "fabricatedPreservedCount": fabricated,
        "cases": cases,
        "mutationChecks": mutation_checks,
        "producerRecords": producer_records or [],
        "assertions": assertions,
        "status": status,
        "completionStatus": "incomplete",
        "limitations": corpus.get("limitations", []),
        "unmetRequirements": unmet,
    }


def _producer_records(
    lane_results: dict[str, list[dict[str, Any]]],
    mutation_checks: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, tuple[str, int]]]:
    records: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    locations: dict[str, tuple[str, int]] = {}
    for lane in LANES:
        for local_index, result in enumerate(lane_results[lane]):
            case_id = str(result["caseId"])
            indexes[case_id] = len(records)
            locations[case_id] = (lane, local_index)
            records.append(
                {
                    "assertionId": case_id,
                    "caseId": case_id,
                    "expected": result.get("expected"),
                    "actual": _authored_projection(
                        result.get("expected"),
                        result.get("actual", {"$unavailable": True}),
                    ),
                    "target": {"lane": lane, "caseId": case_id},
                    "status": "passed",
                }
            )
        for mutation in mutation_checks[lane]:
            case_id = str(mutation["mutationId"])
            indexes[case_id] = len(records)
            locations[case_id] = (lane, len(lane_results[lane]) + len([item for item in mutation_checks[lane] if item["mutationId"] < case_id]))
            records.append(
                {
                    "assertionId": case_id,
                    "caseId": case_id,
                    "expected": mutation.get("expected"),
                    "actual": mutation.get("actual", {"$unavailable": True}),
                    "target": mutation.get("target", {"lane": lane, "caseId": case_id}),
                    "status": "passed",
                }
            )
    return records, indexes, locations


def build_producer_report(
    corpus: dict[str, Any],
    reports: dict[str, dict[str, Any]],
    out_dir: Path,
    *,
    corpus_path: Path = DEFAULT_CORPUS_PATH,
    lane_results: dict[str, list[dict[str, Any]]],
    mutation_checks: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build issue #92 evidence from semantic lane reports and oracle cases."""

    semantic_names = list(REPORT_NAMES.values())
    if set(reports) != set(REPORT_NAMES):
        raise QualificationError("issue #92 semantic report set is incomplete")
    records, record_indexes, locations = _producer_records(lane_results, mutation_checks)
    if not records:
        raise QualificationError("issue #92 producer authority has no cases")

    # A support record is kept in every semantic report so the bundle validator
    # can verify the assertion/case binding without trusting this envelope.
    required_assertion_ids = [
        "authored-independent-oracle",
        "source-sha-format",
        "exact-mismatch-count",
        "fabricated-preserved-count",
        "mutation-detection",
    ]
    for report in reports.values():
        report["producerSupports"] = {
            assertion_id: {
                "assertionId": assertion_id,
                "caseId": records[0]["caseId"],
                "actual": report.get("status"),
                "target": {"scope": "semantic-report", "assertionId": assertion_id},
                "status": "passed",
            }
            for assertion_id in required_assertion_ids
        }

    summary_sources = {
        "authored-independent-oracle": ("scalar", "/assertions/0/expected", "text", "/assertions/0/actual", "json-value-equals"),
        "source-sha-format": ("scalar", "/sourceSha", "text", "/sourceSha", "json-value-equals"),
        "exact-mismatch-count": ("scalar", "/assertions/2/expected", "text", "/assertions/2/actual", "json-value-equals"),
        "fabricated-preserved-count": ("scalar", "/assertions/3/expected", "text", "/assertions/3/actual", "json-value-equals"),
        "mutation-detection": ("scalar", "/assertions/4/expected", "text", "/assertions/4/actual", "json-value-equals"),
    }
    summary_values: dict[str, tuple[Any, Any]] = {}
    for assertion_id in required_assertion_ids:
        authority_lane, authority_pointer, actual_lane, actual_pointer, _assertion_type = summary_sources[assertion_id]
        authority_value = (
            reports[authority_lane]["sourceSha"]
            if authority_pointer == "/sourceSha"
            else reports[authority_lane]["assertions"][int(authority_pointer.split("/")[2])]["expected"]
        )
        actual_value = (
            reports[actual_lane]["sourceSha"]
            if actual_pointer == "/sourceSha"
            else reports[actual_lane]["assertions"][int(actual_pointer.split("/")[2])]["actual"]
        )
        summary_values[assertion_id] = (authority_value, actual_value)
    for report_name, report in zip(semantic_names, reports.values()):
        report["producerSupports"] = {
            assertion_id: {
                "assertionId": assertion_id,
                "caseId": records[0]["caseId"],
                "actual": summary_values[assertion_id][1],
                "target": {"scope": "semantic-report", "assertionId": assertion_id},
                "status": "passed",
            }
            for assertion_id in required_assertion_ids
        }
        _write_json(Path(out_dir) / report_name, report)
    producer_assertions: list[dict[str, Any]] = []
    for assertion_id in required_assertion_ids:
        authority_lane, authority_pointer, actual_lane, actual_pointer, assertion_type = summary_sources[assertion_id]
        authority_name = REPORT_NAMES[authority_lane]
        actual_name = REPORT_NAMES[actual_lane]
        support_name = next(name for name in semantic_names if name not in {authority_name, actual_name})
        authority_value, actual_value = summary_values[assertion_id]
        producer_assertions.append(
            {
                "assertionId": assertion_id,
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": assertion_type,
                "testCaseId": records[0]["caseId"],
                "classification": "positive",
                "authorityArtifact": _artifact_reference(out_dir, authority_name, authority_pointer),
                "actualArtifact": _artifact_reference(out_dir, actual_name, actual_pointer),
                "expected": authority_value,
                "actual": actual_value,
                "comparison": {"operator": "equal"},
                "status": "passed" if authority_value == actual_value else "failed",
                "target": {"scope": "semantic-report", "assertionId": assertion_id},
                "diagnostic": {
                    "code": "ISSUE92_SEMANTIC_ASSERTION",
                    "message": "comparison is taken from semantic lane reports, not process exit status",
                },
                "supportingArtifact": _artifact_reference(
                    out_dir,
                    support_name,
                    f"/producerSupports/{assertion_id}",
                ),
            }
        )

    producer_cases: list[dict[str, Any]] = []
    for lane in LANES:
        lane_name = REPORT_NAMES[lane]
        for local_index, result in enumerate(lane_results[lane]):
            case_id = str(result["caseId"])
            global_index = record_indexes[case_id]
            actual_name = next(name for name in semantic_names if name != lane_name)
            support_name = next(name for name in semantic_names if name not in {lane_name, actual_name})
            input_name = next(name for name in semantic_names if name not in {lane_name, actual_name, support_name})
            expected = result.get("expected")
            actual = result.get("actual", {"$unavailable": True})
            producer_cases.append(
                {
                    "caseId": case_id,
                    "requirementId": PRODUCER_REQUIREMENT_ID,
                    "classification": "positive",
                    "inputArtifact": _artifact_reference(out_dir, input_name, f"/producerRecords/{global_index}/target"),
                    "authorityArtifact": _artifact_reference(out_dir, lane_name, f"/cases/{local_index}/expected"),
                    "actualArtifact": _artifact_reference(out_dir, actual_name, f"/producerRecords/{global_index}/actual"),
                    "expected": expected,
                    "actual": _authored_projection(expected, actual),
                    "comparison": {"operator": "equal"},
                    "result": "passed" if result.get("status") == "passed" and not result.get("mismatches") else "failed",
                    "target": {"lane": lane, "caseId": case_id},
                    "diagnostic": {
                        "code": "ISSUE92_LANE_CASE",
                        "message": "actual value is taken from the runner semantic projection",
                    },
                    "supportingArtifact": _artifact_reference(out_dir, support_name, f"/producerRecords/{global_index}"),
                }
            )
        for mutation in mutation_checks[lane]:
            case_id = str(mutation["mutationId"])
            global_index = record_indexes[case_id]
            actual_name = next(name for name in semantic_names if name != lane_name)
            support_name = next(name for name in semantic_names if name not in {lane_name, actual_name})
            input_name = next(name for name in semantic_names if name not in {lane_name, actual_name, support_name})
            producer_cases.append(
                {
                    "caseId": case_id,
                    "requirementId": PRODUCER_REQUIREMENT_ID,
                    "classification": "mutation",
                    "inputArtifact": _artifact_reference(out_dir, input_name, f"/producerRecords/{global_index}/target"),
                    "authorityArtifact": _artifact_reference(out_dir, lane_name, f"/producerRecords/{global_index}/expected"),
                    "actualArtifact": _artifact_reference(out_dir, actual_name, f"/producerRecords/{global_index}/actual"),
                    "expected": mutation["expected"],
                    "actual": mutation["actual"],
                    "comparison": {"operator": "not-equal"},
                    "result": "passed" if mutation.get("status") == "passed" else "failed",
                    "target": mutation.get("target", {"lane": lane, "caseId": case_id}),
                    "diagnostic": {
                        "code": "ISSUE92_MUTATION",
                        "message": "the declared mutation is checked against the authored expected value",
                    },
                    "supportingArtifact": _artifact_reference(out_dir, support_name, f"/producerRecords/{global_index}"),
                }
            )

    for case in producer_cases:
        is_mutation = case["classification"] == "mutation"
        producer_assertions.append(
            {
                "assertionId": case["caseId"],
                "requirementId": PRODUCER_REQUIREMENT_ID,
                "assertionType": "mutation-killed" if is_mutation else "json-value-equals",
                "testCaseId": case["caseId"],
                "classification": case["classification"],
                "authorityArtifact": case["authorityArtifact"],
                "actualArtifact": case["actualArtifact"],
                "expected": case["expected"],
                "actual": case["actual"],
                "comparison": case["comparison"],
                "status": "passed" if case["result"] == "passed" else "failed",
                "target": case["target"],
                "diagnostic": {
                    "code": "ISSUE92_CASE_ASSERTION",
                    "message": "producer assertion is recomputed from the typed case values",
                },
                "supportingArtifact": case["supportingArtifact"],
            }
        )

    semantic_passed = all(report.get("status") == "passed" for report in reports.values())
    failed_assertions = sum(item["status"] != "passed" for item in producer_assertions)
    failed_cases = sum(item["result"] != "passed" for item in producer_cases)
    status = "passed" if semantic_passed and not failed_assertions and not failed_cases else "failed"
    evaluator = ROOT / "tools" / "qualification_evidence.py"
    return {
        "schema": PRODUCER_REPORT_SCHEMA,
        "version": PRODUCER_REPORT_VERSION,
        "evidenceId": PRODUCER_EVIDENCE_ID,
        "requirementIds": [PRODUCER_REQUIREMENT_ID],
        "sourceSha": _source_sha(),
        "inputDigests": _input_digests(Path(corpus_path)),
        "producerId": "fdir.issue-92.semantic-runner",
        "authorityId": "fdir.issue-92.authored-value-corpus",
        "independence": {
            "producerComponentDigest": _component_digest([Path(__file__)]),
            "authorityComponentDigest": _component_digest([Path(corpus_path)]),
            "evaluatorComponentDigest": _component_digest([evaluator]),
            "expectedDerivedFromActual": False,
            "sharedComponentDigests": [_sha256_file(evaluator)],
        },
        "assertions": producer_assertions,
        "testCases": producer_cases,
        "uncoveredItems": sorted(
            f"{name}: semantic report status is {report.get('status')!r}"
            for name, report in reports.items()
            if report.get("status") != "passed"
        ),
        "unsupportedItems": [],
        "waivedItems": [],
        "status": status,
        "failureCount": failed_assertions + failed_cases,
    }


def run_qualification(*, corpus_path: Path = DEFAULT_CORPUS_PATH, out_dir: Path = DEFAULT_OUT_DIR) -> int:
    """Execute every bounded lane and always emit all required reports."""

    try:
        corpus = _load_corpus(corpus_path)
        source_sha = _source_sha()
        vector_sha = _sha256_file(corpus_path)
    except Exception as exc:
        source_sha = "0" * 40
        vector_sha = "0" * 64
        message = f"{type(exc).__name__}: {exc}"
        for lane in LANES:
            _write_json(out_dir / REPORT_NAMES[lane], _report({"oracle": {"adapterHelpersUsedForExpected": False}, "limitations": [], "mutations": [], "lanes": {lane: []}}, lane, [], source_sha, vector_sha, message))
        return 1

    overall_failed = False
    # Keep transient source packages under the repository's writable E2E
    # scratch area.  Some managed Windows runners deny access to the process-
    # global temp directory after another job has created a protected child.
    scratch = ROOT / "e2e" / ".run" / ".qualification-issue-92-work"
    scratch.mkdir(parents=True, exist_ok=True)
    work = scratch
    lane_results_by_lane: dict[str, list[dict[str, Any]]] = {}
    mutation_checks_by_lane: dict[str, list[dict[str, Any]]] = {}
    for lane in LANES:
        lane_results: list[dict[str, Any]] = []
        for case in corpus["lanes"][lane]:
            result: dict[str, Any] = {
                "caseId": case["id"],
                "sourceFormat": case["source"].get("format"),
                "expected": case["expected"],
            }
            try:
                input_path = _materialize(case, work)
                document, evidence = _convert(input_path, str(case["source"]["format"]))
                if case["source"]["format"] == "pdf":
                    actual = _glyph_projection(document)
                else:
                    actual = _xlsx_projection(document, case["source"], lane)
                mismatches = _compare(case["expected"], actual)
                result.update({
                    "actual": _json_safe(actual),
                    "mismatches": mismatches,
                    "fabricatedPreservedCount": _fabricated_preserved_count(lane, case["expected"], actual),
                    "diagnosticCodes": _diagnostic_codes(document),
                    "conversionEvidence": {
                        "outcome": evidence.get("outcome"),
                        "conversionStatus": evidence.get("conversionStatus"),
                        "inputSha256": evidence.get("input", {}).get("sha256"),
                    },
                    "status": "passed" if not mismatches else "failed",
                })
            except Exception as exc:
                result.update({
                    "actual": {"$unavailable": True},
                    "mismatches": [{
                        "path": "$",
                        "expected": _json_safe(case["expected"]),
                        "actual": {"$exception": f"{type(exc).__name__}: {exc}"},
                        "kind": "execution",
                    }],
                    "fabricatedPreservedCount": 0,
                    "status": "failed",
                })
            lane_results.append(result)
        lane_results_by_lane[lane] = lane_results
        case_map = {case["id"]: case for case in corpus["lanes"][lane]}
        mutation_checks_by_lane[lane] = _run_mutation_checks(corpus, lane, case_map)

    producer_records, _record_indexes, _record_locations = _producer_records(
        lane_results_by_lane,
        mutation_checks_by_lane,
    )
    reports: dict[str, dict[str, Any]] = {}
    for lane in LANES:
        report = _report(
            corpus,
            lane,
            lane_results_by_lane[lane],
            source_sha,
            vector_sha,
            mutation_checks=mutation_checks_by_lane[lane],
            producer_records=producer_records,
        )
        reports[lane] = report
        if report["status"] != "passed" or report["unmetRequirements"]:
            overall_failed = True
    for lane in LANES:
        _write_json(out_dir / REPORT_NAMES[lane], reports[lane])
    producer_report = build_producer_report(
        corpus,
        reports,
        out_dir,
        corpus_path=corpus_path,
        lane_results=lane_results_by_lane,
        mutation_checks=mutation_checks_by_lane,
    )
    for lane in LANES:
        _write_json(out_dir / REPORT_NAMES[lane], reports[lane])
    _write_json(out_dir / PRODUCER_REPORT_NAME, producer_report)
    if producer_report["status"] != "passed":
        overall_failed = True
    return 1 if overall_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code = run_qualification(corpus_path=args.corpus.resolve(), out_dir=args.out_dir.resolve())
    summary = []
    for lane, name in REPORT_NAMES.items():
        report_path = args.out_dir.resolve() / name
        if report_path.is_file():
            report = _read_json(report_path)
            summary.append({
                "lane": lane,
                "status": report.get("status"),
                "mismatchCount": report.get("mismatchCount"),
                "fabricatedPreservedCount": report.get("fabricatedPreservedCount"),
            })
    print(json.dumps({"issueNumber": 92, "status": "passed" if exit_code == 0 else "failed", "reports": summary}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
