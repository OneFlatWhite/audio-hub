#!/usr/bin/env python3
"""端到端冒烟检查：验证「测试绿」之外、浏览器真实会走的路径。

为什么需要它
------------
pytest 用 TestClient 做进程内调用，已有用例覆盖了路由与模板渲染，
但**没有逐个验证页面 HTML 里引用的静态资源真的能取回**。
`url_for('static', path='/style.css')` 若写错一个字符，测试照样全绿，
浏览器打开却是无样式裸页。这个脚本专门堵这个缝。

检查项
------
1. 完整用户流程：未登录被拦 -> /setup 建号 -> 自动登录 -> 十三个页面全 200
   （v0.2 六页 + v0.5 新增 /library /quarantine /series /seeding /events
   + v0.6 新增 /downloaders + v0.7 新增 /discover）
2. 从每个页面 HTML 里**提取实际引用的 /static/* URL**，逐个请求验证 200
   （不写死文件名，页面新增资源自动纳入检查）
3. CSRF meta 标签确实下发（前端 app.js 依赖它自动注入表单字段）
4. 写操作缺 CSRF 被拒（403），带 CSRF 通过
5. 登出后会话失效
6. /health 版本与 __version__ 一致，且等于本轮发布号（防 __init__ 忘改）

用法（WSL 内）：
    cd ~/audio-hub && .venv/bin/python scripts/smoke_check.py

结果落盘到 /mnt/d/Documents/audio-hub/smoke_report.txt，助手自行读取。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPORT = Path("/mnt/d/Documents/audio-hub/smoke_report.txt")
_lines: list[str] = []
_failures: list[str] = []


def log(msg: str = "") -> None:
    print(msg, flush=True)
    _lines.append(msg)


def check(ok: bool, label: str, detail: str = "") -> bool:
    """记录一项检查；失败则累计，不中断后续检查。"""
    if ok:
        log(f"  [PASS] {label}")
    else:
        log(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))
        _failures.append(label)
    return ok


def flush_report() -> None:
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"!! 报告写入失败: {e}", flush=True)


# ────────────────────────────────────────────────────────────
# 环境准备：必须在 import audio_hub.main 之前完成
# ────────────────────────────────────────────────────────────

def prepare_env(tmp: Path) -> None:
    """生成最小可启动的 dev 配置与密钥，写入环境变量。"""
    (tmp / "seeds").mkdir(exist_ok=True)
    db_path = tmp / "hub.db"
    key_path = tmp / "secrets.key"

    cfg = f"""dry_run: true
db_path: "{db_path.as_posix()}"
log_level: "WARNING"
poll_interval_minutes: 30

web:
  cookie_secure: false      # TestClient 走 http，Secure cookie 不会回传
  auth_request_limit: 100
  auth_window_minutes: 15

qbittorrent:
  host: "localhost"
  port: 8085
  api_key_encrypted: ""
  category: "audio-hub"
  tag: "hub"
  save_path: "{(tmp / 'seeds').as_posix()}"

sites:
  - name: "hhanclub"
    enabled: false
    rss_url: ""
