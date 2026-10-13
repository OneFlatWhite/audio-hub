"""馒头 genDlToken 直链换取 + 派发层接入。

覆盖：
- MTeamAdapter.resolve_download_url：成功 / code!="0" / 主域失败切备域 / 直链原样返回
- BaseAdapter.resolve_download_url 默认行为
- Dispatcher：实跑前经 adapter 换取直链并把直链传给 qb；dry-run 不发请求；失败标 failed
"""

from __future__ import annotations

import httpx
import pytest
import respx

from audio_hub.adapters.base import (
    AdapterAuthError,
    AdapterError,
    BaseAdapter,
    MediaCategory,
    NormalizedTorrent,
)
from audio_hub.adapters.mteam import MTeamAdapter
from audio_hub.config import QbittorrentConfig, SiteConfig
from audio_hub.pipeline.dispatcher import Dispatcher
from audio_hub.pipeline.matcher import MatchResult

GEN_CC = "https://api.m-team.cc/api/torrent/genDlToken"
GEN_IO = "https://api.m-team.io/api/torrent/genDlToken"
DIRECT = "https://api.m-team.cc/api/rss/dl?credential=abc123&sign=xyz"


def make_adapter(**kw) -> MTeamAdapter:
    cfg = SiteConfig(
        name="mteam",
        enabled=True,
        search_url="https://api.m-team.cc/api/torrent/search",
        api_key="TEST_MTEAM_KEY",
        max_rows=50,
        **kw,
    )
    return MTeamAdapter(cfg)


def make_mteam_torrent(torrent_id="12345", download_url=None, **kw) -> NormalizedTorrent:
    if download_url is None:
        download_url = f"https://api.m-team.cc/api/torrent/genDlToken?id={torrent_id}"
    return NormalizedTorrent(
        info_hash=kw.pop("info_hash", "a" * 40),
        title=kw.pop("title", "周杰伦 七里香 FLAC"),
        title_normalized="周杰伦 七里香 flac",
        site="mteam",
        category=MediaCategory.MUSIC,
        download_url=download_url,
        torrent_id=torrent_id,
        size_bytes=700 * 1024**2,
        **kw,
    )


def ok_response(url: str = DIRECT) -> httpx.Response:
    # 馒头 genDlToken 的 code 是**字符串** "0"，与 search 的整数 0 不同
    return httpx.Response(200, json={"message": "SUCCESS", "data": url, "code": "0"})


# ── MTeamAdapter.resolve_download_url ───────────────────────

