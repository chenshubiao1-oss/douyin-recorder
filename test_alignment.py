"""Switch to room 168465302284 (was live earlier)"""
import sys, json, os, time, urllib.request, re
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = '168465302284'
duration = int(os.environ.get('TEST_DURATION', '120'))

from playwright.sync_api import sync_playwright

print(f'Room: {room_id}')
print()

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
    print(f'Cookies: {len(cookies)}')

    html = page.content()
    browser.close()

# Get internal room_id
rid_matches = list(re.finditer(r'"room_id_str"\s*:\s*"(\d+)"', html))
internal_rid = rid_matches[0].group(1) if rid_matches else room_id
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

# First call: dump all data
headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Cookie': cookie_str,
}

print('First API call:')
req = urllib.request.Request(api_url, headers=headers)
resp = urllib.request.urlopen(req, timeout=8)
data = json.loads(resp.read())

d0 = data.get('data', {}).get('data', [{}])[0]
print(f'  status: {data.get("status_code")}')
print(f'  room status: {d0.get("status")}')
print(f'  user_count_str: {d0.get("user_count_str")}')
print(f'  stats exists: {"stats" in d0}')
if 'stats' in d0:
    print(f'  stats.user_count_str: {d0["stats"].get("user_count_str")}')
print(f'  room_view_stats exists: {"room_view_stats" in d0}')
if 'room_view_stats' in d0:
    print(f'  room_view_stats: {d0["room_view_stats"]}')

# If API works, poll
if data.get('status_code') == 0 and d0.get('status') == 2:
    print(f'\nRoom is LIVE! Polling for {duration}s...')
    http_data = []
    start = time.time()
    while time.time() - start < duration:
        try:
            req = urllib.request.Request(api_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=8)
            d = json.loads(resp.read())
            dd = d.get('data', {}).get('data', [{}])[0]
            vc = dd.get('stats', {}).get('user_count_str') or dd.get('user_count_str')
            http_data.append({'vc': vc, 't': time.time()})
            print(f'  vc={vc}')
        except Exception as e:
            print(f'  err={str(e)[:40]}')
        time.sleep(3)
    
    print(f'\nPolled {len(http_data)} times')
else:
    print(f'\nRoom not live (status={d0.get("status")}) or API failed')
