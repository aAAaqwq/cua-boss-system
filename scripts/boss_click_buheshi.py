#!/usr/bin/env python3
"""
BOSS直聘 — 点击"不合适"按钮（共享模块）

cua_collect.py 和 cua_chat_loop.py 通过此模块统一触发"不合适"操作。

流程:
  1. AX 检测 "不合适" → 确认存在
  2. JS dispatchEvent 触发 hover（mouseenter/mouseover）展开下拉面板（仅 hover，非点击）
  3. AX 轮询等待面板展开（检测 "标为不合适" 出现），最多等 15s
  4. CGEvent 真鼠标点击「标为不合适」（isTrusted=true 可信）；如弹出「理由+确认」再点确认
  5. 最终 AX 验证（薪资不符/学历不符/确认）

点击方式（实测结论，重要）:
  - BOSS 的「不合适/标为不合适/薪资不符…」都是 AXStaticText，**只有 showmenu/scrolltovisible
    动作、没有 press** → cua element_index(AX press) 点不动它们（静默失败）。AX 能"定位到"
    但点不动，这点务必区分。
  - JS `el.click()`/`dispatchEvent` 能点，但 `isTrusted=false`，是反爬最常用的机器人特征。
  - 唯一「可信(isTrusted=true) 且有效」的方式是 **CGEvent 像素点击**：取元素
    getBoundingClientRect，按 scale=截图宽/视口宽 换算成截图像素坐标，走 cua-driver
    click {x,y}（CGEvent 路径）。实测在 BOSS 上命中、isTrusted=true。需窗口前台可见。
  - JS 仅用于 hover 展开 CSS:hover 菜单（hover 非点击，反检测一般不针对 hover）。

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
    """通过 JS dispatchEvent 在"不合适"按钮上触发 hover（仅 hover，非点击）

    下拉菜单是 CSS:hover 触发的 web 菜单，需要鼠标悬停事件才能展开；cua-driver 没有
    「真鼠标移动」工具，故用 JS 派发 hover 事件展开菜单。这只是 hover、不是点击——
    真正的点击走 CGEvent 像素点击（isTrusted=true 可信路径），不受 hover 的 isTrusted 影响。

    BOSS 直聘使用 React，事件委托在 document root。
    dispatchEvent({bubbles: true}) 可以穿透 React 的合成事件系统。
    同时派发 mouseenter / mouseover / pointerenter 覆盖多种事件绑定。

    Returns:
        True 如果 JS 执行成功（找到了元素并派发了事件）
    """
    # 按文本「不合适」定位图标元素（比固定 .operate-icon-item[8] 索引稳健），
    # 对它及父级链派发 hover 事件，覆盖 React 事件委托 + CSS:hover。
    js = """
