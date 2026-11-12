"""Re0 回填校验与分流：115 分享走转存，ed2k 走 SA 离线下载。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from .p115 import P115Error, ShareSnapshot, parse_share_url

_ALLOWED_HOSTS = {
    "115.com",
    "www.115.com",
    "115cdn.com",
    "www.115cdn.com",
    "115cdn.net",
    "www.115cdn.net",
    "anxia.com",
    "www.anxia.com",
}
_PATH_RE = re.compile(r"/s/([A-Za-z0-9]+)/?")
_PASSWORD_RE = re.compile(r"[A-Za-z0-9]{1,32}")


def canonicalize_share_url(url: str) -> str:
    """校验分享 URL 并转成 115.com  canonical 形式，错误信息不回显原文。"""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        path_match = _PATH_RE.fullmatch(parsed.path)
        if (
            parsed.scheme not in {"http", "https"}
            or host not in _ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or path_match is None
        ):
            raise ValueError("非法 115 分享链接")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key != "password" for key, _ in query) or len(query) > 1:
            raise ValueError("非法 115 分享参数")
        password = query[0][1] if query else ""
        if parsed.fragment:
            if password:
                raise ValueError("115 提取码格式冲突")
            password = parsed.fragment
        if password and not _PASSWORD_RE.fullmatch(password):
            raise ValueError("非法 115 提取码")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("非法", "115")):
            raise
        raise ValueError("非法 115 分享链接") from None

    result = f"https://115.com/s/{path_match.group(1)}"
    if password:
        result += "?" + urlencode({"password": password})
    return result


def normalize_share_links(raw: str | list[str], limit: int = 3) -> list[str]:
    """按行或字符串数组读取链接，规范化和去重后限制单次转存数量。"""
    parts = raw.splitlines() if isinstance(raw, str) else raw
    normalized: list[str] = []
    seen: set[str] = set()
    for index, part in enumerate(parts, start=1):
        if not str(part or "").strip():
            continue
        try:
            url = canonicalize_share_url(part)
        except ValueError:
            raise ValueError(f"第 {index} 条不是有效的 115 分享链接") from None
        if url not in seen:
            seen.add(url)
            normalized.append(url)
    if not normalized:
        raise ValueError("115 分享链接为空")
    if len(normalized) > limit:
        raise ValueError(f"每次最多 {limit} 条 115 分享链接")
    return normalized


def _normalized_text(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _season_patterns(season: int) -> tuple[re.Pattern, ...]:
    return (
        re.compile(rf"(?<![A-Za-z0-9])S0*{season}(?!\d)", re.IGNORECASE),
        re.compile(rf"(?<![A-Za-z0-9])Season\s*0*{season}(?!\d)", re.IGNORECASE),
        re.compile(rf"第\s*0*{season}\s*季"),
    )


def validate_share_package(
    snapshot: ShareSnapshot,
    title: str,
    missing: list[int] | dict[int, list[int]],
    *,
    season: str | None = None,
) -> str:
    """核实非空、剧名与目标季；无法可靠识别时拒绝自动转存。"""
    files = [item for item in snapshot.files if not item["is_dir"]]
    if not files:
        raise P115Error("验包失败：空包或没有可转存文件")
    package_name = snapshot.package_name.strip()
    if not package_name:
        raise P115Error("验包失败：缺少真实包名")
    if season:
        target_seasons = {int(str(season).lstrip("Ss"))}
    elif isinstance(missing, dict):
        target_seasons = {int(value) for value in missing}
    else:
        raise P115Error("验包失败：缺少明确季号")
    if not target_seasons:
        raise P115Error("验包失败：缺少明确季号")

    paths = [str(item.get("path") or item.get("name") or "") for item in files]
    searchable = "\n".join([package_name, *paths])
    if _normalized_text(title) not in _normalized_text(package_name):
        raise P115Error("验包失败：剧名不匹配")
    for target in sorted(target_seasons):
        if not any(pattern.search(searchable) for pattern in _season_patterns(target)):
            raise P115Error(f"验包失败：未找到目标季 S{target:02d}")
    return package_name


_INVISIBLE_RE = re.compile(r"[\u00ad\u180e\u200b-\u200d\u2060\ufeff]")
_ED2K_RE = re.compile(r"ed2k://\|file\|([^|]+)\|(\d+)\|([0-9a-f]{32})\|/", re.IGNORECASE)


def _clean_resource_text(value: object) -> str:
    return " ".join(_INVISIBLE_RE.sub("", str(value or "")).split()).strip()


def validate_package_name(name: str) -> str:
    name = name.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError("资源包名含非法路径字符")
    return name


def normalize_ed2k_link(url: str) -> str:
    cleaned = _clean_resource_text(url)
    match = _ED2K_RE.fullmatch(cleaned)
    if not match:
        raise ValueError("非法 ed2k 链接")
    name = validate_package_name(match.group(1))
    return f"ed2k://|file|{name}|{match.group(2)}|{match.group(3).upper()}|/"


def classify_resource_link(url: str) -> tuple[str, str]:
    if _clean_resource_text(url).lower().startswith("ed2k://"):
        return "ed2k", normalize_ed2k_link(url)
    return "share", canonicalize_share_url(url)


def resource_identity(url: str | list[str]) -> str:
    if isinstance(url, list) or "\n" in str(url) or "\r" in str(url):
        return normalize_ed2k_group(url)[1]
    if _clean_resource_text(url).lower().startswith("ed2k://"):
        match = _ED2K_RE.fullmatch(_clean_resource_text(url))
        if not match:
            raise ValueError("非法 ed2k 链接")
        return match.group(3).upper()
    return parse_share_url(url)[0]


def ed2k_package_name(url: str) -> str:
    match = _ED2K_RE.fullmatch(_clean_resource_text(url))
    if not match:
        raise ValueError("非法 ed2k 链接")
    return validate_package_name(unquote(match.group(1)))


def normalize_ed2k_group(raw: str | list[str]) -> tuple[list[str], str, list[str]]:
    parts = raw.splitlines() if isinstance(raw, str) else raw
    links: list[str] = []
    names_by_hash: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for part in parts:
        if not str(part or "").strip():
            continue
        kind, normalized = classify_resource_link(part)
        if kind != "ed2k":
            raise ValueError("ed2k 整包不能混入其他链接类型")
        identity = resource_identity(normalized)
        name = ed2k_package_name(normalized)
        if name in names_by_hash and names_by_hash[name] != identity:
            raise ValueError("同名不同内容的 ed2k 文件无法自动归档，请人工核对")
        names_by_hash[name] = identity
        key = (name, identity)
        if key not in seen:
            seen.add(key)
            links.append(normalized)
    if not links:
        raise ValueError("ed2k 整包链接为空")
    if len(links) == 1:
        return links, resource_identity(links[0]), [ed2k_package_name(links[0])]
    digest = hashlib.sha256("\n".join(sorted(links)).encode("utf-8")).hexdigest().upper()
    names = [ed2k_package_name(link) for link in links]
    return links, f"group:{digest}", names


def normalize_resource_links(raw: str | list[str], limit: int = 3) -> list[tuple[str, str]]:
    parts = raw.splitlines() if isinstance(raw, str) else raw
    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, part in enumerate(parts, start=1):
        if not str(part or "").strip():
            continue
        try:
            kind, url = classify_resource_link(part)
            identity = (kind, resource_identity(url))
        except ValueError:
            raise ValueError(f"第 {index} 条不是有效的 115/ed2k 资源链接") from None
        if identity not in seen:
            seen.add(identity)
            normalized.append((kind, url))
    if not normalized:
        raise ValueError("资源链接为空")
    if len(normalized) > limit:
        raise ValueError(f"每次最多 {limit} 条资源链接")
    return normalized
