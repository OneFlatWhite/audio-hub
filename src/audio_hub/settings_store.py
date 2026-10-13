"""运行时设置覆盖层（v0.3）：让界面改动可持久化，并诚实标注生效范围。

为什么不让界面直接回写 config.yaml
----------------------------------
config.yaml 是**部署产物**：人手维护、含注释、随容器卷挂载。让 Web 界面
直接回写它有三个真实代价：
1. 注释与排版必然丢失 —— PyYAML 的 round-trip 不保留注释，而这份文件里
   的注释承载着「cat410 需实测确认」这类实测结论，丢了就是知识流失
2. 写入过程被中断会损坏配置文件，导致下次启动直接起不来
3. 部署脚本与界面互相覆盖，谁是真源说不清

故采用**覆盖层**：YAML 仍是基线与唯一手写入口，界面的改动写入 DB 的
app_settings 表，启动时合并到 config 之上。DB 里没有记录的键沿用 YAML 值。

生效范围必须诚实标注
--------------------
并非所有设置都能热生效。适配器与 qb 客户端在启动时构建，dry_run 在
Dispatcher / CompletionMonitor 构造时被固化。若界面对这些谎报「已生效」，
运行时仍在用旧值 —— 那比只读更危险，因为使用者会以为改成功了，
进而以为空跑保护还开着、实际已经在真实下载。

故每个键在 SPEC 中显式声明 scope=hot|restart：
- hot     保存后立即反映到运行时（重排调度、重建适配器、或派发时实时读取）
- restart 只落库，运行时不变，并置「待重启」标记，页面顶部挂横幅

为什么 db_path 不在覆盖层内
--------------------------
DB 路径决定了覆盖层自身存在哪里。让被管理者管理自己会形成鸡生蛋问题
（改完路径，下次读覆盖层就读不到了）。它只能改 YAML，界面只读展示。

凭据只写不读
------------
kind="secret" 的键在 DB 中存 Fernet token，启动时解密进内存。
界面永不回显明文或密文，只暴露「是否已配置」；输入框留空表示不修改。

站点级设置已迁出（v0.6）
------------------------
site_enabled:* / site_rss:* / site_api_key:* / site_hr_*:* 这组键不再出现在设置页：
站点配置的权威源改为 sites 表（/sites 页维护，见 registry_store）。前缀常量保留，
只供 registry_store.seed_from_yaml 把旧覆盖项合并进 sites 后删除。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .config import NOTIFY_KINDS
from .timeutil import iso_utc

SCOPE_HOT = "hot"
SCOPE_RESTART = "restart"

#: 内部标记键，不参与配置合并（load_overrides 会过滤掉 __ 前缀）
RESTART_FLAG_KEY = "__restart_pending"

#: 轮询间隔安全下限（分钟）。必须与 scheduler.build_scheduler 的钳制同值 ——
#: 否则界面校验放行的值又被调度器悄悄钳一次，使用者看到的与实际跑的不一致。
MIN_POLL_MINUTES = 5

#: 站点级键前缀（**仅供迁移**：v0.6 起站点配置在 sites 表，这些键不再由设置页读写；
#: registry_store.seed_from_yaml 启动时把残留的旧键合并进 sites 后删除）。
SITE_ENABLED_PREFIX = "site_enabled:"
SITE_RSS_PREFIX = "site_rss:"
SITE_APIKEY_PREFIX = "site_api_key:"
SITE_HR_HOURS_PREFIX = "site_hr_hours:"
SITE_HR_RATIO_PREFIX = "site_hr_ratio:"
SITE_ADAPTER_PREFIXES = (SITE_ENABLED_PREFIX, SITE_RSS_PREFIX, SITE_APIKEY_PREFIX)
SITE_KEY_PREFIXES = (*SITE_ADAPTER_PREFIXES, SITE_HR_HOURS_PREFIX, SITE_HR_RATIO_PREFIX)

#: 凭据字段的界面标签（credential_status 用）
_CRED_LABELS = {
    "rss_url": "RSS 地址（含 passkey）",
    "api_key": "API Key（存取令牌）",
}


class SettingsError(Exception):
    """设置值非法。消息可直接展示给使用者。"""


@dataclass(frozen=True)
class Spec:
    """一个可编辑设置项的声明。"""

    key: str                       # 表单字段名 / DB 主键
    label: str                     # 界面显示名
    kind: str                      # bool | int | float | str | list | secret
    scope: str                     # hot | restart
    path: str                      # AppConfig 点分路径
    hint: str = ""
    lo: float | None = None        # int/float 下限
    hi: float | None = None        # int/float 上限
    min_len: int = 0               # str 最小长度
    choices: tuple[str, ...] = ()  # str 枚举；list 时为每个元素的合法值
    #: int/float 可为空：表单留空 = 清除该项（写回 None，运行时回落到全局默认）。
    #: 用于站点级 H&R 覆盖 —— 不是每个站都有自己的规则，没有就该是 None
    #: 而非被迫填一个假数字。
    nullable: bool = False
    labels: tuple[str, str] = ("开启", "关闭")   # bool 的两个显示文案（对应 1/0）
    #: 该字段必须是完整 http(s) URL。用于站点 RSS 地址 —— 填了非 URL 的值
    #: 会保存成功但每轮搜索必然失败并触发熔断，故在保存时就拒绝，
    #: 不让使用者事后从站点健康页去猜原因。
    require_url: bool = False


GENERAL_SPECS: tuple[Spec, ...] = (
    Spec(
        "dry_run", "运行模式", "bool", SCOPE_RESTART, "dry_run",
        hint="空跑＝只记录不派发。切到实跑会真实下载 —— 需重启，因为派发器与"
             "完成监听器在启动时固化此值，运行中改一半会造成状态不一致。",
        labels=("空跑（不实际下载）", "实跑（会真实派发）"),
    ),
    Spec(
        "poll_interval_minutes", "完成监听轮询间隔（分钟）", "int", SCOPE_HOT,
        "poll_interval_minutes",
        hint=f"热生效：保存后立即重排 completion_poll 任务。下限 {MIN_POLL_MINUTES} 分钟。",
        lo=MIN_POLL_MINUTES, hi=1440,
    ),
    Spec(
        "log_level", "日志级别", "str", SCOPE_RESTART, "log_level",
        hint="需重启：日志管线在启动时装配。",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    Spec(
        "cookie_secure", "Cookie Secure 标志", "bool", SCOPE_HOT,
        "web.cookie_secure",
        hint="热生效。走 HTTPS / Tailscale 时应开启；纯内网 HTTP 必须关闭，"
             "否则浏览器不回传 cookie，表现为登录后立刻被踢回登录页。",
        labels=("开启（HTTPS 环境）", "关闭（内网 HTTP）"),
    ),
    # 站点熔断：search_round 每轮从 config 实时读阈值，改了下一轮即生效。
    Spec(
        "circuit_fail_threshold", "熔断触发：连续失败次数", "int", SCOPE_HOT,
        "search.circuit_fail_threshold",
        hint="热生效。某站连续失败达此次数即打开熔断，熔断期内整轮跳过该站。"
             "PT 站对重试风暴敏感，不建议调高。",
        lo=1, hi=100,
    ),
    Spec(
        "circuit_open_minutes", "熔断持续时长（分钟）", "int", SCOPE_HOT,
        "search.circuit_open_minutes",
        hint="热生效。熔断到期自动关闭，下一轮恢复对该站的请求。",
        lo=1, hi=1440,
    ),
    # v0.6 调度间隔：三个定时任务各自独立，保存后由 apply_hot 重排对应 job。
    Spec(
        "rss_interval_minutes", "RSS 捕获间隔（分钟）", "int", SCOPE_HOT,
        "search.rss_interval_minutes",
        hint=f"热生效：保存后立即重排 rss_capture 任务。下限 {MIN_POLL_MINUTES} 分钟，"
             "防止误配把 PT 站打爆触发风控。",
        lo=MIN_POLL_MINUTES, hi=1440,
    ),
    Spec(
        "subscription_scan_minutes", "订阅主动搜索间隔（分钟）", "int", SCOPE_HOT,
        "search.subscription_scan_minutes",
        hint="热生效：保存后立即重排 subscription_search 任务。"
             "单条订阅的实际搜索频率另受其自身回退间隔约束。",
        lo=1, hi=1440,
    ),
    Spec(
        "auth_check_hours", "站点认证巡检间隔（小时）", "int", SCOPE_HOT,
        "search.auth_check_hours",
        hint="热生效：保存后立即重排 site_auth_check 任务。",
        lo=1, hi=720,
    ),
    Spec(
        "qb_host", "qBittorrent 主机", "str", SCOPE_RESTART, "qbittorrent.host",
        hint="需重启：qb 客户端在启动时创建并验证连通性。",
        min_len=1,
    ),
    Spec(
        "qb_port", "qBittorrent 端口", "int", SCOPE_RESTART, "qbittorrent.port",
        hint="需重启：同上。", lo=1, hi=65535,
    ),
    Spec(
        "qb_save_path", "种子保存路径", "str", SCOPE_HOT, "qbittorrent.save_path",
        hint="热生效：派发时实时读取。必须是 qb 所在主机能写到的路径。",
        min_len=1,
    ),
    Spec(
        "qb_category", "qb 分类", "str", SCOPE_HOT, "qbittorrent.category",
        hint="热生效。",
    ),
    Spec(
        "qb_tag", "qb 标签", "str", SCOPE_HOT, "qbittorrent.tag",
        hint="热生效。",
    ),
    Spec(
        "qb_api_key", "qBittorrent API Key", "secret", SCOPE_RESTART,
        "qbittorrent.api_key",
        hint="只写不读：保存后 Fernet 加密入库，界面永不回显。留空表示不修改。"
             "需重启：客户端在启动时用 Key 认证。",
    ),
)


#: 归档链路目录。全部需重启：归档/校验/回收模块启动时按这些路径建目录并
#: 校验可写性，运行中换路径会让半截文件落在旧目录。
PATHS_SPECS: tuple[Spec, ...] = (
    Spec(
        "scratch_inbox", "刮削收件箱", "str", SCOPE_RESTART, "paths.scratch_inbox",
        hint="需重启。完成的种子 cp 到此处供 MusicTag Web 刮削，不动原种以保护做种。",
        min_len=1,
    ),
    Spec(
        "library_music", "音乐资料库", "str", SCOPE_RESTART, "paths.library_music",
        hint="需重启。归档终点（容器内视角）。", min_len=1,
    ),
    Spec(
        "library_audiobook", "有声书资料库", "str", SCOPE_RESTART,
        "paths.library_audiobook",
        hint="需重启。有声书归档终点，Audiobookshelf 应挂载同一目录。", min_len=1,
    ),
    Spec(
        "recycle_dir", "回收站目录", "str", SCOPE_RESTART, "paths.recycle_dir",
        hint="需重启。升级替换时旧版本移入此处，到期后清理。", min_len=1,
    ),
    Spec(
        "quarantine_dir", "隔离区目录", "str", SCOPE_RESTART, "paths.quarantine_dir",
        hint="需重启。校验不通过的内容移入此处等待人工处理。", min_len=1,
    ),
    Spec(
        "recycle_retention_days", "回收站保留天数", "int", SCOPE_RESTART,
        "paths.recycle_retention_days",
        hint="需重启：清理任务在启动时按此值注册。0 表示不自动清理。",
        lo=0, hi=3650,
    ),
)

#: 完成后文件校验。全部热生效：校验器在每个任务完成时实时读 config。
VALIDATION_SPECS: tuple[Spec, ...] = (
    Spec(
        "validation_enabled", "启用校验", "bool", SCOPE_HOT, "validation.enabled",
        hint="热生效。关闭后所有完成内容直接进入归档，不做假无损 / 空包检查。",
    ),
    Spec(
        "allowed_exts", "允许的音频扩展名", "list", SCOPE_HOT, "validation.allowed_exts",
        hint="热生效。逗号分隔，不含点号。种子内没有任何匹配文件视为空包。",
    ),
    Spec(
        "min_audio_files", "最少音频文件数", "int", SCOPE_HOT, "validation.min_audio_files",
        hint="热生效。低于此数视为空包。", lo=1, hi=1000,
    ),
    Spec(
        "fake_lossless_check", "假无损检测", "bool", SCOPE_HOT,
        "validation.fake_lossless_check",
        hint="热生效。需配置 auCDtect 路径才会做频谱检测，否则只做容器层检查。",
    ),
    Spec(
        "aucdtect_path", "auCDtect 可执行文件", "str", SCOPE_HOT, "validation.aucdtect_path",
        hint="热生效。留空则跳过频谱检测。",
    ),
    Spec(
        "validation_on_fail", "校验失败处理", "str", SCOPE_HOT, "validation.on_fail",
        hint="热生效。quarantine＝移入隔离区等人工处理；pass_through＝记录事件但照常归档。",
        choices=("quarantine", "pass_through"),
    ),
)

#: 刮削联动。全部热生效：webhook 在每次归档完成时实时读 config。
SCRAPE_SPECS: tuple[Spec, ...] = (
    Spec(
        "mtw_webhook_url", "MusicTag Web 回调地址", "str", SCOPE_HOT,
        "scrape.mtw_webhook_url",
        hint="热生效。留空则不通知 MTW。",
    ),
    Spec(
        "abs_url", "Audiobookshelf 地址", "str", SCOPE_HOT, "scrape.abs_url",
        hint="热生效。完整 http(s) 地址；留空则不触发 ABS 扫描。",
        require_url=True,
    ),
    Spec(
        "abs_library_id", "ABS 资料库 ID", "str", SCOPE_HOT, "scrape.abs_library_id",
        hint="热生效。",
    ),
    Spec(
        "abs_token", "ABS API Token", "secret", SCOPE_HOT, "scrape.abs_token",
        hint="只写不读：保存后 Fernet 加密入库，界面永不回显。留空表示不修改。",
    ),
    Spec(
        "scrape_timeout_seconds", "刮削请求超时（秒）", "int", SCOPE_HOT,
        "scrape.timeout_seconds",
        hint="热生效。", lo=1, hi=300,
    ),
)

#: 做种 / H&R。轮询间隔需重启（调度任务启动时注册），其余由轮询实时读取。
SEEDING_SPECS: tuple[Spec, ...] = (
    Spec(
        "seeding_enabled", "启用做种管理", "bool", SCOPE_HOT, "seeding.enabled",
        hint="热生效。关闭后不再跟踪做种时长与分享率，也不会暂停任何种子。",
    ),
    Spec(
        "seeding_check_interval_minutes", "做种检查间隔（分钟）", "int", SCOPE_RESTART,
        "seeding.check_interval_minutes",
        hint="需重启：轮询任务在启动时注册。", lo=1, hi=1440,
    ),
    Spec(
        "default_min_seed_hours", "默认最少做种时长（小时）", "int", SCOPE_HOT,
        "seeding.default_min_seed_hours",
        hint="热生效。站点未单独设置时使用。", lo=0, hi=8760,
    ),
    Spec(
        "default_min_ratio", "默认最低分享率", "float", SCOPE_HOT,
        "seeding.default_min_ratio",
        hint="热生效。站点未单独设置时使用。0 表示不要求。", lo=0, hi=100,
    ),
    Spec(
        "seeding_on_satisfied", "达标后动作", "str", SCOPE_HOT, "seeding.on_satisfied",
        hint="热生效。keep＝继续做种；pause＝暂停种子释放上行。",
        choices=("keep", "pause"),
    ),
)

#: 外部推送。全部热生效：推送器在每次 emit 时实时读 config。
NOTIFY_SPECS: tuple[Spec, ...] = (
    Spec(
        "wecom_webhook", "企业微信机器人 Webhook", "secret", SCOPE_HOT,
        "notify.wecom_webhook",
        hint="只写不读：地址含 key，整条当凭据加密存储。留空表示不修改。",
    ),
    Spec(
        "telegram_bot_token", "Telegram Bot Token", "secret", SCOPE_HOT,
        "notify.telegram_bot_token",
        hint="只写不读：加密存储，界面永不回显。留空表示不修改。",
    ),
    Spec(
        "telegram_chat_id", "Telegram Chat ID", "str", SCOPE_HOT, "notify.telegram_chat_id",
        hint="热生效。",
    ),
    Spec(
        "generic_webhook_url", "通用 Webhook 地址", "str", SCOPE_HOT,
        "notify.generic_webhook_url",
        hint="热生效。POST JSON 事件体；留空则不推送。",
    ),
    Spec(
        "notify_kinds", "推送的事件种类", "list", SCOPE_HOT, "notify.kinds",
        hint="热生效。逗号分隔，可选：" + "、".join(NOTIFY_KINDS) + "。留空＝全部不推。",
        choices=NOTIFY_KINDS,
    ),
)


def credential_status(config) -> dict:
    """计算 qb 与各站点的凭据配置状态，供界面展示。

    config.sites 在启动时已由 registry_store.load_site_configs 从 sites 表回填，
    站点热重建（rebuild_adapters）也会同步更新它，故这里读到的就是 DB 的现状。

    为什么按站点类型判断而非笼统的 `rss_url or api_key`：
    原实现对 NexusPHP 站实际测的是 rss_url 有无（与 passkey 无关），
    对 mteam 也不对（mteam 必需 api_key，有 rss_url 但没 api_key 仍不可用）。
    界面据此显示「缺什么」，而不是一个含糊的 ✗。
    """
    from .adapters.registry import site_credential_kinds

    sites = []
    for s in config.sites:
        needs = site_credential_kinds(s.name)
        items = []
        ready = True
        for field, required in needs.items():
            has = bool(getattr(s, field, ""))
            items.append({
                "field": field,
                "label": _CRED_LABELS.get(field, field),
                "required": required,
                "configured": has,
            })
            if required and not has:
                ready = False
        sites.append({
            "name": s.name,
            "enabled": s.enabled,
            "ready": ready,
            # 键名用 creds 而非 items：items 与 dict.items() 撞名，
            # Jinja2 的 s.items 会取到内置方法而非本列表（属性访问
            # 优先于下标访问），模板渲染时抛 TypeError。
            "creds": items,
            "unknown": not needs,   # 未注册站点，无法判断凭据需求
        })
    return {
        "qb_api_key": bool(config.qbittorrent.api_key),
        "abs_token": bool(config.scrape.abs_token),
        "wecom_webhook": bool(config.notify.wecom_webhook),
        "telegram_bot_token": bool(config.notify.telegram_bot_token),
        "sites": sites,
    }


def all_specs(config) -> tuple[Spec, ...]:
    """全部可编辑设置项。config 参数保留（历史签名），v0.6 起站点组已迁到 sites 表。"""
    return (
        GENERAL_SPECS + PATHS_SPECS + VALIDATION_SPECS + SCRAPE_SPECS
        + SEEDING_SPECS + NOTIFY_SPECS
    )


# ────────────────────────────────────────────────────────────
# 校验与编解码
# ────────────────────────────────────────────────────────────

def _coerce(spec: Spec, raw: Any) -> Any:
    """把表单原始值校验并转换成 Python 值。非法则抛 SettingsError。"""
    if spec.kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "on", "yes")

    if spec.kind in ("int", "float"):
        text = str(raw or "").strip()
        if not text and spec.nullable:
            return None
        try:
            v = int(text) if spec.kind == "int" else float(text)
        except (TypeError, ValueError):
            unit = "整数" if spec.kind == "int" else "数字"
            raise SettingsError(f"{spec.label} 必须是{unit}") from None
        if spec.kind == "float" and math.isnan(v):   # NaN 能通过 float() 但没有意义
            raise SettingsError(f"{spec.label} 必须是数字")
        if spec.lo is not None and v < spec.lo:
            raise SettingsError(f"{spec.label} 不能小于 {_num(spec.lo)}")
        if spec.hi is not None and v > spec.hi:
            raise SettingsError(f"{spec.label} 不能大于 {_num(spec.hi)}")
        return v

    if spec.kind == "list":
        # 逗号分隔；去空白与空项。choices 存在时逐项校验，
        # 否则 notify.kinds 填个拼错的事件名会被静默忽略、永远收不到推送。
        parts = [p.strip() for p in str(raw or "").split(",")]
        items = [p for p in parts if p]
        if spec.choices:
            bad = [p for p in items if p not in spec.choices]
            if bad:
                raise SettingsError(
                    f"{spec.label} 含无效项 {', '.join(bad)}；"
                    f"可选：{' / '.join(spec.choices)}"
                )
        return items

    # str / secret
    v = str(raw or "").strip()
    if spec.min_len and len(v) < spec.min_len:
        raise SettingsError(f"{spec.label} 不能为空")
    if spec.choices and v not in spec.choices:
        raise SettingsError(f"{spec.label} 只能是 {' / '.join(spec.choices)}")
    if spec.require_url and v:
        from urllib.parse import urlparse

        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SettingsError(
                f"{spec.label} 必须是完整的 http(s) 地址"
            )
    return v


def _num(x: float) -> str:
    """范围提示里的数字：整数值不带 .0。"""
    return str(int(x)) if float(x).is_integer() else str(x)


def encode_for_storage(spec: Spec, value: Any) -> str:
    """转成入库字符串。secret 走 Fernet 加密，明文不落盘。

    可空数值的 None 存为空串（而非删行）：这样「使用者明确清除了站点覆盖」
    与「从未设置过」在库里可区分，hydrate 时也能把 YAML 里的值覆盖回 None。
    list 存逗号串，app_settings 的 value 列是纯文本，不引入 JSON 编码。
    """
    if spec.kind == "bool":
        return "1" if value else "0"
    if spec.kind == "secret":
        from .crypto import encrypt_value
        return encrypt_value(str(value))
    if spec.kind == "list":
        return ",".join(str(v) for v in value)
    if value is None:
        return ""
    return str(value)


def _decode_stored(spec: Spec, stored: str) -> Any:
    """入库字符串还原为 Python 值。"""
    if spec.kind == "bool":
        return stored == "1"
    if spec.kind in ("int", "float"):
        if stored == "" and spec.nullable:
            return None
        return int(stored) if spec.kind == "int" else float(stored)
    if spec.kind == "list":
        return [p.strip() for p in stored.split(",") if p.strip()]
    if spec.kind == "secret":
        from .crypto import decrypt_value
        return decrypt_value(stored)
    return stored


def validate_form(
    specs: tuple[Spec, ...], form
) -> tuple[dict[str, Any], dict[str, str]]:
    """校验整个表单。返回 (解析后的值, 入库编码)。

    **未提交的键一律视为「不修改」**，绝不按缺省值处理。这条对 dry_run
    尤其重要：若把「字段缺失」当成 False，任何漏带该字段的请求都会
    静默关掉空跑保护、开启真实下载。secret 留空同样表示不修改。

    任一键校验失败即抛 SettingsError，调用方据此整体放弃保存 ——
    不做半截写入，避免配置处于自相矛盾的中间态。
    """
    resolved: dict[str, Any] = {}
    encoded: dict[str, str] = {}
    for spec in specs:
        if spec.key not in form:
            continue
        raw = form.get(spec.key)
        if spec.kind == "secret" and not str(raw or "").strip():
            continue
        value = _coerce(spec, raw)
        resolved[spec.key] = value
        encoded[spec.key] = encode_for_storage(spec, value)
    return resolved, encoded


# ────────────────────────────────────────────────────────────
# 读写覆盖层
# ────────────────────────────────────────────────────────────

async def load_overrides(db) -> dict[str, str]:
    """读取全部覆盖项（排除 __ 前缀的内部标记键）。"""
    async with db.execute("SELECT key, value FROM app_settings") as cur:
        rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows if not r["key"].startswith("__")}


async def save_override(db, key: str, value: str) -> None:
    await db.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, iso_utc()),
    )
    await db.commit()


async def set_restart_pending(db, pending: bool) -> None:
    if pending:
        await save_override(db, RESTART_FLAG_KEY, "1")
    else:
        await db.execute("DELETE FROM app_settings WHERE key = ?", (RESTART_FLAG_KEY,))
        await db.commit()


async def is_restart_pending(db) -> bool:
    async with db.execute(
        "SELECT value FROM app_settings WHERE key = ?", (RESTART_FLAG_KEY,)
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row["value"] == "1")


# ────────────────────────────────────────────────────────────
# 应用到 config / 运行时
# ────────────────────────────────────────────────────────────

def _resolve(config, path: str):
    """按点分路径定位 (父对象, 末段属性名)。"""
    parts = path.split(".")
    obj = config
    for p in parts[:-1]:
        obj = getattr(obj, p)
    return obj, parts[-1]


def _find_site(config, name: str):
    """按名取 config.sites 中的站点（页面凭据状态等只读场景用）。"""
    for s in config.sites:
        if s.name == name:
            return s
    return None


def _apply_one(config, spec: Spec, value: Any) -> bool:
    """把单个值写入 config。返回是否命中目标。"""
    obj, attr = _resolve(config, spec.path)
    setattr(obj, attr, value)
    return True


def _spec_by_key(specs: tuple[Spec, ...], key: str) -> Spec | None:
    for s in specs:
        if s.key == key:
            return s
    return None


async def hydrate_config(db, config, specs: tuple[Spec, ...]) -> dict[str, Any]:
    """启动时：读覆盖层 → 解密 secret → 合并进 config。

    必须在 logger 装配与 runner/adapter 构建**之前**调用，
    否则 log_level 覆盖不生效、适配器仍按旧的站点开关构建。

    单项解码失败只记日志并沿用 YAML 值，不阻断启动 ——
    例如换了密钥文件导致旧 token 解不开，此时应能起来让使用者重新填，
    而不是陷入「配置坏了所以起不来，起不来所以改不了配置」的死锁。
    """
    raw = await load_overrides(db)
    resolved: dict[str, Any] = {}
    for spec in specs:
        if spec.key not in raw:
            continue
        try:
            resolved[spec.key] = _decode_stored(spec, raw[spec.key])
        except Exception as e:  # noqa: BLE001 - 单项坏值不得阻断启动
            logger.error(
                f"设置 {spec.key} 解码失败，沿用 YAML 值: {type(e).__name__}: {e}"
            )

    applied = []
    for key, value in resolved.items():
        spec = _spec_by_key(specs, key)
        if spec is None:
            continue
        try:
            if _apply_one(config, spec, value):
                applied.append(key)
        except Exception as e:  # noqa: BLE001
            logger.error(f"应用设置 {key} 失败，沿用 YAML 值: {e}")

    if applied:
        logger.info(f"已从覆盖层加载 {len(applied)} 项设置: {applied}")
    return resolved


#: 热生效的间隔类设置键 → (job id, 触发器单位, 安全下限)。
#: 下限必须与 scheduler.build_scheduler 的钳制同值。
_INTERVAL_JOBS: dict[str, tuple[str, str, int]] = {
    "poll_interval_minutes": ("completion_poll", "minutes", MIN_POLL_MINUTES),
    "rss_interval_minutes": ("rss_capture", "minutes", MIN_POLL_MINUTES),
    "subscription_scan_minutes": ("subscription_search", "minutes", 1),
    "auth_check_hours": ("site_auth_check", "hours", 1),
}


def _reschedule(app, key: str, value: int) -> None:
    """热更新调度间隔。APScheduler 支持对已注册任务重排触发器。"""
    from apscheduler.triggers.interval import IntervalTrigger

    job_id, unit, floor = _INTERVAL_JOBS[key]
    sched = getattr(app.state, "scheduler", None)
    if sched is None:
        logger.warning(f"调度器尚未就绪，{key} 改动需重启生效")
        return
    interval = max(int(value), floor)
    try:
        sched.reschedule_job(job_id, trigger=IntervalTrigger(**{unit: interval}))
        logger.info(f"调度任务 {job_id} 间隔已热更新为 {interval} {unit}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"重排调度任务 {job_id} 失败，该项需重启才生效: {e}")


def _rebuild_adapters_sync(app, config) -> None:
    """按 config.sites 重建适配器（无 DB 访问；供 rebuild_adapters 与测试直接调用）。

    runner.adapters 是适配器列表的唯一引用点，_run_inner 每轮读它，
    故整体替换即可让下一轮生效，无需重启。
    """
    from .adapters.registry import build_adapters

    runner = getattr(app.state, "runner", None)
    if runner is None:
        logger.warning("runner 尚未就绪，站点改动需重启生效")
        return
    runner.adapters = build_adapters(config.sites)
    logger.info(f"适配器已按新配置重建: {list(runner.adapters)}")


#: 适配器 / 下载器重建互斥：/sites 页连续保存两站时，两次重建若交错，
#: 后完成的那次可能拿着更旧的 DB 快照覆盖前者。锁内先读 DB 再替换，保证单调。
_REBUILD_LOCK = asyncio.Lock()


async def rebuild_adapters(app) -> None:
    """从 sites 表重新加载站点配置（含解密凭据）并重建适配器（v0.6）。

    同时回填 app.state.config.sites：做种轮询的站点 H&R 规则、/sites 页的凭据
    状态、派发器的 adapter_lookup 等仍从 config.sites 读，必须与 DB 同步。
    """
    from .registry_store import load_site_configs

    db = getattr(app.state, "db", None)
    config = getattr(app.state, "config", None)
    if db is None or config is None:
        logger.warning("app.state 尚未就绪，站点改动需重启生效")
        return
    async with _REBUILD_LOCK:
        config.sites = await load_site_configs(db)
        _rebuild_adapters_sync(app, config)


#: 旧名兼容
_rebuild_adapters = rebuild_adapters


async def rebuild_downloaders(app) -> None:
    """从 downloaders 表重新加载并替换下载器注册表（v0.6）。

    registry.replace 原地替换：dispatcher / monitor / seeding_monitor 持有的是同一个
    registry 引用，下一轮自然拿到新配置。app.state.qb_client 仍保持为
    「默认下载器客户端」别名，供 /review 单任务派发等旧路径使用。
    """
    from .registry_store import load_downloader_configs

    db = getattr(app.state, "db", None)
    registry = getattr(app.state, "downloaders", None)
    if db is None or registry is None:
        logger.warning("下载器注册表尚未就绪，改动需重启生效")
        return
    async with _REBUILD_LOCK:
        registry.replace(await load_downloader_configs(db))
        default = registry.default()
        app.state.qb_client = default.client if default else None
        runner = getattr(app.state, "runner", None)
        if runner is not None and hasattr(runner, "on_downloaders_rebuilt"):
            runner.on_downloaders_rebuilt()
        logger.info(
            f"下载器注册表已重建: {[c.id for c in registry.configs]}，"
            f"默认={default.id if default else None}"
        )


async def apply_hot(app, resolved: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把已入库的改动应用到运行时。返回 (热生效的键, 需重启的键)。

    需重启的键**只收集不应用** —— 这是刻意的：半应用（比如换了 dry_run
    但 dispatcher 还持旧值）比完全不应用更危险。
    """
    config = app.state.config
    specs = all_specs(config)
    hot: list[str] = []
    restart: list[str] = []

    for key, value in resolved.items():
        spec = _spec_by_key(specs, key)
        if spec is None:
            logger.warning(f"未知设置键，已忽略: {key}")
            continue
        if spec.scope == SCOPE_RESTART:
            restart.append(key)
            continue
        try:
            if not _apply_one(config, spec, value):
                logger.warning(f"设置 {key} 对应对象不存在，跳过")
                continue
        except Exception as e:  # noqa: BLE001
            logger.error(f"应用设置 {key} 失败: {e}")
            continue

        hot.append(key)
        if key in _INTERVAL_JOBS:
            _reschedule(app, key, value)

    return hot, restart
