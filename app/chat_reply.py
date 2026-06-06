"""
聊天回复模块
============
模板匹配 + DeepSeek API + 学历判断 + 岗位感知

零 pip 依赖，纯标准库。

配置:
  config/jobs.json  — 岗位定义 + 专属话术模板（推荐）
  config/templates.json — 话术模板（三层：岗位专属 → 类别 → 兜底）
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
# 配置加载 — templates.json + jobs.json 双文件模式
# 兼容旧格式: jobs.json 内嵌 templates（v4-v5）
# ══════════════════════════════════════════════════

CONFIG_DIR = Path(__file__).parent.parent / "config"
DEFAULT_JOBS_CONFIG = CONFIG_DIR / "jobs.json"
DEFAULT_TEMPLATES_CONFIG = CONFIG_DIR / "templates.json"


def _sort_templates(templates: list[dict]) -> list[dict]:
    """按 priority 排序模板列表"""
    return sorted(templates, key=lambda t: t.get("priority", 99))


def load_jobs_config(config_path: Optional[str] = None) -> dict:
    """加载岗位配置 + 话术模板

    优先: templates.json + jobs.json（双文件模式）
    兜底: jobs.json 内嵌 templates（v4-v5 兼容）

    返回:
      {
        "jobs": [
          {
            "id": "dev", "title": "开发", "category": "tech",
            "requirements": "...", "location": "广州", "salary": "16K-30K", "degree": "本科",
            "templates": [...],          # 岗位专属，按 priority 排序
            "category_templates": [...], # 类别通用 (tech/nontech)
          },
          ...
        ],
        "fallback_templates": [...],
        "mode": "templates" | "jobs"
      }
    """
    config_dir = Path(config_path).parent if config_path else CONFIG_DIR
    jobs_path = Path(config_path) if config_path else DEFAULT_JOBS_CONFIG
    templates_path = config_dir / "templates.json"

    # ── 模式1: templates.json + jobs.json 双文件 ──
    if templates_path.exists() and jobs_path.exists():
        try:
            tpl_data = json.loads(templates_path.read_text(encoding="utf-8"))
            job_data = json.loads(jobs_path.read_text(encoding="utf-8"))
            tpl_jobs = tpl_data.get("jobs", {})
            tpl_categories = tpl_data.get("categories", {})
            fallback = _sort_templates(tpl_data.get("fallback", []))

            jobs = []
            for j in job_data.get("jobs", []):
                jid = j.get("id", "")
                category = j.get("category", "")
                job = dict(j)
                job["templates"] = _sort_templates(tpl_jobs.get(jid, []))
                job["category_templates"] = _sort_templates(tpl_categories.get(category, []))
                jobs.append(job)

            return {
                "jobs": jobs,
                "fallback_templates": fallback,
                "mode": "templates",
            }
        except (json.JSONDecodeError, KeyError):
            pass

    # ── 模式2: jobs.json 内嵌 templates（v4-v5 兼容）──
    if jobs_path.exists():
        try:
            data = json.loads(jobs_path.read_text(encoding="utf-8"))
            if "jobs" in data:
                jobs = []
                for job in data["jobs"]:
                    j = dict(job)
                    j["templates"] = _sort_templates(job.get("templates", []))
                    jobs.append(j)
                return {
                    "jobs": jobs,
                    "fallback_templates": _sort_templates(data.get("fallback_templates", [])),
                    "mode": "jobs",
                }
        except (json.JSONDecodeError, KeyError):
            pass

    # ── 最终兜底 ──
    return {
        "jobs": [],
        "fallback_templates": [
            {"id": "fallback", "reply": "收到，我稍后看一下回复你～", "match_keywords": [], "priority": 99}
        ],
        "mode": "minimal",
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
            "dev": ["开发", "工程师", "java", "架构", "后端", "spring", "全栈", "程序员", "coding", "技术"],
            "annotation": ["获客", "网红", "KOL", "营销", "博主", "达人", "增长", "线索", "流量", "新媒体", "内容运营", "social"],
            "annotation-2": ["助理", "战略", "咨询", "创业", "项目管理", "MBA", "合伙人", "总助", "chief"],
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
# 模板变量替换 — 将 {salary}/{location} 等替换为 jobs.json 字段
# ══════════════════════════════════════════════════

TEMPLATE_VARS = ["salary", "location", "title", "requirements", "degree"]


def _substitute_vars(reply: str, job: dict = None) -> str:
    """替换模板中的 {field} 占位符为 job 对应字段值

    >>> _substitute_vars("薪资{salary}", {"salary": "16K-30K"})
    '薪资16K-30K'
    """
    if not job:
        return reply
    result = reply
    for var in TEMPLATE_VARS:
        placeholder = "{" + var + "}"
        if placeholder in result:
            value = job.get(var, "")
            if value:
                result = result.replace(placeholder, value)
    return result


# ══════════════════════════════════════════════════
# 模板匹配
# ══════════════════════════════════════════════════

def match_template(
    message: str,
    templates: list[dict],
    fallback_templates: list[dict] = None,
    job: dict = None,
) -> Optional[str]:
    """关键词匹配：先在 templates 中找，再在 fallback_templates 中找

    返回匹配到的 reply 文本（已替换 {变量}），或 None
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
                return _substitute_vars(tpl["reply"], job)
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
# ══════════════════════════════════════════════════
# DeepSeek API — 结合模板提示词 + 聊天上下文智能生成回复
# ══════════════════════════════════════════════════

