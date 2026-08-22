"""Run the complete product acceptance matrix through unittest."""

from __future__ import annotations

import unittest

from tools import run_acceptance


class AcceptanceMatrixTests(unittest.TestCase):
    def test_all_product_acceptance_cases(self) -> None:
        self.assertEqual(run_acceptance.main(["--all"]), 0)


if __name__ == "__main__":
    unittest.main()
