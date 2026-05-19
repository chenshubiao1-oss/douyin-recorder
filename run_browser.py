import asyncio, os, time
from playwright.async_api import async_playwright

ROOM_URL = "https://live.douyin.com/48383706721"
BID = os.environ.get("BROWSER_ID", "?")

async def main():
    print(f"[Browser {BID}] Starting...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        await page.goto(ROOM_URL, wait_until="load", timeout=30000)
        await asyncio.sleep(3)
        print(f"[Browser {BID}] Holding for 120s...")
        for i in range(12):
            await asyncio.sleep(10)
            print(f"[Browser {BID}] elapsed: {(i+1)*10}s")
        await browser.close()
        print(f"[Browser {BID}] Done")
asyncio.run(main())
