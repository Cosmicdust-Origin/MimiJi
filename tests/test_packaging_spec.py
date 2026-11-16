from __future__ import annotations

import unittest
from pathlib import Path


class PackagingSpecTests(unittest.TestCase):
    def test_external_icu_runtime_is_removed_from_bundled_binaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "packaging" / "mimiji.spec").read_text(encoding="utf-8")

        self.assertIn("EXTERNAL_ICU_DLL", source)
        self.assertIn("without_external_icu(without_unused_qt(_analysis.binaries))", source)


if __name__ == "__main__":
    unittest.main()
