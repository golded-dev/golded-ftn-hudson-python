"""Strip checkout-only uv sources from the sdist without changing local files."""

import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class SdistHook(BuildHookInterface[BuilderConfig]):
    _temporary: TemporaryDirectory[str] | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        source = Path(self.root) / "pyproject.toml"
        config = re.sub(
            r"(?ms)^\[tool\.uv\.sources\]\n.*?(?=^\[|\Z)",
            "",
            source.read_text(encoding="utf-8"),
        )
        self._temporary = TemporaryDirectory(prefix="golded-ftn-hudson-sdist-")
        packed = Path(self._temporary.name) / "pyproject.toml"
        packed.write_text(config, encoding="utf-8")
        build_data["force_include"].pop(str(source), None)
        build_data["force_include"][str(packed)] = "pyproject.toml"

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
