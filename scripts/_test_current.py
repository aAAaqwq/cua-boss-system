#!/usr/bin/env python3
"""测试当前对话记录提取 — 使用实际 read_conversation 逻辑"""
import json, subprocess, re, sys
sys.path.insert(0, ".")
from scripts.cua_chat_loop import (
    cua, scan_all_contacts, click_contact, read_conversation
)

pid, wid = 15320, 12503

# 0. 导航到沟通页
print("── 导航到沟通页 ──")
r = cua("page", json.dumps({
    "pid": pid, "window_id": wid,
    "action": "navigate",
    "url": "https://www.zhipin.com/web/chat/index",
}))
import time; time.sleep(3)

# 1. 扫描联系人
print("\n── 扫描联系人 ──")
contacts = scan_all_contacts(pid, wid)

if not contacts:
    print("没有联系人")
    sys.exit()

# 取前 3 个测试
for contact in contacts[:3]:
    name = contact.get("name", "?")
    job = contact.get("job", "?")
    time_str = contact.get("time", "?")
    print(f"\n{'='*50}")
    print(f"联系人: {name} | {job} | {time_str}")

    # 2. 点击
    ok = click_contact(pid, wid, name)
    if not ok:
        print(f"  ❌ 点击失败")
        continue
    print(f"  ✓ 已点击")

    import time
    time.sleep(2)

    # 3. 读取对话
    convo = read_conversation(pid, wid)
    school = convo.get("school") or "?"
    degree = convo.get("degree") or "?"
    info = convo.get("info_line") or "?"
    latest = convo.get("latest_candidate_msg") or "(无)"
    last_sender = convo.get("last_sender", "")
    history = convo.get("chat_history", [])

    print(f"  学校: {school} | 学历: {degree}")
    print(f"  信息: {info}")
    print(f"  最新候选人消息: {latest[:60]}")
    print(f"  最后发言: {'🟢候选人' if last_sender == 'candidate' else '🔵我们' if last_sender == 'boss' else '未知'}")

    # 4. 聊天历史
    print(f"\n  聊天历史 ({len(history)} 条):")
    if history:
        for i, msg in enumerate(history):
            role = "🔵我们" if msg["role"] == "boss" else "🟢候选人"
            print(f"    [{i+1}] {role} {msg['content'][:80]}")
    else:
        print(f"    (空)")

    time.sleep(1)
