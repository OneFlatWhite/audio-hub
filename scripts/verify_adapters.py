#!/usr/bin/env python3
"""四站连通性与解析验证工具（PRD 阶段 4）。

用途：
1. 验证四个站的凭据是否有效、RSS/API 是否可达
2. 输出每站的真实样本，用于校准解析器（fixture 替换）
3. 验证已知风险点的实际表现：
   - 憨憨 cat410=1 是否生效
   - NexusPHP RSS search 参数是否被忽略
   - 馒头双域名哪个可用

用法：
    export AUDIO_HUB_CONFIG=/path/to/config.yaml
    export AUDIO_HUB_SECRETS_KEY=/path/to/secrets.key
    python scripts/verify_adapters.py
    python scripts/verify_adapters.py --save-samples     # 保存响应样本
    python scripts/verify_adapters.py --site hhanclub    # 只验证单站

⚠️ 安全：--save-samples 保存的样本可能含 passkey，会先脱敏；
   但保存到版本库前仍需人工复核。默认输出目录在 .verify_samples/（已 gitignore）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from audio_hub.adapters.base import AdapterError
from audio_hub.adapters.registry import build_adapters
from audio_hub.config import load_config

SAMPLE_DIR = Path(__file__).resolve().parent.parent / ".verify_samples"

_PASSKEY_RE = re.compile(r"(passkey=)[^&\s\"']+", re.IGNORECASE)


def mask(text: str) -> str:
    """脱敏 passkey。保存样本前必须调用。"""
    return _PASSKEY_RE.sub(r"\1REDACTED", text)


async def verify_one(name, adapter, save_samples: bool) -> dict:
    """验证单个站点，返回结果字典。"""
    result = {
        "site": name,
        "ok": False,
        "elapsed_s": 0.0,
        "count": 0,
        "error": None,
        "category_breakdown": {},
        "sample_titles": [],
    }

    t0 = time.time()
    try:
        torrents = await adapter.fetch_rss()
        result["count"] = len(torrents)
        result["ok"] = len(torrents) > 0

        for t in torrents:
            cat = t.category.value
            result["category_breakdown"][cat] = result["category_breakdown"].get(cat, 0) + 1

        result["sample_titles"] = [
            {
                "title": t.title[:90],
                "category": t.category.value,
                "size_mb": round(t.size_bytes / 1024**2, 1),
                "discount": t.discount.value,
                "info_hash_present": bool(t.info_hash),
                "has_download_url": bool(t.download_url),
                "format_tags": t.format_tags[:5],
            }
            for t in torrents[:8]
        ]

        if save_samples and torrents:
            SAMPLE_DIR.mkdir(exist_ok=True)
            out = SAMPLE_DIR / f"{name}_{datetime.now().astimezone():%Y%m%d_%H%M%S}.json"
            payload = mask(json.dumps(result["sample_titles"], ensure_ascii=False, indent=2))
            out.write_text(payload, encoding="utf-8")
            result["sample_file"] = str(out)

    except AdapterError as e:
        result["error"] = f"{type(e).__name__}: {mask(str(e))}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"未预期异常 {type(e).__name__}: {mask(str(e))}"

    result["elapsed_s"] = round(time.time() - t0, 2)
    return result


async def check_hhanclub_cat_filter(config) -> str:
    """专项验证：憨憨 cat410=1 是否真的生效。

    对比「带分类参数的 RSS」与「不带分类参数的 RSS」返回内容差异。
    """
    from audio_hub.adapters.nexusphp import NexusPhpAdapter

    site = next((s for s in config.sites if s.name == "hhanclub"), None)
    if site is None or not site.enabled:
        return "⏭️  跳过（hhanclub 未配置或未启用）"

    # 对比：带分类参数 vs 不带分类参数，判断 cat{id}=1 是否真的生效
    adapter_with = NexusPhpAdapter(site.model_copy(update={"music_cat_id": site.music_cat_id}))
    adapter_without = NexusPhpAdapter(site.model_copy(update={"music_cat_id": ""}))

    try:
        with_cat = await adapter_with.fetch_rss()
        without_cat = await adapter_without.fetch_rss()
    except AdapterError as e:
        return f"❌ 验证失败: {mask(str(e))}"

    cat_counts = {}
    for t in with_cat:
        cat_counts[t.category.value] = cat_counts.get(t.category.value, 0) + 1

    # 判据：若带分类参数的结果里仍有大量 video，说明 cat 参数未生效
    video_ratio = cat_counts.get("video", 0) / max(len(with_cat), 1)
    if not with_cat:
        return "⚠️  带分类参数返回 0 条——cat 参数可能导致空结果，建议移除"
    if video_ratio > 0.2:
        return (
            f"❌ cat{site.music_cat_id}=1 **未生效**（结果中 video 占 {video_ratio:.0%}）。"
            f"带参数 {len(with_cat)} 条 / 不带参数 {len(without_cat)} 条。"
            f"→ 必须依赖本地类型判别，config 中该参数仅作装饰。"
        )
    return (
        f"✅ cat{site.music_cat_id}=1 生效（video 占 {video_ratio:.0%}）。"
        f"带参数 {len(with_cat)} 条 / 不带参数 {len(without_cat)} 条"
    )


async def check_nexusphp_search(config) -> dict[str, str]:
    """专项验证：各 NexusPHP 站的 RSS search 参数是否被站点忽略。"""
    from audio_hub.adapters.nexusphp import NexusPhpAdapter

    results = {}
    probe = "FLAC"   # 音乐区几乎必然存在的关键词
    for site in config.sites:
        if not site.enabled or site.name == "mteam":
            continue
        adapter = NexusPhpAdapter(site)
        try:
            full = await adapter.fetch_rss()
            searched = await adapter.search(probe)
            if len(searched) == len(full):
                results[site.name] = (
                    f"⚠️  search 参数疑似被忽略（搜 {probe!r} 返回 {len(searched)} 条 "
                    f"= 全量 {len(full)} 条）→ 必须依赖本地过滤"
                )
            elif not searched:
                results[site.name] = f"❌ search({probe!r}) 返回 0 条 → fallback 生效路径"
            else:
                results[site.name] = (
                    f"✅ search({probe!r}) 返回 {len(searched)} 条（全量 {len(full)}）"
                )
        except AdapterError as e:
            results[site.name] = f"❌ search 失败: {mask(str(e))}"
    return results


async def main_async(args) -> int:
    config = load_config()
    adapters = build_adapters(config)

    if not adapters:
        print("❌ 没有可用适配器。检查 config.yaml 的 sites 是否 enabled、凭据是否已加密填入。")
        return 1

    targets = {args.site: adapters[args.site]} if args.site and args.site in adapters else adapters
    if args.site and args.site not in adapters:
        print(f"❌ 站点 {args.site!r} 不可用。可选: {sorted(adapters)}")
        return 1

    print("=" * 70)
    print(f"  audio-hub 站点验证 | {datetime.now().astimezone():%Y-%m-%d %H:%M:%S} | dry_run={config.dry_run}")
    print("=" * 70)

    all_ok = True
    for name, adapter in targets.items():
        print(f"\n[{name}] 验证中...", flush=True)
        r = await verify_one(name, adapter, args.save_samples)
        all_ok = all_ok and r["ok"]

        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} {name}: {r['count']} 条 | {r['elapsed_s']}s")
        if r["error"]:
            print(f"   错误: {r['error']}")
        if r["category_breakdown"]:
            print(f"   类型分布: {r['category_breakdown']}")
            music = r["category_breakdown"].get("music", 0)
            video = r["category_breakdown"].get("video", 0)
            unknown = r["category_breakdown"].get("unknown", 0)
            if video > music:
                print(f"   ⚠️  video({video}) > music({music})：分类过滤可能未生效")
            if unknown > 0:
                print(f"   ⚠️  {unknown} 条 UNKNOWN：类型判别关键词需按真实标题补充")
        for s in r["sample_titles"][:3]:
            print(f"     · [{s['category']}] {s['title']} ({s['size_mb']}MB)")
        if r.get("sample_file"):
            print(f"   样本已存: {r['sample_file']}")

    # ── 专项风险验证 ──
    if not args.site or args.site == "hhanclub":
        print("\n" + "-" * 70)
        print("专项验证 1：憨憨 cat410=1 是否生效")
        print("  " + await check_hhanclub_cat_filter(config))

    if not args.site:
        print("\n专项验证 2：NexusPHP RSS search 参数是否被忽略")
        for site, msg in (await check_nexusphp_search(config)).items():
            print(f"  [{site}] {msg}")

    if (not args.site or args.site == "mteam") and "mteam" in adapters:
        print("\n专项验证 3：馒头双域名可用性")
        mt = adapters["mteam"]
        print(f"  当前 base URL 顺序: {mt._base_urls}")
        print(f"  本轮实际使用: {mt._active_base or '（未发起 API 请求）'}")

    print("\n" + "=" * 70)
    print(f"总结: {'✅ 全部可用' if all_ok else '⚠️ 存在失败站点，见上方详情'}")
    print("=" * 70)
    return 0 if all_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="四站连通性与解析验证")
    parser.add_argument("--site", help="只验证指定站点")
    parser.add_argument("--save-samples", action="store_true",
                        help="保存响应样本到 .verify_samples/（已脱敏）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
