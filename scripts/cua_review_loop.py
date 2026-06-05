#!/usr/bin/env python3
"""
cua-driver 驱动的 BOSS直聘候选人审查 + 批量回复
=================================================

逐个查看未读联系人，判断：
  1. 上一条消息是否候选人的（未回复）—— 还是我们已经回复过
  2. 学校是否在名校白名单中 — 不符合直接点"不合适"
  3. 符合条件 → 匹配话术模板 → 输入回复

用法:
  python scripts/cua_review_loop.py                # 审查+回复(最多20人)
  python scripts/cua_review_loop.py --dry-run      # 仅预览，输入不发送
  python scripts/cua_review_loop.py --limit 10     # 最多10人
  python scripts/cua_review_loop.py --schools "清华,北大,浙大"  # 自定义学校
  python scripts/cua_review_loop.py --min-degree 硕士
"""
import argparse
import json
import random
import subprocess
import sys
import time
import re
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, match_school
from app.chat_reply import load_jobs_config, generate_reply, check_degree, detect_job

SESSION_ID = "boss-review"
CHROME_BUNDLE_ID = "com.google.Chrome"
CHAT_URL = "https://www.zhipin.com/web/chat/index"

# ── 自己发出的消息特征（用于判断"已回复"） ──
SELF_MESSAGE_PATTERNS = [
    "您好，我们是一支初创",
    "你好！看到你的简历",
    "收到，我稍后看一下",
    "收到，我看一下",
    "薪资根据能力",
    "面试一般",
    "岗位在北京",
    "实习至少",
    "主要是 Python",
    "你好，我是招聘",
]

# ── 限制检测关键词 ──
LIMIT_KEYWORDS = [
    "已达上限", "次数已用完", "今日已达", "已达每日",
    "沟通人数已达", "打招呼次数", "超出限制",
    "明天再来", "今日上限", "已达当天",
    "每天最多", "上限了", "用完了", "今日沟通",
    "权益不足", "开料次数", "剩余次数", "次数不足",
    "会员权益", "升级会员", "额度不足", "免费次数",
    "今日剩余",
]


def cua(*args: str) -> dict:
    cmd = ["cua-driver"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {}
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"text": stdout[:200]}


# ══════════════════════════════════════════════════
# 页面加载
# ══════════════════════════════════════════════════

def wait_for_page(pid: int, window_id: int, timeout: float = 15.0, label: str = "") -> bool:
    prefix = f"[{label}] " if label else ""
    delays = [0.5, 0.5, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 3.0, 3.0]
    elapsed = 0.0

    for delay in delays:
        time.sleep(delay)
        elapsed += delay

        r = cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": "document.readyState",
        }))
        ready_val = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        if ready_val.strip().strip('"') != "complete":
            continue

        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        elem_count = snap.get("element_count", 0)

        if elem_count > 300:
            print(f"  {prefix}✓ 就绪 ({elem_count}元素, {elapsed:.1f}s)")
            return True
        if elem_count > 100:
            print(f"  {prefix}加载中... ({elem_count}元素, {elapsed:.1f}s)")

    print(f"  {prefix}⚠ 超时 ({elapsed:.1f}s)")
    return False


# ══════════════════════════════════════════════════
# 限制弹窗
# ══════════════════════════════════════════════════

