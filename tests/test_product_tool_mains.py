"""Exercise product-focused legacy test modules through unittest discovery."""

from __future__ import annotations

import unittest


class ProductToolMainTests(unittest.TestCase):
    def test_independent_index_suite(self) -> None:
        from tools import test_independent_index
        self.assertEqual(test_independent_index.main(), 0)

    def test_query_surface_suite(self) -> None:
        from tools import test_query_surface
        self.assertEqual(test_query_surface.main(), 0)
