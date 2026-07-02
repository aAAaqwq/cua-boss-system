#!/usr/bin/env python3
# © 2026 Daniel Li (Open CAIO). 伯乐 AI 招聘助手 · 版权所有 All rights reserved.
"""desktop/services.py — 桌面端数据/业务层（被 server.py 的 HTTP 路由调用）

把「读库、跑脚本、登录门禁、评分、配置读写」等真实业务从 HTTP 层剥出来，
server.py 只做路由。所有函数返回可 JSON 序列化的 dict/bytes，不打印到 stdout。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "candidates.db"
ENV_PATH = ROOT / ".env"
RESUMES_DIR = ROOT / "data" / "resumes"
PY = sys.executable or "python3"
sys.path.insert(0, str(ROOT))

# 可发起的后台任务白名单（防任意命令执行）
_TASKS = {
    "greet": ("scripts/cua_greeting_loop.py", "--limit"),
    "collect": ("scripts/cua_collect.py", "--limit"),
    "chat": ("scripts/cua_chat_loop.py", "--limit"),
    "pipeline": ("scripts/boss_pipeline.py", None),
}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# ────────────────────── .env 读写 ──────────────────────
def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env_keys(updates: dict[str, str]) -> None:
    """更新/追加若干键，保留其余行与注释，原子写。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
                continue
        out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}")
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * 6}{secret[-4:]}"


def _clean_env_value(v: str) -> str:
    """.env 值不得含换行/控制字符——否则可注入任意新键（如 SUPABASE_KEY），
    结合 CSRF 会导致把候选人 PII 推到攻击者租户。含控制字符直接判非法。"""
    if any(c in v for c in "\r\n\x00"):
        raise ValueError("值包含非法控制字符")
    return v


def save_config(body: dict) -> dict:
    updates: dict[str, str] = {}
    try:
        key = _clean_env_value((body.get("deepseek_api_key") or "").strip())
        if key and "•" not in key:  # 掩码回传时忽略，防误覆盖真 key
            updates["DEEPSEEK_API_KEY"] = key
            os.environ["DEEPSEEK_API_KEY"] = key  # 进程内即时生效
        model = _clean_env_value((body.get("deepseek_model") or "").strip())
        if model:
            updates["DEEPSEEK_MODEL"] = model
            os.environ["DEEPSEEK_MODEL"] = model
        base = _clean_env_value((body.get("deepseek_base_url") or "").strip())
        if base:
            host = (urlparse(base).hostname or "").lower()
            # 白名单：只放行 https + deepseek.com（防把带 key 的请求指到攻击者端点）。
            # 高级用户要用别的兼容端点，请直接改 .env。
            if urlparse(base).scheme != "https" or not (host == "deepseek.com" or host.endswith(".deepseek.com")):
                return {"ok": False, "error": "接口地址只允许 https 的 deepseek.com 域名（其他端点请手改 .env）"}
            updates["DEEPSEEK_BASE_URL"] = base
            os.environ["DEEPSEEK_BASE_URL"] = base
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if body.get("cloud_sync") in ("on", "off"):
        updates["CLOUD_SYNC"] = body["cloud_sync"]
        os.environ["CLOUD_SYNC"] = body["cloud_sync"]
    if not updates:
        return {"ok": False, "error": "没有要保存的改动"}
    try:
        write_env_keys(updates)
        return {"ok": True, "saved": list(updates.keys())}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"写 .env 失败: {e}"}


def config_status() -> dict:
    env = read_env()
    return {
        "ok": True,
        "deepseek_key_masked": mask(env.get("DEEPSEEK_API_KEY", "")),
        "deepseek_key_set": bool(env.get("DEEPSEEK_API_KEY")),
        "deepseek_model": env.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "deepseek_base_url": env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "cloud_sync": env.get("CLOUD_SYNC", "on"),
    }


