#!/usr/bin/env python3
"""
抖音直播监控录制器 - GitHub Actions 版本
无需登录，无头 Chromium 直接访问公开直播间页面提取推流地址
"""
import os, sys, json, time, subprocess, re, asyncio
from datetime import datetime

# ---------- 配置 ----------
ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))   # 检测间隔(秒)
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# ---------- 状态 ----------
recording = {"proc": None, "outfile": None, "room": None, "start": None}

def log(msg):
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{t}] {msg}", flush=True)

def load_rooms():
    if not os.path.exists(ROOMS_FILE):
        log(f"文件不存在: {ROOMS_FILE}")
        return []
    rooms = []
    with open(ROOMS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rid = line.split("#")[0].strip().split()[0] if "#" in line else line.split()[0]
            if rid.isdigit():
                rooms.append(rid)
    return rooms

# ---------- Playwright 页面操作 ----------
from playwright.sync_api import sync_playwright

def get_stream_url(page, room_id):
    """从页面提取最高的flv推流地址"""
    js = """
    () => {
        const scripts = document.querySelectorAll('script:not([src])');
        for (const s of scripts) {
            const t = s.textContent || '';
            if (!t.includes('flv_pull_url')) continue;
            
            let decoded = t.replace(/\\\\"/g, '"').replace(/\\\\n/g, '').replace(/\\\\t/g, '');
            const regex = /"(FULL_HD1|HD1|SD1|SD2)"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"/g;
            const seen = new Set();
            const results = [];
            let m;
            while ((m = regex.exec(decoded)) !== null) {
                let u = m[2].replace(/\\\\\\//g, '/');
                u = u.replace(/\\\\u0026/g, '&').replace(/\\\\u003d/g, '=');
                const base = u.split('?')[0];
                if (!seen.has(base)) {
                    seen.add(base);
                    results.push({q: m[1], url: u});
                }
            }
            if (results.length) return JSON.stringify(results);
        }
        return '';
    }
    """
    try:
        raw = page.evaluate(js)
        if raw:
            streams = json.loads(raw)
            priority = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}
            flvs = [s for s in streams if 'm3u8' not in s['url']]
            if not flvs:
                flvs = streams
            if flvs:
                best = max(flvs, key=lambda s: priority.get(s['q'], 0))
                return (best['q'], best['url'])
    except Exception as e:
        log(f"JS eval error: {e}")
    return (None, None)

def is_live_page(page):
    """检测页面是否显示直播中"""
    try:
        text = page.evaluate("document.body?.innerText?.slice(0,300) || ''")
        ended_words = ['直播已结束', '主播暂时离开', '下播了', '主播不在', '当前没有直播']
        for w in ended_words:
            if w in text:
                return False
        video = page.evaluate("!!document.querySelector('video')")
        return video
    except:
        return False

def navigate_page(page, room_id):
    """打开直播间页面"""
    url = f"https://live.douyin.com/{room_id}"
    log(f"打开: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

def start_recording(url, quality, room_id):
    """启动ffmpeg录制"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outfile = os.path.join(OUTPUT_DIR, f"{room_id}_{quality}_{ts}.mp4")
    log(f"开始录制: {os.path.basename(outfile)} [{quality}]")
    
    proc = subprocess.Popen(
        [FFMPEG, "-y",
         "-loglevel", "warning",
         "-i", url,
         "-c", "copy",
         "-movflags", "+faststart+frag_keyframe+empty_moov",
         "-f", "mp4",
         outfile],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return proc, outfile

def stop_recording(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except:
            proc.kill()
        time.sleep(1)

def run():
    rooms = load_rooms()
    if not rooms:
        log("ERROR: 没有配置任何房间ID，请编辑 rooms.txt")
        sys.exit(1)
    
    log(f"加载 {len(rooms)} 个房间: {', '.join(rooms[:5])}{'...' if len(rooms) > 5 else ''}")
    log(f"检测间隔: {CHECK_INTERVAL}s | 最长录制: {MAX_DURATION//3600}h")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        
        # 为每个房间创建一个页面
        pages = {}
        try:
            for rid in rooms:
                page = context.new_page()
                navigate_page(page, rid)
                pages[rid] = page
            
            last_refresh = time.time()
            
            while True:
                now = time.time()
                record_over = False
                
                # 定期刷新页面(每5分钟)
                if now - last_refresh > 300:
                    log("周期性刷新页面...")
                    for rid, page in pages.items():
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            time.sleep(3)
                        except:
                            pass
                    last_refresh = now
                
                # 检查每个房间
                for rid, page in pages.items():
                    try:
                        live = is_live_page(page)
                    except:
                        live = False
                    
                    if live and recording["proc"] is None:
                        # 开播
                        log(f"[{rid}] 检测到开播!")
                        
                        # 刷新获取最新推流地址
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            time.sleep(5)
                        except:
                            pass
                        
                        for attempt in range(8):
                            quality, url = get_stream_url(page, rid)
                            if url:
                                break
                            log(f"[{rid}] 等待推流地址... ({attempt+1}/8)")
                            time.sleep(3)
                        
                        if url:
                            proc, outfile = start_recording(url, quality, rid)
                            recording.update({
                                "proc": proc, "outfile": outfile,
                                "room": rid, "start": now
                            })
                        else:
                            log(f"[{rid}] 无法获取推流地址")
                        
                    elif recording["proc"] is not None:
                        # 录制中：检查是否该停了
                        if recording["room"] == rid or recording["room"] is None:
                            dur = now - recording["start"]
                            
                            # 主播下播 或 超时
                            if not live or dur > MAX_DURATION:
                                stop_recording(recording["proc"])
                                f = recording["outfile"]
                                if f and os.path.exists(f):
                                    sz = os.path.getsize(f)
                                    log(f"[{recording['room']}] 录制结束: {os.path.basename(f)} ({sz/1024/1024:.1f}MB, {dur/60:.0f}m)")
                                
                                recording["proc"] = None
                                recording["outfile"] = None
                                record_over = True
                                break  # 重新循环
                
                if record_over:
                    continue
                
                time.sleep(CHECK_INTERVAL)
                
        except KeyboardInterrupt:
            log("用户中断")
        finally:
            if recording["proc"]:
                stop_recording(recording["proc"])
            for p in pages.values():
                try: p.close()
                except: pass
            browser.close()

if __name__ == "__main__":
    run()
