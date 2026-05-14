#!/usr/bin/env python3
"""抖音直播监控录制器 - 多房间同时录制 + 录制完成即实时上传 + 同步抽音频(用于转写)"""
import os, sys, json, time, subprocess, re
from datetime import datetime

ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# 测试模式: 录制指定房间60秒后立即转写并退出
TEST_MODE = os.environ.get("TEST_MODE", "")
TEST_ROOM = os.environ.get("TEST_ROOM", "344763580")
TEST_DURATION = int(os.environ.get("TEST_DURATION", "60"))

recordings = {}
_renew_triggered = False

def load_rooms_from_github():
    if not GH_REPO or not GH_TOKEN:
        return load_rooms()
    try:
        import urllib.request, base64
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
            headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json"})
        resp = json.loads(urllib.request.urlopen(req).read())
        content = base64.b64decode(resp["content"]).decode("utf-8")
        rooms = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                parts = line.split("=", 1)
                rid, name = parts[0].strip(), parts[1].strip()
            else:
                rid = line.split("#")[0].strip().split()[0]
                name = rid
            if rid.isdigit():
                rooms.append({"id": rid, "name": name})
        return rooms
    except Exception as e:
        log(f"从GitHub拉取rooms.txt失败: {e}，使用本地文件")
        return load_rooms()

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
            if not line or line.startswith("#"): continue
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
            if w in text: return False
        return page.evaluate("!!document.querySelector('video')")
    except: return False

def get_anchor_name(page):
    """从抖音直播间页面提取主播真实昵称"""
    try:
        # 尝试从 title 提取 (格式: "主播名 正在直播")
        title = page.evaluate("document.title || ''")
        if title:
            name = title.replace(' 正在直播', '').replace(' 的直播间', '').replace(' - 抖音', '').strip()
            if name:
                return name
        
        # 尝试从页面脚本数据提取 user.nickname
        js = """() => {
            const scripts = document.querySelectorAll('script:not([src])');
            for (const s of scripts) {
                const t = s.textContent || '';
                if (!t.includes('nickname')) continue;
                const m = t.match(/"nickname"\s*:\s*"([^"]+)"/);
                if (m) return m[1];
            }
            return '';
        }"""
        name = page.evaluate(js)
        if name:
            return name
            
        # 从页面可见文本找主播名
        text = page.evaluate("document.body?.innerText?.slice(0,200) || ''")
        for line in text.split('\n'):
            line = line.strip()
            if line and len(line) < 20 and not any(kw in line for kw in ['直播','抖音','关注','粉丝','点赞']):
                return line
    except Exception as e:
        log(f"获取主播名失败: {e}")
    return ""

