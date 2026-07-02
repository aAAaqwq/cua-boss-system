#!/usr/bin/env python3
# © 2026 Daniel Li (Open CAIO). 伯乐 AI 招聘助手 · 版权所有 All rights reserved.
"""desktop/bole_tools.py — 伯乐 agent 的工具集（真实操作本系统数据与脚本）

让伯乐从「只会聊天」变成「能真干活」：读真实候选人库、跑真实招聘脚本、约面试。
工具 schema 是 OpenAI 风格；execute(name, args) 真正执行并把结果喂回模型。
读类工具直接查 data/candidates.db（只读）；动作类工具走 desktop.services 的
后台任务(与操作台共用 _JOBS)，或调 cua_interview 脚本。
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# 回灌给模型的文本里，把手机号/微信号脱敏（伯乐只需进度，不需联系方式）
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_WECHAT_RE = re.compile(r"(微信|wechat|vx|v信)[:：]?\s*([A-Za-z0-9_-]{5,})", re.I)


def _redact(text: str) -> str:
    text = _PHONE_RE.sub("1**********", text or "")
    return _WECHAT_RE.sub(lambda m: m.group(1) + ":***", text)

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "data" / "candidates.db"
PY = sys.executable or "python3"


TOOLS = [
    {"type": "function", "function": {
        "name": "get_dashboard",
        "description": "看板统计：候选人总数、有简历数、已评分数、已加微信数、已约面试数、今日更新数。用户问『整体情况/进度/招了多少人/数据』时用。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "top_candidates",
        "description": "按 AI 评分返回最值得面试的候选人（真实数据：姓名/学校/学历/沟通岗位/分数/综合评价）。用户问『谁最合适/评分榜/推荐谁/看看候选人』时用。",
        "parameters": {"type": "object", "properties": {
            "top": {"type": "integer", "description": "返回前几名，默认 5"},
            "days": {"type": "integer", "description": "只看最近几天有更新的，默认 14；填 0 表示不限"},
        }},
    }},
    {"type": "function", "function": {
        "name": "find_candidate",
        "description": "按姓名（模糊）搜索候选人，返回其档案与评分。用户提到某个具体人名时用。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "候选人姓名或片段"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "run_task",
        "description": ("启动招聘自动化任务（会真实驱动 Chrome 操作 BOSS直聘）。"
                        "task: greet=推荐页打招呼 / collect=收简历+微信入库 / chat=智能沟通回复 / pipeline=全流程一条龙。"
                        "重要：除非用户明确说『真跑/开始执行/别预览』，否则一律先 dry_run=true 预览，并在回复里请用户确认。"),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "enum": ["greet", "collect", "chat", "pipeline"]},
            "limit": {"type": "integer", "description": "人数，默认 20"},
            "min_degree": {"type": "string", "enum": ["大专", "本科", "硕士", "博士"]},
            "dry_run": {"type": "boolean", "description": "true=只预览不真操作（默认建议 true）"},
        }, "required": ["task"]},
    }},
    {"type": "function", "function": {
        "name": "job_status",
        "description": "查一个已启动任务的运行状态与最新日志（配合 run_task 返回的 job_id）。",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"},
        }, "required": ["job_id"]},
    }},
    {"type": "function", "function": {
        "name": "schedule_interview",
        "description": "给某候选人预约面试（真实在 BOSS 上发面试邀请）。需要 uid（可先用 find_candidate/top_candidates 拿到）。除非用户确认，先 dry_run=true。",
        "parameters": {"type": "object", "properties": {
            "uid": {"type": "string"},
            "type": {"type": "string", "enum": ["线上", "线下"]},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "HH:MM"},
            "dry_run": {"type": "boolean"},
        }, "required": ["uid", "type", "date", "time"]},
    }},
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _top_candidates(top: int = 5, days: int = 14) -> str:
    if not DB_PATH.exists():
        return "候选人库还没有数据，先跑一次收集（collect）。"
    top = max(1, min(int(top or 5), 20))
    where = "score IS NOT NULL AND score > 0"
    params: list = []
    if days and int(days) > 0:
        where += " AND updated_at >= datetime('now', ?)"
        params.append(f"-{int(days)} days")
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT name, school, degree, job_position, score, score_summary "
            f"FROM candidates WHERE {where} ORDER BY score DESC LIMIT ?",
            [*params, top]).fetchall()
    finally:
        conn.close()
    if not rows:
        return "还没有已评分的候选人（需要先收集简历并评分）。"
    out = [{"name": r["name"], "school": r["school"], "degree": r["degree"],
            "job": r["job_position"], "score": r["score"],
            "summary": (r["score_summary"] or "")[:120]} for r in rows]
    return json.dumps(out, ensure_ascii=False)


def _find_candidate(name: str) -> str:
    if not name or not DB_PATH.exists():
        return "没给名字或库为空。"
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT uid, name, school, degree, job_position, score, score_summary, "
            "has_resume, has_wechat, status FROM candidates WHERE name LIKE ? LIMIT 6",
            (f"%{name}%",)).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"没找到叫「{name}」的候选人。"
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


def _run_task(args: dict, allow_real: bool) -> str:
    from desktop import services as S
    task = args.get("task", "")
    # 安全底线(代码强制，不信任 LLM)：未获本轮真实执行授权 → 一律 dry_run 预览。
    dry = True if not allow_real else bool(args.get("dry_run", True))
    params = {"min_degree": args.get("min_degree"), "dry_run": dry}
    limit = args.get("limit")
    # pipeline 的人数走 greet/collect/chat 三个键；单步走 limit（否则用户说的人数会被丢）
    if task == "pipeline":
        if limit is not None:
            params["greet"] = params["collect"] = params["chat"] = limit
    else:
        params["limit"] = limit
    r = S.launch_job(task, params)
    if not r.get("ok"):
        return f"启动失败：{r.get('error')}"
    if dry:
        return (f"已按「{task}」跑了一次**预览**(dry-run，未真实操作 BOSS)。"
                f"参数：{r.get('cmd', '')}。若确认无误，请用户明确说「真跑/开始执行」我才会真的执行。")
    return (f"已**真实启动**任务「{task}」(job_id={r['job_id']})。可用 job_status 查进度，"
            f"或让用户去操作台看实时日志。")


def _job_status(job_id: str) -> str:
    from desktop import services as S
    st = S.job_state(job_id, 0)
    if not st.get("ok"):
        return st.get("error", "任务不存在")
    tail = _redact("\n".join(st.get("log", [])[-15:]))
    return json.dumps({"status": st.get("status"), "returncode": st.get("returncode"),
                       "log_tail": tail}, ensure_ascii=False)


_UID_RE = re.compile(r"^\d{4,}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _schedule_interview(args: dict, allow_real: bool) -> str:
    uid = str(args.get("uid", "")).strip()
    date, tm = str(args.get("date", "")).strip(), str(args.get("time", "")).strip()
    itype = args.get("type", "线上")
    # 就近参数校验(防御纵深 + 更清晰的错误反馈)
    if not _UID_RE.match(uid):
        return "uid 不合法（应为数字）；先用 find_candidate 拿到正确 uid。"
    if not _DATE_RE.match(date) or not _TIME_RE.match(tm):
        return "日期/时间格式应为 YYYY-MM-DD / HH:MM。"
    if itype not in ("线上", "线下"):
        return "面试类型只能是 线上 或 线下。"
    # 安全底线：未授权真实执行 → 强制 --dry-run（只预览不真发邀请）
    dry = not allow_real
    cmd = [PY, str(ROOT / "scripts" / "cua_interview.py"),
           "--uid", uid, "--type", itype, "--date", date, "--time", tm]
    if dry:
        cmd.append("--dry-run")
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        out = _redact((r.stdout or r.stderr or "").strip()[-500:])
        if dry:
            return f"已**预览**面试预约(未真实发送)：{out or '完成'}。确认后请用户明确说「真跑/确认」再发。"
        return f"面试邀请已**真实发送**：{out or '完成'}"
    except Exception as e:  # noqa: BLE001
        return f"预约失败：{e}"


def execute(name: str, args: dict, allow_real: bool = False) -> str:
    """执行工具，返回给模型的结果文本。

    allow_real：是否允许本次真实执行动作类工具（run_task/schedule_interview）。
    由 services 按「用户本轮消息是否显式确认」在代码层决定，不信任 LLM 自己判断——
    从架构上切断「污染数据→模型当场真实执行」的单轮闭环（防间接提示注入误操作）。
    """
    if name == "get_dashboard":
        from desktop import services as S
        return json.dumps(S.dashboard().get("stats", {}), ensure_ascii=False)
    if name == "top_candidates":
        return _redact(_top_candidates(args.get("top", 5), args.get("days", 14)))
    if name == "find_candidate":
        return _redact(_find_candidate(args.get("name", "")))
    if name == "run_task":
        return _run_task(args, allow_real)
    if name == "job_status":
        return _job_status(args.get("job_id", ""))
    if name == "schedule_interview":
        return _schedule_interview(args, allow_real)
    return f"未知工具：{name}"
