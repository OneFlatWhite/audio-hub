"""推送 sink 单测：三渠道 payload、kinds 过滤、失败不抛、send_test、build/rebuild、MultiNotifier。

全部用 respx 拦截 httpx，不触网。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from audio_hub.config import NotifyConfig
from audio_hub.notify import (
    Event,
    GenericWebhookNotifier,
    MultiNotifier,
    TelegramNotifier,
    WecomNotifier,
    build_push_sinks,
    rebuild_push_sinks,
)

_WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET"
_GENERIC = "https://hooks.example.com/audio-hub"
_TG = "https://api.telegram.org/botTOKEN123/sendMessage"


def _event(kind="dispatched", **detail) -> Event:
    return Event(kind=kind, site="hhanclub", title="周杰伦 - 范特西 [FLAC]", detail=detail)


# ── 三渠道 payload ───────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_wecom_payload_is_text_message():
    route = respx.post(_WECOM).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    await WecomNotifier(_WECOM).emit(_event(size="500 MB"))

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["msgtype"] == "text"
    text = body["text"]["content"]
    assert text.startswith("[audio-hub] 已派发 | hhanclub | 周杰伦 - 范特西 [FLAC]")
    assert "size=500 MB" in text


@pytest.mark.asyncio
@respx.mock
async def test_telegram_payload_and_url():
    route = respx.post(_TG).mock(return_value=httpx.Response(200, json={"ok": True}))
    await TelegramNotifier("TOKEN123", "-100777").emit(_event())

    body = json.loads(route.calls.last.request.content)
    assert body["chat_id"] == "-100777"
    assert body["text"].startswith("[audio-hub] 已派发 | hhanclub")


@pytest.mark.asyncio
@respx.mock
async def test_generic_webhook_posts_full_event_json():
    route = respx.post(_GENERIC).mock(return_value=httpx.Response(204))
    ev = _event(reason="x")
    await GenericWebhookNotifier(_GENERIC).emit(ev)

    body = json.loads(route.calls.last.request.content)
    assert body == json.loads(ev.to_json())
    assert body["kind"] == "dispatched" and body["detail"] == {"reason": "x"}


def test_format_text_without_detail_has_no_trailing_newline():
    n = GenericWebhookNotifier(_GENERIC)
    text = n.format_text(Event(kind="unknown_kind", title="T"))
    assert text == "[audio-hub] unknown_kind | T"       # 未知 kind 原样输出，无 site 段
    assert "\n" not in text


# ── kinds 过滤 ──────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_kinds_whitelist_filters_events():
    route = respx.post(_GENERIC).mock(return_value=httpx.Response(200))
    n = GenericWebhookNotifier(_GENERIC, kinds=["completed"])

    await n.emit(_event("dispatched"))
    assert not route.called
    await n.emit(_event("completed"))
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_kinds_none_sends_everything_and_empty_list_sends_nothing():
    route = respx.post(_GENERIC).mock(return_value=httpx.Response(200))

    await GenericWebhookNotifier(_GENERIC, kinds=None).emit(_event("round_summary"))
    assert route.call_count == 1

    # 空列表 = 全部不推（与设置页「留空＝全部不推」一致）
    await GenericWebhookNotifier(_GENERIC, kinds=[]).emit(_event("dispatched"))
    assert route.call_count == 1


# ── 失败不抛、不含 URL ──────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_http_500_does_not_raise():
    respx.post(_WECOM).mock(return_value=httpx.Response(500))
    await WecomNotifier(_WECOM).emit(_event())      # 不抛即通过


@pytest.mark.asyncio
@respx.mock
async def test_network_error_does_not_raise():
    respx.post(_WECOM).mock(side_effect=httpx.ConnectError("boom"))
    await WecomNotifier(_WECOM).emit(_event())


# ── send_test ───────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_send_test_success_reports_status():
    respx.post(_WECOM).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    ok, msg = await WecomNotifier(_WECOM).send_test()
    assert ok is True
    assert msg == "企业微信: HTTP 200"


@pytest.mark.asyncio
@respx.mock
async def test_send_test_failure_hides_url():
    respx.post(_WECOM).mock(return_value=httpx.Response(403))
    ok, msg = await WecomNotifier(_WECOM).send_test()
    assert ok is False
    assert msg == "企业微信: HTTP 403"
    assert "SECRET" not in msg and "http" not in msg


@pytest.mark.asyncio
@respx.mock
async def test_send_test_timeout_message():
    respx.post(_TG).mock(side_effect=httpx.ReadTimeout("slow"))
    ok, msg = await TelegramNotifier("TOKEN123", "1").send_test()
    assert ok is False
    assert msg.startswith("Telegram: 超时")
    assert "TOKEN123" not in msg


@pytest.mark.asyncio
@respx.mock
async def test_send_test_ignores_kinds_filter():
    """kinds 为空列表时正常事件全不推，但测试通知必须仍然发出。"""
    route = respx.post(_GENERIC).mock(return_value=httpx.Response(200))
    ok, _ = await GenericWebhookNotifier(_GENERIC, kinds=[]).send_test()
    assert ok and route.called


# ── build_push_sinks ────────────────────────────────────────

def test_build_push_sinks_empty_config():
    assert build_push_sinks(NotifyConfig()) == []


def test_build_push_sinks_all_channels():
    cfg = NotifyConfig(
        wecom_webhook=_WECOM, telegram_bot_token="T", telegram_chat_id="C",
        generic_webhook_url=_GENERIC, kinds=["dispatched", "failed"],
    )
    sinks = build_push_sinks(cfg)
    assert [type(s) for s in sinks] == [WecomNotifier, TelegramNotifier, GenericWebhookNotifier]
    assert all(s.kinds == {"dispatched", "failed"} for s in sinks)
    assert all(s.is_push for s in sinks)


def test_build_push_sinks_telegram_requires_both_fields():
    assert build_push_sinks(NotifyConfig(telegram_bot_token="T")) == []
    assert build_push_sinks(NotifyConfig(telegram_chat_id="C")) == []
    sinks = build_push_sinks(NotifyConfig(telegram_bot_token="T", telegram_chat_id="C"))
    assert len(sinks) == 1 and isinstance(sinks[0], TelegramNotifier)


# ── MultiNotifier 推送 sink 管理 ─────────────────────────────

class _Collect:
    """基础 sink（无 is_push 标记）。"""

    def __init__(self):
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def test_multi_replace_push_sinks_keeps_base_sinks():
    base = _Collect()
    old_push = WecomNotifier(_WECOM)
    multi = MultiNotifier([base, old_push])
    assert multi.push_sinks == [old_push]

    new_push = [GenericWebhookNotifier(_GENERIC)]
    returned = multi.replace_push_sinks(new_push)

    assert returned == new_push
    assert multi.push_sinks == new_push
    assert base in multi.sinks and old_push not in multi.sinks


def test_multi_replace_push_sinks_with_empty_list_removes_all_push():
    base = _Collect()
    multi = MultiNotifier([base, WecomNotifier(_WECOM)])
    multi.replace_push_sinks([])
    assert multi.sinks == [base]


@pytest.mark.asyncio
@respx.mock
async def test_multi_fans_out_to_push_sinks_and_survives_failure():
    route = respx.post(_GENERIC).mock(return_value=httpx.Response(500))
    base = _Collect()
    multi = MultiNotifier([base, GenericWebhookNotifier(_GENERIC)])
    await multi.dispatched("hhanclub", "X")
    assert len(base.events) == 1
    assert route.called                      # 推送失败只记日志，基础 sink 已收到


def test_rebuild_push_sinks_accepts_notifier_or_app():
    multi = MultiNotifier([_Collect()])
    cfg = NotifyConfig(wecom_webhook=_WECOM)

    sinks = rebuild_push_sinks(multi, cfg)
    assert len(sinks) == 1 and isinstance(sinks[0], WecomNotifier)
    assert multi.push_sinks == sinks

    class _State:
        notifier = multi

    class _App:
        state = _State()

    sinks2 = rebuild_push_sinks(_App(), NotifyConfig(generic_webhook_url=_GENERIC))
    assert [type(s) for s in multi.push_sinks] == [GenericWebhookNotifier]
    assert sinks2 == multi.push_sinks


def test_rebuild_push_sinks_when_notifier_not_ready_returns_empty():
    class _App:
        state = object()          # 没有 notifier 属性

    assert rebuild_push_sinks(_App(), NotifyConfig(wecom_webhook=_WECOM)) == []
