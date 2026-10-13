"""设置路由（v0.3+）：界面可编辑的运行时配置。

设计约束见 settings_store 模块文档。本文件只负责 HTTP 层：
表单解析、权限、保存、把生效范围如实反馈给使用者。

七类写端点，各自只接受自己那组字段：
    POST /settings/general      运行参数（模式 / 轮询 / 日志 / cookie）
    POST /settings/qbittorrent  qb 连接与落盘
    POST /settings/paths        归档链路目录（全部需重启）
    POST /settings/validation   完成后文件校验
    POST /settings/scrape       刮削联动（MTW / ABS）
    POST /settings/seeding      做种 / H&R 全局默认
    POST /settings/notify       外部推送渠道
    POST /settings/notify/test  用当前生效的推送配置发一条测试通知

站点开关 / 凭据 / 站点 H&R 规则（原 POST /settings/sites）已于 v0.6 迁到站点中心
（sites 表 + /sites 页），设置页不再承载。

均要求管理员 + CSRF。改配置影响面大于改订阅项，故比页面版写操作
多一层 require_admin —— 普通用户能审片，但不该能关掉空跑保护。

推送渠道的热重建为什么在这里而非 settings_store.apply_hot
----------------------------------------------------------
apply_hot 目前只认两种运行时副作用（重排调度 / 重建适配器），没有通用回调。
推送 sink 在构造时固化 URL / token，apply_hot 改完 config 后仍需整体重建。
本模块在 _save 成功且有热生效键时调 rebuild_push_sinks —— 与适配器重建的效果
一致（保存即生效），但不动 settings_store（那是全中心共享文件，这轮多人并行）。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from .. import settings_store as store
from ..notify import build_push_sinks, rebuild_push_sinks
from .deps import require_admin_csrf

router = APIRouter(tags=["settings"])

SETTINGS_PATH = "/settings"

#: 字段分组。键集合互斥，避免一个表单能改到另一组的值。
GENERAL_KEYS = frozenset({
    "dry_run", "poll_interval_minutes", "log_level", "cookie_secure",
    "circuit_fail_threshold", "circuit_open_minutes",
    "rss_interval_minutes", "subscription_scan_minutes", "auth_check_hours",
})
QB_KEYS = frozenset({
    "qb_host", "qb_port", "qb_save_path", "qb_category", "qb_tag", "qb_api_key",
})
PATHS_KEYS = frozenset(s.key for s in store.PATHS_SPECS)
VALIDATION_KEYS = frozenset(s.key for s in store.VALIDATION_SPECS)
SCRAPE_KEYS = frozenset(s.key for s in store.SCRAPE_SPECS)
SEEDING_KEYS = frozenset(s.key for s in store.SEEDING_SPECS)
NOTIFY_KEYS = frozenset(s.key for s in store.NOTIFY_SPECS)

#: action -> (标题, 说明, 键集合, flash key)
#: 渲染与保存都从这张表取字段，避免「保存端点收得到、渲染却漏掉」的漂移
#: （曾导致凭据输入框不显示）。flash key 对应 routes_pages._FLASH_OK。
_GROUPS: tuple[tuple[str, str, str, frozenset[str], str], ...] = (
    ("/settings/general", "运行参数",
     "dry_run 是安全闸：切到实跑会真实下载并占用 PT 站配额。",
     GENERAL_KEYS, "saved_general"),
    ("/settings/qbittorrent", "qBittorrent",
     "保存路径必须是 qb 所在主机能写到的路径（容器内视角）。",
     QB_KEYS, "saved_qb"),
    ("/settings/paths", "归档路径",
     ("全部需重启：归档 / 校验 / 回收模块启动时按这些路径建目录并校验可写性。"
      "全部为容器内视角。"),
     PATHS_KEYS, "saved_paths"),
    ("/settings/validation", "完成校验",
     "对每个完成的种子做扩展名 / 空包 / 假无损检查，不通过时按「校验失败处理」执行。",
     VALIDATION_KEYS, "saved_validation"),
    ("/settings/scrape", "刮削联动",
     "归档完成后通知 MusicTag Web 与 Audiobookshelf 。地址留空则不联动。",
     SCRAPE_KEYS, "saved_scrape"),
    ("/settings/seeding", "做种与 H&R",
     "全局默认规则；站点中心（/sites）可逐站覆盖。达标前不会对种子做任何动作。",
     SEEDING_KEYS, "saved_seeding"),
    ("/settings/notify", "通知推送",
     "事件始终写入本地事件流；这里配置的是额外的外部推送渠道。",
     NOTIFY_KEYS, "saved_notify"),
)

#: 保存成功后的 flash key（对应 routes_pages._FLASH_OK 的条目）
_OK_KEY = {action: ok for action, _, _, _, ok in _GROUPS}

#: 密钥文件不可用时的 flash 文案。对应 routes_pages._FLASH_ERR 的 secrets_key。
_ERR_SECRETS_KEY = "secrets_key"

#: 通知测试的 flash key（对应 routes_pages._FLASH_OK / _FLASH_ERR）
_OK_NOTIFY_TEST = "notify_test"
_ERR_NOTIFY_NONE = "notify_none"
_ERR_NOTIFY_TEST_FAILED = "notify_test_failed"

# 管理员 + CSRF 双校验已上提到 deps.require_admin_csrf，与隔离丢弃 / 回收清除 /
# 删连载 / 清理事件共用同一件。


def _flatten(form) -> dict[str, str]:
    """把多值 FormData 压平成 {key: 最后一个值}。

    界面的 bool 控件是 **select 而非 checkbox**（value 恒为 "1"/"0"）。
    之所以不用 checkbox：取消勾选时浏览器根本不发送该字段，而本模块把
    「未提交」视为「不修改」，会让取消勾选被静默忽略 —— 使用者点了取消、
    页面提示保存成功、开关实际没动。select 永远提交一个值，无此问题。

    仍取列表尾元素而非 form.get()：显式声明取值语义，不依赖框架默认行为。

    ⚠️ 改这里的 bool 编码约定必须同步改测试数据。v3 就因文档留着
    checkbox 旧说法、测试照旧写法，导致 dry_run 提交成列表，
    尾元素 "1" 即空跑，等于没改。
    """
    out: dict[str, str] = {}
    for key in form:
        vals = form.getlist(key)
        out[key] = vals[-1] if vals else ""
    return out


def _group_specs(specs: tuple[store.Spec, ...], keys: frozenset[str]) -> list[store.Spec]:
    """按组筛字段。"""
    return [s for s in specs if s.key in keys]


def _specs_for(config, action: str) -> tuple[store.Spec, ...]:
    """按端点取该组的字段声明。"""
    specs = store.all_specs(config)
    for act, _, _, keys, _ in _GROUPS:
        if act == action:
            return tuple(_group_specs(specs, keys))
    return ()


async def _save(request: Request, action: str, form, *, on_hot=None) -> RedirectResponse:
    """校验 -> 落库 -> 应用 -> 反馈。任一步失败都不留下半截状态。

    on_hot：有热生效键时调用的钩子 `on_hot(hot_keys)`，用于 apply_hot 不认识的
    运行时副作用（如重建推送 sink）。钩子异常不影响保存结果：配置已落库且
    已应用到 config，重启后必然生效，故只记日志。
    """
    db = request.app.state.db
    config = request.app.state.config
    specs = _specs_for(config, action)

    flat = _flatten(form)

    # ── 校验（全部通过才写库；未提交的键视为不修改）──
    try:
        resolved, encoded = store.validate_form(specs, flat)
    except store.SettingsError as e:
        logger.warning(f"设置保存被拒（{action}）: {e}")
        return RedirectResponse(
            f"{SETTINGS_PATH}?err={quote(str(e))}", status_code=303,
        )
    except (OSError, RuntimeError) as e:
        # secret 的加密在 validate_form 内完成；密钥文件缺失 / 不可读时
        # 抛 FileNotFoundError 或 RuntimeError（见 crypto._key_path）。这是部署
        # 问题而非输入问题，要给出可操作的提示，不能让使用者吃一个 500。
        logger.error(f"设置保存失败（{action}）：密钥文件不可用: {e}")
        return RedirectResponse(
            f"{SETTINGS_PATH}?err={_ERR_SECRETS_KEY}", status_code=303,
        )

    if not resolved:
        return RedirectResponse(
            f"{SETTINGS_PATH}?err={quote('没有需要保存的改动')}", status_code=303,
        )

    # ── 落库（覆盖层）──
    for key, value in encoded.items():
        await store.save_override(db, key, value)

    # ── 应用到运行时：热项立即生效，需重启项只标记 ──
    hot, restart = await store.apply_hot(request.app, resolved)

    # 只在有需重启项时置标记；清除由下次启动统一负责
    # （无法逐项追溯某个键是否已被改回与 YAML 一致，故不在这里清）
    if restart:
        await store.set_restart_pending(db, True)

    if hot and on_hot is not None:
        try:
            on_hot(hot)
        except Exception as e:  # noqa: BLE001 - 副作用失败不影响已落库的保存
            logger.error(f"设置热生效钩子失败（{action}）: {e}")

    logger.info(
        f"设置已保存（{action}）: 热生效={hot} 需重启={restart} "
        f"by={getattr(request.state, 'user', {}).get('username', '?')}"
    )

    parts = []
    if hot:
        parts.append(f"{len(hot)} 项已立即生效")
    if restart:
        parts.append(f"{len(restart)} 项需重启后生效")
    msg = "，".join(parts) or "已保存"
    ok_key = _OK_KEY.get(action, "saved")
    return RedirectResponse(
        f"{SETTINGS_PATH}?ok={ok_key}&detail={quote(msg)}", status_code=303,
    )


# ── 端点 ────────────────────────────────────────────────────

@router.post("/settings/general")
async def save_general(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/general", await request.form())


@router.post("/settings/qbittorrent")
async def save_qbittorrent(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/qbittorrent", await request.form())


@router.post("/settings/paths")
async def save_paths(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/paths", await request.form())


@router.post("/settings/validation")
async def save_validation(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/validation", await request.form())


@router.post("/settings/scrape")
async def save_scrape(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/scrape", await request.form())


@router.post("/settings/seeding")
async def save_seeding(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    return await _save(request, "/settings/seeding", await request.form())


@router.post("/settings/notify")
async def save_notify(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    """保存通知组；有热生效键则按新 config 整体重建推送 sink。

    通知组全部是 hot 键，故保存成功必然触发重建。不区分改的是哪一键：
    重建三个对象的成本可忽略，而「只改 kinds 要不要重建」这类分支最容易漏。
    """
    app = request.app

    def _rebuild(_hot_keys: list[str]) -> None:
        rebuild_push_sinks(app, app.state.config.notify)

    return await _save(request, "/settings/notify", await request.form(), on_hot=_rebuild)


@router.post("/settings/notify/test")
async def test_notify(
    request: Request,
    _user: dict = Depends(require_admin_csrf),
):
    """用**当前生效**的通知配置逐渠道发一条测试通知。

    不读表单：测的是「保存后实际在用的配置」，而非输入框里尚未保存的值 ——
    否则「测试成功」和「实际收得到」可能是两回事。每次现建 sink，不动
    notifier 里正在服务的那一组。
    结果文案由 send_test 保证不含 URL（含 key / token），再经 quote 进查询串。
    """
    config = request.app.state.config
    sinks = build_push_sinks(config.notify)
    if not sinks:
        return RedirectResponse(f"{SETTINGS_PATH}?err={_ERR_NOTIFY_NONE}", status_code=303)

    results = [await s.send_test() for s in sinks]
    detail = "；".join(msg for _, msg in results)
    ok = all(success for success, _ in results)
    logger.info(
        f"通知测试 {'成功' if ok else '失败'}: {detail} "
        f"by={getattr(request.state, 'user', {}).get('username', '?')}"
    )
    key = f"ok={_OK_NOTIFY_TEST}" if ok else f"err={_ERR_NOTIFY_TEST_FAILED}"
    return RedirectResponse(
        f"{SETTINGS_PATH}?{key}&detail={quote(detail)}", status_code=303,
    )


# ── 供模板渲染用的字段描述 ──────────────────────────────────

def _effective(config, spec: store.Spec):
    """取该设置项**当前生效值**。

    secret 永不回显 —— 返回空串，模板据此渲染空输入框，
    留空即「不修改」。这是 settings 页既有约定（只写不读）。
    """
    if spec.kind == "secret":
        return ""
    obj, attr = store._resolve(config, spec.path)
    return getattr(obj, attr)


def _field(config, spec: store.Spec) -> dict:
    value = _effective(config, spec)
    f = {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind,
        "scope": spec.scope,
        "hot": spec.scope == store.SCOPE_HOT,
        "hint": spec.hint,
        "value": value,
    }
    if spec.kind == "bool":
        f["on_label"], f["off_label"] = spec.labels
        f["checked"] = bool(value)
    elif spec.kind in ("int", "float"):
        f["lo"], f["hi"] = spec.lo, spec.hi
        f["nullable"] = spec.nullable
        # 可空项的 None 渲染成空框，而不是字面 "None"
        f["text"] = "" if value is None else str(value)
        f["step"] = "1" if spec.kind == "int" else "any"
    elif spec.kind == "list":
        f["text"] = ", ".join(value or [])
        f["choices"] = list(spec.choices)
    elif spec.kind == "secret":
        f["placeholder"] = _secret_placeholder(config, spec)
    elif spec.kind == "str" and spec.choices:
        f["choices"] = list(spec.choices)
    elif spec.kind == "str":
        f["minlength"] = spec.min_len or None
        f["text"] = str(value)
    return f


def _secret_placeholder(config, spec: store.Spec) -> str:
    """secret 输入框的占位文案：告知当前是否已配置，但绝不回显值。"""
    if spec.key == "qb_api_key":
        has = bool(config.qbittorrent.api_key)
        return "留空表示不修改（当前已配置）" if has else "未配置 —— 实跑派发前必填"
    if spec.path:
        # 其余 secret（ABS token / 推送凭据）：path 指向解密后的明文字段
        obj, attr = store._resolve(config, spec.path)
        has = bool(getattr(obj, attr, ""))
        return "留空表示不修改（当前已配置）" if has else "未配置（可选）"
    return "留空表示不修改"


def build_settings_groups(config) -> list[dict]:
    """按 _GROUPS 构造模板渲染数据。

    模板不写死字段名，全部由这里驱动 —— 以后加设置项只需在
    settings_store 的 SPEC 里加一条，页面自动出现，不会出现
    「后端支持但界面没有」的漂移。
    """
    specs = store.all_specs(config)
    return [
        {
            "action": action,
            "title": title,
            "note": note,
            "fields": [_field(config, s) for s in _group_specs(specs, keys)],
        }
        for action, title, note, keys, _ in _GROUPS
    ]
