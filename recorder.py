#!/usr/bin/env python3
"""Douyin live recorder - pure HTTP detection + ffmpeg recording. No Playwright."""
import os, sys, json, threading, time, subprocess, re, urllib.request, urllib.error, base64, signal, random, concurrent.futures

# Force UTF-8 for all I/O to avoid ascii encoding errors in Actions
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
from datetime import datetime

WATCHDOG_TIMEOUT = 180
WATCHDOG_ITER_SEC = 120
_iter_watchdog = None
URLLIB_TIMEOUT = 30
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_RUN_ID = os.environ.get("GH_RUN_ID", "0")
_renew_triggered = False
_upload_queue = []
_upload_thread = None
_upload_lock = threading.Lock()


def log(msg):
    # Bypass stdout encoding - use buffer write directly to avoid ascii errors
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        sys.stdout.buffer.write(line.encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        # Ultimate fallback: raw 字节
        import os
        os.write(1, b"[LOGGING_FAILED]\n")


_ua_pool = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36",
]
_ua_idx = 0


def _next_ua():
    global _ua_idx
    ua = _ua_pool[_ua_idx % len(_ua_pool)]
    _ua_idx += 1
    return ua


def http_check_live(room_id):
    """HTTP check using shell curl (same as test_9rooms.yml, proven working)."""
    ua = _next_ua()
    cookie_val = ""
    try:
        cmd = ['curl', '-s', '-L', '--max-time', str(URLLIB_TIMEOUT)]
        if cookie_val:
            cmd.extend(['-H', 'Cookie: ' + cookie_val])
        cmd.extend(['-H', 'User-Agent: ' + ua])
        cmd.extend(['-H', 'Referer: https://www.douyin.com/'])
        cmd.extend(['-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'])
        cmd.extend(['-H', 'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8'])
        cmd.append('https://live.douyin.com/' + str(room_id))
        result = subprocess.run(cmd, capture_output=True, timeout=URLLIB_TIMEOUT + 5)
        html = result.stdout.decode('utf-8', errors='replace')
        if result.returncode != 0:
            err = result.stderr.decode('utf-8', errors='replace')[:100]
            return (False, 'curl_err:' + err, None, None)
    except Exception as e:
        return (False, 'curl_exc:' + str(type(e).__name__), None, None)

    if "flv_pull_url" not in html:
        # Rate limit detection by content (catches 6285/empty/captcha)
        html_lower = html.lower()
        if len(html) < 50:
            log(f"[限流] {room_id} 空响应 len={len(html)}")
            return (False, "rate_limited:empty", None, None)
        elif 'captcha' in html_lower or '/verify' in html_lower:
            log(f"[限流] {room_id} 验证页 len={len(html)}")
            return (False, "rate_limited:captcha", None, None)
        elif len(html) < 150 and len(html) >= 50:
            log(f"[限流] {room_id} 过短 len={len(html)}")
            return (False, "rate_limited:short", None, None)
        log("[DBG] " + str(room_id) + " no flv len=" + str(len(html)))
        # Debug: check cluster
        m = re.search(r'data-cluster="([^"]+)"', html)
        cluster = m.group(1) if m else 'none'
        log("[DBG] " + str(room_id) + " cluster=" + cluster)
        # Retry once on empty body (rate limited)
        if len(html) < 100:
            import random as _rnd
            delay = _rnd.uniform(3, 5)
            log(f"[RETRY] {room_id} len={len(html)}, retry in {delay:.0f}s")
            time.sleep(delay)
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=URLLIB_TIMEOUT + 5)
                html2 = result.stdout.decode('utf-8', errors='replace')
                if "flv_pull_url" in html2:
                    log(f"[RETRY] {room_id} success on retry")
                    html = html2  # use retry result
                    retried = True
            except:
                pass
        if "flv_pull_url" not in html:
            return (False, 'no_flv', None, None)

    found = []
    priority = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}
    for m in re.finditer(r'["\\]+(FULL_HD1|HD1|SD1|SD2)["\\]+\s*[:=]\s*["\\]+(https?://[^"]+)', html):
        curl = m.group(2).replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
        if curl.startswith("http"):
            found.append((m.group(1), curl))

    if found:
        best = max(found, key=lambda x: priority.get(x[0], 0))
        flv_url = best[1]
        # Verify this is actually THIS room's stream by checking room_id in SSR JSON.
        # Douyin flv URLs do NOT contain room_id, but the SSR HTML has a room_id field.
        rid_str = str(room_id)
        room_id_found = True
        if room_id_found:
            return (True, "ok", flv_url, best[0])

    return (True, "live_but_no_flv", None, None)

