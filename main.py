"""
主入口脚本
从 Confluence 爬取最近更新的 3 条页面，导出 PDF 并下载附件
"""

import sys
from confluence_crawler import crawl_and_download


def main():
    print("=" * 60)
    print("  Confluence 爬虫工具")
    print("  - 导出页面为 PDF")
    print("  - 下载页面中的附件（PPT/Excel/Word 等）")
    print("=" * 60)
    print()

    try:
        results = crawl_and_download(limit=3)
    except Exception as e:
        print(f"[ERROR] 爬取失败: {e}")
        sys.exit(1)

    if not results:
        print("[INFO] 未获取到任何页面，退出。")
        return


if __name__ == "__main__":
    main()
