import os

from dotenv import load_dotenv

load_dotenv()          # 项目根 .env → 环境变量（已 import 一次即可全局生效）


def connect():
    try:
        import psycopg
    except ImportError:
        try:
            import psycopg2 as psycopg
        except ImportError as exc:
            raise RuntimeError("缺少 PostgreSQL 驱动：pip install psycopg[binary]") from exc
    return psycopg.connect(os.environ.get("DATABASE_URL", ""))