def dismiss_limit_popup(pid: int, window_id: int):
    print(f"  关闭弹窗...", end=" ", flush=True)
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var removed = 0;
            document.querySelectorAll(
                '.dialog-wrap, [class*=overlay], [class*=mask], [class*=backdrop], ' +
                '.boss-popup__wrapper, [class*=modal]'
            ).forEach(function(el){
                var s = getComputedStyle(el);
                if ((s.position === 'fixed' || s.zIndex > 100) && s.display !== 'none') {
                    el.remove(); removed++;
                }
            });
            return 'removed ' + removed;
        })()
        """,
    }))
    time.sleep(0.3)
    cua("press_key", json.dumps({"pid": pid, "window_id": window_id, "key": "escape"}))
    time.sleep(0.3)
    print("✓")


def check_limit_popup(pid: int, window_id: int) -> Optional[str]:
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.4)

        r = cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": """
            (function(){
                var texts = [];
                document.querySelectorAll(
                    '[class*=toast], [class*=popup], [class*=modal], [class*=dialog], ' +
                    '[class*=notice], [class*=tip], [class*=message], [class*=snackbar], ' +
                    '[class*=alert], [class*=confirm]'
                ).forEach(function(el){
                    var style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return;
                    var rect = el.getBoundingClientRect();
                    if (rect.width < 50 || rect.height < 10) return;
                    var t = (el.textContent || '').trim();
                    if (t.length > 2 && t.length < 200) texts.push(t);
                });
                return JSON.stringify(texts);
            })()
            """,
        }))
        popup_text = " ".join(str(x) for x in r) if isinstance(r, list) else str(r.get("result", r.get("text", "")))
        for kw in LIMIT_KEYWORDS:
            if kw in popup_text:
                return f"弹窗: {kw}"

        snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
        tree = snap.get("tree_markdown", "")
        for line in tree.split("\n"):
            for kw in LIMIT_KEYWORDS:
                if kw in line and ("StaticText" in line or "AXButton" in line):
                    return f"页面: {kw}"

    return None


# ══════════════════════════════════════════════════
# 会话 & 窗口
# ══════════════════════════════════════════════════

def start_session():
    print("1. 启动 cua-driver 会话...")
    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True)
        time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION_ID}))
    print("   ✓")


def find_boss_window() -> dict:
    print("2. 查找 BOSS直聘窗口...")
    apps = cua("list_apps")
    chrome_pid = None
    for app in apps.get("apps", []):
        if app.get("bundle_id") == CHROME_BUNDLE_ID and app.get("running"):
            chrome_pid = app.get("pid")
            break
    if not chrome_pid:
        print("  ❌ Chrome 未运行")
        sys.exit(1)

    lw = cua("list_windows", json.dumps({"pid": chrome_pid}))

    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("zhipin" in title or "BOSS直聘" == title.strip()) and w.get("is_on_screen"):
            print(f"  ✓ pid={chrome_pid} wid={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    for w in lw.get("windows", []):
        title = w.get("title", "")
        if ("BOSS" in title or "zhipin" in title) and w.get("is_on_screen"):
            print(f"  ✓ pid={chrome_pid} wid={w['window_id']} '{title[:60]}'")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    # 离屏兜底
    for w in lw.get("windows", []):
        if "BOSS" in w.get("title", "") or "zhipin" in w.get("title", ""):
            print(f"  ⚠ 窗口隐藏, 尝试操作离屏窗口 wid={w['window_id']}")
            return {"pid": chrome_pid, "window_id": w["window_id"]}

    print("  ❌ 找不到 BOSS直聘窗口")
    sys.exit(1)


def navigate_to_chat(pid: int, window_id: int):
    print("3. 进入聊天页面...")
    cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f'window.location.href = "{CHAT_URL}"',
    }))
    print("  等待页面加载...")
    time.sleep(5)
    wait_for_page(pid, window_id, label="聊天页")

    # 确认在联系人列表视图
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if "未读" not in tree:
        print("  切换到联系人列表...")
        for line in tree.split("\n"):
            if "沟通" in line and "AXLink" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    cua("click", json.dumps({"pid": pid, "window_id": window_id, "element_index": int(m.group(1))}))
                    time.sleep(3)
                    break
        print("  ✓")


# ══════════════════════════════════════════════════
# 扫描未读联系人
# ══════════════════════════════════════════════════

def scan_unread_contacts(pid: int, window_id: int) -> list[dict]:
    """扫描左侧未读联系人列表

    BOSS 聊天页 AX 树结构 (联系人列表区域):
      [73] "未读"          ← 未读区起始标记
      [74] "批量"          ← 跳过
      [75] "1"             ← 未读条数（触发保存前一个联系人）
      [76] "15:39"         ← 时间
      [77] "严彭杰"         ← 姓名
      [78] "AI 技术总监"    ← 职位
      [79] "消息内容..."    ← 消息预览

    注意: 未读数字出现在联系人信息之前，所以触发保存的是"上一个"联系人。
    需要在遍历完后额外保存最后一个。
    """
    print("4. 扫描未读联系人...")

    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")
    if not tree:
        print("  ⚠ AX 树为空")
        return []

    lines = tree.split("\n")
    contacts = []
    current_name = None
    current_job = None
    current_msg = None
    current_time = None
    in_contact_list = False

    for line in lines:
        s = line.strip()
        val_m = re.search(r'AXStaticText\s*=\s*"(.+?)"', s)
        idx_m = re.search(r'\[(\d+)\]', s)
        if not val_m or not idx_m:
            continue
        val = val_m.group(1)
        idx = int(idx_m.group(1))

        # 进入未读区域
        if val == "未读":
            in_contact_list = True
            continue

        # 跳过无关标记
        if val in ("批量", "买赠", "帮你问牛人", "不符牛人"):
            continue

        if not in_contact_list:
            continue

        # 筛选标签 — 离开未读区域或不是人名
        if val in ("全部", "新招呼", "沟通中", "已约面", "已获取简历",
                    "已交换电话", "已交换微信", "收藏", "更多", "全部职位",
                    "意向沟通", "高学历", "大厂", "牛人"):
            in_contact_list = False
            continue
        if re.match(r'^\(\d+\)$', val):
            continue

        # 未读数字 — 保存上一个联系人
        if re.match(r'^\d{1,2}$', val) and int(val) > 0:
            if current_name:
                contacts.append({
                    "name": current_name,
                    "job": current_job or "",
                    "message": current_msg or "",
                    "time": current_time or "",
                    "unread": int(val),
                    "ax_index": idx,
                })
            current_name = None
            current_job = None
            current_msg = None
            current_time = None
            continue

        # 时间
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):
            current_time = val
            continue

        # 状态标记
        if re.match(r'^\[.+\]$', val):
            continue

        # 姓名 (2-4个中文字)
        if not current_name and re.match(r'^[一-鿿]{2,4}$', val):
            current_name = val
            continue

        # 职位 (名字后的短文本)
        if current_name and not current_job and len(val) <= 20:
            current_job = val
            continue

        # 消息内容 (长文本)
        if current_name and not current_msg and len(val) > 5:
            current_msg = val[:80]
            continue

    # 保存最后一个（没有被未读数字触发）
    if current_name:
        contacts.append({
            "name": current_name,
            "job": current_job or "",
            "message": current_msg or "",
            "time": current_time or "",
            "unread": 1,
            "ax_index": -1,
        })

    # 去重（同一个名字+时间）
    seen = set()
    unique_contacts = []
    for c in contacts:
        key = (c["name"], c["time"])
        if key not in seen:
            seen.add(key)
            unique_contacts.append(c)

    print(f"  找到 {len(unique_contacts)} 个未读联系人 (去重后)")
    for c in unique_contacts[:5]:
        print(f"    {c['name']:8s} | {c['job']:14s} | 未读{c['unread']}条 | {c['time']} | {c['message'][:30]}")
    if len(unique_contacts) > 5:
        print(f"    ... 还有 {len(unique_contacts) - 5} 个")
    return unique_contacts


# ══════════════════════════════════════════════════
# 点击联系人
# ══════════════════════════════════════════════════

def click_contact(pid: int, window_id: int, name: str) -> bool:
    """通过 JS 点击联系人名字（<span class="geek-name">）"""
    # 转义单引号
    safe_name = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent || '').trim() === '{safe_name}' &&
                    el.children.length <= 1 && el.offsetWidth > 0) {{
                    for (var lvl = 0; lvl < 8; lvl++) {{
                        if (el.onclick || getComputedStyle(el).cursor === 'pointer') {{
                            el.click();
                            return 'clicked';
                        }}
                        el = el.parentElement;
                        if (!el) break;
                    }}
                    return 'not_clickable';
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    result_text = ""
    if isinstance(r, list):
        result_text = " ".join(str(x) for x in r)
    else:
        result_text = str(r.get("result", r.get("text", "")))
    return "clicked" in result_text


# ══════════════════════════════════════════════════
# 读取对话 — 学校 / 学历 / 最新消息 / 是否未回复
# ══════════════════════════════════════════════════

def read_conversation(pid: int, window_id: int) -> dict:
    """读取右侧对话面板：学校、学历、最新候选人消息、是否已回复

    返回:
      {
        "name": str,
        "school": str | None,
        "degree": str | None,
        "info_line": str,          # 如 "南京理工大学 · 硕士 · 技术总监"
        "latest_candidate_msg": str,
        "latest_is_candidate": bool,  # True = 上一句是候选人发的，需要回复
        "already_replied": bool,      # True = 上一句是我们发的，已回复过
      }
    """
    result = {
        "name": "",
        "school": None,
        "degree": None,
        "info_line": "",
        "latest_candidate_msg": "",
        "latest_is_candidate": False,
        "already_replied": False,
    }

    # ── 1. AX 树读取候选人信息 ──
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id, "capture_mode": "ax"}))
    tree = snap.get("tree_markdown", "")

    if tree:
        for line in tree.split("\n"):
            s = line.strip()
            # 学校 (独立出现)
            m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', s)
            if m and not result["school"]:
                result["school"] = m.group(1)
            # 学历 (独立出现)
            m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', s)
            if m and not result["degree"]:
                result["degree"] = m.group(1)
            # 候选人信息行: "郑州大学 · 经济统计学 · 本科" 或 "某 · 技术总监"
            m = re.search(r'AXStaticText\s*=\s*"(.+)"', s)
            if m and "·" in m.group(1) and len(m.group(1)) < 80:
                info = m.group(1)
                # 解析 · 分隔的字段
                parts = [p.strip() for p in info.split("·")]
                # 尝试从各部分中提取学校和学历
                for p in parts:
                    # 学校
                    school_m = re.match(r'^([一-龥]{2,8}(?:大学|学院|学校))$', p)
                    if school_m and not result["school"]:
                        result["school"] = school_m.group(1)
                    # 学历
                    if p in ("博士", "硕士", "本科", "大专") and not result["degree"]:
                        result["degree"] = p
                if not result["info_line"]:
                    result["info_line"] = info

    # ── 2. AX 树 + JS 双通道读取消息 ──
    # AX 树通常更可靠地反映页面文本，用作主通道；JS 作为辅助判断发送者

    # 2a. 从 AX 树收集右侧面板的长文本
    ax_texts = []
    if tree:
        for line in tree.split("\n"):
            m = re.search(r'AXStaticText\s*=\s*"(.+)"', line)
            if m:
                val = m.group(1)
                # 过滤掉导航/UI标签
                if val in ("沟通", "全部", "新招呼", "沟通中", "已约面", "未读", "批量",
                           "已获取简历", "已交换电话", "已交换微信", "收藏", "更多",
                           "全部职位", "买赠", "帮你问牛人", "不符牛人", "意向沟通",
                           "招聘数据", "账号权益", "面试", "道具", "工具箱", "牛人管理",
                           "互动", "搜索", "推荐牛人", "职位管理", "直聘企业版", "招聘规范",
                           "我的客服", "BOSS直聘"):
                    continue
                if re.match(r'^\d+$', val):  # 纯数字
                    continue
                if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):  # 时间
                    continue
                if re.match(r'^\[.+\]$', val):  # 状态标记 [送达] [已读]
                    continue
                if len(val) > 10 and len(val) < 300:
                    ax_texts.append(val)

    # 2b. JS 判断发送者（靠右=自己发的，靠左=候选人发的）
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            // 在 chat-container 中找到所有消息气泡
            var container = document.querySelector('.chat-container, [class*="chat-con"], [class*="conversation"]');
            if (!container) {
                // 找右侧最大的 div
                var divs = document.querySelectorAll('div');
                var best = null, bestArea = 0;
                for (var i = 0; i < divs.length; i++) {
                    var r = divs[i].getBoundingClientRect();
                    if (r.x > 350 && r.width > 300) {
                        var area = r.width * r.height;
                        if (area > bestArea && divs[i].children.length > 1) {
                            best = divs[i]; bestArea = area;
                        }
                    }
                }
                container = best;
            }

            if (!container) return JSON.stringify({msgs: [], note: 'no_container'});

            // BOSS消息通常在 .chat-message-item 或类似结构中
            // 自己的消息靠右(class含self/right/mine)，候选人的靠左
            var items = container.querySelectorAll(
                '[class*="message"], [class*="bubble"], [class*="msg"], [class*="chat-item"], ' +
                'li'
            );

            var msgs = [];
            for (var i = 0; i < items.length; i++) {
                var el = items[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 60 || rect.height < 8) continue;
                var text = (el.textContent || '').trim();
                if (text.length < 5 || text.length > 400) continue;

                var cls = (el.className || '') + ' ' + (el.parentElement ? el.parentElement.className || '' : '');
                var isSelf = cls.indexOf('self') >= 0 || cls.indexOf('right') >= 0 ||
                             cls.indexOf('mine') >= 0 || cls.indexOf('sender') >= 0;

                // 如果class不明确，通过水平位置判断 (x > 800 通常是自己的消息)
                if (!isSelf && rect.x > 800) isSelf = true;

                msgs.push({
                    role: isSelf ? 'assistant' : 'user',
                    content: text
                });
            }

            return JSON.stringify({msgs: msgs.slice(-10)});
        })()
        """,
    }))

    js_msgs = []
    try:
        msg_text = ""
        if isinstance(r, list):
            msg_text = " ".join(str(x) for x in r)
        else:
            msg_text = str(r.get("result", r.get("text", "")))
        parsed = json.loads(msg_text.strip().strip('"'))
        js_msgs = parsed.get("msgs", [])
    except (json.JSONDecodeError, TypeError):
        pass

    # 2c. 合并判断: AX 文本 + JS 发送者信息
    # 策略: 从 AX 文本中取最后几条，对照 JS 判断发送者
    candidate_msgs = []
    for text in ax_texts[-8:]:
        # 跳过明显是我们发出的消息
        is_self = False
        for pattern in SELF_MESSAGE_PATTERNS:
            if pattern in text:
                is_self = True
                break
        if not is_self:
            candidate_msgs.append(text)

    if candidate_msgs:
        result["latest_candidate_msg"] = candidate_msgs[-1]
        result["latest_is_candidate"] = True
        result["already_replied"] = False
    elif ax_texts:
        # 所有文本都是我们发的 → 已回复
        result["latest_is_candidate"] = False
        result["already_replied"] = True

    # 2d. JS 结果修正: 如果 JS 明确找到了候选人消息，优先使用
    for msg in reversed(js_msgs):
        if msg.get("role") == "user":
            result["latest_candidate_msg"] = msg["content"]
            result["latest_is_candidate"] = True
            result["already_replied"] = False
            break

    return result