def test_deepseek() -> dict:
    """真实打一次 DeepSeek（不是查 key 是否存在，而是验证真能用）。"""
    try:
        from app.bole_agent import chat
        reply, err = chat([{"role": "user", "content": "回复两个字：在的"}],
                          system="你只需回复「在的」两字。", max_tokens=8)
        if reply:
            return {"ok": True, "sample": reply.strip()[:20]}
        return {"ok": False, "error": err or "无响应"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ────────────────────── 登录门禁 ──────────────────────
def auth_status() -> dict:
    from app.cloud_sync import _load_auth
    a = _load_auth()
    email = a.get("email") or (a.get("user") or {}).get("email", "")
    return {"logged_in": bool(email), "email": email}


def do_login(email: str, password: str) -> dict:
    if not email or not password:
        return {"ok": False, "error": "邮箱和密码都要填"}
    try:
        from app.cloud_sync import login
        ok, msg = login(email.strip(), password)
        return {"ok": ok, "email": msg if ok else "", "error": "" if ok else msg}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def do_logout() -> dict:
    try:
        from app.cloud_sync import clear_auth
        clear_auth()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ────────────────────── 看板 ──────────────────────
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def dashboard() -> dict:
    if not DB_PATH.exists():
        return {"ok": False, "reason": "尚无数据库，先跑一次采集", "stats": {}, "top": []}
    conn = _conn()
    try:
        def scalar(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])
        stats = {
            "total": scalar("SELECT COUNT(*) FROM candidates"),
            "has_resume": scalar("SELECT COUNT(*) FROM candidates WHERE has_resume=1"),
            "has_wechat": scalar("SELECT COUNT(*) FROM candidates WHERE has_wechat=1"),
            "interviewed": scalar("SELECT COUNT(*) FROM candidates "
                                  "WHERE interview_date IS NOT NULL AND interview_date!=''"),
            "scored": scalar("SELECT COUNT(*) FROM candidates WHERE score IS NOT NULL AND score>0"),
            "today": scalar("SELECT COUNT(*) FROM candidates "
                            "WHERE date(updated_at)=date('now','localtime')"),
        }
        rows = conn.execute(
            "SELECT uid, name, school, degree, job_position, score, status "
            "FROM candidates WHERE score IS NOT NULL AND score>0 "
            "ORDER BY score DESC LIMIT 8").fetchall()
        return {"ok": True, "stats": stats, "top": [dict(r) for r in rows]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"读库失败: {e}", "stats": {}, "top": []}
    finally:
        conn.close()


# ────────────────────── 候选人详情 ──────────────────────
_DETAIL_FIELDS = [
    "uid", "name", "school", "degree", "job_position", "status",
    "wechat", "has_wechat", "phone", "email", "has_resume", "resume_filename",
    "score", "score_summary", "scored_at",
    "interview_type", "interview_date", "interview_time",
    "notes", "created_at", "updated_at",
]


def _scoring_logic(cand: dict) -> dict:
    """该候选人所沟通岗位的评分维度与权重（即 AI 评分的『逻辑』）。"""
    try:
        from app.scoring import resolve_dimensions, load_scoring_config, match_job_by_position
        from app.chat_reply import load_jobs_config
        cfg = load_scoring_config()
        jobs = load_jobs_config().get("jobs", [])
        job_id = match_job_by_position(cand, jobs, cfg) or ""
        dims = resolve_dimensions(job_id, None, cfg)
        return {
            "job_id": job_id or "（未匹配到具体岗位，用类别默认维度）",
            "dimensions": [{"name": d.name, "weight": d.weight, "description": d.description}
                           for d in dims],
        }
    except Exception as e:  # noqa: BLE001
        return {"job_id": "", "dimensions": [], "error": str(e)}