# API 配置优先级: 环境变量 > .env 文件
#   DEEPSEEK_API_KEY   — API 密钥（必须）
#   DEEPSEEK_BASE_URL  — 接口地址（默认 https://api.deepseek.com）
#   DEEPSEEK_MODEL     — 模型名（默认 deepseek-chat）

SYSTEM_PROMPT = (
    "你是BOSS直聘上的招聘官，正在与候选人对话。\n"
    "规则：\n"
    "1. 回复不超过80字，简洁自然\n"
    "2. 语气友好专业，像真人聊天\n"
    "3. 严禁索要微信、电话、转账\n"
    "4. 不承诺 offer\n"
    "5. 根据对话上下文针对性回复，不要泛泛而谈\n"
    "6. 参考「建议回复方向」但用自己的话表达，不要照搬"
)


def _load_env_file() -> None:
    """从项目根目录 .env 文件加载环境变量（不覆盖已有值）"""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_deepseek_config() -> dict:
    """获取 DeepSeek 配置，返回 {api_key, base_url, model}"""
    _load_env_file()
    return {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    }


def call_deepseek(
    candidate_name: str,
    candidate_message: str,
    history: Optional[list[dict]] = None,
    job_context: str = "",
    template_hint: str = "",
) -> tuple[Optional[str], str]:
    """调用 DeepSeek API 生成回复

    参数:
      template_hint: 匹配到的模板文本，作为「建议回复方向」注入 system prompt
    返回: (reply, error_msg)
    """
    cfg = _get_deepseek_config()
    if not cfg["api_key"]:
        return None, "DEEPSEEK_API_KEY not set"

    system = SYSTEM_PROMPT
    if job_context:
        system += f"\n当前招聘的岗位信息: {job_context}"
    if template_hint:
        system += f"\n建议回复方向: {template_hint}"

    messages = [{"role": "system", "content": system}]
    if history:
        for turn in history[-10:]:
            messages.append(turn)
    messages.append({
        "role": "user",
        "content": f"候选人({candidate_name})说：{candidate_message}",
    })

    payload = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 150,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{cfg['base_url']}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
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
    category_templates: list[dict] = None,
    fallback_templates: list[dict] = None,
    job_context: str = "",
    job: dict = None,
) -> str:
    """生成回复 — 模板匹配 + DeepSeek 智能生成

    流程:
      1. 关键词匹配模板（岗位专属 → 类别通用 → 全局兜底）
      2. 命中模板 → 用模板文本作为 DeepSeek 提示词，AI 结合上下文生成回复
      3. DeepSeek 不可用/失败 → 降级返回模板原文
      4. 无模板匹配 → DeepSeek 仅凭岗位上下文生成
      5. 全部失败 → 最终兜底文本

    参数:
      message: 候选人最新消息
      templates: 旧版通用模板（兼容）
      candidate_name: 候选人称呼
      history: 对话历史 [{"role":"assistant","content":"..."}, ...]
      job_templates: 当前岗位专属模板
      category_templates: 当前岗位类别模板 (tech/nontech)
      fallback_templates: 全局兜底模板
      job_context: 岗位描述文本 (传给 DeepSeek)
      job: 岗位字典 (含 salary/location/title 等，用于模板变量替换)
    """
    # 1. 匹配模板 — 三层 fallback
    template_hint = None
    if job_templates:
        template_hint = match_template(message, job_templates, job=job)
    if not template_hint and category_templates:
        template_hint = match_template(message, category_templates, job=job)
    if not template_hint:
        all_fallback = list(fallback_templates or [])
        template_hint = match_template(message, templates, all_fallback, job=job)

    # 2. 尝试 DeepSeek 智能生成（模板作提示词 + 聊天上下文）
    if template_hint or True:  # 总是尝试 DeepSeek（如果已配置）
        reply, err = call_deepseek(
            candidate_name, message, history,
            job_context=job_context,
            template_hint=template_hint or "",
        )
        if reply:
            return reply

    # 3. DeepSeek 不可用 — 降级返回模板原文
    if template_hint:
        return template_hint

    # 4. 最终兜底
    return get_fallback_reply(
        job_templates or [],
        (fallback_templates or []) + templates,
    )


