"""cli.py 冒烟：verify-adapters 在无凭据环境下应跑到逐站验证，而非零适配器退出。

回归背景：v0.6 认证门禁（build_adapters 默认 require_valid_auth=True）叠加
「站点权威源迁 DB」后，直接拿 YAML config 建适配器恒为零（auth_status 恒为
unconfigured），verify-adapters 静默废掉。修复后走 bootstrap 装配并显式绕过门禁。
"""

from __future__ import annotations

import asyncio

import pytest

from audio_hub import cli
from audio_hub.adapters.base import BaseAdapter


@pytest.fixture
def cli_env(tmp_path, tmp_key, monkeypatch):
    """YAML 两站 enabled、无有效凭据；db_path 指向临时目录。"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
qbittorrent:
  host: "qb.local"
  port: 8085
  save_path: "{(tmp_path / 'seeds').as_posix()}"
sites:
  - name: "hhanclub"
    enabled: true
    rss_url: "https://hhanclub.net/torrentrss.php?passkey=FAKE"
  - name: "mteam"
    enabled: true
    api_key: "MTOKEN"
""", encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(tmp_key))
    return tmp_path


async def _fake_health(self):
    return True, "OK"


def test_verify_adapters_proceeds_without_valid_auth(cli_env, monkeypatch, capsys):
    """无凭据环境（无库、auth_status 全 unconfigured）：逐站验证而非零适配器退出。"""
    monkeypatch.setattr(BaseAdapter, "health_check", _fake_health)
    rc = cli.main(["verify-adapters"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "没有可用适配器" not in out
    assert "[hhanclub]" in out and "[mteam]" in out
    assert "数据库不存在" in out  # 纯 YAML 降级有提示
    assert not (cli_env / "hub.db").exists()  # 诊断路径不建库


def test_verify_adapters_reads_sites_from_db(cli_env, monkeypatch, capsys):
    """库存在时站点来自 sites 表：界面里禁用的站不再参与（YAML 的 enabled 说了不算）。"""
    from audio_hub.bootstrap import bootstrap_runtime
    from audio_hub.config import load_config

    async def _seed():
        ctx = await bootstrap_runtime(load_config(), create_db=True)
        await ctx.db.execute("UPDATE sites SET enabled = 0 WHERE id = 'mteam'")
        await ctx.db.commit()
        await ctx.close()

    asyncio.run(_seed())
    monkeypatch.setattr(BaseAdapter, "health_check", _fake_health)
    rc = cli.main(["verify-adapters"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[hhanclub]" in out
    assert "[mteam]" not in out
