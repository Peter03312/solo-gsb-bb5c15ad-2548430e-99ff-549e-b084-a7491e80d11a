FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    AUDIT_CURSOR_SECRET=dev-only-audit-cursor-secret-change-me

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data /backups

# 以非 root 用户运行，保证 /data 与 /backups 可写
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /backups /app
USER appuser

VOLUME ["/data", "/backups"]
EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
