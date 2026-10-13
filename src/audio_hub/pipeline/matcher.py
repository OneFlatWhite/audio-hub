"""匹配与准入层：类型判别 → 关键词命中 → 置信度分级 → 去重准入。

v0.1 用最简规则（PRD §5）：关键词包含 + 体积区间。
v0.2 再上完整归一化打分与待审队列。
v0.6 增「订阅型」匹配（match_subscription / match_for_subscriptions）：按 sub_type
（artist / album / audiobook / keyword）命中，再过 include / exclude / free_only / 体积 /
preferred_quality / min_seeders 这一套按订阅配的过滤器。旧的 match_candidates 原样保留，
search_round 仍用它；订阅引擎（subscription.engine）用新入口。

核心原则（PRD §6.2）：**误抓的代价高于漏抓**。
误抓会计入下载量与 H&R，且污染曲库；漏抓只是下次再试。
故低置信度一律不自动下载。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger

from ..adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from ..models import ParsedTitle, SeriesEntry, SubType, WatchEntry
from ..series.tracker import extract_part_no
from .title_parser import QUALITY_WEIGHTS, album_key_for, normalize_key, parse

Confidence = Literal["high", "medium", "low"]

# ── 置信度阈值（PRD §6.2 第 3 条：起步全部设 high）─────────
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# ── 音质权重（PRD §6.2 第 4 条）。表本体在 title_parser，这里只是别名 ─────
_QUALITY_WEIGHTS: list[tuple[re.Pattern, float, str]] = QUALITY_WEIGHTS

# ── 优惠权重（PRD §6.2 第 5 条）─────────────────────────────
_DISCOUNT_WEIGHTS = {
    DiscountType.FREE_2UP: 0.15,
    DiscountType.FREE: 0.12,
    DiscountType.HALF_DOWN: 0.06,
    DiscountType.PERCENT_50: 0.06,
    DiscountType.PERCENT_30: 0.03,
    DiscountType.NORMAL: 0.0,
    DiscountType.UNKNOWN: 0.0,
}

# ── 排除模式（视频/演唱会，宁漏勿错）────────────────────────
_HARD_EXCLUDE = re.compile(
    r"(bluray|blu-ray|bdrip|remux|1080p|2160p|720p|480p|4k|uhd|"
    r"web-?dl|hdtv|x264|x265|hevc|avc|hdr10|dovi|演唱会|concert|"
    r"纪录片|documentary|\bmv\b|music\s*video)",
    re.IGNORECASE,
)

# ── 有声书特征 ──────────────────────────────────────────────
_AUDIOBOOK_HINT = re.compile(
    r"(有声书|audiobook|主播|演播|评书|广播剧|听书|章节|\bm4b\b|全集\s*mp3)", re.IGNORECASE
)


@dataclass
class MatchResult:
    """单个候选的匹配结果。"""
    torrent: NormalizedTorrent
    action: Literal["accept", "review", "reject"]
    confidence: Confidence
    score: float
    quality: str
    #: 命中的订阅项；未命中任何订阅项（硬排除阶段）时为 None
    watch_entry: WatchEntry | None = None
    reject_reason: str = ""
    matched_terms: list[str] = field(default_factory=list)
    #: v0.4 连载：订阅项绑定的 series.id 与从标题解析出的分集号（解析失败为 None）
    series_id: int | None = None
    part_no: int | None = None
    #: 附加诊断信息（如 part_unparsed），由持久化方按需写入 Task / 事件
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MatchRoundResult:
    """一轮匹配的聚合结果。"""
    accepted: list[MatchResult] = field(default_factory=list)
    review: list[MatchResult] = field(default_factory=list)
    rejected: list[MatchResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.review) + len(self.rejected)

    def stats(self) -> dict[str, int]:
        return {
            "accepted": len(self.accepted),
            "review": len(self.review),
            "rejected": len(self.rejected),
            "total": self.total,
        }


# ── 有声书格式权重（与音乐音质阶梯是两套标准）──────────────
# PRD §6.2：有声书 M4B > 多文件 MP3。MP3 对有声书是正常格式，
# 不应按音乐的音质阶梯扣分，否则有声书永远达不到 high 置信度。
_AUDIOBOOK_FORMATS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bm4b\b", re.IGNORECASE), 0.95, "m4b"),
    (re.compile(r"(有声书|audiobook|主播|演播|评书|广播剧)", re.IGNORECASE), 0.85, "audiobook"),
    (re.compile(r"\bmp3\b", re.IGNORECASE), 0.80, "mp3"),
]


def detect_quality(text: str) -> tuple[str, float]:
    """识别音质规格，返回 (quality_label, weight)。"""
    for pattern, weight, label in _QUALITY_WEIGHTS:
        if pattern.search(text):
            return label, weight
    return "unknown", 0.0


def detect_audiobook_format(text: str) -> tuple[str, float]:
    """识别有声书格式，返回 (label, weight)。

    与 detect_quality 分开：有声书的 MP3 是合格格式（weight 0.80），
    而音乐的 MP3 是低质格式（weight 0.40）。
    """
    for pattern, weight, label in _AUDIOBOOK_FORMATS:
        if pattern.search(text):
            return label, weight
    return "unknown", 0.0


def _match_watch_terms(entry: WatchEntry, torrent: NormalizedTorrent) -> list[str]:
    """返回订阅项在该候选标题中命中的词（query + aliases）。"""
    haystack = torrent.title_normalized
    if not haystack:
        haystack = torrent.title.lower()

    terms = [entry.query] + list(entry.aliases)
    hits = []
    for term in terms:
        t = term.strip().lower()
        if not t:
            continue
        # 归一化后的比对（繁简差异已在 normalize_title 处理）
        if t in haystack:
            hits.append(term)
    return hits


def score_candidate(
    entry: WatchEntry,
    torrent: NormalizedTorrent,
    hits: list[str],
) -> tuple[float, Confidence, str]:
    """打分并给出置信度。

    评分构成：
      基础分 0.5（命中订阅词即给）
      + 音质权重 0～1.0 × 0.3
      + 优惠权重 × 1.0
      + 多别名命中加成
      + 有做种者加成

    Returns:
        (score, confidence, quality)
    """
    score = 0.5
    quality_text = f"{torrent.title} {' '.join(torrent.format_tags)}"
    # 有声书走格式权重（MP3 合格），其余走音乐音质阶梯（MP3 低质）
    if torrent.category == MediaCategory.AUDIOBOOK:
        quality, q_weight = detect_audiobook_format(quality_text)
    else:
        quality, q_weight = detect_quality(quality_text)
    score += q_weight * 0.3

    score += _DISCOUNT_WEIGHTS.get(torrent.discount, 0.0)

    # 多别名命中：更可能是正确目标
    if len(hits) > 1:
        score += 0.05 * (len(hits) - 1)

    # 有活跃做种者，说明资源可用
    if torrent.seeders and torrent.seeders > 0:
        score += 0.05

    # ── 置信度分级 ──
    # high：命中订阅词（query 或别名）+ 明确音质标记 + 类型明确（音乐或有声书）
    #
    # 注意：类型判断必须涵盖 AUDIOBOOK。早期实现写死 == MUSIC，
    # 导致有声书永远达不到 high、整条有声书链路卡在待审队列。
    has_term_hit = bool(hits)
    category_explicit = torrent.category in (MediaCategory.MUSIC, MediaCategory.AUDIOBOOK)

    if has_term_hit and q_weight >= 0.7 and category_explicit:
        confidence: Confidence = "high"
    elif hits and q_weight > 0:
        confidence = "medium"
    else:
        confidence = "low"

    return round(min(score, 1.0), 4), confidence, quality


def match_candidates(
    candidates: list[NormalizedTorrent],
    entries: list[WatchEntry],
    known_hashes: set[str] | None = None,
    min_size_bytes: int = 200 * 1024**2,
    max_size_bytes: int | None = None,
    *,
    series_index: dict[int, SeriesEntry] | None = None,
    known_parts: dict[int, set[int]] | None = None,
) -> MatchRoundResult:
    """对候选列表执行完整匹配流程。

    流程：类型判别 → 硬排除 → 体积区间 → 订阅词命中 → 打分 → 连载分卷去重 → 置信度分流

    Args:
        candidates: 搜索层输出的候选
        entries: 启用的订阅项
        known_hashes: 已处理过的 info_hash（来自 DB torrents 表）
        min_size_bytes: 最小体积，默认 200MB（排除单曲/碎片）
        max_size_bytes: 最大体积，None = 不限（用户为 VIP，不担心空间）
        series_index: series.id → SeriesEntry，提供自定义分集规则；
            缺省时连载订阅用默认规则解析（见 series.tracker.build_series_context）
        known_parts: series.id → 已登记 part_no 集合；命中即按重复拒绝。
            本函数是同步纯函数，两者都由调用方在轮次开始时读库传入。
    """
    known = known_hashes or set()
    series_index = series_index or {}
    known_parts = known_parts or {}
    result = MatchRoundResult()
    seen_this_round: set[str] = set()
    # 同一轮内同一连载同一分集只放行一个（多站点同时放出同一部时）
    seen_parts_this_round: dict[int, set[int]] = {}

    # 按音乐/有声书分别索引订阅项，避免跨类型误匹配
    music_entries = [e for e in entries if e.kind.value == "music"]
    book_entries = [e for e in entries if e.kind.value == "audiobook"]

    for torrent in candidates:
        # ── 去重：全局 info_hash ──
        key = torrent.info_hash or f"{torrent.site}:{torrent.title_normalized}"
        if key in known:
            continue                      # 已处理过，静默跳过（不记 rejected，避免噪声）
        if key in seen_this_round:
            continue
        seen_this_round.add(key)

        # ── 硬排除：视频/演唱会 ──
        if torrent.category == MediaCategory.VIDEO or _HARD_EXCLUDE.search(torrent.title):
            result.rejected.append(MatchResult(
                torrent=torrent, watch_entry=entries[0] if entries else None,
                action="reject", confidence="high", score=0.0, quality="n/a",
                reject_reason="video_or_concert",
            ))
            continue

        # ── 体积区间 ──
        if torrent.size_bytes and torrent.size_bytes < min_size_bytes:
            result.rejected.append(MatchResult(
                torrent=torrent, watch_entry=entries[0] if entries else None,
                action="reject", confidence="high", score=0.0, quality="n/a",
                reject_reason=f"too_small({torrent.size_bytes})",
            ))
            continue
        if max_size_bytes and torrent.size_bytes > max_size_bytes:
            result.rejected.append(MatchResult(
                torrent=torrent, watch_entry=entries[0] if entries else None,
                action="reject", confidence="high", score=0.0, quality="n/a",
                reject_reason=f"too_large({torrent.size_bytes})",
            ))
            continue

        # ── 类型路由：选对应类型的订阅项集合 ──
        if torrent.category == MediaCategory.AUDIOBOOK:
            pool = book_entries
        elif torrent.category == MediaCategory.MUSIC:
            pool = music_entries
        else:
            # UNKNOWN 类型：对两类订阅都试，但最高只给 medium（进待审）
            pool = entries

        if not pool:
            result.rejected.append(MatchResult(
                torrent=torrent, watch_entry=entries[0] if entries else None,
                action="reject", confidence="high", score=0.0, quality="unknown",
                reject_reason=f"no_watchlist_for_{torrent.category.value}",
            ))
            continue

        # ── 订阅词命中：取命中最好的一个订阅项 ──
        best: tuple[list[str], WatchEntry] | None = None
        for entry in pool:
            # 用户自定义正则优先
            if entry.regex:
                try:
                    if re.search(entry.regex, torrent.title, re.IGNORECASE):
                        best = ([entry.query], entry)
                        break
                except re.error as e:
                    logger.warning(f"订阅项 {entry.id} 正则无效，已忽略: {e}")
                    continue

            hits = _match_watch_terms(entry, torrent)
            if hits and (best is None or len(hits) > len(best[0])):
                best = (hits, entry)

        if best is None:
            result.rejected.append(MatchResult(
                torrent=torrent, watch_entry=pool[0],
                action="reject", confidence="high", score=0.0, quality="n/a",
                reject_reason="no_watchlist_hit",
            ))
            continue

        hits, entry = best
        score, confidence, quality = score_candidate(entry, torrent, hits)

        # UNKNOWN 类型强制降级到 medium（宁漏勿错）
        if torrent.category == MediaCategory.UNKNOWN and confidence == "high":
            confidence = "medium"

        match = MatchResult(
            torrent=torrent, watch_entry=entry, action="review",
            confidence=confidence, score=score, quality=quality,
            matched_terms=hits,
        )

        # ── 连载分卷：解析分集号 + 分卷级去重 ──
        # 挂在置信度分流之前：重复分卷无论置信度多高都不该进待审 / 派发；
        # 解析失败不拦截（宁漏勿错只针对误抓，漏解析仍可人工处理），只记 detail。
        if entry.series_id is not None:
            series = series_index.get(entry.series_id)
            pattern = series.part_pattern if series else None
            part_no = extract_part_no(torrent.title, pattern)
            match.series_id = entry.series_id
            if part_no is None:
                match.detail["part_unparsed"] = True
            else:
                match.part_no = part_no
                dup_known = part_no in known_parts.get(entry.series_id, ())
                dup_round = part_no in seen_parts_this_round.get(entry.series_id, ())
                if dup_known or dup_round:
                    match.action = "reject"
                    match.reject_reason = (
                        f"duplicate_part(series={entry.series_id},part={part_no})"
                    )
                    result.rejected.append(match)
                    continue
                seen_parts_this_round.setdefault(entry.series_id, set()).add(part_no)

        # ── 分流：置信度 + 订阅项的 min_confidence 门槛 ──
        threshold = _CONFIDENCE_RANK.get(entry.min_confidence, 2)
        actual = _CONFIDENCE_RANK.get(confidence, 0)

        if actual >= threshold and entry.auto_download:
            match.action = "accept"
            result.accepted.append(match)
        else:
            match.action = "review"
            if actual < threshold:
                match.reject_reason = f"confidence_below_threshold({confidence}<{entry.min_confidence})"
            else:
                match.reject_reason = "auto_download_disabled"
            result.review.append(match)

    _log_summary(result, len(candidates))
    return result


def _log_summary(result: MatchRoundResult, input_count: int) -> None:
    s = result.stats()
    logger.info(
        f"匹配完成：输入 {input_count} → 采纳 {s['accepted']} / 待审 {s['review']} / "
        f"拒绝 {s['rejected']}"
    )
    # 拒绝原因分布，用于规则迭代（PRD §13.7）
    if result.rejected:
        reasons: dict[str, int] = {}
        for m in result.rejected:
            base = m.reject_reason.split("(")[0]
            reasons[base] = reasons.get(base, 0) + 1
        logger.debug(f"拒绝原因分布: {reasons}")


# ── v0.6 订阅型匹配（subscription.engine 的入口）───────────────

_MB = 1024 ** 2
#: 未配 min_size_mb 时沿用 match_candidates 的默认下限（排除单曲 / 碎片）
DEFAULT_MIN_SIZE_BYTES = 200 * _MB
#: lossless_only 拒绝的有损档；hires_only 只放行的高解档
_LOSSY_TAGS = frozenset({"mp3"})
_HIRES_TAGS = frozenset({"flac_24", "dsd"})
_AAC_RE = re.compile(r"(?<![a-z0-9])aac(?![a-z0-9])", re.IGNORECASE)


@dataclass
class SubMatch:
    """一个候选对一条订阅的匹配结果（未经去重 / 洗版判定，那是 engine.decide 的事）。

    matched=False 表示订阅词根本没命中（调用方应静默丢弃，不记命中）；
    matched=True 但 reject_reason 非空表示命中了但被订阅的过滤器拒绝（记 rejected，供用户调规则）。
    """
    torrent: NormalizedTorrent
    watch: WatchEntry
    parsed: ParsedTitle
    album_key: str
    matched: bool
    matched_terms: list[str] = field(default_factory=list)
    reject_reason: str = ""
    score: float = 0.0
    confidence: Confidence = "low"
    #: 音质标签（title_parser 的档位，与 models.quality_rank 对应）；识别不出为 None
    quality_tag: str | None = None
    seeders: int | None = None

    @property
    def accepted(self) -> bool:
        return self.matched and not self.reject_reason


def _norm(text: str) -> str:
    return normalize_key(text or "")


def _kw_norm(text: str) -> str:
    """include / exclude 关键词的轻归一化：NFKC（全半角）+ 小写 + 去空格。

    不用 normalize_key：它会把 FLAC / Live / 分轨 这类词当噪声剔掉，而这正是用户最常用的过滤词。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").lower())


