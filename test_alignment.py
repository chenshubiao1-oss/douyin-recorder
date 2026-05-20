"""Hybrid approach test: Playwright once -> HTTP polling for viewer count + danmaku"""
import sys, json, os, time, urllib.request
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '168465302284')
duration = int(os.environ.get('TEST_DURATION', '120'))

print(f'Room: {room_id}')
print(f'Duration: {duration}s')
print()

# ===== STEP 1: Playwright ONCE (5s) =====
print('Step 1: Playwright open page...')
from playwright.sync_api import sync_playwright

api_url = None
cookie_str = None

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 720}
    )
    page = ctx.new_page()

    captured = [None]
    def on_request(request):
        if 'room/web/enter' in request.url and captured[0] is None:
            captured[0] = request.url
    page.on('request', on_request)

    page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)
    time.sleep(5)

    cookies = ctx.cookies()
    cookie_str = '; '.join([c['name'] + '=' + c['value'] for c in cookies])
    print(f'  Cookies: {len(cookies)}')

    # Build clean API URL (remove a_bogus)
    from urllib.parse import urlparse, parse_qs, urlencode
    parsed = urlparse(captured[0])
    params = parse_qs(parsed.query)
    params.pop('a_bogus', None)
    flat = {}
    for k, v in params.items():
        flat[k] = v[0]
    api_url = parsed.scheme + '://' + parsed.netloc + parsed.path + '?' + urlencode(flat)
    print(f'  API URL: params={len(params)}')
    browser.close()

print('Playwright closed.')
print()

# ===== STEP 2: HTTP-only polling =====
print(f'Step 2: HTTP polling for {duration}s...')
http_data = {'viewer_counts': [], 'http_start': time.time()}
start = time.time()
seen_vc = set()
headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Cookie': cookie_str,
}

round_num = 0
while time.time() - start < duration:
    round_num += 1
    now = time.time()
    offset = round(now - start, 1)
    
    # HTTP poll
    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        if data.get('status_code') == 0:
            d0 = data.get('data', {}).get('data', [{}])[0]
            exact_vc = d0.get('stats', {}).get('user_count_str', None)
            display_vc = d0.get('user_count_str', None)
            if exact_vc:
                entry = {
                    'count': exact_vc,
                    'display': display_vc,
                    'wall_ts': now,
                    'offset': offset,
                }
                http_data['viewer_counts'].append(entry)
                print(f'  [{round(time.time()%100,1)}] round {round_num:>3}: vc={exact_vc} ({display_vc})')
                seen_vc.add(exact_vc)
    except Exception as e:
        print(f'  [{round(time.time()%100,1)}] round {round_num:>3}: err={str(e)[:40]}')
    
    time.sleep(3.0)

# ===== Save results =====
output = '/tmp/recordings'
os.makedirs(output, exist_ok=True)
result = {
    'room_id': room_id,
    'duration': duration,
    'rounds': round_num,
    'datapoints': len(http_data['viewer_counts']),
    'unique_vc_values': len(seen_vc),
    'samples': http_data['viewer_counts'][:50],  # save first 50
}
out_path = os.path.join(output, f'hybrid_test_{room_id}_{int(start)}.json')
with open(out_path, 'w') as f:
    json.dump(result, f, indent=1)

print()
print('=== RESULTS ===')
print(f'Total rounds: {round_num}')
print(f'Successful polls: {len(http_data["viewer_counts"])}')
print(f'Unique VC values: {len(seen_vc)}')
print(f'Saved to: {out_path}')

# Show VC changes
if len(http_data['viewer_counts']) >= 2:
    changes = sum(1 for i in range(1, len(http_data['viewer_counts'])) 
                  if http_data['viewer_counts'][i]['count'] != http_data['viewer_counts'][i-1]['count'])
    print(f'VC changes: {changes}')
    print(f'Last 5: {[x["count"] for x in http_data["viewer_counts"][-5:]]}')
