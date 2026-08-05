"""rules_ref 单测。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ink2reel_driver.qc.rules_ref import check_refs


def _touch_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=(1, 2, 3)).save(path)


def test_missing_asset_block(tmp_path: Path) -> None:
    _touch_png(tmp_path / "characters" / "林夏.png")
    seg = {
        "segment_id": "E1S01",
        "characters_in_segment": ["林夏", "幽灵"],
        "scenes": ["便利店"],
    }
    r = check_refs(seg, tmp_path)
    assert r.verdict == "block"
    assert any(i.code == "ref_missing" for i in r.issues)


def test_three_refs_warn(tmp_path: Path) -> None:
    for name in ("A", "B", "C"):
        _touch_png(tmp_path / "characters" / f"{name}.png")
    seg = {"segment_id": "S1", "characters_in_segment": ["A", "B", "C"], "scenes": []}
    r = check_refs(seg, tmp_path)
    assert r.verdict == "warn"
    assert any(i.code == "ref_count_warn" for i in r.issues)


def test_five_refs_block(tmp_path: Path) -> None:
    for name in ("A", "B", "C", "D", "E"):
        _touch_png(tmp_path / "characters" / f"{name}.png")
    seg = {
        "segment_id": "S1",
        "characters_in_segment": ["A", "B", "C", "D", "E"],
        "scenes": [],
    }
    r = check_refs(seg, tmp_path)
    assert r.verdict == "block"
    assert any(i.code == "ref_count_block" for i in r.issues)


def test_refs_ok(tmp_path: Path) -> None:
    _touch_png(tmp_path / "characters" / "林夏.png")
    _touch_png(tmp_path / "scenes" / "便利店.png")
    seg = {
        "segment_id": "E1S01",
        "characters_in_segment": ["林夏"],
        "scenes": ["便利店"],
    }
    r = check_refs(seg, tmp_path)
    assert r.verdict == "pass"
