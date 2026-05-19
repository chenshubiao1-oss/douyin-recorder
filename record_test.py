import asyncio, os, re, time, json, subprocess, urllib.request

ROOM_URL = os.environ.get("ROOM_URL")
if not ROOM_URL:
    print("ERROR: ROOM_URL not set")
    exit(1)

ROOM_SHORT = ROOM_URL.rstrip("/").split("/")[-1]
DURATION_MIN = int(os.environ.get("DURATION_MIN", "1"))

print(f"Room: {ROOM_SHORT}, Duration: {DURATION_MIN}min")

from playwright.async_api import async_playwright

QUALITY = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}

# Shared data store
class DataStore:
    def __init__(self):
        self.viewer_counts = []
        self.danmaku = []
        self.seen_danmaku = set()


def extract_stream_url(html):
    found = []
    for m in re.finditer(
        r'["\\]+(FULL_HD1|HD1|SD1|SD2)["\\]+\s*[:=]\s*["\\]+(https?://[^"]+)',
        html,
    ):
        url = m.group(2).replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
        if url.startswith("http"):
            found.append((m.group(1), url))
    if found:
        best = max(found, key=lambda x: QUALITY.get(x[0], 0))
        return best[1]
    return None


async def capture_viewer_and_danmaku(page, store):
    """Periodically capture viewer count and danmaku from DOM."""
    while True:
        try:
            now = time.time()

            # Viewer count
            viewer_el = await page.query_selector('[data-e2e="live-room-audience"]')
            if viewer_el:
                txt = await viewer_el.text_content()
                if txt and txt.strip().isdigit():
                    store.viewer_counts.append({
                        "ts": now,
                        "count": int(txt.strip()),
                    })

            # Danmaku - JavaScript to extract chat messages efficiently
            new_msgs = await page.evaluate("""() => {
                const list = document.querySelector('[class*="webcast-chatroom___list"]');
                if (!list) return [];
                const results = [];
                for (const child of list.children) {
                    for (const msgDiv of child.children) {
                        const text = (msgDiv.textContent || '').trim();
                        if (text && text.length < 200) {
                            results.push(text);
                        }
                    }
                }
                return results;
            }""")

            for msg in new_msgs:
                if msg not in store.seen_danmaku:
                    store.seen_danmaku.add(msg)
                    store.danmaku.append({
                        "ts": now,
                        "text": msg,
                    })

            if len(store.viewer_counts) % 5 == 1:
                recent = store.danmaku[-3:] if store.danmaku else []
                print(f"  viewer={store.viewer_counts[-1]['count'] if store.viewer_counts else '?'}, "
                      f"danmaku_total={len(store.danmaku)}, danmaku_last={[d['text'][:20] for d in recent]}")

        except Exception as e:
            print(f"  Fetch err: {e}")

        await asyncio.sleep(2)


async def keep_alive(page):
    """Simulate human activity."""
    while True:
        try:
            await page.mouse.move(300 + (asyncio.get_event_loop().time() % 500), 
                                  200 + (asyncio.get_event_loop().time() % 300))
            await asyncio.sleep(0.3)
            await page.mouse.move(400 + (asyncio.get_event_loop().time() % 400), 
                                  300 + (asyncio.get_event_loop().time() % 200))
        except:
            pass
        await asyncio.sleep(8)


