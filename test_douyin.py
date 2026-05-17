import urllib.request as u
import sys

room_id = sys.argv[1] if len(sys.argv) > 1 else '30972107798'
ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
hdrs = {
    'User-Agent': ua,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}
req = u.Request(f'https://live.douyin.com/{room_id}', headers=hdrs)
resp = u.urlopen(req, timeout=30)
raw = resp.read()
html = raw.decode('utf-8', errors='replace')
print(f'Status: {resp.status}  Size: {len(html)}')
print(f'flv_pull_url: {'flv_pull_url' in html}')
print(f'web_stream_url: {'web_stream_url' in html}')
idx = html.find('web_stream_url')
if idx >= 0:
    print(f'web_stream_url snippet: {repr(html[idx:idx+60])}')
print(f'Server: {resp.headers.get("Server", "?")}')
print(f'Set-Cookie: {resp.headers.get("Set-Cookie", "none")[:60]}')
# Also print first status code check
print(f'data-cluster present: {'data-cluster' in html}')
cluster_idx = html.find('data-cluster')
if cluster_idx >= 0:
    print(f'data-cluster value: {html[cluster_idx:cluster_idx+30]}')