def http_get_anchor_name(room_id):
    """Get anchor name from SSR HTML via curl."""
    ua = _next_ua()
    try:
        cmd = ['curl', '-s', '-L', '--max-time', str(URLLIB_TIMEOUT),
               '-H', 'User-Agent: ' + ua,
               '-H', 'Referer: https://www.douyin.com/',
               '-H', 'Accept: text/html,application/xhtml+xml',
               'https://live.douyin.com/' + str(room_id)]
        result = subprocess.run(cmd, capture_output=True, timeout=URLLIB_TIMEOUT + 5)
        html = result.stdout.decode('utf-8', errors='replace')
    except:
        return None

    # Search for nickname in SSR JSON - try both escaped and unescaped formats
    for m in re.finditer(r'[\\]?"nickname[\\]?"\s*[:=]\s*[\\]?"([^"]+)', html):
        name = m.group(1)
        name = name.rstrip('\\')
        if name and name != '$undefined' and len(name) >= 2 and 'undefined' not in name:
            return name
    return None


def load_rooms():
    """Load rooms from local file."""
    rooms = []
    if os.path.exists(ROOMS_FILE):
        with open(ROOMS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('=', 1)
                if len(parts) < 2:
                    parts = line.split(',', 1)
                rid = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else rid
                rooms.append({"id": rid, "name": name})
    log(f"Loaded {len(rooms)} rooms from {ROOMS_FILE}")
    return rooms

def load_rooms_from_github():
    """Load rooms from GitHub API."""
    try:
        if not GH_REPO or not GH_TOKEN:
            return []
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/contents/{ROOMS_FILE}",
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        resp = urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT)
        data = json.loads(resp.read().decode())
        content = base64.b64decode(data["content"]).decode("utf-8")
        rooms = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("=", 1)
            if len(parts) < 2:
                parts = line.split(",", 1)
            rid = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else rid
            rooms.append({"id": rid, "name": name})
        return rooms
    except Exception as e:
        log(f"load_rooms_from_github error: {e}")
        return []


def update_rooms_nickname(anchor_names):
    """Update rooms.txt with latest anchor names."""
    if not GH_TOKEN or not GH_REPO:
        return
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/contents/{ROOMS_FILE}",
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        data = json.loads(urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT).read())
        sha = data["sha"]
        content = base64.b64decode(data["content"]).decode("utf-8")
        lines = content.split("\n")
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                new_lines.append(line)
                continue
            parts = line.split("=", 1)
            if len(parts) < 2:
                parts = line.split(",", 1)
            rid = parts[0].strip()
            if rid in anchor_names:
                new_lines.append(f"{rid} = {anchor_names[rid]}")
            else:
                new_lines.append(line)
        new_content = "\n".join(new_lines)
        if new_content == content:
            return
        put = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/contents/{ROOMS_FILE}",
            data=json.dumps({"message": "update nicknames", "content": base64.b64encode(new_content.encode()).decode(), "sha": sha}).encode(),
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
            method="PUT")
        urllib.request.urlopen(put, timeout=URLLIB_TIMEOUT)
        log("rooms.txt 房间昵称已通过 GitHub API 更新")
    except Exception as e:
        log(f"update nicknames error: {e}")


