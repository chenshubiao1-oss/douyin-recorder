#!/usr/bin/env python3
"""
抖音直播监控录制器 - GitHub Actions 版本
支持 rooms.txt 格式: room_id=主播名 或 room_id
"""
import os, sys, json, time, subprocess, re, asyncio
from datetime import datetime

# ---------- 配置 ----------
ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_RUN_ID = os.environ.get("GH_RUN_ID", "")

# ---------- 状态 ----------
recording = {"proc": None, "outfile": None, "room": None, "start": None}
# 记录是否已经触发过续命（避免重复触发）
_renew_triggered = False

def log(msg):
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{t}] {msg}", flush=True)

def load_rooms():
    """返回 [{id, name}] 列表"""
    if not os.path.exists(ROOMS_FILE):
        log(f"文件不存在: {ROOMS_FILE}")
        return []
    rooms = []
    with open(ROOMS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                parts = line.split("=", 1)
                rid = parts[0].strip()
                name = parts[1].strip()
            else:
                rid = line.split("#")[0].strip().split()[0]
                name = rid
            if rid.isdigit():
                rooms.append({"id": rid, "name": name})
    return rooms

# ---------- Playwright ----------
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
    """更严格的检测: 有推流URL才算真开播"""
    try:
        # 先看页面有没有flv_pull_url的script
        has_flv_script = page.evaluate('''() => {
            const scripts = document.querySelectorAll("script:not([src])");
            for (const s of scripts) {
                if ((s.textContent || "").includes("flv_pull_url")) return true;
            }
            return false;
        }''')
        if not has_flv_script:
            return False
        
        text = page.evaluate("document.body?.innerText?.slice(0,300) || ''")
        ended_words = ['直播已结束', '主播暂时离开', '下播了', '主播不在', '当前没有直播', '主播正在赶来的路上']
        for w in ended_words:
            if w in text:
                return False

        video = page.evaluate("!!document.querySelector('video')")
        return video
    except:
        return False

def navigate_page(page, room_id):
    url = f"https://live.douyin.com/{room_id}"
    log(f"打开: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

def start_recording(url, quality, room_id, room_name=""):
    """启动ffmpeg录制 + 写入主播元数据"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outfile = os.path.join(OUTPUT_DIR, f"{room_id}_{quality}_{ts}.mp4")
    # 写入元数据供baidu_upload.py读取
    meta = {"room_id": room_id, "room_name": room_name, "filename": os.path.basename(outfile), "quality": quality}
    with open(os.path.join(OUTPUT_DIR, f"{room_id}_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    log(f"开始录制: {room_name}/{os.path.basename(outfile)} [{quality}]")

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

    log(f"加载 {len(rooms)} 个房间:")
    for r in rooms:
        log(f"  {r['id']} = {r['name']}")
    log(f"检测间隔: {CHECK_INTERVAL}s | 最长录制: {MAX_DURATION//3600}h")
    if GH_REPO and GH_TOKEN:
        log(f"续命模式: 开启 (3小时自动触发下一个任务)"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-setuid-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )

        pages = {}
        room_names = {r["id"]: r["name"] for r in rooms}
        try:
            for r in rooms:
                page = context.new_page()
                navigate_page(page, r["id"])
                pages[r["id"]] = page

            last_refresh = time.time()

            start_time = time.time()

            while True:
                now = time.time()
                record_over = False

                # 自续命: 运行3小时后触发下一个workflow（如果还没触发过）
                if GH_REPO and GH_TOKEN and not _renew_triggered and (now - start_time) > 3 * 3600:
                    try:
                        import urllib.request
                        data = json.dumps({"ref": "main", "inputs": {"reason": "auto-renew"}}).encode()
                        req = urllib.request.Request(
                            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/continuous.yml/dispatches",
                            data=data,
                            headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
                            method="POST"
                        )
                        urllib.request.urlopen(req)
                        log("续命成功: 已触发下一个workflow")
                    except Exception as e:
                        log(f"续命失败: {e}")
                    _renew_triggered = True

                if now - last_refresh > 300:
                    log("周期性刷新页面...")
                    for rid, page in pages.items():
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=30000)
                            time.sleep(3)
                        except:
                            pass
                    last_refresh = now

                for rid, page in pages.items():
                    try:
                        live = is_live_page(page)
                    except:
                        live = False

                    if live and recording["proc"] is None:
                        log(f"[{room_names.get(rid, rid)}] 检测到开播!")

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
                            rname = room_names.get(rid, rid)
                            proc, outfile = start_recording(url, quality, rid, rname)
                            recording.update({"proc": proc, "outfile": outfile, "room": rid, "start": now})
                        else:
                            log(f"[{rid}] 无法获取推流地址")

                    elif recording["proc"] is not None:
                        if recording["room"] == rid or recording["room"] is None:
                            dur = now - recording["start"]
                            if not live or dur > MAX_DURATION:
                                stop_recording(recording["proc"])
                                f = recording["outfile"]
                                if f and os.path.exists(f):
                                    sz = os.path.getsize(f)
                                    log(f"[{recording['room']}] 录制结束: {os.path.basename(f)} ({sz/1024/1024:.1f}MB, {dur/60:.0f}m)")
                                recording["proc"] = None
                                recording["outfile"] = None
                                record_over = True
                                break

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
        finally:
            if recording["proc"]:
                stop_recording(recording["proc"])
            for p in pages.values():
                try: p.close()
                except: pass
            browser.close()

if __name__ == "__main__":
    run()
