"""下载器客户端统一工厂 + Transmission 词表翻译层（v0.8.0）。

build_client 按 DownloaderConfig.type 构造下载器客户端：
- qbittorrent（默认，未知类型也回落到此）：原生 qbittorrentapi.Client
- transmission：TransmissionFacade 包装 transmission_rpc.Client

dispatcher、完成监听、做种管理只认 qbittorrentapi 的方法名、状态词表与
异常类（torrents_add / torrents_info / torrents_pause…、uploading /
pausedUP…、Conflict409Error / LoginFailed / APIConnectionError）。
TransmissionFacade 把 Transmission 的语义翻译成这套词表，下游一行不改。

翻译约定（qb 词表 → Transmission 实际操作）：
- category + tags 合并成 labels（Transmission 没有分类概念）
- content_path 按 download_dir + name 拼（Transmission RPC 没有 contentPath 字段）
- 重复添加（duplicate）→ Conflict409Error：复用 qb 的异常类，保持 dispatcher
  的幂等成功分支不变
- 状态映射：seeding→uploading、download pending→queuedDL、stopped+完成→pausedUP…
- 本地错误（error=3）→ state="error"；tracker 警告 / tracker 错误不影响下载，
  不算错误态（qb 的 error 语义也是指任务本身出错，而非某个 tracker 失联）
"""

from __future__ import annotations

from types import SimpleNamespace

import qbittorrentapi
import transmission_rpc
from loguru import logger
from transmission_rpc import (
    TransmissionAuthError,
    TransmissionConnectError,
    TransmissionError,
    TransmissionTimeoutError,
)

#: Transmission RPC 请求超时（秒）；探测路径用自己的更短超时
TR_CLIENT_TIMEOUT = 15.0

#: Transmission status → qb 状态词表（可直译的部分）
_TR_STATE_MAP = {
    "downloading": "downloading",
    "download pending": "queuedDL",
    "seeding": "uploading",
    "seed pending": "queuedUP",
}


def _tr_state(t) -> str:
    """Torrent 状态 → qb 词表；checking / stopped 按完成度二分。"""
    status = getattr(t, "status", "")
    status = getattr(status, "value", status)   # transmission-rpc 7.x 返回 str-Enum
    status = str(status)
    percent = float(getattr(t, "percent_done", 0) or 0)
    if status in _TR_STATE_MAP:
        return _TR_STATE_MAP[status]
    if status in ("checking", "check pending"):
        return "checkingUP" if percent >= 1 else "checkingDL"
    if status == "stopped":
        return "pausedUP" if percent >= 1 else "pausedDL"
    return "downloading"


