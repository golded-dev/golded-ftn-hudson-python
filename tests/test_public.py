"""Public runtime/type exports and executable README example."""

import ast
from pathlib import Path

import golded_ftn_hudson


def test_exports() -> None:
    assert golded_ftn_hudson.__all__ == ["HudsonReader"]
    assert golded_ftn_hudson.HudsonReader.__module__ == "golded_ftn_hudson.reader"
    package = Path(golded_ftn_hudson.__file__).parent
    assert (package / "py.typed").is_file()
    source = (package / "__init__.py").read_text()
    assert any(isinstance(node, ast.ImportFrom) for node in ast.parse(source).body)


def test_readme_example() -> None:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    example = readme.read_text().split("```python\n", 1)[1].split("```", 1)[0]
    exec(compile(example, str(readme), "exec"), {})
