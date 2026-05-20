"""Full test: PW open (danmaku+VC) + HTTP VC + cross-correlation + MKV"""
import sys, json, os, time, urllib.request, re, subprocess
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '168465302284')
duration = int(os.environ.get('TEST_DURATION', '120'))
OUT = '/tmp/recordings'
os.makedirs(OUT, exist_ok=True)
rec_start = time.time()

def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')
    sys.stdout.flush()

log(f'Room: {room_id}, duration: {duration}s')

# ===== STEP 1: Playwright open (stay open) =====
log('Step 1: Playwright start...')
from playwright.sync_api import sync_playwright
pw_instance = sync_playwright()
p = pw_instance.__enter__()
browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
ctx = browser.new_context(
    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    viewport={'width': 1280, 'height': 720}
)
page = ctx.new_page()
page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)

# Get cookies and internal room_id
time.sleep(4)
cookies = ctx.cookies()
cookie_str = '; '.join([c['name'] + '=' + c['value'] for c in cookies])
html = page.content()
rid_m = list(re.finditer(r'"room_id_str"\s*:\s*"(\d+)"', html))
internal_rid = rid_m[0].group(1) if rid_m else room_id
log(f'  Cookies: {len(cookies)}, internal_rid: {internal_rid}')

# Build API URL
from urllib.parse import urlencode
api_params = {
    'aid': '6383', 'app_name': 'douyin_web', 'live_id': '1',
    'device_platform': 'web', 'language': 'zh-CN', 'enter_from': 'link_share',
    'cookie_enabled': 'true', 'screen_width': '1280', 'screen_height': '720',
    'browser_language': 'zh-CN', 'browser_platform': 'Win32',
    'browser_name': 'Chrome', 'browser_version': '120.0.0.0',
    'os_name': 'Windows', 'os_version': '10',
    'web_rid': room_id, 'room_id_str': internal_rid,
    'is_need_double_stream': 'false',
}
api_url = 'https://live.douyin.com/webcast/room/web/enter/?' + urlencode(api_params)

# ===== STEP 2: Get FLV URL =====
log('Step 2: Get FLV URL...')
api_headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Cookie': cookie_str,
}
req = urllib.request.Request(api_url, headers=api_headers)
data = json.loads(urllib.request.urlopen(req, timeout=8).read())
d0 = data.get('data', {}).get('data', [{}])[0]
flv_url = d0.get('stream_url', {}).get('flv_pull_url', {}).get('FULL_HD1', '') or \
          d0.get('stream_url', {}).get('flv_pull_url', {}).get('HD1', '') or \
          data.get('data', {}).get('web_stream_url', {}).get('flv_pull_url', {}).get('FULL_HD1', '')
log(f'  FLV: {flv_url[:60]}...')

