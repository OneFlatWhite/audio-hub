"""发现墙路由集成测试：空态 / 渲染 / 一键订阅（含去重与立即搜索）/ 已订阅置灰 / 权限与 CSRF / 封面代理。

fixture 与 test_subscription_routes.py 同构（独立复制，避免跨文件 import 测试模块）。
封面 provider 置 none，不触网；/covers/{key} 的 200 用例直接往缓存目录写文件。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.adapters.base import DiscountType, MediaCategory, NormalizedTorrent
from audio_hub.discover.aggregator import WallCache
from audio_hub.discover.covers import cache_key
from audio_hub.main import app
from audio_hub.models import SubType, WatchEntry, WatchKind
from audio_hub.subscription import engine
from audio_hub.web import routes_discover, routes_subscriptions


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch):
    from audio_hub.crypto import generate_key
    key_file = tmp_path / "secrets.key"
    key_file.write_bytes(generate_key())

    config_text = f"""
dry_run: true
db_path: "{(tmp_path / 'hub.db').as_posix()}"
log_level: "INFO"
poll_interval_minutes: 30

qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: ""
  category: "audio-hub"
  tag: "hub"
  save_path: "{(tmp_path / 'seeds').as_posix()}"

web:
  cookie_secure: false
  auth_request_limit: 5
  auth_window_minutes: 15

discover:
  cover_provider: "none"

sites:
  - name: "hhanclub"
    enabled: false
    rss_url: "https://hhanclub.net/torrentrss.php?passkey=FAKE"
    music_cat_id: "410"
    max_rows: 50