def generate_ass(data, output_path, duration):
    """Generate ASS subtitle file from collected data."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Top-center for viewer count (Alignment=8)
        "Style: ViewerCount,Microsoft YaHei,32,&H00FFFFFF,&H000000FF,&H80000000,&H00000000,"
        "1,0,0,0,100,100,0,0,1,2,0,8,50,50,50,1",
        # Bottom-left for danmaku scrolling (Alignment=2)
        "Style: Danmaku,Microsoft YaHei,26,&H00FFFFFF,&H000000FF,&H80000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,0,1,2,20,20,150,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def fmt_ts(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h}:{m:02d}:{sec:05.2f}"

    # Viewer count subtitles - hold each value for ~2 seconds
    if data["viewer_counts"]:
        for i, vp in enumerate(data["viewer_counts"]):
            start = max(0, vp["offset"] - 0.5)
            if i + 1 < len(data["viewer_counts"]):
                end = data["viewer_counts"][i + 1]["offset"] - 0.3
            else:
                end = duration
            if end > start:
                lines.append(
                    f'Dialogue: 0,{fmt_ts(start)},{fmt_ts(end)},ViewerCount,,0,0,0,,'
                    f'{{\\an8}}在线人数 {vp["count"]}'
                )

    # Danmaku subtitles - show each message as karaoke-style scrolling
    # Actually, for simpler rendering: batch danmaku by 2-second intervals
    if data["danmaku"]:
        # Group by 3-second windows
        window = 3.0
        current_window = 0
        batch_msgs = []

        def flush_batch(start, end, msgs):
            if not msgs:
                return
            # Show up to 4 messages per batch
            display = msgs[-4:]
            text = "\\N".join([m[:50] for m in display])
            lines.append(
                f'Dialogue: 0,{fmt_ts(start)},{fmt_ts(end)},Danmaku,,0,0,0,,'
                f'{{\\an2}}{text}'
            )

        for dp in data["danmaku"]:
            w = int(dp["offset"] / window)
            if w != current_window and batch_msgs:
                flush_batch(current_window * window, (current_window + 1) * window, batch_msgs)
                batch_msgs = []
                current_window = w
            batch_msgs.append(dp["text"])

        if batch_msgs:
            flush_batch(current_window * window, duration, batch_msgs)

    ass_content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ass_content)
    print(f"ASS: {len(ass_content)} bytes, {len(data['viewer_counts'])} viewer points, {len(data['danmaku'])} danmaku")


async def main():
    print("[1/3] Opening Playwright browser...")
    pw = await async_playwright().__aenter__()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    )
    page = await context.new_page()

    await page.goto(ROOM_URL, wait_until="domcontentloaded", timeout=30000)
    print("  Page loaded, waiting for stream data...")
    await asyncio.sleep(5)

    html = await page.content()
    stream_url = extract_stream_url(html)
    print(f"  Stream URL: {'YES' if stream_url else 'NO'}")

    if not stream_url:
        print("  ERROR: Could not find stream URL")
        await browser.close()
        return

    # Start ffmpeg recording
    print(f"\n[2/3] Recording {DURATION_MIN}min...")
    out = f"{ROOM_SHORT}_test.flv"
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner",
         "-headers", f"Referer: {ROOM_URL}\r\nUser-Agent: Mozilla/5.0",
         "-i", stream_url, "-t", str(DURATION_MIN * 60), "-c", "copy", out],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    print(f"  ffmpeg PID: {ffmpeg.pid}")

    # Start data capture
    print(f"\n[3/3] Capturing viewer count + danmaku for {DURATION_MIN}min...")
    store = DataStore()
    t0 = time.time()

    data_task = asyncio.create_task(capture_viewer_and_danmaku(page, store))
    alive_task = asyncio.create_task(keep_alive(page))

    await asyncio.sleep(DURATION_MIN * 60)

    # Stop everything
    data_task.cancel()
    alive_task.cancel()
    try: await data_task
    except: pass
    try: await alive_task
    except: pass

    ffmpeg.terminate()
    ffmpeg.wait()
    await browser.close()

    elapsed = time.time() - t0
    sz = os.path.getsize(out) if os.path.exists(out) else 0
    print(f"\nDone! {elapsed:.0f}s, {out} ({sz} bytes)")
    print(f"  Viewer points: {len(store.viewer_counts)}")
    print(f"  Danmaku items: {len(store.danmaku)}")

    if store.danmaku:
        print(f"  Sample messages:")
        for d in store.danmaku[-5:]:
            print(f"    [{d['offset']:.0f}s] {d['text'][:60]}")

    # Save raw data
    data = {
        "room": ROOM_URL,
        "duration": elapsed,
        "viewer_counts": [{"offset": round(v["ts"] - t0, 1), "count": v["count"]}
                          for v in store.viewer_counts],
        "danmaku": [{"offset": round(d["ts"] - t0, 1), "text": d["text"]}
                    for d in store.danmaku],
    }

    with open("page_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  Raw data saved: page_data.json")

    # Generate ASS
    # Recompute offsets relative to start
    for v in data["viewer_counts"]:
        if "offset" not in v:
            v["offset"] = 0
    for d in data["danmaku"]:
        if "offset" not in d:
            d["offset"] = 0

    ass_path = f"{ROOM_SHORT}_overlay.ass"
    generate_ass(data, ass_path, elapsed)
    print(f"  ASS: {ass_path}")


asyncio.run(main())
