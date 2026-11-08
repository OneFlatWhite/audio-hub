"""缺集差集计算:播出集数 vs 已有集数。

纯函数、零 IO;输入输出都是普通 dict,方便单测与复用。
约定:aired 的值为该季已播出集号列表,have 的值为该季已有集号集合。
"""

from __future__ import annotations


def compute_missing(
    aired: dict[int, list[int]], have: dict[int, set[int]]
) -> dict[int, list[int]]:
    """计算缺失集号。

    规则:
    - 第 0 季(特别篇)跳过,不参与计算;
    - 只比对 aired 中出现的集号——未播出的集不算缺,本地多出的集也不算错;
    - 无缺失的季不出现在结果里。

    Args:
        aired: 季号 → 该季已播出集号列表(来自 TMDB 播出进度)。
        have: 季号 → 该季已有集号集合(来自 Emby)。

    Returns:
        季号 → 缺失集号列表(升序);整部无缺时返回空 dict。
    """
    missing: dict[int, list[int]] = {}
    for season, episodes in aired.items():
        if season == 0:
            continue
        owned = have.get(season, set())
        lack = sorted(ep for ep in episodes if ep not in owned)
        if lack:
            missing[season] = lack
    return missing


def is_complete(missing: dict[int, list[int]]) -> bool:
    """判断缺集字典是否为空(即已集齐)。"""
    return not missing
