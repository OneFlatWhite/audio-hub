"""站点六态：未配置 → 未认证 → 认证失效 → 已禁用 → 熔断中 → 正常。

判定顺序即优先级：凭据都没有谈不上认证，认证没过谈不上启用，启用了才看
熔断与健康。/sites 与总览都走 site_overview，不再各算一套。

熔断与 last_error 作为**次级提示**始终随行渲染（即使主状态是「未认证」）：
主状态回答「这站现在能不能用」，次级提示回答「上一轮发生了什么」，两者独立。
"""

from __future__ import annotations

from .. import repo
from ..adapters.registry import resolve_site_name
from ..models import AuthStatus, SiteRecord
from ..timeutil import SITE_TZ, parse_iso, utcnow

#: 六态标识（模板按 state 分支，state_label / state_css 直接渲染）
STATE_UNCONFIGURED = "unconfigured"
STATE_UNAUTHENTICATED = "unauthenticated"
STATE_AUTH_FAILED = "auth_failed"
STATE_DISABLED = "disabled"
STATE_CIRCUIT_OPEN = "circuit_open"
STATE_OK = "ok"

#: state → (文案, style.css 里的 pill 修饰类)。
#: 颜色只有 ok / warn / err / muted 四档：high=绿、medium=橙黄、low=红、exclude=灰。
_STATE_VIEW: dict[str, tuple[str, str]] = {
    STATE_UNCONFIGURED: ("未配置", "exclude"),
    STATE_UNAUTHENTICATED: ("未认证", "medium"),
    STATE_AUTH_FAILED: ("认证失效", "low"),
    STATE_DISABLED: ("已禁用", "exclude"),
    STATE_CIRCUIT_OPEN: ("熔断中", "medium"),
    STATE_OK: ("正常", "high"),
}


def compute_state(
    *, enabled: bool, auth_status: str, has_credentials: bool, circuit_tripped: bool,
) -> str:
    """纯函数：四个输入 → 六态之一。"""
    status = str(getattr(auth_status, "value", auth_status) or AuthStatus.UNCONFIGURED.value)
    if status == AuthStatus.UNCONFIGURED.value:
        return STATE_UNAUTHENTICATED if has_credentials else STATE_UNCONFIGURED
    if status != AuthStatus.VALID.value:
        return STATE_AUTH_FAILED
    if not enabled:
        return STATE_DISABLED
    if circuit_tripped:
        return STATE_CIRCUIT_OPEN
    return STATE_OK


def state_view(state: str) -> tuple[str, str]:
    """state → (label, css)。未知值按「未配置」兜底，模板永远有东西可渲染。"""
    return _STATE_VIEW.get(state, _STATE_VIEW[STATE_UNCONFIGURED])


def _circuit(st: dict, now) -> tuple[bool, str | None, str | None]:
    """site_stats 行 → (是否熔断中, 熔断截止 ISO, 站点时区 HH:MM)。口径同 repo.is_circuit_open。"""
    raw = st.get("circuit_open_until")
    if not raw:
        return False, None, None
    until = parse_iso(raw)
    if until is None or until <= now:
        return False, None, None
    return True, raw, until.astimezone(SITE_TZ).strftime("%H:%M")


def _has_credentials(rec: SiteRecord | None, cfg) -> bool:
    """有任一凭据即算「已配置」：NexusPHP 看 Cookie / RSS，mteam 看 API Key。"""
    if cfg is not None and (cfg.cookie or cfg.rss_url or cfg.api_key):
        return True
    return bool(rec and (rec.has_cookie or rec.has_rss or rec.has_api_key))


def _fmt_local(dt) -> str | None:
    return dt.astimezone(SITE_TZ).strftime("%Y-%m-%d %H:%M") if dt else None


async def site_overview(db, config) -> list[dict]:
    """合并 sites 表、运行时 config.sites 与 site_stats，产出页面用的站点行。

    以 config.sites 为主序；enabled / auth_status 以 DB 记录为准（持久真相：巡检或
    外部写入后即使尚未 rebuild 也能如实显示），记录缺失时才退回 config。
    DB 里有而 config 里没有的站点追加在后（刚新建、尚未 rebuild 的瞬间）。
    返回字段兼容旧版 _site_status（name / enabled / healthy / circuit_* / 计数），
    新增 state / state_label / state_css / auth_* / user_info / adapter_type / has_* 等。
    """
    stats = await repo.list_site_stats(db)
    records = {r.id: r for r in await repo.list_sites(db)}
    now = utcnow()

    rows: list[dict] = []
    seen: set[str] = set()

    def _row(sid: str, cfg, rec: SiteRecord | None) -> dict:
        st = stats.get(sid, {})
        tripped, circuit_iso, until_local = _circuit(st, now)
        enabled = bool(rec.enabled) if rec is not None else bool(cfg is not None and cfg.enabled)
        auth_status = (rec.auth_status.value if rec is not None
                       else (cfg.auth_status if cfg is not None else AuthStatus.UNCONFIGURED.value))
        state = compute_state(
            enabled=enabled, auth_status=auth_status,
            has_credentials=_has_credentials(rec, cfg), circuit_tripped=tripped,
        )
        label, css = state_view(state)
        consecutive_fail = st.get("consecutive_fail", 0)
        return {
            "id": sid,
            "name": rec.name if rec and rec.name else sid,
            "adapter_type": rec.adapter_type if rec else "nexusphp",
            "enabled": enabled,
            "priority": rec.priority if rec else (cfg.priority if cfg is not None else 0),
            "state": state, "state_label": label, "state_css": css,
            "auth_status": auth_status,
            "auth_checked_at": rec.auth_checked_at if rec else None,
            "auth_checked_local": _fmt_local(rec.auth_checked_at) if rec else None,
            "auth_error": rec.auth_error if rec else None,
            "user_info": rec.user_info if rec else None,
            "last_rss_test": rec.last_rss_test if rec else None,
            "has_cookie": bool(rec and rec.has_cookie) or bool(cfg is not None and cfg.cookie),
            "has_rss": bool(rec and rec.has_rss) or bool(cfg is not None and cfg.rss_url),
            "has_api_key": bool(rec and rec.has_api_key) or bool(cfg is not None and cfg.api_key),
            "domain": (rec.domain if rec else None) or (cfg.domain if cfg is not None else ""),
            # ── 以下与旧版 _site_status 同名，总览与现有测试沿用 ──
            "healthy": state == STATE_OK and consecutive_fail == 0,
            "circuit_tripped": tripped,
            "circuit_open_until": circuit_iso,
            "circuit_until_local": until_local,
            "consecutive_fail": consecutive_fail,
            "last_success_at": st.get("last_success_at"),
            "last_error": st.get("last_error"),
            "last_count": st.get("last_count", 0),
            "total_requests": st.get("total_requests", 0),
            "total_errors": st.get("total_errors", 0),
        }

    for cfg in getattr(config, "sites", []) or []:
        sid = resolve_site_name(cfg.name)
        seen.add(sid)
        rows.append(_row(sid, cfg, records.get(sid)))
    for sid, rec in records.items():
        if sid not in seen:
            rows.append(_row(sid, None, rec))
    return rows
