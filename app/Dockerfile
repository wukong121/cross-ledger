# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ARG AZCOPY_URL=https://aka.ms/downloadazcopy-v10-linux
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL "$AZCOPY_URL" -o /tmp/azcopy.tgz \
 && tar -xzf /tmp/azcopy.tgz -C /tmp && cp /tmp/azcopy_linux_amd64_*/azcopy /usr/local/bin/azcopy \
 && chmod +x /usr/local/bin/azcopy && rm -rf /tmp/azcopy* /var/lib/apt/lists/* \
 && azcopy --version
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system --no-cache . && useradd -m -u 10001 xl
USER 10001
ENV PYTHONUNBUFFERED=1
ENTRYPOINT []
CMD ["crossledger", "--help"]
