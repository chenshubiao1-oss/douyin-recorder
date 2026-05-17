#!/usr/bin/env python3
"""Douyin live recorder - pure HTTP detection + ffmpeg recording. No Playwright."""
import os, sys, json, threading, time, subprocess, re, urllib.request, base64, signal, base64, random
from datetime import datetime

WATCHDOG_TIMEOUT = 180
WATCHDOG_ITER_SEC = 120
_iter_watchdog = None
URLLIB_TIMEOUT = 30
FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")

ROOMS_FILE = os.environ.get("ROOMS_FILE", "rooms.txt")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "15"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", str(5 * 3600)))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_RUN_ID = os.environ.get("GH_RUN_ID", "0")
_renew_triggered = False


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_check_live(room_id):
    """HTTP check - simple: flv_pull_url in html = live."""
    import urllib.request as _ur
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    try:
        req = _ur.Request('https://live.douyin.com/' + str(room_id),
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        cookie_val = os.environ.get("DOUYIN_COOKIE")
        if cookie_val:
            req.add_header("Cookie", cookie_val)
        resp = _ur.urlopen(req, timeout=URLLIB_TIMEOUT)
        raw = resp.read()
        html = raw.decode("utf-8", errors="replace")
    except Exception as e:
        err_str = str(e).encode('ascii', errors='replace').decode('ascii')
        return (False, 'http_error:' + err_str, None, None)

    if 'flv_pull_url' not in html:
        return (False, 'no_flv_pull_url', None, None)

    found = []
    priority = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}
    for m in re.finditer(r'["\\]+(FULL_HD1|HD1|SD1|SD2)["\\]+\s*[:=]\s*["\\]+(https?://[^"\\\s,}\]>]+)', html):
        url = m.group(2).replace('\\/', '/').replace('\\u0026', '&').replace('\\u003d', '=')
        if url.startswith('http'):
            found.append((m.group(1), url))

    if found:
        best = max(found, key=lambda x: priority.get(x[0], 0))
        return (True, 'ok', best[1], best[0])

    return (True, 'live_but_no_flv_url', None, None)


def http_get_anchor_name(room_id):
    """Get anchor name from SSR HTML."""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    try:
        req = urllib.request.Request(f"https://live.douyin.com/{room_id}",
            headers={"User-Agent": ua, "Accept": "text/html"})
        resp = urllib.request.urlopen(req, timeout=URLLIB_TIMEOUT)
        html = resp.read().decode("utf-8", errors="replace")
    except:
        return None

    # Two occurrences: first is placeholder "$undefined", second is real name
    idx = html.find('"nickname"')
    if idx > 0:
        # Skip first ($undefined)
        idx2 = html.find('"nickname"', idx + 5)
        if idx2 > 0:
            # Extract value
            start = html.find('"', idx2 + 10)
            if start > 0:
                end = html.find('"', start + 1)
                if end > start:
                    name = html[start+1:end]
                    if name and name != '$undefined':
                        return name
    # Fallback: try escaped format
    m = re.search(r'nickname[\\]*":\s*[\\]*"([^"\\]+)', html)
    if m:
        name = m.group(1)
        if name and name != '$undefined':
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
            parts = line.split(",", 1)
            rid = parts[0].strip()
            if rid in anchor_names:
                new_lines.append(f"{rid},{anchor_names[rid]}")
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
        log("rooms.txt nicknames updated via GitHub API")
    except Exception as e:
        log(f"update nicknames error: {e}")