def candidate_detail(uid: str) -> dict:
    if not uid or not DB_PATH.exists():
        return {"ok": False, "error": "缺少 uid 或数据库"}
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM candidates WHERE uid=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not r:
        return {"ok": False, "error": "候选人不存在"}
    d = dict(r)
    try:
        chat = json.loads(d.get("chat_history") or "[]")
        if not isinstance(chat, list):
            chat = []
    except Exception:  # noqa: BLE001
        chat = []
    rp = d.get("resume_path") or ""
    has_pdf = bool(rp) and Path(rp).exists()
    resume_text = (d.get("resume_content") or "")
    return {
        "ok": True,
        "candidate": {k: d.get(k) for k in _DETAIL_FIELDS},
        "has_pdf": has_pdf,
        "resume_text": resume_text[:8000],
        "resume_text_len": len(resume_text),
        "chat": chat[-40:],
        "scoring_logic": _scoring_logic(d),
    }


def resume_pdf_path(uid: str) -> Path | None:
    """返回该候选人 PDF 的真实路径（校验落在 data/resumes/ 内，防穿越）。"""
    if not uid or not DB_PATH.exists():
        return None
    conn = _conn()
    try:
        r = conn.execute("SELECT resume_path FROM candidates WHERE uid=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not r or not r["resume_path"]:
        return None
    p = Path(r["resume_path"]).resolve()
    if not p.is_relative_to(RESUMES_DIR.resolve()) or not p.is_file():
        return None
    return p


def rescore(uid: str) -> dict:
    """现场跑一遍 AI 评分，返回每个维度的得分+依据（真实评分逻辑）。"""
    if not uid or not DB_PATH.exists():
        return {"ok": False, "error": "缺少 uid 或数据库"}
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM candidates WHERE uid=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not r:
        return {"ok": False, "error": "候选人不存在"}
    try:
        from app.scoring import evaluate_candidate_auto, load_scoring_config
        from app.chat_reply import load_jobs_config
        jobs = load_jobs_config().get("jobs", [])
        score = evaluate_candidate_auto(dict(r), jobs, load_scoring_config())
        if score.skipped:
            return {"ok": False, "error": "无简历附件内容，按规则跳过评分"}
        dims = [{"name": ds.dimension.name, "weight": ds.dimension.weight,
                 "raw": round(ds.raw_score, 1), "weighted": round(ds.weighted_score, 1),
                 "evidence": ds.evidence} for ds in score.dimensions]
        return {"ok": True, "total": score.total_score, "summary": score.summary,
                "job_id": score.job_id, "dimensions": dims, "errors": score.errors}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ────────────────────── 伯乐对话 ──────────────────────
_TOOL_LABELS = {
    "get_dashboard": "查看板数据", "top_candidates": "查评分榜",
    "find_candidate": "搜候选人", "run_task": "启动任务",
    "job_status": "查任务进度", "schedule_interview": "约面试",
}


# 只有用户在【本轮消息】里明确确认，才允许伯乐真实执行动作类工具（否则一律预览）。
# 关键：确认词只能来自用户消息，候选人简历/DB 里的注入文本无法伪造 → 阻断间接提示注入误操作。
_CONFIRM_RE = re.compile(
    r"(真跑|真的跑|直接跑|开始执行|立即执行|立刻执行|别预览|不用预览|确认执行|执行吧|真执行|就这么(办|干))")


def _user_confirmed(msg: str) -> bool:
    return bool(_CONFIRM_RE.search(msg or ""))


def bole_reply(message: str, history: list[dict]) -> dict:
    """伯乐 agent：可调用真实工具（读库/跑脚本/约面试）后再作答。"""
    try:
        from app.bole_agent import run_agent
        from desktop import bole_tools as bt
        msgs = [m for m in history if m.get("role") in ("user", "assistant")][-20:]
        msgs.append({"role": "user", "content": message})
        confirmed = _user_confirmed(message)
        real_budget = [1 if confirmed else 0]   # 本轮最多允许 1 次真实动作，防连环误触发

        def execute(name: str, args: dict) -> str:
            allow = False
            if confirmed and name in ("run_task", "schedule_interview") and real_budget[0] > 0:
                allow = True
                real_budget[0] -= 1
            return bt.execute(name, args, allow_real=allow)

        reply, actions, err = run_agent(msgs, bt.TOOLS, execute)
        # 动作摘要（去重，给前端做透明化展示：伯乐真的做了什么）
        acted = []
        for a in actions:
            label = _TOOL_LABELS.get(a["name"], a["name"])
            if label not in acted:
                acted.append(label)
        return {"ok": bool(reply), "reply": reply or "", "error": err, "actions": acted}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reply": "", "error": str(e), "actions": []}


def bole_suggest(history: list[dict], reply: str) -> dict:
    """基于伯乐最新回复动态生成快捷回复 chips（单独接口，不拖慢主回复显示）。"""
    try:
        from app.bole_agent import suggest_replies
        msgs = [m for m in history if m.get("role") in ("user", "assistant")][-6:]
        return {"ok": True, "suggestions": suggest_replies(msgs, reply)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "suggestions": [], "error": str(e)}


# ────────────────────── 后台任务 ──────────────────────
def run_subcmd_json(rel: str, args: list[str], timeout: int = 30) -> dict:
    try:
        r = subprocess.run([PY, str(ROOT / rel), *args],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        for line in reversed((r.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": (r.stderr or "无 JSON 输出")[:500]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def launch_job(task: str, params: dict) -> dict:
    if task not in _TASKS:
        return {"ok": False, "error": f"未知任务: {task}"}
    rel, limit_flag = _TASKS[task]
    # -u 无缓冲：子脚本 print 到管道默认块缓冲，会导致日志攒到最后才出；
    # 加 -u 让 stdout 实时逐行刷出，操作台才能真正「实时」看到执行输出。
    cmd = [PY, "-u", str(ROOT / rel)]
    try:
        if task == "pipeline":
            for k in ("greet", "collect", "chat"):
                if params.get(k) is not None:
                    cmd += [f"--{k}", str(int(params[k]))]
        elif limit_flag is not None and params.get("limit") is not None:
            cmd += [limit_flag, str(int(params["limit"]))]
    except (TypeError, ValueError):
        return {"ok": False, "error": "人数必须是整数"}
    if params.get("min_degree") in ("大专", "本科", "硕士", "博士"):
        cmd += ["--min-degree", params["min_degree"]]
    if params.get("dry_run"):
        cmd += ["--dry-run"]

    job_id = uuid.uuid4().hex[:12]
    # dropped = 已从窗口头部丢弃的行数；游标语义 = dropped + len(log)，
    # 保证截断后前端 since 游标不会卡死（截断前后游标单调递增，见 job_state）。
    job = {"id": job_id, "task": task, "status": "running", "log": [],
           "dropped": 0, "started": time.time(), "proc": None}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def worker() -> None:
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            job["proc"] = proc
            for line in iter(proc.stdout.readline, ""):
                with _JOBS_LOCK:
                    job["log"].append(line.rstrip("\n"))
                    if len(job["log"]) > 5000:  # 滚动窗口：丢弃头部并累加 dropped
                        excess = len(job["log"]) - 5000
                        job["log"] = job["log"][excess:]
                        job["dropped"] += excess
            proc.wait()
            job["status"] = "done" if proc.returncode == 0 else "failed"
            job["returncode"] = proc.returncode
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            with _JOBS_LOCK:
                job["log"].append(f"[server] 启动失败: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id, "cmd": " ".join(cmd[1:])}


def job_state(job_id: str, since: int = 0) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "任务不存在"}
        dropped = job["dropped"]
        total = dropped + len(job["log"])          # 绝对游标（含已丢弃行）
        start = max(0, since - dropped)            # since 是绝对索引，换算成窗口内下标
        return {"ok": True, "status": job["status"], "next": total,
                "log": job["log"][start:], "returncode": job.get("returncode")}


def stop_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        job["status"] = "stopped"
    return {"ok": True, "status": job["status"]}


def terminate_all_jobs() -> None:
    """关 App / 退出前，杀掉所有在跑的任务子进程，绝不留驱动 Chrome 的孤儿进程。"""
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        proc = job.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                job["status"] = "stopped"
            except Exception:  # noqa: BLE001
                pass
