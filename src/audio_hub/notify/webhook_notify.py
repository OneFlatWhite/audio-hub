"""推送 sink：企业微信机器人 / Telegram Bot / 通用 webhook。

三者共享一个基类：kinds 白名单过滤、10s 超时的 httpx 调用、失败只记
warning。**失败绝不再向 MultiNotifier 发事件** —— 推送失败若自身也产生
事件，事件又被推送、再失败……会形成递归；推送是旁路，失败只能落日志。

凭据安全：webhook 地址含 key、Telegram URL 含 token。本模块的日志与
`send_test` 的返回文案只给渠道名 + 状态码 / 异常类名，**绝不带 URL**，
否则设置页 flash 和日志里就会出现明文凭据。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx
from loguru import logger

from .base import BaseNotifier, Event

#: 单次推送超时（秒）。推送是旁路，宁可丢一条也不能拖住流水线
_TIMEOUT = 10.0

#: detail 摘要最长字符数（企微 text 上限 2048 字节，留足余量）
_DETAIL_MAX = 300

#: 事件种类的中文名，推送文本里更易读；没列到的原样输出 kind
_KIND_LABELS = {
    "dispatched": "已派发",
    "completed": "下载完成",
    "routed": "已归档",
    "quarantined": "已隔离",
    "hr_satisfied": "H&R 达标",
    "series_completed": "连载完结",
    "failed": "失败",
    "adapter_error": "站点异常",
    "round_summary": "轮次汇总",
    "test": "测试通知",
}


class WebhookNotifierBase(BaseNotifier):
    """推送渠道公共基类。子类只需实现 `build_request`。

    kinds：None 表示不过滤（全部推送）；列表则为白名单 —— 空列表即全部不推，
    与设置页「推送的事件种类」留空的语义一致（见 settings_store.NOTIFY_SPECS）。
    `send_test` 不受此过滤影响，否则 kinds 为空时测试按钮永远「成功」却什么都没发。
    """

    #: MultiNotifier.replace_push_sinks 按此标记区分推送 sink 与基础 sink
    is_push = True
    #: 渠道显示名（设置页测试结果 / 日志用）
    channel = "webhook"

    def __init__(self, kinds: Iterable[str] | None = None):
        self.kinds: set[str] | None = None if kinds is None else set(kinds)

    # ── 子类接口 ──

    def build_request(self, event: Event) -> tuple[str, Any]:
        """返回 (url, JSON 负载)。"""  # pragma: no cover - 子类必须覆写
        raise NotImplementedError

    # ── 公共逻辑 ──

    def accepts(self, event: Event) -> bool:
        return self.kinds is None or event.kind in self.kinds

    def format_text(self, event: Event) -> str:
        """纯文本格式：「[audio-hub] <kind> | <site> | <title>\\n<detail 摘要>」。"""
        head = " | ".join(
            p for p in (
                f"[audio-hub] {_KIND_LABELS.get(event.kind, event.kind)}",
                event.site or "",
                event.title or "",
            ) if p
        )
        if not event.detail:
            return head
        summary = _summarize_detail(event.detail)
        return f"{head}\n{summary}" if summary else head

    async def _post(self, url: str, payload: Any) -> httpx.Response:
        """发请求并对非 2xx 抛 HTTPStatusError。"""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp

    async def emit(self, event: Event) -> None:
        if not self.accepts(event):
            return
        url, payload = self.build_request(event)
        try:
            await self._post(url, payload)
        except Exception as e:  # noqa: BLE001 - 旁路推送失败只记日志，绝不外溢、绝不再发事件
            logger.warning(
                f"推送 {self.channel} 事件 {event.kind} 失败: {_describe_error(e)}"
            )

    async def send_test(self) -> tuple[bool, str]:
        """发一条测试事件。返回 (是否成功, 可展示的说明)。说明不含 URL。"""
        event = Event(
            kind="test", title="audio-hub 测试通知",
            detail={"message": "如果你看到这条消息，说明该推送渠道配置正确。"},
        )
        url, payload = self.build_request(event)
        try:
            resp = await self._post(url, payload)
        except Exception as e:  # noqa: BLE001 - 把任何失败都翻译成文案，不让测试端点 500
            return False, f"{self.channel}: {_describe_error(e)}"
        return True, f"{self.channel}: HTTP {resp.status_code}"


class WecomNotifier(WebhookNotifierBase):
    """企业微信群机器人（text 消息）。"""

    channel = "企业微信"

    def __init__(self, webhook_url: str, kinds: Iterable[str] | None = None):
        super().__init__(kinds)
        self.webhook_url = webhook_url

    def build_request(self, event: Event) -> tuple[str, Any]:
        return self.webhook_url, {
            "msgtype": "text",
            "text": {"content": self.format_text(event)},
        }


class TelegramNotifier(WebhookNotifierBase):
    """Telegram Bot sendMessage。"""

    channel = "Telegram"

    def __init__(self, bot_token: str, chat_id: str, kinds: Iterable[str] | None = None):
        super().__init__(kinds)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def build_request(self, event: Event) -> tuple[str, Any]:
        return (
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            {"chat_id": self.chat_id, "text": self.format_text(event)},
        )


class GenericWebhookNotifier(WebhookNotifierBase):
    """通用 webhook：POST 完整事件 JSON（与 Event.to_json 同构）。"""

    channel = "通用 Webhook"

    def __init__(self, url: str, kinds: Iterable[str] | None = None):
        super().__init__(kinds)
        self.url = url

    def build_request(self, event: Event) -> tuple[str, Any]:
        # 走 json.loads(to_json()) 而非 asdict：保证与 to_json 的序列化
        # （ensure_ascii=False / default=str）完全一致，接收方只需认一种格式
        return self.url, json.loads(event.to_json())


def build_push_sinks(notify_cfg) -> list[WebhookNotifierBase]:
    """按 NotifyConfig 中非空的渠道构造推送 sink。

    Telegram 需 token 与 chat_id 同时具备；只填一半视为未配置（而非构造一个
    必然失败的 sink 让每条事件都报 warning）。
    """
    kinds = list(notify_cfg.kinds)
    sinks: list[WebhookNotifierBase] = []
    if notify_cfg.wecom_webhook:
        sinks.append(WecomNotifier(notify_cfg.wecom_webhook, kinds))
    if notify_cfg.telegram_bot_token and notify_cfg.telegram_chat_id:
        sinks.append(TelegramNotifier(
            notify_cfg.telegram_bot_token, notify_cfg.telegram_chat_id, kinds,
        ))
    if notify_cfg.generic_webhook_url:
        sinks.append(GenericWebhookNotifier(notify_cfg.generic_webhook_url, kinds))
    return sinks


def rebuild_push_sinks(app_or_notifier, notify_cfg) -> list[WebhookNotifierBase]:
    """按当前 notify 配置重建推送 sink（设置页热生效用）。

    接受 FastAPI app（取 app.state.notifier）或 MultiNotifier 本身。
    notifier 尚未就绪（lifespan 未跑完）时返回空列表并记 warning，不抛。
    """
    notifier = getattr(getattr(app_or_notifier, "state", None), "notifier", app_or_notifier)
    if not hasattr(notifier, "replace_push_sinks"):
        logger.warning("notifier 尚未就绪，推送渠道改动需重启生效")
        return []
    sinks = notifier.replace_push_sinks(build_push_sinks(notify_cfg))
    logger.info(f"推送渠道已按新配置重建: {[s.channel for s in sinks]}")
    return sinks


# ── 内部辅助 ────────────────────────────────────────────────

def _summarize_detail(detail: dict[str, Any]) -> str:
    """detail 字典 → 一行 `k=v` 摘要，超长截断。"""
    parts = []
    for k, v in detail.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        parts.append(f"{k}={text}")
    line = "  ".join(parts)
    return line if len(line) <= _DETAIL_MAX else line[: _DETAIL_MAX - 1] + "…"


def _describe_error(e: Exception) -> str:
    """异常 → 不含 URL 的短文案。httpx 的 str(e) 会带完整 URL（含凭据），不能直接用。"""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.TimeoutException):
        return f"超时（{_TIMEOUT:.0f}s）"
    if isinstance(e, httpx.HTTPError):
        return f"请求失败 {type(e).__name__}"
    return f"{type(e).__name__}: {e}"
