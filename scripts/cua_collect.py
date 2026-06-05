#!/usr/bin/env python3
"""
沟通页批量获取简历 & 微信 — 收集候选人信息到 SQLite

流程:
  ① 进入聊天页 → 滚动加载
  ② AX树扫描所有联系人（不限未读）
  ③ 逐个审查:
      学校不在白名单 / 学历不达标 → 点"不合适"
      符合条件 → 获取简历(求简历/预览附件) + 获取微信(换微信/记录)
  ④ 所有数据存入 candidates.db (SQLite)

用法:
  python scripts/cua_collect.py                  # 全部联系人
  python scripts/cua_collect.py --limit 10        # 前10个
  python scripts/cua_collect.py --dry-run          # 预览
  python scripts/cua_collect.py --min-degree 硕士  # 学历筛选
"""
import json
import sqlite3
import subprocess
import sys
import time
import re
import random
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.filter_criteria import ALL_ELITE_SCHOOLS, match_school
from app.chat_reply import check_degree

SESSION = "boss-collect"
CHROME = "com.google.Chrome"
CHAT = "https://www.zhipin.com/web/chat/index"
DB_PATH = Path(__file__).parent.parent / "data" / "candidates.db"

NAV = {"职位管理","推荐牛人","搜索","沟通","意向沟通","互动","牛人管理",
       "道具","工具箱","更多","直聘企业版","招聘规范","","投递保",
       "关闭","编辑","1","2","直播招聘","道具 首充礼"}


def cua(*args):
    cmd = ["cua-driver"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0: return {}
    try: return json.loads(r.stdout.strip() or "{}")
    except json.JSONDecodeError: return {"text": (r.stdout or "")[:200]}


def ax_tree(pid, wid):
    return cua("get_window_state", json.dumps({
        "pid": pid, "window_id": wid, "capture_mode": "ax"
    })).get("tree_markdown", "")


def find_window():
    apps = cua("list_apps")
    for a in apps.get("apps", []):
        if a.get("bundle_id") == CHROME and a.get("running"):
            pid = a["pid"]; break
    else: print("❌ Chrome 未运行"); sys.exit(1)
    lw = cua("list_windows", json.dumps({"pid": pid}))
    for w in lw.get("windows", []):
        t = w.get("title", "")
        if ("zhipin" in t or "BOSS" in t) and w.get("is_on_screen"):
            return pid, w["window_id"]
    for w in lw.get("windows", []):
        if "BOSS" in w.get("title", "") or "zhipin" in w.get("title", ""):
            return pid, w["window_id"]
    print("❌ 找不到窗口"); sys.exit(1)


def nav_to(url, pid, wid, check_fn, timeout=20):
    cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f'window.location.href = "{url}"',
    }))
    for _ in range(timeout):
        time.sleep(1)
        if check_fn(pid, wid): return True
    return False


def has_contacts(pid, wid):
    """检测页面是否渲染了联系人列表"""
    tree = ax_tree(pid, wid)
    return tree.count("AXStaticText") > 100


