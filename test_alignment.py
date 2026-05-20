"""Hybrid approach test - debug why cookies/URL missing"""
import sys, json, os, time, urllib.request
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '168465302284')
duration = int(os.environ.get('TEST_DURATION', '120'))

print(f'Room: {room_id}')
print()

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

    # Track ALL requests
    requests_seen = set()
    def on_request(request):
        url = request.url
        requests_seen.add(url)
    page.on('request', on_request)

    # Navigate
    page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)
    
    # Wait for page to settle
    time.sleep(8)

    # Get cookies
    cookies = ctx.cookies()
    print(f'Cookies: {len(cookies)}')
    for c in cookies:
        print(f'  {c["name"]} = {c["value"][:30]}')

    # Show all webcast API requests made
    webcast_urls = [u for u in requests_seen if 'webcast' in u]
    print(f'\nWebcast requests: {len(webcast_urls)}')
    enter_urls = [u for u in webcast_urls if 'room/web/enter' in u]
    if enter_urls:
        print(f'room/web/enter found: {len(enter_urls)}')
        api_url = list(enter_urls)[0]
        print(f'  URL length: {len(api_url)}')
    else:
        print('NO room/web/enter request found!')
        # Show all webcast URLs to debug
        for u in webcast_urls[:10]:
            print(f'  {u[:120]}')

    # Check page title/status
    try:
        title = page.title()
        print(f'\nPage title: {title}')
    except:
        pass

    try:
        url = page.url
        print(f'Final URL: {url}')
    except:
        pass

    # Check if live indicator exists
    try:
        has_audience = page.evaluate('''() => {
            var el = document.querySelector("[data-e2e=live-room-audience]");
            return el ? el.textContent : 'N/A';
        }''')
        print(f'DOM audience: {has_audience}')
    except Exception as e:
        print(f'DOM audience error: {e}')

    if api_url:
        cookie_str = '; '.join([c['name'] + '=' + c['value'] for c in cookies])
        from urllib.parse import urlparse, parse_qs, urlencode
        parsed = urlparse(api_url)
        params = parse_qs(parsed.query)
        print(f'\nAPI URL params: {len(params)}')
        print(f'a_bogus present: {"a_bogus" in params}')
        
        # Clean URL
        params.pop('a_bogus', None)
        flat = {}
        for k, v in params.items():
            flat[k] = v[0]
        scheme = parsed.scheme.decode() if isinstance(parsed.scheme, bytes) else parsed.scheme
        netloc = parsed.netloc.decode() if isinstance(parsed.netloc, bytes) else parsed.netloc
        path = parsed.path.decode() if isinstance(parsed.path, bytes) else parsed.path
        api_url = scheme + '://' + netloc + path + '?' + urlencode(flat)

    browser.close()

if not api_url:
    print('\nCannot proceed: room/web/enter URL not captured')
    sys.exit(1)

# ===== STEP 2: HTTP polling =====
print(f'\nHTTP polling for {duration}s...')

http_data = {'viewer_counts': []}
start = time.time()
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

    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        if data.get('status_code') == 0:
            d0 = data.get('data', {}).get('data', [{}])[0]
            exact_vc = d0.get('stats', {}).get('user_count_str', None)
            display_vc = d0.get('user_count_str', None)
            if exact_vc:
                http_data['viewer_counts'].append({
                    'count': exact_vc,
                    'display': display_vc,
                    'wall_ts': now,
                    'offset': offset,
                })
                print(f'  round {round_num:>3}: vc={exact_vc} ({display_vc})')
            else:
                print(f'  round {round_num:>3}: no vc field, keys={list(d0.keys())[:8]}')
        else:
            print(f'  round {round_num:>3}: API fail status={data.get("status_code")}')
    except Exception as e:
        print(f'  round {round_num:>3}: err={str(e)[:60]}')

    time.sleep(3.0)

# Results
print()
print('=== RESULTS ===')
print(f'Total rounds: {round_num}')
print(f'Successful: {len(http_data["viewer_counts"])}')
if http_data['viewer_counts']:
    vcs = [x['count'] for x in http_data['viewer_counts']]
    print(f'VC values seen: {len(set(vcs))}')
    print(f'VC range: {min(vcs)} - {max(vcs)}')
    print(f'Samples: {vcs[:10]}')
