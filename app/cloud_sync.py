"""候选人数据上云到 Supabase (PostgREST) —— 单向 local→cloud 镜像。

零 pip 依赖，纯 urllib。**best-effort**：失败入本地队列 `data/cloud_queue.jsonl`，
下次自动补推，**绝不阻塞本地采集**。本地 SQLite 永远是真源，云端是下游镜像。

v1：全字段镜像、按 `(tenant_id, uid)` 幂等 upsert。

配置（`.env` / 环境变量，优先级：环境变量 > .env）：
  CLOUD_SYNC     总开关 on/off（默认 off；未开则所有 push 直接跳过）
  SUPABASE_URL   形如 https://xxxxx.supabase.co
  SUPABASE_KEY   service_role 或 anon key（同时用于 apikey + Bearer）
  TENANT_ID      本机数据归属的租户（= 用户 Supabase auth uid，uuid）
  CLOUD_TABLE    目标表名（默认 candidates）

> 详见 docs/cloud-sync-plan.md。建表 SQL 见 supabase/schema.sql。
"""
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from app.db import DB_PATH

_QUEUE_PATH = Path(__file__).parent.parent / "data" / "cloud_queue.jsonl"
_ENV_PATH = Path(__file__).parent.parent / ".env"
_CONFLICT_KEYS = "tenant_id,uid"
_HTTP_TIMEOUT = 30
_QUEUE_MAX_LINES = 50000  # 队列上限，防失控膨胀

# 上云字段（本地 candidates 列 → 云端同名列）。本地自增主键 id 不上云。
_CLOUD_COLUMNS = (
    "uid", "name", "job_position", "school", "degree",
    "resume_content", "resume_filename", "resume_path", "has_resume",
    "wechat", "has_wechat", "phone", "email",
    "score", "score_summary", "scored_at", "status",
    "chat_history", "notes",
    "interview_type", "interview_date", "interview_time", "interview_at",
    "extracted_at", "updated_at",
)

_env_loaded = False


def _load_env() -> None:
    """从项目根 .env 加载（不覆盖已有环境变量，仅一次）。复用与 chat_reply 相同的约定。"""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def config() -> dict:
    """读取云同步配置。"""
    _load_env()
    return {
        "enabled": os.environ.get("CLOUD_SYNC", "off").lower() in ("on", "1", "true", "yes"),
        "url": os.environ.get("SUPABASE_URL", "").rstrip("/"),
        "key": os.environ.get("SUPABASE_KEY", ""),
        "tenant": os.environ.get("TENANT_ID", ""),
        "table": os.environ.get("CLOUD_TABLE", "candidates"),
    }


def is_configured(cfg: Optional[dict] = None) -> bool:
    """URL / KEY / TENANT 是否都齐了（齐了才能真上传）。"""
    cfg = cfg or config()
    return bool(cfg["url"] and cfg["key"] and cfg["tenant"])


def to_cloud(row: dict, tenant: str) -> dict:
    """本地 candidates 行（dict）→ 云端 payload（全字段 + tenant_id）。"""
    payload = {"tenant_id": tenant}
    for col in _CLOUD_COLUMNS:
        if col in row:
            payload[col] = row[col]
    return payload


def _post(cfg: dict, payloads: list) -> tuple:
    """向 PostgREST 批量 upsert。返回 (ok, msg)。"""
    url = f"{cfg['url']}/rest/v1/{cfg['table']}?on_conflict={_CONFLICT_KEYS}"
    data = json.dumps(payloads, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        # merge-duplicates = upsert；return=minimal 省流量
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            code = getattr(resp, "status", resp.getcode())
            return (200 <= code < 300), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:200]
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:  # noqa: BLE001 — 网络层异常统一降级入队
        return False, str(e)


def _enqueue(payloads: list) -> None:
    """上云失败 → 追加到本地队列，下次补推。"""
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_PATH.open("a", encoding="utf-8") as f:
        for p in payloads:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def queue_size() -> int:
    if not _QUEUE_PATH.exists():
        return 0
    return sum(1 for line in _QUEUE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())


def push(rows: list, cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    """把本地行（dict 列表）上云。失败入队列，绝不抛出。"""
    cfg = cfg or config()
    if not is_configured(cfg):
        if verbose:
            print("    ⚠ 云同步未配置(SUPABASE_URL/KEY/TENANT_ID)，跳过")
        return {"ok": False, "reason": "unconfigured", "pushed": 0}
    payloads = [to_cloud(r, cfg["tenant"]) for r in rows]
    if not payloads:
        return {"ok": True, "pushed": 0}
    ok, msg = _post(cfg, payloads)
    if ok:
        if verbose:
            print(f"    ☁ 已上云 {len(payloads)} 条")
        return {"ok": True, "pushed": len(payloads)}
    _enqueue(payloads)
    if verbose:
        print(f"    ⚠ 上云失败({msg})，已入队列待补推 {len(payloads)} 条")
    return {"ok": False, "reason": msg, "pushed": 0, "queued": len(payloads)}


def flush_queue(cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    """补推本地队列里积压的记录；全部成功才清空队列。"""
    cfg = cfg or config()
    if not _QUEUE_PATH.exists() or not is_configured(cfg):
        return {"ok": True, "flushed": 0}
    lines = [line for line in _QUEUE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return {"ok": True, "flushed": 0}
    try:
        payloads = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        if verbose:
            print("    ⚠ 队列文件损坏，跳过补推（保留文件待人工检查）")
        return {"ok": False, "reason": "corrupt_queue", "flushed": 0}
    ok, msg = _post(cfg, payloads)
    if ok:
        _QUEUE_PATH.unlink()
        if verbose:
            print(f"    ☁ 补推队列 {len(payloads)} 条成功")
        return {"ok": True, "flushed": len(payloads)}
    if verbose:
        print(f"    ⚠ 队列补推失败({msg})，留待下次")
    return {"ok": False, "reason": msg, "flushed": 0}


def _fetch_rows(uids: Optional[list] = None, limit: Optional[int] = None) -> list:
    """从本地 DB 取候选人行（dict）。uids 指定则按 uid 取，否则全量(可 limit)。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if uids:
            uids = [u for u in uids if u]
            if not uids:
                return []
            qs = ",".join("?" * len(uids))
            cur = conn.execute(f"SELECT * FROM candidates WHERE uid IN ({qs})", uids)
        else:
            sql = "SELECT * FROM candidates WHERE uid IS NOT NULL AND uid != '' ORDER BY updated_at DESC"
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur = conn.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def push_uids(uids: list, cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    """按 uid 从本地 DB 取行并上云（供 pipeline/collect/score 末尾调用）。

    总开关 CLOUD_SYNC=off 或未配置 → 直接跳过（不打扰、不报错）。
    """
    cfg = cfg or config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled", "pushed": 0}
    if not is_configured(cfg):
        if verbose:
            print("    ⚠ CLOUD_SYNC=on 但未配置 SUPABASE_URL/KEY/TENANT_ID，跳过上云")
        return {"ok": False, "reason": "unconfigured", "pushed": 0}
    flush_queue(cfg, verbose=verbose)  # 先补历史积压
    rows = _fetch_rows(uids=uids)
    return push(rows, cfg, verbose=verbose)