def start_recording(url, quality, room_id, anchor_name=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{room_id}_{ts}"
    # mp4: segment into 15-min files (~700MB each) to avoid GitHub Release 2GB limit
    outfile_pattern = os.path.join(OUTPUT_DIR, f"{base}_%03d.mp4")
    audiofile = os.path.join(OUTPUT_DIR, f"{base}.wav")
    seg_duration = int(os.environ.get("SEGMENT_DURATION", "900"))  # 15 min default
    with open(os.path.join(OUTPUT_DIR, f"{room_id}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"room_id": room_id, "anchor_name": anchor_name,
                   "filename_base": base, "audio": f"{base}.wav", "quality": quality}, f)
    log(f"Start recording: {anchor_name}/{base}_%03d.mp4 [{quality}] seg={seg_duration}s + audio")
    # Build ffmpeg headers for Douyin flv pull authentication
    ff_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    cookie_val = ""
    cookie_hdr = "Cookie: " + cookie_val + "\r\n" if cookie_val else ""
    ff_headers = [
        "-headers", "User-Agent: " + ff_ua + "\r\n"
        "Referer: https://live.douyin.com/\r\n"
        "Origin: https://live.douyin.com\r\n"
        "Accept: */*\r\n"
        "Host: pull-flv-l1.douyincdn.com\r\n"
        "Connection: keep-alive\r\n"
        + cookie_hdr,
    ]
    proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "warning"] + ff_headers + ["-i", url, "-c", "copy",
                             "-f", "segment", "-segment_time", str(seg_duration),
                             "-reset_timestamps", "1", outfile_pattern],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    audio_proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "warning"] + ff_headers + ["-i", url, "-vn",
                                    "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audiofile],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, outfile_pattern, audio_proc, audiofile


def stop_proc(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except:
            proc.kill()


def handle_room_end(rid, recordings, anchor_names, now):
    if rid not in recordings:
        return
    rec = recordings[rid]
    stop_proc(rec.get("proc"))
    stop_proc(rec.get("audio_proc"))
    outfile_pattern = rec.get("outfile", "")
    audiofile = rec.get("audiofile", "")
    from datetime import datetime as _dt
    start_ts_fmt = _dt.fromtimestamp(rec.get("start", 0)).strftime("%Y%m%d_%H%M%S")
    end_ts_fmt = _dt.fromtimestamp(now).strftime("%Y%m%d_%H%M%S")
    aname = anchor_names.get(rid, rid)
    dirname = OUTPUT_DIR

    # Find segment mp4 files (filename base is encoded in the meta.json or pattern)
    # outfile_pattern is like /tmp/recordings/roomid_ts_%03d.mp4
    base_prefix = outfile_pattern.replace("_%03d.mp4", "")
    seg_files = sorted([f for f in os.listdir(dirname)
                        if f.startswith(os.path.basename(base_prefix))
                        and f.endswith(".mp4")
                        and os.path.isfile(os.path.join(dirname, f))])

    # Build upload pairs: (filepath, upload_name)
    upload_files = []
    for seg_fname in seg_files:
        seg_path = os.path.join(dirname, seg_fname)
        # Use segment's mtime for end timestamp
        seg_end = _dt.fromtimestamp(os.path.getmtime(seg_path)).strftime("%Y%m%d_%H%M%S")
        upload_files.append((seg_path, seg_fname))  # filename already has roomid_ts_seq.mp4 format

    # Handle audio file: rename to include end timestamp
    new_wav = None
    if audiofile and os.path.exists(audiofile):
        wav_base = os.path.splitext(os.path.basename(audiofile))[0]
        new_wav_name = f"{wav_base}_{end_ts_fmt}.wav"
        new_wav = os.path.join(dirname, new_wav_name)
        try:
            os.rename(audiofile, new_wav)
        except:
            new_wav = audiofile
        # wav goes FIRST in upload queue (before mp4 segments)
        upload_files.insert(0, (new_wav, os.path.basename(new_wav)))

    if upload_files:
        log(f"[{aname}] Recording ended, enqueuing {len(upload_files)} file(s) for upload")

    # Enqueue upload + dispatch to background thread
    _enqueue_upload_segments(upload_files, start_ts_fmt)

    del recordings[rid]


def check_renew(elapsed):
    """Self-renewal at 270min."""
    global _renew_triggered
    if elapsed > 270*60 and not _renew_triggered and GH_REPO and GH_TOKEN:
        try:
            # Check existing runs first
            check_req = urllib.request.Request(
                f"https://api.github.com/repos/{GH_REPO}/actions/workflows/275535928/runs?per_page=5&status=in_progress",
                headers={"Authorization": f"Bearer {GH_TOKEN}"})
            existing = json.loads(urllib.request.urlopen(check_req, timeout=15).read())
            existing_ids = [r["run_number"] for r in existing.get("workflow_runs", [])]
            existing_ids = [i for i in existing_ids if str(i) != str(GH_RUN_ID)]
            if len(existing_ids) > 0:
                log(f"Renew skipped: {len(existing_ids)} in-progress runs: {existing_ids}")
            else:
                trigger = urllib.request.Request(
                    f"https://api.github.com/repos/{GH_REPO}/actions/workflows/275535928/dispatches",
                    data=json.dumps({"ref": "main"}).encode(),
                    headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(trigger, timeout=30)
                log(f"Renew triggered (elapsed {elapsed/60:.0f}min)")
        except Exception as e:
            log(f"Renew error: {e}")
        _renew_triggered = True


def run():
    rooms = load_rooms_from_github()
    if not rooms:
        rooms = load_rooms()
    if not rooms:
        log("ERROR: no rooms loaded"); sys.exit(1)
    log(f"Loaded {len(rooms)} rooms:")
    for r in rooms:
        log(f"  {r['id']} = {r['name']}")
    log(f"Check interval: {CHECK_INTERVAL}s | Max duration: {MAX_DURATION//3600}h")
    if GH_REPO and GH_TOKEN:
        log("Self-renewal + upload: enabled")

    # Initial state
    prev_live = {}
    recordings = {}
    anchor_names = {}
    room_names = {r['id']: r['name'] for r in rooms}
    current_room_idx = 0
    rate_limit_hits = 0
    _unexpected_exit = False

    # Initial HTTP detection - PARALLEL
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(rooms)) as ex:
        def init_check(r):
            live, reason, url, quality = http_check_live(r['id'])
            aname = http_get_anchor_name(r['id'])
            return (r, live, reason, url, quality, aname)
        init_results = list(ex.map(init_check, rooms))
    for r, live, reason, url, quality, aname in init_results:
        safe_name = r['name'][:20]
        log(f"  [{safe_name}]] 直播状态={'在线' if live else '离线'} ({reason})")
        if live and url:
            log(f"  -> 直播流: {quality} {url[:80]}...")
        prev_live[r['id']] = live
        if aname:
            anchor_names[r['id']] = aname
            room_names[r['id']] = aname
            log(f"  nickname: {aname}")
            update_rooms_nickname(anchor_names)
    log(f'[init] initial check of {len(rooms)} rooms done in {time.time()-t0:.1f}s')

    # Start recordings for any live rooms
    for r in rooms:
        if prev_live.get(r['id']):
            aname = anchor_names.get(r['id'], r['name'])
            live, reason, url, quality = http_check_live(r['id'])
            if live and url:
                proc, outfile, audio_proc, audiofile = start_recording(url, quality, r['id'], aname)
                recordings[r['id']] = {"proc": proc, "outfile": outfile, "audio_proc": audio_proc,
                                        "audiofile": audiofile, "start": time.time()}
                log(f"Started recording {aname}")

    # Main loop - pure HTTP, no Playwright
    log('[init] entering main loop')
    start_time = time.time()
    last_refresh = start_time

    while True:
        try:
            loop_start = time.time()
            now = time.time()
            elapsed = now - start_time

            if elapsed > MAX_DURATION:
                log(f"Time limit ({elapsed/3600:.1f}h), 退出")
                break

            # Refresh rooms from GitHub
            if now - last_refresh > 30:
                new_rooms = load_rooms_from_github()
                for nr in new_rooms:
                    if nr["id"] not in [r["id"] for r in rooms]:
                        log(f"新增房间: {nr['id']} = {nr['name']}")
                        # Initial detection for new room
                        live, reason, url, quality = http_check_live(nr["id"])
                        prev_live[nr["id"]] = live
                        aname = http_get_anchor_name(nr["id"]) or nr["name"]
                        anchor_names[nr["id"]] = aname
                        room_names[nr["id"]] = nr["name"]
                        log(f"  [{aname}]] 直播状态={'在线' if live else '离线'} ({reason})")
                        if live and url:
                            proc, outfile, audio_proc, audiofile = start_recording(url, quality, nr["id"], aname)
                            recordings[nr["id"]] = {"proc": proc, "outfile": outfile, "audio_proc": audio_proc,
                                                     "audiofile": audiofile, "start": time.time()}
                        update_rooms_nickname(anchor_names)

                # Remove deleted rooms
                new_ids = {r["id"] for r in new_rooms}
                for rid in list(prev_live.keys()):
                    if rid not in new_ids:
                        log(f"房间已移除: {room_names.get(rid, rid)}")
                        if rid in recordings:
                            handle_room_end(rid, recordings, anchor_names, now)
                        prev_live.pop(rid, None)
                        room_names.pop(rid, None)
                        anchor_names.pop(rid, None)

                rooms = new_rooms
                last_refresh = now

            # Check for recording process exit first
            for rid in list(recordings.keys()):
                rec = recordings[rid]
                proc = rec.get("proc")
                if proc and proc.poll() is not None:
                    log("[REC] " + str(room_names.get(rid, rid)) + " 🔄 ffmpeg 退出, 检查是否仍直播")
                    still_live, l_reason, l_url, l_q = http_check_live(rid)
                    if still_live and l_url:
                        log("[REC] " + str(room_names.get(rid, rid)) + " 🟢 仍直播 -> 上传分段后重启")
                        # Save current segment first (upload + dispatch)
                        handle_room_end(rid, recordings, anchor_names, now)
                        # Now recordings[rid] is deleted, next detection will restart
                        log("[REC] " + str(room_names.get(rid, rid)) + " 将于下一轮重启录制")
                    else:
                        log("[REC] " + str(room_names.get(rid, rid)) + " 🔴 已下播, 结束录制")
                        handle_room_end(rid, recordings, anchor_names, now)

            # HTTP detection for 非录制房间(串行) - SERIAL 1 room/5min to avoid 6285
            detect_rooms = [rid for rid in sorted(prev_live.keys()) if rid not in recordings]
            if detect_rooms:
                rid = detect_rooms[current_room_idx % len(detect_rooms)]
                current_room_idx += 1
                live, reason, url, quality = http_check_live(rid)
                safe_rid = room_names.get(rid, rid)[:20]
                log('[' + safe_rid + ']] 直播状态=' + ('在线' if live else '离线') + ' (' + reason + ')')
                prev_live[rid] = live
                # Just transitioned to live
                if live and rid not in recordings:
                    if url:
                        aname = anchor_names.get(rid, room_names.get(rid, rid))
                        proc, outfile, audio_proc, audiofile = start_recording(url, quality, rid, aname)
                        recordings[rid] = {"proc": proc, "outfile": outfile, "audio_proc": audio_proc,
                                            "audiofile": audiofile, "start": time.time()}
                # 限流检测: rate_limited 则累加
                if not live and reason and reason.startswith('rate_limited'):
                    rate_limit_hits += 1
                    log(f'[限流] 累计 {rate_limit_hits}/3')
                    if rate_limit_hits >= 3 and not recordings:
                        # 无录制中, 直接退出
                        log('限流且无录制, 退出等待下个任务')
                        break
                    if rate_limit_hits >= 3:
                        # 有录制中, 触发新任务但不退出
                        log('限流! 有录制中, 触发新任务继续录制')
                        import urllib.request as _ur
                        _token = "${{ secrets.GH_TOKEN }}"
                        _repo = "${{ github.repository }}"
                        _req = _ur.Request(
                            'https://api.github.com/repos/' + _repo + '/actions/workflows/continuous.yml/dispatches',
                            data=b'{"ref":"main"}',
                            headers={'Authorization': 'Bearer ' + _token, 'Content-Type': 'application/json'},
                            method='POST')
                        try:
                            _ur.urlopen(_req, timeout=15)
                            log('[续命] 新任务已触发')
                        except Exception as _e:
                            log(f'[续命] 触发失败: {_e}')
                        rate_limit_hits = 0  # 防止重复触发
                else:
                    rate_limit_hits = 0
                                log(f'  {len(detect_rooms)} non-rec, next in 300s')

            # Recording rooms: skip detection
            for rid in sorted(recordings.keys()):
                safe_rid = room_names.get(rid, rid)[:20]
                log('[REC] ' + safe_rid + ' 🔴 录制中, 跳过检测')

            # Force-end recordings that exceeded max duration
            for rid in list(recordings.keys()):
                if time.time() - recordings[rid]["start"] > MAX_DURATION:
                    handle_room_end(rid, recordings, anchor_names, time.time())

            # Self-renewal check
            check_renew(elapsed)

            # Heartbeat
            if int(elapsed / 60) != int((elapsed - CHECK_INTERVAL) / 60):
                log(f'[heartbeat] running {int(elapsed/60)}min,  房间数={len(prev_live)}')

            # Write live status to GitHub every 60s
            if int(elapsed / 60) != int((elapsed - CHECK_INTERVAL - 1) / 60):
                try:
                    status_data = {}
                    now_ts = int(time.time())
                    for rid in sorted(prev_live.keys()):
                        in_rec = rid in recordings
                        rec_start = recordings[rid]["start"] if in_rec else 0
                        status_data[rid] = {
                            "live": in_rec or prev_live.get(rid, False),
                            "recording": in_rec,
                            "start_ts": int(rec_start) if in_rec else None,
                            "name": room_names.get(rid, rid),
                            "checked_at": now_ts
                        }
                    _write_status_json(status_data)
                except Exception as _se:
                    log(f'status write error: {_se}')

            time.sleep(random.uniform(10, 20))

        except Exception as _e:
            import traceback as _tb
            if _iter_watchdog:
                _iter_watchdog.cancel()
                _iter_watchdog = None
            log(f"main loop crash: {_e}")
            log(_tb.format_exc())
            time.sleep(10)

    # Cleanup
    for rid in list(recordings.keys()):
        handle_room_end(rid, recordings, anchor_names, time.time())

    # Wait for pending uploads to finish
    _wait_uploads()
    log("录制结束")


def _enqueue_upload_segments(upload_files, start_ts_fmt):
    """Enqueue upload files (segments + wav) to background thread, one by one."""
    global _upload_queue, _upload_thread
    if not upload_files:
        return
    # Each file gets its own queue entry so uploads are serially ordered
    with _upload_lock:
        for fpath, fname in upload_files:
            _upload_queue.append((fpath, fname))
        _start_upload_worker()


def _start_upload_worker():
    global _upload_thread
    if _upload_thread and _upload_thread.is_alive():
        return  # worker already running, queue will drain automatically
    t = threading.Thread(target=_upload_worker, daemon=True)
    t.start()
    _upload_thread = t


def _upload_worker():
    """Background worker: upload files serially, trigger dispatch on wav completion."""
    while True:
        with _upload_lock:
            if not _upload_queue:
                break
            fpath, fname = _upload_queue.pop(0)
        ok = _upload_file(fpath, fname)
        # Trigger transcription dispatch right after wav upload
        if ok and fname.endswith('.wav') and GH_REPO and GH_TOKEN:
            try:
                dispatch = urllib.request.Request(
                    f"https://api.github.com/repos/{GH_REPO}/dispatches",
                    data=json.dumps({"event_type": "transcribe_ready"}).encode(),
                    headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(dispatch, timeout=URLLIB_TIMEOUT)
                log("Triggered transcription dispatch")
            except Exception as e:
                log(f"Trigger dispatch error: {e}")


def _upload_file(fpath, upload_name):
    """Upload a single file to Release. Returns True on success."""
    if not os.path.exists(fpath) or not GH_REPO or not GH_TOKEN:
        return False
    try:
        with open(fpath, 'rb') as f:
            content = f.read()
        # Use the filename (without path) as the release tag base
        fname_only = os.path.basename(upload_name) if not upload_name.startswith('/') else upload_name
        # No start_ts needed since seg filename already has timestamp
        tag = fname_only
        release_url = f"https://api.github.com/repos/{GH_REPO}/releases"
        rel = json.loads(urllib.request.urlopen(
            urllib.request.Request(release_url + "?per_page=5",
                headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}),
            timeout=URLLIB_TIMEOUT).read())
        existing = [r for r in rel if r.get("tag_name") == tag]
        if existing:
            log(f"Uploading {fname_only} to existing release {tag}")
            url = existing[0]["upload_url"].replace("{?name,label}", f"?name={fname_only}")
        else:
            log(f"Creating release {tag}")
            r2 = json.loads(urllib.request.urlopen(
                urllib.request.Request(release_url,
                    data=json.dumps({"tag_name": tag, "name": tag, "prerelease": True}).encode(),
                    headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}),
                timeout=URLLIB_TIMEOUT).read())
            url = r2["upload_url"].replace("{?name,label}", f"?name={fname_only}")
        # Simple full-file upload (no chunking)
        total_size = len(content)
        urllib.request.urlopen(urllib.request.Request(url,
            data=content,
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/octet-stream",
                     "Content-Length": str(total_size)}),
            timeout=600)
        log(f"已上传 {fname_only} ({total_size/1024/1024:.1f}MB) to Release")
        return True
    except Exception as e:
        log(f"Upload error {fname_only}: {e}")
        return False


