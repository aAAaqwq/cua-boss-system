#!/usr/bin/env python3
# © 2026 Daniel Li (Open CAIO). 伯乐 AI 招聘助手 · 版权所有 All rights reserved.
"""desktop/server.py — 伯乐桌面端本地服务（纯标准库，只做 HTTP 路由）

只监听 127.0.0.1，把浏览器 UI（desktop/ui）桥接到 desktop/services.py 的真实业务。
业务逻辑全在 services.py；这里只负责解析请求、分发、序列化响应。零第三方依赖。
即将来 Tauri 壳的 WebView 前端 + 本地后端（打包直接复用，不重写）。

用法:
  python desktop/server.py                 # 起服务 + 开浏览器（默认 127.0.0.1:8765）
  python desktop/server.py --port 8888 --no-open
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
from desktop import services as S  # noqa: E402

UI_DIR = Path(__file__).parent / "ui"
_CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
           ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}
_SRV = None  # 供 /api/shutdown 优雅停服（在主循环里赋值）


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ── 响应助手 ──
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        # 只解析 application/json：挡掉 text/plain「简单请求」绕过 CORS 预检的 CSRF 手法
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            return {}
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 2_000_000:  # 上限 2MB，防超大 body 占内存
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _origin_ok(self) -> bool:
        """CSRF 防护：若带 Origin(浏览器跨站请求必带)，必须与本机 Host 同源。
        无 Origin(同源导航/curl 等非浏览器)放行。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin == f"http://{self.headers.get('Host', '')}"

    def _guard(self, require_auth: bool) -> bool:
        """统一 /api 门禁：先查同源(CSRF)，再按需查登录(许可门禁)。挡下则已回响应。"""
        if not self._origin_ok():
            self._json({"ok": False, "error": "跨站请求被拒绝"}, 403)
            return False
        if require_auth and not S.auth_status().get("logged_in"):
            self._json({"ok": False, "error": "未登录，请先登录"}, 401)
            return False
        return True

    def _qs(self, key: str, default: str = "") -> str:
        return parse_qs(urlparse(self.path).query).get(key, [default])[0]

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if not target.is_relative_to(UI_DIR.resolve()) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(),
                   _CTYPES.get(target.suffix, "application/octet-stream"))

    # ── GET ──
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth":                       # 判断是否登录，开放（仅同源）
            if self._guard(require_auth=False):
                self._json(S.auth_status())
            return
        if path.startswith("/api/"):                  # 其余 API 一律需登录 + 同源
            if not self._guard(require_auth=True):
                return
            if path == "/api/doctor":
                self._json(S.run_subcmd_json("scripts/doctor.py", ["--json"]))
            elif path == "/api/dashboard":
                self._json(S.dashboard())
            elif path == "/api/config":
                self._json(S.config_status())
            elif path == "/api/candidate":
                self._json(S.candidate_detail(self._qs("uid")))
            elif path == "/api/resume":
                self._serve_resume(self._qs("uid"))
            elif path.startswith("/api/job/"):
                self._json(S.job_state(path.rsplit("/", 1)[-1], int(self._qs("since", "0") or 0)))
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
            return
        self._serve_static(path)

    def _serve_resume(self, uid: str) -> None:
        p = S.resume_pdf_path(uid)
        if not p:
            self._send(404, b"resume not found", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", "inline")
        data = p.read_bytes()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── POST ──
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # 登录/登出：仅同源(防登录 CSRF)，无需已登录
        if path in ("/api/auth/login", "/api/auth/logout"):
            if not self._guard(require_auth=False):
                return
            body = self._body()
            self._json(S.do_login(body.get("email", ""), body.get("password", ""))
                       if path.endswith("login") else S.do_logout())
            return
        # 其余 POST：同源 + 已登录
        if not self._guard(require_auth=True):
            return
        body = self._body()
        if path == "/api/bole":
            self._json(S.bole_reply(body.get("message", ""), body.get("history", [])))
        elif path == "/api/bole/suggest":
            self._json(S.bole_suggest(body.get("history", []), body.get("reply", "")))
        elif path == "/api/config":
            self._json(S.save_config(body))
        elif path == "/api/config/test":
            self._json(S.test_deepseek())
        elif path == "/api/candidate/rescore":
            self._json(S.rescore(body.get("uid", "")))
        elif path == "/api/run":
            self._json(S.launch_job(body.get("task", ""), body.get("params", {})))
        elif path == "/api/shutdown":
            self._json({"ok": True})
            # 先杀在跑的子进程再停服；另起线程避免在处理线程里等自身
            threading.Thread(target=_do_shutdown, daemon=True).start()
        elif path.startswith("/api/job/") and path.endswith("/stop"):
            self._json(S.stop_job(path.split("/")[3]))
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)


def _do_shutdown() -> None:
    """优雅停服：先杀在跑的自动化子进程，再停 HTTP 循环。"""
    S.terminate_all_jobs()
    if _SRV is not None:
        _SRV.shutdown()


def main() -> None:
    global _SRV
    p = argparse.ArgumentParser(description="伯乐桌面端本地服务")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-open", action="store_true")
    args = p.parse_args()

    # 端口被占用（多为已双击过一次）→ 自动往后找空闲端口，不抛栈
    srv = None
    for port in range(args.port, args.port + 12):
        try:
            srv = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print(f"❌ {args.host}:{args.port}~{args.port + 11} 都被占用了。"
              f"伯乐可能已在运行——去浏览器看看，或换 --port。")
        sys.exit(1)
    _SRV = srv

    url = f"http://{args.host}:{srv.server_address[1]}/"
    print(f"🎯 伯乐桌面端已启动 → {url}")
    print("   关闭：Ctrl-C（或直接关这个窗口）")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # 退出 App / kill 时（SIGTERM）也干净关闭，不留僵尸端口
    def _on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
        S.terminate_all_jobs()   # 关 App 前先杀掉在跑的 Chrome 自动化子进程，绝不留孤儿
        srv.shutdown()
        print("已退出。")


if __name__ == "__main__":
    main()
