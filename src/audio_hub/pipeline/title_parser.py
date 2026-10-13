"""PT 种子标题解析：歌手 / 专辑 / 年份 / 音质 / 码率 / 版本 / 卷册 → ParsedTitle。

订阅引擎（subscription.engine）要做「专辑级去重」与「同专辑多候选择优」，
前提是能从五花八门的站点命名里拆出 歌手|专辑 这对键。本模块只负责**解析**，
不做订阅匹配（那是 matcher 的事），也不查库。

解析原则：**宁可留空，不可拆错**。拆不出歌手 / 专辑时把整段干净文本放进
title，artist 留空；album_key 对空值也能生成稳定的键（退化为全文键），
去重只是粒度变粗，不会把两张不同专辑并成一张。

支持的命名模式（见 tests/unit/test_title_parser.py 的样本）：
  1. ``Artist - Album (Year) [FLAC 24-96]``   最常见的欧美 / 日系写法
  2. ``Artist – Album`` / ``Artist — Album``     en dash / em dash / 全角横线
  3. ``【歌手】专辑``                          首个非标记【】视为歌手
  4. ``歌手《专辑》`` / ``歌手 -《专辑》``       书名号内为专辑
  5. ``歌手 - 专辑 - 2020 - FLAC``               多段横线：第 3 段起只吸收年份 / 音质 / 版本
  6. ``【CD】【FLAC分轨】歌手 - 专辑 (2003)``     站点前置标记（【】或 []）全部剥掉
  7. ``[2003] Artist - Album [Deluxe Edition]``  方括号年份 / 版本
  8. 有声书 ``书名 第N卷 / 卷N / Vol.N / 第N部 / Part N``（含中文数字）→ volume，
     ``XX演播 / 主播 / 朗读`` 视为旁白信息剥掉

音质档位（quality_tag）与 matcher 共用同一张权重表 QUALITY_WEIGHTS —— 表只维护一份，
matcher 从这里导入，避免两处漂移。
"""

from __future__ import annotations

import re

from ..adapters.base import normalize_title
from ..models import ParsedTitle

# ── 音质权重（PRD §6.2 第 4 条）。matcher._QUALITY_WEIGHTS 是它的别名 ─────────
# 注意：ape / wav 这类短词不能用 \b —— 「WAV整轨」里 CJK 字符算 \w，\b 不成立。
_HIRES_RATES = r"(?:44\.1|48|88\.2|96|176\.4|192|352\.8|384)"
QUALITY_WEIGHTS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"(dsd|dff|dsf)", re.IGNORECASE), 1.00, "dsd"),
    (re.compile(r"(24\s*-?bit|hi-?res|24\s*[/\-]\s*" + _HIRES_RATES + r"|96khz|192khz|88\.2khz|176\.4khz)",
                re.IGNORECASE), 0.95, "flac_24"),
    (re.compile(r"flac", re.IGNORECASE), 0.85, "flac_16"),
    (re.compile(r"(?<![a-z0-9])ape(?![a-z0-9])", re.IGNORECASE), 0.75, "ape"),
    (re.compile(r"(?<![a-z0-9])wav(?![a-z0-9])", re.IGNORECASE), 0.75, "wav"),
    (re.compile(r"(alac|m4a)", re.IGNORECASE), 0.70, "alac"),
    (re.compile(r"(320k|v0|mp3)", re.IGNORECASE), 0.40, "mp3"),
]


def detect_quality_tag(text: str) -> str | None:
    """文本 → 音质标签（dsd / flac_24 / flac_16 / ape / wav / alac / mp3）；识别不出返回 None。"""
    for pattern, _weight, label in QUALITY_WEIGHTS:
        if pattern.search(text):
            return label
    return None


