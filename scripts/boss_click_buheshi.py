#!/usr/bin/env python3
"""
BOSS直聘 — 点击"不合适"按钮（共享模块）

cua_collect.py 和 cua_chat_loop.py 通过此模块统一触发"不合适"操作。

流程（与 boss_click_buheshi.sh 一致）:
  1. AX 检测 "不合适" → JS 获取 DOM 元素屏幕坐标
  2. macOS 原生移动鼠标到按钮上（hover），触发下拉面板
  3. AX 轮询等待面板展开（检测 "标为不合适" 出现）
  4. macOS 原生点击
  5. 最终 AX 验证（薪资不符/学历不符/确认）

用法:
  from scripts.boss_click_buheshi import click_buheshi

  ok = click_buheshi(pid, wid)
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ══════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════

HID_BIN = "/tmp/cua_hid"
BROWSER = "com.google.Chrome"

# ── 颜色输出 ──
RED = "\033[0;31m"
GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _log(level: str, msg: str) -> None:
    color = {"INFO": CYAN, "OK": GREEN, "WARN": YELLOW, "ERROR": RED}.get(level, NC)
    tag = f"{color}[{level}]{NC}"
    print(f"{tag}  {msg}", file=sys.stderr)


# ══════════════════════════════════════════════════
# cua-driver 封装
# ══════════════════════════════════════════════════

def _cua(*args: str) -> dict:
    """cua-driver 命令封装"""
    cmd = ["cua-driver"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"text": (r.stdout or "")[:200]}


def _find_window() -> tuple[int, int]:
    """查找 BOSS 直聘 Chrome 窗口 → (pid, window_id)"""
    apps = _cua("list_apps")
    pid = None
    for a in apps.get("apps", []):
        if a.get("bundle_id") == BROWSER and a.get("running"):
            pid = a["pid"]
            break
    if not pid:
        _log("ERROR", "Chrome 未运行")
        sys.exit(1)

    lw = _cua("list_windows", json.dumps({"pid": pid}))

    # 优先: 前台可见 + 标题含 BOSS直聘/zhipin
    for w in lw.get("windows", []):
        t = w.get("title", "")
        if ("BOSS直聘" in t or "zhipin" in t) and w.get("is_on_screen"):
            _log("OK", f"pid={pid} window_id={w['window_id']}")
            return pid, w["window_id"]

    # 兜底: 任何含 BOSS/zhipin 的窗口
    for w in lw.get("windows", []):
        t = w.get("title", "")
        if "BOSS" in t or "zhipin" in t:
            _log("OK", f"pid={pid} window_id={w['window_id']}")
            return pid, w["window_id"]

    _log("ERROR", "未找到 BOSS直聘窗口")
    sys.exit(1)


def _cua_js(javascript: str, pid: int, wid: int) -> str:
    """执行 JS 并返回结果字符串"""
    r = _cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        "javascript": javascript,
    }))
    if isinstance(r, list):
        return " ".join(str(x) for x in r)
    return str(r.get("result", r.get("text", "")))


def _ax_has_text(text: str, pid: int, wid: int) -> bool:
    """检查 AX 树中是否包含指定文本"""
    r = _cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax", "query": text,
    }))
    tree = r.get("tree_markdown", "")
    return text in tree


# ══════════════════════════════════════════════════
# macOS CGEvent 原生鼠标操作
# ══════════════════════════════════════════════════

def _ensure_hid() -> None:
    """编译 CGEvent Swift 鼠标工具（如不存在）"""
    if os.path.isfile(HID_BIN) and os.access(HID_BIN, os.X_OK):
        return

    _log("INFO", "编译 CGEvent 鼠标工具...")
    swift_src = r'''
import CoreGraphics; import Foundation
let a=CommandLine.arguments
guard a.count>=4,let x=Double(a[2]),let y=Double(a[3]) else {exit(1)}
let p=CGPoint(x:x,y:y);let s=CGEventSource(stateID:.hidSystemState)
switch a[1] {
case "move":
    if let m=CGEvent(mouseEventSource:s,mouseType:.mouseMoved,mouseCursorPosition:p,mouseButton:.left){m.post(tap:.cghidEventTap)}
    print("moved to (\(x),\(y))")
case "click":
    if let m=CGEvent(mouseEventSource:s,mouseType:.mouseMoved,mouseCursorPosition:p,mouseButton:.left){m.post(tap:.cghidEventTap);usleep(30000)}
    if let d=CGEvent(mouseEventSource:s,mouseType:.leftMouseDown,mouseCursorPosition:p,mouseButton:.left){d.post(tap:.cghidEventTap);usleep(10000)}
    if let u=CGEvent(mouseEventSource:s,mouseType:.leftMouseUp,mouseCursorPosition:p,mouseButton:.left){u.post(tap:.cghidEventTap)}
    print("clicked (\(x),\(y))")
default: exit(1)
}
'''
    swift_path = "/tmp/cua_hid.swift"
    Path(swift_path).write_text(swift_src)
    r = subprocess.run(
        ["swiftc", "-o", HID_BIN, swift_path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        _log("ERROR", f"swiftc 编译失败: {r.stderr}")
        sys.exit(1)
    _log("OK", "CGEvent 鼠标工具就绪")


def _hid_move(x: int, y: int) -> str:
    """原生移动鼠标到屏幕坐标 (x, y)"""
    r = subprocess.run([HID_BIN, "move", str(x), str(y)],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def _hid_click(x: int, y: int) -> str:
    """原生点击屏幕坐标 (x, y)"""
    r = subprocess.run([HID_BIN, "click", str(x), str(y)],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


# ══════════════════════════════════════════════════
# 核心: 获取"不合适"按钮屏幕坐标
# ══════════════════════════════════════════════════

def _get_pos_buheshi(pid: int, wid: int) -> tuple[int, int] | None:
    """JS 获取 .operate-icon-item[8] 的屏幕坐标（第9个操作图标="不合适"）

    Returns:
        (screen_x, screen_y) 或 None
    """
    js = """
