"""app/bole_agent.py — 「伯乐」对话 agent（DeepSeek 驱动）

产品化内核：桌面 App 的「问伯乐」对话框、CLI（scripts/bole.py）都走这里。
人设来自 IDENTITY/SOUL/AGENTS.md + references/faq.md，用 .env 的 DEEPSEEK_API_KEY。
零第三方依赖（纯 urllib）；未配 key 时返回明确错误（不静默）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent

# 人设三件套 + 应对手册（面向用户时的行为层）
_PERSONA_FILES = ["IDENTITY.md", "SOUL.md", "AGENTS.md", "references/faq.md"]

_TIMEOUT = 60
_MAX_TOKENS = 800
_TEMPERATURE = 0.6
_RETRIES = 2

# 让伯乐知道自己能驱动哪些产品能力（默认参数即「最佳」）
_CAPABILITIES = """# === 你能真实操作本系统（通过工具调用，不是嘴上说说）===
你不是只会聊天的机器人，而是**能直接干活**的招聘助手。用户要数据或操作时，**务必调用工具拿真实结果，绝不编造**：
- 问「谁最合适 / 评分榜 / 推荐谁 / 看看候选人」→ 调 **top_candidates** 取真实评分排名，再基于返回的真人真分回答。
- 问「整体情况 / 招了多少人 / 进度 / 数据」→ 调 **get_dashboard**。
- 提到某个具体人名 → 调 **find_candidate**。
- 要「打招呼 / 收简历 / 智能沟通 / 全流程」→ 调 **run_task**（task=greet/collect/chat/pipeline）。
  **默认 dry_run=true 先预览**，把要做的事讲清楚并请用户确认；用户明确说「真跑/开始/别预览」才 dry_run=false。
- 要「约面试」→ 调 **schedule_interview**（同样默认先预览确认）。
- 已启动任务想看进度 → 调 **job_status**（用 run_task 返回的 job_id）。

原则：**能查真实数据就先查再答，不要凭空说**；**动作类操作（打招呼/收简历/沟通/约面试）默认先预览+请用户确认**，
除非用户明确要真执行。前置缺失（未登录/没岗位/没话术）先引导配好。