# ── 码率 / 采样率 ───────────────────────────────────────────
_BITRATE_RES: list[tuple[re.Pattern, str]] = [
    # 24bit/96kHz、24 bit 96 kHz、24bit-96kHz
    (re.compile(r"(\d{2})\s*bit\s*[/\-]?\s*(\d{2,3}(?:\.\d)?)\s*khz", re.IGNORECASE), "{0}/{1}"),
    # 24-96 / 24/192 / 16-44.1（只接受 16/24/32 开头，避免把年份段误读）
    (re.compile(r"(?<![\d.])(16|24|32)\s*[/\-]\s*(44\.1|44|48|88\.2|88|96|176\.4|176|192|352\.8|384)"
                r"(?![\d.])"), "{0}/{1}"),
    (re.compile(r"\bdsd\s*(64|128|256|512)\b", re.IGNORECASE), "DSD{0}"),
    (re.compile(r"\b(320|256|192|128)\s*k(?:bps)?\b", re.IGNORECASE), "{0}k"),
    (re.compile(r"\bV0\b"), "V0"),
]


def detect_bitrate(text: str) -> str | None:
    for pattern, fmt in _BITRATE_RES:
        m = pattern.search(text)
        if m:
            return fmt.format(*[g for g in m.groups() if g is not None])
    return None


# ── 版本标记 ────────────────────────────────────────────────
_EDITION_WORDS = (
    r"deluxe(?:\s+edition)?|remaster(?:ed)?(?:\s+edition)?|live(?:\s+edition)?|"
    r"anniversary(?:\s+edition)?|special\s+edition|limited\s+edition|collector'?s?\s+edition|"
    r"expanded\s+edition|bonus\s+tracks?(?:\s+edition)?|reissue|edition|"
    r"豪华版|纪念版|限量版|重制版|典藏版|完全版|珍藏版|现场版|精选集?|新歌\+精选|"
    r"日版|欧版|港版|台版|首版|初回限定盤?|通常盤|完全生産限定盤"
)
_EDITION_RE = re.compile(r"(?<![A-Za-z])(" + _EDITION_WORDS + r")(?![A-Za-z])", re.IGNORECASE)
#: 自由文本里只认**结尾**的版本词：`Live at Wembley` 这种专辑名中段的 Live 不能被剥掉
_EDITION_TAIL_RE = re.compile(r"(?<![A-Za-z])(" + _EDITION_WORDS + r")\s*$", re.IGNORECASE)

# ── 年份：仅接受括号内 / 独立横线段 / 独立空格包裹的 19xx·20xx ───
_YEAR_TOKEN = r"((?:19|20)\d{2})"
_YEAR_BRACKET_RE = re.compile(r"[(\[（【]\s*" + _YEAR_TOKEN + r"(?:[.\-/]\d{1,2}){0,2}\s*[)\]）】]")
_YEAR_WORD_RE = re.compile(r"(?:^|(?<=\s))" + _YEAR_TOKEN + r"(?=\s|$)")

# ── 括号标记：站点前置标记（CD / FLAC分轨 / WEB ...）与内容性括号的区分 ──
_BRACKET_RE = re.compile(r"[【\[]([^】\]]{1,40})[】\]]")
_PAREN_RE = re.compile(r"[（(]([^）)]{1,60})[）)]")
_TAG_LIKE_RE = re.compile(
    r"^(?:cd|cds?\s*\d*|\d+\s*cds?|flac|ape|wav|wv|dsd|dsf|dff|mp3|m4a|m4b|aac|alac|ogg|"
    r"分轨|整轨|flac分轨|flac整轨|ape整轨|wav整轨|web|web-?dl|hi-?res|hr|hq|hd|mqa|"
    r"sacd|xrcd|hdcd|shm-?cd|blu-?spec(?:\s*cd2?)?|uhqcd|vinyl|lp|cue|log|lossless|无损|"
    r"有声书?|audiobook|音乐|music|专辑|单曲|ep|single|album|ost|原声|数字|digital|合集|套装|"
    r"box\s*set|精选|"
    r"\d{1,3}\s*bit(?:\s*[/\-]?\s*\d{2,3}(?:\.\d)?\s*khz)?|\d{2,3}(?:\.\d)?\s*khz|"
    r"\d{2}\s*[/\-]\s*\d{2,3}(?:\.\d)?|\d{2,3}\s*k(?:bps)?|v0|"
    r"(?:19|20)\d{2}"
    r")$",
    re.IGNORECASE,
)

