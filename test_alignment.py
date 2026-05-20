"""Full integrated test: ffmpeg + HTTP VC + ASS + MKV (no PW DOM needed)"""
import sys, json, os, time, urllib.request, re, subprocess
sys.stdout = open(sys.stdout.fileno(), 'w', encoding='utf-8', buffering=1)

room_id = os.environ.get('TEST_ROOM', '168465302284')
duration = int(os.environ.get('TEST_DURATION', '120'))
OUT = '/tmp/recordings'
os.makedirs(OUT, exist_ok=True)

def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}')
    sys.stdout.flush()

rec_start = time.time()

log(f'Room: {room_id}, duration: {duration}s')

# ===== STEP 1: Playwright ONCE (3s) =====
log('Step 1: Playwright get cookies...')
from playwright.sync_api import sync_playwright

api_url = None
cookie_str = None
internal_rid = room_id

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        viewport={'width': 1280, 'height': 720}
    )
    page = ctx.new_page()
    page.goto(f'https://live.douyin.com/{room_id}', wait_until='domcontentloaded', timeout=15000)
    time.sleep(3)

    cookies = ctx.cookies()
    cookie_str = '; '.join([c['name'] + '=' + c['value'] for c in cookies])
    log(f'  Cookies: {len(cookies)}')

    html = page.content()
    rid_m = list(re.finditer(r'"room_id_str"\s*:\s*"(\d+)"', html))
    if rid_m:
        internal_rid = rid_m[0].group(1)
    browser.close()

log(f'  Internal room_id: {internal_rid}')

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

# ===== STEP 2: Get FLV stream URL via same API =====
log('Step 2: Get FLV URL...')
api_headers = {
    'Accept': 'application/json, text/plain, */*',
    'Referer': f'https://live.douyin.com/{room_id}',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Cookie': cookie_str,
}
try:
    req = urllib.request.Request(api_url, headers=api_headers)
    resp = urllib.request.urlopen(req, timeout=8)
    data = json.loads(resp.read())
    d0 = data.get('data', {}).get('data', [{}])[0]
    flv_url = d0.get('stream_url', {}).get('flv_pull_url', {}).get('FULL_HD1', '')
    if not flv_url:
        flv_url = d0.get('stream_url', {}).get('flv_pull_url', {}).get('HD1', '')
    if not flv_url:
        # Try web_stream_url
        ws = data.get('data', {}).get('web_stream_url', {})
        flv_url = ws.get('flv_pull_url', {}).get('FULL_HD1', '')
    
    log(f'  FLV URL: {flv_url[:80]}...' if flv_url else '  NO FLV URL!')
    start_vc = d0.get('stats', {}).get('user_count_str', 0)
    log(f'  Initial VC: {start_vc}')
except Exception as e:
    log(f'  API error: {e}')
    flv_url = ''

os.environ['REC_START'] = str(rec_start)

# ===== STEP 3: ffmpeg recording (2 min) =====
log(f'Step 3: ffmpeg recording {duration}s...')
seg_prefix = f'{OUT}/{room_id}_seg_'
ffmpeg_cmd = [
    'ffmpeg', '-y',
    '-fflags', 'nobuffer',
    '-i', flv_url,
    '-c', 'copy',
    '-t', str(duration),
    '-f', 'segment',
    '-segment_time', '900',
    '-reset_timestamps', '1',
    f'{seg_prefix}%03d.mp4'
]

ffmpeg_proc = subprocess.Popen(
    ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
)

# ===== STEP 4: HTTP poll viewer count =====
log('Step 4: HTTP poll VC...')
vc_data = []
start = time.time()

while time.time() - start < duration:
    now = time.time()
    try:
        req = urllib.request.Request(api_url, headers=api_headers)
        resp = urllib.request.urlopen(req, timeout=8)
        d = json.loads(resp.read())
        dd = d.get('data', {}).get('data', [{}])[0]
        vc = dd.get('stats', {}).get('user_count_str', 0)
        vc_data.append({'count': vc, 'wall_ts': now, 'offset': round(now - rec_start, 1)})
    except Exception as e:
        pass
    time.sleep(3)