# ══════════════════════════════════════════════════
# 点击"不合适"
# ══════════════════════════════════════════════════

def click_unsuitable(pid: int, window_id: int) -> bool:
    """点击对话右上角的"不合适"按钮

    BOSS直聘聊天页 "不合适" 在右侧对话面板顶部工具栏。
    AX 树中可能是 AXStaticText 或 AXButton，都尝试点击。
    先 AX 树 → JS DOM 查找兜底。
    """
    # 1. AX 树查找 (AXStaticText 或 AXButton)
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    if tree:
        for line in tree.split("\n"):
            if "不合适" in line:
                if "已标记" in line:
                    print("    (已标记不合适)")
                    return True
                # 匹配 AXButton 或 AXStaticText
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    idx = int(m.group(1))
                    # 先尝试 click
                    r = cua("click", json.dumps({"pid": pid, "window_id": window_id, "element_index": idx}))
                    if not r.get("error"):
                        print("    ✓ 已点击'不合适' (AX)")
                        return True
                    # AXStaticText 可能不支持 press → 用 JS 点击
                    r2 = cua("page", json.dumps({
                        "pid": pid, "window_id": window_id,
                        "action": "execute_javascript",
                        "javascript": f"""
                        (function(){{
                            var all = document.querySelectorAll('*');
                            for (var i = 0; i < all.length; i++) {{
                                if ((all[i].textContent || '').trim() === '不合适') {{
                                    all[i].click();
                                    return 'clicked_ax_text';
                                }}
                            }}
                            return 'not_found';
                        }})()
                        """,
                    }))
                    result_text = " ".join(str(x) for x in r2) if isinstance(r2, list) else str(r2.get("result", r2.get("text", "")))
                    if "clicked" in result_text:
                        print("    ✓ 已点击'不合适' (JS via AX)")
                        return True

    # 2. JS DOM 查找 - 遍历所有元素，找文字是"不合适"的可点击元素
    r = cua("page", json.dumps({
        "pid": pid, "window_id": window_id,
        "action": "execute_javascript",
        "javascript": """
        (function(){
            var all = document.querySelectorAll('button, [role="button"], [class*="btn"], [class*="button"], span, div');
            for (var i = 0; i < all.length; i++) {
                var t = (all[i].textContent || '').trim();
                if (t === '不合适') {
                    // 向上查找可点击的父元素
                    var target = all[i];
                    for (var lvl = 0; lvl < 6; lvl++) {
                        if (target.onclick || getComputedStyle(target).cursor === 'pointer' ||
                            target.tagName === 'BUTTON' || target.tagName === 'A') {
                            target.click();
                            return 'clicked lvl=' + lvl + ' tag=' + target.tagName;
                        }
                        target = target.parentElement;
                        if (!target) break;
                    }
                    all[i].click();
                    return 'clicked_self';
                }
            }
            return 'not_found';
        })()
        """,
    }))
    result_text = ""
    if isinstance(r, list):
        result_text = " ".join(str(x) for x in r)
    else:
        result_text = str(r.get("result", r.get("text", "")))

    if "clicked" in result_text:
        print(f"    ✓ 已点击'不合适' (JS: {result_text[:50]})")
        return True

    print("    ⚠ 未找到'不合适'按钮")
    return False


