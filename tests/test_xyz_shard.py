"""The shard split, which the published tree's completeness rests on."""

from __future__ import annotations

from pathlib import Path

import pytest

from basemap.pipeline.xyz import MAX_ZOOM, Shard, _check_complete, stage_path_for

WHOLE_TREE = 22_369_621          # sum(4**z for z in range(13))


def test_the_whole_tree_is_the_number_the_docs_quote():
    """Every other check here compares against this, so pin it."""
    assert Shard().expected_tiles(MAX_ZOOM) == WHOLE_TREE
    assert sum(4**z for z in range(MAX_ZOOM + 1)) == WHOLE_TREE


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7, 8, 64])
def test_shards_partition_the_tree_exactly(count):
    """No tile rendered twice and none left out, at any shard count.

    A shard that is short puts a hole in the published tree, and a column
    rendered twice makes the resume marks ambiguous. Both are silent, so the
    arithmetic is checked rather than trusted.
    """
    shards = [Shard(i, count) for i in range(count)]
    assert sum(s.expected_tiles(MAX_ZOOM) for s in shards) == WHOLE_TREE

    for z in range(MAX_ZOOM + 1):
        seen: set[int] = set()
        for shard in shards:
            columns = set(shard.columns(z))
            assert not (columns & seen), f"z{z} columns overlap"
            seen |= columns
        assert seen == set(range(1 << z)), f"z{z} columns do not cover the zoom"


def test_shards_are_balanced_because_they_stride_rather_than_block():
    """Contiguous blocks would differ in cost several-fold.

    A block of columns over the Pacific is nearly all fast-path open ocean and
    one over Europe is nearly all coastline; the job then waits for the slowest.
    Striding samples the whole world in every shard, so the counts come out
    within a tile of each other.
    """
    for count in (2, 4, 7, 8):
        sizes = [Shard(i, count).expected_tiles(MAX_ZOOM) for i in range(count)]
        assert max(sizes) - min(sizes) <= WHOLE_TREE * 0.01
        assert list(Shard(0, count).columns(12))[:2] == [0, count]


def test_a_shard_of_one_is_every_column():
    assert list(Shard().columns(3)) == list(range(8))


@pytest.mark.parametrize("index,count", [(-1, 4), (4, 4), (0, 0), (1, 1)])
def test_a_nonsense_shard_is_refused_at_construction(index, count):
    with pytest.raises(ValueError):
        Shard(index, count)


def test_the_unsharded_stage_path_is_unchanged(monkeypatch):
    """The existing build Job must not start writing somewhere new."""
    monkeypatch.delenv("BASEMAP_XYZ_STAGE", raising=False)
    assert stage_path_for(Path("/data")).name == "xyz.stage.mbtiles"
    assert Shard().tag is None


def test_a_shard_names_its_own_stage(monkeypatch):
    monkeypatch.delenv("BASEMAP_XYZ_STAGE", raising=False)
    assert stage_path_for(Path("/data"), Shard(1, 4)).name == "xyz.stage.001of004.mbtiles"


def test_an_explicit_stage_wins_because_wal_needs_local_disk(monkeypatch):
    monkeypatch.setenv("BASEMAP_XYZ_STAGE", "/scratch/job/x.mbtiles")
    assert stage_path_for(Path("/data"), Shard(2, 4)) == Path("/scratch/job/x.mbtiles")


def test_shards_come_from_the_environment_unset_meaning_the_whole_tree(monkeypatch):
    monkeypatch.delenv("BASEMAP_XYZ_SHARD", raising=False)
    monkeypatch.delenv("BASEMAP_XYZ_SHARDS", raising=False)
    assert Shard.from_env() == Shard(0, 1)
    monkeypatch.setenv("BASEMAP_XYZ_SHARDS", "4")
    monkeypatch.setenv("BASEMAP_XYZ_SHARD", "3")
    assert Shard.from_env() == Shard(3, 4)


def test_a_short_shard_is_an_error_and_a_short_whole_tree_is_not():
    """A shard that rendered nothing looks like one with nothing to render.

    The first flat tree shipped with no contours in any of its twenty-two
    million tiles and nothing said so. Sharded, a short count means a hole
    nobody downstream would notice, so it stops the job. Unsharded, the render
    resumes across runs and a short count is a normal intermediate state.
    """
    with pytest.raises(SystemExit, match="incomplete"):
        _check_complete(Path("xyz.stage.001of004.mbtiles"), 10, Shard(1, 4), MAX_ZOOM)

    _check_complete(Path("xyz.stage.mbtiles"), 10, Shard(), MAX_ZOOM)      # warns only
    _check_complete(Path("xyz.stage.001of004.mbtiles"),
                    Shard(1, 4).expected_tiles(MAX_ZOOM), Shard(1, 4), MAX_ZOOM)
