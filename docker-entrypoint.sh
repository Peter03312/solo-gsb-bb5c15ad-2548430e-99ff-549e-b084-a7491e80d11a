#!/bin/sh
# 容器入口：
#   docker-entrypoint.sh init    仅安装后初始化数据库（建表）后退出
#   docker-entrypoint.sh serve   初始化数据库并启动 API 服务（默认）
#   docker-entrypoint.sh <cmd>   执行任意命令
#     （如 python -m app.backup create /backups/<名> 创建数据库归档，
#       python -m app.backup verify /backups/<名> 只读核验归档）
set -e

init_db() {
    echo "[entrypoint] 初始化数据库: ${DB_PATH:-${DATA_DIR:-/data}/handoff.db}"
    python -c "from app.database import init_db; init_db(); print('[entrypoint] 数据库就绪')"
}

case "${1:-serve}" in
    init)
        init_db
        ;;
    serve)
        # 单条命令同时完成：依赖安装（镜像构建期已装）→ 数据库初始化 → 启动服务
        init_db
        echo "[entrypoint] 启动服务: http://0.0.0.0:${PORT:-8000}"
        exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
        ;;
    *)
        exec "$@"
        ;;
esac
