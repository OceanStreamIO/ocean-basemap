"""The shard split, which the published tree's completeness rests on."""

from __future__ import annotations

from pathlib import Path

import pytest

from basemap.pipeline.xyz import MAX_ZOOM, Shard, _check_complete, stage_path_for

WHOLE_TREE = 89_478_485          # sum(4**z for z in range(14))


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


def test_a_tile_can_always_reach_the_global_sea_archive():
    """Every zoom must be able to find a stand-in, or the tree gets holes.

    The global sea archive stops at z8 and is the only thing under open ocean,
    so a tile at zoom Z has to be allowed to reach Z-8 levels up. That was a
    fixed four, which is exactly right at z12 and one short at z13 -- so a z13
    render came out missing 2.1 million open-ocean tiles, every one of them a
    hole that looks identical to a tile with nothing in it.

    Caught by the count check rather than by eye, which is the whole reason the
    count check exists.
    """
    import importlib
    import os

    from basemap.pipeline import xyz

    previous = os.environ.get("BASEMAP_XYZ_MAX_ZOOM")
    try:
        for depth in (12, 13, 14, 15):
            os.environ["BASEMAP_XYZ_MAX_ZOOM"] = str(depth)
            importlib.reload(xyz)
            reach = xyz.MAX_ZOOM - xyz.MAX_UPSAMPLE
            assert reach <= xyz.GLOBAL_SEA_MAX_ZOOM, (
                f"at z{depth} a tile reaches only z{reach}, "
                f"but the global sea archive stops at z{xyz.GLOBAL_SEA_MAX_ZOOM}"
            )
        # And z12 still reaches the four levels it always did, so a z12 tree
        # renders exactly as it did before this was derived.
        os.environ["BASEMAP_XYZ_MAX_ZOOM"] = "12"
        importlib.reload(xyz)
        assert xyz.MAX_UPSAMPLE == 4
    finally:
        if previous is None:
            os.environ.pop("BASEMAP_XYZ_MAX_ZOOM", None)
        else:
            os.environ["BASEMAP_XYZ_MAX_ZOOM"] = previous
        importlib.reload(xyz)
