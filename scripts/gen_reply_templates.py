#!/usr/bin/env python3
"""
根据 jobs.json 中的岗位信息，调用 DeepSeek 自动生成专属话术模板

用法:
  # 为指定岗位生成模板（预览）
  python scripts/gen_reply_templates.py --job-id dev

  # 生成并直接写入 reply.json
  python scripts/gen_reply_templates.py --job-id dev --write

  # 为所有岗位生成
  python scripts/gen_reply_templates.py --all --write
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.chat_reply import _get_deepseek_config

JOBS_PATH = Path(__file__).parent.parent / "config" / "jobs.json"
REPLY_PATH = Path(__file__).parent.parent / "config" / "reply.json"

PROMPT = """你是BOSS直聘的顶尖招聘专家。根据以下岗位信息，生成8-12条专属话术模板。

## 岗位信息
- 岗位名: {title}
- 薪资范围: {salary}
- 学历要求: {degree}
- 工作地点: {location}
- 职位描述:
{requirements}

## 公司背景
AI赛道创业公司，硅碳共治的分布式智能组织，运营着数十个AI Agent。产品已上线有付费客户。
团队小而精，扁平直接，广州天河智慧城办公。

## 模板要求
每条模板覆盖一个常见对话场景，包含:
- id: 岗位前缀_scene，如 dev_salary
- name: 场景名（中文，短）
- match_keywords: 3-8个匹配关键词
- reply: 1-3句话术，以问题或引导结尾，体现公司特色
- priority: 1-10 数字越小越优先

## 必须覆盖的场景
1. 打招呼/问候
2. 问薪资
3. 问工作内容/技术栈
4. 问工作经验/背景
5. 问面试流程
6. 问团队规模/氛围
7. 问福利/五险一金
8. 问加班/工作节奏
9. 问晋升/发展
10. 问项目/产品方向

## 输出格式
只输出 JSON 数组，不要其他文字:
[{{"id":"...","name":"...","match_keywords":[...],"reply":"...","priority":1}},...]"""


def gen_templates_for_job(job: dict) -> list[dict]:
    """调用 DeepSeek 为一个岗位生成话术模板"""
    cfg = _get_deepseek_config()
    if not cfg["api_key"]:
        print("❌ DeepSeek API 未配置，无法生成")
        return []

    prompt = PROMPT.format(
        title=job.get("title", ""),
        salary=job.get("salary", ""),
        degree=job.get("degree", ""),
        location=job.get("location", ""),
        requirements=job.get("requirements", "")[:2000],
    )

    payload = json.dumps({
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是招聘话术专家。只输出 JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.8,
        "max_tokens": 3000,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            # 清理 markdown 代码块包裹
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[:-3]
            return json.loads(text)
    except Exception as e:
        print(f"❌ DeepSeek 调用失败: {e}")
        return []


def main():
    import argparse
    p = argparse.ArgumentParser(description="根据岗位信息生成专属话术模板")
    p.add_argument("--job-id", help="目标岗位名(即岗位 title)")
    p.add_argument("--all", action="store_true", help="为所有岗位生成")
    p.add_argument("--write", action="store_true", help="写入 reply.json")
    args = p.parse_args()

    if not args.job_id and not args.all:
        p.print_help()
        sys.exit(1)

    # 加载岗位和现有话术
    jobs_data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    reply_data = json.loads(REPLY_PATH.read_text(encoding="utf-8")) if REPLY_PATH.exists() else {"jobs": {}, "categories": {}, "fallback": []}

    targets = jobs_data["jobs"] if args.all else [j for j in jobs_data["jobs"] if j["title"] == args.job_id]
    if not targets:
        print(f"❌ 找不到岗位: {args.job_id}")
        sys.exit(1)

    for job in targets:
        jid = job["title"]  # 岗位名即唯一键
        print(f"\n{'='*60}")
        print(f"生成: {job['title']} ({jid})")
        print(f"{'='*60}")

        templates = gen_templates_for_job(job)
        if not templates:
            print("  ❌ 生成失败")
            continue

        print(f"  ✓ 生成 {len(templates)} 条模板:")
        for t in templates:
            print(f"    [{t['id']}] {t['name']}")
            print(f"      关键词: {t.get('match_keywords',[])}")
            print(f"      话术: {t['reply'][:80]}...")

        if args.write:
            reply_data["jobs"][jid] = templates
            print(f"  ✓ 已写入 reply.json jobs.{jid}")

    if args.write:
        REPLY_PATH.write_text(json.dumps(reply_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ 已保存 {REPLY_PATH}")


if __name__ == "__main__":
    main()
