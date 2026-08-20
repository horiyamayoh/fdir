"""Expand the Issue #89 defect matrix into reproducible mutation variants.

The source selectors remain anchored to the original case.  Each generated
variant changes only the replacement text, keeps the same disposable-checkout
detector, and receives its own invariant-matrix row.  Keeping this expansion
script in the repository makes the case count auditable instead of relying on
an opaque hand-edited JSON blob.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "machine" / "defect-injection-contract.json"

FORMAT_PROBES = {
    "canonical": {
        "canonical-authority-validation",
        "canonical-collection-order",
        "canonical-key-order",
    },
    "docx": {
        "docx-hyperlink-run",
        "docx-drawing-handler",
        "docx-story-processing",
        "docx-missing-relationship",
        "docx-style-inheritance",
    },
    "xlsx": {
        "xlsx-shared-string",
        "xlsx-binary-float",
        "xlsx-date-system",
        "xlsx-formula-lanes",
        "xlsx-table-relationship",
        "xlsx-displayed-lane",
    },
    "pdf": {
        "pdf-page-order",
        "pdf-tounicode",
        "pdf-graphics-restore",
        "pdf-unknown-operator",
        "pdf-annotation-target",
        "pdf-interleaved-paint",
    },
    "markdown": {
        "markdown-span-end",
        "markdown-delimiter-resolution",
        "markdown-reference-resolution",
        "markdown-table-separator",
        "markdown-unsupported-construct",
    },
}

BASE_REPLACEMENTS = {
    "xlsx-formula-lanes": "                            cached_value = None",
}


VARIANTS: dict[str, list[str]] = {
    "val-discriminator-branch": [
        "        if isinstance(kind, str) and kind in variants and False:",
        "        if isinstance(kind, str) and kind in variants and 0:",
        "        if isinstance(kind, str) and kind in variants and is_color:",
    ],
    "docx-hyperlink-run": [
        "            elif item_local == \"hyperlink\" and False:",
        "            elif item_local == \"hyperlink\" and 0:",
        "            elif item_local == \"hyperlink\" and item_local != \"hyperlink\":",
    ],
    "docx-drawing-handler": [
        "                        elif item_local == \"drawing\" and False:",
        "                        elif item_local == \"drawing\" and 0:",
        "                        elif item_local == \"drawing\" and item_local != \"drawing\":",
    ],
    "docx-story-processing": [
        "                    if False:\n                        _parse_story_part(builder, archive, part_name, str(package_part[\"partId\"]), styles)",
        "                    if 0:\n                        _parse_story_part(builder, archive, part_name, str(package_part[\"partId\"]), styles)",
        "                    if part_name == \"__never__\":\n                        _parse_story_part(builder, archive, part_name, str(package_part[\"partId\"]), styles)",
    ],
    "docx-missing-relationship": [
        "                            relation_status = \"ambiguous\"",
        "                            relation_status = \"unsupported\"",
        "                            relation_status = \"failed\"",
    ],
    "docx-style-inheritance": [
        "                    if False:\n                        _resolve_styles(builder, style_graph, style_declarations, styles)",
        "                    if 0:\n                        _resolve_styles(builder, style_graph, style_declarations, styles)",
        "                    if styles is None:\n                        _resolve_styles(builder, style_graph, style_declarations, styles)",
    ],
    "xlsx-shared-string": [
        "                                raw_value = \"\"",
        "                                raw_value = shared[0] if False else raw_value",
    ],
    "xlsx-binary-float": [
        "        number = Decimal(str(float(value)))",
        "        number = Decimal(\"0\")",
    ],
    "xlsx-date-system": [
        "    return \"1904\" if props is not None else \"1900\"",
        "    return \"1904\"",
    ],
    "xlsx-formula-lanes": [
        "                            cached_value = None",
        "                            cached_value = {\"type\": \"blank\", \"value\": None, \"status\": \"unavailable\"}",
    ],
    "xlsx-table-relationship": [
        "                for table_name in ():",
        "                for table_name in [\"__missing__\"]:",
        "                for table_name in sorted(()) :",
    ],
    "xlsx-displayed-lane": [
        "                        displayed = raw_value",
        "                        displayed = \"\"",
        "                        displayed = str(raw_value)",
    ],
    "pdf-page-order": [
        "    pages = [(key, value) for key, value in list(objects.items()) if re.search(rb\"/Type\\s*/Page\\b\", value)]",
        "    pages = [(key, value) for key, value in sorted(objects.items(), reverse=True) if re.search(rb\"/Type\\s*/Page\\b\", value)]",
    ],
    "pdf-tounicode": [
        "                mapping = _parse_cmap(b\"\")",
        "                mapping = _parse_cmap(_decode_stream(cmap_data)) if False else []",
    ],
    "pdf-graphics-restore": [
        "            elif operator == \"Q\" and False:",
        "            elif operator == \"Q\" and 0:",
    ],
    "pdf-unknown-operator": [
        "            unsupported.extend([])",
        "            unsupported += []",
    ],
    "pdf-annotation-target": [
        "                builder.add_item(\"annotations\", {\"annotationId\": annotation_id, \"kind\": \"comment\", \"targetIds\": [page_id], \"body\": \"PDF annotation destination retained as form fact\", \"status\": \"preserved\"}, \"annotationId\")",
        "                builder.add_item(\"annotations\", {\"annotationId\": annotation_id, \"kind\": \"bookmark\", \"targetIds\": [page_id], \"body\": \"PDF annotation destination retained as form fact\", \"status\": \"preserved\"}, \"annotationId\")",
        "                builder.add_item(\"annotations\", {\"annotationId\": annotation_id, \"kind\": \"form\", \"targetIds\": [page_id], \"body\": \"PDF annotation destination retained as form fact\", \"status\": \"preserved\"}, \"annotationId\")",
    ],
    "pdf-interleaved-paint": [
        "enumerate(list(reversed(builder.document[\"nodes\"][1:])))",
        "enumerate(sorted(builder.document[\"nodes\"][1:], key=lambda item: item[\"nodeId\"]))",
        "enumerate(sorted(builder.document[\"nodes\"][1:], key=lambda item: item.get(\"status\", \"\")))",
    ],
    "markdown-span-end": [
        "            \"tokenEnd\": max(0, token_end if token_end is not None else end_column - column + 2),",
        "            \"tokenEnd\": max(0, token_end if token_end is not None else end_column - column + 3),",
        "            \"tokenEnd\": max(0, 0),",
    ],
    "markdown-delimiter-resolution": [
        "    tokens = []",
        "    tokens = [{\"kind\": \"text\", \"raw\": raw, \"start\": 0, \"end\": len(raw)}]",
        "    tokens = _inline_tokens(\"\", references) or [{\"kind\": \"text\", \"raw\": raw, \"start\": 0, \"end\": len(raw)}]",
    ],
    "markdown-reference-resolution": [
        "    references = dict()",
        "    references = None",
        "    references = {\"__defect__\": (\"\", \"\")}",
    ],
    "markdown-table-separator": [
        "    row_lines = list(lines[1:])",
        "    row_lines = [lines[0]]",
        "    row_lines = list(reversed(lines))",
    ],
    "markdown-unsupported-construct": [
        "                node = _paragraph(builder, parent_id, \"\\n\".join(item[0] for item in node_records), node_records[0][1], column=node_records[0][2], references=references, footnotes=footnotes, state=state, status=\"normalized\")",
        "                node = _paragraph(builder, parent_id, \"\\n\".join(item[0] for item in node_records), node_records[0][1], column=node_records[0][2], references=references, footnotes=footnotes, state=state, status=\"ambiguous\")",
        "                node = _paragraph(builder, parent_id, \"\\n\".join(item[0] for item in node_records), node_records[0][1], column=node_records[0][2], references=references, footnotes=footnotes, state=state, status=\"unavailable\")",
    ],
    "query-field-mapping": [
        "    result.sort(key=lambda item: str(item.get(\"kind\", \"\")))",
        "    result.sort(key=lambda item: str(item.get(\"status\", \"\")))",
        "    result.sort(key=lambda item: str(item.get(\"missing\", \"\")))",
        "    result.sort(key=lambda item: \"constant\")",
    ],
    "query-index-validation": [
        "    if index.get(\"authority\") != expected[\"authority\"] and False:",
        "    if False and index.get(\"authority\") != expected[\"authority\"]:",
        "    if index.get(\"authority\") == \"__never__\":",
        "    if index.get(\"authority\") is None:",
    ],
    "canonical-collection-order": [
        "            return list(reversed(result))",
        "            return sorted(result, key=lambda item: item[id_field], reverse=True)",
    ],
    "canonical-authority-validation": [
        "    if 0:\n        _validate_authority(document)\n    projected = projection_document(document, projection)",
        "    if document.get(\"documentId\") == \"__never__\":\n        _validate_authority(document)\n    projected = projection_document(document, projection)",
    ],
    "canonical-key-order": [
        "return {key: child for key, child in sorted(normalized, key=lambda item: len(item[0]))}",
        "return {key: child for key, child in sorted(normalized, key=lambda item: item[0], reverse=True)}",
        "return {key: child for key, child in sorted(normalized, key=lambda item: item[0][::-1])}",
    ],
    "release-claim-source-check": [
        "    require(1 == 1, \"independent corpus runner is not claimed\")",
        "    require(\"independent\" not in \"\", \"independent corpus runner is not claimed\")",
    ],
    "release-required-checks": [
        "    passed = bool(checks)",
        "    passed = True if checks else False",
    ],
    "release-status-aggregation": [
        "        passed = command_result.get(\"return_code\") is not None",
        "        passed = bool(command_result)",
        "        passed = command_result.get(\"return_code\") != -999",
    ],
}


def _variant_case(base: dict[str, Any], index: int, new_text: str) -> dict[str, Any]:
    case = copy.deepcopy(base)
    base_id = str(base["id"])
    suffix = f"-variant-{index:02d}"
    case["id"] = base_id + suffix
    case["operatorId"] = str(base["operatorId"]) + suffix
    case["variantOf"] = base_id
    changes = case["patch"]["changes"]
    if len(changes) != 1:
        raise ValueError(f"variant expansion expects one patch change: {base_id}")
    changes[0]["new"] = new_text
    return case


def _variant_invariant(base_row: dict[str, Any], case: dict[str, Any], index: int) -> dict[str, Any]:
    row = copy.deepcopy(base_row)
    row["id"] = str(base_row["id"]) + f"-V{index:02d}"
    row["operatorId"] = case["operatorId"]
    row["caseId"] = case["id"]
    return row


def expand(contract: dict[str, Any]) -> dict[str, Any]:
    cases = list(contract["cases"])
    base_cases = {str(case["id"]): case for case in cases if "variantOf" not in case}
    matrix = list(contract["invariantMatrix"])
    rows_by_case = {str(row["caseId"]): row for row in matrix}
    generated_cases: list[dict[str, Any]] = []
    generated_rows: list[dict[str, Any]] = []
    for base_id, replacements in VARIANTS.items():
        base = base_cases.get(base_id)
        if base is None:
            raise ValueError(f"variant base case is missing: {base_id}")
        base_row = rows_by_case.get(base_id)
        if base_row is None:
            raise ValueError(f"variant base invariant is missing: {base_id}")
        for index, new_text in enumerate(replacements, start=1):
            case = _variant_case(base, index, new_text)
            generated_cases.append(case)
            generated_rows.append(_variant_invariant(base_row, case, index))

    retained_cases = [case for case in cases if "variantOf" not in case]
    for case in retained_cases:
        profile = str(case.get("releaseProfile"))
        case_id = str(case.get("id"))
        if case_id in BASE_REPLACEMENTS:
            case["patch"]["changes"][0]["new"] = BASE_REPLACEMENTS[case_id]
        if profile == "canonical" and any(case_id in probe_ids for probe_ids in FORMAT_PROBES.values()):
            case["gateCommand"] = ["python", "tools/defect_profile_canonical.py", "--probe", case_id]
        elif any(case_id in probe_ids for probe_ids in FORMAT_PROBES.values()):
            case["gateCommand"] = ["python", "tools/defect_profile_formats.py", "--format", profile, "--probe", case_id]
        base_cases[case_id] = case
    gate_commands = {str(case["id"]): copy.deepcopy(case.get("gateCommand")) for case in retained_cases if case.get("gateCommand")}
    for case in generated_cases:
        parent = str(case.get("variantOf", ""))
        if parent in gate_commands:
            case["gateCommand"] = copy.deepcopy(gate_commands[parent])
    retained_rows = [row for row in matrix if str(row.get("caseId")) in {str(case["id"]) for case in retained_cases}]
    if "meta-equivalent-comment" not in {str(row.get("caseId")) for row in retained_rows}:
        retained_rows.append(
            {
                "id": "DFIR-89-META-EQUIVALENT",
                "ownerIssues": [89],
                "requirementIds": ["DFIR-QA-008"],
                "operatorId": "meta-equivalent-comment",
                "caseId": "meta-equivalent-comment",
                "releaseProfile": "adapter-common",
                "must": True,
            }
        )
    result = copy.deepcopy(contract)
    result["cases"] = sorted(retained_cases + generated_cases, key=lambda item: str(item["id"]))
    result["invariantMatrix"] = sorted(retained_rows + generated_rows, key=lambda item: str(item["id"]))
    result["supportFiles"] = sorted(set(result.get("supportFiles", [])) | {"tools/defect_profile_canonical.py", "tools/defect_profile_formats.py"})
    return result


def main() -> int:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8-sig"))
    expanded = expand(contract)
    CONTRACT.write_text(json.dumps(expanded, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    counts: dict[str, int] = {}
    for case in expanded["cases"]:
        if case.get("expectedOutcome") == "non-equivalent":
            profile = str(case.get("releaseProfile"))
            counts[profile] = counts.get(profile, 0) + 1
    print(json.dumps({"caseCount": len(expanded["cases"]), "invariantCount": len(expanded["invariantMatrix"]), "profileCounts": counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
