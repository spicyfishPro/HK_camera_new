import hashlib
import json
import shutil
import tempfile
import unittest
from importlib import metadata
from pathlib import Path

from scripts.collect_third_party_licenses import collect_licenses
from scripts.verify_license_bundle import verify_bundle

ROOT = Path(__file__).resolve().parents[1]


class LicensePackagingTests(unittest.TestCase):
    def test_actual_environment_round_trip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = Path(temp)
            for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
                shutil.copyfile(ROOT / name, bundle / name)
            shutil.copytree(ROOT / "licenses", bundle / "licenses")
            shutil.copytree(ROOT / "docs" / "licensing", bundle / "docs" / "licensing")
            manifest, datas = collect_licenses(bundle / "licenses" / "installed")
            self.assertEqual(len(datas) - 1, verify_bundle(bundle))
            numpy = next(p for p in manifest["packages"] if p["name"] == "numpy")
            self.assertGreater(len(numpy["notices"]), 1)  # Nested third-party notices survive.
            opencv = next(p for p in manifest["packages"] if p["name"] == "opencv-contrib-python")
            self.assertTrue(any("LICENSE-3RD-PARTY" in n["path"] for n in opencv["notices"]))
            target = bundle / "licenses" / "installed" / numpy["notices"][-1]["path"]
            target.write_text("damaged", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                verify_bundle(bundle)

    def test_missing_required_notice_fails(self):
        class EmptyDistribution:
            files = ()

        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(RuntimeError, "No license files"):
            collect_licenses(temp, distribution=lambda name: EmptyDistribution())

    def test_qt_upgrade_requires_notice_refresh(self):
        class NewQt:
            files = (Path("LICENSE"),)
            version = "6.12.0"

        def lookup(name):
            return NewQt() if name == "PyQt6-Qt6" else metadata.distribution(name)

        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(RuntimeError, "Refresh Qt third-party notices"):
            collect_licenses(temp, distribution=lookup)

    def test_static_provenance_checksums(self):
        sources = json.loads((ROOT / "licenses" / "SOURCES.json").read_text(encoding="utf-8"))
        for source in sources["files"]:
            with self.subTest(path=source["path"]):
                self.assertEqual(source["sha256"], hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
