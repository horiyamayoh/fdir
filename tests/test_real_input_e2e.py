"""Run the bounded four-format real-input regression suite."""

from __future__ import annotations

import unittest
from pathlib import Path

from tools import run_e2e


class RealInputE2ETests(unittest.TestCase):
    def test_all_formats_and_negative_cases(self) -> None:
        report = run_e2e.run_all()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["formats"]), 4)
        self.assertEqual(len(report["cases"]), 16)
        self.assertFalse(Path(report["workdir"]).exists())


if __name__ == "__main__":
    unittest.main()
