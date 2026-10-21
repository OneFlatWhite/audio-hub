"""p115 模块测试:分享链接解析、snap 验包(分页/深度/总量上限)、cookie 转存(重试/延迟/表单)。

覆盖:
- parse_share_url 三域名解析、无提取码、非法输入
- snap 单页整形、count 驱动分页、子目录递归深度上限、总量 3000 上限、state 假抛错
- receive 成功表单与 Cookie 头、失败重试一次、两次皆败、网络异常、随机延迟注入
- aclose 关闭外部传入客户端
"""
from __future__ import annotations

import urllib.parse

import httpx
import pytest
import respx

from audio_hub.lackfill.p115 import P115Client, P115Error, parse_share_url

_SNAP = "https://webapi.115.com/share/snap"
_RECEIVE = "https://webapi.115.com/share/receive"


class _Sleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _client(sleeper: _Sleeper | None = None, **kwargs) -> P115Client:
    return P115Client("UID=1; CID=2", sleep=sleeper or _Sleeper(), **kwargs)


def test_parse_share_url_supports_all_domains() -> None:
    """115.com / 115cdn.com / 115cdn.net 三种域名均可解析出分享码与提取码。"""
    for host in ("115.com", "115cdn.com", "115cdn.net"):
        assert parse_share_url(f"https://{host}/s/abc123?password=x9y8") == ("abc123", "x9y8")


def test_parse_share_url_without_password() -> None:
    """无 password 参数时提取码返回 None。"""
    assert parse_share_url("https://115cdn.com/s/abc123") == ("abc123", None)


def test_parse_share_url_rejects_invalid() -> None:
    """非 115 分享链接与纯文本一律抛 ValueError。"""
    for bad in ("https://example.com/s/abc", "not a url", ""):
        with pytest.raises(ValueError):
            parse_share_url(bad)


@respx.mock
async def test_snap_single_page() -> None:
    """单页快照:文件取 fid、目录取 cid,整形为 name/file_id/is_dir;目录会再递归一次。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cid"):
            return httpx.Response(200, json={"state": True, "data": {"count": 0, "list": []}})
        return httpx.Response(200, json={"state": True, "data": {"count": 2, "list": [
            {"n": "Ep01.mkv", "fid": 111}, {"n": "Season1", "cid": 222}]}})

    respx.get(_SNAP).mock(side_effect=handler)
    items = await _client().snap("abc123", "x9y8")
    assert items == [
        {"name": "Ep01.mkv", "file_id": "111", "is_dir": False},
        {"name": "Season1", "file_id": "222", "is_dir": True},
    ]


@respx.mock
async def test_snap_pagination() -> None:
    """count 驱动分页:offset=0 返回 2/3 项,offset=2 返回最后 1 项,合计 3 项。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("offset", "0") == "0":
            lst = [{"n": "a", "fid": 1}, {"n": "b", "fid": 2}]
        else:
            lst = [{"n": "c", "fid": 3}]
        return httpx.Response(200, json={"state": True, "data": {"count": 3, "list": lst}})

    respx.get(_SNAP).mock(side_effect=handler)
    items = await _client().snap("code")
    assert [i["name"] for i in items] == ["a", "b", "c"]


@respx.mock
async def test_snap_respects_max_depth() -> None:
    """目录递归最多 3 层:第 3 层内容收录,其下子目录不再展开。"""
    pages: dict[str | None, list[dict]] = {
        None: [{"n": "dirB", "cid": "B"}],
        "B": [{"n": "dirC", "cid": "C"}],
        "C": [{"n": "F.mkv", "fid": "F"}, {"n": "dirD", "cid": "D"}],
    }
    requested: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cid = request.url.params.get("cid") or None
        requested.append(cid)
        lst = pages[cid]
        return httpx.Response(200, json={"state": True, "data": {"count": len(lst), "list": lst}})

    respx.get(_SNAP).mock(side_effect=handler)
    items = await _client().snap("code")
    assert "F.mkv" in [i["name"] for i in items]
    assert "D" not in requested


@respx.mock
async def test_snap_respects_total_cap() -> None:
    """清单总数达 3000 上限即停止追加。"""
    page = [{"n": f"f{i}", "fid": i} for i in range(1000)]
    respx.get(_SNAP).mock(return_value=httpx.Response(
        200, json={"state": True, "data": {"count": 5000, "list": page}}))
    items = await _client().snap("code")
    assert len(items) == 3000


@respx.mock
async def test_snap_state_false_raises() -> None:
    """state 为假(如提取码错误)抛 P115Error。"""
    respx.get(_SNAP).mock(return_value=httpx.Response(200, json={"state": False, "error": "x"}))
    with pytest.raises(P115Error):
        await _client().snap("code", "bad")


@respx.mock
async def test_receive_success_posts_form() -> None:
    """转存提交 form 字段齐全且带 Cookie 头,state 为真返回 True。"""
    route = respx.post(_RECEIVE).mock(return_value=httpx.Response(200, json={"state": True}))
    ok = await _client().receive("abc", "pwd", ["1", "2", "3"], cid="999")
    assert ok is True
    request = route.calls.last.request
    form = urllib.parse.parse_qs(request.content.decode())
    assert form["share_code"] == ["abc"]
    assert form["receive_code"] == ["pwd"]
    assert form["file_id"] == ["1,2,3"]
    assert form["cid"] == ["999"]
    assert request.headers["Cookie"] == "UID=1; CID=2"


@respx.mock
async def test_receive_retries_once_after_failure() -> None:
    """首次 state 为假重试一次,第二次成功返回 True。"""
    route = respx.post(_RECEIVE).mock(side_effect=[
        httpx.Response(200, json={"state": False}),
        httpx.Response(200, json={"state": True}),
    ])
    assert await _client().receive("a", "", ["1"], "9") is True
    assert len(route.calls) == 2


@respx.mock
async def test_receive_gives_up_after_two_failures() -> None:
    """两次均失败返回 False,恰好请求两次。"""
    route = respx.post(_RECEIVE).mock(return_value=httpx.Response(200, json={"state": False}))
    assert await _client().receive("a", "", ["1"], "9") is False
    assert len(route.calls) == 2


@respx.mock
async def test_receive_network_error_returns_false() -> None:
    """网络异常不抛出,按失败处理,重试后返回 False。"""
    respx.post(_RECEIVE).mock(side_effect=httpx.ConnectError("down"))
    assert await _client().receive("a", "", ["1"], "9") is False


@respx.mock
async def test_receive_sleeps_configured_delay() -> None:
    """每次转存调用前按 [delay_min, delay_max] 随机睡眠(两端相等便于断言),每次调用仅一次。"""
    respx.post(_RECEIVE).mock(return_value=httpx.Response(200, json={"state": True}))
    sleeper = _Sleeper()
    client = P115Client("c", delay_min=2.5, delay_max=2.5, sleep=sleeper)
    await client.receive("a", "", ["1"], "9")
    assert sleeper.calls == [2.5]


async def test_aclose_closes_owned_client() -> None:
    """外部传入的 httpx 客户端由 aclose 负责关闭。"""
    http_client = httpx.AsyncClient()
    client = P115Client("c", sleep=_Sleeper(), client=http_client)
    await client.aclose()
    assert http_client.is_closed
