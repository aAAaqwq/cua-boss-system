#!/usr/bin/env python3
"""
BOSS直聘 — 点击"不合适"按钮（共享模块）

cua_collect.py 和 cua_chat_loop.py 通过此模块统一触发"不合适"操作。

流程:
  1. AX 检测 "不合适" → 提取 element_index
  2. JS dispatchEvent 触发 hover（mouseenter/mouseover），展开下拉面板
  3. AX 轮询等待面板展开（检测 "标为不合适" 出现），最多等 15s
  4. cua-driver click（element_index，AX 路径，无坐标依赖）
     → 兜底: JS el.click()
  5. 最终 AX 验证（薪资不符/学历不符/确认）

用法:
  from scripts.boss_click_buheshi import click_buheshi

  ok = click_buheshi(pid, wid)
"""

import json
import re
import subprocess
import sys
import time

# ══════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════

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
    cmd = ["cua-driver", "call"] + list(args)
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
# AX element_index 提取（替代 CGEvent 坐标定位）
# ══════════════════════════════════════════════════

def _find_element_index(text: str, pid: int, wid: int) -> int | None:
    """在 AX 树中查找包含指定文本的可交互元素，返回 element_index

    遍历 get_window_state 返回的 tree_markdown，匹配首个包含 text 且
    带有 [N] 标记的行，提取并返回 N。
    """
    r = _cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax", "query": text,
    }))
    tree = r.get("tree_markdown", "")
    if not tree:
        return None
    for line in tree.split("\n"):
        if text in line:
            m = re.search(r'\[(\d+)\]', line)
            if m:
                return int(m.group(1))
    return None


# ══════════════════════════════════════════════════
# JS hover 触发（替代 CGEvent 鼠标移动）
# ══════════════════════════════════════════════════

def _trigger_hover_js(pid: int, wid: int) -> bool:
    """通过 JS dispatchEvent 在"不合适"按钮上触发 hover

    BOSS 直聘使用 React，事件委托在 document root。
    dispatchEvent({bubbles: true}) 可以穿透 React 的合成事件系统。
    同时派发 mouseenter / mouseover / pointerenter 覆盖多种事件绑定。

    Returns:
        True 如果 JS 执行成功（找到了元素并派发了事件）
    """
    js = """
(function(){
    var el = document.querySelectorAll(".operate-icon-item")[8];
    if (!el) return "no_element";

    // mouseenter + mouseover（兼容传统 onmouseenter/onmouseover）
    el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true, cancelable: true}));
    el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, cancelable: true}));

    // pointerenter（React 17+ 使用 pointer events）
    try {
        el.dispatchEvent(new PointerEvent('pointerenter', {bubbles: true, cancelable: true}));
    } catch(e) {
        // PointerEvent 构造在某些环境不可用 → 忽略
    }

    return "hovered";
})()
"""
    result = _cua_js(js, pid, wid).strip().strip('"')
    return result == "hovered"


def _click_via_js(pid: int, wid: int) -> bool:
    """JS click 兜底 — 在"不合适"按钮上触发 click 事件"""
    js = """
(function(){
    var el = document.querySelectorAll(".operate-icon-item")[8];
    if (!el) return "no_element";
    el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    // el.click() 作为兜底（触发元素默认行为 + 事件）
    try { el.click(); } catch(e) {}
    return "clicked";
})()
"""
    result = _cua_js(js, pid, wid).strip().strip('"')
    return result == "clicked"


# ══════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════

def click_buheshi(pid: int, wid: int, verbose: bool = True) -> bool:
    """点击 BOSS 直聘聊天页右侧面板的"不合适"按钮

    流程:
      Step 1: AX 检测 "不合适" + 提取 element_index
      Step 2: JS dispatchEvent 触发 hover，展开下拉面板
      Step 3: AX 轮询等待面板展开（检测 "标为不合适"），最多等 15s
      Step 4: cua-driver click（element_index AX 路径）→ 兜底 JS click
      Step 5: 最终 AX 验证

    Args:
        pid: Chrome 进程 ID
        wid: BOSS 直聘窗口 ID
        verbose: 是否打印步骤日志

    Returns:
        True 成功 / False 失败
    """
    # ── Step 1: AX 检测 + 提取 element_index ──
    if verbose:
        _log("INFO", "Step 1/3: AX 检测 '不合适' + 提取 element_index...")

    if not _ax_has_text("不合适", pid, wid):
        if verbose:
            _log("WARN", "未检测到 '不合适' — 无需操作")
        return False

    element_idx = _find_element_index("不合适", pid, wid)
    if verbose:
        if element_idx is not None:
            _log("OK", f"element_index={element_idx}")
        else:
            _log("WARN", "未找到 element_index，将使用 JS click 兜底")

    # ── Step 2: JS 触发 hover → 展开下拉面板 ──
    if verbose:
        _log("INFO", "Step 2/3: JS 触发 hover → 等待面板展开...")

    _trigger_hover_js(pid, wid)

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

    clicked = False
    if element_idx is not None:
        r = _cua("click", json.dumps({
            "pid": pid, "window_id": wid, "element_index": element_idx,
        }))
        if not r or r.get("error"):
            if verbose:
                _log("WARN", f"AX click 失败: {r.get('error', 'unknown')} → 尝试 JS click")
        else:
            clicked = True
            if verbose:
                _log("OK", "AX click 完成")

    if not clicked:
        if _click_via_js(pid, wid):
            clicked = True
            if verbose:
                _log("OK", "JS click 完成")
        else:
            if verbose:
                _log("ERROR", "JS click 也失败 — 无法定位按钮")

    if not clicked:
        return False

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
# CLI（独立调试用）
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║   BOSS '不合适' (hover → 面板 → 点击)       ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    pid, wid = _find_window()

    ok = click_buheshi(pid, wid)
    print()
    if ok:
        _log("OK", "全部完成")
    else:
        _log("WARN", "未触发（按钮不可见或不需要）")
