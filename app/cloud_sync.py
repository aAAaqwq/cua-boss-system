"""候选人数据上云到 Supabase (PostgREST) —— 单向 local→cloud 镜像。

零 pip 依赖，纯 urllib。**best-effort**：失败入本地队列 `data/cloud_queue.jsonl`，
下次自动补推，**绝不阻塞本地采集**。本地 SQLite 永远是真源，云端是下游镜像。

鉴权（两种，**优先「登录绑定」**）：
  1) 登录绑定（推荐·多用户）：`python scripts/cloud_sync.py login` 用账号密码换 user token，
     存 `data/.cloud_auth.json`（gitignored）。push 用 **user token**——RLS 保证**只能写自己 tenant**，
     tenant 自动 = 登录账号的 uid，**无需手填 TENANT_ID**。账号由服务端(我们)创建/注册下发。
  2) service_role（仅拥有者/管理·绕过 RLS）：`.env` 设 `SUPABASE_KEY`(service_role)+`TENANT_ID`。
     权限过大，**不要下发给普通用户**。

配置（`.env`）：
  CLOUD_SYNC          总开关 on/off（默认 off）
  SUPABASE_URL        https://xxxxx.supabase.co
  SUPABASE_ANON_KEY   anon key（公开安全；登录 + push 用）
  SUPABASE_KEY        service_role（可选，仅拥有者）
  TENANT_ID           service_role 模式下的租户（登录模式忽略，自动取登录 uid）
  CLOUD_TABLE         目标表（默认 candidates）
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from app.db import DB_PATH

_ROOT = Path(__file__).parent.parent
_QUEUE_PATH = _ROOT / "data" / "cloud_queue.jsonl"
_AUTH_PATH = _ROOT / "data" / ".cloud_auth.json"   # 登录态(refresh_token等)，gitignored
_ENV_PATH = _ROOT / ".env"
_CONFLICT_KEYS = "tenant_id,uid"
_HTTP_TIMEOUT = 30

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


# 项目公开常量——内置默认值，用户无需在 .env 配置，装好直接 `login` 即用。
# anon key 是**公开安全**的（受 RLS 保护、本就嵌在网页里公开），URL 是公开地址；全体用户相同。
# 如需切换项目，仍可用 .env / 环境变量 SUPABASE_URL / SUPABASE_ANON_KEY 覆盖。
_DEFAULT_URL = "https://nfmeknfnopeakugqphia.supabase.co"
_DEFAULT_ANON = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5m"
                 "bWVrbmZub3BlYWt1Z3FwaGlhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI1MjI2MzgsImV4cCI6"
                 "MjA5ODA5ODYzOH0.gnyQOD2o75Os3rr0PPT2gNnP9WaLJYBCG6YCC2uacpY")


def config() -> dict:
    _load_env()
    return {
        "enabled": os.environ.get("CLOUD_SYNC", "off").lower() in ("on", "1", "true", "yes"),
        "url": (os.environ.get("SUPABASE_URL") or _DEFAULT_URL).rstrip("/"),
        "anon": os.environ.get("SUPABASE_ANON_KEY") or _DEFAULT_ANON,
        "service_key": os.environ.get("SUPABASE_KEY", ""),
        "tenant": os.environ.get("TENANT_ID", ""),
        "table": os.environ.get("CLOUD_TABLE", "candidates"),
    }


# ── 通用 HTTP（返回 (status, json|text, err)）──
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")  # 避开 api.supabase.com 对 urllib UA 的 WAF(1010)


def _http(url: str, method: str = "GET", headers: Optional[dict] = None,
          body: Optional[dict] = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = dict(headers or {})
    headers.setdefault("User-Agent", _UA)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return getattr(resp, "status", resp.getcode()), json.loads(raw or "null"), ""
            except json.JSONDecodeError:
                return getattr(resp, "status", resp.getcode()), raw, ""
    except urllib.error.HTTPError as e:
        return e.code, None, e.read().decode("utf-8", "ignore")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, None, str(e)


# ══════════════════════════════════════════════════
# 登录绑定（推荐的多用户鉴权）
# ══════════════════════════════════════════════════

def _load_auth() -> dict:
    if not _AUTH_PATH.exists():
        return {}
    try:
        return json.loads(_AUTH_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_auth(d: dict) -> None:
    _AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(_AUTH_PATH, 0o600)  # 仅本人可读
    except OSError:
        pass


def clear_auth() -> None:
    if _AUTH_PATH.exists():
        _AUTH_PATH.unlink()


def login(email: str, password: str, cfg: Optional[dict] = None) -> tuple:
    """用账号密码登录 Supabase，换取并保存 user token。返回 (ok, msg)。"""
    cfg = cfg or config()
    if not (cfg["url"] and cfg["anon"]):
        return False, "缺少 SUPABASE_URL / SUPABASE_ANON_KEY"
    status, body, err = _http(
        f"{cfg['url']}/auth/v1/token?grant_type=password",
        method="POST",
        headers={"apikey": cfg["anon"], "Content-Type": "application/json"},
        body={"email": email, "password": password},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        return False, f"登录失败: {err or status}"
    user = body.get("user") or {}
    _save_auth({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", ""),
        "uid": user.get("id", ""),
        "email": user.get("email", email),
        "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60,
    })
    return True, user.get("email", email)


def _refresh(cfg: dict, auth: dict) -> dict:
    """用 refresh_token 续期 access_token；失败返回 {}。"""
    rt = auth.get("refresh_token")
    if not rt:
        return {}
    status, body, _ = _http(
        f"{cfg['url']}/auth/v1/token?grant_type=refresh_token",
        method="POST",
        headers={"apikey": cfg["anon"], "Content-Type": "application/json"},
        body={"refresh_token": rt},
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        return {}
    user = body.get("user") or {}
    auth.update({
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", rt),
        "uid": user.get("id", auth.get("uid", "")),
        "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60,
    })
    _save_auth(auth)
    return auth


def auth_context(cfg: Optional[dict] = None) -> Optional[dict]:
    """返回当前可用的鉴权上下文（优先登录态），否则 None。

    {apikey, bearer, tenant, source('login'|'service_role'), email}
    """
    cfg = cfg or config()
    # 1) 登录态优先
    auth = _load_auth()
    if auth.get("access_token") and cfg["url"] and cfg["anon"]:
        if time.time() >= auth.get("expires_at", 0):
            auth = _refresh(cfg, auth)  # 过期 → 续期
        if auth.get("access_token"):
            return {"apikey": cfg["anon"], "bearer": auth["access_token"],
                    "tenant": auth.get("uid", ""), "source": "login",
                    "email": auth.get("email", "")}
    # 2) service_role（仅拥有者）
    if cfg["service_key"] and cfg["tenant"] and cfg["url"]:
        return {"apikey": cfg["service_key"], "bearer": cfg["service_key"],
                "tenant": cfg["tenant"], "source": "service_role", "email": ""}
    return None


def is_ready(cfg: Optional[dict] = None) -> bool:
    return auth_context(cfg) is not None


def require_account() -> dict:
    """许可门禁：本产品的 agent **必须用我们下发的账号登录**后才能运行。

    未登录(且非拥有者 service_role) → 打印登录指引并退出。
    在 greeting/collect/chat/pipeline 入口处调用，确保「无账号无法驱动程序」。
    """
    ctx = auth_context()
    if ctx is None:
        import sys
        print("=" * 58)
        print("🔒 需要登录授权账号才能使用本产品")
        print("  本程序须用【我们下发的账号】登录后才能运行：")
        print("    python scripts/cloud_sync.py login --email <邮箱> --password <密码>")
        print("  没有账号？请联系管理员开通（账号仅由后台创建）。")
        print("=" * 58)
        sys.exit(1)
    return ctx


# ══════════════════════════════════════════════════
# 上云
# ══════════════════════════════════════════════════

def to_cloud(row: dict, tenant: str) -> dict:
    payload = {"tenant_id": tenant}
    for col in _CLOUD_COLUMNS:
        if col in row:
            payload[col] = row[col]
    return payload


def _post(cfg: dict, ctx: dict, payloads: list) -> tuple:
    url = f"{cfg['url']}/rest/v1/{cfg['table']}?on_conflict={_CONFLICT_KEYS}"
    data = json.dumps(payloads, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "apikey": ctx["apikey"],
        "Authorization": f"Bearer {ctx['bearer']}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            code = getattr(resp, "status", resp.getcode())
            return (200 <= code < 300), f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _enqueue(payloads: list) -> None:
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _QUEUE_PATH.open("a", encoding="utf-8") as f:
        for p in payloads:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def queue_size() -> int:
    if not _QUEUE_PATH.exists():
        return 0
    return sum(1 for line in _QUEUE_PATH.read_text(encoding="utf-8").splitlines() if line.strip())


def push(rows: list, cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    cfg = cfg or config()
    ctx = auth_context(cfg)
    if ctx is None:
        if verbose:
            print("    ⚠ 云同步未鉴权(请先 `cloud_sync.py login`，或配置 service_role)，跳过")
        return {"ok": False, "reason": "unauthenticated", "pushed": 0}
    payloads = [to_cloud(r, ctx["tenant"]) for r in rows]
    if not payloads:
        return {"ok": True, "pushed": 0}
    ok, msg = _post(cfg, ctx, payloads)
    if ok:
        if verbose:
            print(f"    ☁ 已上云 {len(payloads)} 条（{ctx['source']}{(' '+ctx['email']) if ctx['email'] else ''}）")
        return {"ok": True, "pushed": len(payloads)}
    _enqueue(payloads)
    if verbose:
        print(f"    ⚠ 上云失败({msg})，已入队列待补推 {len(payloads)} 条")
    return {"ok": False, "reason": msg, "pushed": 0, "queued": len(payloads)}


def flush_queue(cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    cfg = cfg or config()
    ctx = auth_context(cfg)
    if not _QUEUE_PATH.exists() or ctx is None:
        return {"ok": True, "flushed": 0}
    lines = [l for l in _QUEUE_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return {"ok": True, "flushed": 0}
    try:
        payloads = [json.loads(l) for l in lines]
    except json.JSONDecodeError:
        if verbose:
            print("    ⚠ 队列文件损坏，跳过补推")
        return {"ok": False, "reason": "corrupt_queue", "flushed": 0}
    # 队列里的 tenant 以当前登录身份为准（防止换账号后串租户）
    for p in payloads:
        p["tenant_id"] = ctx["tenant"]
    ok, msg = _post(cfg, ctx, payloads)
    if ok:
        _QUEUE_PATH.unlink()
        if verbose:
            print(f"    ☁ 补推队列 {len(payloads)} 条成功")
        return {"ok": True, "flushed": len(payloads)}
    if verbose:
        print(f"    ⚠ 队列补推失败({msg})，留待下次")
    return {"ok": False, "reason": msg, "flushed": 0}


def _fetch_rows(uids: Optional[list] = None, limit: Optional[int] = None) -> list:
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
    """按 uid 从本地 DB 取行并上云（供 pipeline/collect/chat 末尾调用）。

    CLOUD_SYNC=off → 跳过；未登录/未鉴权 → 跳过（不报错）。
    """
    cfg = cfg or config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled", "pushed": 0}
    if auth_context(cfg) is None:
        if verbose:
            print("    ⚠ CLOUD_SYNC=on 但未登录(先 `cloud_sync.py login`)，跳过上云")
        return {"ok": False, "reason": "unauthenticated", "pushed": 0}
    flush_queue(cfg, verbose=verbose)
    rows = _fetch_rows(uids=uids)
    return push(rows, cfg, verbose=verbose)


# ══════════════════════════════════════════════════
# 增量同步（推「未同步 或 改动过」的所有行，绝不漏）
# ══════════════════════════════════════════════════

def _pending_rows(limit: Optional[int] = None) -> list:
    """待同步行：有 uid 且（从未同步 synced_at IS NULL，或数据比上次同步更新 updated_at>synced_at）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT * FROM candidates WHERE uid IS NOT NULL AND uid != '' "
               "AND (synced_at IS NULL OR updated_at > synced_at) ORDER BY updated_at")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _mark_synced(uids: list) -> None:
    """标记已同步：synced_at=now。synced_at 不在 _TOUCH_COLUMNS，不会反向触发 updated_at。"""
    uids = [u for u in uids if u]
    if not uids:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        qs = ",".join("?" * len(uids))
        conn.execute(f"UPDATE candidates SET synced_at = CURRENT_TIMESTAMP WHERE uid IN ({qs})", uids)
        conn.commit()
    finally:
        conn.close()


