"""命令行入口：管理 watchlist、手动跑一轮、初始化与诊断。

用法：
    python -m audio_hub.cli init-db                 # 初始化数据库
    python -m audio_hub.cli add --query 周杰伦       # 添加音乐订阅
    python -m audio_hub.cli add --query 三体 --kind audiobook
    python -m audio_hub.cli list                    # 列出订阅项
    python -m audio_hub.cli remove --id <uuid>       # 删除订阅项
    python -m audio_hub.cli run-once [--no-dry-run]  # 手动跑一轮
    python -m audio_hub.cli status                  # 任务状态统计
    python -m audio_hub.cli verify-adapters         # 四站连通性验证

凭据通过环境变量传入，不接受命令行明文（避免进入 shell history）：
    AUDIO_HUB_CONFIG   配置文件路径
    AUDIO_HUB_SECRETS_KEY  密钥文件路径
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from loguru import logger

from . import repo
from .config import load_config
from .db import init_db
from .models import WatchEntry, WatchKind


async def cmd_init_db(config) -> int:
    db = await init_db(config.db_path)
    await db.close()
    print(f"✅ 数据库已初始化: {config.db_path}")
    return 0


async def cmd_add(config, query: str, kind: str, aliases: list[str]) -> int:
    db = await init_db(config.db_path)
    try:
        entry = WatchEntry(
            id=str(uuid.uuid4()),
            query=query,
            kind=WatchKind(kind),
            aliases=aliases or [],
            min_confidence="high",     # PRD §6.2：起步全部设 high
            auto_download=True,
        )
        await repo.add_watch_entry(db, entry)
        print(f"✅ 已添加订阅: [{kind}] {query!r} (id={entry.id})")
        return 0
    finally:
        await db.close()


async def cmd_list(config) -> int:
    db = await init_db(config.db_path)
    try:
        entries = await repo.list_watch_entries(db, enabled_only=False)
        if not entries:
            print("（watchlist 为空）")
            return 0
        print(f"{'ID':38} {'类型':10} {'查询词':20} 启用")
        print("-" * 80)
        for e in entries:
            print(f"{e.id:38} {e.kind.value:10} {e.query[:20]:20} {'✓' if e.enabled else '✗'}")
        return 0
    finally:
        await db.close()


async def cmd_remove(config, entry_id: str) -> int:
    db = await init_db(config.db_path)
    try:
        ok = await repo.delete_watch_entry(db, entry_id)
        print(f"{'✅ 已删除' if ok else '⚠️ 未找到'}: {entry_id}")
        return 0 if ok else 1
    finally:
        await db.close()


async def cmd_status(config) -> int:
    db = await init_db(config.db_path)
    try:
        by_state = await repo.count_tasks_by_state(db)
        entries = await repo.list_watch_entries(db, enabled_only=False)
        print(f"dry_run: {config.dry_run}")
        print(f"订阅项: {len(entries)}")
        print("任务状态:")
        if not by_state:
            print("  （无任务）")
        for state, n in sorted(by_state.items()):
            print(f"  {state}: {n}")
        return 0
    finally:
        await db.close()


async def cmd_run_once(config, no_dry_run: bool) -> int:
    from .notify.log_notify import LogNotifier
    from .scheduler import PipelineRunner

    if no_dry_run:
        config = config.model_copy(update={"dry_run": False})
        logger.warning("已关闭 dry_run，将真实派发下载到 qb！")

    db = await init_db(config.db_path)
    try:
        qb_client = None
        if not config.dry_run:
            from .main import _make_qb_client
            qb_client = _make_qb_client(config.qbittorrent)
            if qb_client is None:
                print("❌ 实跑模式但 qb 连接失败，中止")
                return 1

        runner = PipelineRunner(db, config, qb_client=qb_client, notifier=LogNotifier())
        await runner.monitor.reconcile_on_startup()
        summary = await runner.run_once()

        print("\n本轮结果:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return 0
    finally:
        await db.close()


async def cmd_verify_adapters(config) -> int:
    """四站连通性验证（PRD 阶段 4）。需真实凭据。"""
    from .adapters.registry import build_adapters

    adapters = build_adapters(config)
    if not adapters:
        print("❌ 没有可用适配器，检查 config.yaml")
        return 1

    print(f"验证 {len(adapters)} 个站点...\n")
    any_ok = False
    for name, adapter in adapters.items():
        print(f"[{name}] ", end="", flush=True)
        ok, msg = await adapter.health_check()
        print(f"{'✅' if ok else '❌'} {msg}")
        if ok:
            any_ok = True
    return 0 if any_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audio-hub", description="音频获取中枢 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库")

    p_add = sub.add_parser("add", help="添加订阅项")
    p_add.add_argument("--query", required=True, help="艺术家名 / 书名")
    p_add.add_argument("--kind", default="music", choices=["music", "audiobook"])
    p_add.add_argument("--aliases", nargs="*", default=[], help="别名列表")

    sub.add_parser("list", help="列出订阅项")

    p_rm = sub.add_parser("remove", help="删除订阅项")
    p_rm.add_argument("--id", required=True)

    sub.add_parser("status", help="任务状态统计")

    p_run = sub.add_parser("run-once", help="手动跑一轮")
    p_run.add_argument("--no-dry-run", action="store_true", help="关闭空跑，真实下载")

    sub.add_parser("verify-adapters", help="四站连通性验证")

    args = parser.parse_args(argv)

    try:
        config = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 配置加载失败: {e}", file=sys.stderr)
        return 1

    coro = {
        "init-db": lambda: cmd_init_db(config),
        "add": lambda: cmd_add(config, args.query, args.kind, args.aliases),
        "list": lambda: cmd_list(config),
        "remove": lambda: cmd_remove(config, args.id),
        "status": lambda: cmd_status(config),
        "run-once": lambda: cmd_run_once(config, args.no_dry_run),
        "verify-adapters": lambda: cmd_verify_adapters(config),
    }[args.command]()

    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