(function(){
    var el = document.querySelectorAll(".operate-icon-item")[8];
    if (!el) return "null";
    var r = el.getBoundingClientRect();
    var sx = Math.round(window.screenX + r.x + r.width / 2);
    var sy = Math.round(window.screenY + (window.outerHeight - window.innerHeight) + r.y + r.height / 2);
    return sx + " " + sy;
})()
"""
    result = _cua_js(js, pid, wid).strip().strip('"')
    if result == "null" or not result:
        return None
    parts = result.split()
    if len(parts) >= 2:
        return int(parts[0]), int(parts[1])
    return None


# ══════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════

def click_buheshi(pid: int, wid: int, verbose: bool = True) -> bool:
    """点击 BOSS 直聘聊天页右侧面板的"不合适"按钮

    流程:
      Step 1: AX 检测 "不合适" → JS 获取屏幕坐标
      Step 2: 原生移动鼠标到按钮上（hover），触发下拉面板
      Step 3: AX 轮询等待面板展开（检测 "标为不合适"），最多等 15s
      Step 4: 原生点击
      Step 5: 最终 AX 验证

    Args:
        pid: Chrome 进程 ID
        wid: BOSS 直聘窗口 ID
        verbose: 是否打印步骤日志

    Returns:
        True 成功 / False 失败
    """
    # 确保 CGEvent 工具就绪
    _ensure_hid()

    # ── Step 1: AX 检测 + 获取坐标 ──
    if verbose:
        _log("INFO", "Step 1/3: AX 检测 '不合适' + 获取坐标...")

    if not _ax_has_text("不合适", pid, wid):
        if verbose:
            _log("WARN", "未检测到 '不合适' — 无需操作")
        return False

    pos = _get_pos_buheshi(pid, wid)
    if pos is None:
        if verbose:
            _log("ERROR", "无法定位 '不合适' 按钮")
        return False

    sx, sy = pos
    if verbose:
        _log("OK", f"坐标: ({sx}, {sy})")

    # ── Step 2: 移动鼠标到按钮上 → 触发 hover ──
    if verbose:
        _log("INFO", "Step 2/3: 移动鼠标到按钮 → 等待面板展开...")

    _hid_move(sx, sy)

    # ── Step 3: AX 轮询等待 "标为不合适" 出现 ──
    waited = 0
    max_wait = 30  # 0.5s × 30 = 15s
    while waited < max_wait:
        if _ax_has_text("标为不合适", pid, wid):
            if verbose:
                _log("OK", f"面板已展开 (等待 {waited * 0.5:.0f}s)")
            break
        time.sleep(0.5)
        waited += 1

    if waited >= max_wait:
        if verbose:
            _log("WARN", "等待超时，面板未展开 — 仍然尝试点击")

    # ── Step 4: 点击 ──
    if verbose:
        _log("INFO", "Step 3/3: 点击 '不合适'...")

    _hid_click(sx, sy)
    if verbose:
        _log("OK", "点击完成")

    # ── Step 5: 最终验证 ──
    time.sleep(0.5)
    if verbose:
        _log("INFO", "最终 AX 验证...")
        r = _cua("get_window_state", json.dumps({
            "pid": pid, "window_id": wid, "capture_mode": "ax",
            "query": "薪资不符",
        }))
        tree = r.get("tree_markdown", "")
        for keyword in ("薪资不符", "学历不符", "确认"):
            if keyword in tree:
                if verbose:
                    _log("OK", f"验证到 '{keyword}' — 操作生效")
                return True

    return True  # 点击已发出，即使未验证到也返回 True


# ══════════════════════════════════════════════════
# CLI（独立调试用 — 与 boss_click_buheshi.sh 行为一致）
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║   BOSS '不合适' (hover等待面板 → 点击)      ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    _ensure_hid()
    pid, wid = _find_window()

    ok = click_buheshi(pid, wid)
    print()
    if ok:
        _log("OK", "全部完成")
    else:
        _log("WARN", "未触发（按钮不可见或不需要）")
