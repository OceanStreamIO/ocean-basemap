"""Every third-party package the code imports must be a declared dependency.

The build Job installs from the lockfile with no dev extras, so a package that
only happens to be in a local venv passes every test here and then fails there
-- which is how a missing `pmtiles` ended a four-hour render at its last step.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Import names that differ from their distribution names.
DISTRIBUTION = {"PIL": "pillow"}


def _declared() -> set[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].lower().replace("_", "-") for d in deps}


def _imported() -> set[str]:
    names = set()
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {n for n in names if n not in sys.stdlib_module_names and n != "basemap"}


def test_every_import_is_declared():
    declared = _declared()
    missing = sorted(
        n for n in _imported()
        if DISTRIBUTION.get(n, n).lower().replace("_", "-") not in declared
    )
    assert not missing, f"imported but not in [project.dependencies]: {missing}"