def _terms(*groups: str | list[str] | None) -> list[str]:
    """展平、去空、去重，保持顺序。"""
    out: list[str] = []
    for g in groups:
        items = g if isinstance(g, list) else [g]
        for t in items:
            t = (t or "").strip()
            if t and t not in out:
                out.append(t)
    return out


def _hit_terms(terms: list[str], *haystacks: str) -> list[str]:
    """返回在任一 haystack（已归一化）中出现的词（原文）。"""
    hays = [h for h in haystacks if h]
    hits: list[str] = []
    for term in terms:
        tn = _norm(term)
        if tn and any(tn in h for h in hays):
            hits.append(term)
    return hits


def _regex_hit(watch: WatchEntry, title: str) -> bool:
    if not watch.regex:
        return False
    try:
        return re.search(watch.regex, title, re.IGNORECASE) is not None
    except re.error as e:
        logger.warning(f"订阅项 {watch.id} 正则无效，已忽略: {e}")
        return False


def match_sub_type(watch: WatchEntry, torrent: NormalizedTorrent, parsed: ParsedTitle) -> list[str]:
    """按 sub_type 判订阅词是否命中，返回命中的词（空列表 = 未命中）。

    artist   ：歌手名 / 别名 / 正则 命中解析出的 artist 或整标题
    album    ：歌手与专辑都要命中（订阅未填歌手时只要专辑）
    audiobook：书名（query / aliases）或作者命中
    keyword  ：原逻辑（query / aliases / 正则 在标题里）
    """
    title_n = _norm(torrent.title)
    artist_n = _norm(parsed.artist)
    sub_type = watch.sub_type

    if _regex_hit(watch, torrent.title):
        return [watch.regex or watch.query]

    if sub_type == SubType.ARTIST:
        return _hit_terms(_terms(watch.artist, watch.query, watch.aliases), artist_n, title_n)

    if sub_type == SubType.ALBUM:
        # album 字段是专辑名；旧数据没填时 query 就是专辑名。aliases 视为歌手别名。
        album_hits = _hit_terms(_terms(watch.album) or _terms(watch.query), title_n)
        if not album_hits:
            return []
        artist_terms = _terms(watch.artist, watch.aliases)
        if not artist_terms:
            return album_hits
        artist_hits = _hit_terms(artist_terms, artist_n, title_n)
        return album_hits + artist_hits if artist_hits else []

    if sub_type == SubType.AUDIOBOOK:
        hits = _hit_terms(_terms(watch.query, watch.aliases), title_n)
        if hits:
            return hits
        return _hit_terms(_terms(watch.author), title_n)

    # keyword（与旧 _match_watch_terms 同义，但比对用去标点的键，容忍空格 / 全半角差异）
    return _hit_terms(_terms(watch.query, watch.aliases), title_n)