def start_recording(url, quality, room_id, anchor_name=""):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{room_id}_{ts}"
    outfile = os.path.join(OUTPUT_DIR, f"{base}.mp4")
    audiofile = os.path.join(OUTPUT_DIR, f"{base}.wav")
    with open(os.path.join(OUTPUT_DIR, f"{room_id}_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"room_id": room_id, "anchor_name": anchor_name,
                   "filename": f"{base}.mp4", "audio": f"{base}.wav", "quality": quality}, f)
    log(f"Start recording: {anchor_name}/{base}.mp4 [{quality}] + audio")
    proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "warning", "-i", url, "-c", "copy",
                             "-movflags", "+faststart+frag_keyframe+empty_moov", "-f", "mp4", outfile],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    audio_proc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "warning", "-i", url, "-vn",
                                    "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", audiofile],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, outfile, audio_proc, audiofile


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
    outfile = rec.get("outfile", "")
    audiofile = rec.get("audiofile", "")
    start_ts = int(rec.get("start", 0))
    end_ts = int(now)
    if outfile and os.path.exists(outfile):
        # Use file mtime for more accurate end timestamp
        end_ts = int(os.path.getmtime(outfile))
    aname = anchor_names.get(rid, rid)
    base = os.path.splitext(os.path.basename(outfile))[0] if outfile else f"{rid}_{start_ts}"
    if outfile:
        ext = ".mp4"
        base_new = f"{rid}_{start_ts}.{end_ts}"
        dirname = os.path.dirname(outfile)
        new_mp4 = os.path.join(dirname, f"{base_new}.mp4")
        new_wav = os.path.join(dirname, f"{base_new}.wav")
        try:
            os.rename(outfile, new_mp4)
        except:
            pass
        if audiofile and os.path.exists(audiofile):
            try:
                os.rename(audiofile, new_wav)
            except:
                pass
        log(f"[{aname}] Recording ended, saved as {base_new}")

    # Upload
    def upload(fpath, upload_name):
        if not os.path.exists(fpath) or not GH_REPO or not GH_TOKEN:
            return
        try:
            with open(fpath, 'rb') as f:
                content = f.read()
            tag = f"{upload_name}_{start_ts}"
            release_url = f"https://api.github.com/repos/{GH_REPO}/releases"
            rel = json.loads(urllib.request.urlopen(
                urllib.request.Request(release_url + "?per_page=5",
                    headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}),
                timeout=URLLIB_TIMEOUT).read())
            existing = [r for r in rel if r.get("tag_name") == tag]
            if existing:
                log(f"Uploading {upload_name} to existing release {tag}")
                url = existing[0]["upload_url"].replace("{?name,label}", f"?name={upload_name}")
            else:
                log(f"Creating release {tag}")
                r2 = json.loads(urllib.request.urlopen(
                    urllib.request.Request(release_url,
                        data=json.dumps({"tag_name": tag, "name": tag, "prerelease": True}).encode(),
                        headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"}),
                    timeout=URLLIB_TIMEOUT).read())
                url = r2["upload_url"].replace("{?name,label}", f"?name={upload_name}")
            urllib.request.urlopen(urllib.request.Request(url,
                data=content,
                headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/octet-stream",
                         "Content-Length": str(len(content))}),
                timeout=300)
            log(f"Uploaded {upload_name} to Release")
        except Exception as e:
            log(f"Upload error {upload_name}: {e}")

    if outfile and os.path.exists(new_mp4 if 'new_mp4' in dir() else outfile):
        fpath = new_mp4 if 'new_mp4' in dir() else outfile
        upload(fpath, os.path.basename(fpath))
    if audiofile and os.path.exists(new_wav if 'new_wav' in dir() else audiofile):
        fpath2 = new_wav if 'new_wav' in dir() else audiofile
        upload(fpath2, os.path.basename(fpath2))

    # Trigger transcription via repository_dispatch
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

    # Initial HTTP detection
    for r in rooms:
        live, reason, url, quality = http_check_live(r['id'])
        safe_name = r['name'].encode('ascii', errors='replace').decode('ascii')
        log(f"  [{safe_name}] is_live={'ONAIR' if live else 'OFF'} ({reason})")
        if live:
            log(f"  -> stream: {quality} {url[:80]}...")
        prev_live[r['id']] = live
        aname = http_get_anchor_name(r['id'])
        if aname:
            anchor_names[r['id']] = aname
            log(f"  nickname: {aname}")
            update_rooms_nickname(anchor_names)
        time.sleep(random.uniform(6, 10))

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
                log(f"Time limit ({elapsed/3600:.1f}h) reached, exiting")
                break

            # Refresh rooms from GitHub
            if now - last_refresh > 30:
                new_rooms = load_rooms_from_github()
                for nr in new_rooms:
                    if nr["id"] not in [r["id"] for r in rooms]:
                        log(f"New room detected: {nr['id']} = {nr['name']}")
                        # Initial detection for new room
                        live, reason, url, quality = http_check_live(nr["id"])
                        prev_live[nr["id"]] = live
                        aname = http_get_anchor_name(nr["id"]) or nr["name"]
                        anchor_names[nr["id"]] = aname
                        room_names[nr["id"]] = nr["name"]
                        log(f"  [{aname}] is_live={'ONAIR' if live else 'OFF'} ({reason})")
                        if live and url:
                            proc, outfile, audio_proc, audiofile = start_recording(url, quality, nr["id"], aname)
                            recordings[nr["id"]] = {"proc": proc, "outfile": outfile, "audio_proc": audio_proc,
                                                     "audiofile": audiofile, "start": time.time()}
                        update_rooms_nickname(anchor_names)

                # Remove deleted rooms
                new_ids = {r["id"] for r in new_rooms}
                for rid in list(prev_live.keys()):
                    if rid not in new_ids:
                        log(f"Room removed: {room_names.get(rid, rid)}")
                        if rid in recordings:
                            handle_room_end(rid, recordings, anchor_names, now)
                        prev_live.pop(rid, None)
                        room_names.pop(rid, None)
                        anchor_names.pop(rid, None)

                rooms = new_rooms
                last_refresh = now

            # HTTP detection for all rooms
            for rid in sorted(prev_live.keys()):
                live, reason, url, quality = http_check_live(rid)
                prev = prev_live.get(rid)
                safe_rid = room_names.get(rid, rid).encode('ascii', errors='replace').decode('ascii')\n                log('[' + safe_rid + '] is_live=' + ('ONAIR' if live else 'OFF') + ' (' + reason + ')')
                prev_live[rid] = live

                # Just transitioned to live
                if live and rid not in recordings:
                    if url:
                        aname = anchor_names.get(rid, room_names.get(rid, rid))
                        proc, outfile, audio_proc, audiofile = start_recording(url, quality, rid, aname)
                        recordings[rid] = {"proc": proc, "outfile": outfile, "audio_proc": audio_proc,
                                            "audiofile": audiofile, "start": time.time()}

                # Check if recording ended
                if rid in recordings:
                    rec = recordings[rid]
                    proc = rec.get("proc")
                    if (proc and proc.poll() is not None) or (rid in recordings and not live):
                        handle_room_end(rid, recordings, anchor_names, now)

                time.sleep(random.uniform(6, 10))

            # Force-end recordings that exceeded max duration
            for rid in list(recordings.keys()):
                if time.time() - recordings[rid]["start"] > MAX_DURATION:
                    handle_room_end(rid, recordings, anchor_names, time.time())

            # Self-renewal check
            check_renew(elapsed)

            # Heartbeat
            if int(elapsed / 60) != int((elapsed - CHECK_INTERVAL) / 60):
                log(f'[heartbeat] running {int(elapsed/60)}min, rooms={len(prev_live)}')

            time.sleep(CHECK_INTERVAL)

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

    log("Recorder finished")


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
