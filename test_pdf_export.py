"""调查 Confluence PDF 导出 API"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import os

load_dotenv()

s = requests.Session()
s.auth = HTTPBasicAuth(os.getenv('CONFLUENCE_USERNAME'), os.getenv('CONFLUENCE_PASSWORD'))
base = os.getenv('CONFLUENCE_BASE_URL').rstrip('/')

# 获取最近一个页面
r = s.get(f'{base}/rest/api/content/search', params={
    'cql': 'type=page ORDER BY lastmodified DESC',
    'limit': 1
})
pages = r.json().get('results', [])
if not pages:
    print("No pages found")
    exit()

pid = pages[0]['id']
title = pages[0]['title']
space_key = pages[0].get('space', {}).get('key', '')
print(f"Page ID: {pid}")
print(f"Title: {title}")
print(f"Space: {space_key}")
print(f"Base URL: {base}")
print()

# 测试各种 PDF 导出 URL
test_urls = [
    ("flyingpdf action", f'{base}/spaces/flyingpdf/pdfpageexport.action?pageId={pid}'),
    ("flyingpdf /wiki", f'{base}/wiki/spaces/flyingpdf/pdfpageexport.action?pageId={pid}'),
    ("exportpdf", f'{base}/exportpdf?pageId={pid}'),
    ("wiki/exportpdf", f'{base}/wiki/exportpdf?pageId={pid}'),
    ("pdfpageexport direct", f'{base}/pdfpageexport.action?pageId={pid}'),
    ("flypdf with space", f'{base}/spaces/flyingpdf/pdfpageexport.action?pageId={pid}&spaceKey={space_key}'),
]

for name, url in test_urls:
    try:
        resp = s.get(url, allow_redirects=False, timeout=10)
        ct = resp.headers.get('Content-Type', '')
        cd = resp.headers.get('Content-Disposition', '')
        status = resp.status_code
        is_pdf = 'pdf' in ct.lower() or 'pdf' in cd.lower()
        print(f"[{status}] {name}")
        print(f"  URL: {url}")
        print(f"  Content-Type: {ct}")
        print(f"  Content-Disposition: {cd}")
        if is_pdf:
            print(f"  >>> PDF FOUND! Size: {len(resp.content)} bytes")
        elif status == 302 or status == 301:
            print(f"  Redirect: {resp.headers.get('Location', '')}")
        print()
    except Exception as e:
        print(f"[ERR] {name}: {e}")
        print()