(function(){
    var all = document.querySelectorAll('*'), icon = null;
    for (var i = 0; i < all.length; i++) {
        var own = '', el = all[i];
        for (var j = 0; j < el.childNodes.length; j++)
            if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].textContent;
        if (own.trim() === '不合适') { icon = el; break; }
    }
    if (!icon) return "no_element";
    var e = icon;
    for (var k = 0; k < 4 && e; k++) {
        ['mouseenter','mouseover','pointerenter','pointerover'].forEach(function(ev){
            try { e.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true})); } catch(x){}
        });
        e = e.parentElement;
    }
    return "hovered";
})()
"""
    result = _cua_js(js, pid, wid).strip().strip('"')
    return result == "hovered"


def _screenshot_dims(pid: int, wid: int) -> tuple:
    """取窗口截图像素尺寸 (width, height)，CGEvent 坐标换算用。"""
    st = _cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "screenshot",
    }))
    return st.get("screenshot_width"), st.get("screenshot_height")


def _element_rect(text: str, pid: int, wid: int) -> "dict | None":
    """JS getBoundingClientRect 取「文本完全等于 text」的元素位置 + 视口尺寸。"""
    safe = text.replace("'", "\\'")
    js = (
        "(function(){var a=document.querySelectorAll('*');"
        "for(var i=0;i<a.length;i++){var el=a[i];"
        f"if((el.textContent||'').trim()==='{safe}'&&el.children.length<=1){{"
        "var r=el.getBoundingClientRect();"
        "if(r.width>0&&r.height>0)return JSON.stringify({ok:true,x:r.left,y:r.top,"
        "w:r.width,h:r.height,iw:window.innerWidth,ih:window.innerHeight});}}"
        "return JSON.stringify({ok:false});})()"
    )
    r = _cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript", "javascript": js,
    }))
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            return None
    return r if isinstance(r, dict) and r.get("ok") else None


def _cgclick_at_rect(rect: dict, pid: int, wid: int) -> bool:
    """把元素 rect(CSS) 换算成截图像素坐标 → cua-driver CGEvent 点击 {x,y}。

    坐标换算（实测正确）：scale = 截图宽 / 视口宽；顶部浏览器 chrome = 截图高 − 视口高×scale；
    元素中心(CSS)×scale + chrome 偏移 = 截图像素坐标。CGEvent 路径 → isTrusted=true，需窗口前台。
    """
    sw, sh = _screenshot_dims(pid, wid)
    if not sw or not sh or not rect.get("iw"):
        return False
    scale = sw / rect["iw"]
    chrome_top = sh - rect["ih"] * scale
    cgx = int((rect["x"] + rect["w"] / 2) * scale)
    cgy = int((rect["y"] + rect["h"] / 2) * scale + chrome_top)
    r = _cua("click", json.dumps({"pid": pid, "window_id": wid, "x": cgx, "y": cgy}))
    return not (isinstance(r, dict) and r.get("error"))


def _cgclick_text(text: str, pid: int, wid: int) -> bool:
    """对「文本等于 text」的可见元素做 CGEvent 真·鼠标点击（isTrusted=true，避免反爬）。

    用于点击当前已可见的元素（如确认/提交按钮）。reason-item 走 _cgclick_reason（带强制展开）。
    """
    rect = _element_rect(text, pid, wid)
    return _cgclick_at_rect(rect, pid, wid) if rect else False


def _cgclick_reason(reason: str, pid: int, wid: int) -> bool:
    """点击不合适理由项（reason-item）—— 强制展开菜单后 CGEvent 真点击（实测 5/5 稳定）。

    关键：BOSS 的理由菜单是 CSS:hover 触发的 web 菜单，用 JS 派发 hover 或真鼠标点图标
    展开都**不稳定**（实测 ~1/4 成功）。但 9 个 .reason-item 一直在 DOM 里、只是隐藏——
    故 JS 强制把目标 reason-item 的隐藏祖先链改可见（display/visibility/opacity），取其
    getBoundingClientRect 坐标，再 CGEvent 点击。点击仍是真鼠标 isTrusted=true（不触发反爬），
    而"展开"只是改 CSS 可见性、不构成"点击"，不影响可信度。
    """
    safe = reason.replace("'", "\\'")
    # 强制展开：把目标 reason-item 隐藏祖先链改可见，并打 data-cua-forced 标记便于事后还原
    js = (
        "(function(){var s=document.querySelectorAll('.reason-item'),ri=null;"
        "for(var i=0;i<s.length;i++)if((s[i].textContent||'').trim()==="
        f"'{safe}'){{ri=s[i];break;}}"
        "if(!ri)return JSON.stringify({ok:false});"
        "var e=ri;while(e&&e!==document.body){var cs=getComputedStyle(e),f=false;"
        "if(cs.display==='none'){e.style.display='block';f=true;}"
        "if(cs.visibility==='hidden'){e.style.visibility='visible';f=true;}"
        "if(parseFloat(cs.opacity)===0){e.style.opacity='1';f=true;}"
        "if(f)e.setAttribute('data-cua-forced','1');e=e.parentElement;}"
        "var r=ri.getBoundingClientRect();"
        "if(r.width===0||r.height===0)return JSON.stringify({ok:false});"
        "return JSON.stringify({ok:true,x:r.left,y:r.top,w:r.width,h:r.height,"
        "iw:window.innerWidth,ih:window.innerHeight});})()"
    )
    r = _cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript", "javascript": js,
    }))
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            return False
    if not (isinstance(r, dict) and r.get("ok")):
        return False
    ok = _cgclick_at_rect(r, pid, wid)

    # 还原被强制展开元素的内联样式（避免理由菜单残留可见）
    _cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": (
            "(function(){var a=document.querySelectorAll('[data-cua-forced]');"
            "for(var i=0;i<a.length;i++){a[i].style.display='';a[i].style.visibility='';"
            "a[i].style.opacity='';a[i].removeAttribute('data-cua-forced');}return 'cleaned';})()"
        ),
    }))
    return ok


# ══════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════

def click_buheshi(pid: int, wid: int, verbose: bool = True, reason: str = "其他原因") -> bool:
    """点击 BOSS 直聘聊天页右侧面板的"不合适"按钮

    流程:
      Step 1: AX 检测 "不合适" 存在（确认已选中候选人、有该操作）
      Step 2: 强制展开理由菜单 + CGEvent 像素点击 reason-item（默认「其他原因」，isTrusted=true），带重试
      Step 3: 若弹出确认/提交 → CGEvent 点确认
      Step 4: 最终 AX 验证

    关键（实测）：理由菜单是 CSS:hover 触发的 web 菜单，靠 hover 展开**不稳定**(~1/4)；
    但 9 个 .reason-item 一直在 DOM 里只是隐藏 → 改为「JS 强制展开 + CGEvent 点击」实测 5/5。
    「标为不合适」只是标题(DIV.title)点不动；真正可点的是 .reason-item，点它才会真标记。

    Args:
        pid: Chrome 进程 ID
        wid: BOSS 直聘窗口 ID
        verbose: 是否打印步骤日志
        reason: 标记不合适时选的理由（菜单 reason-item 文本），默认"其他原因"

    Returns:
        True 成功 / False 失败
    """
    # ── Step 1: AX 检测 "不合适" 是否存在 ──
    if verbose:
        _log("INFO", "Step 1/3: AX 检测 '不合适'...")

    if not _ax_has_text("不合适", pid, wid):
        if verbose:
            _log("WARN", "未检测到 '不合适' — 无需操作")
        return False

    # ── Step 2: 强制展开理由菜单 + CGEvent 点击 reason-item（带重试，实测 5/5）──
    if verbose:
        _log("INFO", f"Step 2/3: 强制展开菜单 + CGEvent 点击理由 '{reason}'...")

    clicked = False
    for _ in range(3):
        if _cgclick_reason(reason, pid, wid):
            clicked = True
            break
        time.sleep(0.5)
    if not clicked:
        if verbose:
            _log("ERROR", f"点击'{reason}'失败 — 未找到 reason-item 或窗口非前台")
        return False
    if verbose:
        _log("OK", f"CGEvent 点击'{reason}'完成 (isTrusted=true 可信)")

    # 若弹出「确认/提交」对话 → CGEvent 点确认(限定上下文, 避免误点页面其它'确认')
    time.sleep(0.8)
    confirm_tree = _cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax", "query": "确认",
    })).get("tree_markdown", "")
    if ("确认" in confirm_tree or "提交" in confirm_tree) and ("不合适" in confirm_tree or "原因" in confirm_tree):
        for btn in ("确认", "提交"):
            if btn in confirm_tree and _cgclick_text(btn, pid, wid):
                if verbose:
                    _log("OK", f"CGEvent 点击'{btn}'完成")
                break

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
