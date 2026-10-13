#!/usr/bin/env python3
"""qb 连通性与凭据诊断：本地(WSL) → NAS qBittorrent。

为什么需要它
------------
切实跑前必须先确认三件事，否则失败会以很难懂的形式出现
（任务只是被标记 failed，错误信息一行「qb 客户端不可用」，
看不出到底是网络、凭据还是路径的问题）：

1. 网络可达：WSL 能否连到 NAS 的 qb WebUI 端口
2. 凭据有效：账密或 API Key 能否通过 qb 认证（与服务一致：账密优先，Key 兜底）
3. 路径正确：save_path 是 **qb 所在主机的视角**，不是本机路径

第 3 点最容易翻车：qb 在 NAS 上，save_path 若写成本机 WSL 路径
（如 /home/cold/...），qb 写不到 → 派发失败，或下载到音乐库之外，
归档链路（scratch/ → MTW 刮削 → Navidrome）全断。

本脚本**只读**：不改任何配置、不添加任何种子、不删除任何东西。

用法（WSL 内）：
    cd ~/audio-hub && .venv/bin/python /mnt/d/Documents/audio-hub/scripts/check_qb.py

它会自动沿用 serve_dev.sh 的配置与密钥路径，并读取数据库里的
downloaders 表（v0.6 起下载器的权威源，/downloaders 页维护）与
app_settings 旧覆盖项 —— 也就是说你在页面上改过 host / 凭据之后，
**不必重启服务**就能用本脚本验证新值是否可用。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from pathlib import Path

# ── 路径约定（与 serve_dev.sh 保持一致）────────────────────
DST = Path.home() / "audio-hub"
DEFAULT_CFG_DIR = DST / "config"
FALLBACK_CFG_DIRS = (
    DEFAULT_CFG_DIR,
    Path("/mnt/d/Documents/audio-hub/config"),
)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

# qb WebUI 返回的状态码 → 诊断建议
QB_HTTP_HINTS = {
    403: (
        "qb 拒绝访问。最常见两个原因（qb WebUI → 选项 → Web用户界面 → 安全）：\n"
        "       ① 「启用 Host 头验证」开着 —— 从非本机访问会被拒，需关闭或\n"
        "          把本机加入白名单\n"
        "       ② 「绕过以下 IP 地址列表的认证」未包含你的 WSL 出口 IP\n"
        "       也可能是 CSRF 保护拦截了跨主机请求。"
    ),
    401: "凭据被拒：账密错误、API Key 无效或已过期；qb < 5.2 不支持 API Key（可改用账密）。",
    404: "路径不对：qb 的 WebUI 可能配了「备用 WebUI」或子路径前缀。",
}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def hr(title: str) -> None:
    say("")
    say(f"── {title} " + "─" * max(0, 52 - len(title)))


def find_config() -> Path | None:
    env = os.environ.get("AUDIO_HUB_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    for d in FALLBACK_CFG_DIRS:
        p = d / "config.yaml"
        if p.exists():
            return p
    return None


def find_key(cfg_dir: Path) -> Path | None:
    env = os.environ.get("AUDIO_HUB_SECRETS_KEY")
    if env and Path(env).exists():
        return Path(env)
    p = cfg_dir / "secrets.key"
    return p if p.exists() else None


def _open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """只读打开数据库。

    优先只读 URI；但 WAL 模式下若 -wal 有数据而 -shm 不存在（服务被强杀
    等非正常关闭），只读连接会因无法创建 -shm 而失败。此时回退到普通连接
    + PRAGMA query_only（仍只 SELECT，绝不写数据），避免因读不到而静默用
    YAML 旧值诊断 —— 那会验错配置：你在界面填的 NAS host 是 restart 级
    覆盖项，只存在 DB 里，YAML 仍是 localhost。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_db_overrides(config) -> list[str]:
    """把数据库覆盖层叠加到 config（复用项目自身的解码与应用逻辑）。

    为什么不用 settings_store.hydrate_config：它需要 async 的 aiosqlite
    连接。诊断脚本用标准库 sqlite3 只读打开即可，少一层依赖与事件循环。
    """
    db_path = Path(config.db_path)
    if not db_path.exists():
        say(f"  （数据库尚不存在，无界面覆盖项: {db_path}）")
        return []

    conn = None
    try:
        conn = _open_db_readonly(db_path)
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    except sqlite3.Error as e:
        say(f"  ⚠️  读取覆盖层失败，将仅按 YAML 诊断: {e}")
        say("     若你在界面改过 qb host/key（restart 级，只存在 DB 里），")
        say("     请先重启服务、或停掉服务后重跑本脚本，否则会验错配置。")
        return []
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    from audio_hub import settings_store as store

    specs = {s.key: s for s in store.all_specs(config)}
    applied = []
    for row in rows:
        key = row["key"]
        if key.startswith("__"):
            continue
        spec = specs.get(key)
        if spec is None:
            continue
        try:
            value = store._decode_stored(spec, row["value"])
            if store._apply_one(config, spec, value):
                applied.append(key)
        except Exception as e:  # noqa: BLE001 - 单项坏值不阻断诊断
            say(f"  ⚠️  覆盖项 {key} 解码失败，已跳过: {type(e).__name__}: {e}")
    return applied