def click_contact(name, pid, wid):
    """JS 点击联系人"""
    safe = name.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                var el = all[i];
                if ((el.textContent || '').trim() === '{safe}' &&
                    el.children.length <= 1 && el.offsetWidth > 0) {{
                    for (var lvl = 0; lvl < 8; lvl++) {{
                        if (el.onclick || getComputedStyle(el).cursor === 'pointer') {{
                            el.click(); return 'clicked';
                        }}
                        el = el.parentElement; if (!el) break;
                    }}
                    return 'not_clickable';
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    result = str(r.get("result", r.get("text", "")))
    return "clicked" in result


def scan_contacts(pid, wid):
    """扫描左侧联系人列表（遍历所有可见联系人）

    不依赖状态标记，直接用模式匹配:
      时间(HH:MM/昨天) → 名字(2-4中文) → 职位(短文本) → [消息/状态]
    每一组时间-名字-职位就是一个联系人
    """
    tree = ax_tree(pid, wid)
    contacts = []
    current_name, current_job, current_time = None, None, None

    for line in tree.split("\n"):
        m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if not m: continue
        val = m.group(1)

        # 时间 → 保存上一个，开始新联系人
        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):
            if current_name:
                contacts.append({"name": current_name, "job": current_job or "",
                                 "time": current_time or ""})
            current_name = current_job = None
            current_time = val
            continue

        # 有当前时间，找名字（2-4中文/英文，排除纯数字/文件/推广）
        if current_time and not current_name:
            if re.match(r'^[一-鿿a-zA-Z]{2,10}$', val) \
                    and not re.match(r'^\d+k?$', val, re.IGNORECASE) \
                    and '顾问' not in val and '心仪' not in val \
                    and val not in ("全部", "未读", "批量", "全部职位", "买赠", "帮你问牛人",
                                   "不符牛人", "意向沟通", "已约面", "已获取简历", "已交换电话",
                                   "已交换微信", "收藏", "更多", "沟通中", "新招呼"):
                current_name = val
                continue

        # 有名字，找职位（短文本，非时间非数字）
        if current_name and not current_job:
            if 2 <= len(val) <= 20 \
                    and not re.match(r'^\d+$', val) \
                    and not re.match(r'^\[.+\]$', val) \
                    and not re.search(r'\.(docx?|pdf)$', val):
                current_job = val
                continue

        # 有名字+职位后的长文本 → 消息，跳过
        if current_name and current_job and len(val) > 5:
            continue

    # 保存最后一个
    if current_name:
        contacts.append({"name": current_name, "job": current_job or "",
                         "time": current_time or ""})

    # 去重
    seen, unique = set(), []
    for c in contacts:
        if c["name"] not in seen:
            seen.add(c["name"]); unique.append(c)
    return unique


def read_panel(pid, wid):
    """读右侧对话面板: name, school, degree, job_position"""
    tree = ax_tree(pid, wid)
    result = {"name": "", "school": "", "degree": "", "job": ""}

    for line in tree.split("\n"):
        # 学校
        m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', line)
        if m and not result["school"]: result["school"] = m.group(1)

        # 学历
        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]: result["degree"] = m.group(1)

        # "·" 分隔信息行 → 提取岗位
        m = re.search(r'AXStaticText\s*=\s*"(.+)"', line)
        if m and "·" in m.group(1) and len(m.group(1)) < 80:
            parts = [p.strip() for p in m.group(1).split("·")]
            for p in parts:
                school_m = re.match(r'^([一-龥]{2,8}(?:大学|学院|学校))$', p)
                if school_m and not result["school"]:
                    result["school"] = school_m.group(1)
                if p in ("博士", "硕士", "本科", "大专") and not result["degree"]:
                    result["degree"] = p
            # 第一个含中文的不是学校的作为名字
            for p in parts:
                if re.search(r'[一-鿿]', p) and not re.match(r'.*(?:大学|学院|学校).*', p) \
                        and p not in ("博士","硕士","本科","大专") and not result["name"]:
                    result["name"] = p
            if not result["job"]:
                result["job"] = m.group(1)

    # 兜底: 从JS拿岗位名
    if not result["job"]:
        r = cua("page", json.dumps({
            "pid": pid, "window_id": wid, "action": "execute_javascript",
            "javascript": """
            (function(){
                var el = document.querySelector('.chat-top-filter .dropmenu-label, .chat-select-job');
                if (el) return (el.textContent || '').trim();
                return '';
            })()
            """,
        }))
        js_job = r.get("result", r.get("text", "")) if isinstance(r, dict) else ""
        if js_job and js_job != "全部职位":
            result["job"] = js_job

    return result


def check_resume_status(pid, wid):
    """检查简历状态: {has_attachment, has_online, can_request, filename}"""
    tree = ax_tree(pid, wid)
    status = {"has_attachment": False, "has_online": False,
              "can_request": False, "filename": "", "already_has": False}

    for line in tree.split("\n"):
        if "已获取简历" in line: status["already_has"] = True
        if "AXLink (附件简历)" in line or "AXLink (在线简历)" in line:
            if "附件简历" in line: status["has_attachment"] = True
            if "在线简历" in line: status["has_online"] = True
        if "求简历" in line and "AXStaticText" not in line:
            status["can_request"] = True
        if "对方想发送附件简历给您" in line:
            status["can_request"] = True

    # 附件文件名
    m = re.search(r'AXStaticText\s*=\s*"([^"]+\.(?:docx?|pdf))"', tree)
    if m: status["filename"] = m.group(1)

    return status


