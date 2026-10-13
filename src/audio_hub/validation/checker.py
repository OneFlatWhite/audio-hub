"""入库前校验：扩展名白名单、音频文件数下限、FLAC 容器层假无损、auCDtect 频谱检测。

全部同步函数、纯 stdlib（FLAC 头解析自己写，不引 mutagen）：校验跑在
asyncio.to_thread 里，依赖越少越不容易在 NAS 的精简容器里缺库。

失败原因（reasons）是机器可读的短码，前缀固定，隔离区页面与事件流直接展示：
- no_audio_files / too_few_audio_files:<n><<min>
- suspect_lossless:<file>      FLAC 位深 <16 或采样率 <44100
- aucdtect_mpeg:<file>         auCDtect 判定为 MPEG 转码

头解析不出来的 .flac（ID3v2 前缀 / 半截文件）**跳过而非判失败**：ID3 前缀的 FLAC
是合法文件，误拦真无损比漏拦假无损代价更高（PRD §6.2 宁漏勿错的反向推论）。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from ..config import ValidationConfig

_FLAC_MAGIC = b"fLaC"
_STREAMINFO_TYPE = 0
_STREAMINFO_LEN = 34
# 低于这两个阈值的 FLAC 几乎必然是有损升频或劣质转码
_MIN_SAMPLE_RATE = 44_100
_MIN_BITS = 16
_AUCDTECT_TIMEOUT = 60


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    audio_files: int = 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reasons": list(self.reasons), "audio_files": self.audio_files}


@dataclass
class FlacInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int


def probe_flac(path: Path) -> FlacInfo | None:
    """读 FLAC 头的 STREAMINFO 块；不是 FLAC 或头损坏返回 None。

    布局：4 字节 magic → 元数据块头 1 字节（高位 last 标志 + 低 7 位类型）+ 3 字节长度
    → STREAMINFO 34 字节，其中偏移 10 起的 8 字节含采样率(20bit)/声道-1(3bit)/位深-1(5bit)。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4 + 4 + _STREAMINFO_LEN)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != _FLAC_MAGIC:
        return None
    block_type = head[4] & 0x7F
    block_len = int.from_bytes(head[5:8], "big")
    if block_type != _STREAMINFO_TYPE or block_len < _STREAMINFO_LEN:
        return None
    body = head[8:8 + _STREAMINFO_LEN]
    if len(body) < _STREAMINFO_LEN:
        return None
    packed = int.from_bytes(body[10:18], "big")
    sample_rate = packed >> 44
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    return FlacInfo(sample_rate=sample_rate, channels=channels, bits_per_sample=bits)


def _iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _aucdtect_exe(cfg: ValidationConfig) -> str | None:
    """配置了路径且可执行才返回；找不到就静默跳过（不是校验失败）。"""
    raw = (cfg.aucdtect_path or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return str(p)
    found = shutil.which(raw)
    if found:
        return found
    logger.warning(f"auCDtect 路径不可用，跳过频谱检测: {raw}")
    return None


def _run_aucdtect(exe: str, file: Path) -> str | None:
    """返回 'aucdtect_mpeg:<file>' 或 None。工具本身出错不算校验失败。"""
    try:
        # exe 来自配置，参数为固定选项 + 文件路径
        proc = subprocess.run(
            [exe, "-m0", str(file)],
            capture_output=True, text=True, timeout=_AUCDTECT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"auCDtect 执行失败 {file.name}: {e}")
        return None
    output = f"{proc.stdout}\n{proc.stderr}"
    if "MPEG" in output.upper():
        return f"aucdtect_mpeg:{file.name}"
    return None


def validate_dir(path: Path, cfg: ValidationConfig) -> ValidationResult:
    """对 scratch 副本做容器层 + 可选频谱层校验。

    cfg.enabled=False 直接放行（仍统计音频文件数，方便日志）。
    """
    path = Path(path)
    allowed = {e.lower().lstrip(".") for e in cfg.allowed_exts}
    audio: list[Path] = []
    for f in _iter_files(path):
        if f.suffix.lower().lstrip(".") in allowed:
            audio.append(f)

    if not cfg.enabled:
        return ValidationResult(ok=True, reasons=[], audio_files=len(audio))

    reasons: list[str] = []
    if not audio:
        reasons.append("no_audio_files")
    elif len(audio) < cfg.min_audio_files:
        reasons.append(f"too_few_audio_files:{len(audio)}<{cfg.min_audio_files}")

    if cfg.fake_lossless_check:
        for f in audio:
            if f.suffix.lower() != ".flac":
                continue
            info = probe_flac(f)
            if info is None:
                logger.debug(f"FLAC 头无法解析，跳过假无损检查: {f.name}")
                continue
            if info.bits_per_sample < _MIN_BITS or info.sample_rate < _MIN_SAMPLE_RATE:
                reasons.append(f"suspect_lossless:{f.name}")

    exe = _aucdtect_exe(cfg)
    if exe:
        for f in audio:
            if f.suffix.lower() not in (".wav", ".flac"):
                continue
            bad = _run_aucdtect(exe, f)
            if bad:
                reasons.append(bad)

    result = ValidationResult(ok=not reasons, reasons=reasons, audio_files=len(audio))
    if not result.ok:
        logger.warning(f"校验未通过 {path.name}: {reasons}")
    return result
