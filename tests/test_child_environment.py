from __future__ import annotations

import os
import unittest
from unittest import mock

from tools import build_qualification_bundle


class QualificationChildEnvironmentTests(unittest.TestCase):
    def test_secret_tokens_are_not_inherited_by_qualification_children(self) -> None:
        command = [
            "python",
            "-c",
            "import os; print(os.environ.get('GITHUB_TOKEN', '<missing>')); print(os.environ.get('GH_TOKEN', '<missing>'))",
        ]
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret2"}, clear=False):
            status, stdout, stderr = build_qualification_bundle.run_qualification_command(command, 30)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(stdout.splitlines(), ["<missing>", "<missing>"])


if __name__ == "__main__":
    unittest.main()
