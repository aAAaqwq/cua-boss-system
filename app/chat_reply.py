"""
聊天回复模块
============
模板匹配 + DeepSeek API + 学历判断 + 岗位感知

零 pip 依赖，纯标准库。

配置:
  config/jobs.json  — 岗位定义 + 专属话术模板（推荐）
  config/chat_templates.json — 旧版扁平模板（兼容）
"""
import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

# ── 学历等级 ──

DEGREE_RANK = {"博士": 4, "硕士": 3, "本科": 2, "大专": 1}


def check_degree(degree: str, min_degree: str = "本科") -> bool:
    """学历是否达到最低要求"""
    return DEGREE_RANK.get(degree, 0) >= DEGREE_RANK.get(min_degree, 0)


# ══════════════════════════════════════════════════
# 配置加载 — 岗位模式 (jobs.json) 优先，兼容旧格式
# ══════════════════════════════════════════════════

DEFAULT_JOBS_CONFIG = Path(__file__).parent.parent / "config" / "jobs.json"
DEFAULT_OLD_CONFIG = Path(__file__).parent.parent / "config" / "chat_templates.json"


def load_jobs_config(config_path: Optional[str] = None) -> dict:
    """加载岗位配置

    返回:
      {
        "jobs": [
          {
            "id": "ai-fullstack",
            "title": "AI全栈开发工程师",
            "requirements": "...",
            "location": "广州",
            "salary": "12-18K",
            "degree": "本科",
            "templates": [...],
          },
          ...
        ],
        "fallback_templates": [...],
        "mode": "jobs" | "flat"   # jobs=新格式, flat=旧格式兼容
      }
    """
    path = Path(config_path) if config_path else DEFAULT_JOBS_CONFIG

    # 优先加载 jobs.json
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "jobs" in data:
                result = {
                    "jobs": data["jobs"],
                    "fallback_templates": data.get("fallback_templates", []),
                    "mode": "jobs",
                }
                # 确保每个 job 的 templates 按 priority 排序
                for job in result["jobs"]:
                    job["templates"] = sorted(
                        job.get("templates", []),
                        key=lambda t: t.get("priority", 99),
                    )
                result["fallback_templates"] = sorted(
                    result["fallback_templates"],
                    key=lambda t: t.get("priority", 99),
                )
                return result
        except (json.JSONDecodeError, KeyError):
            pass

    # 兜底: 旧格式 chat_templates.json
    old_path = Path(config_path) if config_path else DEFAULT_OLD_CONFIG
    if old_path.exists():
        try:
            data = json.loads(old_path.read_text(encoding="utf-8"))
            templates = data.get("templates", [])
            return {
                "jobs": [],
                "fallback_templates": sorted(
                    templates, key=lambda t: t.get("priority", 99)
                ),
                "mode": "flat",
            }
        except (json.JSONDecodeError, KeyError):
            pass

    # 最终兜底
    return {
        "jobs": [],
        "fallback_templates": [
            {"id": "fallback", "reply": "收到，我稍后看一下回复你～", "match_keywords": [], "priority": 99}
        ],
        "mode": "flat",
    }


# ══════════════════════════════════════════════════
# 岗位检测 — 从候选人消息/职位推断目标岗位
# ══════════════════════════════════════════════════

def detect_job(
    candidate_message: str,
    candidate_job: str = "",
    jobs: list[dict] = None,
) -> Optional[str]:
    """根据候选人消息和当前职位，推断他/她对应哪个招聘岗位

    返回 job_id 或 None（无法判断时）
    """
    if not jobs:
        return None

    combined = f"{candidate_job} {candidate_message}".lower()

    # 每个岗位的关键词（从 title + requirements 提取）
    job_scores = []
    for job in jobs:
        score = 0
        title = job.get("title", "").lower()
        reqs = job.get("requirements", "").lower()

        # 岗位名直接匹配
        for word in title.split():
            if len(word) > 1 and word in combined:
                score += 3

        # 关键角色词
        role_keywords = {
            "ai-fullstack": ["全栈", "开发", "工程师", "python", "react", "fastapi", "agent", "rag", "llm", "后端", "前端"],
            "tech-intern": ["实习", "intern", "大三", "大二", "大四", "应届", "在校", "暑假", "寒假"],
            "ai-product-manager": ["产品", "pm", "axure", "需求", "原型", "product", "经理"],
            "ai-ops-intern": ["运营", "内容", "公众号", "小红书", "社群", "增长", "写作", "排版", "媒体"],
            "chief-scientist": ["首席", "科学家", "算法", "博士", "研究", "论文", "科研", "学术", "phd"],
        }

        for kw in role_keywords.get(job["id"], []):
            if kw in combined:
                score += 2

        # 招聘岗位名包含候选人的当前职位关键词
        for word in candidate_job.lower().split():
            if len(word) > 1 and word in title:
                score += 1

        if score > 0:
            job_scores.append((score, job["id"]))

    if job_scores:
        job_scores.sort(reverse=True)
        return job_scores[0][1]

    return None