class TransmissionFacade:
    """把 transmission_rpc.Client 包装成 qbittorrentapi.Client 的方法子集。

    只实现下游实际调用的方法面（见 dispatcher / monitor / lifecycle）；
    超出子集的调用会直接 AttributeError 暴露，不静默吞掉。
    """

    def __init__(self, client):
        self._client = client
        self._session = None

    # ── 会话信息（日志 / 诊断用）────────────────────────────

    def _get_session(self):
        if self._session is None:
            self._session = self._client.get_session()
        return self._session

    @property
    def app(self):
        """模拟 qb 的 client.app：main / cli 的连接日志读 version。"""
        session = self._get_session()
        return SimpleNamespace(
            version=str(getattr(session, "version", "unknown")),
            web_api_version=f"rpc-{getattr(session, 'rpc_version', '?')}",
        )

    # ── 派发 ────────────────────────────────────────────────

    @staticmethod
    def _labels(category: str | None, tags) -> list[str]:
        """分类并入标签；去空、去重、保序（Transmission 只有 labels）。"""
        labels: list[str] = []
        for raw in [category, *(tags or [])]:
            x = str(raw or "").strip()
            if x and x not in labels:
                labels.append(x)
        return labels

    @staticmethod
    def _translate(e: Exception) -> Exception:
        """Transmission 异常 → qb 异常词表，保持 dispatcher 分支语义。"""
        msg = str(e)
        if "duplicate" in msg.lower():
            return qbittorrentapi.Conflict409Error(msg)
        if isinstance(e, TransmissionAuthError) or "401" in msg or "403" in msg:
            return qbittorrentapi.LoginFailed(msg)
        if isinstance(e, (TransmissionConnectError, TransmissionTimeoutError)):
            return qbittorrentapi.APIConnectionError(msg)
        return e

    def torrents_add(
        self, urls=None, torrent_files=None, save_path=None, category=None,
        tags=None, is_paused=False, **_ignored,
    ):
        torrent = torrent_files if torrent_files is not None else urls
        try:
            self._client.add_torrent(
                torrent=torrent,
                download_dir=save_path or None,
                labels=self._labels(category, tags),
                paused=bool(is_paused),
            )
        except TransmissionError as e:
            raise self._translate(e) from e
        except (ConnectionError, TimeoutError, OSError) as e:
            raise qbittorrentapi.APIConnectionError(str(e)) from e

    # ── 完成监听 / 做种轮询 ─────────────────────────────────

    @staticmethod
    def _to_qb_shape(t) -> SimpleNamespace:
        download_dir = str(getattr(t, "download_dir", "") or "")
        name = str(getattr(t, "name", "") or "")
        state = _tr_state(t)
        # tr error: 0=ok 1=tracker 警告 2=tracker 错误 3=本地错误；只有 3 算错误态
        if int(getattr(t, "error", 0) or 0) >= 3:
            state = "error"
        return SimpleNamespace(
            hash=str(getattr(t, "hash_string", "") or "").lower(),
            state=state,
            name=name,
            save_path=download_dir,
            content_path=(
                download_dir.rstrip("/") + "/" + name
                if download_dir and name else (download_dir or name)
            ),
            seeding_time=int(getattr(t, "seconds_seeding", 0) or 0),
            ratio=float(getattr(t, "upload_ratio", 0) or 0.0),
            # v0.14 进度条字段（qb 原生同名语义）：progress 0..1、dlspeed B/s、
            # eta 秒（-1 = 未知）
            progress=float(getattr(t, "percent_done", 0) or 0),
            dlspeed=int(getattr(t, "rate_download", 0) or 0),
            eta=int(getattr(t, "eta", 0) or -1),
        )

    def torrents_info(self, tag=None, **_ignored):
        out = []
        for t in self._client.get_torrents():
            if tag is not None:
                labels = getattr(t, "labels", None) or []
                if tag not in labels:
                    continue
            out.append(self._to_qb_shape(t))
        return out

    # ── 做种 keep/pause ─────────────────────────────────────

    def torrents_pause(self, torrent_hashes=None, **_ignored):
        self._client.stop_torrent(torrent_hashes)

    def torrents_resume(self, torrent_hashes=None, **_ignored):
        self._client.start_torrent(torrent_hashes)


def build_client(config, *, login: bool = False):
    """DownloaderConfig → 下载器客户端；qb 路径未配置凭据返回 None。

    login=True：qb 路径做一次 auth_log_in 轻验证（main 启动 / registry 构建路径）。
    login=False：惰性连接（做种轮询的只读自建路径）。Transmission 的 Client
    构造本身就会握手取会话，无真正的惰性模式，login 参数对它无效果。

    Raises:
        qbittorrentapi / transmission_rpc 的连接或认证异常由调用方捕获 ——
        与原 main._make_qb_client / lifecycle._build_client 的语义一致。
    """
    dl_type = (getattr(config, "type", "") or "qbittorrent").strip().lower()
    if dl_type == "transmission":
        client = transmission_rpc.Client(
            host=config.host,
            port=int(config.port),
            username=config.username or None,
            password=config.password or None,
            timeout=TR_CLIENT_TIMEOUT,
        )
        logger.debug(f"下载器 {config.id} Transmission 客户端已构建（{config.host}:{config.port}）")
        return TransmissionFacade(client)

    # qbittorrent（默认，未知类型回落）
    import qbittorrentapi as _qba

    if config.username:
        client = _qba.Client(
            host=config.host, port=int(config.port),
            username=config.username, password=config.password or "",
        )
    elif config.api_key:
        client = _qba.Client(host=config.host, port=int(config.port), api_key=config.api_key)
    else:
        return None
    if login:
        client.auth_log_in()
    return client