def apply_filters(
    watch: WatchEntry,
    torrent: NormalizedTorrent,
    quality_tag: str | None,
    *,
    default_min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
) -> str:
    """按订阅的过滤器检查候选，返回 reject_reason（空串 = 通过）。

    顺序：硬排除（视频 / 演唱会）→ exclude_keywords → include_keywords → free_only
    → 体积区间 → preferred_quality → min_seeders。

    min_size_mb / max_size_mb 覆盖硬编码的 200MB 下限；min_seeders 只在做种数**已知**时
    生效 —— RSS 条目普遍不带做种数，不能因此把整条 RSS 链路全拒。
    """
    title = torrent.title
    if torrent.category == MediaCategory.VIDEO or _HARD_EXCLUDE.search(title):
        return "video_or_concert"
    # 站点已明确分类且与订阅类型不符：有声书订阅不该吃音乐区的种，反之亦然
    if torrent.category in (MediaCategory.MUSIC, MediaCategory.AUDIOBOOK) \
            and torrent.category.value != watch.kind.value:
        return f"category_mismatch({torrent.category.value}!={watch.kind.value})"

    title_k = _kw_norm(title)
    for kw in watch.exclude_keywords:
        if _kw_norm(kw) and _kw_norm(kw) in title_k:
            return f"excluded_keyword({kw})"
    for kw in watch.include_keywords:
        if _kw_norm(kw) and _kw_norm(kw) not in title_k:
            return f"missing_keyword({kw})"

    if watch.free_only and torrent.discount not in (DiscountType.FREE, DiscountType.FREE_2UP):
        return f"not_free({torrent.discount.value})"

    min_bytes = watch.min_size_mb * _MB if watch.min_size_mb else default_min_size_bytes
    max_bytes = watch.max_size_mb * _MB if watch.max_size_mb else None
    if torrent.size_bytes and torrent.size_bytes < min_bytes:
        return f"too_small({torrent.size_bytes})"
    if max_bytes and torrent.size_bytes > max_bytes:
        return f"too_large({torrent.size_bytes})"

    pref = watch.preferred_quality
    if pref == "lossless_only":
        if quality_tag in _LOSSY_TAGS or _AAC_RE.search(f"{title} {' '.join(torrent.format_tags)}"):
            return f"quality_below_preference({quality_tag or 'aac'}<lossless)"
    elif pref == "hires_only" and quality_tag not in _HIRES_TAGS:
        return f"quality_below_preference({quality_tag or 'unknown'}<hires)"

    seeders = getattr(torrent, "seeders", None)
    if watch.min_seeders > 0 and seeders is not None and seeders < watch.min_seeders:
        return f"seeders_below_min({seeders}<{watch.min_seeders})"
    return ""


