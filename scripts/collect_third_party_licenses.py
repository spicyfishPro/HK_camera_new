"""Collect notices from the installed build environment without importing native libraries.

The wheel contents are platform dependent. Run this on the machine building the
application; do not substitute license files from a different platform's wheel.
"""

import argparse
import hashlib
import json
import platform
import shutil
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath

PACKAGES = {
    "PyQt6": "runtime",
    "PyQt6-Qt6": "runtime",
    "PyQt6-sip": "runtime",
    "opencv-contrib-python": "runtime",
    "numpy": "runtime",
    "tifffile": "runtime",
    "pyinstaller": "build tool and bundled bootloader",
    "pyinstaller-hooks-contrib": "build tool and runtime hooks",
}


def is_notice(path):
    path = PurePosixPath(str(path))
    return "licenses" in (part.lower() for part in path.parts) or path.name.lower().startswith(
        ("license", "licence", "copying", "notice", "copyright")
    )


def collect_licenses(destination, *, project_root=None, distribution=metadata.distribution):
    """Return the manifest and the exact files to include in a PyInstaller bundle."""
    destination = Path(destination)
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    entries = []
    copied = []

    def copy_notice(source, relative):
        relative = PurePosixPath(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe license path: {relative}")
        source = Path(source)
        if not source.is_file() or not source.stat().st_size:
            raise RuntimeError(f"Missing or empty license: {relative}")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append((str(target), str(PurePosixPath("licenses/installed") / relative.parent)))
        return {"path": str(relative), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}

    for name, role in PACKAGES.items():
        dist = distribution(name)
        files = sorted((f for f in dist.files or () if is_notice(f)), key=str)
        if not files:
            raise RuntimeError(f"No license files in installed distribution: {name}")
        # Static Qt notices were checked against this exact upstream release.
        if name == "PyQt6-Qt6" and dist.version != "6.11.2":
            raise RuntimeError("Refresh Qt third-party notices before changing Qt 6.11.2")
        notices = [copy_notice(dist.locate_file(f), f"{name}/{f}") for f in files]
        entries.append({
            "name": name, "version": dist.version, "role": role,
            "license_expression": dist.metadata.get("License-Expression"),
            "project_urls": dist.metadata.get_all("Project-URL") or [],
            "wheel": dist.read_text("WHEEL"), "notices": notices,
        })

    # The Windows Python distribution supplies its own combined license file.
    # Some Unix Python installations omit it; use the audited upstream copy only
    # for the matching interpreter version, never silently for a newer runtime.
    python_version = platform.python_version()
    python_license = next((p for p in (
        Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE",
        root / "licenses" / f"Python-{python_version}.txt",
    ) if p.is_file()), None)
    if python_license is None:
        raise RuntimeError(f"No license for Python {python_version}; install its original LICENSE")
    entries.append({
        "name": "CPython", "version": python_version, "role": "runtime",
        "source": f"https://github.com/python/cpython/tree/v{python_version}",
        "notices": [copy_notice(python_license, "CPython/LICENSE.txt")],
    })
    manifest = {
        "schema_version": 1, "platform": sys.platform,
        "machine": platform.machine(), "packages": entries,
        "scope": "Installed-package notices; not a legal clearance or a complete binary SBOM.",
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    copied.append((str(manifest_path), "licenses/installed"))
    return manifest, copied


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result, files = collect_licenses(args.output)
    print(f"Collected {len(files) - 1} notices for {len(result['packages'])} packages")
