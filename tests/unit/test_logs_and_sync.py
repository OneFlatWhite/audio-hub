"""日志中心（/logs、/api/logs/tail）与 records 立即同步的测试。

tail_lines / filter_level 是纯函数，直接单测（含窗口截断的残行丢弃）；
页面与 API 用 TestClient 走真实路由：web_env 的 db_path 在 tmp 下，
lifespan 会在 tmp/logs/ 建文件 sink，测试往里写标记行再断言渲染。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import Task, TaskState
from audio_hub.web import routes_logs

# ── 纯函数单测 ──────────────────────────────────────────────

def _mklog(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "hub.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_tail_lines_returns_last_n(tmp_path):
    p = _mklog(tmp_path, [f"line-{i}" for i in range(10)])
    assert routes_logs.tail_lines(p, n=3) == ["line-7", "line-8", "line-9"]


def test_tail_lines_missing_file_returns_empty(tmp_path):
    assert routes_logs.tail_lines(tmp_path / "nope.log") == []


def test_tail_lines_drops_partial_first_line(tmp_path, monkeypatch):
    """读取窗口切在行中间时，第一行残行必须丢弃。"""
    monkeypatch.setattr(routes_logs, "_TAIL_WINDOW_BYTES", 8)
    p = _mklog(tmp_path, ["aaaaaaaaaa", "bbbb", "cccc"])
    out = routes_logs.tail_lines(p, n=10)
    assert out[0].startswith(("aaaa", "bbbb", "cccc")) and "aaaacccc" not in out
    assert out[-1] == "cccc"


def test_filter_level():
    lines = [
        "2026-10-14 00:00:00 | INFO     | a:b:1 | hello",
        "2026-10-14 00:00:01 | WARNING  | a:b:2 | careful",
        "2026-10-14 00:00:02 | ERROR    | a:b:3 | boom",
    ]
    assert len(routes_logs.filter_level(lines, None)) == 3
    assert len(routes_logs.filter_level(lines, "all")) == 3
    assert routes_logs.filter_level(lines, "error") == [lines[2]]
    assert routes_logs.filter_level(lines, "warning") == [lines[1]]
    assert routes_logs.filter_level(lines, "info") == [lines[0]]
    assert routes_logs.filter_level(lines, "unknown") == lines


# ── 集成：页面与 API ────────────────────────────────────────

@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    """与 test_web_routes 同款：tmp 配置 + 密钥；db_path 在 tmp 下。"""
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "INFO"
qbittorrent:
  host: "localhost"
  port: 8085
web:
  cookie_secure: false
sites:
  - name: "hhanclub"
    enabled: false
    rss_url: "https://hhanclub.net/torrentrss.php?passkey=FAKE"
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    return tmp_path


@pytest.fixture
def client(web_env):
    with TestClient(app) as c:
        yield c


def _setup_admin(client: TestClient):
    resp = client.post("/setup", data={
        "username": "ops", "password": "strongpass123", "password2": "strongpass123",
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)


def _csrf(client: TestClient) -> str:
    page = client.get("/records").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', page)
    assert m, "页面没有 csrf-token meta"
    return m.group(1)


def test_logs_requires_login(web_env, client):
    resp = client.get("/logs", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers["location"]


def test_logs_page_renders_file_lines(web_env, client):
    _setup_admin(client)
    log = web_env / "logs" / "hub.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write("2026-10-14 10:00:00.000 | INFO     | test.module:1 | MARKER-ALPHA\n")
        f.write("2026-10-14 10:00:01.000 | ERROR    | test.module:2 | MARKER-BOOM\n")
    page = client.get("/logs").text
    assert "MARKER-ALPHA" in page and "MARKER-BOOM" in page
    assert "实时跟随" in page
    err_page = client.get("/logs?level=error").text
    assert "MARKER-BOOM" in err_page and "MARKER-ALPHA" not in err_page


def test_logs_tail_api(web_env, client):
    _setup_admin(client)
    log = web_env / "logs" / "hub.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write("2026-10-14 10:00:00.000 | INFO     | test.module:1 | MARKER-JSON\n")
    data = client.get("/api/logs/tail?n=10").json()
    assert any("MARKER-JSON" in ln for ln in data["lines"])
    data_err = client.get("/api/logs/tail?n=10&level=error").json()
    assert data_err["lines"] == []


def test_sync_now_requires_csrf(web_env, client):
    _setup_admin(client)
    resp = client.post("/records/sync-now", data={}, follow_redirects=False)
    assert resp.status_code in (403, 303)  # CSRF 拒绝或安全回落


def test_sync_now_redirects_with_flash(web_env, client):
    _setup_admin(client)
    resp = client.post("/records/sync-now", data={"csrf_token": _csrf(client)},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "ok=synced" in resp.headers["location"]
    page = client.get("/records?ok=synced").text
    assert "已同步下载器状态" in page


# ── 设置热生效：日志级别与归档路径 ──────────────────────────

def test_log_level_is_hot(web_env, client):
    """改日志级别：立即生效、不挂待重启横幅（v0.11 起）。"""
    import asyncio

    from audio_hub import settings_store
    _setup_admin(client)
    resp = client.post("/settings/general",
                       data={"log_level": "DEBUG", "csrf_token": _csrf(client)},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "saved_general" in resp.headers["location"]
    assert app.state.config.log_level == "DEBUG"
    assert asyncio.run(settings_store.is_restart_pending(app.state.db)) is False


def test_path_setting_is_hot_and_creates_dir(web_env, client):
    """改归档路径：立即生效且目录被创建（v0.11 起不再要求重启）。"""
    import asyncio

    from audio_hub import settings_store
    _setup_admin(client)
    new_dir = web_env / "custom-music-library"
    resp = client.post("/settings/paths",
                       data={"library_music": str(new_dir), "csrf_token": _csrf(client)},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "saved_paths" in resp.headers["location"]
    assert app.state.config.paths.library_music == str(new_dir)
    assert new_dir.is_dir(), "保存后应立即建目录"
    assert asyncio.run(settings_store.is_restart_pending(app.state.db)) is False


# ── 命中记录分页 ────────────────────────────────────────────

def _seed_task(title: str, state=TaskState.DISPATCHED) -> str:
    import asyncio
    tid = str(uuid.uuid4())
    asyncio.run(repo.create_task(app.state.db, Task(
        id=tid, info_hash=uuid.uuid4().hex, site="hhanclub", title=title,
        state=state, confidence="high",
    )))
    return tid


def test_records_pagination(web_env, client):
    """page/page_size 分页：每页行数正确、页脚有总数与翻页链接。"""
    _setup_admin(client)
    for i in range(3):
        _seed_task(f"分页专辑 {i}")
    p1 = client.get("/records?page=1&page_size=2").text
    p2 = client.get("/records?page=2&page_size=2").text
    p3 = client.get("/records?page=3&page_size=2").text
    # 标题在 td 文本与 title 属性各出现一次，">" 前缀只计单元格文本
    assert p1.count(">分页专辑") == 2
    assert p2.count(">分页专辑") == 1
    assert p3.count(">分页专辑") == 0
    assert "共 3 条" in p1 and "第 1 / 2 页" in p1
    assert "上一页" not in p1 and "下一页" in p1
    assert "下一页" not in p2 and "上一页" in p2
    # 状态筛选与分页共存：链接保留 state 参数
    p_filtered = client.get("/records?state=dispatched&page=1&page_size=2").text
    assert "state=dispatched&amp;page=2" in p_filtered
