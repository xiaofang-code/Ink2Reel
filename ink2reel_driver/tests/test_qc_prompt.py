"""rules_prompt 单测。"""

from __future__ import annotations

from ink2reel_driver.qc.rules_prompt import PROMPT_LEN_LIMIT, check_prompt


def test_clean_pass() -> None:
    r = check_prompt("A quiet night store, woman restocking shelves, cool fluorescent light.")
    assert r.verdict == "pass"
    assert not r.issues


def test_too_long_block() -> None:
    text = "x" * (PROMPT_LEN_LIMIT + 1)
    r = check_prompt(text)
    assert r.verdict == "block"
    assert any(i.code == "prompt_too_long" for i in r.issues)


def test_length_warn() -> None:
    text = "a" * 1300
    r = check_prompt(text)
    assert r.verdict == "warn"
    assert any(i.code == "prompt_length_warn" for i in r.issues)


def test_sensitive_auto_fixed() -> None:
    r = check_prompt("the sinister figure leans menacingly with dark intent")
    assert r.verdict == "fixed"
    assert r.fixed_text is not None
    assert "sinister" not in r.fixed_text.lower()
    assert "menacingly" not in r.fixed_text.lower()
    assert any(i.code == "sensitive_auto_fixed" for i in r.issues)


def test_cn_sensitive_fixed() -> None:
    r = check_prompt("字幕：这个反派不死心")
    assert r.verdict == "fixed"
    assert "反派" not in (r.fixed_text or "")
    assert "不死心" not in (r.fixed_text or "")


def test_fixed_then_length_recheck() -> None:
    # 极长 + 敏感词：先长度 block（不进入替换）
    r = check_prompt("sinister " + ("y" * PROMPT_LEN_LIMIT))
    assert r.verdict == "block"
    assert any(i.code == "prompt_too_long" for i in r.issues)
