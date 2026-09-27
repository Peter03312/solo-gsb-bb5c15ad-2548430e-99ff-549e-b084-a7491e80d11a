"""应用配置。"""
import os
from pathlib import Path

# 数据库文件路径（容器内默认 /data/handoff.db，可用环境变量覆盖）
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "handoff.db")))

# 服务监听
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# 交接事件审计检索（GET /audit/events）翻页游标的 HMAC 签名密钥。
# 生产环境必须通过环境变量注入强随机密钥；未设置时仅回退到下面的开发默认值，
# 供本地与 docker compose 一键启动使用（docker-compose.yml 内置同一开发默认值，
# 可在环境或 .env 中覆盖）。
_DEV_AUDIT_CURSOR_SECRET = "dev-only-audit-cursor-secret-change-me"
AUDIT_CURSOR_SECRET = os.getenv(
    "AUDIT_CURSOR_SECRET", _DEV_AUDIT_CURSOR_SECRET
)
