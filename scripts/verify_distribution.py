"""Inspect/rebuild a distribution and test its wheel without sibling checkouts.

Set GOLDED_WHEELHOUSE for unpublished dependencies; otherwise use the public index.
"""

from __future__ import annotations

import email
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    (wheel,) = (ROOT / "dist").glob("*.whl")
    (sdist,) = (ROOT / "dist").glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = email.message_from_bytes(
            archive.read(next(n for n in names if n.endswith("/METADATA")))
        )
        assert metadata["Name"] == project["name"]
        assert metadata["Version"] == project["version"]
        assert metadata["License-Expression"] == "MIT"
        assert any(n.endswith("/py.typed") for n in names)
        assert any(n.endswith("/licenses/LICENSE") for n in names)
        assert not any("file:" in d for d in metadata.get_all("Requires-Dist", []))
    with tempfile.TemporaryDirectory(prefix="golded-distribution-") as temporary:
        work = Path(temporary)
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                assert not Path(member.name).is_absolute() and ".." not in parts
                assert not {".env", ".git", ".venv", "__pycache__"}.intersection(parts)
            archive.extractall(work, filter="data")
        (source,) = work.iterdir()
        config = tomllib.loads((source / "pyproject.toml").read_text())
        assert "sources" not in config.get("tool", {}).get("uv", {})
        run(["uv", "build", "--no-sources", "--wheel"], source)
        (rebuilt,) = (source / "dist").glob("*.whl")
        with zipfile.ZipFile(wheel) as first, zipfile.ZipFile(rebuilt) as second:
            assert set(first.namelist()) == set(second.namelist())
            assert all(first.read(n) == second.read(n) for n in first.namelist())
        for index, candidate in enumerate([wheel, rebuilt]):
            env = work / f"consumer-{index}"
            run(["uv", "venv", "--python", sys.executable, str(env)], work)
            python = str(env / "bin/python")
            install = [
                "uv",
                "pip",
                "install",
                "--python",
                python,
                "--no-cache",
                "--reinstall",
            ]
            if os.environ.get("GOLDED_WHEELHOUSE"):
                install += [
                    "--no-index",
                    "--find-links",
                    os.environ["GOLDED_WHEELHOUSE"],
                ]
            run(install + [str(candidate)], work)
            run(["uv", "pip", "check", "--python", python], work)
            module = next(p.name for p in (source / "src").iterdir() if p.is_dir())
            run(
                [
                    python,
                    "-I",
                    "-c",
                    f"import {module}; assert 'site-packages' in {module}.__file__",
                ],
                work,
            )
        # The suite matrix executes tests after deleting the extracted src tree.
    print("Metadata, contents, sdist rebuild and two clean consumer installs passed.")


if __name__ == "__main__":
    main()
