"""Verify that the frozen package includes the exact collected license files."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify_bundle(bundle):
    bundle = Path(bundle)
    for required in (
        "LICENSE", "THIRD_PARTY_NOTICES.md", "docs/licensing/HIKROBOT.md",
        "licenses/OpenCV-Apache-2.0.txt", "licenses/BRISQUE-UT-Austin.txt",
        "licenses/Qt-6.11.2-THIRD-PARTY.txt", "licenses/SOURCES.json",
    ):
        if not (bundle / required).is_file():
            raise RuntimeError(f"Required license document missing: {required}")
    installed = bundle / "licenses" / "installed"
    manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
    expected = {
        "PyQt6", "PyQt6-Qt6", "PyQt6-sip", "opencv-contrib-python",
        "numpy", "tifffile", "pyinstaller", "pyinstaller-hooks-contrib", "CPython",
    }
    if {p["name"] for p in manifest["packages"]} != expected:
        raise RuntimeError("License manifest does not cover the expected distributions")
    for package in manifest["packages"]:
        if not package["notices"]:
            raise RuntimeError(f"No license for {package['name']}")
        for notice in package["notices"]:
            path = PurePosixPath(notice["path"])
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe license manifest path: {path}")
            content = (installed / path).read_bytes()
            if hashlib.sha256(content).hexdigest() != notice["sha256"]:
                raise RuntimeError(f"License checksum mismatch: {path}")
    sources = json.loads((bundle / "licenses" / "SOURCES.json").read_text(encoding="utf-8"))
    for source in sources["files"]:
        path = PurePosixPath(source["path"])
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Unsafe static license path: {path}")
        if hashlib.sha256((bundle / path).read_bytes()).hexdigest() != source["sha256"]:
            raise RuntimeError(f"Static license checksum mismatch: {path}")
    return sum(len(package["notices"]) for package in manifest["packages"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    print(f"Verified {verify_bundle(args.bundle)} installed-package notices and static license documents")
