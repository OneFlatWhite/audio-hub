"""订阅中心集成测试：列表渲染 / 新建各类型与校验 / 详情 / 编辑 / 立即搜索 / 暂停恢复 / 删除 / CSRF。

fixture 与 test_web_routes.py 同构（独立复制，避免跨文件 import 测试模块）。
主动搜索用 FakeAdapter 替换 routes_subscriptions._adapters_of，不触网。
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
from audio_hub.config import SiteConfig
from audio_hub.main import app
from audio_hub.models import (
    AuthStatus,
    FoundVia,
    HitDecision,
    SubscriptionHit,
    SubStatus,
    SubType,
    Task,
    TaskState,
    WatchEntry,
    WatchKind,
    utcnow,
)
from audio_hub.subscription import engine
from audio_hub.web import auth, routes_subscriptions


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


def _login_as_viewer(client: TestClient, username="viewer", password="strongpass123") -> None:
    async def insert():
        await app.state.db.execute(
            """INSERT INTO users (id, username, password_hash, is_admin, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), username, auth.hash_password(password), 0,
             auth.iso_utc(), auth.iso_utc()),
        )
        await app.state.db.commit()

    asyncio.run(insert())
    client.post("/logout", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text[:200]


def _run(coro):
    return asyncio.run(coro)


def _add_watch(**kw) -> WatchEntry:
    base = {"query": "周杰伦", "kind": WatchKind.MUSIC, "sub_type": SubType.ARTIST, "artist": "周杰伦"}
    base.update(kw)
    e = WatchEntry(**base)
    _run(repo.add_watch_entry(app.state.db, e))
    return _run(repo.get_watch_entry(app.state.db, e.id))


def _get_watch(watch_id: str) -> WatchEntry | None:
    return _run(repo.get_watch_entry(app.state.db, watch_id))


def _find_watch(query: str) -> WatchEntry:
    entries = _run(repo.list_watch_entries(app.state.db, enabled_only=False))
    return next(e for e in entries if e.query == query)


def _all_tasks() -> list[dict]:
    async def q():
        async with app.state.db.execute("SELECT * FROM tasks") as cur:
            return [dict(r) for r in await cur.fetchall()]
    return _run(q())


def _post(client: TestClient, path: str, **data):
    data.setdefault("csrf_token", _csrf(client))
    return client.post(path, data=data, follow_redirects=False)


def _add_valid_site(name: str, *, enabled: bool = True, valid: bool = True) -> str:
    sid = _run(repo.upsert_site(app.state.db, SiteConfig(name=name, enabled=enabled)))
    _run(repo.set_site_auth(app.state.db, sid,
                            AuthStatus.VALID if valid else AuthStatus.EXPIRED))
    return sid


def _torrent(title, *, site="fake", size_gb=1.0, seeders=10):
    return NormalizedTorrent(
        info_hash=uuid.uuid4().hex + "00000000", title=title, title_normalized=title.lower(),
        site=site, category=MediaCategory.MUSIC,
        download_url=f"https://{site}/dl/{abs(hash(title))}",
        size_bytes=int(size_gb * 1024 ** 3), discount=DiscountType.NORMAL, seeders=seeders,
    )


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
        return []


@pytest.fixture
def fake_adapter(monkeypatch):
    """把 routes_subscriptions 用的适配器集合换成一个会命中「周杰伦」的替身站点。"""
    ad = FakeAdapter("fake", [_torrent("周杰伦 - 叶惠美 (2003) [FLAC]"),
                              _torrent("周杰伦 - 范特西 [FLAC 24-96]")])
    monkeypatch.setattr(routes_subscriptions, "_adapters_of", lambda request: {"fake": ad})
    return ad


# ── 渲染 ────────────────────────────────────────────────────

class TestWatchlistRender:
    def test_empty_page_guides_to_sites(self, client):
        _setup_admin(client)
        html = client.get("/watchlist").text
        assert "新建订阅" in html and "还没有订阅" in html
        # 没有已启用且认证通过的站点 → 表单顶部引导到站点中心
        assert 'href="/sites"' in html and "认证通过" in html
        # 类型下拉含四种订阅类型
        for val in ("artist", "album", "audiobook", "keyword"):
            assert f'value="{val}"' in html

    def test_guidance_disappears_with_valid_site_and_downloader(self, client):
        _setup_admin(client)
        _add_valid_site("fake")
        html = client.get("/watchlist").text
        assert "认证通过</b>的站点" not in html
        # 站点多选只列 valid 站点
        assert 'name="sites" type="checkbox" value="fake"' in html
        assert 'value="hhanclub"' not in html

    def test_list_columns(self, client):
        _setup_admin(client)
        e = _add_watch(query="周杰伦", aliases=["Jay Chou"], preferred_quality="hires_only",
                       free_only=True, min_seeders=3, exclude_keywords=["Live"],
                       upgrade_enabled=True)
        _add_watch(query="三体", kind=WatchKind.AUDIOBOOK, sub_type=SubType.AUDIOBOOK,
                   author="刘慈欣", artist="", status=SubStatus.PAUSED)
        _add_watch(query="叶惠美", sub_type=SubType.ALBUM, album="叶惠美",
                   status=SubStatus.COMPLETED)
        html = client.get("/watchlist").text
        # 别名行已从列表精简（别名在详情页才能看到）；
        # 注意不能整页断言 "Jay Chou" not in html —— 新建表单的 placeholder 里有它
        assert "周杰伦" in html
        assert f'href="/watchlist/{e.id}"' in html
        # 类型徽标与状态
        for word in ("歌手", "专辑", "有声书", "进行中", "已暂停", "已完成", "刘慈欣"):
            assert word in html, word
        # 过滤摘要
        for word in ("Hi-Res", "仅免费", "做种≥3", "排:Live", "洗版"):
            assert word in html, word
        # 暂停 / 恢复按钮按状态切换
        assert "重新追踪" in html and ">恢复<" in html and ">暂停<" in html

    def test_audiobook_row_series_links_once(self, client):
        _setup_admin(client)
        e = _add_watch(query="鬼吹灯", kind=WatchKind.AUDIOBOOK, sub_type=SubType.AUDIOBOOK,
                       artist="")
        html = client.get("/watchlist").text
        assert html.count("建立连载") == 1 and f'href="/series?watch_id={e.id}"' in html
        assert "查看连载" not in html

    def test_requires_login(self, client):
        resp = client.get("/watchlist", follow_redirects=False)
        assert resp.status_code in (302, 303)


# ── 新建 ────────────────────────────────────────────────────

class TestCreate:
    def test_artist_default(self, client):
        _setup_admin(client)
        resp = _post(client, "/watchlist", sub_type="artist", query="周杰伦",
                     aliases="Jay Chou, 周杰倫", search_interval_hours="12")
        assert resp.status_code == 303
        # 无可用站点：明确提示而不是静默
        assert resp.headers["location"] == "/watchlist?ok=added_no_sites"
        e = _find_watch("周杰伦")
        assert e.sub_type == SubType.ARTIST and e.artist == "周杰伦"
        assert e.kind == WatchKind.MUSIC
        assert e.aliases == ["Jay Chou", "周杰倫"]
        assert e.search_interval_hours == 12
        assert e.auto_download is False  # 复选框未提交 = 未勾选
        assert e.status == SubStatus.ACTIVE

    def test_album(self, client):
        _setup_admin(client)
        _post(client, "/watchlist", sub_type="album", artist="周杰伦", query="叶惠美",
              auto_download="true", upgrade_enabled="true")
        e = _find_watch("叶惠美")
        assert e.sub_type == SubType.ALBUM
        assert e.artist == "周杰伦" and e.album == "叶惠美"
        assert e.auto_download is True and e.upgrade_enabled is True

    def test_audiobook_with_series(self, client):
        _setup_admin(client)
        _post(client, "/watchlist", sub_type="audiobook", query="鬼吹灯", author="天下霸唱",
              series="true")
        e = _find_watch("鬼吹灯")
        assert e.sub_type == SubType.AUDIOBOOK and e.kind == WatchKind.AUDIOBOOK
        assert e.author == "天下霸唱"
        assert e.series_id is not None
        series = _run(repo.get_series(app.state.db, e.series_id))
        assert series is not None and series.watch_id == e.id and series.name == "鬼吹灯"
        html = client.get("/watchlist").text
        assert html.count("查看连载") == 1 and f'href="/series/{e.series_id}"' in html

    def test_audiobook_without_series(self, client):
        _setup_admin(client)
        _post(client, "/watchlist", sub_type="audiobook", query="三体")
        assert _find_watch("三体").series_id is None

    def test_legacy_form_is_keyword(self, client):
        """旧表单只给 query（+kind）：按关键词订阅处理，kind 沿用。"""
        _setup_admin(client)
        _post(client, "/watchlist", query="林俊杰")
        e = _find_watch("林俊杰")
        assert e.sub_type == SubType.KEYWORD and e.kind == WatchKind.MUSIC
        _post(client, "/watchlist", query="盗墓笔记", kind="audiobook")
        e2 = _find_watch("盗墓笔记")
        assert e2.sub_type == SubType.AUDIOBOOK and e2.kind == WatchKind.AUDIOBOOK

    def test_advanced_filters(self, client):
        _setup_admin(client)
        _post(client, "/watchlist", sub_type="artist", query="五月天",
              preferred_quality="lossless_only", min_size_mb="300", max_size_mb="5000",
              min_seeders="2", free_only="true", include_keywords="FLAC, WAV",
              exclude_keywords="Live，伴奏", regex="五月天.*FLAC", qb_category="music",
              qb_tags="a, b", save_path="/x", priority="-2", min_confidence="medium")
        e = _find_watch("五月天")
        assert e.preferred_quality == "lossless_only"
        assert (e.min_size_mb, e.max_size_mb, e.min_seeders) == (300, 5000, 2)
        assert e.free_only is True
        assert e.include_keywords == ["FLAC", "WAV"]
        assert e.exclude_keywords == ["Live", "伴奏"]  # 中文逗号也能分
        assert e.regex == "五月天.*FLAC"
        assert e.qb_category == "music" and e.qb_tags == ["a", "b"] and e.save_path == "/x"
        assert e.priority == -2 and e.min_confidence == "medium"

    def test_sites_only_valid_kept(self, client):
        _setup_admin(client)
        _add_valid_site("good")
        _add_valid_site("bad", valid=False)
        token = _csrf(client)
        client.post("/watchlist", data={
            "sub_type": "artist", "query": "陈奕迅", "csrf_token": token,
            "sites": ["good", "bad", "hhanclub"],
        }, follow_redirects=False)
        assert _find_watch("陈奕迅").sites == ["good"]

    def test_downloader_choice(self, client):
        _setup_admin(client)
        dls = _run(repo.list_downloaders(app.state.db, enabled_only=True))
        if not dls:
            pytest.skip("环境未从 yaml 播种默认下载器")
        _post(client, "/watchlist", sub_type="artist", query="孙燕姿", downloader_id=dls[0].id)
        assert _find_watch("孙燕姿").downloader_id == dls[0].id
        resp = _post(client, "/watchlist", sub_type="artist", query="梁静茹",
                     downloader_id="nope")
        assert resp.headers["location"] == "/watchlist?err=bad_downloader"

    @pytest.mark.parametrize("data,err", [
        ({"sub_type": "artist", "query": "  "}, "empty"),
        ({"sub_type": "movie", "query": "x"}, "bad_type"),
        ({"sub_type": "artist", "query": "x", "search_interval_hours": "0"}, "bad_interval"),
        ({"sub_type": "artist", "query": "x", "search_interval_hours": "abc"}, "bad_interval"),
        ({"sub_type": "artist", "query": "x", "min_size_mb": "900", "max_size_mb": "100"}, "bad_size"),
        ({"sub_type": "artist", "query": "x", "min_size_mb": "-1"}, "bad_size"),
        ({"sub_type": "artist", "query": "x", "min_seeders": "x"}, "bad_seeders"),
        ({"sub_type": "artist", "query": "x", "regex": "(["}, "bad_regex"),
        ({"sub_type": "artist", "query": "x", "preferred_quality": "mp3"}, "bad_quality"),
    ])
    def test_validation(self, client, data, err):
        _setup_admin(client)
        resp = _post(client, "/watchlist", **data)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/watchlist?err={err}"
        assert not _run(repo.list_watch_entries(app.state.db, enabled_only=False))

    def test_error_flash_rendered(self, client):
        _setup_admin(client)
        html = client.get("/watchlist?err=bad_interval").text
        assert "搜索间隔" in html

    def test_create_triggers_immediate_search(self, client, fake_adapter):
        _setup_admin(client)
        resp = _post(client, "/watchlist", sub_type="artist", query="周杰伦",
                     aliases="Jay Chou", auto_download="true")
        assert resp.headers["location"] == "/watchlist?ok=added_searched"
        # 歌手名 + 别名各搜一次
        assert [q for q, _ in fake_adapter.search_calls] == ["周杰伦", "Jay Chou"]
        e = _find_watch("周杰伦")
        assert e.last_search_at is not None and e.next_search_at is not None
        hits = _run(repo.list_hits(app.state.db, e.id))
        assert len(hits) == 2
        assert {h.decision for h in hits} == {HitDecision.DISPATCHED}
        assert e.hit_count == 2
        # dry_run：任务已建但不进待审
        tasks = _all_tasks()
        assert len(tasks) == 2
        assert all(t["state"] == TaskState.SCORED.value and t["reject_reason"] == engine.DRY_RUN_REASON
                   for t in tasks)
        html = client.get(resp.headers["location"]).text
        assert "完成首次搜索" in html


# ── 详情 ────────────────────────────────────────────────────

class TestDetail:
    def test_notfound(self, client):
        _setup_admin(client)
        resp = client.get("/watchlist/nope", follow_redirects=False)
        assert resp.headers["location"] == "/watchlist?err=notfound"

    def test_detail_renders_hits_and_owned(self, client):
        _setup_admin(client)
        e = _add_watch(query="周杰伦", aliases=["Jay Chou"], free_only=True)
        for title, dec, key, q in [
            ("周杰伦 - 叶惠美 [FLAC]", HitDecision.DISPATCHED, "周杰伦|叶惠美", "flac_16"),
            ("周杰伦 - 叶惠美 [MP3]", HitDecision.SKIPPED_DUP, "周杰伦|叶惠美", "mp3"),
            ("周杰伦 - 范特西 [WAV]", HitDecision.REVIEW, "周杰伦|范特西", "wav"),
            ("周杰伦 演唱会 Live", HitDecision.REJECTED, "", None),
        ]:
            _run(repo.add_hit(app.state.db, SubscriptionHit(
                watch_id=e.id, site="hhanclub", torrent_title=title, info_hash=uuid.uuid4().hex,
                album_key=key, quality_tag=q, decision=dec, found_via=FoundVia.SEARCH,
                size_bytes=700 * 1024 ** 2, seeders=5, score=80,
                reject_reason="outranked" if dec == HitDecision.SKIPPED_DUP else None,
            )))
        html = client.get(f"/watchlist/{e.id}").text
        assert "订阅详情" in html and "周杰伦" in html and "Jay Chou" in html
        for title in ("叶惠美 [FLAC]", "叶惠美 [MP3]", "范特西 [WAV]", "演唱会 Live"):
            assert title in html
        # 决策筛选链接与计数
        assert f'href="/watchlist/{e.id}?decision=review"' in html
        assert "命中记录（4）" in html
        # 已获得专辑：只有派发过的那一张
        assert "已获得专辑（1）" in html and "周杰伦|叶惠美" in html
        assert "周杰伦|范特西" in html  # 命中记录里出现
        # 编辑表单预填 + 操作按钮
        assert 'value="周杰伦"' in html and "保存修改" in html
        assert f'action="/watchlist/{e.id}/search-now"' in html and "立即搜索" in html
        assert f'action="/watchlist/{e.id}/pause"' in html
        assert f'action="/watchlist/{e.id}/delete"' in html
        assert 'name="free_only" type="checkbox" value="true" checked' in html

    def test_detail_decision_filter(self, client):
        _setup_admin(client)
        e = _add_watch()
        for title, dec in [("A [FLAC]", HitDecision.DISPATCHED), ("B [FLAC]", HitDecision.REVIEW)]:
            _run(repo.add_hit(app.state.db, SubscriptionHit(
                watch_id=e.id, site="hhanclub", torrent_title=title, info_hash=uuid.uuid4().hex,
                album_key="", decision=dec,  # 空键：不进「已获得专辑」，只看命中表
            )))
        html = client.get(f"/watchlist/{e.id}?decision=review").text
        assert "B [FLAC]" in html and "A [FLAC]" not in html
        assert "命中记录（2）" in html  # 计数仍是全部
        html_all = client.get(f"/watchlist/{e.id}?decision=bogus").text
        assert "A [FLAC]" in html_all and "B [FLAC]" in html_all

    def test_detail_audiobook_series_link(self, client):
        _setup_admin(client)
        e = _add_watch(query="鬼吹灯", kind=WatchKind.AUDIOBOOK, sub_type=SubType.AUDIOBOOK,
                       artist="")
        html = client.get(f"/watchlist/{e.id}").text
        assert f'href="/series?watch_id={e.id}"' in html


# ── 编辑 ────────────────────────────────────────────────────

class TestEdit:
    def test_edit_keeps_runtime_fields(self, client):
        _setup_admin(client)
        e = _add_watch(query="周杰伦")
        _run(repo.incr_hit(app.state.db, e.id))
        resp = _post(client, f"/watchlist/{e.id}", sub_type="artist", query="周杰倫",
                     aliases="Jay", min_seeders="4", preferred_quality="hires_only",
                     search_interval_hours="24", auto_download="true")
        assert resp.headers["location"] == f"/watchlist/{e.id}?ok=updated"
        e2 = _get_watch(e.id)
        assert e2.query == "周杰倫" and e2.artist == "周杰倫"
        assert e2.aliases == ["Jay"] and e2.min_seeders == 4
        assert e2.preferred_quality == "hires_only" and e2.search_interval_hours == 24
        assert e2.hit_count == 1  # 运行态列不被表单覆盖
        assert e2.status == SubStatus.ACTIVE and e2.created_at == e.created_at

    def test_edit_type_defaults_to_existing(self, client):
        """编辑表单没带 sub_type（旧客户端）→ 沿用原类型。"""
        _setup_admin(client)
        e = _add_watch(query="三体", kind=WatchKind.AUDIOBOOK, sub_type=SubType.AUDIOBOOK,
                       artist="")
        _post(client, f"/watchlist/{e.id}", query="三体 全集")
        e2 = _get_watch(e.id)
        assert e2.sub_type == SubType.AUDIOBOOK and e2.query == "三体 全集"

    def test_edit_validation_redirects_to_detail(self, client):
        _setup_admin(client)
        e = _add_watch()
        resp = _post(client, f"/watchlist/{e.id}", sub_type="artist", query="x",
                     search_interval_hours="0")
        assert resp.headers["location"] == f"/watchlist/{e.id}?err=bad_interval"
        assert _get_watch(e.id).query == "周杰伦"

    def test_edit_notfound(self, client):
        _setup_admin(client)
        resp = _post(client, "/watchlist/nope", sub_type="artist", query="x")
        assert resp.headers["location"] == "/watchlist?err=notfound"


# ── 立即搜索 / 暂停恢复 / 开关 / 删除 ──────────────────────

class TestActions:
    def test_search_now_no_sites(self, client):
        _setup_admin(client)
        e = _add_watch()
        resp = _post(client, f"/watchlist/{e.id}/search-now")
        assert resp.headers["location"] == "/watchlist?ok=search_no_sites"
        assert _get_watch(e.id).last_search_at is None

    def test_search_now_from_detail_returns_to_detail(self, client, fake_adapter):
        _setup_admin(client)
        e = _add_watch(auto_download=False)
        resp = client.post(f"/watchlist/{e.id}/search-now", data={"csrf_token": _csrf(client)},
                           headers={"referer": f"http://testserver/watchlist/{e.id}"},
                           follow_redirects=False)
        assert resp.headers["location"] == f"/watchlist/{e.id}?ok=searched"
        e2 = _get_watch(e.id)
        assert e2.last_search_at is not None
        hits = _run(repo.list_hits(app.state.db, e.id))
        assert hits and all(h.decision == HitDecision.REVIEW for h in hits)
        # 待审项出现在待审队列
        assert "叶惠美" in client.get("/review").text

    def test_search_now_ignores_schedule(self, client, fake_adapter):
        """只对该订阅搜索，且无视 next_search_at 是否到点。"""
        _setup_admin(client)
        e = _add_watch()
        other = _add_watch(query="林俊杰", artist="林俊杰")
        _run(repo.mark_searched(app.state.db, e.id, utcnow(), interval_hours=6))
        _post(client, f"/watchlist/{e.id}/search-now")
        assert fake_adapter.search_calls and all(q == "周杰伦" for q, _ in fake_adapter.search_calls)
        assert _get_watch(other.id).last_search_at is None

    def test_search_now_notfound(self, client):
        _setup_admin(client)
        resp = _post(client, "/watchlist/nope/search-now")
        assert resp.headers["location"] == "/watchlist?err=notfound"

    def test_search_now_failure_is_flash_not_500(self, client, monkeypatch):
        _setup_admin(client)
        e = _add_watch()

        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(routes_subscriptions, "run_subscription_search_once", boom)
        resp = _post(client, f"/watchlist/{e.id}/search-now")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/watchlist?err=search_failed"

    def test_pause_resume(self, client):
        _setup_admin(client)
        e = _add_watch()
        resp = _post(client, f"/watchlist/{e.id}/pause")
        assert resp.headers["location"] == "/watchlist?ok=paused"
        assert _get_watch(e.id).status == SubStatus.PAUSED
        resp = _post(client, f"/watchlist/{e.id}/resume")
        assert resp.headers["location"] == "/watchlist?ok=resumed"
        assert _get_watch(e.id).status == SubStatus.ACTIVE

    def test_resume_completed_reactivates_and_enables(self, client):
        _setup_admin(client)
        e = _add_watch(status=SubStatus.COMPLETED, enabled=False)
        _post(client, f"/watchlist/{e.id}/resume")
        e2 = _get_watch(e.id)
        assert e2.status == SubStatus.ACTIVE and e2.enabled is True

    def test_pause_notfound(self, client):
        _setup_admin(client)
        assert _post(client, "/watchlist/nope/pause").headers["location"] == "/watchlist?err=notfound"
        assert _post(client, "/watchlist/nope/resume").headers["location"] == "/watchlist?err=notfound"

    def test_toggle(self, client):
        _setup_admin(client)
        e = _add_watch()
        _post(client, f"/watchlist/{e.id}/toggle")
        assert _get_watch(e.id).enabled is False
        _post(client, f"/watchlist/{e.id}/toggle")
        assert _get_watch(e.id).enabled is True

    def test_delete_with_hits_and_tasks(self, client):
        """有历史任务（外键引用 watchlist）与命中流水时也能删；任务保留、流水清除。"""
        _setup_admin(client)
        e = _add_watch()
        tid = str(uuid.uuid4())
        _run(repo.create_task(app.state.db, Task(
            id=tid, info_hash=uuid.uuid4().hex, site="hhanclub", title="t",
            state=TaskState.SCORED, watch_entry_id=e.id, watch_id=e.id,
        )))
        _run(repo.add_hit(app.state.db, SubscriptionHit(
            watch_id=e.id, site="hhanclub", torrent_title="t", info_hash="h" * 40,
            decision=HitDecision.DISPATCHED, task_id=tid,
        )))
        resp = _post(client, f"/watchlist/{e.id}/delete")
        assert resp.headers["location"] == "/watchlist?ok=deleted"
        assert _get_watch(e.id) is None
        assert _run(repo.list_hits(app.state.db, e.id)) == []
        assert _run(repo.get_task(app.state.db, tid)) is not None

    def test_delete_notfound(self, client):
        _setup_admin(client)
        assert _post(client, "/watchlist/nope/delete").headers["location"] == "/watchlist?err=notfound"

    def test_delete_requires_admin(self, client):
        _setup_admin(client)
        e = _add_watch()
        _login_as_viewer(client)
        resp = _post(client, f"/watchlist/{e.id}/delete")
        assert resp.status_code == 403
        assert _get_watch(e.id) is not None
        # 非管理员仍可暂停 / 搜索 / 编辑
        assert _post(client, f"/watchlist/{e.id}/pause").status_code == 303
        html = client.get("/watchlist").text
        assert f'action="/watchlist/{e.id}/delete"' not in html

    def test_csrf_required(self, client):
        _setup_admin(client)
        e = _add_watch()
        for path in (f"/watchlist/{e.id}/search-now", f"/watchlist/{e.id}/pause",
                     f"/watchlist/{e.id}/delete", f"/watchlist/{e.id}"):
            resp = client.post(path, data={"query": "x"}, follow_redirects=False)
            assert resp.status_code == 403, path