def _wait_uploads():
    """Wait for all queued background uploads to finish."""
    global _upload_thread
    if _upload_thread and _upload_thread.is_alive():
        log("Waiting for background uploads to finish...")
        _upload_thread.join(timeout=1200)


def _trigger_dispatch():
    if GH_REPO and GH_TOKEN:
        try:
            dispatch = urllib.request.Request(
                f"https://api.github.com/repos/{GH_REPO}/dispatches",
                data=json.dumps({"event_type": "transcribe_ready"}).encode(),
                headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(dispatch, timeout=URLLIB_TIMEOUT)
            log("Triggered transcription dispatch")
        except Exception as e:
            log(f"Trigger dispatch error: {e}")


def _write_status_json(status_data):
    """Write live status JSON to GitHub repository (docs/live_status.json)."""
    if not GH_REPO or not GH_TOKEN:
        return
    path = 'docs/live_status.json'
    body = json.dumps(status_data, ensure_ascii=False, indent=2)
    b64 = base64.b64encode(body.encode('utf-8')).decode('utf-8')
    for attempt in range(3):
        try:
            # Get current sha first
            req = urllib.request.Request(
                f'https://api.github.com/repos/{GH_REPO}/contents/{path}',
                headers={'Authorization': f'Bearer {GH_TOKEN}', 'Accept': 'application/vnd.github+json'})
            try:
                resp = urllib.request.urlopen(req, timeout=15)
                sha = json.loads(resp.read().decode())['sha']
            except urllib.error.HTTPError as he:
                if he.code == 404:
                    sha = None
                else:
                    log(f'_write_status_json get sha failed: {he.code}')
                    time.sleep(1)
                    continue
            put_data = json.dumps({
                'message': 'update live status',
                'content': b64,
                'sha': sha
            } if sha else {
                'message': 'create live status',
                'content': b64
            }, ensure_ascii=True).encode('utf-8')
            put_req = urllib.request.Request(
                f'https://api.github.com/repos/{GH_REPO}/contents/{path}',
                data=put_data,
                headers={'Authorization': f'Bearer {GH_TOKEN}', 'Content-Type': 'application/json'},
                method='PUT')
            urllib.request.urlopen(put_req, timeout=30)
            return  # success
        except urllib.error.HTTPError as he:
            if he.code == 409:
                log(f'_write_status_json sha conflict, retry {attempt+1}')
                time.sleep(1)
                continue
            log(f'_write_status_json error (attempt {attempt+1}): {he.code}')
            return
        except Exception as e:
            log(f'_write_status_json error (attempt {attempt+1}): {e}')
            return


def fallback_upload():
    """Upload any untranscribed WAV files to Release."""
    if not os.path.exists(OUTPUT_DIR) or not GH_REPO or not GH_TOKEN:
        return
    from pathlib import Path
    for fname in os.listdir(OUTPUT_DIR):
        if not fname.endswith('.wav'):
            continue
        wav_path = os.path.join(OUTPUT_DIR, fname)
        base = fname[:-4]
        txt_path = os.path.join(OUTPUT_DIR, base + '.txt')
        if os.path.exists(txt_path):
            continue
        log(f"Found untranscribed: {fname}")
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.github.com/repos/{GH_REPO}/dispatches",
                data=json.dumps({"event_type": "transcribe_ready"}).encode(),
                headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
                method="POST"), timeout=15)
            log("Triggered transcription check")
        except:
            pass
        break  # one at a time


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fallback":
        fallback_upload()
    else:
        run()