def read_default_downloader(db_path: Path):
    """只读 downloaders 表，返回默认下载器配置（含解密凭据）；无库 / 表空 / 失败返回 None。

    选择顺序与 repo.get_default_downloader 一致：is_default 优先，
    其次优先级最高的已启用实例。行 → DownloaderConfig 复用
    repo.downloader_config_from_row（解密逻辑的唯一来源，不另造一份）。
    """
    conn = None
    try:
        conn = _open_db_readonly(db_path)
        row = conn.execute(
            "SELECT * FROM downloaders "
            "ORDER BY is_default DESC, enabled DESC, priority DESC, name LIMIT 1",
        ).fetchone()
    except sqlite3.Error as e:
        say(f"  ⚠️  读取 downloaders 表失败，将按 YAML qbittorrent 段诊断: {e}")
        return None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    if row is None:
        return None

    from audio_hub import repo
    return repo.downloader_config_from_row(row)


def resolve_effective_downloader(config):
    """决定诊断用哪个下载器：downloaders 表默认实例优先，无库 / 表空回落 YAML qb 段。

    返回 (DownloaderConfig, 来源, 备注)；来源为 "db" 或 "yaml"。
    config.qbittorrent 在调用前已被 apply_db_overrides 叠加过界面覆盖项。
    """
    db_path = Path(config.db_path)
    if db_path.exists():
        dl = read_default_downloader(db_path)
        if dl is not None:
            return dl, "db", []
        return config.qbittorrent, "yaml", [
            ("downloaders 表为空，按 YAML qbittorrent 段诊断；"
             "服务下次启动会把该段种子导入 /downloaders"),
        ]
    return config.qbittorrent, "yaml", []


def qb_auth_kwargs(dl) -> tuple[dict, str]:
    """认证参数：账密优先，API Key 兜底（与 main._make_qb_client 的顺序一致）。

    返回 (Client 关键字参数, 形态说明)；形态为空串表示无任何凭据。
    """
    if dl.username:
        return {"username": dl.username, "password": dl.password}, "账密"
    if dl.api_key:
        return {"api_key": dl.api_key}, "API Key"
    return {}, ""


# probe_tcp / probe_http 已迁入 audio_hub.downloaders.probe（v0.6，与下载器中心页
# 「测试连接」共用）；本脚本在 main() 把 src 加进 sys.path 后再导入。


def check_save_path(dl) -> None:
    """save_path 语义检查：这是 qb 所在主机的路径，不是本机路径。"""
    host = dl.host
    sp = dl.save_path
    remote = host not in LOCAL_HOSTS

    say(f"  save_path      : {sp}")
    say(f"  qb 主机        : {host}  ({'远程' if remote else '本机'})")

    if not sp:
        say("  ❌ save_path 为空，派发必然失败")
        return

    if remote:
        suspicious = sp.startswith(("/home/", "/mnt/", "/tmp/", "/root/", "/Users/"))
        if suspicious:
            say("")
            say("  ❌ 路径很可能是错的！")
            say(f"     qb 跑在 {host}（NAS）上，它会按**自己的文件系统**解释这个路径。")
            say(f"     而 {sp} 是本机（WSL）的路径，NAS 上并不存在。")
            say("     后果：种子可能添加成功但下载失败，或下载到音乐库之外，")
            say("           归档链路（scratch/ → MTW 刮削 → Navidrome）会断掉。")
            say("     应填 NAS 侧路径，与 qb1 容器内的挂载一致，例如：")
            say("       /volume1/musiclab/seeds")
            say("     （docker-compose.yml 里挂的是 /volume1/musiclab:/musiclab，")
            say("      故容器视角是 /musiclab/...，宿主机视角是 /volume1/musiclab/...，")
            say("      取决于 qb1 自己的挂载方式 —— 下面会打印 qb 的默认保存路径作参照）")
        else:
            say("  ✓ 路径不像本机路径，看起来是 qb 侧路径（仍需人工确认 NAS 上真实存在）")