# Wait for ffmpeg
ffmpeg_proc.wait(timeout=10)
log(f'  ffmpeg done, exit={ffmpeg_proc.returncode}')

# ===== STEP 5: Find recorded segments =====
log('Step 5: Find segments...')
import glob
mp4_files = sorted(glob.glob(f'{seg_prefix}*.mp4'))
log(f'  Segments: {len(mp4_files)}')
for f in mp4_files:
    sz = os.path.getsize(f)
    log(f'    {os.path.basename(f)}: {sz} bytes')

if not mp4_files:
    log('No MP4 segments! Trying single output...')
    single_out = f'{OUT}/{room_id}_single.mp4'
    subprocess.run([
        'ffmpeg', '-y', '-fflags', 'nobuffer',
        '-i', flv_url, '-c', 'copy', '-t', str(duration), single_out
    ], capture_output=True, timeout=130)
    if os.path.exists(single_out):
        mp4_files = [single_out]
        log(f'  Single file: {os.path.getsize(single_out)} bytes')

if not mp4_files:
    log('FAILED: no recording files')
    sys.exit(1)

# ===== STEP 6: Generate ASS =====
log('Step 6: Generate ASS...')
seg_duration = 900
seg_idx = {os.path.basename(f).split('_seg_')[1].split('.')[0]: f for f in mp4_files}
sorted_segs = sorted(seg_idx.keys())

for seq_idx, seg_name in enumerate(sorted_segs):
    mp4_path = seg_idx[seg_name]
    seg_begin = seq_idx * seg_duration
    seg_end = seg_begin + seg_duration

    # Filter VC data for this segment
    seg_vc = [v for v in vc_data if seg_begin - 5 <= v['offset'] <= seg_end + 5]
    log(f'  Segment {seq_idx}: {len(seg_vc)} VC points ({seg_begin}s-{seg_end}s)')

    # Generate simple ASS with VC overlay
    ass_path = mp4_path.replace('.mp4', '.ass')
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write('[Script Info]\n')
        f.write('ScriptType: v4.00+\n')
        f.write('PlayResX: 1920\n')
        f.write('PlayResY: 1080\n')
        f.write('Timer: 100.0000\n\n')
        f.write('[V4+ Styles]\n')
        f.write('Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n')
        f.write('Style: vc,Arial,18,&HFFFFFF,&HFFFFFF,&H000000,&H000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n')
        f.write('\n[Events]\n')
        f.write('Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')

        for v in seg_vc:
            offset = v['offset']
            if offset < seg_begin or offset > seg_end:
                continue
            ass_ts = max(0, offset - seg_begin)
            start_time = f'{int(ass_ts//3600):01d}:{int(ass_ts%3600//60):02d}:{ass_ts%60:05.2f}'
            end_time = f'{int((ass_ts+3)//3600):01d}:{int((ass_ts+3)%3600//60):02d}:{(ass_ts+3)%60:05.2f}'
            f.write(f'Dialogue: 0,{start_time},{end_time},vc,,0,0,0,,在线人数: {v["count"]}\\N')

    # ===== STEP 7: Remux MKV =====
    mkv_path = mp4_path.replace('.mp4', '.mkv')
    log(f'  Remuxing to MKV...')
    remux = subprocess.run([
        'ffmpeg', '-y',
        '-i', mp4_path,
        '-i', ass_path,
        '-c:v', 'copy',
        '-c:a', 'copy',
        '-c:s', 'ass',
        mkv_path
    ], capture_output=True, timeout=30)

    if remux.returncode == 0:
        mkv_sz = os.path.getsize(mkv_path)
        log(f'    MKV:{os.path.basename(mkv_path)} ({mkv_sz} bytes)')
    else:
        log(f'    Remux failed: {remux.stderr.decode()[:100]}')

# ===== SUMMARY =====
log('\n=== TEST SUMMARY ===')
log(f'Room: {room_id}')
log(f'Duration: {duration}s')
log(f'Playwright cookies: {len(cookies)}')
log(f'VC polls: {len(vc_data)}')
log(f'Segments: {len(mp4_files)}')
if vc_data:
    vcs = [v['count'] for v in vc_data]
    log(f'VC range: {min(vcs)}-{max(vcs)}')
log('DONE')