# ══════════════════════════════════════════════════
# 输入回复
# ══════════════════════════════════════════════════

def find_input_index(pid: int, window_id: int) -> Optional[int]:
    """在 AX 树中查找聊天输入框的 element_index"""
    snap = cua("get_window_state", json.dumps({"pid": pid, "window_id": window_id}))
    tree = snap.get("tree_markdown", "")
    if tree:
        for line in tree.split("\n"):
            if ("AXTextArea" in line or "textArea" in line) and "[" in line:
                m = re.search(r'\[(\d+)\]', line)
                if m:
                    return int(m.group(1))
    return None


def type_reply(pid: int, window_id: int, text: str, dry_run: bool = True) -> bool:
    """输入回复；dry_run=True 只输入不发送"""
    input_idx = find_input_index(pid, window_id)

    if input_idx is None:
        # JS 聚焦输入框兜底
        cua("page", json.dumps({
            "pid": pid, "window_id": window_id,
            "action": "execute_javascript",
            "javascript": """
            (function(){
                var el = document.querySelector(
                    'textarea, [contenteditable="true"], [class*="chat-input"], [class*="input-box"]'
                );
                if (el) { el.focus(); return 'focused'; }
                return 'not_found';
            })()
            """,
        }))
        time.sleep(0.3)

    if input_idx:
        r = cua("type_text", json.dumps({
            "pid": pid, "window_id": window_id,
            "element_index": input_idx, "text": text,
        }))
    else:
        r = cua("type_text", json.dumps({
            "pid": pid, "window_id": window_id,
            "text": text,
        }))

    if r.get("error"):
        print(f"    ❌ 输入失败: {r['error']}")
        return False

    if dry_run:
        return True

    # 发送
    time.sleep(0.3)
    if input_idx:
        cua("press_key", json.dumps({
            "pid": pid, "window_id": window_id,
            "element_index": input_idx, "key": "return",
        }))
    else:
        cua("press_key", json.dumps({
            "pid": pid, "window_id": window_id,
            "key": "return",
        }))
    time.sleep(0.5)
    return True


