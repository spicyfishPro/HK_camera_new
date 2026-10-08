import tempfile
import unittest
from pathlib import Path

from core.photo_catalog import scan_material, ThumbnailCache


class CatalogTests(unittest.TestCase):
    def test_exact_prefix_raw_versions_and_unknown_rep(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in (
                "A_Rep1_front.png",
                "A_Rep1_front_raw.png",
                "A_Rep1_front.jpg",
                "A_Rep2_side.png",
                "A1_Rep1_front.png",
                "AB_Rep1_front.png",
                "A_2026-08-03-12-00-00_001.png",
                "A_R3_side_2026-08-03-12-00-00_001.png",
                ".A_Rep1_front.partial.png",
                "A_something.png",
                "A.png",
            ):
                (root / name).write_bytes(b"not decoded by catalog")
            photos = scan_material(root, "A")
            self.assertEqual(len(photos), 6)
            self.assertFalse(
                any(p.path.name.startswith(("A1", "AB", ".")) for p in photos)
            )
            p = next(p for p in photos if p.path.name == "A_Rep1_front.png")
            self.assertEqual(p.raw_path.name, "A_Rep1_front_raw.png")
            self.assertEqual((p.rep, p.side), (1, "front"))
            self.assertEqual(sum(p.rep is None for p in photos), 2)

    def test_prefix_regex_metacharacters_literal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "A+_Rep1_front.png").touch()
            (root / "AAA_Rep1_front.png").touch()
            self.assertEqual(len(scan_material(root, "A+")), 1)

    def test_thumbnail_lru_budget(self):
        cache = ThumbnailCache(10)
        cache.put("a", "a", 4)
        cache.put("b", "b", 4)
        self.assertEqual(cache.get("a"), "a")
        cache.put("c", "c", 4)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.bytes, 8)
        cache.put("a", "new", 2)
        self.assertEqual(cache.bytes, 6)
