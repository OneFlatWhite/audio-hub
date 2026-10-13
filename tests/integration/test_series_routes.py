"""连载页集成测试：渲染 / 建立 / 编辑 / 删除 / CSRF / watchlist 入口。

fixture 与 test_web_routes.py 同构（独立复制，避免跨文件 import 测试模块）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_hub import repo
from audio_hub.main import app
from audio_hub.models import PartEntry, SeriesEntry, WatchEntry, WatchKind
from audio_hub.web import auth


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


def _add_watch(query: str, kind: WatchKind = WatchKind.AUDIOBOOK, **kw) -> str:
    return asyncio.run(repo.add_watch_entry(app.state.db, WatchEntry(query=query, kind=kind, **kw)))


def _login_as_viewer(client: TestClient, username="viewer", password="strongpass123") -> None:
    """直接插库造一个非管理员，在同一 client 上登出管理员再以它登入。"""
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


def _get_watch(watch_id: str) -> WatchEntry | None:
    return asyncio.run(repo.get_watch_entry(app.state.db, watch_id))


def _get_series(sid: int) -> SeriesEntry | None:
    return asyncio.run(repo.get_series(app.state.db, sid))


def _create_series(client: TestClient, token: str, watch_id: str, **data) -> int:
    resp = client.post("/series", data={"watch_id": watch_id, "csrf_token": token, **data},
                       follow_redirects=False)
    assert resp.status_code == 303, resp.text
    loc = resp.headers["location"]
    m = re.match(r"/series/(\d+)\?ok=created", loc)
    assert m, f"建立后应跳转详情页，实际 {loc}"
    return int(m.group(1))


# ── 渲染 ────────────────────────────────────────────────────

class TestSeriesRender:
    def test_empty_page(self, client):
        _setup_admin(client)
        html = client.get("/series").text
        assert "连载追踪" in html and "尚无连载" in html
        assert "建立连载" in html
        # 无有声书订阅 → 提示去添加，建立按钮禁用
        assert "还没有有声书订阅" in html

    def test_list_renders_progress_and_watch_name(self, client):
        _setup_admin(client)
        wid = _add_watch("三体")
        db = app.state.db

        async def seed():
            sid = await repo.add_series(db, SeriesEntry(watch_id=wid, name="三体", total_parts=3))
            await repo.set_watch_series(db, wid, sid)
            await repo.add_series_part(db, PartEntry(series_id=sid, part_no=1, title="三体 第1部"))
            await repo.add_series_part(db, PartEntry(series_id=sid, part_no=2, title="三体 第2部"))
            await repo.add_series(db, SeriesEntry(name="未设总数"))
            return sid

        sid = asyncio.run(seed())
        html = client.get("/series").text
        assert f'href="/series/{sid}"' in html
        assert "2 / 3" in html                 # last_part / total_parts
        assert "width:67%" in html             # 2/3 进度条
        assert "0 / 未设定" in html             # total_parts 为空
        assert "<td class=\"num\">2</td>" in html   # 已登记数
        # 订阅项列显示订阅词而非 id
        assert "三体" in html

    def test_status_filter(self, client):
        _setup_admin(client)
        db = app.state.db

        async def seed():
            await repo.add_series(db, SeriesEntry(name="进行中", status="active"))
            await repo.add_series(db, SeriesEntry(name="已完", status="completed"))

        asyncio.run(seed())
        html = client.get("/series?status=completed").text
        assert "已完" in html and "进行中" not in html

    def test_detail_page_shows_parts_shelf_and_form(self, client):
        _setup_admin(client)
        db = app.state.db

        async def seed():
            sid = await repo.add_series(db, SeriesEntry(name="鬼吹灯", total_parts=4,
                                                        part_pattern=r"vol\.(\d+)"))
            await repo.add_series_part(db, PartEntry(series_id=sid, part_no=1, task_id="task-aaaa1111",
                                                     title="鬼吹灯 vol.1"))
            await repo.add_series_part(db, PartEntry(series_id=sid, part_no=3, title="鬼吹灯 vol.3"))
            return sid

        sid = asyncio.run(seed())
        html = client.get(f"/series/{sid}").text
        assert 'id="series-detail"' in html
        assert "鬼吹灯 vol.1" in html and "鬼吹灯 vol.3" in html
        assert "task-aaa" in html                       # 任务 id 前 8 位
        # 书脊：1、3 亮起，4 为下一部（last_part=3 → next=4）
        assert '<b class="got">1</b>' in html
        assert '<b class="">2</b>' in html
        assert '<b class="got">3</b>' in html
        assert '<b class="next">4</b>' in html
        # 编辑表单回填
        assert f'action="/series/{sid}/edit"' in html
        assert 'value="鬼吹灯"' in html
        assert r"vol\.(\d+)" in html
        assert 'value="4"' in html

    def test_detail_unknown_total_uses_bar(self, client):
        _setup_admin(client)
        sid = asyncio.run(repo.add_series(app.state.db, SeriesEntry(name="S")))
        html = client.get(f"/series/{sid}").text
        assert "未设定" in html and 'class="shelf"' not in html

    def test_detail_missing_redirects(self, client):
        _setup_admin(client)
        resp = client.get("/series/9999", follow_redirects=False)
        assert resp.status_code == 303 and "err=notfound" in resp.headers["location"]
        assert "连载不存在" in client.get("/series?err=notfound").text

    def test_prefill_from_watch_id(self, client):
        _setup_admin(client)
        wid = _add_watch("明朝那些事儿")
        _add_watch("周杰伦", kind=WatchKind.MUSIC)     # 音乐订阅不该出现在下拉里
        html = client.get(f"/series?watch_id={wid}").text
        assert f'value="{wid}" selected' in html
        assert 'value="明朝那些事儿"' in html
        assert "周杰伦" not in html

    def test_anonymous_redirected(self, client):
        _setup_admin(client)
        with TestClient(app) as anon:
            for path in ("/series", "/series/1"):
                resp = anon.get(path, follow_redirects=False)
                assert resp.status_code == 303 and "/login" in resp.headers["location"]


# ── 建立 ────────────────────────────────────────────────────

class TestSeriesCreate:
    def test_create_binds_watch(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("三体")
        sid = _create_series(client, token, wid, name="三体 三部曲", total_parts="3")

        s = _get_series(sid)
        assert s.name == "三体 三部曲" and s.total_parts == 3 and s.part_pattern is None
        assert s.watch_id == wid and s.status == "active"
        assert _get_watch(wid).series_id == sid
        html = client.get(f"/series/{sid}?ok=created").text
        assert "连载已建立" in html

    def test_create_defaults_name_to_query_and_blank_total(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("盗墓笔记")
        sid = _create_series(client, token, wid, name="   ", total_parts="")
        s = _get_series(sid)
        assert s.name == "盗墓笔记" and s.total_parts is None

    def test_create_with_custom_pattern(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("凡人修仙传")
        sid = _create_series(client, token, wid, part_pattern=r"vol\.(\d+)")
        assert _get_series(sid).part_pattern == r"vol\.(\d+)"

    def test_create_default_pattern_text_stored_as_null(self, client):
        from audio_hub.series.tracker import DEFAULT_PART_PATTERN
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("S")
        sid = _create_series(client, token, wid, part_pattern=DEFAULT_PART_PATTERN)
        assert _get_series(sid).part_pattern is None

    @pytest.mark.parametrize("data,err", [
        ({"total_parts": "abc"}, "bad_total"),
        ({"total_parts": "0"}, "bad_total"),
        ({"part_pattern": "第(\\d+"}, "bad_pattern"),
        ({"part_pattern": "\\d+"}, "bad_pattern"),       # 无捕获组
    ])
    def test_create_validation_errors(self, client, data, err):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("S")
        resp = client.post("/series", data={"watch_id": wid, "csrf_token": token, **data},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert f"err={err}" in resp.headers["location"]
        assert f"watch_id={wid}" in resp.headers["location"]   # 保留预填
        assert asyncio.run(repo.count_series(app.state.db)) == 0

    def test_create_rejects_music_or_missing_watch(self, client):
        _setup_admin(client)
        token = _csrf(client)
        music = _add_watch("周杰伦", kind=WatchKind.MUSIC)
        for wid in (music, "nope", ""):
            resp = client.post("/series", data={"watch_id": wid, "csrf_token": token},
                               follow_redirects=False)
            assert resp.status_code == 303
            assert "err=bad_watch" in resp.headers["location"]
        assert "请选择一个有声书订阅项" in client.get("/series?err=bad_watch").text

    def test_create_rejects_already_bound_watch(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("三体")
        first = _create_series(client, token, wid)
        resp = client.post("/series", data={"watch_id": wid, "csrf_token": token},
                           follow_redirects=False)
        assert "err=watch_bound" in resp.headers["location"]
        assert _get_watch(wid).series_id == first

    def test_create_requires_csrf(self, client):
        _setup_admin(client)
        wid = _add_watch("三体")
        resp = client.post("/series", data={"watch_id": wid}, follow_redirects=False)
        assert resp.status_code == 403
        resp = client.post("/series", data={"watch_id": wid, "csrf_token": "bogus"},
                           follow_redirects=False)
        assert resp.status_code == 403


# ── 编辑 ────────────────────────────────────────────────────

class TestSeriesEdit:
    def test_edit_updates_fields_and_refreshes(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("三体")
        sid = _create_series(client, token, wid, total_parts="5")
        asyncio.run(repo.add_series_part(app.state.db, PartEntry(series_id=sid, part_no=1)))
        asyncio.run(repo.add_series_part(app.state.db, PartEntry(series_id=sid, part_no=2)))

        # 总部数改为 2 → 刚好集齐 → refresh_status 判完结
        resp = client.post(f"/series/{sid}/edit", data={
            "name": "三体（新译）", "total_parts": "2", "part_pattern": r"第(\d+)部",
            "status": "active", "csrf_token": token,
        }, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/series/{sid}?ok=updated"
        s = _get_series(sid)
        assert s.name == "三体（新译）" and s.total_parts == 2
        assert s.part_pattern == r"第(\d+)部"
        assert s.status == "completed" and s.last_part == 2
        assert "连载已更新" in client.get(resp.headers["location"]).text

    def test_edit_pause_and_resume(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("S")
        sid = _create_series(client, token, wid)
        client.post(f"/series/{sid}/edit", data={"name": "S", "status": "paused",
                                                 "csrf_token": token}, follow_redirects=False)
        assert _get_series(sid).status == "paused"
        client.post(f"/series/{sid}/edit", data={"name": "S", "status": "active",
                                                 "csrf_token": token}, follow_redirects=False)
        assert _get_series(sid).status == "active"

    def test_edit_raising_total_reopens_completed(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("S")
        sid = _create_series(client, token, wid, total_parts="1")
        asyncio.run(repo.add_series_part(app.state.db, PartEntry(series_id=sid, part_no=1)))
        client.post(f"/series/{sid}/edit", data={"name": "S", "total_parts": "1",
                                                 "status": "active", "csrf_token": token},
                    follow_redirects=False)
        assert _get_series(sid).status == "completed"
        client.post(f"/series/{sid}/edit", data={"name": "S", "total_parts": "3",
                                                 "status": "active", "csrf_token": token},
                    follow_redirects=False)
        assert _get_series(sid).status == "active"

    @pytest.mark.parametrize("data,err", [
        ({"name": "  "}, "empty_name"),
        ({"name": "S", "total_parts": "-1"}, "bad_total"),
        ({"name": "S", "part_pattern": "("}, "bad_pattern"),
        ({"name": "S", "status": "completed"}, "bad_status"),
        ({"name": "S", "status": "bogus"}, "bad_status"),
    ])
    def test_edit_validation_errors(self, client, data, err):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("S")
        sid = _create_series(client, token, wid)
        resp = client.post(f"/series/{sid}/edit", data={"csrf_token": token, **data},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/series/{sid}?err={err}"
        assert _get_series(sid).name == "S"          # 未改动

    def test_edit_missing_series(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/series/4242/edit", data={"name": "x", "csrf_token": token},
                           follow_redirects=False)
        assert "err=notfound" in resp.headers["location"]

    def test_edit_requires_csrf(self, client):
        _setup_admin(client)
        token = _csrf(client)
        sid = _create_series(client, token, _add_watch("S"))
        resp = client.post(f"/series/{sid}/edit", data={"name": "X"}, follow_redirects=False)
        assert resp.status_code == 403
        assert _get_series(sid).name == "S"


# ── 删除 ────────────────────────────────────────────────────

class TestSeriesDelete:
    def test_delete_removes_parts_and_unbinds_watch(self, client):
        _setup_admin(client)
        token = _csrf(client)
        wid = _add_watch("三体")
        sid = _create_series(client, token, wid)
        asyncio.run(repo.add_series_part(app.state.db, PartEntry(series_id=sid, part_no=1)))
        assert _get_watch(wid).series_id == sid

        resp = client.post(f"/series/{sid}/delete", data={"csrf_token": token},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/series?ok=deleted"
        assert _get_series(sid) is None
        assert asyncio.run(repo.count_series_parts(app.state.db, sid)) == 0
        assert _get_watch(wid).series_id is None
        assert "连载已删除" in client.get("/series?ok=deleted").text

    def test_delete_missing(self, client):
        _setup_admin(client)
        token = _csrf(client)
        resp = client.post("/series/4242/delete", data={"csrf_token": token},
                           follow_redirects=False)
        assert "err=notfound" in resp.headers["location"]

    def test_delete_requires_csrf(self, client):
        _setup_admin(client)
        token = _csrf(client)
        sid = _create_series(client, token, _add_watch("S"))
        resp = client.post(f"/series/{sid}/delete", follow_redirects=False)
        assert resp.status_code == 403
        assert _get_series(sid) is not None

    def test_delete_requires_admin(self, client):
        """删连载不可逆：普通用户带有效 CSRF 也 403，连载与绑定原样。"""
        _setup_admin(client)
        wid = _add_watch("S")
        sid = _create_series(client, _csrf(client), wid)
        _login_as_viewer(client)
        resp = client.post(f"/series/{sid}/delete", data={"csrf_token": _csrf(client)},
                           follow_redirects=False)
        assert resp.status_code == 403
        assert _get_series(sid) is not None
        assert _get_watch(wid).series_id == sid

    def test_edit_allowed_for_normal_user(self, client):
        """编辑不删东西，仍对所有已登录用户开放。"""
        _setup_admin(client)
        sid = _create_series(client, _csrf(client), _add_watch("S"))
        _login_as_viewer(client)
        resp = client.post(f"/series/{sid}/edit", data={
            "name": "S2", "status": "active", "csrf_token": _csrf(client),
        }, follow_redirects=False)
        assert resp.headers["location"] == f"/series/{sid}?ok=updated"
        assert _get_series(sid).name == "S2"


# ── watchlist 入口 ──────────────────────────────────────────

class TestWatchlistSeriesLink:
    def test_audiobook_row_links_to_create_or_view(self, client):
        _setup_admin(client)
        token = _csrf(client)
        unbound = _add_watch("盗墓笔记")
        bound = _add_watch("三体")
        _add_watch("周杰伦", kind=WatchKind.MUSIC)
        sid = _create_series(client, token, bound)

        html = client.get("/watchlist").text
        assert f'href="/series?watch_id={unbound}"' in html and "建立连载" in html
        assert f'href="/series/{sid}"' in html and "查看连载" in html
        # 音乐订阅行不带连载入口：只有两个有声书各一个链接
        assert html.count("建立连载") == 1 and html.count("查看连载") == 1