def check_wechat_status(pid, wid):
    """检查微信状态: {already_exchanged, can_request}"""
    tree = ax_tree(pid, wid)
    status = {"already_exchanged": False, "can_request": False}

    for line in tree.split("\n"):
        if "已交换微信" in line: status["already_exchanged"] = True
        if "换微信" in line and "AXLink" in line:
            status["can_request"] = True

    return status


def click_element_by_text(text, pid, wid):
    """在 AX 树中找文字为 text 的可点击元素并点击"""
    tree = ax_tree(pid, wid)
    # 先找 AXLink/AXButton
    for line in tree.split("\n"):
        if text in line and ('AXLink' in line or 'AXButton' in line):
            m = re.search(r'\[(\d+)\]', line)
            if m:
                r = cua("click", json.dumps({
                    "pid": pid, "window_id": wid,
                    "element_index": int(m.group(1))
                }))
                if not r.get("error"): return True

    # 兜底: JS 点击
    safe = text.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                if ((all[i].textContent || '').trim().indexOf('{safe}') >= 0) {{
                    for (var lvl = 0; lvl < 6; lvl++) {{
                        if (all[i].onclick || getComputedStyle(all[i]).cursor === 'pointer' ||
                            all[i].tagName === 'BUTTON' || all[i].tagName === 'A') {{
                            all[i].click(); return 'clicked';
                        }}
                        all[i] = all[i].parentElement; if (!all[i]) break;
                    }}
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    return "clicked" in str(r.get("result", r.get("text", "")))


# ══════════════════════════════════════════════════
# SQLite
# ══════════════════════════════════════════════════

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            job_position TEXT,
            school TEXT,
            degree TEXT,
            resume_content TEXT,
            resume_filename TEXT,
            has_resume INTEGER DEFAULT 0,
            wechat TEXT,
            has_wechat INTEGER DEFAULT 0,
            phone TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, job_position)
        )
    """)
    conn.commit()
    return conn


def upsert_candidate(conn, data):
    """插入或更新候选人记录"""
    conn.execute("""
        INSERT OR REPLACE INTO candidates
            (name, job_position, school, degree, resume_content, resume_filename,
             has_resume, wechat, has_wechat, phone, score, status, notes, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("name", ""),
        data.get("job", ""),
        data.get("school", ""),
        data.get("degree", ""),
        data.get("resume_content", ""),
        data.get("resume_filename", ""),
        1 if data.get("has_resume") else 0,
        data.get("wechat", ""),
        1 if data.get("has_wechat") else 0,
        data.get("phone", ""),
        data.get("score", 0),
        data.get("status", "collected"),
        data.get("notes", ""),
        datetime.now().isoformat(),
    ))
    conn.commit()


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-degree", default="本科")
    p.add_argument("--schools", type=str)
    args = p.parse_args()

    whitelist = ([s.strip() for s in args.schools.split(",")] if args.schools
                 else ALL_ELITE_SCHOOLS)

    print("=" * 60)
    print(f"BOSS候选人收集 | {len(whitelist)}所学校 | 最低{args.min_degree}")
    print(f"模式: {'预览' if args.dry_run else '执行'}")
    print("=" * 60)

    if "running" not in str(cua("status")).lower():
        subprocess.run(["cua-driver", "serve"], capture_output=True); time.sleep(1)
    cua("start_session", json.dumps({"session": SESSION}))

    pid, wid = find_window()
    print(f"✓ pid={pid} wid={wid}")

    # ① 进入聊天页
    print("\n① 进入聊天页...")
    nav_to(CHAT, pid, wid, has_contacts, timeout=20)

    # 滚动加载
    for pg in range(3):
        cua("scroll", json.dumps({"pid": pid, "window_id": wid,
                                   "direction": "down", "amount": 8}))
        time.sleep(1.5)

    # ② 扫描联系人
    print("\n② 扫描联系人...")
    contacts = scan_contacts(pid, wid)
    if not contacts:
        print("❌ 未找到联系人"); sys.exit(1)

    total = len(contacts) if not args.limit else min(len(contacts), args.limit)
    print(f"  {len(contacts)} 个联系人 (处理 {total})")
    for c in contacts[:5]:
        print(f"    {c['name']:8s} | {c['job']:14s} | {c['time']}")
    if len(contacts) > 5: print(f"    ... +{len(contacts)-5}")

    # ③ 逐个审查
    print(f"\n③ 逐个收集 ({total} 人)...")
    conn = init_db()
    stats = {"collected": 0, "unsuitable": 0, "skipped": 0}

    for i, contact in enumerate(contacts[:total]):
        name = contact["name"]
        print(f"\n  [{i+1}/{total}] {name} | {contact['job']}")

        # 点击
        if not click_contact(name, pid, wid):
            print(f"    ❌ 点击失败"); stats["skipped"] += 1; continue
        time.sleep(2)

        # 读面板
        panel = read_panel(pid, wid)
        school = panel["school"] or ""
        degree = panel["degree"] or ""
        job = panel["job"] or contact.get("job", "")

        school_ok = "✅" if match_school(school, whitelist) else "❌"
        print(f"    学校: {school or '?'} {school_ok}  学历: {degree or '?'}  岗位: {job}")

        # 筛选
        if not match_school(school, whitelist):
            print(f"    → 学校不符，点'不合适'")
            if not args.dry_run:
                click_element_by_text("不合适", pid, wid)
            stats["unsuitable"] += 1
        elif degree and not check_degree(degree, args.min_degree):
            print(f"    → 学历不符，点'不合适'")
            if not args.dry_run:
                click_element_by_text("不合适", pid, wid)
            stats["unsuitable"] += 1
        else:
            # 获取简历
            resume = check_resume_status(pid, wid)
            print(f"    简历: 已有={resume['already_has']} 附件={resume['has_attachment']} "
                  f"在线={resume['has_online']} 可求={resume['can_request']} {resume['filename']}")

            resume_content = ""
            if resume["has_attachment"] and not args.dry_run:
                # 点附件简历预览 → 截图（后续OCR）
                click_element_by_text("附件简历", pid, wid)
                time.sleep(3)
                # 截图保存
                img_path = DB_PATH.parent / "resumes" / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.png"
                img_path.parent.mkdir(parents=True, exist_ok=True)
                cua("get_window_state", json.dumps({
                    "pid": pid, "window_id": wid,
                    "screenshot_out_file": str(img_path),
                }))
                print(f"    → 简历截图: {img_path}")
                resume_content = f"[screenshot: {img_path}]"

            if resume["can_request"] and not args.dry_run:
                click_element_by_text("求简历", pid, wid)
                print(f"    → 已点击'求简历'")
                time.sleep(1)

            # 获取微信
            wechat_status = check_wechat_status(pid, wid)
            print(f"    微信: 已交换={wechat_status['already_exchanged']} 可换={wechat_status['can_request']}")

            if wechat_status["can_request"] and not args.dry_run:
                click_element_by_text("换微信", pid, wid)
                print(f"    → 已点击'换微信'")
                time.sleep(1)

            # 存入数据库
            data = {
                "name": name, "job": job, "school": school, "degree": degree,
                "resume_content": resume_content,
                "resume_filename": resume.get("filename", ""),
                "has_resume": resume["already_has"] or bool(resume_content),
                "has_wechat": wechat_status["already_exchanged"],
                "status": "collected", "score": 0,
            }
            if not args.dry_run:
                upsert_candidate(conn, data)
            stats["collected"] += 1
            print(f"    ✓ 已收集")

        # 返回列表
        if i < total - 1:
            delay = 2 + random.random() * 3
            time.sleep(delay)
            nav_to(CHAT, pid, wid, has_contacts, timeout=15)

    # ⑤ 汇总
    print(f"\n{'=' * 60}")
    print(f"收集完成:")
    print(f"  ✅ 已收集: {stats['collected']}")
    print(f"  🚫 不合适: {stats['unsuitable']}")
    print(f"  ⏭ 跳过:   {stats['skipped']}")

    if not args.dry_run:
        count = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"\n数据库: {DB_PATH} ({count} 条记录)")
    conn.close()


if __name__ == "__main__":
    main()
