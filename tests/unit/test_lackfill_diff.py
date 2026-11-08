"""lackfill.diff 单元测试:缺集差集计算。

覆盖:常规缺集、第 0 季跳过、整季缺失、无缺失返回空 dict、
have 为空 dict 时全缺,以及 is_complete 的正反两例。
"""

from __future__ import annotations

from audio_hub.lackfill.diff import compute_missing, is_complete


def test_compute_missing_regular() -> None:
    """常规场景:各季部分缺集,缺的按升序返回,已有集不误报。"""
    aired = {1: [1, 2, 3, 4], 2: [1, 2, 3]}
    have = {1: {1, 3}, 2: {1, 2, 3}}
    assert compute_missing(aired, have) == {1: [2, 4]}


def test_compute_missing_skips_season_zero() -> None:
    """第 0 季(特别篇)不参与计算,即使全缺也不出现在结果里。"""
    aired = {0: [1, 2], 1: [1]}
    have = {}
    assert compute_missing(aired, have) == {1: [1]}


def test_compute_missing_whole_season_missing() -> None:
    """整季缺失:该季在 have 中完全不存在,仍按 aired 升序全量返回。"""
    aired = {1: [1, 2], 2: [5, 3]}
    have = {1: {1, 2}}
    assert compute_missing(aired, have) == {2: [3, 5]}


def test_compute_missing_no_missing_returns_empty() -> None:
    """全部集齐(含 aired 为空)时返回空 dict。"""
    assert compute_missing({1: [1, 2]}, {1: {1, 2}}) == {}
    assert compute_missing({}, {1: {1}}) == {}


def test_compute_missing_empty_have_all_missing() -> None:
    """have 为空 dict:aired 里每个非 0 季全缺。"""
    aired = {1: [2, 1], 2: [1]}
    assert compute_missing(aired, {}) == {1: [1, 2], 2: [1]}


def test_is_complete_positive() -> None:
    """空缺集字典视为已集齐。"""
    assert is_complete({}) is True


def test_is_complete_negative() -> None:
    """任何季有任何缺集都视为未集齐。"""
    assert is_complete({1: [3]}) is False
