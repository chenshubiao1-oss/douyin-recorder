"""Hybrid approach: extract URL params from SSR HTML (no hydration needed)"""
import sys, json, os, time, urllib.request, re
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '344763580')
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

    # Get page HTML
    html = page.content()
    browser.close()

print(f'HTML size: {len(html)}')
print()

# ===== Extract room/web/enter URL from SSR HTML =====
# Find the SSR data that contains room/web/enter params
# The page SSR has all API URLs in JavaScript data

# Strategy: look for /webcast/room/web/enter/ in HTML and extract query params from context
# Method 1: Find the full URL in the SSR payload
enter_ctx = ''

# Look for the SSR state with all params
# The SSR data is embedded as JSON in <script> tags
# Search for room/web/enter URL patterns
matches = list(re.finditer(r'/webcast/room/web/enter/[^"\'&]+', html))
if matches:
    for m in matches[:3]:
        ctx_start = max(0, m.start() - 50)
        ctx_end = min(len(html), m.end() + 200)
        chunk = html[ctx_start:ctx_end]
        print(f'Found context:\n...{chunk[:300]}...')
        print()
        enter_ctx = chunk

# Method 2: Try to find the SSR JSON state 
# The SSR often has a large JSON object with all initial data
# Look for patterns like "room_id": or "aid": in the HTML near /webcast/room/web/enter/
print('Looking for direct params...')

# Method 3: Build the URL from known params
# From local testing: room/web/enter needs: aid, app_name, live_id, device_platform, language, 
# enter_from, cookie_enabled, screen_width, screen_height, browser_language, browser_platform,
# browser_name, browser_version, os_name, os_version, web_rid, room_id_str
# plus cookies

# Let's try with the EXACT params the local test used but with the runner's cookies
# The key params are: aid=6383, app_name=douyin_web, live_id=1, device_platform=web, room_id_str=INTERNAL_ROOM_ID

# Extract the internal room_id from HTML
# Look for data-room-id or similar
rid_matches = list(re.finditer(r'"room_id_str"\s*:\s*"(\d+)"', html))
if not rid_matches:
    rid_matches = list(re.finditer(r'room_id_str[=:]["\']?(\d+)', html))
if not rid_matches:
    # Try other patterns
    rid_matches = list(re.finditer(r'\\"roomId\\"\s*:\s*"(\d+)"', html))

if rid_matches:
    internal_rid = rid_matches[0].group(1)
    print(f'Internal room_id: {internal_rid}')
else:
    internal_rid = room_id
    print(f'Using web room_id: {room_id}')

# Extract a_bogus from HTML if present
bogus_matches = list(re.finditer(r'a_bogus["\']?\s*[:=]\s*["\']([^"\'&]+)', html))
if bogus_matches:
    print(f'a_bogus found in HTML: {bogus_matches[0].group(1)[:30]}...')

# Build the API URL
# Use the same params as the browser does, with runner's cookies
from urllib.parse import urlencode

params = {
    'aid': '6383',
    'app_name': 'douyin_web',
    'live_id': '1',
    'device_platform': 'web',
    'language': 'zh-CN',
    'enter_from': 'link_share',
    'cookie_enabled': 'true',
    'screen_width': '1280',
    'screen_height': '720',
    'browser_language': 'zh-CN',
    'browser_platform': 'Win32',
    'browser_name': 'Chrome',
    'browser_version': '120.0.0.0',
    'os_name': 'Windows',
    'os_version': '10',
    'web_rid': room_id,
    'room_id_str': internal_rid,
    'is_need_double_stream': 'false',
}

api_url = 'https://live.douyin.com/webcast/room/web/enter/?' + urlencode(params)
print(f'\nBuilt API URL length: {len(api_url)}')

# ===== HTTP polling =====
print(f'\nHTTP polling for {duration}s...')

http_data = {'viewer_counts': []}
start = time.time()
headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Cookie': cookie_str,
}

round_num = 0
while time.time() - start < duration:
    round_num += 1
    now = time.time()

    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        if data.get('status_code') == 0:
            d0 = data.get('data', {}).get('data', [{}])[0]
            exact_vc = d0.get('stats', {}).get('user_count_str', None)
            display_vc = d0.get('user_count_str', None)
            if exact_vc is not None:
                http_data['viewer_counts'].append({
                    'count': exact_vc,
                    'display': display_vc,
                    'wall_ts': now,
                })
                print(f'  round {round_num:>3}: vc={exact_vc} ({display_vc})')
            else:
                print(f'  round {round_num:>3}: vc field not found, keys={list(d0.keys())[:8]}')
                # Debug: print top-level API response
                print(f'  resp keys: {list(data.get("data",{}).keys())[:8]}')
        else:
            print(f'  round {round_num:>3}: API fail status={data.get("status_code")}')
            if data.get('status_code') == 10011:
                print('  NOTE: 10011 = auth failure. Cookies may be insufficient.')
    except Exception as e:
        print(f'  round {round_num:>3}: err={str(e)[:60]}')

    time.sleep(3.0)

print()
print('=== RESULTS ===')
print(f'Total rounds: {round_num}')
print(f'Successful: {len(http_data["viewer_counts"])}')
if http_data['viewer_counts']:
    vcs = [x['count'] for x in http_data['viewer_counts']]
    print(f'Unique VC values: {len(set(vcs))}')
    print(f'VC range: {min(vcs)} - {max(vcs)}')
    print(f'Samples: {vcs[:15]}')
