#!/usr/bin/env python3
"""抖音直播监控录制器 - 多房间同时录制 + 录制完成即实时上传 + 同步抽音频(用于转写)"""
import os, sys, json, threading, time, subprocess, re, urllib.request, base64, signal, base64
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
        # Handle literal backslash+n (some saves corrupt newlines)
        if '\\n' in content:
            content = content.replace('\\n', '\n')
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

# SIGTERM handler for graceful shutdown on workflow cancel
def _handle_sigterm(signum, frame):
    log(f"收到取消信号(SIGTERM)，正在清理录制文件...")
    end_ts = datetime.now().strftime("%%Y%%m%%d_%%H%%M%%S")
    for rid, rec in list(recordings.items()):
        try:
            # Kill ffmpeg
            if "proc" in rec and rec["proc"].poll() is None:
                rec["proc"].kill()
            # Rename to include end timestamp
            fpath = rec.get("outfile", "")
            if fpath and os.path.exists(fpath):
                dirn, fn = os.path.split(fpath)
                base_name = fn.rsplit('.', 1)[0]
                ext = fn.rsplit('.', 1)[1] if '.' in fn else ''
                new_fn = base_name + '~' + end_ts + '.' + ext
                new_path = os.path.join(dirn, new_fn)
                os.rename(fpath, new_path)
                log(f"取消时重命名: {fn} -> {new_fn}")
                upload_now(new_path, rid, anchor_names.get(rid, rid))
            # Rename audio too
            if "audiofile" in rec and os.path.exists(rec["audiofile"]):
                wav = rec["audiofile"]
                wdir, wfn = os.path.split(wav)
                wbase = wfn.rsplit('.', 1)[0]
                wext = wfn.rsplit('.', 1)[1] if '.' in wfn else ''
                new_wav = os.path.join(wdir, wbase + '~' + end_ts + '.' + wext)
                os.rename(wav, new_wav)
                upload_now(new_wav, rid, anchor_names.get(rid, rid))
        except Exception as e:
            log(f"取消清理异常: {e}")
    # Also handle TEST_MODE in global scope
    try:
        global _test_recording_path, _test_audio_path
        for attr in ['_test_recording_path', '_test_audio_path']:
            fpath = globals().get(attr, '')
            if fpath and os.path.exists(fpath):
                dirn, fn = os.path.split(fpath)
                base_name = fn.rsplit('.', 1)[0]
                ext = fn.rsplit('.', 1)[1] if '.' in fn else ''
                new_fn = base_name + '~' + end_ts + '.' + ext
                new_path = os.path.join(dirn, new_fn)
                os.rename(fpath, new_path)
                log(f"取消时重命名测试文件: {fn} -> {new_fn}")
                upload_now(new_path, rid, anchor_names.get(rid, rid) if 'rid' in dir() else rid)
    except: pass
    # Upload any orphan recordings left
    for f in os.listdir(OUTPUT_DIR):
        fpath = os.path.join(OUTPUT_DIR, f)
        if os.path.isfile(fpath):
            log(f"上传取消时的残留文件: {f}")
            upload_now(fpath, None, None)
    log("取消清理完成")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)
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