# ── 分隔符：空格包裹的 - / – / — / －，或 CJK 字符之间无空格的横线 ────
_CJK = r"\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af"
#: 无空格横线只在**至少一侧是 CJK** 时当分隔符：`陈奕迅-U87` 要拆，`Hi-Res` / `WEB-DL` 不拆
_SEP_RE = re.compile(
    r"\s+[-–—－]+\s+|\s*[–—]\s*|"
    r"(?<=[" + _CJK + r"》」』])[-－](?=\S)|(?<=\S)[-－](?=[" + _CJK + r"《「『])"
)
_BOOK_TITLE_RE = re.compile(r"《([^》]{1,80})》")

# ── 卷册号：在 series.tracker 默认规则之上补「卷N / Vol.N / 中文数字」 ──
_CN_NUM = "零〇一二两三四五六七八九十百"
_VOLUME_RES: list[re.Pattern] = [
    re.compile(r"第\s*([0-9" + _CN_NUM + r"]+)\s*[卷部集册季章回期辑]"),
    re.compile(r"(?:^|[\s\]】）)])卷\s*([0-9" + _CN_NUM + r"]{1,4})(?![0-9" + _CN_NUM + r"])"),
    re.compile(r"\bvol(?:ume)?\.?\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bpart\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\b(?:book|bk)\.?\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bEP?\s*(\d{1,3})\b"),
    re.compile(r"\((\d{1,3})\)"),
]
_VOLUME_STRIP_RE = re.compile(
    r"(第\s*[0-9" + _CN_NUM + r"]+\s*[卷部集册季章回期辑]|(?<![" + _CJK + r"])卷\s*[0-9" + _CN_NUM + r"]{1,4}|"
    r"\bvol(?:ume)?\.?\s*\d{1,4}\b|\bpart\s*\d{1,4}\b|\b(?:book|bk)\.?\s*\d{1,4}\b)",
    re.IGNORECASE,
)
#: 有声书旁白信息：周建龙演播 / 主播：XX / 朗读
_NARRATOR_RE = re.compile(r"[\s:：]*[^\s\-–—－【】\[\]（）()《》]{1,12}(?:演播|播讲|主播|朗读|讲述)[\s:：]*")

