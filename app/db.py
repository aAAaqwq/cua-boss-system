"""
candidates.db 初始化与公共数据库操作

所有脚本（collect / chat_loop / greeting 等）共用此模块，
保证表结构一致，不再各自散落 ALTER TABLE 补丁。
"""
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"
BACKUP_DIR = DB_PATH.parent / "backups"

# ── 完整 schema（新建表用） ──
_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT NOT NULL,
    job_position TEXT,
    school TEXT,
    degree TEXT,
    resume_content TEXT,
    resume_filename TEXT,
    has_resume INTEGER DEFAULT 0,
    wechat TEXT,
    has_wechat INTEGER DEFAULT 0,
    phone TEXT,
    email TEXT,
    score INTEGER DEFAULT 0,
    status TEXT DEFAULT 'collected',
    chat_history TEXT,
    notes TEXT,
    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# ── 兼容旧表：需要补齐的列 ──
_PATCH_COLUMNS = ("uid", "chat_history")


def init_db() -> sqlite3.Connection:
    """初始化 candidates.db，返回已连接的 sqlite3.Connection。

    - 表不存在 → 按完整 schema 创建
    - 表已存在但缺列 → ALTER TABLE 补齐
    - uid 唯一索引不存在 → 创建
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(_SCHEMA)

    # 兼容旧表：补齐新增列
    for col in _PATCH_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # uid 唯一索引
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_uid ON candidates(uid)"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    return conn


def backup_db(suffix: str = "") -> Path:
    """备份当前 candidates.db 到 data/backups/ 目录

    文件名格式: candidates_YYYYMMDD_HHMMSS_<suffix>.db
    如果 DB 文件不存在则跳过，返回空 Path。

    用法:
      from app.db import backup_db
      path = backup_db("before-clear")
    """
    if not DB_PATH.exists():
        return Path()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"_{suffix}" if suffix else ""
    dest = BACKUP_DIR / f"candidates_{ts}{tag}.db"
    shutil.copy2(str(DB_PATH), str(dest))
    return dest


def clear_db(backup: bool = True) -> None:
    """清空 candidates 表所有数据

    默认先备份再清空，防止误操作丢失数据。
    表结构和索引保留不变。

    用法:
      from app.db import clear_db
      clear_db()           # 自动备份 + 清空
      clear_db(backup=False) # 不备份直接清空（谨慎）
    """
    if backup and DB_PATH.exists():
        path = backup_db("before-clear")
        print(f"  ✓ 已备份: {path}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM candidates")
    conn.commit()
    conn.close()
    print(f"  ✓ 已清空: {DB_PATH}")