# ══════════════════════════════════════════════════
# 单个候选人审查
# ══════════════════════════════════════════════════

def review_one_candidate(
    pid: int,
    window_id: int,
    contact: dict,
    school_whitelist: list[str],
    jobs_config: dict,
    min_degree: str,
    dry_run: bool,
) -> dict:
    """审查一个候选人：读对话 → 学校筛选 → 未回复判断 → 回复/不合适

    决策流程:
      1. 点击联系人，打开对话
      2. 读取学校、学历、最新消息
      3. 判断上一句是否候选人发的（未回复）→ 已经是我们的消息 → 跳过
      4. 学校不在白名单 → 点"不合适"
      5. 学校在白名单 → 匹配模板 → 输入回复
    """
    name = contact.get("name", "?")
    print(f"\n  ── {name} ──")

    # a. 点击
    if not click_contact(pid, window_id, name):
        print(f"    ❌ 点击失败")
        return {"status": "click_error", "name": name}
    time.sleep(1.5)

    # b. 读取
    convo = read_conversation(pid, window_id)
    school = convo.get("school")
    degree = convo.get("degree")
    info_line = convo.get("info_line", "")
    latest_msg = convo.get("latest_candidate_msg", "")
    already_replied = convo.get("already_replied", False)
    latest_is_candidate = convo.get("latest_is_candidate", False)

    # 输出审查信息
    school_flag = "✅" if school and match_school(school, school_whitelist) else "❌"
    reply_flag = "🟢待回复" if latest_is_candidate else "🔵已回复"
    print(f"    信息: {info_line or '?'}")
    print(f"    学校: {school or '?'} {school_flag} | 学历: {degree or '?'} | {reply_flag}")
    if latest_msg:
        print(f"    最新: {latest_msg[:60]}")

    # c. 判断是否已回复
    if already_replied:
        print(f"    → 已回复过（上一句是我们发的），跳过")
        return {"status": "already_replied", "name": name}

    # d. 学校筛选
    school_ok = school and match_school(school, school_whitelist)

    if not school_ok:
        school_str = school or "未知"
        print(f"    → 学校不符 ({school_str} 不在白名单)，标记'不合适'")
        if not dry_run:
            click_unsuitable(pid, window_id)
        else:
            print(f"    [预览] 将点击'不合适'")
        return {"status": "unsuitable", "name": name, "school": school_str}

    # e. 学历筛选
    if degree and not check_degree(degree, min_degree):
        print(f"    → 学历不符 ({degree} < {min_degree})，标记'不合适'")
        if not dry_run:
            click_unsuitable(pid, window_id)
        else:
            print(f"    [预览] 将点击'不合适'")
        return {"status": "rejected_degree", "name": name, "degree": degree}

    # f. 无候选人消息
    if not latest_msg:
        print(f"    → 无候选人消息，跳过")
        return {"status": "no_message", "name": name}

    # g. 生成回复 (岗位感知)
    jobs = jobs_config.get("jobs", [])
    fallback_tpls = jobs_config.get("fallback_templates", [])

    # 检测候选人对应的岗位
    job_id = detect_job(latest_msg, contact.get("job", ""), jobs)
    job_templates = []
    job_context = ""
    if job_id:
        for job in jobs:
            if job["id"] == job_id:
                job_templates = job.get("templates", [])
                job_context = f"{job['title']} | {job['requirements']} | {job['salary']}"
                print(f"    岗位: {job['title']}")
                break

    reply = generate_reply(
        latest_msg,
        templates=[],  # 旧格式兼容（jobs模式下为空）
        candidate_name=name,
        job_templates=job_templates,
        fallback_templates=fallback_tpls,
        job_context=job_context,
    )
    print(f"    → 回复: {reply[:60]}")

    # h. 输入（dry-run 不发送）
    ok = type_reply(pid, window_id, reply, dry_run=dry_run)
    if not ok:
        return {"status": "send_error", "name": name}

    if dry_run:
        print(f"    [预览] 已输入，未发送")
    else:
        print(f"    ✓ 已发送")

    return {"status": "replied", "name": name, "school": school, "reply": reply}


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="cua-driver 驱动 BOSS直聘候选人审查 + 批量回复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                          审查+回复(最多20人)
  %(prog)s --dry-run                仅预览，输入不发送
  %(prog)s --limit 10               最多10人
  %(prog)s --schools "清华,北大,浙大"  自定义学校
  %(prog)s --min-degree 硕士         最低硕士
        """,
    )
    parser.add_argument("--limit", type=int, default=20, help="最多处理N个未读 (默认20)")
    parser.add_argument("--dry-run", action="store_true", help="仅预览: 输入回复但不发送，不点不合适")
    parser.add_argument("--schools", type=str, default=None, help="学校白名单，逗号分隔 (默认ALL_ELITE_SCHOOLS)")
    parser.add_argument("--min-degree", type=str, default="本科", help="最低学历要求 (默认本科)")
    parser.add_argument("--config", type=str, default=None, help="话术模板文件路径")
    parser.add_argument("--no-scroll", action="store_true", help="不滚动页面加载更多")
    parser.add_argument("--scroll-pages", type=int, default=3, help="滚动多少页来加载更多联系人 (默认3)")
    args = parser.parse_args()

    # 学校白名单
    school_whitelist = (
        [s.strip() for s in args.schools.split(",")]
        if args.schools else ALL_ELITE_SCHOOLS
    )

    jobs_config = load_jobs_config(args.config)

    job_count = len(jobs_config.get("jobs", []))
    tpl_count = sum(len(j.get("templates", [])) for j in jobs_config.get("jobs", []))
    tpl_count += len(jobs_config.get("fallback_templates", []))

    print("=" * 60)
    print(f"BOSS候选人审查 | {len(school_whitelist)}所学校 | "
          f"最低{args.min_degree} | 上限{args.limit}人")
    print(f"模式: {'预览(dry-run)' if args.dry_run else '执行'} | "
          f"{job_count}个岗位 | {tpl_count}条话术")
    print("=" * 60)

    # ── 初始化 ──
    start_session()
    chrome = find_boss_window()
    pid, wid = chrome["pid"], chrome["window_id"]

    navigate_to_chat(pid, wid)

    # ── 滚动加载更多 ──
    if not args.no_scroll:
        for page in range(args.scroll_pages):
            print(f"  滚动加载 ({page + 1}/{args.scroll_pages})...")
            cua("scroll", json.dumps({"pid": pid, "window_id": wid, "direction": "down", "amount": 8}))
            time.sleep(1.5)
        print()

    # ── 扫描 ──
    contacts = scan_unread_contacts(pid, wid)
    if not contacts:
        print("\n✅ 没有未读消息")
        return

    contacts = contacts[:args.limit]
    print(f"\n5. 逐个审查 ({len(contacts)} 人)...")

    # ── 统计 ──
    stats = {
        "replied": 0,
        "unsuitable": 0,
        "rejected_degree": 0,
        "already_replied": 0,
        "no_message": 0,
        "send_error": 0,
        "click_error": 0,
    }

    for i, contact in enumerate(contacts):
        print(f"\n  [{i + 1}/{len(contacts)}] {contact.get('name', '?')} "
              f"| {contact.get('job', '?')} | {contact.get('time', '?')}")

        # 检测上限
        limit = check_limit_popup(pid, wid)
        if limit:
            print(f"  🛑 {limit}")
            dismiss_limit_popup(pid, wid)
            break

        result = review_one_candidate(
            pid, wid, contact, school_whitelist, jobs_config, args.min_degree, args.dry_run
        )
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        # 随机间隔
        if i < len(contacts) - 1:
            delay = random.uniform(1.5, 3)
            time.sleep(delay)

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    print(f"审查完成:")
    print(f"  ✅ 已回复:     {stats.get('replied', 0)}")
    print(f"  🚫 不合适:     {stats.get('unsuitable', 0)}")
    print(f"  📉 学历不符:   {stats.get('rejected_degree', 0)}")
    print(f"  🔵 已回复过:   {stats.get('already_replied', 0)}")
    print(f"  ⚠ 无消息:     {stats.get('no_message', 0)}")
    print(f"  ❌ 发送失败:   {stats.get('send_error', 0)}")
    total = sum(stats.values())
    print(f"  ── 总计: {total}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
