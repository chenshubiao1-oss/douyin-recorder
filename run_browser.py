import asyncio, os, time
from playwright.async_api import async_playwright

ROOM_URL = os.environ.get('ROOM_URL', 'https://live.douyin.com/919096107345')
BID = os.environ.get('BROWSER_ID', '?')

async def main():
    print(f'[Browser {BID}] Starting...')
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        print(f'[Browser {BID}] Navigating...')
        await page.goto(ROOM_URL, wait_until='domcontentloaded', timeout=60000)
        print(f'[Browser {BID}] Loaded. Holding 120s...')
        t0 = time.time()
        for i in range(12):
            await asyncio.sleep(10)
            print(f'[Browser {BID}] elapsed {(i+1)*10}s')
        await browser.close()
        print(f'[Browser {BID}] Done')
asyncio.run(main())
