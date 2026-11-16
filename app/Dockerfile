# syntax=docker/dockerfile:1.7
FROM python:3.12-slim
ARG RCLONE_URL=https://downloads.rclone.org/rclone-current-linux-amd64.zip
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && curl -fsSL "$RCLONE_URL" -o /tmp/rclone.zip \
 && unzip -q /tmp/rclone.zip -d /tmp && cp /tmp/rclone-*-linux-amd64/rclone /usr/local/bin/rclone \
 && chmod +x /usr/local/bin/rclone && rm -rf /tmp/rclone* /var/lib/apt/lists/* \
 && rclone version
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system --no-cache . && useradd -m -u 10001 xl
USER 10001
ENV PYTHONUNBUFFERED=1 HOME=/home/xl
ENTRYPOINT []
CMD ["crossledger", "--help"]
