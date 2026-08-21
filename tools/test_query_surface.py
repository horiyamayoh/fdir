"""Focused #103 tests for direct query and independent-index parity.

Expected values in this file are literal review data for ``callout.json``;
they are not obtained by walking ``COLLECTION_KEYS`` or by copying rows from
either implementation.  The persistent index test is deliberately kept
separate from ``query_ir`` import paths in ``test_independent_index.py``.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from query_ir import (  # noqa: E402
    QueryError,
    DOCUMENT_COLLECTION,
    _validated_document,
    find_field_equals,
    find_references,
    get_document_field,
    get_field,
    index_parity,
    list_entities_page,
    query_contract,
    query_field_coverage,
    query_fields,
    rebuild_index,
    validate_index,
)


def _load(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def _expect_rejection(callback, label: str) -> None:
    try:
        callback()
    except (QueryError, ValueError, KeyError, TypeError):
        return
    raise AssertionError(f"negative query case survived: {label}")


def main() -> int:
    document = _load("callout.json")
    contract = query_contract()
    if contract["fieldPathCount"] < 1500:
        raise AssertionError("generated authoritative field registry is unexpectedly small")
    coverage = query_field_coverage(document)
    if coverage["status"] != "passed" or coverage["unqueryableFacts"]:
        raise AssertionError(f"authoritative query coverage failed: {coverage['unqueryableFacts'][:3]}")
    if DOCUMENT_COLLECTION not in coverage["observedRegisteredFactCounts"]:
        raise AssertionError("document-level facts were not explored")

    # The batch path is not a validation cache for the public API: it is a
    # digest-bound internal snapshot.  A mutation after validation must be
    # rejected before a parity result can be accepted.
    validated = _validated_document(document)
    original_status = document["conversion"]["status"]
    document["conversion"]["status"] = "failed" if original_status != "failed" else "complete"
    _expect_rejection(validated.assert_current, "mutated validated batch")
    document["conversion"]["status"] = original_status

    # Literal positive facts cover nested payloads, list ordinals, provenance,
    # status, and an extension payload field.
    expected_fields = [
        ("nodes", "node-callout", "/layoutIds/0", "layout-callout"),
        ("geometries", "geometry-callout", "/primitives/0/width/value", "180"),
        ("extensions", "extension-docx-callout", "/payload/presetGeometry", "wedgeRoundRectCallout"),
    ]
    for collection, identifier, pointer, expected in expected_fields:
        if get_field(document, collection, identifier, pointer) != expected:
            raise AssertionError(f"direct field mismatch: {collection}/{identifier}{pointer}")
    if get_document_field(document, "/conversion/status") != "complete":
        raise AssertionError("direct document conversion status query failed")
    if find_field_equals(document, "/zIndex", 20, collection="layouts") != [
        {"collection": "layouts", "id": "layout-callout"}
    ]:
        raise AssertionError("direct integer equality query failed")
    if find_field_equals(document, "/zIndex", "20", collection="layouts"):
        raise AssertionError("direct integer field was compared as text")
    if not any(
        row["field"] == "/anchor/surfaceId"
        and row["ordinal"] is None
        and row["toId"] == "surface-page-1"
        for row in find_references(document, target_id="surface-page-1", source_collection="layouts")
    ):
        raise AssertionError("direct nested reference query failed")

    first_page = list_entities_page(document, "nodes", limit=2)
    if len(first_page["items"]) != 2 or not first_page["nextCursor"]:
        raise AssertionError("direct deterministic pagination did not produce a cursor")
    second_page = list_entities_page(document, "nodes", limit=2, cursor=first_page["nextCursor"])
    if {item["nodeId"] for item in first_page["items"]}.intersection(item["nodeId"] for item in second_page["items"]):
        raise AssertionError("direct pagination cursor repeated an entity")
    _expect_rejection(
        lambda: list_entities_page(document, "nodes", status="preserved", limit=2, cursor=first_page["nextCursor"]),
        "cursor reused with a different query",
    )

    # Null and missing remain different query states.
    blank = _load("cell-formula.json")
    for node in blank["nodes"]:
        if node["nodeId"] == "cell-b2":
            node["value"] = {"type": "blank", "value": None, "status": "preserved"}
            break
    else:
        raise AssertionError("cell-formula fixture has no cell-b2")
    null_rows = query_fields(blank, "/value/value", None, operator="eq", collection="nodes")
    if not any(item["id"] == "cell-b2" and item["presence"] == "null" for item in null_rows):
        raise AssertionError("stored null was not queryable")
    missing_rows = query_fields(blank, "/value/value", operator="is-missing", collection="nodes")
    if any(item["id"] == "cell-b2" for item in missing_rows):
        raise AssertionError("stored null was confused with missing")
    _expect_rejection(
        lambda: get_field(document, "nodes", "node-callout", "/notRegistered"),
        "unregistered field path",
    )

    index = rebuild_index(document)
    parity = index_parity(document, index)
    if parity["status"] != "passed" or parity["directFactCount"] != parity["indexFactCount"]:
        raise AssertionError("direct in-memory field parity failed")
    tampered = copy.deepcopy(index)
    tampered["fieldFacts"].pop()
    _expect_rejection(lambda: validate_index(document, tampered), "deleted in-memory field fact")

    output = {
        "schema": "fdir/query-surface-focused-test",
        "status": "passed",
        "fieldPathCount": contract["fieldPathCount"],
        "observedFactCount": coverage["checkedFactCount"],
        "unqueryableFacts": coverage["unqueryableFacts"],
        "directIndexFieldFacts": parity["indexFactCount"],
        "pid": os.getpid(),
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
