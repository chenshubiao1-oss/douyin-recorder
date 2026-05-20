"""Debug: page content on runner"""
import sys, json, os, time, urllib.request
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

    # Capture ALL responses
    responses = []

    def on_response(response):
        responses.append({
            'url': response.url[:120],
            'status': response.status,
        })

    page.on('response', on_response)

    page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)
    
    # Wait longer
    time.sleep(12)

    # Check final URL
    print('Final URL:', page.url)
    print('Title:', page.title())
    print()

    # Show all responses
    print(f'All responses ({len(responses)}):')
    live_responses = [r for r in responses if 'live.douyin.com' in r['url']]
    for r in live_responses[:20]:
        print(f'  [{r["status"]}] {r["url"][:100]}')

    # Get page HTML first 2000 chars
    html = page.content()
    print(f'\nHTML length: {len(html)}')
    print(f'First 1000 chars:\n{html[:1000]}')

    # Check for keywords
    keywords = ['验证', 'captcha', 'login', '登录', 'blocked', 'access denied', 'robot', 'verify']
    for kw in keywords:
        if kw.lower() in html.lower():
            print(f'\nFOUND keyword: {kw}')

    # Get cookies
    cookies = ctx.cookies()
    print(f'\nCookies: {len(cookies)}')
    for c in cookies:
        print(f'  {c["name"]}')

    # Check HTML for room/web/enter
    if 'room/web/enter' in html:
        print('\nroom/web/enter URL found in HTML!')
        idx = html.find('room/web/enter')
        print(html[idx:idx+200])
    else:
        print('\nroom/web/enter NOT in HTML')

    browser.close()