# ===== STEP 3: ffmpeg recording =====
log('Step 3: ffmpeg recording...')
seg_prefix = f'{OUT}/{room_id}_seg_'
ffmpeg = subprocess.Popen([
    'ffmpeg', '-y', '-fflags', 'nobuffer',
    '-i', flv_url, '-c', 'copy', '-t', str(duration),
    '-f', 'segment', '-segment_time', '900', '-reset_timestamps', '1',
    f'{seg_prefix}%03d.mp4'
], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

# ===== STEP 4: PW + HTTP simultaneous collection =====
log('Step 4: Collecting data...')
data_records = []  # {source, vc, danmaku, wall_ts, offset}
seen_dm = set()
start = time.time()

while time.time() - start < duration:
    now = time.time()
    offset = round(now - rec_start, 1)

    # --- HTTP VC poll ---
    http_vc = None
    try:
        req = urllib.request.Request(api_url, headers=api_headers)
        d = json.loads(urllib.request.urlopen(req, timeout=8).read())
        dd = d.get('data', {}).get('data', [{}])[0]
        http_vc = dd.get('stats', {}).get('user_count_str')
        if http_vc is None:
            http_vc = dd.get('user_count_str')
    except:
        pass

    # --- PW VC + danmaku ---
    pw_vc = None
    pw_dms = []
    try:
        ct = page.evaluate('''() => {
            var vc = document.querySelector("[data-e2e=live-room-audience]");
            var v = vc ? vc.textContent.trim() : null;
            var chat = document.querySelector("[class*=chatroom]");
            var dms = [];
            if (chat) {
                var divs = chat.querySelectorAll(":scope > div");
                for (var d of divs) {
                    var t = d.textContent.trim();
                    if (t && t.indexOf("：") >= 0) dms.push(t);
                }
            }
            return {vc: v, dms: dms};
        }''')
        if ct:
            pw_vc = ct.get('vc')
            for dm_text in (ct.get('dms') or []):
                if dm_text and dm_text not in seen_dm:
                    pw_dms.append({'text': dm_text, 'wall_ts': now, 'offset': offset})
                    seen_dm.add(dm_text)
    except:
        pass

    rec = {
        'wall_ts': now, 'offset': offset,
        'pw_vc': pw_vc, 'http_vc': http_vc,
        'pw_dms': pw_dms,
    }
    data_records.append(rec)
    time.sleep(1.0)

# Wait for ffmpeg
ffmpeg.wait(timeout=10)
log(f'  ffmpeg exit={ffmpeg.returncode}')

# Collect all danmaku
all_dms = []
for r in data_records:
    for dm in r.get('pw_dms', []):
        all_dms.append(dm)

# ===== STEP 5: Save raw data =====
log('Step 5: Analyze time alignment...')
# Extract VC time series
http_vcs = [(r['offset'], int(r['http_vc'])) for r in data_records if r.get('http_vc') and isinstance(r['http_vc'], int)]
pw_vcs_raw = []
for r in data_records:
    v = r.get('pw_vc')
    if v:
        v_str = str(v).replace(',', '')
        if '万' in v_str:
            v_int = int(float(v_str.replace('万', '')) * 10000)
        else:
            try:
                v_int = int(v_str)
            except:
                continue
        pw_vcs_raw.append((r['offset'], v_int))

log(f'HTTP VC points: {len(http_vcs)}')
log(f'PW VC points: {len(pw_vcs_raw)}')
log(f'Danmaku: {len(all_dms)}')

# Show VC comparison (first 20 matching)
if http_vcs and pw_vcs_raw:
    log('\nVC comparison (PW vs HTTP):')
    min_len = min(20, len(http_vcs), len(pw_vcs_raw))
    for i in range(min_len):
        ht = http_vcs[i]
        pt = pw_vcs_raw[i]
        match = '✓' if ht[1] == pt[1] else '✗'
        log(f'  t={ht[0]:.0f}s HTTP={ht[1]} PW={pt[1]} {match}')

# Show danmaku samples
if all_dms:
    log(f'\nDanmaku ({len(all_dms)} total):')
    for dm in all_dms[:10]:
        log(f'  t={dm["offset"]:.0f}s: {dm["text"][:50]}')

# ===== STEP 6: Generate ASS =====
log('\nStep 6: Generate ASS...')
import glob
mp4_files = sorted(glob.glob(f'{seg_prefix}*.mp4'))
if not mp4_files:
    log('No segments found, trying single output...')
    single = f'{OUT}/{room_id}_single.mp4'
    subprocess.run(['ffmpeg','-y','-i',flv_url,'-c','copy','-t',str(duration),single], capture_output=True, timeout=130)
    if os.path.exists(single):
        mp4_files = [single]

log(f'  Segments: {len(mp4_files)}')

for seq_idx, mp4_path in enumerate(mp4_files):
    seg_begin = seq_idx * 900
    seg_end = seg_begin + 900
    seg_vcs = [v for v in http_vcs if seg_begin - 5 <= v[0] <= seg_end + 5]
    seg_dms = [d for d in all_dms if seg_begin - 5 <= d['offset'] <= seg_end + 5]
    
    log(f'  Seg {seq_idx}: {len(seg_vcs)} VC, {len(seg_dms)} DM')
    
    ass_path = mp4_path.replace('.mp4', '.ass')
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write('[Script Info]\n')
        f.write('ScriptType: v4.00+\n')
        f.write('PlayResX: 1920\n')
        f.write('PlayResY: 1080\n')
        f.write('Timer: 100.0000\n\n')
        f.write('[V4+ Styles]\n')
        f.write('Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n')
        f.write('Style: vc,Arial,17,&HFFFFFF,&HFFFFFF,&H000000,&H000000,0,0,0,0,100,100,0,0,1,2,0,8,10,10,10,1\n')
        f.write('Style: dm,Arial,22,&HFFFFFF,&HFFFFFF,&H0000FF,&H000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n')
        f.write('\n[Events]\n')
        f.write('Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
        
        # VC overlay at top
        for v in seg_vcs:
            ass_ts = max(0, v[0] - seg_begin)
            st = f'{int(ass_ts//3600):01d}:{int(ass_ts%3600//60):02d}:{ass_ts%60:05.2f}'
            et = f'{int((ass_ts+3)//3600):01d}:{int((ass_ts+3)%3600//60):02d}:{(ass_ts+3)%60:05.2f}'
            f.write(f'Dialogue: 1,{st},{et},vc,,0,0,0,,{{\\move(960,30,960,30)}}在线人数: {v[1]}\\N')
        
        # Danmaku push-up style
        for i, dm in enumerate(seg_dms):
            ass_ts = max(0, dm['offset'] - seg_begin)
            st = f'{int(ass_ts//3600):01d}:{int(ass_ts%3600//60):02d}:{ass_ts%60:05.2f}'
            et = f'{int((ass_ts+5)//3600):01d}:{int((ass_ts+5)%3600//60):02d}:{(ass_ts+5)%60:05.2f}'
            f.write(f'Dialogue: 0,{st},{et},dm,,0,0,0,,{dm["text"]}\\N')
    
    # Remux MKV
    mkv_path = mp4_path.replace('.mp4', '.mkv')
    remux = subprocess.run([
        'ffmpeg', '-y', '-i', mp4_path, '-i', ass_path,
        '-c:v', 'copy', '-c:a', 'copy', '-c:s', 'ass', mkv_path
    ], capture_output=True, timeout=30)
    if remux.returncode == 0:
        log(f'    MKV: {os.path.basename(mkv_path)} ({os.path.getsize(mkv_path)} bytes)')
    else:
        log(f'    Remux failed: {remux.stderr.decode()[:100]}')

# Close Playwright
browser.close()
pw_instance.__exit__(None, None, None)

# ===== SUMMARY =====
log('\n=== SUMMARY ===')
log(f'Room: {room_id}')
log(f'Duration: {duration}s')
log(f'Cookies: {len(cookies)}')
log(f'HTTP VC: {len(http_vcs)}')
log(f'PW VC: {len(pw_vcs_raw)}')
log(f'Danmaku: {len(all_dms)}')
log(f'Segments: {len(mp4_files)}')
log('DONE')
