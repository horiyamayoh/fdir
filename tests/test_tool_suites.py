"""Discover product-focused test modules kept beside their implementation."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str):
    return loader.discover(str(ROOT / "tools"), pattern="test_*.py", top_level_dir=str(ROOT))
