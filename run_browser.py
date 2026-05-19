import asyncio, os, time, urllib.request

ROOM_URL = os.environ.get('ROOM_URL', 'https://live.douyin.com/919096107345')
BID = os.environ.get('BROWSER_ID', '?')

# Print public IP
try:
    ip_req = urllib.request.Request('https://api.ipify.org?format=text', headers={'User-Agent': 'curl/7.0'})
    ip = urllib.request.urlopen(ip_req, timeout=10).read().decode().strip()
    print(f'[Browser {BID}] PUBLIC IP: {ip}')
except Exception as e:
    print(f'[Browser {BID}] IP lookup failed: {e}')

from playwright.async_api import async_playwright

async def main():
    print(f'[Browser {BID}] Starting...')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        print(f'[Browser {BID}] Navigating...')
        await page.goto(ROOM_URL, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(5)
        print(f'[Browser {BID}] Loaded. Holding 120s...')
        t0 = time.time()
        for i in range(12):
            await asyncio.sleep(10)
            print(f'[Browser {BID}] elapsed {(i+1)*10}s')
            import sys; sys.stdout.flush()
        await browser.close()
        print(f'[Browser {BID}] Done')

asyncio.run(main())
