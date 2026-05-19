import asyncio, os, re, time, json, subprocess, urllib.request

ROOM_URL = os.environ.get("ROOM_URL", "https://live.douyin.com/852733918841")
DURATION_MIN = int(os.environ.get("DURATION_MIN", "5"))
ROOM_SHORT = ROOM_URL.rstrip("/").split("/")[-1]
print(f"Room: {ROOM_SHORT}, Duration: {DURATION_MIN}min")

from playwright.async_api import async_playwright

# "在线观众" in UTF-8 bytes
ZH_ONLINE_BYTES = b"\xe5\x9c\xa8\xe7\xba\xbf\xe8\xa7\x82\xe4\xbc\x97"
VIEWER_RE = re.compile(rb"(\d+)" + ZH_ONLINE_BYTES)


async def get_tokens():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        fetch_url = None

        async def on_resp(response):
            nonlocal fetch_url
            if "/im/fetch/" in response.url:
                fetch_url = response.url

        page.on("response", on_resp)

        await page.goto(ROOM_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        stream_url = await page.evaluate(
            """() => {
            const d = localStorage.getItem("live_debug_info");
            if (d) { try { return JSON.parse(d).src; } catch(e) {} }
            return null;
        }"""
        )

        cookies = await context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        await browser.close()
        return fetch_url, cookie_str, stream_url


async def main():
    print("[1/3] Getting tokens...")
    try:
        fetch_url, cookie_str, stream_url = await get_tokens()
    except Exception as e:
        print(f"Failed: {e}")
        return

    if not fetch_url:
        print("ERROR: No im/fetch URL")
        return

    print(f"Stream: {len(stream_url or '')} chars")
    print(f"Fetch URL: {len(fetch_url)} chars")

    # Start recording
    out = f"{ROOM_SHORT}_test.flv"
    ffmpeg = None
    if stream_url:
        print(f"\n[2/3] Recording {DURATION_MIN}min...")
        ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-headers",
                f"Referer: {ROOM_URL}\r\nUser-Agent: Mozilla/5.0",
                "-i",
                stream_url,
                "-t",
                str(DURATION_MIN * 60),
                "-c",
                "copy",
                out,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # Poll
    print(f"\n[3/3] Polling every 5s...")
    points = []
    t0 = time.time()
    end = t0 + DURATION_MIN * 60
    n = 0

    while time.time() < end:
        try:
            req = urllib.request.Request(fetch_url)
            req.add_header("Cookie", cookie_str)
            req.add_header("User-Agent", "Mozilla/5.0")
            req.add_header("Referer", ROOM_URL)
            r = urllib.request.urlopen(req, timeout=10)
            body = r.read()

            m = VIEWER_RE.search(body)
            if m:
                c = int(m.group(1))
                now = time.time()
                points.append({"ts": now, "count": c, "offset": round(now - t0, 1)})
                n += 1
                if n % 6 == 0:
                    print(f"  [{n}] Count: {c} @ {now-t0:.0f}s")
        except Exception as e:
            if n % 12 == 0:
                print(f"  Err: {type(e).__name__}")

        await asyncio.sleep(5)

    elapsed = time.time() - t0
    print(f"\nDone! {elapsed:.0f}s, {n} polls")

    if ffmpeg:
        ffmpeg.terminate()
        ffmpeg.wait()
        sz = os.path.getsize(out) if os.path.exists(out) else 0
        print(f"File: {out} ({sz} bytes)")

    with open("viewer_count.json", "w") as f:
        json.dump(
            {"room": ROOM_URL, "duration": elapsed, "points": points}, f, indent=2
        )
    print(f"Data: {len(points)} points")

    if points:
        vals = [p["count"] for p in points]
        print(
            f"Min: {min(vals)}, Max: {max(vals)}, Avg: {sum(vals)/len(vals):.0f}"
        )


asyncio.run(main())
