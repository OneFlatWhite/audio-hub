"""归档链路的文件系统原语：路径围栏、唯一目标名、线程化 move / copytree / rmtree。

所有删除与移动都要先过 `path_within` 围栏：删除只允许发生在 scratch / recycle /
quarantine 三个中枢自有目录之下。原种目录（qb save_path）**永不在围栏内**，
这是 PRD §8.2 做种哈希红线在代码层的落地 —— 不靠调用方自觉。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from loguru import logger


def path_within(path: str | Path, root: str | Path) -> bool:
    """path 是否严格位于 root 之下（等于 root 本身也算不在：不允许删整个根目录）。

    两边都 resolve：recycle_dir 配成相对路径或含符号链接时，字面比较会漏判。
    """
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except OSError:
        return False
    return p != r and r in p.parents


def unique_path(target: Path, hint: str = "") -> Path:
    """target 不存在就原样返回；存在则加 `-<hint>` / `-<n>` 后缀直到不冲突。"""
    if not target.exists():
        return target
    if hint:
        cand = target.with_name(f"{target.name}-{hint}")
        if not cand.exists():
            return cand
    n = 2
    while True:
        cand = target.with_name(f"{target.name}-{n}")
        if not cand.exists():
            return cand
        n += 1


async def move_tree(src: Path, dst: Path) -> Path:
    """shutil.move 的线程化封装；自动建父目录。返回实际落位路径。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src), str(dst))
    return dst


async def copy_tree(src: Path, dst: Path) -> Path:
    """把 src（目录或单文件）拷到 dst 目录下；dst 必须尚不存在。"""
    if src.is_dir():
        await asyncio.to_thread(shutil.copytree, src, dst, symlinks=False)
    else:
        dst.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, src, dst / src.name)
    return dst


async def remove_tree(path: Path, *, root: Path) -> bool:
    """删除 path（目录或文件），但只在它位于 root 之下时执行。

    围栏不过 → 记 error 并返回 False，调用方据此决定是否继续（通常是跳过而非中断）。
    """
    if not path_within(path, root):
        logger.error(f"拒绝删除：{path} 不在 {root} 之下")
        return False
    if not path.exists():
        return True
    if path.is_dir():
        await asyncio.to_thread(shutil.rmtree, path)
    else:
        await asyncio.to_thread(path.unlink)
    return True
