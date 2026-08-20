"""Focused probes for shared adapter invariants."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from adapter_common import DocumentBuilder  # type: ignore  # noqa: E402


def main() -> int:
    path = Path("identity-probe.bin")
    docx = DocumentBuilder(path, "docx", "ECMA-376")
    xlsx = DocumentBuilder(path, "xlsx", "Office Open XML")
    # The format is part of the authoritative identity seed.  A shared seed
    # would make unrelated source formats collide.
    return 0 if docx.root_id != xlsx.root_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