def main() -> int:
    say("=" * 60)
    say("  qb 连通性与凭据诊断（只读，不改动任何配置或种子）")
    say("=" * 60)

    cfg_path = find_config()
    if cfg_path is None:
        say("!! 找不到 config.yaml。请先运行 serve_dev.sh 生成，或设置 AUDIO_HUB_CONFIG")
        return 2
    say(f"配置文件: {cfg_path}")

    cfg_dir = cfg_path.parent
    key_path = find_key(cfg_dir)
    if key_path is None:
        say(f"⚠️  找不到密钥文件（{cfg_dir / 'secrets.key'}），"
            "已加密的凭据将无法解密")
    else:
        os.environ.setdefault("AUDIO_HUB_SECRETS_KEY", str(key_path))
        say(f"密钥文件: {key_path}")

    os.environ.setdefault("AUDIO_HUB_CONFIG", str(cfg_path))
    sys.path.insert(0, str(DST / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    try:
        from audio_hub.config import load_config
        from audio_hub.downloaders.probe import probe_http, probe_tcp
        config = load_config()
    except Exception as e:  # noqa: BLE001
        say(f"!! 配置加载失败: {type(e).__name__}: {e}")
        return 2

    hr("① 生效配置（YAML + 界面覆盖层 + downloaders 表）")
    applied = apply_db_overrides(config)
    if applied:
        say(f"  已叠加界面覆盖项 {len(applied)} 项: {applied}")
    else:
        say("  无界面覆盖项，使用 YAML 原值")
    say("")
    # v0.6 起下载器的权威源是 downloaders 表（/downloaders 页维护）；
    # 库不存在或表空时回落到叠加过覆盖项的 YAML qbittorrent 段。
    qb, source, notes = resolve_effective_downloader(config)
    origin = "downloaders 表（/downloaders 页）" if source == "db" else "YAML qbittorrent 段"
    say(f"  诊断对象       : {qb.name}（id={qb.id}），配置来自 {origin}")
    for note in notes:
        say(f"  ⚠️  {note}")
    say(f"  dry_run        : {config.dry_run}"
        f"  ({'空跑，不会真实下载' if config.dry_run else '实跑，会真实派发！'})")
    say(f"  qb host:port   : {qb.host}:{qb.port}")
    auth_kwargs, auth_label = qb_auth_kwargs(qb)
    if auth_label == "账密":
        say(f"  qb 凭据        : 账密（用户 {qb.username}，密码 {len(qb.password)} 字符）")
    elif auth_label == "API Key":
        say(f"  qb 凭据        : API Key（{len(qb.api_key)} 字符）")
    else:
        say("  qb 凭据        : ❌ 未配置（账密与 API Key 均为空）")
    say(f"  qb category    : {qb.category}")
    say(f"  qb tag         : {qb.tag}")

    hr("② save_path 语义检查")
    check_save_path(qb)

    hr("③ TCP 可达性")
    ok, err = probe_tcp(qb.host, qb.port)
    if ok:
        say(f"  ✓ {qb.host}:{qb.port} TCP 可连通")
    else:
        say(f"  ❌ {qb.host}:{qb.port} 不可达")
        say(f"     {err}")
        say("")
        say("  网络不通就没必要继续测凭据了。排查顺序：")
        say("    1. 在 Windows 上确认能打开 qb WebUI（浏览器访问同一地址）")
        say("    2. 确认 NAS 的局域网 IP 与端口（qb WebUI → 选项 → Web用户界面）")
        say("    3. 从 WSL 里 ping 一下 NAS：ping <NAS_IP>")
        say("    4. 若 ping 通但端口不通，是 qb 监听地址或 NAS 防火墙问题")
        return 1

    hr("④ HTTP 响应")
    code, detail = probe_http(qb.host, qb.port)
    say(f"  {detail}")
    if code in QB_HTTP_HINTS:
        say("")
        say(f"  ⚠️  {QB_HTTP_HINTS[code]}")

    hr(f"⑤ 认证（{auth_label or '无凭据'}）")
    if not auth_kwargs:
        say("  ⏭️  跳过：未配置凭据")
        say("     在 /downloaders 页给默认下载器填账密或 API Key 后重跑本脚本。")
        say("     （API Key 获取：qb WebUI → 选项 → Web用户界面 → 认证 → API Key 生成）")
        return 1

    try:
        import qbittorrentapi
    except ImportError as e:
        say(f"  ❌ qbittorrentapi 未安装: {e}")
        return 2

    client = None
    try:
        client = qbittorrentapi.Client(host=qb.host, port=qb.port, **auth_kwargs)
        client.auth_log_in()
        say(f"  ✓ 认证成功（{auth_label}）")
        say(f"    qb 版本        : {client.app.version}")
        say(f"    WebAPI 版本    : {client.app.web_api_version}")
    except TypeError as e:
        say(f"  ❌ 当前 qbittorrentapi 版本不支持所用认证参数: {e}")
        say("     pyproject 要求 qbittorrent-api>=2026.8，请确认已安装该版本：")
        say("       .venv/bin/pip show qbittorrent-api")
        return 1
    except Exception as e:  # noqa: BLE001
        say(f"  ❌ 认证失败: {type(e).__name__}: {e}")
        say("")
        say("  常见原因：")
        say("    · 账密错误；或 API Key 复制时带了空格或换行")
        say("    · Key 已被重新生成（旧的立即失效）")
        say("    · qb 版本 < 5.2.0，不支持 API Key 认证（可改用账密）")
        say("    · qb WebUI 的 Host 头验证 / IP 白名单拦截（见上方 HTTP 响应）")
        return 1
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.auth_log_out()

    hr("⑥ qb 侧真实路径参照")
    try:
        client2 = qbittorrentapi.Client(host=qb.host, port=qb.port, **auth_kwargs)
        client2.auth_log_in()
        try:
            say(f"  qb 默认保存路径  : {client2.app_default_save_path()}")
        except Exception as e:  # noqa: BLE001
            say(f"  （取默认保存路径失败: {e}）")
        try:
            paths = {t.save_path for t in client2.torrents_info(limit=20) if t.save_path}
            if paths:
                say("  现有种子的保存路径（最多 5 个，可作为正确写法参照）：")
                for p in sorted(paths)[:5]:
                    say(f"    · {p}")
            else:
                say("  （qb 中暂无种子可参照）")
        except Exception as e:  # noqa: BLE001
            say(f"  （读取种子列表失败: {e}）")
        client2.auth_log_out()
    except Exception as e:  # noqa: BLE001
        say(f"  （本段跳过: {type(e).__name__}: {e}）")

    say("")
    say("  ↑ 把上面「默认保存路径」的前缀作为 save_path 的依据。")
    say("    例如默认是 /volume1/musiclab/downloads，那 save_path 应写成")
    say("    /volume1/musiclab/seeds 这类同前缀路径，而不是 /home/cold/...")

    hr("⑦ 结论")
    problems = []
    if not auth_kwargs:
        problems.append("qb 凭据未配置（账密与 API Key 均为空，到 /downloaders 页填写）")
    remote = qb.host not in LOCAL_HOSTS
    if remote and qb.save_path.startswith(("/home/", "/mnt/", "/tmp/", "/root/")):
        problems.append(f"save_path 是本机路径，qb 在 {qb.host} 上写不到")
    if not problems:
        say("  ✓ 网络与凭据均通过，save_path 看不出明显问题")
        say("")
        if config.dry_run:
            say("  下一步（切实跑）：")
            say("    1. /downloaders 页：确认默认下载器的 save_path 是 qb 侧路径，保存")
            say("    2. /sites 页：填完整 RSS 地址（含 passkey），保存并跑凭据验证")
            say("    3. 设置页 → 运行参数：运行模式改为「实跑」，保存")
            say("       （dry_run 是启动时固化项，保存后页面会挂橙色横幅）")
            say("    4. 重启服务：Ctrl+C 后重跑 serve_dev.sh")
            say("    5. 重跑本脚本确认 dry_run 已变为 False")
        else:
            say("  ⚠️  当前已是实跑模式：采纳待审项会真实下载到 NAS。")
            say("     首次建议只放一个订阅项、max_rows 调小，观察一轮再放开。")
    else:
        say("  仍有问题需先解决：")
        for p in problems:
            say(f"    ❌ {p}")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断")
        sys.exit(130)
