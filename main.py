"""
主入口脚本
从 Confluence 爬取最近更新的 3 条页面，并同步到 Weknora 知识库
"""

import json
import sys
from confluence_crawler import crawl_recent_pages
from weknora_client import sync_pages_to_weknora


def main():
    print("=" * 60)
    print("  Confluence -> Weknora 自动同步工具")
    print("=" * 60)
    print()

    # 第一步：爬取 Confluence 最近更新的 3 条页面
    try:
        pages = crawl_recent_pages(limit=3)
    except Exception as e:
        print(f"[ERROR] 爬取 Confluence 失败: {e}")
        sys.exit(1)

    if not pages:
        print("[INFO] 未找到任何更新的页面，退出。")
        return

    # 打印爬取结果摘要
    print("-" * 60)
    print(f"  爬取完成，共 {len(pages)} 个页面:")
    for p in pages:
        print(f"    - {p['title']} (v{p['version']}, 修改时间: {p['last_modified']})")
        if p["attachments"]:
            print(f"      附件: {len(p['attachments'])} 个")
    print("-" * 60)
    print()

    # 第二步：同步到 Weknora
    try:
        results = sync_pages_to_weknora(pages)
    except Exception as e:
        print(f"[ERROR] 同步到 Weknora 失败: {e}")
        sys.exit(1)

    # 打印同步结果摘要
    success = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] == "failed")
    print("=" * 60)
    print(f"  同步完成: 成功 {success} 项, 失败 {failed} 项")
    print("=" * 60)

    # 保存结果到 JSON
    output = {"pages": pages, "sync_results": results}
    with open("sync_result.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 详细结果已保存到 sync_result.json")


if __name__ == "__main__":
    main()
