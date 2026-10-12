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


# The steps the remote build Job runs, from deploy/edito/relief-build/job.sh.
REMOTE_STEPS = ("bathymetry", "hypsometric")


def test_remote_build_steps_do_not_shell_out_to_gdal():
    """The Job's image has GDAL only inside the rasterio and fiona wheels.

    No gdalwarp, no ogr2ogr, no gdal_translate: the command-line tools are a
    separate package that image does not carry. A step that calls one runs
    perfectly here, where GDAL is installed system-wide, and dies remotely --
    which is what ended a rebuild after it had already re-read every DEM.
    """
    offenders = []
    for step in REMOTE_STEPS:
        path = ROOT / "src" / "basemap" / "pipeline" / f"{step}.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "basemap.utils.gdal":
                offenders.append(f"{step}.py imports {[a.name for a in node.names]}")
            if isinstance(node, ast.Import):
                offenders += [
                    f"{step}.py imports {a.name}"
                    for a in node.names
                    if a.name == "basemap.utils.gdal"
                ]
    assert not offenders, "GDAL command-line tools are not in the build image: " + "; ".join(
        offenders
    )


# Files the pipeline reads from the repository root rather than from src/.
# The build Job sends the whole repository as a git archive and so cannot lose
# them; the cluster sync sends an explicit path list and can.
ROOT_DATA_FILES = ("lake-sources.json", "lake-depths.json")


def test_the_cluster_sync_ships_every_root_data_file():
    """deploy/hpc/sync-code.sh sends a path list, not the whole repository.

    lake-depths.json was added to the root and not to that list, and
    lake_depth_overrides() returns an empty dict when the file is absent -- so
    the cluster would have rendered every corrected lake at its HydroLAKES
    depth and nothing in the log would have said so.
    """
    script = (ROOT / "deploy" / "hpc" / "sync-code.sh").read_text()
    paths = next(line for line in script.splitlines() if line.startswith("PATHS="))
    missing = [name for name in ROOT_DATA_FILES if name not in paths]
    assert not missing, f"read at runtime but never sent to the cluster: {missing}"


# The two scripts that generate the viewer's config.js. They must generate the
# same one: the bucket copy carried the corrected lake depths and the cluster
# copy did not, so the same lake showed two different depths depending on which
# viewer you opened.
CONFIG_GENERATORS = ("apply.sh", "publish-viewer.sh")
CONFIG_PARTS = ("tile-versions.sh", "lake-depths.sh")


def test_both_viewer_configs_are_built_from_the_same_parts():
    """Each generator sources every shared block, rather than inlining one."""
    missing = {
        f"{script}: {part}"
        for script in CONFIG_GENERATORS
        for part in CONFIG_PARTS
        if part not in (ROOT / "deploy" / "edito" / script).read_text()
    }
    assert not missing, f"viewer config generators have drifted: {sorted(missing)}"