def score_subscription(
    watch: WatchEntry,
    torrent: NormalizedTorrent,
    hits: list[str],
) -> tuple[float, Confidence]:
    """订阅型打分：构成与 score_candidate 一致，但音质表由**订阅类型**而非种子分类决定。

    站内搜索结果常常没有分类映射（category=UNKNOWN），而订阅本身已经限定了类型，
    所以这里不再要求「分类明确」才能 high；误抓由 apply_filters 的类型不符 /
    preferred_quality / include 兑底。
    """
    quality_text = f"{torrent.title} {' '.join(torrent.format_tags)}"
    if watch.kind.value == "audiobook":
        _label, q_weight = detect_audiobook_format(quality_text)
    else:
        _label, q_weight = detect_quality(quality_text)
    score = 0.5 + q_weight * 0.3 + _DISCOUNT_WEIGHTS.get(torrent.discount, 0.0)
    if len(hits) > 1:
        score += 0.05 * (len(hits) - 1)
    seeders = getattr(torrent, "seeders", None)
    if seeders and seeders > 0:
        score += 0.05
    if q_weight >= 0.7:
        confidence: Confidence = "high"
    elif q_weight > 0:
        confidence = "medium"
    else:
        confidence = "low"
    return round(min(score, 1.0), 4), confidence


