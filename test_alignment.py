import urllib.request, json, http.cookiejar, os, sys, json, random, time
from datetime import datetime

room_id = int(os.environ.get('TEST_ROOM', '344763580'))
duration = int(os.environ.get('TEST_DURATION', '120'))  # 2 minutes

output_dir = '/tmp/recordings'
os.makedirs(output_dir, exist_ok=True)

def log(msg):
    t = datetime.now().strftime('%H:%M:%S')
    print(f'[{t}] {msg}')
    sys.stdout.flush()

# ========== STEP 1: Playwright open page to get cookies ==========
log('Opening Playwright to get cookies...')
from playwright.sync_api import sync_playwright
pw_context = sync_playwright()
p = pw_context.__enter__()
browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
ctx = browser.new_context(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    viewport={"width": 1280, "height": 720}
)
page = ctx.new_page()
# Extract all cookies before navigation
page.goto(f"https://live.douyin.com/{room_id}", wait_until="domcontentloaded", timeout=15000)
time.sleep(5)  # wait for JS hydration to set cookies

all_cookies = ctx.cookies()
log(f'Cookies ({len(all_cookies)}):')
cookie_dict = {}
for c in all_cookies:
    log(f'  {c["name"]} = {c["value"][:40]}')
    cookie_dict[c['name']] = c['value']

# Extract cookies string for HTTP
cookie_str = '; '.join([f'{c["name"]}={c["value"]}' for c in all_cookies])

# ========== STEP 2: Try room/web/enter API ==========
log('\nTesting room/web/enter API...')
try:
    api_url = f'https://live.douyin.com/webcast/room/web/enter/?room_id={room_id}&app_id=1128'
    req = urllib.request.Request(api_url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Cookie': cookie_str,
        'Accept': 'application/json',
    })
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    status = data.get('status_code', -1)
    log(f'API status: {status}')
    if status == 0:
        room = data.get('data', {}).get('room', {})
        room_info = data.get('data', {}).get('room_info', {})
        vc = room.get('viewer_count') or room_info.get('viewer_count') or \
             room.get('live_info', {}).get('viewer_count') or \
             data.get('data', {}).get('viewer_count')
        log(f'  viewer_count = {vc}')
        log(f'  Room keys: {list(room.keys())[:10] if room else "none"}')
        if room_info:
            log(f'  Room_info keys: {list(room_info.keys())[:10]}')
    else:
        log(f'  msg: {data.get("status_msg")}')
except Exception as e:
    log(f'  Error: {e}')

# ========== STEP 3: Parallel collection ==========
# Keep PW open and collect both via PW + HTTP simultaneously
log(f'\nStarting parallel collection for {duration}s...')

pw_data = {'viewer_counts': [], 'danmaku': [], 'pw_start': time.time()}
http_data = {'viewer_counts': [], 'http_start': time.time()}
seen_texts = set()
start_ts = time.time()

while time.time() - start_ts < duration:
    now = time.time()
    offset = round(now - pw_data['pw_start'], 1)
    wall_ts = round(now, 1)

    # -- PW collection --
    try:
        # Viewer count via evaluate
        vc_pw = page.evaluate('''() => {
            var el = document.querySelector("[data-e2e=live-room-audience]");
            if(el) return el.textContent.trim();
            return null;
        }''')
        if vc_pw:
            vc_pw = str(vc_pw).replace(',', '')
            if '万' in vc_pw:
                vc_pw = int(float(vc_pw.replace('万', '')) * 10000)
            else:
                vc_pw = int(vc_pw)
            pw_data['viewer_counts'].append({'count': vc_pw, 'offset': offset, 'wall_ts': wall_ts})
        
        # Danmaku via evaluate
        texts = page.evaluate('''() => {
            var el = document.querySelector("[class*=chatroom]");
            if(!el) return [];
            var divs = el.querySelectorAll(":scope > div");
            var r = [];
            for(var d of divs){
                var t = d.textContent.trim();
                if(t && t.indexOf("：") >= 0) r.push(t);
            }
            return r;
        }''')
        for text in (texts or []):
            if text and text not in seen_texts and '：' in text:
                pw_data['danmaku'].append({'text': text[:80], 'offset': offset, 'wall_ts': wall_ts})
                seen_texts.add(text)
    except Exception as e:
        pass

    # -- HTTP collection --
    try:
        api_url = f'https://live.douyin.com/webcast/room/web/enter/?room_id={room_id}&app_id=1128'
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Cookie': cookie_str,
            'Accept': 'application/json',
        })
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        if data.get('status_code') == 0:
            room = data.get('data', {}).get('room', {})
            vc = room.get('viewer_count') or data.get('data', {}).get('viewer_count') or 0
            if vc:
                http_data['viewer_counts'].append({'count': vc, 'wall_ts': wall_ts})
    except Exception:
        pass

    time.sleep(random.uniform(0.8, 1.2))

# Save results
result = {
    'room_id': room_id,
    'duration': duration,
    'cookies': {k: v[:20] for k, v in cookie_dict.items()},
    'pw': pw_data,
    'http': http_data,
}
out_path = os.path.join(output_dir, f'test_alignment_{room_id}_{int(start_ts)}.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

log(f'\nResults saved to {out_path}')
log(f'PW: {len(pw_data["viewer_counts"])} vc, {len(pw_data["danmaku"])} dm')
log(f'HTTP: {len(http_data["viewer_counts"])} vc')

# Print comparison
if pw_data['viewer_counts'] and http_data['viewer_counts']:
    log('\n=== PW vs HTTP comparison (first 10) ===')
    for i in range(min(10, len(pw_data['viewer_counts']), len(http_data['viewer_counts']))):
        p = pw_data['viewer_counts'][i]
        h = http_data['viewer_counts'][i]
        log(f'  PW: count={p["count"]} wall_ts={p["wall_ts"]} | HTTP: count={h["count"]} wall_ts={h["wall_ts"]} {"✓" if p["count"]==h["count"] else "✗"}')

browser.close()
pw_context.__exit__(None, None, None)
log('Done')
