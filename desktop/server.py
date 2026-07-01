#!/usr/bin/env python3
"""desktop/server.py — 伯乐桌面端本地服务（纯标准库）

P2 桌面化的引擎层：一个只监听 127.0.0.1 的本地 HTTP 服务，把浏览器 UI
（desktop/ui）桥接到项目脚本（doctor / bole / pipeline …）与 candidates.db。
零第三方依赖，双击 desktop/伯乐.command 即启动并自动开浏览器。

这一层就是将来 Tauri 壳的 WebView 前端 + 本地后端：Tauri 打包时直接复用
desktop/ui 与这些 /api 接口，无需重写。

用法:
  python desktop/server.py                 # 起服务 + 开浏览器（默认 127.0.0.1:8765）
  python desktop/server.py --port 8888 --no-open
安全:
  仅绑定回环地址；不接受外部连接。所有子进程在项目根下执行。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
UI_DIR = Path(__file__).parent / "ui"
DB_PATH = ROOT / "data" / "candidates.db"
ENV_PATH = ROOT / ".env"
PY = sys.executable or "python3"

sys.path.insert(0, str(ROOT))

# ── 可发起的后台任务白名单（防止任意命令执行）──────────────────
# key -> (脚本相对路径, 数量参数名)。数量与 min-degree/schools/dry-run 受控透传。
_TASKS = {
    "greet": ("scripts/cua_greeting_loop.py", "--limit"),
    "collect": ("scripts/cua_collect.py", "--limit"),
    "chat": ("scripts/cua_chat_loop.py", "--limit"),
    "pipeline": ("scripts/boss_pipeline.py", None),  # 用 --greet/--collect/--chat
}

# 运行中任务表：job_id -> {task, status, log[], proc, started}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ────────────────────────── .env 读写 ──────────────────────────
def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _write_env_keys(updates: dict[str, str]) -> None:
    """更新/追加 .env 中的若干键，保留其余行与注释。原子写。"""
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


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * 6}{secret[-4:]}"


# ────────────────────────── 数据看板 ──────────────────────────
def _dashboard() -> dict:
    if not DB_PATH.exists():
        return {"ok": False, "reason": "尚无数据库，先跑一次采集", "stats": {}, "top": []}
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        def scalar(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        stats = {
            "total": scalar("SELECT COUNT(*) FROM candidates"),
            "has_resume": scalar("SELECT COUNT(*) FROM candidates WHERE has_resume=1"),
            "has_wechat": scalar("SELECT COUNT(*) FROM candidates WHERE has_wechat=1"),
            "interviewed": scalar(
                "SELECT COUNT(*) FROM candidates WHERE interview_date IS NOT NULL AND interview_date!=''"),
            "scored": scalar("SELECT COUNT(*) FROM candidates WHERE score IS NOT NULL AND score>0"),
            "today": scalar(
                "SELECT COUNT(*) FROM candidates WHERE date(updated_at)=date('now','localtime')"),
        }
        rows = conn.execute(
            "SELECT name, school, degree, job_position, score, score_summary, status "
            "FROM candidates WHERE score IS NOT NULL AND score>0 "
            "ORDER BY score DESC LIMIT 8").fetchall()
        top = [dict(r) for r in rows]
        return {"ok": True, "stats": stats, "top": top}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"读库失败: {e}", "stats": {}, "top": []}
    finally:
        conn.close()


# ────────────────────────── 伯乐对话（进程内）──────────────────
def _bole_reply(message: str, history: list[dict]) -> dict:
    try:
        from app.bole_agent import chat
        msgs = [m for m in history if m.get("role") in ("user", "assistant")][-20:]
        msgs.append({"role": "user", "content": message})
        reply, err = chat(msgs)
        return {"ok": bool(reply), "reply": reply or "", "error": err}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reply": "", "error": str(e)}


# ────────────────────────── 子进程任务 ─────────────────────────
def _run_subcmd_json(rel: str, args: list[str], timeout: int = 30) -> dict:
    """跑一个输出 JSON 的脚本（doctor 等），解析 stdout 最后一行 JSON。"""
    try:
        r = subprocess.run(
            [PY, str(ROOT / rel), *args],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip().splitlines()
        for line in reversed(out):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"ok": False, "error": (r.stderr or "无 JSON 输出")[:500]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _launch_job(task: str, params: dict) -> dict:
    if task not in _TASKS:
        return {"ok": False, "error": f"未知任务: {task}"}
    rel, limit_flag = _TASKS[task]
    cmd = [PY, str(ROOT / rel)]

    if task == "pipeline":
        for k in ("greet", "collect", "chat"):
            if params.get(k) is not None:
                cmd += [f"--{k}", str(int(params[k]))]
    elif limit_flag is not None and params.get("limit") is not None:
        cmd += [limit_flag, str(params["limit"])]

    md = params.get("min_degree")
    if md in ("大专", "本科", "硕士", "博士"):
        cmd += ["--min-degree", md]
    if params.get("dry_run"):
        cmd += ["--dry-run"]

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "task": task, "status": "running",
           "log": [], "started": time.time(), "proc": None}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            job["proc"] = proc
            for line in iter(proc.stdout.readline, ""):
                with _JOBS_LOCK:
                    job["log"].append(line.rstrip("\n"))
                    if len(job["log"]) > 500:
                        job["log"] = job["log"][-500:]
            proc.wait()
            job["status"] = "done" if proc.returncode == 0 else "failed"
            job["returncode"] = proc.returncode
        except Exception as e:  # noqa: BLE001
            job["status"] = "failed"
            with _JOBS_LOCK:
                job["log"].append(f"[server] 启动失败: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id, "cmd": " ".join(cmd[1:])}


def _job_state(job_id: str, since: int = 0) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "任务不存在"}
        log = job["log"][since:]
        return {"ok": True, "status": job["status"], "next": len(job["log"]),
                "log": log, "returncode": job.get("returncode")}


def _stop_job(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    proc = job.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        job["status"] = "stopped"
    return {"ok": True, "status": job["status"]}


# ────────────────────────── HTTP 处理 ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # 静态文件（仅 UI 目录，防目录穿越）
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if not str(target).startswith(str(UI_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/doctor":
            self._json(_run_subcmd_json("scripts/doctor.py", ["--json"]))
        elif path == "/api/dashboard":
            self._json(_dashboard())
        elif path == "/api/config":
            env = _read_env()
            self._json({
                "ok": True,
                "deepseek_key_masked": _mask(env.get("DEEPSEEK_API_KEY", "")),
                "deepseek_key_set": bool(env.get("DEEPSEEK_API_KEY")),
                "deepseek_model": env.get("DEEPSEEK_MODEL", "deepseek-chat"),
                "deepseek_base_url": env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                "cloud_sync": env.get("CLOUD_SYNC", "on"),
            })
        elif path.startswith("/api/job/"):
            job_id = path.rsplit("/", 1)[-1]
            since = int(urlparse(self.path).query.split("since=")[-1] or 0) \
                if "since=" in self.path else 0
            self._json(_job_state(job_id, since))
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/bole":
            self._json(_bole_reply(body.get("message", ""), body.get("history", [])))
        elif path == "/api/config":
            self._json(self._save_config(body))
        elif path == "/api/run":
            self._json(_launch_job(body.get("task", ""), body.get("params", {})))
        elif path.startswith("/api/job/") and path.endswith("/stop"):
            self._json(_stop_job(path.split("/")[3]))
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)

    def _save_config(self, body: dict) -> dict:
        updates: dict[str, str] = {}
        key = (body.get("deepseek_api_key") or "").strip()
        # 前端只在用户真正改 key 时才传；掩码原样回传时忽略
        if key and "•" not in key:
            updates["DEEPSEEK_API_KEY"] = key
            os.environ["DEEPSEEK_API_KEY"] = key  # 让进程内伯乐即时生效
        model = (body.get("deepseek_model") or "").strip()
        if model:
            updates["DEEPSEEK_MODEL"] = model
            os.environ["DEEPSEEK_MODEL"] = model
        base = (body.get("deepseek_base_url") or "").strip()
        if base:
            updates["DEEPSEEK_BASE_URL"] = base
            os.environ["DEEPSEEK_BASE_URL"] = base
        cloud = body.get("cloud_sync")
        if cloud in ("on", "off"):
            updates["CLOUD_SYNC"] = cloud
            os.environ["CLOUD_SYNC"] = cloud
        if not updates:
            return {"ok": False, "error": "没有要保存的改动"}
        try:
            _write_env_keys(updates)
            return {"ok": True, "saved": list(updates.keys())}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"写 .env 失败: {e}"}


def main() -> None:
    p = argparse.ArgumentParser(description="伯乐桌面端本地服务")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = p.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"🎯 伯乐桌面端已启动 → {url}")
    print("   关闭：Ctrl-C（或直接关这个窗口）")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        srv.shutdown()


if __name__ == "__main__":
    main()
