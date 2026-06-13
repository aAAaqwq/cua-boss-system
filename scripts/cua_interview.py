#!/usr/bin/env python3
"""
BOSS直聘 — 约面试自动化

通过 UID 或姓名找到候选人，打开对话，填写并发送面试邀请。

流程:
  ① 从数据库查询候选人 → 获取姓名/岗位
  ② 进入沟通页 → 找到候选人 → 打开对话
  ③ 点击「约面试」按钮打开面试邀请表单
  ④ 填写表单: 面试类型 → 日期 → 时间
  ⑤ 点击「发送」

用法:
  python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30
  python scripts/cua_interview.py --uid 12345678 --type 线上 --date 2026-06-20 --time 14:30
  python scripts/cua_interview.py --uid 12345678 --date 2026-06-20 --time 14:30 --dry-run
  python scripts/cua_interview.py --name 张三 --date 2026-06-20 --time 10:00 --no-db
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import init_db, DB_PATH

# ── Constants ──────────────────────────────────────────────────────────────

SESSION = "boss-interview"
CHROME = "com.google.Chrome"
CHAT_URL = "https://www.zhipin.com/web/chat/index"

# Interview type → UI button text variants (in priority order)
INTERVIEW_TYPE_TEXTS = {
    "线上": ["线上面试", "线上"],
    "线下": ["线下面试", "线下"],
}

# cua-driver text response error patterns
_CUA_ERROR_PATTERNS = [
    "not found in cache", "No cached", "Call get_window_state first",
    "failed", "error", "Error", "invalid", "Invalid",
    "could not", "Could not", "unable", "Unable",
    "denied", "permission", "timeout", "not found",
]


# ── cua-driver wrapper ─────────────────────────────────────────────────────

def cua(*args: str) -> dict:
    """Call cua-driver CLI. Returns dict, or {"error": ...} on failure."""
    cmd = ["cua-driver", "call"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    stdout = r.stdout.strip()
    stderr = r.stderr.strip()

    if r.returncode != 0:
        err_msg = stderr or stdout or f"exit code {r.returncode}"
        return {"error": err_msg[:300]}

    if not stdout:
        return {}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    text = stdout[:300]
    for pat in _CUA_ERROR_PATTERNS:
        if pat.lower() in text.lower():
            return {"error": text}
    return {"text": text}


# ── Helpers ─────────────────────────────────────────────────────────────────

def ax_tree(pid: int, wid: int) -> str:
    """Return AX tree markdown for the given window."""
    return cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax"
    })).get("tree_markdown", "")


def _find_element_index(text: str, pid: int, wid: int) -> int | None:
    """Find AX element_index of the first AXButton/AXLink/AXRadioButton
    containing `text` in its label."""
    tree = ax_tree(pid, wid)
    for line in tree.split("\n"):
        if text in line and any(t in line for t in (
            "AXButton", "AXLink", "AXRadioButton", "AXTextField",
            "AXTextArea", "AXPopUpButton",
        )):
            m = re.search(r'\[(\d+)\]', line)
            if m:
                return int(m.group(1))
    return None


def _dump_ax_tree(pid: int, wid: int, label: str = "", max_lines: int = 80) -> None:
    """Print AX tree for debugging."""
    tree = ax_tree(pid, wid)
    lines = tree.split("\n")
    print(f"\n  [{label}] AX tree ({len(lines)} lines):")
    for line in lines[:max_lines]:
        print(f"    {line}")
    if len(lines) > max_lines:
        print(f"    ... ({len(lines) - max_lines} more lines)")


def _js(pid: int, wid: int, javascript: str) -> dict:
    """Execute JavaScript in the page. Returns parsed JSON result."""
    return cua("page", json.dumps({
        "pid": pid, "window_id": wid,
        "action": "execute_javascript",
        "javascript": javascript,
    }))


# ── Session & Navigation ───────────────────────────────────────────────────

def start_session() -> None:
    """Ensure cua-driver is running and start a named session."""
    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True)
        time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION}))


def find_boss_window() -> dict:
    """Return {"pid": int, "window_id": int} for the BOSS直聘 Chrome window."""
    apps = cua("list_apps")
    chrome_pid = None
    for a in apps.get("apps", []):
        if a.get("bundle_id") == CHROME and a.get("running"):
            chrome_pid = a["pid"]
            break
    if not chrome_pid:
        print("❌ Chrome 未运行")
        sys.exit(1)

    lw = cua("list_windows", json.dumps({"pid": chrome_pid}))
    # Priority: title contains "zhipin" or "BOSS" + is_on_screen
    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("zhipin" in title or "BOSS" in title) and w.get("is_on_screen"):
            print(f"  ✓ BOSS窗口: pid={chrome_pid} wid={w['window_id']} title=\"{title[:50]}\"")
            return {"pid": chrome_pid, "window_id": w["window_id"]}
    # Fallback: any BOSS window
    for w in lw.get("windows", []):
        title = w.get("title", "")
        if "zhipin" in title or "BOSS" in title:
            print(f"  ⚠ BOSS窗口(非前台): pid={chrome_pid} wid={w['window_id']}")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    print("❌ 找不到 BOSS直聘窗口")
    sys.exit(1)


def navigate_to_chat(pid: int, wid: int) -> None:
    """Navigate to the BOSS chat page and wait for it to load."""
    print("  导航到沟通页...")
    # JS SPA navigation
    _js(pid, wid, f'window.location.href = "{CHAT_URL}"')
    time.sleep(3)
    # Hard refresh fallback
    cua("hotkey", json.dumps({"pid": pid, "window_id": wid, "keys": ["cmd", "r"]}))
    # Wait for page to load
    for delay in [0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 3.0, 3.0]:
        time.sleep(delay)
        r = _js(pid, wid, "document.readyState")
        if isinstance(r, dict) and r.get("text", "").strip() == "complete":
            snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": wid}))
            if snap.get("element_count", 0) > 300:
                print("  ✓ 沟通页已加载")
                return
    print("  ⚠ 页面加载超时，继续尝试...")


# ── Contact Operations ─────────────────────────────────────────────────────

def click_contact(pid: int, wid: int, name: str) -> tuple[bool, str | None]:
    """Click a contact by name in the sidebar to open conversation.
    Returns (clicked: bool, uid: str|None)."""
    clean = name.replace("'", "\\'").replace('"', '\\"')
    r = _js(pid, wid, f"""
    (function(){{
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.textContent || '').trim() === '{clean}' &&
                el.children.length <= 1 && el.offsetWidth > 0) {{
                // Extract uid from parent data-id
                var uid = null;
                for (var p = el; p && p !== document.body; p = p.parentElement) {{
                    var did = p.getAttribute('data-id');
                    if (did) {{ uid = did.replace(/-\\d+$/, ''); break; }}
                }}
                // Find clickable ancestor
                for (var lvl = 0; lvl < 8; lvl++) {{
                    if (el.onclick || getComputedStyle(el).cursor === 'pointer') {{
                        el.click();
                        return JSON.stringify({{status:'clicked', uid: uid}});
                    }}
                    el = el.parentElement; if (!el) break;
                }}
                return JSON.stringify({{status:'not_clickable', uid: uid}});
            }}
        }}
        return JSON.stringify({{status:'not_found', uid: null}});
    }})()
    """)
    if isinstance(r, dict):
        return (r.get("status") == "clicked", r.get("uid"))
    return (False, None)


def click_yuemian_button(pid: int, wid: int) -> bool:
    """Click the '约面试' button in the conversation toolbar.

    BOSS renders 约面试 inside a hidden popover dropdown at
    .operate-icon-item[7] > .popover > .popover-wrap > ul.more-list > li.
    We must reveal the popover first, then click the <li> item."""
    print("  点击「约面试」按钮...")
    r = _js(pid, wid, """
    (function(){
        var items = document.querySelectorAll('.operate-icon-item');
        if (items.length <= 7)
            return JSON.stringify({status:'not_found', count: items.length});

        var el = items[7];

        // Step 1: reveal the hidden popover by removing mini-hide and forcing display
        var popover = el.querySelector('.popover');
        if (popover) {
            popover.classList.remove('mini-hide');
            popover.classList.remove('popover-top');
            popover.style.position = 'static';
            popover.style.display = 'block';
        }
        var wrap = el.querySelector('.popover-wrap');
        if (wrap) {
            wrap.style.display = 'block';
            wrap.style.position = 'static';
        }

        // Step 2: find the <li> with "约面试" text and click it
        var allLi = el.querySelectorAll('li');
        for (var i = 0; i < allLi.length; i++) {
            var t = (allLi[i].textContent || '').trim();
            if (t === '约面试') {
                allLi[i].click();
                allLi[i].dispatchEvent(new MouseEvent('click', {bubbles: true}));
                return JSON.stringify({status:'clicked', via:'popover-li'});
            }
        }
        return JSON.stringify({status:'no_yuemian_li', liCount: allLi.length});
    })()
    """)
    return isinstance(r, dict) and r.get("status") == "clicked"


def wait_for_interview_form(pid: int, wid: int, timeout: float = 5.0) -> bool:
    """Wait for the interview invitation form modal to appear.
    Checks multiple selectors because BOSS renders the form in a dialog wrapper."""
    for _ in range(int(timeout * 2)):
        time.sleep(0.5)
        r = _js(pid, wid, """
        (function(){
            // Check inner form container
            var dlg = document.querySelector('.interview-invite-ui');
            if (dlg && getComputedStyle(dlg).display !== 'none' &&
                dlg.getBoundingClientRect().width > 100) {
                return JSON.stringify({found:true, via:'interview-invite-ui'});
            }
            // Check outer boss dialog wrapper
            var outer = document.querySelector('.boss-popup__wrapper.interview, ' +
                '.boss-dialog.interview, [class*="interview"][class*="dialog"]');
            if (outer && getComputedStyle(outer).display !== 'none' &&
                outer.getBoundingClientRect().width > 100) {
                return JSON.stringify({found:true, via:'boss-dialog'});
            }
            // Check for heading "线上面试邀请" or "线下面试邀请"
            var headings = document.querySelectorAll('*');
            for (var i = 0; i < headings.length; i++) {
                var t = (headings[i].textContent || '').trim();
                if ((t === '线上面试邀请' || t === '线下面试邀请') &&
                    headings[i].offsetWidth > 0 && headings[i].offsetHeight > 0) {
                    return JSON.stringify({found:true, via:'heading'});
                }
            }
            return JSON.stringify({found:false});
        })()
        """)
        if isinstance(r, dict) and r.get("found"):
            return True
    return False


# ── Form Operations ────────────────────────────────────────────────────────

def select_interview_type(interview_type: str, pid: int, wid: int) -> bool:
    """Select interview type (线上/线下) by clicking the AXRadioButton."""
    type_texts = INTERVIEW_TYPE_TEXTS.get(interview_type, [interview_type])
    tree = ax_tree(pid, wid)

    # Find AXRadioButton matching interview type
    for line in tree.split("\n"):
        for text in type_texts:
            if text in line and "AXRadioButton" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    idx = int(m.group(1))
                    print(f"  选择面试类型: {text} (idx={idx})")
                    r = cua("click", json.dumps({
                        "pid": pid, "window_id": wid,
                        "element_index": idx,
                    }))
                    if not r.get("error"):
                        time.sleep(0.3)
                        return True

    # Fallback: JS click on radio label
    print(f"  ⚠ AXRadioButton 未找到，尝试 JS...")
    r = _js(pid, wid, f"""
    (function(){{
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.textContent || '').trim() === '{type_texts[0]}' &&
                el.offsetWidth > 0) {{
                el.click();
                el.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));
                return JSON.stringify({{clicked:true}});
            }}
        }}
        return JSON.stringify({{clicked:false}});
    }})()
    """)
    return isinstance(r, dict) and r.get("clicked") is True


def open_date_picker(pid: int, wid: int) -> bool:
    """Click the date picker field to expand the calendar, then verify."""
    # Find the date input — look for placeholder="选择日期"
    r = _js(pid, wid, """
    (function(){
        var input = document.querySelector('input[placeholder*="选择日期"]');
        if (input) {
            input.click();
            input.focus();
            return JSON.stringify({status:'clicked', via:'js'});
        }
        return JSON.stringify({status:'not_found'});
    })()
    """)
    if isinstance(r, dict) and r.get("status") == "clicked":
        print("    日期选择器已打开 (JS)")
        time.sleep(1)
        # Verify calendar expanded
        return _wait_calendar_expanded(pid, wid)

    # Fallback: AX click
    idx = _find_element_index("选择日期", pid, wid)
    if idx:
        print(f"    尝试 AX 点击日期字段 (idx={idx})")
        cua("click", json.dumps({
            "pid": pid, "window_id": wid, "element_index": idx,
        }))
        time.sleep(1)
        return _wait_calendar_expanded(pid, wid)
    return False


def _wait_calendar_expanded(pid: int, wid: int, timeout: float = 3.0) -> bool:
    """Wait for the calendar day cells to appear."""
    for _ in range(int(timeout * 2)):
        time.sleep(0.5)
        r = _js(pid, wid, """
        (function(){
            var cells = document.querySelectorAll('.cell.day');
            if (cells.length > 5) {
                var enabled = 0;
                for (var i = 0; i < cells.length; i++) {
                    if (cells[i].className.indexOf('disabled') === -1) enabled++;
                }
                return JSON.stringify({expanded:true, total:cells.length, enabled:enabled});
            }
            return JSON.stringify({expanded:false, total:cells.length});
        })()
        """)
        if isinstance(r, dict) and r.get("expanded"):
            print(f"    日历已展开 ({r.get('total')} 格, {r.get('enabled')} 可选)")
            return True
    return False


def click_calendar_day(day: int, pid: int, wid: int) -> bool:
    """Click a specific day number in the expanded calendar.
    Days with class 'cell day disabled' (past dates) are skipped."""
    day_str = str(day)
    r = _js(pid, wid, f"""
    (function(){{
        var cells = document.querySelectorAll('.cell.day');
        for (var i = 0; i < cells.length; i++) {{
            var el = cells[i];
            var t = (el.textContent || '').trim();
            if (t === '{day_str}' && el.className.indexOf('disabled') === -1) {{
                el.click();
                el.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));
                return JSON.stringify({{clicked:true, day:{day}}});
            }}
        }}
        // Try today marker
        for (var i = 0; i < cells.length; i++) {{
            if (cells[i].className.indexOf('today') !== -1 && {day} === new Date().getDate()) {{
                cells[i].click();
                return JSON.stringify({{clicked:true, day:{day}, via:'today'}});
            }}
        }}
        return JSON.stringify({{clicked:false, day:{day}}});
    }})()
    """)
    return isinstance(r, dict) and r.get("clicked") is True


def open_time_picker(pid: int, wid: int) -> bool:
    """Click the time picker field to expand time selection, then verify."""
    r = _js(pid, wid, """
    (function(){
        var input = document.querySelector('input.time-select');
        if (input) {
            input.click();
            input.focus();
            return JSON.stringify({status:'clicked', via:'js'});
        }
        return JSON.stringify({status:'not_found'});
    })()
    """)
    if isinstance(r, dict) and r.get("status") == "clicked":
        print("    时间选择器已打开 (JS)")
        time.sleep(1)
        return _wait_time_expanded(pid, wid)

    # Fallback: AX click
    idx = _find_element_index("选择开始时间", pid, wid)
    if idx:
        print(f"    尝试 AX 点击时间字段 (idx={idx})")
        cua("click", json.dumps({
            "pid": pid, "window_id": wid, "element_index": idx,
        }))
        time.sleep(1)
        return _wait_time_expanded(pid, wid)
    return False


def _wait_time_expanded(pid: int, wid: int, timeout: float = 3.0) -> bool:
    """Wait for time picker <li> items to appear."""
    for _ in range(int(timeout * 2)):
        time.sleep(0.5)
        r = _js(pid, wid, """
        (function(){
            var container = document.querySelector('.time-range-container');
            if (!container) return JSON.stringify({expanded:false});
            var lis = container.querySelectorAll('li');
            return JSON.stringify({expanded:lis.length >= 20, count:lis.length});
        })()
        """)
        if isinstance(r, dict) and r.get("expanded"):
            print(f"    时间选择器已展开 ({r.get('count')} 选项)")
            return True
    return False


def click_time_option(hour: int, minute: int, pid: int, wid: int) -> bool:
    """Click hour and minute in the expanded time picker popup.
    The time picker has two <ul> columns: hours (08-20) and minutes (00-55, step 5).
    We click by LI index: hour index = hour-8, minute index = minute/5 + hour_count."""
    # Hour list: 08,09,10,11,12,13,14,15,16,17,18,19,20 (13 items, index 0-12)
    hour_idx = hour - 8
    # Minute list: 00,05,10,15,20,25,30,35,40,45,50,55 (12 items, index 0-11)
    min_idx = minute // 5

    r = _js(pid, wid, f"""
    (function(){{
        var container = document.querySelector('.time-range-container');
        if (!container) return JSON.stringify({{error:'no time picker'}});
        var lis = container.querySelectorAll('li');
        var HC = 13;  // hour count 08-20

        if (lis.length >= 25) {{
            // Click hour (first column)
            lis[{hour_idx}].click();
            lis[{hour_idx}].dispatchEvent(new MouseEvent('click', {{bubbles:true}}));

            // Click minute (second column, offset by hour count)
            lis[HC + {min_idx}].click();
            lis[HC + {min_idx}].dispatchEvent(new MouseEvent('click', {{bubbles:true}}));

            return JSON.stringify({{status:'clicked',
                hour: lis[{hour_idx}].textContent.trim(),
                min: lis[HC + {min_idx}].textContent.trim()}});
        }}
        return JSON.stringify({{error:'not enough li', count: lis.length}});
    }})()
    """)
    if isinstance(r, dict) and r.get("status") == "clicked":
        return True
    # Fallback: search by text and click
    hour_str = str(hour).zfill(2)
    min_str = str(minute).zfill(2)
    r2 = _js(pid, wid, f"""
    (function(){{
        var container = document.querySelector('.time-range-container');
        if (!container) return JSON.stringify({{error:'no container'}});
        var lis = container.querySelectorAll('li');
        var results = [];
        for (var i = 0; i < lis.length; i++) {{
            results.push(lis[i].textContent.trim());
        }}
        return JSON.stringify({{texts: results}});
    }})()
    """)
    print(f"    时间选择器内容: {r2}")
    return False


def click_send_button(pid: int, wid: int) -> bool:
    """Click the '发送' button to submit the interview invitation."""
    idx = _find_element_index("发送", pid, wid)
    if idx:
        print(f"  点击「发送」按钮 (idx={idx})")
        r = cua("click", json.dumps({
            "pid": pid, "window_id": wid, "element_index": idx,
        }))
        if not r.get("error"):
            return True

    # Fallback: JS click
    r = _js(pid, wid, """
    (function(){
        var btns = document.querySelectorAll('.interview-btns button, .interview-btns span, .interview-btns div');
        for (var i = 0; i < btns.length; i++) {
            if ((btns[i].textContent || '').trim() === '发送') {
                btns[i].click();
                return JSON.stringify({clicked:true});
            }
        }
        return JSON.stringify({clicked:false});
    })()
    """)
    return isinstance(r, dict) and r.get("clicked") is True


# ── Main Orchestration ─────────────────────────────────────────────────────

def schedule_interview(
    pid: int,
    wid: int,
    uid: str,
    name: str,
    interview_type: str,
    date_str: str,   # "YYYY-MM-DD"
    time_str: str,   # "HH:MM"
    dry_run: bool = False,
) -> dict:
    """Complete interview scheduling flow for one candidate.

    Returns:
        {"status": "scheduled"|"skipped"|"error",
         "uid": str, "name": str, "step": str, "error": str|None}
    """

    def fail(step: str, msg: str) -> dict:
        return {"status": "error", "uid": uid, "name": name,
                "step": step, "error": msg}

    # Parse date/time
    try:
        year, month, day = date_str.split("-")
        day_int = int(day)
        hour, minute = time_str.split(":")
        hour_int = int(hour)
        min_int = int(minute)
    except ValueError:
        return fail("parse", f"Invalid date/time: {date_str} {time_str}")

    # ① Navigate to chat
    print("\n① 导航到沟通页...")
    navigate_to_chat(pid, wid)

    # ② Click contact to open conversation
    print(f"\n② 打开与「{name}」的对话...")
    clicked, extracted_uid = click_contact(pid, wid, name)
    if not clicked:
        return fail("click_contact", f"无法点击联系人: {name}")
    actual_uid = extracted_uid or uid

    # Wait for the right conversation panel to load (BOSS SPA needs time)
    print("  等待对话面板加载...")
    panel_ready = False
    for i in range(10):
        time.sleep(1)
        r = _js(pid, wid, """
        (function(){
            var items = document.querySelectorAll('.operate-icon-item');
            if (items.length >= 9) {
                return JSON.stringify({ready:true, count:items.length});
            }
            return JSON.stringify({ready:false, count:items.length});
        })()
        """)
        if isinstance(r, dict) and r.get("ready"):
            panel_ready = True
            print(f"  ✓ 对话面板已加载 (operate-items={r.get('count')})")
            break
    if not panel_ready:
        print("  ⚠ 对话面板加载超时，继续尝试...")
    print(f"  ✓ 已打开对话 (uid={actual_uid})")

    # ③ Click 约面试 button
    print(f"\n③ 点击「约面试」...")
    # Try clicking multiple times with short delays (BOSS React may need it)
    form_opened = False
    for attempt in range(3):
        if attempt > 0:
            print(f"  重试 {attempt + 1}/3...")
            time.sleep(1.5)
        if not click_yuemian_button(pid, wid):
            continue
        time.sleep(1.5)
        if wait_for_interview_form(pid, wid, timeout=3.0):
            form_opened = True
            break

    if not form_opened:
        print("  ⚠ 面试表单未出现")
        print("  提示: BOSS要求双方至少各发一条消息后才能约面试")
        print("  请先在 BOSS 网页上手动发送一条消息给候选人，再运行本脚本")
        # Diagnostic: check button state
        r = _js(pid, wid, """
        (function(){
            var items = document.querySelectorAll('.operate-icon-item');
            if (items.length > 7) {
                var el = items[7];
                return JSON.stringify({
                    text:(el.textContent||'').trim().substring(0,80),
                    opacity:getComputedStyle(el).opacity,
                    className:el.className
                });
            }
            return JSON.stringify({count:items.length});
        })()
        """)
        return fail("form_not_found",
                     f"面试表单未出现。请先确认双方已互发消息。按钮: {r}")

    print("  ✓ 面试邀请表单已打开")

    # ④ Select interview type
    print(f"\n④ 选择面试类型: {interview_type}")
    if not select_interview_type(interview_type, pid, wid):
        return fail("select_type", f"无法选择面试类型: {interview_type}")
    # BOSS re-renders form fields after type switch — wait for DOM to stabilize
    time.sleep(1.5)

    # ⑤ Fill date
    print(f"\n⑤ 选择面试日期: {date_str}")
    if not open_date_picker(pid, wid):
        return fail("open_date", "无法打开日期选择器")
    if not click_calendar_day(day_int, pid, wid):
        return fail("click_day", f"无法点击日期: {day_int}")
    # Verify date was actually set
    r = _js(pid, wid, """
    (function(){
        var input = document.querySelector('input[placeholder*="选择日期"]');
        if (input && input.value && input.value.indexOf('选择') === -1) {
            return JSON.stringify({set:true, value:input.value});
        }
        // Check if datepicker shows the selected date
        var dp = document.querySelector('.datepicker-wrap input');
        if (dp && dp.value && dp.value.indexOf('选择') === -1) {
            return JSON.stringify({set:true, value:dp.value});
        }
        return JSON.stringify({set:false});
    })()
    """)
    if not (isinstance(r, dict) and r.get("set")):
        print("  ⚠ 日期可能未设置，继续...")
    else:
        print(f"  ✓ 已选择日期: {r.get('value', date_str)}")

    # ⑥ Fill time
    print(f"\n⑥ 选择面试时间: {time_str}")
    if not open_time_picker(pid, wid):
        return fail("open_time", "无法打开时间选择器")
    time.sleep(0.5)
    if not click_time_option(hour_int, min_int, pid, wid):
        return fail("click_time", f"无法选择时间: {time_str}")
    print(f"  ✓ 已选择时间")
    time.sleep(0.3)

    # ⑦ Submit (skip in dry-run)
    if dry_run:
        print(f"\n  🔍 [DRY-RUN] 表单已完整填写（未发送）")
        print(f"     类型: {interview_type} | 日期: {date_str} | 时间: {time_str}")
        return {"status": "dry_run", "uid": uid, "name": name,
                "step": "form_filled", "error": None}

    print(f"\n⑦ 发送面试邀请...")
    if not click_send_button(pid, wid):
        return fail("submit", "无法点击发送按钮")
    time.sleep(2)

    # Verify submission
    r = _js(pid, wid, """
    (function(){
        var dlg = document.querySelector('.interview-invite-ui');
        if (!dlg || getComputedStyle(dlg).display === 'none') {
            return JSON.stringify({submitted:true, reason:'form_closed'});
        }
        // Check for success message
        var body = document.body.textContent || '';
        if (body.indexOf('已发送') !== -1 || body.indexOf('邀请已发送') !== -1) {
            return JSON.stringify({submitted:true, reason:'success_msg'});
        }
        return JSON.stringify({submitted:false});
    })()
    """)
    if isinstance(r, dict) and r.get("submitted"):
        print("  ✓ 面试邀请已发送")
        return {"status": "scheduled", "uid": actual_uid, "name": name,
                "step": "done", "error": None}
    else:
        print("  ⚠ 无法确认是否发送成功（表单可能仍在）")
        return {"status": "scheduled", "uid": actual_uid, "name": name,
                "step": "done", "error": "verify_failed"}


# ── CLI ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BOSS直聘 — 约面试自动化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --uid 12345678 --date 2026-06-20 --time 14:30
  %(prog)s --uid 12345678 --type 线上 --date 2026-06-20 --time 10:00
  %(prog)s --name 张三 --date 2026-06-20 --time 14:30 --no-db
  %(prog)s --uid 12345678 --date 2026-06-20 --time 14:30 --dry-run
        """,
    )
    parser.add_argument("--uid", type=str, required=True,
                        help="候选人 UID（从 candidates.db 查询或 BOSS data-id）")
    parser.add_argument("--name", type=str, default=None,
                        help="候选人姓名（fallback：不查 DB 时必需）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式：打开表单但不填写不发送")
    parser.add_argument("--type", type=str, default="线上",
                        choices=["线上", "线下"],
                        help="面试类型（默认: 线上）")
    parser.add_argument("--date", type=str, required=True,
                        help="面试日期 YYYY-MM-DD（如 2026-06-20）")
    parser.add_argument("--time", type=str, required=True,
                        help="面试开始时间 HH:MM（如 14:30）")
    parser.add_argument("--no-db", action="store_true",
                        help="跳过数据库查询（仅使用 --name）")
    parser.add_argument("--debug-ax", action="store_true",
                        help="每步后 dump AX 树用于调试")
    args = parser.parse_args()

    # Validate formats
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.date):
        print("❌ --date 格式必须为 YYYY-MM-DD")
        sys.exit(1)
    if not re.match(r'^\d{2}:\d{2}$', args.time):
        print("❌ --time 格式必须为 HH:MM")
        sys.exit(1)

    # Lookup candidate from DB
    name = args.name
    uid = args.uid
    if not args.no_db:
        conn = init_db()
        row = conn.execute(
            "SELECT name, job_position, school, degree FROM candidates WHERE uid = ?",
            (uid,)
        ).fetchone()
        if row:
            name = row[0]
            print(f"  DB: {name} | {row[1]} | {row[2]} | {row[3]}")
        else:
            print(f"  ⚠ UID {uid} 未在数据库中找到")
            if not name:
                print("❌ 请提供 --name 或使用 --no-db")
                sys.exit(1)
        conn.close()

    if not name:
        print("❌ 无法确定候选人姓名")
        sys.exit(1)

    # Header
    print("=" * 60)
    print(f"BOSS约面试 | UID={uid} | {name}")
    print(f"类型: {args.type} | 日期: {args.date} | 时间: {args.time}")
    print(f"模式: {'🔍 dry-run(预览不发送)' if args.dry_run else '✅ 执行'}")
    print("=" * 60)

    # Setup
    print("\n🔧 启动会话...")
    start_session()
    chrome = find_boss_window()
    pid, wid = chrome["pid"], chrome["window_id"]

    # Execute
    result = schedule_interview(
        pid=pid, wid=wid,
        uid=uid, name=name,
        interview_type=args.type,
        date_str=args.date, time_str=args.time,
        dry_run=args.dry_run,
    )

    # Result
    emoji = {"scheduled": "✅", "skipped": "⏭️", "error": "❌", "dry_run": "🔍"}
    print(f"\n{'=' * 60}")
    print(f"{emoji.get(result['status'], '❓')} {result['status']}")
    if result.get("error"):
        print(f"  错误: {result['error']}")
    if result.get("step"):
        print(f"  步骤: {result['step']}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
