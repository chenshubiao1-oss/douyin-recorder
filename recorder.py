#!/usr/bin/env python3
"""抖音直播监控录制器 - GitHub Actions 版本，多房间同时录制 + 录制完成即实时上传"""
import os, sys, json, time, subprocess, re
from datetime import datetime

ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

recordings = {}
_renew_triggered = False

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def load_rooms():
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
                rid, name = parts[0].strip(), parts[1].strip()
            else:
                rid = line.split("#")[0].strip().split()[0]
                name = rid
            if rid.isdigit():
                rooms.append({"id": rid, "name": name})
    return rooms

from playwright.sync_api import sync_playwright

def get_stream_url(page, room_id):
    js = r"""
    () => {
        const scripts = document.querySelectorAll('script:not([src])');
        for (const s of scripts) {
            const t = s.textContent || '';
            if (!t.includes('flv_pull_url')) continue;
            let decoded = t.replace(/\\"/g, '"').replace(/\\n/g, '').replace(/\\t/g, '');
            const regex = /"(FULL_HD1|HD1|SD1|SD2)"\s*:\s*"((?:[^"\\]|\\.)*)"/g;
            const seen = new Set(); const results = [];
            let m;
            while ((m = regex.exec(decoded)) !== null) {
                let u = m[2].replace(/\\\//g, '/').replace(/\\u0026/g, '&').replace(/\\u003d/g, '=');
                const base = u.split('?')[0];
                if (!seen.has(base)) { seen.add(base); results.push({q: m[1], url: u}); }
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
            flvs = [s for s in streams if 'm3u8' not in s['url']] or streams
            if flvs:
                best = max(flvs, key=lambda s: priority.get(s['q'], 0))
                return (best['q'], best['url'])
    except Exception as e:
        log(f"JS eval error: {e}")
    return (None, None)

def is_live_page(page):
    try:
        if not page.evaluate("""() => {const s=document.querySelectorAll('script:not([src])');for(const x of s){if((x.textContent||'').includes('flv_pull_url'))return true}return false}"""):
            return False
        text = page.evaluate("document.body?.innerText?.slice(0,300)||''")
        for w in ['直播已结束','主播暂时离开','下播了','主播不在','当前没有直播','主播正在赶来的路上']:
            if w in text:
                return False
        return page.evaluate("!!document.querySelector('video')")
    except:
        return False

def navigate_page(page, room_id):
    url = f"https://live.douyin.com/{room_id}"
    log(f"打开: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

def start_recording(url, quality, room_id, room_name=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outfile = os.path.join(OUTPUT_DIR, f"{room_id}_{quality}_{ts}.mp4")
    with open(os.path.join(OUTPUT_DIR, f"{room_id}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"room_id":room_id,"room_name":room_name,"filename":os.path.basename(outfile),"quality":quality}, f)
    log(f"开始录制: {room_name}/{os.path.basename(outfile)} [{quality}]")
    proc = subprocess.Popen([FFMPEG,"-y","-loglevel","warning","-i",url,"-c","copy","-movflags","+faststart+frag_keyframe+empty_moov","-f","mp4",outfile],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, outfile

def stop_recording(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=10)
        except: proc.kill()
        time.sleep(1)

def upload_now(filepath, room_name):
    """录制结束后立即上传单个文件到当前workflow的Artifact"""
    if not filepath or not os.path.exists(filepath):
        return
    fname = os.path.basename(filepath)
    fsize = os.path.getsize(filepath)
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "POST", f"/repos/{GH_REPO}/actions/artifacts",
             "-f", f"name=rec-{room_name}-{datetime.now().strftime('%H%M%S')}",
             "-f", f"path={filepath}"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GITHUB_TOKEN": GH_TOKEN}
        )
        if result.returncode == 0:
            log(f"实时上传成功: {room_name}/{fname} ({fsize/1024/1024:.1f}MB)")
        else:
            log(f"实时上传失败: {result.stderr[:200]}")
            # fallback: 用gh release upload
            log(f"尝试用Release上传...")
            release_tag = f"rec-{datetime.now().strftime('%Y%m%d')}"
            subprocess.run(["gh","release","create",release_tag,"--repo",GH_REPO,"--title",f"录制 {datetime.now().strftime('%Y-%m-%d')}","--notes","自动上传","--target","main"],
                          capture_output=True, timeout=30, env={**os.environ, "GITHUB_TOKEN": GH_TOKEN})
            r2 = subprocess.run(["gh","release","upload",release_tag,filepath,"--repo",GH_REPO,"--clobber"],
                               capture_output=True, text=True, timeout=120,
                               env={**os.environ, "GITHUB_TOKEN": GH_TOKEN})
            if r2.returncode == 0:
                log(f"Release上传成功: {room_name}/{fname} ({fsize/1024/1024:.1f}MB)")
            else:
                log(f"Release上传失败: {r2.stderr[:200]}")
    except Exception as e:
        log(f"上传异常: {e}")

def trigger_renewal():
    global _renew_triggered
    if not GH_REPO or not GH_TOKEN or _renew_triggered:
        return
    try:
        import urllib.request
        d = json.dumps({"ref":"main"}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/continuous.yml/dispatches",
            data=d, headers={"Authorization":f"Bearer {GH_TOKEN}","Content-Type":"application/json"}, method="POST")
        urllib.request.urlopen(req)
        log("续命: 已触发下一个workflow")
    except Exception as e:
        log(f"续命失败: {e}")
    _renew_triggered = True

def run():
    rooms = load_rooms()
    if not rooms:
        log("ERROR: 没有配置任何房间ID")
        sys.exit(1)
    log(f"加载 {len(rooms)} 个房间:")
    for r in rooms:
        log(f"  {r['id']} = {r['name']}")
    log(f"检测间隔: {CHECK_INTERVAL}s | 任务最长: {MAX_DURATION//3600}h")
    if GH_REPO and GH_TOKEN:
        log("续命+实时上传: 开启")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width":1280,"height":720})
        pages, room_names = {}, {r["id"]:r["name"] for r in rooms}
        prev_live = {}
        try:
            for r in rooms:
                page = context.new_page()
                navigate_page(page, r["id"])
                pages[r["id"]] = page
            start_time = last_refresh = time.time()
            while True:
                now = time.time()
                elapsed = now - start_time
                if elapsed > 350*60: trigger_renewal()
                if elapsed > MAX_DURATION:
                    log(f"任务超时 ({elapsed/3600:.1f}h)，退出"); break
                if now - last_refresh > 300:
                    # 重新读取rooms.txt，支持动态加人
                    new_rooms = load_rooms()
                    for nr in new_rooms:
                        if nr["id"] not in pages:
                            # 新房间: 打开新页面
                            log(f"检测到新房间: {nr['id']} = {nr['name']}，动态添加")
                            new_page = context.new_page()
                            navigate_page(new_page, nr["id"])
                            pages[nr["id"]] = new_page
                            room_names[nr["id"]] = nr["name"]
                    # 检查已删除的房间
                    new_ids = {r["id"] for r in new_rooms}
                    for rid in list(pages.keys()):
                        if rid not in new_ids:
                            log(f"房间已移除: {room_names.get(rid,rid)}")
                            if rid in recordings:
                                rec = recordings.pop(rid)
                                stop_recording(rec["proc"])
                            try: pages[rid].close()
                            except: pass
                            del pages[rid]
                            room_names.pop(rid, None)
                            prev_live.pop(rid, None)
                    log(f"周期性刷新页面... ({len(pages)}个房间)")
                    for rid, page in pages.items():
                        try: page.reload(wait_until="domcontentloaded",timeout=30000); time.sleep(3)
                        except: pass
                    last_refresh = now
                for rid, page in pages.items():
                    try: live = is_live_page(page)
                    except: live = False
                    prev = prev_live.get(rid)
                    if prev is None or prev != live:
                        log(f"[{room_names.get(rid,rid)}] is_live={'ONAIR' if live else 'OFF'}")
                        prev_live[rid] = live
                    if live and rid not in recordings:
                        log(f"[{room_names.get(rid,rid)}] 检测到开播!")
                        try: page.reload(wait_until="domcontentloaded",timeout=30000); time.sleep(5)
                        except: pass
                        for attempt in range(8):
                            quality, url = get_stream_url(page, rid)
                            if url: break
                            log(f"[{rid}] 等待推流地址... ({attempt+1}/8)"); time.sleep(3)
                        if url:
                            proc, outfile = start_recording(url, quality, rid, room_names.get(rid,rid))
                            recordings[rid] = {"proc":proc,"outfile":outfile,"start":now}
                        else: log(f"[{rid}] 获取推流地址失败")
                    if rid in recordings and not live:
                        rec = recordings.pop(rid)
                        stop_recording(rec["proc"])
                        f = rec["outfile"]
                        if f and os.path.exists(f):
                            sz, dur = os.path.getsize(f), now-rec["start"]
                            log(f"[{room_names.get(rid,rid)}] 录制结束: {os.path.basename(f)} ({sz/1024/1024:.1f}MB, {dur/60:.0f}m)")
                            upload_now(f, room_names.get(rid,rid))
                for rid in list(recordings.keys()):
                    rec = recordings[rid]
                    if now-rec["start"] > MAX_DURATION:
                        recordings.pop(rid)
                        stop_recording(rec["proc"])
                        f = rec["outfile"]
                        if f and os.path.exists(f):
                            log(f"[{room_names.get(rid,rid)}] 超时停止: {os.path.basename(f)} ({os.path.getsize(f)/1024/1024:.1f}MB)")
                            upload_now(f, room_names.get(rid,rid))
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt: log("用户中断")
        finally:
            for rid in list(recordings.keys()):
                rec = recordings.pop(rid); stop_recording(rec["proc"])
                if rec["outfile"] and os.path.exists(rec["outfile"]):
                    upload_now(rec["outfile"], room_names.get(rid,rid))
            for p in pages.values():
                try: p.close()
                except: pass
            browser.close()

if __name__ == "__main__":
    run()
