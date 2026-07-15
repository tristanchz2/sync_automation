"""
主入口脚本
用法:
  python main.py -p <页面ID或URL>    # 爬取指定页面
"""

import sys
import argparse
from confluence_crawler import download_single_page


def parse_args():
    parser = argparse.ArgumentParser(
        description="Confluence 爬虫 - 导出 PDF 并下载附件"
    )
    parser.add_argument(
        "-p", "--page",
        type=str,
        required=True,
        help="指定页面（页面 ID 或 URL）"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("  Confluence 爬虫工具")
    print("  - 导出页面为 PDF")
    print("  - 下载页面中的附件（PPT/Excel/Word 等）")
    print("  - 下载页面中链接的其他页面")
    print("=" * 60)
    print()

    try:
        print(f"[页面] {args.page}\n")
        results = download_single_page(args.page)
    except Exception as e:
        print(f"[ERROR] 爬取失败: {e}")
        sys.exit(1)

    if not results:
        print("[INFO] 未获取到任何页面，退出。")
        return


if __name__ == "__main__":
    main()
