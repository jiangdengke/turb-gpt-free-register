FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm

ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="turb-gpt-free-register" \
      org.opencontainers.image.description="ChatGPT registration and Codex OAuth WebUI" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_EXECUTABLE=/usr/bin/node \
    TURB_DATA_DIR=/data \
    HOME=/data/home \
    XDG_CACHE_HOME=/data/cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

COPY config /app/config
COPY core /app/core
COPY webui /app/webui
COPY sentinel /app/sentinel
COPY main.py web.py requirements.txt /app/
COPY docker/entrypoint.sh /usr/local/bin/turb-entrypoint

RUN chmod 0755 /usr/local/bin/turb-entrypoint \
    && set -eux; \
       for path in \
         .env \
         turb.sqlite3 turb.sqlite3-wal turb.sqlite3-shm \
         "用于注册的邮箱.json" "用于注册的邮箱.txt" \
         "用于注册的API邮箱.json" "用于注册的API邮箱.txt" \
         "用于注册的域名邮箱.json" \
         "注册成功的邮箱.json" "注册成功的邮箱.txt" \
         "注册成功的token.txt" "注册任务.json" \
         "codex_导出状态.json" accounts_viewer.html \
         outlook_accounts.txt outlook_accounts_used.json sub2api.json; \
       do \
         rm -rf "/app/$path"; \
         ln -s "/data/$path" "/app/$path"; \
       done; \
       for path in logs run "注册日志" cache accounts data codex_accounts codex_agent_accounts; \
       do \
         rm -rf "/app/$path"; \
         ln -s "/data/$path" "/app/$path"; \
       done

EXPOSE 5000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/turb-entrypoint"]
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:5000", "--timeout", "300", "--graceful-timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "webui.app:create_app()"]
