"""Tests for cli module — Typer command interface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from basemap.cli import app

runner = CliRunner()


class TestRunCommand:
    def test_unknown_step(self, tmp_path: Path):
        with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
             patch("basemap.cli.require_mount"):
            result = runner.invoke(app, ["run", "nonexistent"])
        assert result.exit_code != 0

    def test_run_all_skip_upload(self, tmp_path: Path):
        """'run all --skip-upload' should not include upload step."""
        called_steps: list[str] = []

        def fake_run_step(name, data_dir):
            called_steps.append(name)

        with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
             patch("basemap.cli.require_mount"), \
             patch("basemap.cli._run_step", side_effect=fake_run_step):
            result = runner.invoke(app, ["run", "all", "--skip-upload"])

        assert result.exit_code == 0
        assert "upload" not in called_steps
        assert len(called_steps) > 0

    def test_run_single_step(self, tmp_path: Path):
        called_steps: list[str] = []

        def fake_run_step(name, data_dir):
            called_steps.append(name)

        with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
             patch("basemap.cli.require_mount"), \
             patch("basemap.cli._run_step", side_effect=fake_run_step):
            result = runner.invoke(app, ["run", "hillshade"])

        assert result.exit_code == 0
        assert called_steps == ["hillshade"]


class TestOptionalSteps:
    def _run_all(self, tmp_path: Path, args: list[str]) -> tuple[int, list[str]]:
        called_steps: list[str] = []

        def fake_run_step(name, data_dir):
            called_steps.append(name)

        with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
             patch("basemap.cli.require_mount"), \
             patch("basemap.cli._run_step", side_effect=fake_run_step):
            result = runner.invoke(app, ["run", "all", *args])

        return result.exit_code, called_steps

    def test_excluded_by_default(self, tmp_path: Path):
        code, steps = self._run_all(tmp_path, [])
        assert code == 0
        assert "emodnet" not in steps
        assert "hr-bathymetry" not in steps
        assert "depth-polygons" in steps

    def test_included_on_request(self, tmp_path: Path):
        code, steps = self._run_all(tmp_path, ["--with", "emodnet"])
        assert code == 0
        assert "emodnet" in steps
        assert "hr-bathymetry" not in steps
        # Keeps canonical ordering
        assert steps.index("depth-polygons") < steps.index("emodnet") < steps.index("contours")

    def test_with_all(self, tmp_path: Path):
        code, steps = self._run_all(tmp_path, ["--with", "all"])
        assert code == 0
        assert {"emodnet", "hr-bathymetry"} <= set(steps)

    def test_rejects_non_optional_step(self, tmp_path: Path):
        code, steps = self._run_all(tmp_path, ["--with", "hillshade"])
        assert code != 0
        assert steps == []


class TestStatusCommand:
    def test_status_runs(self, tmp_path: Path):
        with patch("basemap.cli.get_data_dir", return_value=tmp_path), \
             patch("basemap.cli.get_tile_dir", return_value=tmp_path / "tiles"):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0


class TestHelp:
    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "basemap" in result.output.lower() or "ocean" in result.output.lower()

    def test_run_help(self):
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