# ══════════════════════════════════════════════════
# 模板匹配
# ══════════════════════════════════════════════════

def match_template(
    message: str,
    templates: list[dict],
    fallback_templates: list[dict] = None,
) -> Optional[str]:
    """关键词匹配：先在 templates 中找，再在 fallback_templates 中找

    返回匹配到的 reply 文本，或 None
    """
    if not message:
        return None

    all_templates = list(templates)
    if fallback_templates:
        all_templates.extend(fallback_templates)
    # 按 priority 排序
    all_templates.sort(key=lambda t: t.get("priority", 99))

    msg_lower = message.lower()
    for tpl in all_templates:
        keywords = tpl.get("match_keywords", [])
        if not keywords:
            continue  # 跳过纯兜底（空关键词）
        for kw in keywords:
            if kw.lower() in msg_lower:
                return tpl["reply"]
    return None


def get_fallback_reply(templates: list[dict], fallback_templates: list[dict] = None) -> str:
    """获取兜底回复：取 match_keywords 为空的第一条"""
    for tpl in templates:
        if not tpl.get("match_keywords"):
            return tpl["reply"]
    if fallback_templates:
        for tpl in fallback_templates:
            if not tpl.get("match_keywords"):
                return tpl["reply"]
    return "收到，我稍后看一下回复你～"


# ══════════════════════════════════════════════════
# DeepSeek API
# ══════════════════════════════════════════════════

SYSTEM_PROMPT = (
    "你是BOSS直聘上的招聘官，正在与候选人对话。\n"
    "规则：\n"
    "1. 回复不超过80字，简洁自然\n"
    "2. 语气友好专业，像真人聊天\n"
    "3. 严禁索要微信、电话、转账\n"
    "4. 不承诺 offer\n"
    "5. 针对性回复候选人问题，不要泛泛而谈"
)


def call_deepseek(
    candidate_name: str,
    candidate_message: str,
    history: Optional[list[dict]] = None,
    job_context: str = "",
) -> tuple[Optional[str], str]:
    """调用 DeepSeek API 生成回复，返回 (reply, error_msg)"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None, "DEEPSEEK_API_KEY not set"

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    system = SYSTEM_PROMPT
    if job_context:
        system += f"\n当前招聘的岗位信息: {job_context}"

    messages = [{"role": "system", "content": system}]
    if history:
        for turn in history[-10:]:
            messages.append(turn)
    messages.append({
        "role": "user",
        "content": f"候选人({candidate_name})说：{candidate_message}",
    })

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 150,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip(), ""
    except Exception as e:
        return None, str(e)


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════

def generate_reply(
    message: str,
    templates: list[dict],
    candidate_name: str = "",
    history: Optional[list[dict]] = None,
    job_templates: list[dict] = None,
    fallback_templates: list[dict] = None,
    job_context: str = "",
) -> str:
    """生成回复

    匹配优先级:
      1. 岗位专属模板 (job_templates)
      2. 通用模板 (templates，兼容旧格式)
      3. 兜底模板 (fallback_templates)
      4. DeepSeek API
      5. 最终 fallback 文本

    参数:
      message: 候选人最新消息
      templates: 通用模板列表 (旧格式兼容)
      job_templates: 当前岗位的专属模板
      fallback_templates: 全局兜底模板
      job_context: 岗位描述文本 (传给 DeepSeek)
    """
    # 1. 岗位专属模板优先
    if job_templates:
        reply = match_template(message, job_templates)
        if reply:
            return reply

    # 2. 通用模板
    all_fallback = list(fallback_templates or [])
    reply = match_template(message, templates, all_fallback)
    if reply:
        return reply

    # 3. DeepSeek API
    reply, err = call_deepseek(candidate_name, message, history, job_context)
    if reply:
        return reply

    # 4. 最终兜底
    return get_fallback_reply(
        job_templates or [],
        (fallback_templates or []) + templates,
    )


# ══════════════════════════════════════════════════
# 兼容旧接口 (cua_chat_loop.py 用)
# ══════════════════════════════════════════════════

def load_templates(config_path: Optional[str] = None) -> list[dict]:
    """兼容旧接口: 返回扁平模板列表"""
    cfg = load_jobs_config(config_path)
    templates = list(cfg.get("fallback_templates", []))
    for job in cfg.get("jobs", []):
        templates.extend(job.get("templates", []))
    return sorted(templates, key=lambda t: t.get("priority", 99))