回答面向不懂技术的 HR：**简短、用结果说话、每次给下一步**；不外露脚本名/参数等技术细节。
"""

_persona_cache: Optional[str] = None


def _cfg() -> dict:
    """复用 chat_reply 的 .env 加载与 DeepSeek 配置。"""
    from app.chat_reply import _get_deepseek_config
    return _get_deepseek_config()


def load_persona() -> str:
    """拼出伯乐 system prompt（人设三件套 + 应对手册 + 能力清单），带缓存。"""
    global _persona_cache
    if _persona_cache is not None:
        return _persona_cache
    parts = []
    for rel in _PERSONA_FILES:
        p = ROOT / rel
        if not p.exists():
            continue
        try:
            parts.append(f"# ===== {rel} =====\n{p.read_text(encoding='utf-8')}")
        except Exception as e:  # noqa: BLE001 单个文件坏了不拖垮整个人设加载
            print(f"[bole] 跳过人设文件 {rel}: {e}", file=sys.stderr)
    parts.append(_CAPABILITIES)
    _persona_cache = "\n\n".join(parts)
    return _persona_cache


def _request(cfg: dict, payload: dict) -> tuple[Optional[dict], str]:
    """向 DeepSeek 发一次请求，带退避重试。返回 (完整响应 message, 错误)。"""
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
    )
    last = ""
    for attempt in range(_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"], ""
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            last = f"HTTP {e.code} {body}".strip()
            if e.code != 429 and 400 <= e.code < 500:
                return None, last
        except Exception as e:  # noqa: BLE001 网络层统一重试
            last = str(e)
        if attempt < _RETRIES:
            time.sleep(1.5 * (2 ** attempt))
    return None, last


def chat(messages: list[dict], system: Optional[str] = None,
         max_tokens: int = _MAX_TOKENS, temperature: float = _TEMPERATURE
         ) -> tuple[Optional[str], str]:
    """多轮对话（无工具）。messages=[{role, content}]。返回 (回复, 错误)。"""
    cfg = _cfg()
    if not cfg["api_key"]:
        return None, "DEEPSEEK_API_KEY 未配置（cp .env.example .env 并填入）"
    sys_prompt = system if system is not None else load_persona()
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": sys_prompt}, *messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    msg, err = _request(cfg, payload)
    if not msg:
        return None, err
    return (msg.get("content") or "").strip(), ""


def run_agent(messages: list[dict], tools: list[dict], execute,
              system: Optional[str] = None, max_rounds: int = 5
              ) -> tuple[Optional[str], list[dict], str]:
    """带工具调用的 agent 循环。

    tools = OpenAI 风格工具 schema 列表；execute(name, args) -> str 真正执行工具并返回结果文本。
    模型可多轮调用工具（读真实数据 / 跑真实脚本），拿到结果后继续，直到给出最终答复。
    返回 (最终回复, 已执行动作列表, 错误)。actions=[{name, args}]。
    """
    cfg = _cfg()
    if not cfg["api_key"]:
        return None, [], "DEEPSEEK_API_KEY 未配置（cp .env.example .env 并填入）"
    sys_prompt = system if system is not None else load_persona()
    msgs: list[dict] = [{"role": "system", "content": sys_prompt}, *messages]
    actions: list[dict] = []

    for _ in range(max_rounds):
        payload = {
            "model": cfg["model"], "messages": msgs,
            "max_tokens": _MAX_TOKENS, "temperature": _TEMPERATURE,
            "tools": tools, "tool_choice": "auto",
        }
        msg, err = _request(cfg, payload)
        if not msg:
            return None, actions, err
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return (msg.get("content") or "").strip(), actions, ""
        # 记录并执行本轮所有工具调用
        msgs.append(msg)
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:  # noqa: BLE001
                args = {}
            try:
                result = execute(name, args)
            except Exception as e:  # noqa: BLE001 工具异常不崩整轮
                result = f"工具执行失败: {e}"
            actions.append({"name": name, "args": args})
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": str(result)[:3000]})
    return "（这个需求有点复杂，我已执行了部分操作。你再具体说下要什么？）", actions, ""


def ask(question: str, history: Optional[list[dict]] = None
        ) -> tuple[Optional[str], str]:
    """单轮便捷封装（可带历史）。"""
    msgs = list(history or []) + [{"role": "user", "content": question}]
    return chat(msgs)


_SUGGEST_SYSTEM = (
    "你是招聘助手对话的『快捷回复』生成器。站在 HR（用户）角度，"
    "针对伯乐刚说的话，给出 2-4 个最可能的下一步简短回复。"
    "要求：每个 ≤10 字、口语化、可直接点发；紧扣伯乐的话（他若在问选择/确认，就给对应选项）。"
    "只输出 JSON 字符串数组，例如 [\"好，开始吧\",\"先看简历\"]，不要任何多余文字或解释。"
)


def suggest_replies(history: Optional[list[dict]], reply: str) -> list[str]:
    """根据对话与伯乐最新回复，动态生成 HR 可能点的快捷回复。失败/未配置返回 []。"""
    if not reply or not _cfg().get("api_key"):
        return []
    ctx = ""
    for m in (history or [])[-4:]:
        role = "用户" if m.get("role") == "user" else "伯乐"
        ctx += f"{role}：{str(m.get('content', ''))[:120]}\n"
    prompt = f"对话上文：\n{ctx}\n伯乐最新回复：{reply[:600]}\n\n给出快捷回复 JSON 数组："
    text, _ = chat([{"role": "user", "content": prompt}], system=_SUGGEST_SYSTEM,
                   max_tokens=120, temperature=0.5)
    if not text:
        return []
    try:
        import re
        m = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(m.group(0)) if m else []
        out = [str(x).strip()[:16] for x in arr if str(x).strip()]
        return out[:4]
    except Exception:  # noqa: BLE001 生成失败就不给动态建议，前端有兜底
        return []
