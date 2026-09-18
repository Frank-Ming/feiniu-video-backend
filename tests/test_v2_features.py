"""v5.1: pick_random only_first_episode + DELETE /api/videos/{id} + can_delete 权限。"""
import pytest

from app import scanner as scanner_mod
from app.scanner import VideoItem


def _mk_item(vid: str, series_id: str = "", ep: int = 0,
             sc: int = 0, is_series: bool = False, size: int = 100):
    return VideoItem(
        id=vid, name=f"{vid}.mp4", path=f"/v/{vid}.mp4",
        full_path=f"/v/{vid}.mp4", size=size, mtime=0.0,
        is_series=is_series, series_id=series_id,
        episode_no=ep, series_count=sc, siblings=[],
    )


def test_pick_random_excludes_mid_episodes_when_first_only(monkeypatch):
    pool = [
        _mk_item("plain1"),
        _mk_item("seriesA-ep1", series_id="A", ep=1, sc=5,
                 is_series=True),
        _mk_item("seriesA-ep2", series_id="A", ep=2, sc=5,
                 is_series=True),
        _mk_item("seriesA-ep3", series_id="A", ep=3, sc=5,
                 is_series=True),
        _mk_item("plain2"),
    ]
    monkeypatch.setattr(scanner_mod.scanner, "list_videos", lambda: pool)
    # 试 50 次，确保不返回 ep2/ep3
    for _ in range(50):
        v = scanner_mod.scanner.pick_random(
            max_size_bytes=10**9,
            only_first_episode=True,
        )
        assert v is not None
        assert v.episode_no <= 1, f"picked {v.id} ep={v.episode_no}"
    # 验证关掉开关时能选到 ep3
    picked_eps = set()
    for _ in range(200):
        v = scanner_mod.scanner.pick_random(
            max_size_bytes=10**9,
            only_first_episode=False,
        )
        if v:
            picked_eps.add(v.episode_no)
    # 概率上会偶尔选到 ep2/ep3
    assert 2 in picked_eps or 3 in picked_eps, (
        f"应能选到中间集，实际只挑到 {picked_eps}"
    )


def test_pick_random_exclude_ids_works(monkeypatch):
    pool = [_mk_item(f"v{i}") for i in range(5)]
    monkeypatch.setattr(scanner_mod.scanner, "list_videos", lambda: pool)
    # 排除 v0 v1
    for _ in range(20):
        v = scanner_mod.scanner.pick_random(
            max_size_bytes=10**9,
            exclude_ids=["v0", "v1"],
        )
        assert v is not None
        assert v.id not in {"v0", "v1"}


def test_pick_random_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr(scanner_mod.scanner, "list_videos", lambda: [])
    v = scanner_mod.scanner.pick_random(max_size_bytes=10**9)
    assert v is None
