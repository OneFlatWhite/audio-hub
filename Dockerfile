FROM python:3.12-slim

WORKDIR /app

# argon2-cffi 在无预编译 wheel 的平台上需要 libffi 头文件才能编译
RUN apt-get update && \
    apt-get install -y --no-install-recommends libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# hatchling 构建 wheel 需要 pyproject.toml 声明的全部输入：
# 版本号取自 src/audio_hub/__init__.py，故 src/ 必须在 pip install 之前就位。
# 早期写法先 COPY pyproject.toml 再 install，此时 src/ 尚不存在，构建必失败。
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 默认环境变量
ENV AUDIO_HUB_CONFIG=/config/config.yaml
ENV AUDIO_HUB_SECRETS_KEY=/config/secrets.key

# 端口读 config.web.port（默认 8090），host 固定 0.0.0.0（见 audio_hub/__main__.py）
CMD ["python", "-m", "audio_hub"]