def navigate_page(page, room_id):
    url = f"https://live.douyin.com/{room_id}"
    log(f"打开: {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"页面加载超时({room_id}): {e}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except:
            pass
    time.sleep(5)

def start_recording(url, quality, room_id, anchor_name=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 文件名用主播真实名称（去掉非法字符）
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', anchor_name) if anchor_name else room_id
    base = f"{safe_name}_{quality}_{ts}"
    outfile = os.path.join(OUTPUT_DIR, f"{base}.mp4")
    audiofile = os.path.join(OUTPUT_DIR, f"{base}.wav")
    with open(os.path.join(OUTPUT_DIR, f"{safe_name}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"room_id":room_id,"anchor_name":anchor_name,"filename":f"{base}.mp4","audio":f"{base}.wav","quality":quality}, f)
    log(f"开始录制: {anchor_name}/{base}.mp4 [{quality}]  + 同步抽音频")
    # 视频 -c copy
    proc = subprocess.Popen([FFMPEG,"-y","-loglevel","warning","-i",url,"-c","copy","-movflags","+faststart+frag_keyframe+empty_moov","-f","mp4",outfile],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 音频 - 16kHz mono pcm_s16le wav
    audio_proc = subprocess.Popen([FFMPEG,"-y","-loglevel","warning","-i",url,"-vn","-acodec","pcm_s16le","-ar","16000","-ac","1",audiofile],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, outfile, audio_proc, audiofile

def stop_proc(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=10)
        except: proc.kill()
    time.sleep(0.5)

def upload_now(filepath, room_name):
    if not filepath or not os.path.exists(filepath): return
    fname = os.path.basename(filepath)
    fsize = os.path.getsize(filepath)
    
    # 用 Release API 上传 (永久存储，全部用 urllib，不依赖 gh cli)
    try:
        import urllib.request
        release_tag = f"rec-{datetime.now().strftime('%Y%m%d')}"
        headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
        
        # 1. 查找或创建 Release
        d = json.dumps({"tag_name": release_tag, "name": f"录制 {datetime.now().strftime('%Y-%m-%d')}",
                        "body": "自动上传", "target_commitish": "main"}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/releases",
            data=d, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req)
            rel_data = json.loads(resp.read())
        except urllib.error.HTTPError as e2:
            if e2.code == 422:  # already exists, fetch it
                req2 = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/releases/tags/{release_tag}", headers=headers)
                rel_data = json.loads(urllib.request.urlopen(req2).read())
            else:
                log(f"创建Release失败: {e2.code} {e2.read().decode('utf-8')[:150]}")
                return
        
        upload_url_template = rel_data.get('upload_url', '')
        if not upload_url_template:
            log("Release创建成功但无upload_url")
            return
        
        # 2. 上传文件到 Release (需要替换 {?name,label})
        upload_url = upload_url_template.replace('{?name,label}', f'?name={fname}')
        with open(filepath, 'rb') as f:
            file_data = f.read()
        upload_headers = {
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(file_data))
        }
        upload_req = urllib.request.Request(upload_url, data=file_data, headers=upload_headers, method="POST")
        upload_resp = urllib.request.urlopen(upload_req, timeout=300)
        log(f"实时上传成功: {room_name}/{fname} ({fsize/1024/1024:.1f}MB) -> Release")
    except Exception as e:
        log(f"上传异常: {e}")

def trigger_renewal():
    global _renew_triggered
    if not GH_REPO or not GH_TOKEN or _renew_triggered: return
    try:
        import urllib.request
        d = json.dumps({"ref":"main"}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/continuous.yml/dispatches",
            data=d, headers={"Authorization":f"Bearer {GH_TOKEN}","Content-Type":"application/json"}, method="POST")
        urllib.request.urlopen(req)
        log("续命: 已触发下一个workflow")
    except Exception as e: log(f"续命失败: {e}")
    _renew_triggered = True

def handle_room_end(rid, recordings, room_names, now):
    """停止录制并上传视频+音频"""
    rec = recordings.pop(rid)
    stop_proc(rec.get("proc"))
    stop_proc(rec.get("audio_proc"))
    for key in ["outfile", "audiofile"]:
        f = rec.get(key)
        if f and os.path.exists(f):
            upload_now(f, room_names.get(rid, rid))

def run_test():
    """测试模式: 录制 -> 转写 -> 退出"""
    from transcriber import transcribe
    log(f"测试模式: 录制 {TEST_ROOM} {TEST_DURATION}秒后转写")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0", viewport={"width":1280,"height":720})
        page = context.new_page()
        navigate_page(page, TEST_ROOM)
        time.sleep(5)
        test_anchor_name = get_anchor_name(page) or f"room_{TEST_ROOM}"
        log(f"主播昵称: {test_anchor_name}")
        
        live = is_live_page(page)
        log(f"直播间: {'ONAIR' if live else 'OFF'}")
        
        if live:
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except:
                log("reload超时，继续使用当前页面状态")
            for attempt in range(8):
                quality, url = get_stream_url(page, TEST_ROOM)
                if url: break
                log(f"等待推流... ({attempt+1}/8)")
                time.sleep(3)
            
            if url:
                proc, outfile, audio_proc, audiofile = start_recording(url, quality, TEST_ROOM, test_anchor_name)
                log(f"录制 {TEST_DURATION}秒...")
                time.sleep(TEST_DURATION)
                
                stop_proc(proc)
                stop_proc(audio_proc)
                
                vsize = os.path.getsize(outfile) if os.path.exists(outfile) else 0
                asize = os.path.getsize(audiofile) if os.path.exists(audiofile) else 0
                log(f"完成: 视频 {vsize/1024/1024:.1f}MB, 音频 {asize/1024/1024:.1f}MB")
                
                # 上传文件
                upload_now(outfile, test_anchor_name)
                upload_now(audiofile, test_anchor_name)
                
                # 转写
                if os.path.exists(audiofile) and asize > 0:
                    log("=== 开始转写 ===")
                    import traceback as tb
                    try:
                        txt, srt = transcribe(audiofile)
                        log("=== 转写结果 ===")
                        with open(txt, "r", encoding="utf-8") as f:
                            for line in f:
                                print(line, end="")
                        log(f"=== 字幕文件: {os.path.basename(srt)} ===")
                        # 上传转写结果
                        upload_now(txt, test_anchor_name)
                        upload_now(srt, test_anchor_name)
                    except Exception as e:
                        log(f"转写出错: {e}")
                        tb.print_exc()
                        # 转写出错也标记为失败
                        sys.exit(1)
                else:
                    log("音频文件不存在或为空，跳过转写")
            else:
                log("获取推流地址失败")
        else:
            log("当前不在直播")
        
        browser.close()
    log("测试结束")

def run():
    rooms = load_rooms()
    if not rooms:
        log("ERROR: 没有配置任何房间ID"); sys.exit(1)
    log(f"加载 {len(rooms)} 个房间:")
    for r in rooms: log(f"  {r['id']} = {r['name']}")
    log(f"检测间隔: {CHECK_INTERVAL}s | 任务最长: {MAX_DURATION//3600}h")
    if GH_REPO and GH_TOKEN: log("续命+实时上传: 开启")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", viewport={"width":1280,"height":720})
        pages, room_names, anchor_names = {}, {}, {}
        prev_live = {}
        try:
            for r in rooms:
                page = context.new_page()
                navigate_page(page, r["id"])
                pages[r["id"]] = page
                # 取主播真实昵称
                aname = get_anchor_name(page)
                if aname:
                    anchor_names[r["id"]] = aname
                    log(f"  主播昵称: {aname}")
            start_time = last_refresh = time.time()
            while True:
                now = time.time()
                elapsed = now - start_time
                if elapsed > 350*60: trigger_renewal()
                if elapsed > MAX_DURATION:
                    log(f"任务超时 ({elapsed/3600:.1f}h)，退出"); break
                if now - last_refresh > 300:
                    new_rooms = load_rooms_from_github()
                    for nr in new_rooms:
                        if nr["id"] not in pages:
                            log(f"检测到新房间: {nr['id']} = {nr['name']}，动态添加")
                            new_page = context.new_page()
                            navigate_page(new_page, nr["id"])
                            pages[nr["id"]] = new_page
                            aname = get_anchor_name(new_page)
                            anchor_names[nr["id"]] = aname if aname else nr["name"]
                            room_names[nr["id"]] = nr["name"]
                            log(f"  主播昵称: {aname}")
                    new_ids = {r["id"] for r in new_rooms}
                    for rid in list(pages.keys()):
                        if rid not in new_ids:
                            log(f"房间已移除: {room_names.get(rid,rid)}")
                            if rid in recordings: handle_room_end(rid, recordings, anchor_names, now)
                            try: pages[rid].close()
                            except: pass
                            del pages[rid]
                            room_names.pop(rid, None)
                            anchor_names.pop(rid, None)
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
                            aname = anchor_names.get(rid, room_names.get(rid, rid))
                            proc, outfile, audio_proc, audiofile = start_recording(url, quality, rid, aname)
                            recordings[rid] = {"proc":proc,"outfile":outfile,"audio_proc":audio_proc,"audiofile":audiofile,"start":now}
                        else: log(f"[{rid}] 获取推流地址失败")
                    if rid in recordings and not live:
                        handle_room_end(rid, recordings, anchor_names, now)
                for rid in list(recordings.keys()):
                    if time.time()-recordings[rid]["start"] > MAX_DURATION:
                        handle_room_end(rid, recordings, anchor_names, time.time())
                time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt: log("用户中断")
        finally:
            for rid in list(recordings.keys()): handle_room_end(rid, recordings, anchor_names, time.time())
            for p in pages.values():
                try: p.close()
                except: pass
            browser.close()

if __name__ == "__main__":
    if TEST_MODE:
        run_test()
    else:
        run()
