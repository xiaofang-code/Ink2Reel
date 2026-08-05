"""textclean 单测。"""

from __future__ import annotations

from ink2reel_driver.textclean import clean_novel


def test_clean_headings_quotes_blanks() -> None:
    raw = """# 第1章 十七分钟

凌晨两点。


>>> 被引用的一行

## 第2章 城西

正文继续。


"""
    # 用标准 > 引用
    raw = """# 第1章 十七分钟

凌晨两点。


> 被引用的一行

## 第2章 城西

正文继续。



尾声。
"""
    r = clean_novel(raw)
    assert "第1章 十七分钟" in r.chapters
    assert "第2章 城西" in r.chapters
    assert "# 第1章" not in r.cleaned_text
    assert "## 第2章" not in r.cleaned_text
    assert "被引用的一行" in r.cleaned_text
    assert not any(line.lstrip().startswith(">") for line in r.cleaned_text.splitlines() if line.strip())
    # ≥3 空行压成 ≤1 空行（两个换行）
    assert "\n\n\n" not in r.cleaned_text
    assert "凌晨两点。" in r.cleaned_text
    assert "正文继续。" in r.cleaned_text


def test_clean_idempotent() -> None:
    raw = """# 标题 A

> 引用句

段落一。


段落二。
"""
    once = clean_novel(raw).cleaned_text
    twice = clean_novel(once).cleaned_text
    assert once == twice


def test_clean_no_heading() -> None:
    raw = "只有正文，没有标题。"
    r = clean_novel(raw)
    assert r.chapters == []
    assert r.cleaned_text == "只有正文，没有标题。"
