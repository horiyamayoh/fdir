"""Compatibility entry point for the full source-level defect campaign."""

from __future__ import annotations

try:
    from run_defect_injection_campaign import main
except ImportError:  # pragma: no cover - package-style execution
    from tools.run_defect_injection_campaign import main


if __name__ == "__main__":
    raise SystemExit(main())
