"""Focused negative probes used by the executable defect campaign.

Each probe exercises the real validator/adapter function with a minimal
machine-readable input.  A probe exits zero when the base implementation
behaves as expected and non-zero when the injected defect escapes or changes
the accepted result.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import adapter_common  # type: ignore  # noqa: E402
import ir_validation  # type: ignore  # noqa: E402


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8-sig"))


def _expect_rejection(action: Callable[[], Any]) -> int:
    try:
        action()
    except Exception:
        return 0
    return 1


def _expect_acceptance(action: Callable[[], Any]) -> int:
    try:
        action()
    except Exception:
        return 1
    return 0


def probe_required() -> int:
    document = _load("callout.json")
    document.pop("documentId", None)
    return _expect_rejection(lambda: ir_validation.validate_normative_schema(document))


def probe_wrong_type() -> int:
    return _expect_rejection(
        lambda: ir_validation._id_ref(
            "node-wrong",
            {"node-wrong": "nodes"},
            {"nodes"},
            "$.probe",
            optional=False,
            target_kinds={"paragraph"},
            kind_by_id={"node-wrong": "table"},
        )
    )


def probe_reciprocity() -> int:
    document = _load("callout.json")
    nodes = {item["nodeId"]: item for item in document["nodes"]}
    child = nodes["node-run"]
    parent = nodes["node-callout"]
    # node-run is first assigned to node-paragraph by the normal child list,
    # then declares node-callout as its parent.  node-callout also lists the
    # child, so the reciprocity check passes and the conflicting-parent check
    # is the isolated expected rejection.  With that check disabled the final
    # parent assignment remains reachable and otherwise well formed.
    child["parentId"] = parent["nodeId"]
    parent["childIds"] = [child["nodeId"]]
    return _expect_rejection(lambda: ir_validation.validate_document(document))


def probe_extension() -> int:
    document = _load("callout.json")
    extension = document["extensions"][0]
    extension["payload"] = {}
    return _expect_rejection(lambda: ir_validation.validate_document(document))


def probe_status() -> int:
    builder = adapter_common.DocumentBuilder(Path("probe.docx"), "docx", "ECMA-376")
    diagnostic = builder.add_diagnostic("DFIR-PROBE-LOSS", "controlled non-preserved probe")
    builder.add_feature("probe-loss", "unsupported", target_id=builder.root_id, diagnostic_ids=[diagnostic])
    document = builder.finish()
    return _expect_acceptance(lambda: ir_validation.validate_document(document))


def main(argv: list[str] | None = None) -> int:
    probes: dict[str, Callable[[], int]] = {
        "required": probe_required,
        "wrong-type": probe_wrong_type,
        "reciprocity": probe_reciprocity,
        "extension": probe_extension,
        "status": probe_status,
    }
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 2 or argv[0] != "--probe" or argv[1] not in probes:
        print("usage: defect_profile_validator.py --probe <required|wrong-type|reciprocity|extension|status>", file=sys.stderr)
        return 2
    return probes[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
