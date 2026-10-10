"""Check the locked runtime distributions against reviewed local license metadata.

This is a reproducibility gate, not a legal opinion.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re


APPROVED = {
    "MIT": {
        "annotated-doc", "annotated-types", "anyio", "beautifulsoup4",
        "et-xmlfile", "fastapi", "h11", "openpyxl", "pydantic",
        "pydantic-core", "soupsieve", "typing-inspection",
    },
    "BSD-3-Clause": {"click", "idna", "markupsafe", "starlette", "uvicorn"},
    "BSD": {"jinja2", "xlrd"},
    "Apache-2.0": {"opentelemetry-api", "python-multipart"},
    "PSF-2.0": {"typing-extensions"},
}


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _lock(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or line[0].isspace():
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if match is None:
            raise ValueError(f"Unreviewed lock entry: {line}")
        name, version = _name(match[1]), match[2]
        if name in result:
            raise ValueError(f"Duplicate locked distribution: {name}")
        result[name] = version
    return result


def _license(distribution: metadata.Distribution) -> str:
    value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
    if value in {"MIT", "MIT License"}:
        return "MIT"
    if value in {"BSD", "BSD-3-Clause", "Apache-2.0", "PSF-2.0"}:
        return value
    if value is None and any(
        item == "License :: OSI Approved :: BSD License"
        for item in distribution.metadata.get_all("Classifier") or []
    ):
        return "BSD"
    raise ValueError(f"Unreviewed license declaration for {distribution.metadata['Name']}: {value!r}")


def check(path: Path) -> int:
    locked = _lock(path)
    reviewed = {name for names in APPROVED.values() for name in names}
    if set(locked) != reviewed:
        raise ValueError(f"Review dependency additions/removals: +{sorted(set(locked)-reviewed)}, "
                         f"-{sorted(reviewed-set(locked))}")
    for name, version in locked.items():
        package = metadata.distribution(name)
        if package.version != version:
            raise ValueError(f"Installed {name} {package.version} differs from lock {version}")
        kind = _license(package)
        if name not in APPROVED[kind]:
            raise ValueError(f"Review changed license for {name}: {kind}")
        files = [package.locate_file(item) for item in package.files or []
                 if any(part.endswith(".dist-info") for part in item.parts)
                 and item.name.lower().startswith(("license", "licence", "copying"))]
        if not files or not any(item.is_file() and item.stat().st_size > 0 for item in files):
            raise ValueError(f"Installed {name} has no nonempty license file")
    return len(locked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", nargs="?", type=Path, default=Path("requirements.lock"))
    args = parser.parse_args()
    try:
        count = check(args.lock)
    except (ValueError, metadata.PackageNotFoundError, OSError) as exc:
        parser.exit(1, f"Dependency license audit failed: {exc}\n")
    print(f"{count} locked runtime distributions: versions, license declarations and files OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
