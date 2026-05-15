#!/usr/bin/env python3
"""抖音直播监控录制器 - 多房间同时录制 + 录制完成即实时上传 + 同步抽音频(用于转写)"""
import os, sys, json, threading, time, subprocess, re
from datetime import datetime

WATCHDOG_TIMEOUT = 180
WATCHDOG_ITER_SEC = 120  # per-iteration hard limit
_iter_watchdog = None  # 主循环看门狗（秒）超过则认为本轮卡死，跳过
URLLIB_TIMEOUT = 30     # urllib 请求超时（秒）
PAGE_EVAL_TIMEOUT = 30000  # page.evaluate 超时(ms)

ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")

TEST_MODE = os.environ.get("TEST_MODE", "")
TEST_ROOM = os.environ.get("TEST_ROOM", "344763580")
TEST_DURATION = int(os.environ.get("TEST_DURATION", "60"))

recordings = {}
_renew_triggered = False
_refresh_counter = 0

def _try_eval(page, js, default=None):
    try:
        return page.evaluate(js, timeout=PAGE_EVAL_TIMEOUT)
    except Exception:
        return default

def _iter_timeout_hard(pg):
    """Thread watchdog: force close page when iteration timeout."""
    try:
        log(f"IWATCHDOG: iteration {WATCHDOG_ITER_SEC}s timeout - closing page")
        pg.close()
    except:
        pass

def _safe_reload(page):
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    except:
        pass

