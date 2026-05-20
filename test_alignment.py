"""Find exact viewer count in response - dump full structure"""
import sys, json, os, time, urllib.request, re
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '344763580')

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 720}
    )
    page = ctx.new_page()
    page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)
    time.sleep(3)

    cookies = ctx.cookies()
    cookie_str = '; '.join([c['name'] + '=' + c['value'] for c in cookies])

    # Get SSR HTML and extract internal room_id
    html = page.content()
    browser.close()

rid_matches = list(re.finditer(r'"room_id_str"\s*:\s*"(\d+)"', html))
if rid_matches:
    internal_rid = rid_matches[0].group(1)
else:
    internal_rid = room_id

print(f'Internal room_id: {internal_rid}')

# Build API URL
from urllib.parse import urlencode
params = {
    'aid': '6383', 'app_name': 'douyin_web', 'live_id': '1',
    'device_platform': 'web', 'language': 'zh-CN', 'enter_from': 'link_share',
    'cookie_enabled': 'true', 'screen_width': '1280', 'screen_height': '720',
    'browser_language': 'zh-CN', 'browser_platform': 'Win32',
    'browser_name': 'Chrome', 'browser_version': '120.0.0.0',
    'os_name': 'Windows', 'os_version': '10',
    'web_rid': room_id, 'room_id_str': internal_rid,
    'is_need_double_stream': 'false',
}
api_url = 'https://live.douyin.com/webcast/room/web/enter/?' + urlencode(params)

# Call API and dump EVERYTHING
headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Cookie': cookie_str,
}
req = urllib.request.Request(api_url, headers=headers)
resp = urllib.request.urlopen(req, timeout=8)
data = json.loads(resp.read())

d0 = data.get('data', {}).get('data', [{}])[0]

# Print ALL keys of d0
print(f'\nd0 has {len(d0)} keys:')
for k in sorted(d0.keys()):
    v = d0[k]
    vtype = type(v).__name__
    if isinstance(v, (str, int, float, bool, type(None))):
        print(f'  {k}: {v}  [{vtype}]')
    elif isinstance(v, dict):
        print(f'  {k}: dict[{len(v)} keys]')
        for sk in sorted(v.keys())[:5]:
            sv = v[sk]
            print(f'    {sk}: {sv}')
    elif isinstance(v, list):
        print(f'  {k}: list[{len(v)} items]')
    else:
        print(f'  {k}: ({vtype})')

# Also print full data top-level keys
top = data.get('data', {})
print(f'\nTop-level data keys:')
for k in sorted(top.keys()):
    v = top[k]
    print(f'  {k}: ({type(v).__name__})')

# Deep scan for any integer or 'user_count' related field
print(f'\nDeep scan for viewer/user count:')
def deep_scan(d, path='', depth=0):
    if depth > 5: return
    if isinstance(d, dict):
        for k, v in d.items():
            np = f'{path}.{k}'
            if any(x in k.lower() for x in ['count', 'viewer', 'user', 'audience', 'online']):
                print(f'  {np}: {v}')
            deep_scan(v, np, depth+1)
    elif isinstance(d, list) and d and isinstance(d[0], dict):
        deep_scan(d[0], f'{path}[0]', depth+1)

deep_scan(data)
