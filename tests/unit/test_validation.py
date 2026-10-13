"""入库校验：扩展名白名单、文件数下限、FLAC 头假无损检测、auCDtect 跳过逻辑。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from audio_hub.config import ValidationConfig
from audio_hub.validation.checker import probe_flac, validate_dir


def make_flac(path: Path, sample_rate: int = 44_100, bits: int = 16, channels: int = 2) -> Path:
    """构造只含 magic + STREAMINFO 的最小 FLAC 头（足够让 probe_flac 解析）。"""
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
    streaminfo = bytearray(34)
    streaminfo[10:18] = packed.to_bytes(8, "big")
    head = b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + bytes(streaminfo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + b"\x00" * 64)
    return path


class TestProbeFlac:
    def test_parses_streaminfo(self, tmp_path):
        f = make_flac(tmp_path / "a.flac", sample_rate=96_000, bits=24, channels=2)
        info = probe_flac(f)
        assert info is not None
        assert (info.sample_rate, info.bits_per_sample, info.channels) == (96_000, 24, 2)

    def test_non_flac_returns_none(self, tmp_path):
        f = tmp_path / "x.flac"
        f.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 60)
        assert probe_flac(f) is None
        assert probe_flac(tmp_path / "missing.flac") is None


class TestValidateDir:
    def test_valid_flac_passes(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        make_flac(tmp_path / "album" / "02.flac")
        (tmp_path / "album" / "cover.jpg").write_bytes(b"\xff\xd8")
        r = validate_dir(tmp_path / "album", ValidationConfig())
        assert r.ok and r.reasons == [] and r.audio_files == 2

    def test_8bit_flac_is_suspect(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac", bits=8)
        r = validate_dir(tmp_path / "album", ValidationConfig())
        assert not r.ok
        assert r.reasons == ["suspect_lossless:01.flac"]

    def test_low_sample_rate_is_suspect(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac", sample_rate=22_050)
        r = validate_dir(tmp_path / "album", ValidationConfig())
        assert r.reasons == ["suspect_lossless:01.flac"]

    def test_fake_lossless_check_can_be_disabled(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac", bits=8)
        r = validate_dir(tmp_path / "album", ValidationConfig(fake_lossless_check=False))
        assert r.ok

    def test_no_allowed_extensions(self, tmp_path):
        (tmp_path / "album").mkdir()
        (tmp_path / "album" / "movie.mkv").write_bytes(b"x")
        (tmp_path / "album" / "readme.txt").write_text("x")
        r = validate_dir(tmp_path / "album", ValidationConfig())
        assert not r.ok
        assert r.reasons == ["no_audio_files"] and r.audio_files == 0

    def test_too_few_audio_files(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        r = validate_dir(tmp_path / "album", ValidationConfig(min_audio_files=3))
        assert r.reasons == ["too_few_audio_files:1<3"]

    def test_disabled_passes_everything(self, tmp_path):
        (tmp_path / "album").mkdir()
        (tmp_path / "album" / "movie.mkv").write_bytes(b"x")
        r = validate_dir(tmp_path / "album", ValidationConfig(enabled=False))
        assert r.ok and r.reasons == [] and r.audio_files == 0

    def test_single_file_path(self, tmp_path):
        f = make_flac(tmp_path / "single.flac")
        r = validate_dir(f, ValidationConfig())
        assert r.ok and r.audio_files == 1

    def test_unparseable_flac_header_is_skipped(self, tmp_path):
        # ID3v2 前缀的 FLAC 是合法文件：解析不出头不算失败
        (tmp_path / "album").mkdir()
        (tmp_path / "album" / "01.flac").write_bytes(b"ID3" + b"\x00" * 80)
        r = validate_dir(tmp_path / "album", ValidationConfig())
        assert r.ok

    def test_missing_aucdtect_is_skipped(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        cfg = ValidationConfig(aucdtect_path=str(tmp_path / "no-such-aucdtect"))
        r = validate_dir(tmp_path / "album", cfg)
        assert r.ok


@pytest.mark.skipif(sys.platform == "win32", reason="需要可执行 shell 脚本")
class TestAucdtect:
    def _fake_tool(self, tmp_path: Path, output: str) -> str:
        exe = tmp_path / "aucdtect"
        exe.write_text(f"#!/bin/sh\necho '{output}'\n")
        exe.chmod(exe.stat().st_mode | 0o111)
        return str(exe)

    def test_mpeg_output_fails(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        exe = self._fake_tool(tmp_path, "This track looks like MPEG with probability 95%")
        r = validate_dir(tmp_path / "album", ValidationConfig(aucdtect_path=exe))
        assert r.reasons == ["aucdtect_mpeg:01.flac"]

    def test_cdda_output_passes(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        exe = self._fake_tool(tmp_path, "This track looks like CDDA with probability 100%")
        r = validate_dir(tmp_path / "album", ValidationConfig(aucdtect_path=exe))
        assert r.ok

    def test_only_wav_flac_are_checked(self, tmp_path):
        (tmp_path / "album").mkdir()
        (tmp_path / "album" / "01.mp3").write_bytes(b"\xff\xfb" * 8)
        exe = self._fake_tool(tmp_path, "MPEG")
        r = validate_dir(tmp_path / "album", ValidationConfig(aucdtect_path=exe))
        assert r.ok

    def test_tool_crash_is_not_a_failure(self, tmp_path):
        make_flac(tmp_path / "album" / "01.flac")
        exe = tmp_path / "aucdtect"
        exe.write_text("#!/bin/sh\nexit 3\n")
        exe.chmod(exe.stat().st_mode | 0o111)
        r = validate_dir(tmp_path / "album", ValidationConfig(aucdtect_path=str(exe)))
        assert r.ok
        assert os.access(exe, os.X_OK)
