FROM python:3.12-alpine AS violentmonkey

ARG VM_URL=https://github.com/violentmonkey/violentmonkey/releases/download/v2.50.0/Violentmonkey-mv3-v2.50.0.zip
ARG VM_SHA256=72f1de5a2edc4bc3cd3b3ca06c2b3631cdde4f06f8ed9e8b2b565bf77258b36c

ADD ${VM_URL} /tmp/violentmonkey.zip
RUN echo "${VM_SHA256}  /tmp/violentmonkey.zip" | sha256sum -c - && \
    python -c 'import zipfile; zipfile.ZipFile("/tmp/violentmonkey.zip").extractall("/opt/violentmonkey")'

FROM lscr.io/linuxserver/chromium:latest@sha256:f2d4b8b284918d8d65e10921e2fc84c57fd7edbc15345ddb3c024f0949f0031c

COPY --from=violentmonkey /opt/violentmonkey /opt/violentmonkey
