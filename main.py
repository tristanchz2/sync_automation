"""
主入口脚本
从 Confluence 爬取最近更新的 3 条页面，生成 PDF 到根目录
"""

import sys
from confluence_crawler import crawl_and_generate_pdfs


def main():
    print("=" * 60)
    print("  Confluence 爬虫工具 - 生成 PDF")
    print("=" * 60)
    print()

    try:
        pdf_paths = crawl_and_generate_pdfs(limit=3)
    except Exception as e:
        print(f"[ERROR] 爬取失败: {e}")
        sys.exit(1)

    if not pdf_paths:
        print("[INFO] 未生成任何 PDF，退出。")
        return

    print("=" * 60)
    print(f"  完成！共生成 {len(pdf_paths)} 个 PDF:")
    for p in pdf_paths:
        print(f"    - {p}")
    print("=" * 60)


if __name__ == "__main__":
    main()
