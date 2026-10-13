"""服务启动入口：``python -m audio_hub``。

uvicorn 的监听端口读 ``config.web.port``（配置模型的唯一真源），
host 固定 0.0.0.0：host 网络 / 容器内必须全绑，安全边界在路由器端口映射表。
此前 serve_dev.sh 与 Dockerfile 各自硬编码端口，web.port 是死字段；
收敛到这里之后，改端口只需要改 config.yaml 一处。
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from .config import load_config

    config = load_config()
    uvicorn.run(
        "audio_hub.main:app",
        host="0.0.0.0",
        port=config.web.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