"""
    (tmp / "config.yaml").write_text(cfg, encoding="utf-8")
    os.environ["AUDIO_HUB_CONFIG"] = str(tmp / "config.yaml")
    os.environ["AUDIO_HUB_SECRETS_KEY"] = str(key_path)

    # 用项目自身的密钥生成逻辑，避免自造格式
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from audio_hub.crypto import ensure_key_file
    ensure_key_file(key_path)


# ────────────────────────────────────────────────────────────
# 静态资源提取与验证
# ────────────────────────────────────────────────────────────

# 匹配 href/src 里指向 /static/ 的 URL
# Starlette 的 url_for 渲染为绝对 URL（http://<host>/static/xxx），
# 故匹配任意以 /static/ 结尾路径段的引用，不假设相对或绝对形式。
STATIC_RE = re.compile(r'(?:href|src)="([^"]*/static/[^"?#]+)"')


def collect_static_refs(client, urls: list[str]) -> dict[str, list[str]]:
    """抓取每个页面，提取其引用的静态资源 URL。返回 {资源: [引用它的页面]}。"""
    refs: dict[str, list[str]] = {}
    for u in urls:
        resp = client.get(u)
        for m in STATIC_RE.findall(resp.text):
            refs.setdefault(m, []).append(u)
    return refs


# ────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────

# v0.2 六页 + v0.5 五页 + v0.6 /downloaders + v0.7 /discover。新页面在空库下必须也能渲染（无数据时显示空态而非 500）。
PAGES = [
    "/dashboard", "/watchlist", "/records", "/review", "/sites", "/settings",
    "/library", "/quarantine", "/series", "/seeding", "/events", "/downloaders",
    "/discover",
]

# 本轮发布号。与 __version__ 双向校验：一边忘改另一边就会报出来。
EXPECTED_VERSION = "0.7.0"


def main() -> int:
    log("=" * 58)
    log(f" audio-hub 端到端冒烟检查 v3   {datetime.now(tz=UTC).astimezone():%F %T}")
    log("=" * 58)

    with tempfile.TemporaryDirectory(prefix="ah-smoke-") as td:
        tmp = Path(td)
        prepare_env(tmp)
        log(f"临时配置: {tmp}")
        log("")

        from fastapi.testclient import TestClient

        from audio_hub.main import app

        with TestClient(app) as client:

            # ── 1. 未登录访问受保护页应被重定向 ──
            log("--- [1] 未登录守卫 ---")
            r = client.get("/dashboard", follow_redirects=False)
            check(r.status_code == 303, "GET /dashboard 未登录返回 303（跳 /login）",
                  f"实际 {r.status_code}")
            check(r.headers.get("location", "").startswith("/login"),
                  "重定向指向 /login", r.headers.get("location", ""))

            r = client.get("/health")
            check(r.status_code == 200 and r.json().get("status") == "ok",
                  "/health 无需登录且返回 ok", f"{r.status_code} {r.text[:80]}")
            log("")

            # ── 2. /setup 建号 + 自动登录 ──
            log("--- [2] 首次初始化 ---")
            r = client.get("/setup")
            check(r.status_code == 200, "GET /setup 返回 200", f"{r.status_code}")
            check("创建" in r.text or "管理员" in r.text,
                  "/setup 页面渲染出初始化表单文案")

            r = client.post("/setup", data={
                "username": "smoke_ops",
                "password": "SmokePass123",
                "password2": "SmokePass123",
            }, follow_redirects=False)
            check(r.status_code == 303, "POST /setup 建号返回 303",
                  f"{r.status_code} {r.text[:120]}")

            r = client.get("/dashboard", follow_redirects=False)
            check(r.status_code == 200, "建号后已自动登录（/dashboard 直接 200）",
                  f"{r.status_code}，若为 307 说明会话 cookie 未生效")

            r = client.get("/setup", follow_redirects=False)
            check(r.status_code == 303, "/setup 已有用户时拒绝再次访问",
                  f"{r.status_code}")
            log("")

            # ── 3. 十三个页面全部可渲染 ──
            log("--- [3] 页面渲染 ---")
            page_html: dict[str, str] = {}
            for p in PAGES:
                r = client.get(p)
                ok = check(r.status_code == 200, f"GET {p} 返回 200",
                           f"{r.status_code} {r.text[:120]}")
                if ok:
                    page_html[p] = r.text
            log("")

            # ── 4. 静态资源：按页面实际引用逐个验证 ──
            log("--- [4] 静态资源可达性（按页面实际引用提取，非写死）---")
            refs = collect_static_refs(client, list(page_html))
            if not refs:
                check(False, "页面中未发现任何 /static/ 引用",
                      "可能 url_for 名称错误，页面将是无样式裸页")
            for url, pages in sorted(refs.items()):
                r = client.get(url)
                ctype = r.headers.get("content-type", "")
                check(r.status_code == 200 and len(r.content) > 0,
                      f"GET {url} -> 200 且非空 "
                      f"({len(r.content)} bytes, {ctype.split(';')[0]})",
                      f"{r.status_code}，被引用于 {pages}")

            # 已知必备资源额外确认（防页面漏引导致上面检查空转）
            for must in ("/static/style.css", "/static/app.js"):
                r = client.get(must)
                check(r.status_code == 200, f"必备资源 {must} 可达",
                      f"{r.status_code}")
            log("")

            # ── 5. CSRF meta 下发（app.js 依赖）──
            log("--- [5] CSRF 令牌下发 ---")
            html = page_html.get("/dashboard", "")
            m = re.search(r'name="csrf-token" content="([^"]+)"', html)
            check(bool(m), "dashboard 含 csrf-token meta 标签",
                  "缺失则前端无法自动注入 CSRF，所有表单提交将 403")
            token = m.group(1) if m else ""
            check(len(token) > 20, f"CSRF token 长度合理（{len(token)} 字符）")
            log("")

            # ── 6. 写操作 CSRF 防护 ──
            log("--- [6] 写操作防护 ---")
            r = client.post("/api/run-once")
            check(r.status_code == 403, "JSON 端点缺 CSRF -> 403",
                  f"实际 {r.status_code}")
            r = client.post("/api/run-once", headers={"X-CSRF-Token": "bogus"})
            check(r.status_code == 403, "JSON 端点伪造 CSRF -> 403",
                  f"实际 {r.status_code}")
            r = client.post("/api/run-once", headers={"X-CSRF-Token": token})
            check(r.status_code == 200, "JSON 端点带正确 CSRF -> 200",
                  f"实际 {r.status_code} {r.text[:120]}")
            r = client.post("/run-once", data={"csrf_token": token},
                            follow_redirects=False)
            check(r.status_code == 303, "页面版 run-once 带 CSRF -> 303",
                  f"实际 {r.status_code} {r.text[:120]}")
            log("")

            # ── 7. 登出与会话失效 ──
            log("--- [7] 登出 ---")
            r = client.post("/logout", data={"csrf_token": token},
                            follow_redirects=False)
            check(r.status_code == 303, "POST /logout 返回 303",
                  f"{r.status_code}")
            r = client.get("/dashboard", follow_redirects=False)
            check(r.status_code == 303, "登出后 /dashboard 重新被拦（303 跳 /login）",
                  f"{r.status_code}")
            log("")

            # ── 8. 版本单一真源 ──
            log("--- [8] 版本一致性 ---")
            from audio_hub import __version__
            r = client.get("/health")
            check(r.json().get("version") == __version__,
                  f"/health 版本 == __version__ ({__version__})",
                  f"实际 {r.json().get('version')}")
            check(r.json().get("version") == EXPECTED_VERSION,
                  f"/health 版本 == 本轮发布号 {EXPECTED_VERSION}",
                  f"实际 {r.json().get('version')}，__init__.__version__ 或本脚本未同步")
            # 侧边栏 brand 读的是 Jinja 全局 version，与 /health 同源；这里顺带确认模板没写死旧值
            html = page_html.get("/dashboard", "")
            check(f"v{EXPECTED_VERSION}" in html,
                  f"页面 brand 显示 v{EXPECTED_VERSION}",
                  "base.html 的 brand 可能仍写死旧版本号")

    log("=" * 58)
    if _failures:
        log(f" 冒烟检查未通过：{len(_failures)} 项失败")
        for f in _failures:
            log(f"   - {f}")
    else:
        log(" 冒烟检查全部通过")
    log("=" * 58)
    flush_report()
    return 1 if _failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback
        log("")
        log("!! 冒烟检查异常终止")
        log(traceback.format_exc())
        flush_report()
        sys.exit(2)
