"""rules_script 单测。"""

from __future__ import annotations

from ink2reel_driver.qc.rules_script import check_script


def _seg(sid: str, novel: str, dur: int = 6, prompt: str = "scene ok") -> dict:
    return {
        "segment_id": sid,
        "novel_text": novel,
        "duration_seconds": dur,
        "image_prompt": {"scene": prompt},
    }


def test_empty_prompt_block() -> None:
    ep = {
        "segments": [
            _seg("S1", "字" * 50, prompt="ok scene"),
            {
                "segment_id": "S2",
                "novel_text": "字" * 50,
                "duration_seconds": 6,
                "image_prompt": {},
            },
        ]
    }
    r = check_script(ep)
    assert r.verdict == "block"
    assert any(i.code == "prompt_empty" for i in r.issues)


def test_novel_length_block() -> None:
    ep = {"segments": [_seg("S1", "短")]}
    r = check_script(ep)
    assert r.verdict == "block"
    assert any(i.code == "novel_text_length" for i in r.issues)


def test_same_duration_streak_warn() -> None:
    novel = "旁白内容足够长用于测试规则。" * 3
    ep = {
        "segments": [
            _seg("S1", novel, 6, "p1 unique alpha"),
            _seg("S2", novel, 6, "p2 unique beta"),
            _seg("S3", novel, 6, "p3 unique gamma"),
        ]
    }
    r = check_script(ep)
    assert any(i.code == "same_duration_streak" for i in r.issues)


def test_prompt_similarity_warn() -> None:
    novel = "旁白内容足够长用于测试相邻相似度规则。" * 3
    same = "identical prompt text for similarity check pad pad pad"
    ep = {
        "segments": [
            _seg("S1", novel, 4, same),
            _seg("S2", novel, 5, same),
        ]
    }
    r = check_script(ep)
    assert any(i.code == "prompt_similarity" for i in r.issues)


def test_pass_short_pack() -> None:
    novel = "这是一段足够长度的旁白文本用于通过字数校验规则。" * 2
    ep = {
        "segments": [
            _seg("S1", novel, 8, "unique scene one with shelves"),
            _seg("S2", novel, 10, "unique scene two with counter"),
        ]
    }
    r = check_script(ep)
    assert r.verdict in ("pass", "warn")
    assert not r.blocked