def pending_count() -> int:
    return len(_pending_rows())


def sync_pending(cfg: Optional[dict] = None, verbose: bool = True) -> dict:
    """**自动增量同步**：推所有「未同步/改动过」的行（不只本轮），成功后标记 synced_at。

    失败不标记 → 下次自动重推（synced_at 即持久化的「待同步」状态，比临时队列更可靠）。
    供 collect/chat/pipeline 末尾调用——任何原因漏掉的行下次一跑自动补齐，无需手动 push。
    """
    cfg = cfg or config()
    if not cfg["enabled"]:
        return {"ok": False, "reason": "disabled", "pushed": 0}
    ctx = auth_context(cfg)
    if ctx is None:
        if verbose:
            print("    ⚠ CLOUD_SYNC=on 但未登录(先 `cloud_sync.py login`)，跳过上云")
        return {"ok": False, "reason": "unauthenticated", "pushed": 0}
    flush_queue(cfg, verbose=verbose)  # 先排空历史队列(向后兼容)
    rows = _pending_rows()
    if not rows:
        if verbose:
            print("    ☁ 云同步: 无待同步行")
        return {"ok": True, "pushed": 0}
    payloads = [to_cloud(r, ctx["tenant"]) for r in rows]
    ok, msg = _post(cfg, ctx, payloads)
    if ok:
        _mark_synced([r["uid"] for r in rows])
        if verbose:
            print(f"    ☁ 增量上云 {len(rows)} 条(含历史漏网，自动补齐)")
        return {"ok": True, "pushed": len(rows)}
    if verbose:
        print(f"    ⚠ 上云失败({msg})，{len(rows)} 条 synced_at 未更新 → 下次自动补推")
    return {"ok": False, "reason": msg, "pushed": 0}
