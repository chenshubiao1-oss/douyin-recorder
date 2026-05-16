import os, sys, json, urllib.request, urllib.parse, base64, re

token = os.environ['GH_TOKEN']
repo = os.environ['GH_REPO']

gh = {'Accept':'application/vnd.github+json','Authorization':'Bearer '+token}

# Step 1: Scan ALL releases for untranscribed .wav files
print('Scanning all releases for untranscribed audio...')
release_jobs = []  # list of (asset_dict, upload_url)
page = 1
while True:
    url = f'https://api.github.com/repos/{repo}/releases?per_page=100&page={page}'
    try:
        rels = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=gh)).read())
    except: break
    if not rels: break
    for rel in rels:
        upload_url = rel.get('upload_url', '')
        existing_names = {a['name'] for a in rel.get('assets', [])}
        for a in rel.get('assets', []):
            name = a['name']
            if not name.endswith('.wav'):
                continue
            base = name.rsplit('.', 1)[0]
            if base + '.txt' not in existing_names and base + '.srt' not in existing_names:
                release_jobs.append((a, upload_url))
    page += 1

if not release_jobs:
    print('No new audio files to transcribe')
    exit(0)

print(f'Found {len(release_jobs)} audio file(s) to transcribe')

# Step 3: Download model (once)
from funasr import AutoModel
print('Loading SenseVoiceSmall model...')
model = AutoModel(model='iic/SenseVoiceSmall', vad_model=None, punc_model=None,
                  spk_model=None, disable_update=True, device='cpu')
print('Model loaded')

# Step 4: Transcribe each
for asset, upload_url_template in release_jobs:
    try:
        name = asset['name']
        base = name.rsplit('.', 1)[0]
    download_url = asset['browser_download_url']
    wav_path = f'/tmp/{name}'
    print(f'Downloading: {name} ({asset["size"]//1024//1024} MB)')
    urllib.request.urlretrieve(download_url, wav_path)

    # Get audio duration via ffprobe
    import subprocess
    dur_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', wav_path]
    dur_result = subprocess.run(dur_cmd, capture_output=True, text=True)
    total_sec = float(dur_result.stdout.strip() or 0)
    total_sec = int(total_sec)
    print(f'Audio duration: {total_sec}s ({total_sec//60}m{total_sec%60}s)')

    # Split into 10-minute segments (600s)
    segment_sec = 600
    text_lines = []
    srt_lines = []
    srt_idx = 1
    seg_offset = 0
    seg_num = 0

    while seg_offset < total_sec:
        seg_end = min(seg_offset + segment_sec, total_sec)
        seg_path = f'/tmp/seg_{seg_num}.wav'
        seg_cmd = ['ffmpeg', '-y', '-loglevel', 'warning', '-i', wav_path,
                   '-ss', str(seg_offset), '-to', str(seg_end),
                   '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', seg_path]
        subprocess.run(seg_cmd)
        seg_len = seg_end - seg_offset
        print(f'  Segment {seg_num+1}: {seg_offset//60}m{seg_offset%60}s - {seg_end//60}m{seg_end%60}s ({seg_len}s)')

        result = model.generate(input=seg_path, cache={})
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    txt = item.get('text', '') or item.get('sentence', '') or ''
                    if txt.strip():
                        text_lines.append(txt.strip())
                        ts = item.get('timestamp', '')
                        if ts:
                            if isinstance(ts, list) and len(ts) > 0:
                                for seg in ts:
                                    if isinstance(seg, list) and len(seg) >= 3:
                                        st_ms, et_ms, seg_txt = int(seg[0]), int(seg[1]), seg[2]
                                        # Adjust timestamps by segment offset
                                        st_ms += seg_offset * 1000
                                        et_ms += seg_offset * 1000
                                        st_s = st_ms // 1000
                                        st_fmt = f'{st_s//3600:02d}:{(st_s%3600)//60:02d}:{st_s%60:02d},{st_ms%1000:03d}'
                                        et_s = et_ms // 1000
                                        et_fmt = f'{et_s//3600:02d}:{(et_s%3600)//60:02d}:{et_s%60:02d},{et_ms%1000:03d}'
                                        srt_lines.append(f'{srt_idx}\n{st_fmt} --> {et_fmt}\n{seg_txt}\n')
                                        srt_idx += 1
                elif isinstance(item, str) and item.strip():
                    text_lines.append(item.strip())
        elif isinstance(result, dict):
            txt = result.get('text', '') or result.get('sentence', '') or ''
            if txt.strip():
                text_lines.append(txt.strip())

        os.remove(seg_path)
        seg_offset = seg_end
        seg_num += 1

    os.remove(wav_path)
    if not text_lines:
        print(f'  No transcription text for {name}')
        continue

    # Upload TXT
    txt_name = base + '.txt'
    txt_text = '\n'.join(text_lines)
    upload_url = upload_url_template.replace('{?name,label}', '?name=' + urllib.parse.quote(txt_name))
    req = urllib.request.Request(upload_url,
        data=txt_text.encode('utf-8'),
        headers={**gh, 'Content-Type': 'text/plain; charset=utf-8'},
        method='POST')
    urllib.request.urlopen(req, timeout=120)
    print(f'  Uploaded: {txt_name}')

    # Upload SRT if we have timestamps
    if srt_lines:
        srt_name = base + '.srt'
        srt_text = ''.join(srt_lines)
        upload_url2 = upload_url_template.replace('{?name,label}', '?name=' + urllib.parse.quote(srt_name))
        req2 = urllib.request.Request(upload_url2,
            data=srt_text.encode('utf-8'),
            headers={**gh, 'Content-Type': 'text/plain; charset=utf-8'},
            method='POST')
        urllib.request.urlopen(req2, timeout=120)
        print(f'  Uploaded: {srt_name}')

    except Exception as e:
        print(f'  Error transcribing {name}: {e}')
        continue
    print('Transcription complete')
