#!/usr/bin/env python3
"""
沟通页批量收集候选人信息 — 简历 & 微信 → SQLite

流程:
  ① 进入聊天页 → 滚动加载
  ② AX树扫描所有联系人
  ③ 逐个审查:
      学校不在白名单/学历不达标 → 点"不合适"
      符合条件:
        a. 获取简历: 同意附件→点附件简历→AX树提取全文→保存
        b. 获取微信: 点换微信→确认→记录
  ④ 所有数据 & 简历内容存入 candidates.db (SQLite)

用法:
  python scripts/cua_collect.py                  # 全部联系人
  python scripts/cua_collect.py --limit 10        # 前10个
  python scripts/cua_collect.py --dry-run          # 预览(不操作不写库)
  python scripts/cua_collect.py --min-degree 硕士  # 学历筛选
  python scripts/cua_collect.py --schools "清华,北大" # 学校白名单
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
    return ax_tree(pid, wid).count("AXStaticText") > 100


def click_contact(name, pid, wid):
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
    return "clicked" in str(r.get("result", r.get("text", "")))


def js_click(text, pid, wid):
    """JS点击任意文字元素"""
    safe = text.replace("'", "\\'")
    r = cua("page", json.dumps({
        "pid": pid, "window_id": wid, "action": "execute_javascript",
        "javascript": f"""
        (function(){{
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {{
                if ((all[i].textContent || '').trim() === '{safe}' &&
                    all[i].children.length === 0) {{
                    for (var lvl = 0; lvl < 8; lvl++) {{
                        if (all[i].onclick || getComputedStyle(all[i]).cursor === 'pointer' ||
                            all[i].tagName === 'BUTTON' || all[i].tagName === 'A') {{
                            all[i].click(); return 'clicked ' + all[i].tagName;
                        }}
                        all[i] = all[i].parentElement; if (!all[i]) break;
                    }}
                    all[i].click(); return 'clicked self';
                }}
            }}
            return 'not_found';
        }})()
        """,
    }))
    return "clicked" in str(r.get("result", r.get("text", "")))


def ax_click(text, pid, wid):
    """AX树找元素并点击"""
    tree = ax_tree(pid, wid)
    for line in tree.split("\n"):
        if text in line and ('AXLink' in line or 'AXButton' in line):
            m = re.search(r'\[(\d+)\]', line)
            if m:
                r = cua("click", json.dumps({
                    "pid": pid, "window_id": wid,
                    "element_index": int(m.group(1))
                }))
                if not r.get("error"): return True
    return False


# ══════════════════════════════════════════════════
# 扫描 & 读取
# ══════════════════════════════════════════════════

def scan_contacts(pid, wid):
    """扫描左侧联系人列表（时间→名字→职位模式）"""
    tree = ax_tree(pid, wid)
    contacts = []
    current_name, current_job, current_time = None, None, None

    for line in tree.split("\n"):
        m = re.search(r'AXStaticText\s*=\s*"(.+?)"', line)
        if not m: continue
        val = m.group(1)

        if re.match(r'^(?:\d{1,2}:\d{2}|昨天|前天|\d{1,2}-\d{1,2})$', val):
            if current_name:
                contacts.append({"name": current_name, "job": current_job or "",
                                 "time": current_time or ""})
            current_name = current_job = None
            current_time = val; continue

        if current_time and not current_name:
            if re.match(r'^[一-鿿a-zA-Z]{2,10}$', val) \
                    and not re.match(r'^\d+k?$', val, re.IGNORECASE) \
                    and '顾问' not in val and '心仪' not in val \
                    and val not in ("全部","未读","批量","全部职位","买赠","帮你问牛人",
                                   "不符牛人","意向沟通","已约面","已获取简历","已交换电话",
                                   "已交换微信","收藏","更多","沟通中","新招呼"):
                current_name = val; continue

        if current_name and not current_job:
            if 2 <= len(val) <= 20 and not re.match(r'^\d+$', val) \
                    and not re.match(r'^\[.+\]$', val) \
                    and not re.search(r'\.(docx?|pdf)$', val):
                current_job = val; continue

        if current_name and current_job and len(val) > 5: continue

    if current_name:
        contacts.append({"name": current_name, "job": current_job or "",
                         "time": current_time or ""})

    seen, unique = set(), []
    for c in contacts:
        if c["name"] not in seen:
            seen.add(c["name"]); unique.append(c)
    return unique


def read_panel(pid, wid):
    """读右侧对话面板: name, school, degree, job_position, wechat"""
    tree = ax_tree(pid, wid)
    result = {"name": "", "school": "", "degree": "", "job": "", "wechat": "",
              "phone": "", "has_attachment": False, "has_online": False,
              "resume_filename": "", "can_request_resume": False,
              "can_request_wechat": False, "already_has_wechat": False,
              "already_has_resume": False}

    for line in tree.split("\n"):
        # 学校
        m = re.search(r'AXStaticText\s*=\s*"([一-龥]{2,8}(?:大学|学院|学校))"', line)
        if m and not result["school"]: result["school"] = m.group(1)

        # 学历
        m = re.search(r'AXStaticText\s*=\s*"(博士|硕士|本科|大专)"', line)
        if m and not result["degree"]: result["degree"] = m.group(1)

        # "·" 分隔信息行
        m = re.search(r'AXStaticText\s*=\s*"(.+)"', line)
        if m and "·" in m.group(1) and len(m.group(1)) < 80:
            parts = [p.strip() for p in m.group(1).split("·")]
            for p in parts:
                school_m = re.match(r'^([一-龥]{2,8}(?:大学|学院|学校))$', p)
                if school_m and not result["school"]: result["school"] = school_m.group(1)
                if p in ("博士","硕士","本科","大专") and not result["degree"]: result["degree"] = p
            for p in parts:
                if re.search(r'[一-鿿]', p) and not re.match(r'.*(?:大学|学院|学校).*', p) \
                        and p not in ("博士","硕士","本科","大专") and not result["name"]:
                    result["name"] = p
            if not result["job"]: result["job"] = m.group(1)

        # 简历 & 微信状态
        if "附件简历" in line and "AXLink" in line: result["has_attachment"] = True
        if "在线简历" in line and "AXLink" in line: result["has_online"] = True
        if "已获取简历" in line: result["already_has_resume"] = True
        if "已交换微信" in line: result["already_has_wechat"] = True
        if "换微信" in line and "AXStaticText" in line: result["can_request_wechat"] = True
        if "求简历" in line and "AXStaticText" in line: result["can_request_resume"] = True

        # 附件文件名
        m = re.search(r'AXStaticText\s*=\s*"([^"]+\.(?:docx?|pdf|doc))"', line)
        if m: result["resume_filename"] = m.group(1)

        # 对方要发附件
        if "对方想发送附件简历给您" in line: result["can_request_resume"] = True

    return result


def extract_resume_text(pid, wid):
    """从简历预览区提取完整文本（AX树高index区域）"""
    tree = ax_tree(pid, wid)
    lines = []
    in_resume = False

    for line in tree.split("\n"):
        m = re.search(r'\[(\d+)\].*AXStaticText\s*=\s*"([^"]+)"', line)
        if not m: continue
        idx, val = int(m.group(1)), m.group(2)

        # 简历预览区在 250-760 范围
        if not (250 <= idx <= 760): continue

        # 过滤左侧面板聊天内容
        if re.match(r'^\d{1,2}:\d{2}$', val): continue
        if re.match(r'^(?:昨天|前天|\d{1,2}-\d{1,2})$', val): continue
        if val in ('开发', 'CEO标注助理', '已读', '送达', '没有更多了'): continue
        if re.match(r'^(?:你好|您好|BOSS|Boss|boss|牛人|对方|此牛人|顾问|比较感兴趣)', val): continue
        if '沟通的职位' in val: continue
        if '优先提醒' in val: continue
        if '设置邮箱' in val: continue
        if '您可以在线预览' in val: continue
        if '后投递的简历' in val: continue
        if '对方想发送' in val: continue
        if '求简历' == val or '换电话' == val or '换微信' == val: continue
        if '约面试' == val or '不合适' == val or '发送' == val: continue
        if re.match(r'^(?:拒绝|同意|在线简历|附件简历)$', val): continue

        # 简历区域标记
        if '个人简历' in val or '个人资料' in val:
            in_resume = True
            lines.append(val); continue

        if in_resume or len(val) > 3:
            # 过滤 BOSS 内部 ID
            if re.match(r'^[a-f0-9]{40,}~+$', val): continue
            lines.append(val)

    return "\n".join(lines)


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
            email TEXT,
            score INTEGER DEFAULT 0,
            status TEXT DEFAULT 'collected',
            notes TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, job_position)
        )
    """)
    conn.commit()
    return conn