class TestMTeamResolveDownloadUrl:
    @pytest.mark.asyncio
    @respx.mock
    async def test_success_returns_direct_link(self):
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())

        url = await adapter.resolve_download_url(make_mteam_torrent("12345"))

        assert url == DIRECT
        assert route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_posts_form_id_and_api_key_header(self):
        """请求体为表单 id=<torrent_id>，header 带 x-api-key。"""
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())

        await adapter.resolve_download_url(make_mteam_torrent("98765"))

        req = route.calls.last.request
        assert req.headers["x-api-key"] == "TEST_MTEAM_KEY"
        assert "application/x-www-form-urlencoded" in req.headers["content-type"]
        assert req.content.decode() == "id=98765"

    @pytest.mark.asyncio
    @respx.mock
    async def test_accepts_plain_url_string(self):
        """也接受 str 输入（/review 采纳路径只有 download_url 字符串）。"""
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())

        url = await adapter.resolve_download_url(
            "https://api.m-team.cc/api/torrent/genDlToken?id=777"
        )

        assert url == DIRECT
        assert route.calls.last.request.content.decode() == "id=777"

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_url_falls_back_to_torrent_id(self):
        """download_url 为空时用 torrent 对象上的 torrent_id。"""
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())

        t = make_mteam_torrent("4242", download_url="")
        url = await adapter.resolve_download_url(t)

        assert url == DIRECT
        assert route.calls.last.request.content.decode() == "id=4242"

    @pytest.mark.asyncio
    @respx.mock
    async def test_direct_link_returned_as_is_without_request(self):
        """已是直链的 URL 原样返回，不发请求。"""
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())

        t = make_mteam_torrent("1", download_url=DIRECT)
        url = await adapter.resolve_download_url(t)

        assert url == DIRECT
        assert not route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_nonzero_code_raises_adapter_error(self):
        adapter = make_adapter()
        respx.post(GEN_CC).mock(
            return_value=httpx.Response(
                200, json={"message": "TORRENT_NOT_FOUND", "data": None, "code": "1"},
            )
        )

        with pytest.raises(AdapterError, match="业务错误"):
            await adapter.resolve_download_url(make_mteam_torrent("12345"))

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_data_raises_adapter_error(self):
        adapter = make_adapter()
        respx.post(GEN_CC).mock(return_value=ok_response(""))

        with pytest.raises(AdapterError, match="未返回直链"):
            await adapter.resolve_download_url(make_mteam_torrent("12345"))

    @pytest.mark.asyncio
    @respx.mock
    async def test_failover_to_secondary_domain(self):
        """主域名连接失败应切备域并成功换取。"""
        adapter = make_adapter()
        respx.post(GEN_CC).mock(side_effect=httpx.ConnectError("connection refused"))
        route_io = respx.post(GEN_IO).mock(return_value=ok_response())

        url = await adapter.resolve_download_url(make_mteam_torrent("12345"))

        assert url == DIRECT
        assert route_io.called
        assert adapter._active_base == "https://api.m-team.io"

    @pytest.mark.asyncio
    @respx.mock
    async def test_all_domains_fail_raises(self):
        adapter = make_adapter()
        respx.post(GEN_CC).mock(side_effect=httpx.ConnectError("fail"))
        respx.post(GEN_IO).mock(side_effect=httpx.ConnectError("fail"))

        with pytest.raises(AdapterError, match="所有域名均不可用"):
            await adapter.resolve_download_url(make_mteam_torrent("12345"))

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_auth_error(self):
        adapter = make_adapter()
        respx.post(GEN_CC).mock(return_value=httpx.Response(401))

        with pytest.raises(AdapterAuthError):
            await adapter.resolve_download_url(make_mteam_torrent("12345"))

    @pytest.mark.asyncio
    async def test_no_torrent_id_raises(self):
        adapter = make_adapter()
        t = make_mteam_torrent("", download_url="")
        with pytest.raises(AdapterError, match="torrent_id"):
            await adapter.resolve_download_url(t)

    def test_extract_torrent_id(self):
        assert MTeamAdapter._extract_torrent_id(
            "https://api.m-team.cc/api/torrent/genDlToken?id=123"
        ) == "123"
        assert MTeamAdapter._extract_torrent_id(
            "https://x/genDlToken?foo=1&id=456"
        ) == "456"
        assert MTeamAdapter._extract_torrent_id("https://x/dl?credential=abc") == ""
        assert MTeamAdapter._extract_torrent_id("") == ""


# ── BaseAdapter 默认行为 ────────────────────────────────────

class PassthroughAdapter(BaseAdapter):
    def __init__(self, name="fakesite"):
        self.config = None
        self.site_name = name

    async def fetch_rss(self, query=None):
        return []

    async def search(self, keyword):
        return []

    def parse_title(self, raw):
        return {}


class TestBaseAdapterDefault:
    @pytest.mark.asyncio
    async def test_default_returns_torrent_download_url(self):
        t = make_mteam_torrent("1", download_url="https://site/download.php?id=1&passkey=k")
        assert await PassthroughAdapter().resolve_download_url(t) == t.download_url

    @pytest.mark.asyncio
    async def test_default_returns_str_as_is(self):
        assert await PassthroughAdapter().resolve_download_url("https://x/dl") == "https://x/dl"

    @pytest.mark.asyncio
    async def test_default_none_becomes_empty(self):
        assert await PassthroughAdapter().resolve_download_url("") == ""


# ── Dispatcher 接入 ─────────────────────────────────────────

