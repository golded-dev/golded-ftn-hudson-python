"""Inspect archives, rebuild the sdist, and test a wheel outside the checkout."""

import email
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run(args, cwd=cwd, env=env, check=True)


def main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    wheels = list((ROOT / "dist").glob("*.whl"))
    sdists = list((ROOT / "dist").glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "Expected exactly one wheel and sdist"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "golded_ftn_hudson/py.typed" in names
        expected = {
            "golded_ftn_hudson/" + path.name
            for path in (ROOT / "src/golded_ftn_hudson").glob("*")
            if path.is_file()
        }
        actual = {name for name in names if name.startswith("golded_ftn_hudson/")}
        assert actual == expected, (actual, expected)
        metadata = email.message_from_bytes(
            archive.read(next(n for n in names if n.endswith("/METADATA")))
        )
        assert metadata["Name"] == project["name"]
        assert metadata["Version"] == project["version"]
        assert metadata["Requires-Python"] == ">=3.12"
        assert metadata["License-Expression"] == "MIT"
        assert metadata.get_all("Requires-Dist", []) == ["golded-ftn<2,>=1.0.0"]
        assert any(n.endswith("/licenses/LICENSE") for n in names)
    with tempfile.TemporaryDirectory(prefix="golded-ftn-check-") as temporary:
        work = Path(temporary)
        with tarfile.open(sdists[0]) as archive:
            assert all(
                "/.venv/" not in n and "/.git/" not in n for n in archive.getnames()
            )
            archive.extractall(work, filter="data")
        (source,) = [p for p in work.iterdir() if p.is_dir()]
        for name in (
            "LICENSE",
            "README.md",
            "CONTRIBUTING.md",
            "src/golded_ftn_hudson/py.typed",
        ):
            assert (source / name).is_file(), name
        assert not (source / "uv.lock").exists(), "Development lock leaked into sdist"
        config = tomllib.loads((source / "pyproject.toml").read_text())
        assert "sources" not in config.get("tool", {}).get("uv", {}), (
            "Development sources leaked into sdist pyproject.toml"
        )
        assert not (source / "uv.toml").exists(), (
            "Development uv config leaked into sdist"
        )
        package_info = email.message_from_bytes((source / "PKG-INFO").read_bytes())
        assert package_info["Name"] == project["name"]
        assert package_info["Version"] == project["version"]
        assert package_info.get_all("Requires-Dist", []) == ["golded-ftn<2,>=1.0.0"]
        assert package_info["Requires-Python"] == ">=3.12"
        assert package_info["License-Expression"] == "MIT"
        run("uv", "build", "--wheel", cwd=source)
        (rebuilt,) = (source / "dist").glob("*.whl")
        with zipfile.ZipFile(wheels[0]) as first, zipfile.ZipFile(rebuilt) as second:
            assert set(first.namelist()) == set(second.namelist())
            assert all(first.read(n) == second.read(n) for n in first.namelist())
        core_output = work / "core-dist"
        run(
            "uv",
            "build",
            "--wheel",
            str(ROOT.parent / "golded-ftn"),
            "--out-dir",
            str(core_output),
            cwd=work,
        )
        (core_wheel,) = core_output.glob("*.whl")
        shutil.copytree(source / "tests", work / "tests")
        shutil.copy(source / "README.md", work / "README.md")
        for index, wheel in enumerate((wheels[0], rebuilt)):
            env_path = work / f"clean-env-{index}"
            run("uv", "venv", "--python", sys.executable, str(env_path), cwd=work)
            python = env_path / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(core_wheel),
                str(wheel),
                cwd=work,
            )
            run(
                str(python),
                "-c",
                "import golded_ftn, golded_ftn_hudson; "
                "from importlib.metadata import requires; "
                'assert requires("golded-ftn-hudson") == ["golded-ftn<2,>=1.0.0"]; '
                "print(golded_ftn.__file__, golded_ftn_hudson.__file__)",
                cwd=work,
            )
            run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "pytest",
                "mypy",
                cwd=work,
            )
            run("uv", "pip", "check", "--python", str(python), cwd=work)
            readme_example = (work / "README.md").read_text().split("```python\n")[1]
            readme_example = readme_example.split("```", 1)[0]
            run(str(python), "-c", readme_example, cwd=env_path)
            run(str(python), "-m", "pytest", "tests", "-q", cwd=work)
            run(str(python), "-m", "mypy", "--strict", "tests", cwd=work)
            run(str(python), "-m", "mypy.stubtest", "golded_ftn_hudson", cwd=work)
    print("Metadata, contents, sdist rebuild, both wheel tests and typing passed.")


if __name__ == "__main__":
    main()