def upsert_candidate(conn, data):
    conn.execute("""
        INSERT OR REPLACE INTO candidates
            (name, job_position, school, degree, resume_content, resume_filename,
             has_resume, wechat, has_wechat, phone, email, score, status, notes, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("name", ""), data.get("job", ""),
        data.get("school", ""), data.get("degree", ""),
        data.get("resume_content", ""), data.get("resume_filename", ""),
        1 if data.get("has_resume") else 0,
        data.get("wechat", ""), 1 if data.get("has_wechat") else 0,
        data.get("phone", ""), data.get("email", ""),
        data.get("score", 0), data.get("status", "collected"),
        data.get("notes", ""), datetime.now().isoformat(),
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

    # ①
    print("\n① 进入聊天页...")
    nav_to(CHAT, pid, wid, has_contacts, timeout=20)
    for pg in range(3):
        cua("scroll", json.dumps({"pid": pid, "window_id": wid,
                                   "direction": "down", "amount": 8}))
        time.sleep(1.5)

    # ②
    print("\n② 扫描联系人...")
    contacts = scan_contacts(pid, wid)
    if not contacts: print("❌ 未找到联系人"); sys.exit(1)

    total = len(contacts) if not args.limit else min(len(contacts), args.limit)
    print(f"  {len(contacts)} 个联系人 (处理 {total})")
    for c in contacts[:5]:
        print(f"    {c['name']:8s} | {c['job']:14s} | {c['time']}")
    if len(contacts) > 5: print(f"    ... +{len(contacts)-5}")

    # ③
    print(f"\n③ 逐个收集 ({total} 人)...")
    conn = init_db() if not args.dry_run else None
    stats = {"collected": 0, "unsuitable": 0, "skipped": 0}

    for i, contact in enumerate(contacts[:total]):
        name = contact["name"]
        print(f"\n  [{i+1}/{total}] {name} | {contact['job']}")

        if not click_contact(name, pid, wid):
            print(f"    ❌ 点击失败"); stats["skipped"] += 1; continue
        time.sleep(2)

        panel = read_panel(pid, wid)
        school = panel["school"] or ""
        degree = panel["degree"] or ""
        job = panel["job"] or contact.get("job", "")

        school_ok = "✅" if match_school(school, whitelist) else "❌"
        print(f"    学校: {school or '?'} {school_ok}  学历: {degree or '?'}  岗位: {job}")

        if not match_school(school, whitelist):
            print(f"    → 学校不符，点'不合适'")
            if not args.dry_run: ax_click("不合适", pid, wid)
            stats["unsuitable"] += 1
        elif degree and not check_degree(degree, args.min_degree):
            print(f"    → 学历不符，点'不合适'")
            if not args.dry_run: ax_click("不合适", pid, wid)
            stats["unsuitable"] += 1
        else:
            print(f"    简历: 附件={panel['has_attachment']} 在线={panel['has_online']} "
                  f"已有={panel['already_has_resume']} 可求={panel['can_request_resume']} "
                  f"{panel['resume_filename']}")
            print(f"    微信: 已交换={panel['already_has_wechat']} 可换={panel['can_request_wechat']}")

            resume_content = ""
            if not args.dry_run:
                # a. 有附件 → 先同意(如需要) → 点附件简历 → 提取
                if panel["has_attachment"]:
                    # 同意接收附件
                    if panel["can_request_resume"] and "对方想发送" in ax_tree(pid, wid):
                        js_click("同意", pid, wid); time.sleep(2)
                    # 点附件简历打开预览
                    ax_click("附件简历", pid, wid); time.sleep(3)
                    # 提取
                    resume_content = extract_resume_text(pid, wid)
                    print(f"    → 简历提取: {len(resume_content)} 字")

                # b. 无附件但有在线简历 → 点在线简历
                elif panel["has_online"]:
                    ax_click("在线简历", pid, wid); time.sleep(3)
                    resume_content = extract_resume_text(pid, wid)
                    print(f"    → 在线简历提取: {len(resume_content)} 字")

                # c. 可以求简历 → 点求简历
                elif panel["can_request_resume"]:
                    js_click("求简历", pid, wid)
                    print(f"    → 已点'求简历'")
                    time.sleep(1)

                # d. 换微信 → 点换微信 → 确认
                if panel["can_request_wechat"]:
                    js_click("换微信", pid, wid); time.sleep(1)
                    # 确认框 "确定"
                    if "确定与对方交换微信" in ax_tree(pid, wid):
                        js_click("确定", pid, wid)
                        print(f"    → 已确认交换微信")
                    time.sleep(1)

            # 保存
            data = {
                "name": name, "job": job, "school": school, "degree": degree,
                "resume_content": resume_content,
                "resume_filename": panel.get("resume_filename", ""),
                "has_resume": panel["already_has_resume"] or bool(resume_content),
                "wechat": "", "has_wechat": panel["already_has_wechat"],
                "phone": "", "email": "", "score": 0, "status": "collected",
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

    # ⑤
    print(f"\n{'=' * 60}")
    print(f"收集完成: ✅{stats['collected']} 🚫{stats['unsuitable']} ⏭{stats['skipped']}")
    if conn:
        count = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        print(f"数据库: {DB_PATH} ({count} 条)")
        conn.close()


if __name__ == "__main__":
    main()