"""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("AUDIO_HUB_CONFIG", str(cfg_file))
    monkeypatch.setenv("AUDIO_HUB_SECRETS_KEY", str(key_file))
    monkeypatch.setattr(engine, "MIN_REQUEST_INTERVAL_SECONDS", 0)
    return tmp_path


@pytest.fixture
def client(web_env):
    with TestClient(app) as c:
        # 墙缓存挂在 app.state 上会跨用例残留，每个用例重置
        app.state.discover_cache = WallCache()
        yield c


def _setup_admin(client: TestClient, username="ops", password="strongpass123"):
    resp = client.post("/setup", data={
        "username": username, "password": password, "password2": password,
    }, follow_redirects=False)
    assert resp.status_code in (303, 200), resp.text
    return resp


def _csrf(client: TestClient) -> str:
    page = client.get("/dashboard")
    m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert m, f"页面未找到 CSRF meta: {page.status_code}"
    return m.group(1)


def _run(coro):
    return asyncio.run(coro)


def _post(client: TestClient, path: str, **data):
    data.setdefault("csrf_token", _csrf(client))
    return client.post(path, data=data, follow_redirects=False)


def _all_entries() -> list[WatchEntry]:
    return _run(repo.list_watch_entries(app.state.db, enabled_only=False))


class _FakeCfg:
    auth_status = "valid"
    priority = 0


class FakeAdapter:
    def __init__(self, site, results):
        self.site_name = site
        self.config = _FakeCfg()
        self.results = results
        self.search_calls: list[tuple[str, str | None]] = []

    async def search(self, keyword, kind=None):
        self.search_calls.append((keyword, kind))
        return list(self.results)

    async def fetch_rss(self, query=None, kind=None):
        return list(self.results)


def _torrent(title, *, site="fake", category=MediaCategory.MUSIC, seeders=10):
    return NormalizedTorrent(
        info_hash=uuid.uuid4().hex + "00000000", title=title, title_normalized=title.lower(),
        site=site, category=category,
        download_url=f"https://{site}/dl/{abs(hash(title))}",
        size_bytes=700 * 1024 ** 2, discount=DiscountType.NORMAL, seeders=seeders,
    )


@pytest.fixture
def fake_wall(monkeypatch):
    """发现墙与立即搜索都用同一批替身站点（2 站，各 1 条音乐 + 1 条有声书）。"""
    a1 = FakeAdapter("fake1", [_torrent("周杰伦 - 叶惠美 (2003) [FLAC 24-96]", site="fake1")])
    a2 = FakeAdapter("fake2", [_torrent("周杰伦 - 叶惠美 (2003) [FLAC]", site="fake2"),
                               _torrent("林俊杰 - 幸存者 [FLAC]", site="fake2"),
                               _torrent("刘慈欣 - 三体 有声书", site="fake2",
                                        category=MediaCategory.AUDIOBOOK)])
    adapters = {"fake1": a1, "fake2": a2}
    monkeypatch.setattr(routes_discover, "_adapters_of", lambda request: dict(adapters))
    monkeypatch.setattr(routes_subscriptions, "_adapters_of", lambda request: dict(adapters))
    return adapters


# ── 渲染 ────────────────────────────────────────────────────

class TestRender:
    def test_empty_state_guides_to_sites(self, client):
        """无启用站点：引导卡指向 /sites。"""
        _setup_admin(client)
        html = client.get("/discover").text
        assert "还没有可用的站点" in html and 'href="/sites"' in html

    def test_render_with_data(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover").text
        # 同专辑跨站合一卡，代表取高音质（flac_24）
        assert "叶惠美" in html and "周杰伦" in html
        assert "FLAC 24bit" in html
        assert "fake1" in html and "fake2" in html
        assert "追踪歌手" in html and "仅此专辑" in html
        # 站点筛选 chips + 缓存元信息行
        assert "全部站点" in html and "2 个来源站" in html
        assert "有声书" not in html.split('class="discover-grid"')[1]  # music tab 不含有声书卡

    def test_audiobook_tab(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover?kind=audiobook").text
        assert "三体" in html and "刘慈欣" in html
        assert "追踪作者" in html and "仅此书" in html
        assert "叶惠美" not in html

    def test_site_filter(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover?kind=music&site=fake1").text
        # 跨站合并卡含 fake1 → 保留；fake2 独占的专辑被滤掉
        assert "叶惠美" in html
        assert "幸存者" not in html
        # 合卡上仍如实标注全部来源站
        assert "fake1、fake2" in html

    def test_bad_kind_falls_back_to_music(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover?kind=movie").text
        assert "叶惠美" in html

    def test_requires_login(self, client):
        resp = client.get("/discover", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers["location"]


# ── 一键订阅 ────────────────────────────────────────────────

class TestSubscribe:
    def test_artist_subscribe_creates_and_searches(self, client, fake_wall):
        _setup_admin(client)
        resp = _post(client, "/discover/subscribe", kind="music", sub_type="artist",
                     artist="周杰伦")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/discover?kind=music&ok=added_searched")
        entries = _all_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e.sub_type == SubType.ARTIST and e.artist == "周杰伦" and e.query == "周杰伦"
        assert e.kind == WatchKind.MUSIC
        # 立即搜索确实跑了
        searches = [q for a in fake_wall.values() for q, _ in a.search_calls]
        assert "周杰伦" in searches

    def test_album_subscribe(self, client, fake_wall):
        _setup_admin(client)
        resp = _post(client, "/discover/subscribe", kind="music", sub_type="album",
                     artist="周杰伦", album="叶惠美")
        assert resp.status_code == 303
        e = _all_entries()[0]
        assert e.sub_type == SubType.ALBUM
        assert e.artist == "周杰伦" and e.album == "叶惠美"
        assert e.query == "周杰伦 叶惠美"

    def test_audiobook_subscribe(self, client, fake_wall):
        """有声书：追踪作者（sub_type 推导为 AUDIOBOOK）与仅此书。"""
        _setup_admin(client)
        _post(client, "/discover/subscribe", kind="audiobook", sub_type="artist",
              artist="刘慈欣")
        _post(client, "/discover/subscribe", kind="audiobook", sub_type="album",
              artist="刘慈欣", album="三体")
        entries = {e.album or e.author: e for e in _all_entries()}
        author = entries["刘慈欣"]
        assert author.kind == WatchKind.AUDIOBOOK and author.sub_type == SubType.AUDIOBOOK
        assert author.author == "刘慈欣" and not author.album
        book = entries["三体"]
        assert book.sub_type == SubType.AUDIOBOOK and book.album == "三体"
        assert book.query == "刘慈欣 三体"

    def test_duplicate_not_created(self, client, fake_wall):
        _setup_admin(client)
        resp1 = _post(client, "/discover/subscribe", kind="music", sub_type="artist",
                      artist="周杰伦")
        assert "ok=" in resp1.headers["location"]
        resp2 = _post(client, "/discover/subscribe", kind="music", sub_type="artist",
                      artist="周杰伦")
        assert resp2.headers["location"] == "/discover?kind=music&err=exists"
        assert len(_all_entries()) == 1
        # 同名专辑型订阅也按 album_key 去重
        _post(client, "/discover/subscribe", kind="music", sub_type="album",
              artist="周杰伦", album="叶惠美")
        resp4 = _post(client, "/discover/subscribe", kind="music", sub_type="album",
                      artist="周杰伦", album="叶惠美")
        assert resp4.headers["location"] == "/discover?kind=music&err=exists"
        assert len(_all_entries()) == 2

    def test_empty_artist_rejected(self, client, fake_wall):
        _setup_admin(client)
        resp = _post(client, "/discover/subscribe", kind="music", sub_type="artist", artist=" ")
        assert resp.headers["location"] == "/discover?kind=music&err=empty"
        assert not _all_entries()

    def test_bad_params(self, client, fake_wall):
        _setup_admin(client)
        resp = _post(client, "/discover/subscribe", kind="movie", sub_type="artist", artist="x")
        assert resp.headers["location"].endswith("err=bad_kind")
        resp = _post(client, "/discover/subscribe", kind="music", sub_type="movie", artist="x")
        assert resp.headers["location"].endswith("err=bad_type")
        assert not _all_entries()


# ── 已订阅置灰 ──────────────────────────────────────────────

class TestSubscribedGrey:
    def test_artist_button_greyed(self, client, fake_wall):
        _setup_admin(client)
        _run(repo.add_watch_entry(app.state.db, WatchEntry(
            kind=WatchKind.MUSIC, sub_type=SubType.ARTIST, query="周杰伦", artist="周杰伦")))
        html = client.get("/discover").text
        assert "已订阅 ✓" in html
        # 歌手按钮置灰，专辑按钮仍可点
        assert "disabled" in html and "仅此专辑" in html

    def test_album_button_greyed(self, client, fake_wall):
        _setup_admin(client)
        _run(repo.add_watch_entry(app.state.db, WatchEntry(
            kind=WatchKind.MUSIC, sub_type=SubType.ALBUM,
            query="周杰伦 叶惠美", artist="周杰伦", album="叶惠美")))
        html = client.get("/discover").text
        assert "已订阅 ✓" in html
        # 仅置灰一次（专辑按钮），歌手按钮仍可订阅
        assert html.count("已订阅 ✓") == 1
        assert "追踪歌手" in html


# ── 刷新 ────────────────────────────────────────────────────

class TestRefresh:
    def test_refresh_redirects(self, client, fake_wall):
        _setup_admin(client)
        client.get("/discover")          # 先拉一次入缓存
        resp = _post(client, "/discover/refresh", kind="music")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/discover?kind=music&ok=refreshed"

    def test_csrf_required(self, client, fake_wall):
        _setup_admin(client)
        for path in ("/discover/refresh", "/discover/subscribe"):
            resp = client.post(path, data={"kind": "music", "sub_type": "artist",
                                           "artist": "周杰伦"}, follow_redirects=False)
            assert resp.status_code == 403, path


# ── 搜索模式（v0.7.4）──────────────────────────────────────

class TestSearch:
    def test_search_mode_music(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover?kind=music&q=叶惠美").text
        assert "搜索「叶惠美」" in html
        assert "叶惠美" in html and "周杰伦" in html
        # 走的是适配器 search，而非 RSS 缓存
        assert ("叶惠美", "music") in fake_wall["fake1"].search_calls
        # 搜索模式下有「清除」入口，没有「刷新」按钮
        assert "清除" in html

    def test_search_mode_audiobook(self, client, fake_wall):
        _setup_admin(client)
        html = client.get("/discover?kind=audiobook&q=三体").text
        assert "搜索「三体」" in html and "三体" in html
        assert ("三体", "audiobook") in fake_wall["fake2"].search_calls

    def test_search_no_result(self, client, monkeypatch):
        empty = {"e1": FakeAdapter("e1", [])}
        monkeypatch.setattr(routes_discover, "_adapters_of", lambda request: dict(empty))
        _setup_admin(client)
        html = client.get("/discover?kind=music&q=不存在的专辑").text
        assert "没有找到匹配" in html

    def test_rate_limited_shown_separately_from_failed(self, client, monkeypatch):
        """限速站（如 audiences 2 分钟间隔）不再被标红为「失败站」。"""
        from audio_hub.adapters.base import AdapterError, AdapterRateLimited

        class _Err(FakeAdapter):
            def __init__(self, site, err):
                super().__init__(site, [])
                self._err = err

            async def fetch_rss(self, query=None, kind=None):
                raise self._err

        adapters = {
            "ok": FakeAdapter("ok", [_torrent("周杰伦 - 叶惠美 [FLAC]", site="ok")]),
            "slowsite": _Err("slowsite", AdapterRateLimited("slowsite 站点限速：RSS 请求间隔受限")),
            "badsite": _Err("badsite", AdapterError("badsite HTTP 500")),
        }
        monkeypatch.setattr(routes_discover, "_adapters_of", lambda request: dict(adapters))
        _setup_admin(client)
        html = client.get("/discover").text
        assert "限速站: slowsite" in html
        assert "失败站: badsite" in html
        assert "叶惠美" in html                     # 正常站照常出卡


# ── 封面代理 ────────────────────────────────────────────────

class TestCoverProxy:
    def test_rejects_bad_key(self, client, web_env):
        """非法键（非 sha1 hex / 路径穿越 / 任意 URL）一律 404。"""
        for bad in ("../etc/passwd", "zz", "a" * 39, "https://evil.example/x", ""):
            resp = client.get(f"/covers/{bad}")
            assert resp.status_code == 404, bad

    def test_missing_cache_404(self, client, web_env):
        resp = client.get(f"/covers/{'a' * 40}")
        assert resp.status_code == 404

    def test_serves_cached_without_login(self, client, web_env):
        """已缓存的键免登录可读（img 标签带不了 CSRF，内容非敏感）。"""
        covers = web_env / "covers"
        covers.mkdir()
        key = cache_key("周杰伦", "叶惠美", "music")
        (covers / f"{key}.img").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        (covers / f"{key}.json").write_text(
            '{"hit": true, "content_type": "image/png"}', encoding="utf-8")
        resp = client.get(f"/covers/{key}")          # 未登录
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.content.startswith(b"\xff\xd8")