class RecordingQb:
    """假 qb client：记录 torrents_add 入参。"""

    def __init__(self):
        self.calls: list[dict] = []

    def torrents_add(self, **kw):
        self.calls.append(kw)


def make_match(t: NormalizedTorrent) -> MatchResult:
    return MatchResult(torrent=t, action="accept", confidence="high", score=1.0, quality="flac")


def qb_config() -> QbittorrentConfig:
    return QbittorrentConfig(api_key="x", category="audio-hub", tag="hub", save_path="/seeds")


class TestDispatcherResolvesDirectLink:
    @pytest.mark.asyncio
    @respx.mock
    async def test_real_dispatch_passes_direct_link_to_qb(self):
        """实跑：馒头任务派发前换取直链，qb 收到的是直链而非占位 URL。"""
        adapter = make_adapter()
        respx.post(GEN_CC).mock(return_value=ok_response())
        qb = RecordingQb()
        dispatcher = Dispatcher(
            qb_config(), client=qb, dry_run=False,
            adapter_lookup={"mteam": adapter}.get,
        )

        result = await dispatcher.dispatch([make_match(make_mteam_torrent("12345"))])

        assert result.ok_count == 1
        assert result.failed == []
        assert len(qb.calls) == 1
        assert qb.calls[0]["urls"] == DIRECT
        assert "genDlToken" not in qb.calls[0]["urls"]
        assert qb.calls[0]["tags"] == ["hub", "mteam"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_resolve_failure_marks_failed_and_skips_qb(self):
        """换取失败 → 标 failed 并记录原因，不碰 qb。"""
        adapter = make_adapter()
        respx.post(GEN_CC).mock(
            return_value=httpx.Response(200, json={"data": None, "code": "1", "message": "x"})
        )
        qb = RecordingQb()
        dispatcher = Dispatcher(
            qb_config(), client=qb, dry_run=False,
            adapter_lookup={"mteam": adapter}.get,
        )

        result = await dispatcher.dispatch([make_match(make_mteam_torrent("12345"))])

        assert result.ok_count == 0
        assert len(result.failed) == 1
        _, reason = result.failed[0]
        assert reason.startswith("resolve_download_url_failed")
        assert "业务错误" in reason
        assert qb.calls == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_dry_run_does_not_call_gen_dl_token(self):
        """dry-run：不换取直链、不碰 qb。"""
        adapter = make_adapter()
        route = respx.post(GEN_CC).mock(return_value=ok_response())
        qb = RecordingQb()
        dispatcher = Dispatcher(
            qb_config(), client=qb, dry_run=True,
            adapter_lookup={"mteam": adapter}.get,
        )

        result = await dispatcher.dispatch([make_match(make_mteam_torrent("12345"))])

        assert result.ok_count == 1
        assert not route.called
        assert qb.calls == []

    @pytest.mark.asyncio
    async def test_unknown_site_uses_url_as_is(self):
        """查不到适配器（站点禁用/未注入）时 download_url 原样交给 qb。"""
        qb = RecordingQb()
        dispatcher = Dispatcher(
            qb_config(), client=qb, dry_run=False, adapter_lookup=lambda _site: None,
        )
        t = make_mteam_torrent("1", download_url="https://hdsky.me/download.php?id=1&passkey=k")
        t = t.model_copy(update={"site": "hdsky"})

        result = await dispatcher.dispatch([make_match(t)])

        assert result.ok_count == 1
        assert qb.calls[0]["urls"] == t.download_url

    @pytest.mark.asyncio
    async def test_no_lookup_configured_uses_url_as_is(self):
        """未注入 adapter_lookup（旧构造方式）仍可派发。"""
        qb = RecordingQb()
        dispatcher = Dispatcher(qb_config(), client=qb, dry_run=False)
        t = make_mteam_torrent("1", download_url="https://x/dl?passkey=k")

        result = await dispatcher.dispatch([make_match(t)])

        assert result.ok_count == 1
        assert qb.calls[0]["urls"] == "https://x/dl?passkey=k"

    @pytest.mark.asyncio
    @respx.mock
    async def test_public_resolve_by_site_and_url_string(self):
        """公开方法：/review 采纳路径按 (url, site) 换取直链。"""
        adapter = make_adapter()
        respx.post(GEN_CC).mock(return_value=ok_response())
        dispatcher = Dispatcher(
            qb_config(), client=None, dry_run=False, adapter_lookup={"mteam": adapter}.get,
        )

        url = await dispatcher.resolve_download_url(
            "https://api.m-team.cc/api/torrent/genDlToken?id=555", site="mteam",
        )
        assert url == DIRECT

        # 非馒头站点原样返回
        plain = await dispatcher.resolve_download_url("https://hdsky.me/dl?id=1", site="hdsky")
        assert plain == "https://hdsky.me/dl?id=1"

    @pytest.mark.asyncio
    async def test_resolver_exception_isolated_per_item(self):
        """一条解析炸了不影响同批其他项。"""

        class BoomAdapter(PassthroughAdapter):
            async def resolve_download_url(self, torrent_or_url):
                raise RuntimeError("boom")

        qb = RecordingQb()
        dispatcher = Dispatcher(
            qb_config(), client=qb, dry_run=False,
            adapter_lookup={"boom": BoomAdapter("boom")}.get,
        )
        bad = make_mteam_torrent("1", download_url="https://boom/x", info_hash="b" * 40)
        bad = bad.model_copy(update={"site": "boom"})
        good = make_mteam_torrent("2", download_url="https://ok/x", info_hash="c" * 40)
        good = good.model_copy(update={"site": "hdsky"})

        result = await dispatcher.dispatch([make_match(bad), make_match(good)])

        assert result.ok_count == 1
        assert len(result.failed) == 1
        assert "RuntimeError" in result.failed[0][1]
        assert qb.calls[0]["urls"] == "https://ok/x"


# ── 派发守卫（原 test_pipeline_dryrun.py，旧 search_round 删除后迁来）───────

class TestDispatcherGuards:
    @pytest.mark.asyncio
    async def test_defensive_block_video_in_dispatch(self):
        """即使 matcher 漏判，dispatcher 也应防御性拦截 VIDEO（下载前最后关口）。"""
        video = make_mteam_torrent("1", title="某演唱会 BluRay").model_copy(
            update={"category": MediaCategory.VIDEO}
        )
        # 手动构造一个被错误标为 accept 的 VIDEO
        m = MatchResult(torrent=video, action="accept", confidence="high",
                        score=1.0, quality="n/a")
        dispatcher = Dispatcher(qb_config(), client=None, dry_run=False)
        result = await dispatcher.dispatch([m])
        assert len(result.failed) == 1
        assert "defensive_block" in result.failed[0][1]

    @pytest.mark.asyncio
    async def test_no_candidates_dispatch_noop(self):
        dispatcher = Dispatcher(qb_config(), client=None, dry_run=True)
        assert (await dispatcher.dispatch([])).ok_count == 0
        assert (await dispatcher.dispatch_tasks([])).ok_count == 0

    @pytest.mark.asyncio
    async def test_dry_run_dispatch_tasks_never_touches_client(self):
        """dry-run 下 dispatch_tasks（v0.6 生产入口）即使持有 client 也不应调用它。"""
        from audio_hub.models import Task, TaskState

        class ExplodingClient:
            def torrents_add(self, *a, **kw):
                raise AssertionError("dry-run 不应调用 qb！")

        dispatcher = Dispatcher(qb_config(), client=ExplodingClient(), dry_run=True)
        task = Task(
            info_hash="d" * 40, site="mteam", title="周杰伦 七里香 FLAC",
            state=TaskState.DISCOVERED,
            download_url="https://api.m-team.cc/api/torrent/genDlToken?id=1",
            size_bytes=700 * 1024**2,
        )
        result = await dispatcher.dispatch_tasks([task])
        assert result.ok_count == 1   # 没抛异常即证明未调用 qb
        assert result.dry_run is True