def match_subscription(
    watch: WatchEntry,
    torrent: NormalizedTorrent,
    parsed: ParsedTitle | None = None,
    *,
    default_min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
) -> SubMatch:
    """单候选 × 单订阅：解析 → sub_type 命中 → 过滤 → 打分。"""
    kind = watch.kind.value
    parsed = parsed or parse(torrent.title, kind)
    key = album_key_for(parsed, kind)
    quality_tag = parsed.quality_tag
    seeders = getattr(torrent, "seeders", None)
    result = SubMatch(torrent=torrent, watch=watch, parsed=parsed, album_key=key,
                      matched=False, quality_tag=quality_tag, seeders=seeders)

    hits = match_sub_type(watch, torrent, parsed)
    if not hits:
        return result
    result.matched = True
    result.matched_terms = hits

    reason = apply_filters(watch, torrent, quality_tag, default_min_size_bytes=default_min_size_bytes)
    result.score, result.confidence = score_subscription(watch, torrent, hits)
    result.reject_reason = reason
    return result


def match_for_subscriptions(
    candidates: list[NormalizedTorrent],
    watches: list[WatchEntry],
    *,
    default_min_size_bytes: int = DEFAULT_MIN_SIZE_BYTES,
) -> list[SubMatch]:
    """批量：每个候选对每条订阅试匹配，只返回订阅词命中的（含被过滤器拒绝的）。

    一个候选可以同时命中多条订阅（歌手型 + 该歌手某专辑的专辑型），每条订阅独立决策。
    订阅限定了站点（watch.sites 非空）时跳过其他站的候选。
    解析按 (title, kind) 缓存，一轮 RSS 几百条条目×几十条订阅不至于重复解析。
    """
    out: list[SubMatch] = []
    parsed_cache: dict[tuple[str, str], ParsedTitle] = {}
    for torrent in candidates:
        for watch in watches:
            if watch.sites and torrent.site not in watch.sites:
                continue
            cache_key = (torrent.title, watch.kind.value)
            parsed = parsed_cache.get(cache_key)
            if parsed is None:
                parsed = parsed_cache[cache_key] = parse(torrent.title, watch.kind.value)
            m = match_subscription(watch, torrent, parsed, default_min_size_bytes=default_min_size_bytes)
            if m.matched:
                out.append(m)
    return out
