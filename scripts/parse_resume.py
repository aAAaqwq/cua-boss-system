#!/usr/bin/env python3
"""parse_resume.py — PDF 简历转文字（主路径解析器，可独立使用 / 批量回填）

主张：简历正文应【从 PDF 附件解析】入库，AX 直接提取只是降级。本脚本就是那个
「PDF 转文字」工具——既能单文件解析，也能扫 data/resumes/ + DB 批量回填正文。
解析引擎复用 app.pdf_util.extract_resume_from_pdf（文本层优先，扫描件自动 OCR）。

用法:
  # 解析单个 PDF，打印正文（不写库）
  python scripts/parse_resume.py data/resumes/张三.pdf

  # 回填：对 DB 中「有 resume_path 但 resume_content 为空/过短」的候选人，解析其 PDF 补正文
  python scripts/parse_resume.py --backfill
  python scripts/parse_resume.py --backfill --dry-run     # 只看会改谁，不写库
  python scripts/parse_resume.py --backfill --min 50      # 正文短于 50 字也视为需回填
"""
import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from app.db import init_db, DB_PATH                       # noqa: E402
from app.pdf_util import extract_resume_from_pdf          # noqa: E402


def parse_one(pdf_path: str, expected_name: str = "") -> tuple[str, str]:
    """解析单个 PDF → (正文, 来源 text|ocr|'')。"""
    return extract_resume_from_pdf(pdf_path, expected_name=expected_name)


def backfill(min_chars: int, dry_run: bool) -> None:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT uid, name, resume_path, resume_content FROM candidates "
        "WHERE resume_path IS NOT NULL AND resume_path != ''"
    ).fetchall()

    need = [r for r in rows
            if len((r["resume_content"] or "")) < min_chars and Path(r["resume_path"]).exists()]
    missing_file = [r for r in rows if not Path(r["resume_path"]).exists()]

    print(f"共 {len(rows)} 行有 PDF 记录；需回填正文 {len(need)} 行"
          f"；PDF 文件已丢失 {len(missing_file)} 行")
    if missing_file:
        for r in missing_file[:10]:
            print(f"  ⚠ 文件丢失: {r['name']} → {r['resume_path']}")

    updated = 0
    for r in need:
        text, src = parse_one(r["resume_path"], expected_name=r["name"] or "")
        if text and len(text) >= min_chars:
            tag = "OCR" if src == "ocr" else "直解析"
            print(f"  ✓ {r['name']:10} | {tag} {len(text)} 字 ← {Path(r['resume_path']).name}")
            if not dry_run:
                conn.execute(
                    "UPDATE candidates SET resume_content=?, has_resume=1 WHERE uid=?",
                    (text, r["uid"]),
                )
                updated += 1
        else:
            print(f"  ⚠ {r['name']:10} | 解析为空/过短({len(text or '')}字) ← {Path(r['resume_path']).name}")

    if not dry_run:
        conn.commit()
    conn.close()
    print(f"\n{'[dry-run] 将回填' if dry_run else '✓ 已回填'} {updated if not dry_run else len(need)} 行")


def main() -> None:
    p = argparse.ArgumentParser(description="PDF 简历转文字 / 批量回填正文")
    p.add_argument("pdf", nargs="?", help="单个 PDF 路径（解析并打印正文，不写库）")
    p.add_argument("--backfill", action="store_true", help="批量回填 DB 中缺正文的 PDF")
    p.add_argument("--min", type=int, default=1, help="正文短于该字数视为需回填（默认 1）")
    p.add_argument("--dry-run", action="store_true", help="回填仅预览，不写库")
    args = p.parse_args()

    if args.backfill:
        backfill(args.min, args.dry_run)
    elif args.pdf:
        if not Path(args.pdf).exists():
            print(f"❌ 文件不存在: {args.pdf}"); sys.exit(1)
        text, src = parse_one(args.pdf)
        tag = {"text": "文本层", "ocr": "OCR", "": "失败"}.get(src, src)
        print(f"── 来源: {tag} | {len(text)} 字 ──\n{text}")
    else:
        p.print_help()


if __name__ == "__main__":
    main()