# 噪声：体积、上传者、格式词。ASCII 词用显式前后断言 —— `\b` 在 CJK 相邻处不成立
_ASCII_NOISE = (
    r"web-?dl|web|cds?\d*|sacd|vinyl|lp|hi-?res|lossless|\d{1,2}bit|\d{2,3}(?:\.\d)?khz|"
    r"flac|ape|wav|dsd\d*|dsf|dff|mp3|m4a|m4b|alac|aac|ogg|320k(?:bps)?|v0|mqa|hq"
)
_NOISE_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB|KB|KiB|TB|TiB)(?![A-Za-z])|(?:@|by[:：])\s*[\w\-_.]+|"
    r"(?<![A-Za-z0-9])(?:" + _ASCII_NOISE + r")(?![A-Za-z0-9])|"
    r"分轨|整轨|无损|有声书|有声小说|audiobook)",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[\s\-_–—－·•・,，.。!！?？:：;；'\"‘’“”`~／/\\|+&*#@^%$()（）\[\]【】《》「」『』<>]+")


def cn_to_int(text: str) -> int | None:
    """中文数字（含「两」「〇」）→ 整数，最多到百位；阿拉伯数字直接转。失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total, current = 0, 0
    for ch in text:
        if ch in digits:
            current = digits[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def extract_volume(title: str) -> int | None:
    """从标题解析卷册 / 分集号：中文「第N卷/部/集」、「卷N」、Vol.N、Part N、EP N、(N)。"""
    if not title:
        return None
    for pattern in _VOLUME_RES:
        m = pattern.search(title)
        if not m:
            continue
        for g in m.groups():
            if g:
                n = cn_to_int(g)
                if n is not None and 0 < n < 10000:
                    return n
    return None


def _is_tag_like(content: str) -> bool:
    c = content.strip()
    if not c:
        return True
    if detect_quality_tag(c) or detect_bitrate(c):
        return True
    return bool(_TAG_LIKE_RE.match(c))


def _clean(text: str) -> str:
    """压缩空格，去掉首尾残留的分隔符与标点。"""
    text = re.sub(r"\s{2,}", " ", text or "")
    return text.strip(" \t-–—－_:：,，.·/|")


def _split_parts(text: str) -> list[str]:
    return [p for p in (_clean(x) for x in _SEP_RE.split(text)) if p]


def _strip_brackets(text: str, out: ParsedTitle) -> tuple[str, str | None]:
    """剥掉【】/ [] 标记。

    规则：标记类（CD / FLAC分轨 / WEB / 年份 ...）直接丢；纯版本词（[Deluxe Edition]）
    记入 edition 后丢；**位于开头**（前面只有已剥掉的标记）的第一个内容性括号视为歌手；
    其余内容性方括号当普通文本保留。
    """
    pieces: list[str] = []
    artist: str | None = None
    pos = 0
    only_tags_so_far = True
    for m in _BRACKET_RE.finditer(text):
        before = text[pos:m.start()]
        if before.strip():
            only_tags_so_far = False
        pieces.append(before)
        pos = m.end()
        content = m.group(1).strip()
        if _is_tag_like(content):
            pieces.append(" ")
            continue
        em = _EDITION_RE.search(content)
        if em and not _clean(_EDITION_RE.sub("", content)):
            out.edition = out.edition or em.group(1)
            pieces.append(" ")
            continue
        if artist is None and only_tags_so_far:
            artist = content
            pieces.append(" ")
            continue
        pieces.append(f" {content} ")
        only_tags_so_far = False
    pieces.append(text[pos:])
    return "".join(pieces), artist


def _strip_parens(text: str, out: ParsedTitle) -> str:
    """圆括号：版本词 → edition；标记类丢弃；其余并入正文。"""
    def _sub(mm: re.Match) -> str:
        content = mm.group(1).strip()
        em = _EDITION_RE.search(content)
        if em:
            out.edition = out.edition or em.group(1)
            return " "
        if _is_tag_like(content):
            return " "
        return f" {content} "
    return _PAREN_RE.sub(_sub, text)


def _absorb_tail(parts: list[str], out: ParsedTitle) -> list[str]:
    """多段横线写法：第 3 段起若是年份 / 音质 / 版本 / 标记，就吸收掉而不当作专辑名。"""
    kept: list[str] = []
    for i, p in enumerate(parts):
        if i < 2:
            kept.append(p)
            continue
        if re.fullmatch(_YEAR_TOKEN, p):
            out.year = out.year or int(p)
            continue
        if _is_tag_like(p):
            continue
        m = _EDITION_RE.fullmatch(p)
        if m:
            out.edition = out.edition or m.group(1)
            continue
        kept.append(p)
    return kept


def _take_tail_year(parts: list[str], out: ParsedTitle) -> list[str]:
    """没有括号年份时，从各段里摘独立的 19xx/20xx；整段就是年份且处在专辑位时保留（如《1989》）。"""
    if out.year is not None:
        return parts
    result: list[str] = []
    for i, p in enumerate(parts):
        m = _YEAR_WORD_RE.search(p)
        if m and out.year is None:
            rest = _clean(p[:m.start()] + " " + p[m.end():])
            if rest or (i >= 2):
                out.year = int(m.group(1))
                if rest:
                    result.append(rest)
                continue
        result.append(p)
    return result


def parse(title: str, kind: str = "music") -> ParsedTitle:
    """解析种子标题。

    Args:
        title: 站点原始标题
        kind: "music" / "audiobook"（也接受 WatchKind / MediaCategory 枚举）。
            有声书把书名放进 title（album 同值），并解析卷册号与剥掉旁白信息；
            音乐把专辑放进 album 与 title。
    """
    kind_val = getattr(kind, "value", kind) or "music"
    is_book = str(kind_val).lower() == "audiobook"
    out = ParsedTitle(raw=title or "")
    if not title or not title.strip():
        return out

    text = title.strip()
    out.quality_tag = detect_quality_tag(text)
    out.bitrate = detect_bitrate(text)

    # ── 1. 年份：括号内优先 ──
    m = _YEAR_BRACKET_RE.search(text)
    if m:
        out.year = int(m.group(1))
        text = text[:m.start()] + " " + text[m.end():]

    # ── 2. 卷册（有声书必解析；音乐也解析，合辑分卷同样有用）──
    out.volume = extract_volume(text)

    # ── 3. 括号：标记剥掉，开头的内容性【】作歌手候选 ──
    text, bracket_artist = _strip_brackets(text, out)
    text = _strip_parens(text, out)

    # ── 4. 去噪：旁白 / 体积 / 上传者 / 格式词 / 卷册词 / 结尾版本词 ──
    if is_book:
        text = _NARRATOR_RE.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    text = _VOLUME_STRIP_RE.sub(" ", text)
    em = _EDITION_TAIL_RE.search(text)
    if em:
        out.edition = out.edition or em.group(1)
        text = text[:em.start()]
    text = _clean(text)

    # ── 5. 书名号：《专辑》 ──
    bm = _BOOK_TITLE_RE.search(text)
    if bm:
        album = _clean(bm.group(1))
        outside_parts = _take_tail_year(_split_parts(text[:bm.start()] + " " + text[bm.end():]), out)
        if bracket_artist:
            out.artist = bracket_artist
        elif outside_parts:
            out.artist = outside_parts[0]
        out.album = out.title = album
        return _finish(out, is_book)

    # ── 6. 横线分段 ──
    parts = _take_tail_year(_absorb_tail(_split_parts(text), out), out)
    if bracket_artist:
        out.artist = bracket_artist
        out.album = _clean(" ".join(parts))
    elif len(parts) >= 2:
        out.artist, out.album = parts[0], _clean(" ".join(parts[1:]))
    elif parts:
        out.album = parts[0]
    out.title = out.album
    return _finish(out, is_book)


def _finish(out: ParsedTitle, is_book: bool) -> ParsedTitle:
    out.artist = _clean(_NOISE_RE.sub(" ", out.artist))
    out.album = _clean(out.album)
    out.title = _clean(out.title) or out.album
    if not out.album and out.artist:
        # 只有一段文本时，那段是专辑 / 书名而非歌手
        out.album = out.title = out.artist
        out.artist = ""
    return out


# ── album_key ───────────────────────────────────────────────

def normalize_key(text: str) -> str:
    """专辑键的归一化：小写、全半角、繁→简、去括号内容、去版本词、去空格与标点。"""
    if not text:
        return ""
    t = _PAREN_RE.sub(" ", text)
    t = _BRACKET_RE.sub(" ", t)
    t = normalize_title(t)                 # 繁简 / 全半角 / 小写 / 体积噪声
    t = _EDITION_RE.sub(" ", t)
    t = _NOISE_RE.sub(" ", t)
    t = _PUNCT_RE.sub("", t)
    return t


def album_key(artist: str, album: str) -> str:
    """歌手|专辑 → 归一化去重键。两者都为空时返回空串（调用方按「无法识别」处理，不去重）。"""
    a, b = normalize_key(artist), normalize_key(album)
    if not a and not b:
        return ""
    return f"{a}|{b}"


def album_key_for(parsed: ParsedTitle, kind: str = "music") -> str:
    """按类型生成去重键：音乐 歌手|专辑；有声书 书名|volN（无卷号则 书名|）。

    有声书的卷册号是区分同一书不同卷的唯一依据，故放进键里；音乐的 volume
    通常是合辑分卷（CD1/CD2 已被当作标记剥掉），不进键。
    拆不出结构时退化为全文键，粒度变粗但不会误并。
    """
    kind_val = str(getattr(kind, "value", kind) or "music").lower()
    if kind_val == "audiobook":
        name = normalize_key(parsed.title or parsed.album or parsed.raw)
        if not name:
            return ""
        return f"{name}|vol{parsed.volume}" if parsed.volume else f"{name}|"
    key = album_key(parsed.artist, parsed.album or parsed.title)
    if key:
        return key
    whole = normalize_key(parsed.raw)
    return f"|{whole}" if whole else ""
