"""日志管线装配（v0.11 自 main.lifespan 抽出，支持热更新级别）。

两个 sink：
- 标准 logging 桥接（uvicorn / 容器编排习惯消费 stdout）
- 文件 sink：db_path 同级 logs/hub.log，10MB×5 轮转 —— 界面「日志」页的数据源

级别热更新：settings_store.apply_hot 收到 log_level 改动时调 set_level()，
整体重建两个 sink（loguru 的 remove/add 是线程安全的，enqueue 队列会排空）。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

_SINK_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"


def log_dir_for(db_path: str | Path) -> Path:
    """日志目录：数据卷内（db_path 同级），随数据走。"""
    return Path(db_path).parent / "logs"


def setup_logging(level: str, db_path: str | Path) -> None:
    """（重）建全部 sink。启动与级别热更新共用这一条路径。"""
    logger.remove()
    # 桥接 stdout：容器环境 docker logs 仍能看到
    logger.add(
        lambda msg: _bridge(msg, level),
        level=level,
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )
    # 文件 sink：/logs 页与排障依据；目录不可写只降级不致命
    try:
        log_dir = log_dir_for(db_path)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "hub.log",
            rotation="10 MB", retention=5,
            level=level, encoding="utf-8", enqueue=True,
            format=_SINK_FORMAT,
        )
    except OSError as e:
        logger.warning(f"日志目录不可写（{log_dir_for(db_path)}）: {e}；仅保留标准输出日志")


def _bridge(msg, level: str) -> None:
    """loguru → 标准 logging。级别由 sink 的 level 过滤，这里只做转发。"""
    import logging

    logging.getLogger("audio_hub").handle(
        logging.LogRecord(
            name="audio_hub",
            level=getattr(logging, level.upper(), logging.INFO),
            pathname="", lineno=0, msg=msg.rstrip(), args=(), exc_info=None,
        )
    )