def http_check_live(room_id):
    """Pure HTTP GET detection - no Playwright needed. Returns (bool, str)"""
    import urllib.request, re
    try:
        req = urllib.request.Request(
            f"https://live.douyin.com/{room_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"liveStatus"\s*:\s*"(\w+)"', html)
        if not m:
            return (False, 'no_liveStatus_in_html')
        status = m.group(1)
        if status != "normal":
            return (False, f'liveStatus={status}')
        return (True, 'ok')
    except Exception as e:
        log(f"[http_check_live] exception for {room_id}: {e}")
        return (False, 'http_exception')


def http_get_anchor_name(room_id):
    """Get anchor name from SSR HTML - no Playwright needed."""
    import urllib.request, re
    try:
        req = urllib.request.Request(
            f"https://live.douyin.com/{room_id}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        big = sorted([s for s in scripts], key=len, reverse=True)[0]
        m = re.search(r'"nickname"\s*:\s*"([^"]+)"', big)
        if m:
            return m.group(1)
        m2 = re.search(r'<title>([^<]+)</title>', html)
        if m2:
            title = m2.group(1)
            for suffix in [' 的抖音直播', ' 的直播间', ' - 抖音', '抖音直播']:
                title = title.replace(suffix, "")
            title = title.strip()
            if title and len(title) < 30:
                return title
        return ""
    except:
        return ""

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
    """Legacy stub. Use http_check_live instead."""
    return (True, 'ok')
def get_anchor_name(page):
    """从抖音直播页面获取主播真实昵称"""
    try:
        title = page.evaluate("document.title || ''")
        if title:
            # 跳过默认标题（页面未完全加载时的通用标题）
            is_default = ('抖音直播' in title and '电脑版' in title)
            if not is_default:
                name = title.replace(' 正在直播', '').replace(' 的直播间', '').replace(' - 抖音', '').replace('的抖音直播间直播', '').replace('的抖音直播间', '').strip()
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


def update_rooms_nickname(anchor_names):
    """Update rooms.txt with detected anchor names = roomId=nickname format"""
    if not GH_REPO or not GH_TOKEN or not anchor_names:
        return
    try:
        req = urllib.request.Request(f'https://api.github.com/repos/{GH_REPO}/contents/rooms.txt',
            headers={'Authorization': f'Bearer {GH_TOKEN}', 'Accept': 'application/vnd.github+json'})
        resp = urllib.request.urlopen(req, timeout=15)
        d = json.loads(resp.read())
        content = base64.b64decode(d['content']).decode('utf-8')
        sha = d['sha']
        # Handle literal backslash+n
        if '\\n' in content:
            content = content.replace('\\n', '\n')
        lines = content.split('\n')
        changed = False
        for i, line in enumerate(lines):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('=', 1)
            rid = parts[0].strip()
            if rid in anchor_names:
                nickname = anchor_names[rid]
                # Clean nicknames: remove known suffixes
                for suffix in ['的抖音直播间直播', '的抖音直播间', ' 正在直播', ' 的直播间', ' - 抖音']:
                    nickname = nickname.replace(suffix, '')
                nickname = nickname.strip()
                if nickname and nickname != rid and not re.match(r'^\d+$', nickname):
                    if len(parts) == 1:
                        # Pure ID, no nickname - add it
                        lines[i] = f'{rid}={nickname}'
                        changed = True
                    elif len(parts) > 1:
                        # Already has nickname, update if different
                        old_name = parts[1].strip()
                        if old_name != nickname:
                            lines[i] = f'{rid}={nickname}'
                            changed = True
        if changed:
            new_content = '\n'.join(lines)
            new_b64 = base64.b64encode(new_content.encode('utf-8')).decode()
            put_req = urllib.request.Request(f'https://api.github.com/repos/{GH_REPO}/contents/rooms.txt',
                data=json.dumps({'message': f'auto-update nickname(s)', 'content': new_b64, 'sha': sha}).encode(),
                headers={'Authorization': f'Bearer {GH_TOKEN}', 'Content-Type': 'application/json'}, method='PUT')
            urllib.request.urlopen(put_req, timeout=15)
            log(f"rooms.txt 昵称已自动更新")
    except Exception as e:
        log(f"更新rooms.txt昵称失败: {e}")

def start_recording(url, quality, room_id, anchor_name=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{room_id}_{ts}"
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
    # Auto-rename: add end timestamp if missing
    if '~' not in fname:
        try:
            ets = datetime.now().strftime('%Y%m%d_%H%M%S')
            dn = os.path.dirname(filepath)
            bn = fname.rsplit('.', 1)[0]
            ext = fname.split('.')[-1] if '.' in fname else ''
            nfn = bn + '~' + ets + '.' + ext
            np = os.path.join(dn, nfn)
            os.rename(filepath, np)
            log(f"upload_now重命名: {fname} -> {nfn}")
            filepath = np
            fname = nfn
        except Exception as e:
            log(f"upload_now重命名失败: {e}")
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
                release_api_url = rel_data.get('url', '')
                if release_api_url:
                    patch_hdrs = {'Authorization': f'Bearer {GH_TOKEN}', 'Content-Type': 'application/json; charset=utf-8'}
                    body_json = json.dumps({'body': new_body}, ensure_ascii=False).encode('utf-8')
                    patch_req = urllib.request.Request(release_api_url,
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
    if isinstance(now, float): from datetime import datetime; end_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    else: end_ts = now.split('.')[0].replace(':', '').replace('-', '').replace(' ', '_')
    # Stop processes first
    try:
        p = rec.get("proc")
        if p: p.terminate(); p.wait(timeout=5)
    except: pass
    try:
        ap = rec.get("audio_proc")
        if ap: ap.terminate(); ap.wait(timeout=5)
    except: pass
    # Rename files to include end timestamp, then upload
    for key, ext in [("outfile", ".mp4"), ("audiofile", ".wav")]:
        f = rec.get(key)
        if f and os.path.exists(f):
            try:
                dirn, fn = os.path.split(f)
                base_name = fn.rsplit('.', 1)[0]
                new_fn = base_name + '~' + end_ts + ext
                new_path = os.path.join(dirn, new_fn)
                os.rename(f, new_path)
                log(f"重命名: {fn} -> {new_fn}")
                upload_now(new_path, room_names.get(rid, rid))
                # Transcribe WAV immediately after upload
                if ext == '.wav':
                    log(f"[{rid}] 开始转录: {new_fn}")
                    try:
                        import urllib.request as _ur, json as _json
                        _gh = {"Authorization": "Bearer " + GH_TOKEN, "Content-Type": "application/json"}
                        _body = _json.dumps({"event_type": "transcribe_self_renew"}).encode()
                        _req = _ur.Request("https://api.github.com/repos/" + GH_REPO + "/dispatches",
                            data=_body, headers=_gh, method="POST")
                        _ur.urlopen(_req, timeout=10)
                        log(f"[{rid}] 触发转录通知")
                    except Exception as _et:
                        log(f"[{rid}] 触发转录通知失败: {_et}")
            except Exception as e:
                log(f"[{rid}] upload/rename error: {e}")
    # Keep page open for re-detection (do NOT close)
    log(f"[{room_names.get(rid,rid)}] end, page kept open for re-detection")

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
        live, rsn = http_check_live(TEST_ROOM)
        log(f"直播间: {'ONAIR' if live else 'OFF'} ({rsn})")
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
    rooms = load_rooms_from_github()
    if not rooms:
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
                        # Initial detection - pure HTTP, no Playwright
                        for r in rooms:
                                live, reason = http_check_live(r["id"])
                                log(f"  [{r['name']}] is_live={'ONAIR' if live else 'OFF'} ({reason})")
                                prev_live[r["id"]] = live
                                aname = http_get_anchor_name(r["id"])
                                if aname:
                                        anchor_names[r["id"]] = aname
                                        log(f"  主播昵称: {aname}")
                                        update_rooms_nickname(anchor_names)
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
                                update_rooms_nickname(anchor_names)
                                try:
                                                                new_live, new_rsn = http_check_live(nr["id"])
                                except:
                                    new_live = False
                                log(f"[{room_names.get(nr['id'],nr['id'])}] is_live={'ONAIR' if new_live else 'OFF'} ({new_rsn})")
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
                                if rid in set([r['id'] for r in new_rooms]):
                                    log(f"房间 {rid} 临时丢失，已跳过移除")
                                    continue
                                log(f"房间已移除: {room_names.get(rid,rid)}")
                                if rid in recordings: handle_room_end(rid, recordings, anchor_names, now)
                                if rid in pages:
                                    del pages[rid]
                                room_names.pop(rid, None)
                                anchor_names.pop(rid, None)
                                prev_live.pop(rid, None)
                        last_refresh = now
                                        # 页面仅在录制时创建，下播后由HTTP检测
                    for rid in list(prev_live.keys()):
                        if rid not in pages:
                            log(f"[{room_names.get(rid,rid)}] 重新打开页面检查...")
                            try:
                                new_p = context.new_page()
                                navigate_page(new_p, rid)
                                pages[rid] = new_p
                            except:
                                log(f"[{room_names.get(rid,rid)}] 页面打开失败")
                                        # HTTP-based detection (no Playwright)
                                        for rid in list(pages.keys()):
                                                live, live_rsn = http_check_live(rid)
                                                prev = prev_live.get(rid)
                                                log(f"[{room_names.get(rid,rid)}] is_live={'ONAIR' if live else 'OFF'} ({live_rsn})")
                                                prev_live[rid] = live
                                                if live and rid not in recordings:
                                                        log(f"[{room_names.get(rid,rid)}] 检测到开播!")
                                                        # Create Playwright page lazily to get stream URL
                                                        if rid not in pages or pages[rid] is None:
                                                                try:
                                                                        new_pg = context.new_page()
                                                                        navigate_page(new_pg, rid)
                                                                        pages[rid] = new_pg
                                                                        time.sleep(3)
                                                                        aname = get_anchor_name(new_pg) or http_get_anchor_name(rid) or room_names.get(rid, rid)
                                                                        if aname and re.match(r'^\d+$', aname):
                                                                                aname = anchor_names.get(rid, room_names.get(rid, rid))
                                                                        anchor_names[rid] = aname
                                                                except Exception as _e:
                                                                        log(f"[{rid}] 创建页面失败: {_e}")
                                                                        pages[rid] = None
                                                        if rid in pages and pages[rid]:
                                                                _safe_reload(pages[rid])
                                                                for attempt in range(8):
                                                                        quality, url = get_stream_url(pages[rid], rid)
                                                                        if url: break
                                                                        log(f"[{rid}] 等待推流地址... ({attempt+1}/8)"); time.sleep(3)