def load_rooms_from_github():
    if not GH_REPO or not GH_TOKEN:
        return load_rooms()
    try:
        import urllib.request, base64
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
            headers={"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT).read())
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
    """从抖音直播页面获取主播真实昵称"""
    try:
        title = page.evaluate("document.title || ''")
        if title:
            # 跳过默认标题（页面未完全加载时的通用标题）
            is_default = ('抖音直播' in title and '电脑版' in title)
            if not is_default:
                name = title.replace(' 正在直播', '').replace(' 的直播间', '').replace(' - 抖音', '').strip()
                if name:
                    return name
        # 从页面所有 script 搜 nickname
        js = """() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const t = s.textContent || '';
                const idx = t.indexOf('nickname');
                if (idx < 0) continue;
                const chunk = t.slice(idx, idx + 300);
                const m = chunk.match(/"nickname"\s*:\s*"([^"]+)"/);
                if (m && m[1].length < 40) return m[1];
            }
            return '';
        }"""
        name = page.evaluate(js)
        if name:
            return name
        # 尝试解码 \u 转义
        js2 = """() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const t = (s.textContent || '').replace(/\\u[0-9a-fA-F]{4}/g, function(m) { return String.fromCharCode(parseInt(m.slice(2), 16)); });
                const idx = t.indexOf('nickname');
                if (idx < 0) continue;
                const chunk = t.slice(idx, idx + 300);
                const m = chunk.match(/"nickname"\s*:\s*"([^"]+)"/);
                if (m && m[1].length < 40) return m[1];
            }
            return '';
        }"""
        name = page.evaluate(js2)
        if name:
            return name
        text = page.evaluate("document.body?.innerText?.slice(0,200) || ''")
        for line in text.split('\n'):
            line = line.strip()
            if line and len(line) < 20 and line != '开启读屏标签' and not any(kw in line for kw in ['直播','抖音','关注','粉丝','点赞','读屏']):
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
    base = f"{room_id}_{quality}_{ts}"
    outfile = os.path.join(OUTPUT_DIR, f"{base}.mp4")
    audiofile = os.path.join(OUTPUT_DIR, f"{base}.wav")
    with open(os.path.join(OUTPUT_DIR, f"{room_id}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"room_id":room_id,"anchor_name":anchor_name,"filename":f"{base}.mp4","audio":f"{base}.wav","quality":quality}, f)
    log(f"开始录制: {anchor_name}/{base}.mp4 [{quality}] + 同步抽音频")
    proc = subprocess.Popen([FFMPEG,"-y","-loglevel","warning","-i",url,"-c","copy","-movflags","+faststart+frag_keyframe+empty_moov","-f","mp4",outfile],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    import re
    fsize = os.path.getsize(filepath)
    try:
        import urllib.request, urllib.parse
        release_tag = f"rec-{datetime.now().strftime('%Y%m%d')}"
        headers = {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}
        safe_tag = release_tag.encode('ascii', errors='replace').decode('ascii')
        d = json.dumps({"tag_name": safe_tag, "name": safe_tag,
                        "body": "auto upload", "target_commitish": "main"}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/releases",
            data=d, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT)
            rel_data = json.loads(resp.read())
        except urllib.error.HTTPError as e2:
            if e2.code == 422:
                req2 = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/releases/tags/{safe_tag}", headers=headers)
                rel_data = json.loads(urllib.request.urlopen(req2, timeout=URLLIB_TIMEOUT).read())
            else:
                log(f"创建Release失败: {e2.code}")
                return
        upload_url_template = rel_data.get('upload_url', '')
        if not upload_url_template:
            log("Release创建成功但无upload_url")
            return
        safe_fname = re.sub(r'[^a-zA-Z0-9._-]', '_', fname)
        upload_url = upload_url_template.replace('{?name,label}', f'?name={safe_fname}')
        with open(filepath, 'rb') as f:
            file_data = f.read()
        upload_headers = {
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(file_data))
        }
        upload_req = urllib.request.Request(upload_url, data=file_data, headers=upload_headers, method="POST")
        upload_resp = urllib.request.urlopen(upload_req, timeout=300)
        try:
            old_body = rel_data.get('body', '')
            cn_entry = '\n' + room_name + '/' + fname
            if cn_entry not in old_body:
                new_body = old_body + cn_entry if old_body != 'auto upload' else 'auto upload' + cn_entry
                patch_hdrs = {'Authorization': f'Bearer {GH_TOKEN}', 'Content-Type': 'application/json; charset=utf-8'}
                # Ensure proper unicode JSON
                body_json = json.dumps({'body': new_body}, ensure_ascii=False).encode('utf-8')
                patch_req = urllib.request.Request(upload_url_template.split('{')[0],
                    data=body_json,
                    headers=patch_hdrs, method='PATCH')
                urllib.request.urlopen(patch_req, timeout=30)
        except Exception as _eb:
            log(f"body update failed: {_eb}")
        log(f"upload OK: {room_name}/{fname} -> [{safe_fname}] ({fsize/1024/1024:.1f}MB)")
    except Exception as e:
        log(f"上传异常: {e}")

def handle_room_end(rid, recordings, room_names, now):
    rec = recordings.pop(rid)
    # Upload original files FIRST (before stopping processes, to survive cancel)
    for key in ["outfile", "audiofile"]:
        f = rec.get(key)
        if f and os.path.exists(f):
            try:
                upload_now(f, room_names.get(rid, rid))
            except Exception as e:
                log(f"[{rid}] upload error: {e}")
    # Then stop processes (with timeout)
    try:
        p = rec.get("proc")
        if p: p.terminate(); p.wait(timeout=5)
    except: pass
    try:
        ap = rec.get("audio_proc")
        if ap: ap.terminate(); ap.wait(timeout=5)
    except: pass
    # 实时转录
    wav_file = rec.get("audiofile")
    if wav_file and os.path.exists(wav_file):
        pass  # transcribe disabled    # Keep page open for re-detection (do NOT close)
    log(f"[{room_names.get(rid,rid)}] recording ended, page kept open for re-detection")

def run_test():
    # from transcriber import transcribe  # disabled
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
                upload_now(outfile, test_anchor_name)
                upload_now(audiofile, test_anchor_name)
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
                        upload_now(txt, test_anchor_name)
                        upload_now(srt, test_anchor_name)
                    except Exception as e:
                        log(f"转写出错: {e}")
                        tb.print_exc()
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
        global _renew_triggered
        prev_live = {}
        # model_obj = None  # transcribe disabled
        try:
            for r in rooms:
                page = context.new_page()
                navigate_page(page, r["id"])
                pages[r["id"]] = page
                aname = get_anchor_name(page)
                if aname:
                    anchor_names[r["id"]] = aname
                    log(f"  主播昵称: {aname}")
            start_time = last_refresh = time.time()
            _iter_watchdog = None
            while True:
                try:
                    loop_start = time.time()
                    _iter_watchdog = threading.Timer(WATCHDOG_ITER_SEC, lambda pg=page: _iter_timeout_hard(pg))
                    _iter_watchdog.daemon = True
                    _iter_watchdog.start()
                    now = time.time()
                    elapsed = now - start_time
                    if elapsed > MAX_DURATION:
                        log(f"任务超时 ({elapsed/3600:.1f}h)，退出"); break
                    if now - last_refresh > 30:
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
                                try:
                                    new_live = is_live_page(new_page)
                                except:
                                    new_live = False
                                log(f"[{room_names.get(nr['id'],nr['id'])}] is_live={'ONAIR' if new_live else 'OFF'}")
                                prev_live[nr['id']] = new_live
                                if new_live:
                                    log(f"[{room_names.get(nr['id'],nr['id'])}] 检测到开播!")
                                    try: new_page.reload(wait_until="domcontentloaded",timeout=30000)
                                    except: pass
                                    time.sleep(3)
                                    for attempt in range(8):
                                        new_quality, new_url = get_stream_url(new_page, nr['id'])
                                        if new_url: break
                                        log(f"[{nr['id']}] 等待推流地址... ({attempt+1}/8)"); time.sleep(3)
                                    if new_url:
                                        new_name = anchor_names.get(nr['id'], room_names.get(nr['id'], nr['id']))
                                        new_proc, new_outfile, new_audio_proc, new_audiofile = start_recording(new_url, new_quality, nr['id'], new_name)
                                        recordings[nr['id']] = {"proc":new_proc,"outfile":new_outfile,"audio_proc":new_audio_proc,"audiofile":new_audiofile,"start":time.time()}
                                    else: log(f"[{nr['id']}] 获取推流地址失败")
                        new_ids = {r["id"] for r in new_rooms}
                        for rid in list(pages.keys()):
                            if rid not in new_ids:
                                log(f"房间已移除: {room_names.get(rid,rid)}")
                                if rid in recordings: handle_room_end(rid, recordings, anchor_names, now)
                                if rid in pages:
                                    del pages[rid]
                                room_names.pop(rid, None)
                                anchor_names.pop(rid, None)
                                prev_live.pop(rid, None)
                        last_refresh = now
                    # 重新打开已关闭的页面（下播后关闭的）
                    for rid in list(prev_live.keys()):
                        if rid not in pages:
                            log(f"[{room_names.get(rid,rid)}] 重新打开页面检查...")
                            try:
                                new_p = context.new_page()
                                navigate_page(new_p, rid)
                                pages[rid] = new_p
                            except:
                                log(f"[{room_names.get(rid,rid)}] 页面打开失败")
                    for rid, page in list(pages.items()):
                        try: live = is_live_page(page)
                        except: live = False
                        prev = prev_live.get(rid)
                        if prev is None or prev != live:
                            log(f"[{room_names.get(rid,rid)}] is_live={'ONAIR' if live else 'OFF'}")
                            prev_live[rid] = live
                        if live and rid not in recordings:
                            log(f"[{room_names.get(rid,rid)}] 检测到开播!")
                            _safe_reload(page)
                            time.sleep(5)
                            for attempt in range(8):
                                quality, url = get_stream_url(page, rid)
                                if url: break
                                log(f"[{rid}] 等待推流地址... ({attempt+1}/8)"); time.sleep(3)
                            if url:
                                aname = anchor_names.get(rid, room_names.get(rid, rid))
                                if re.match(r'^\d+$', aname):
                                    try:
                                        nn = get_anchor_name(page)
                                        if nn: aname = nn
                                    except: pass
                                proc, outfile, audio_proc, audiofile = start_recording(url, quality, rid, aname)
                                recordings[rid] = {"proc":proc,"outfile":outfile,"audio_proc":audio_proc,"audiofile":audiofile,"start":now}
                            else: log(f"[{rid}] 获取推流地址失败")
                        # 对录制中的房间，用ffmpeg进程检查替代页面检测
                        if rid in recordings:
                            proc = recordings[rid].get("proc")
                            if proc and proc.poll() is not None:
                                log(f"[{room_names.get(rid,rid)}] ffmpeg进程已退出，触发下播处理")
                                handle_room_end(rid, recordings, anchor_names, now)
                        elif rid in recordings and not live:
                            handle_room_end(rid, recordings, anchor_names, now)
                    for rid in list(recordings.keys()):
                        if time.time()-recordings[rid]["start"] > MAX_DURATION:
                            handle_room_end(rid, recordings, anchor_names, time.time(), model_obj)
                    # 续命：运行270分钟（4.5小时）后触发下一轮
                    if elapsed > 270*60 and not _renew_triggered:
                        try:
                            import urllib.request, json
                            wf_id = os.environ.get("GH_RUN_ID", "")
                            repo = os.environ.get("GH_REPO", "")
                            token = os.environ.get("GH_TOKEN", "")
                            if repo and token:
                                req = urllib.request.Request(
                                    f"https://api.github.com/repos/{repo}/actions/workflows/275535928/dispatches",
                                    data=json.dumps({"ref":"main"}).encode(),
                                    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"},
                                    method="POST"
                                )
                                urllib.request.urlopen(req, timeout=30)
                                log(f"续命成功: 触发新任务 (运行{elapsed/60:.0f}分)")
                        except Exception as e:
                            log(f"续命失败: {e}")
                        _renew_triggered = True
                    if _iter_watchdog: _iter_watchdog.cancel(); _iter_watchdog = None
                    time.sleep(CHECK_INTERVAL)
                    if time.time() - loop_start > WATCHDOG_TIMEOUT:
                        log("看门狗触发：本轮执行超时，跳过进入下一轮")
                except Exception as _e:
                    import traceback as _tb
                    if _iter_watchdog: _iter_watchdog.cancel(); _iter_watchdog = None
                    log(f"main loop crash: {_e}")
                    log(_tb.format_exc())
                    time.sleep(10)
        except KeyboardInterrupt: log("用户中断")
        except: pass  # 其他异常
        finally:
            # 1. 正常结束当前录制任务
            for rid in list(recordings.keys()): handle_room_end(rid, recordings, anchor_names, time.time(), model_obj)
            # 2. 清理未结束的页面
            for p in pages.values():
                try: p.close()
                except: pass
            # 3. 强制取消后，扫描 OUTPUT_DIR 下未被转录的音频
            if os.path.exists(OUTPUT_DIR):
                for fname in os.listdir(OUTPUT_DIR):
                    if fname.endswith('.wav'):
                        wav_path = os.path.join(OUTPUT_DIR, fname)
                        base = fname[:-4]
                        # 检查是否已有同名字幕文件
                        srt_path = os.path.join(OUTPUT_DIR, base + '.srt')
                        if os.path.exists(srt_path):
                            continue  # 已转录，跳过
                        log(f"扫描到未转录音频: {fname}，开始转录...")
                        wav_oom2 = os.path.getsize(wav_path)
                        if wav_oom2 > 50 * 1024 * 1024:
                            log(f"audio ({wav_oom2/1024/1024:.0f}MB) chunking...")
                            # from transcriber import transcribe  # disabled
                            wd2 = os.path.dirname(wav_path)
                            wb2 = os.path.splitext(os.path.basename(wav_path))[0]
                            cp2 = os.path.join(wd2, wb2 + '_chunk_%03d.wav')
                            sp2 = subprocess.Popen([FFMPEG, '-y', '-loglevel', 'warning', '-i', wav_path,
                                '-f', 'segment', '-segment_time', '600', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', cp2],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            sp2.wait(timeout=600)
                            cfs2 = sorted([f for f in os.listdir(wd2) if f.startswith(wb2 + '_chunk_') and f.endswith('.wav')])
                            all_t2, all_s2 = [], []
                            for cf2 in cfs2:
                                cp2 = os.path.join(wd2, cf2)
                                try:
                                    tp2, sp3 = transcribe(cp2)
                                    if tp2 and os.path.exists(tp2):
                                        with open(tp2, 'r', encoding='utf-8') as _f: all_t2.append(_f.read())
                                        os.remove(tp2)
                                    if sp3 and os.path.exists(sp3):
                                        with open(sp3, 'r', encoding='utf-8') as _f: all_s2.append(_f.read())
                                        os.remove(sp3)
                                except: pass
                                finally:
                                    try: os.remove(cp2)
                                    except: pass
                            if all_t2:
                                mt2 = os.path.join(wd2, wb2 + '.txt')
                                with open(mt2, 'w', encoding='utf-8') as _f: _f.write((chr(10)*2).join(all_t2))

                                upload_now(mt2, base)
                            if all_s2:
                                ms2 = os.path.join(wd2, wb2 + '.srt')
                                ln2 = 0
                                with open(ms2, 'w', encoding='utf-8') as _f:
                                    for seg2 in all_s2:
                                        for line2 in seg2.split("\n"):
                                            if line2.strip().isdigit():
                                                ln2 += 1; _f.write(str(ln2) + chr(10))
                                            else: _f.write(line2 + chr(10))
                                    _f.write('\n')
                                upload_now(ms2, base)
                            log(f"chunk done: {fname}")
                        else:
                            try:
                                # from transcriber import transcribe  # disabled
                                txt_path, srt_path = transcribe(wav_path)
                                if txt_path and os.path.exists(txt_path):
                                    upload_now(txt_path, base)
                                if srt_path and os.path.exists(srt_path):
                                    upload_now(srt_path, base)
                                log(f"transcribe done: {fname}")
                            except Exception as e:
                                    log(f"transcribe fail {fname}: {e}")
            browser.close()

if __name__ == "__main__":
    if TEST_MODE:
        run_test()
    else:
        run()
