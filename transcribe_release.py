import os, sys, json, urllib.request, urllib.parse, base64, re

token = os.environ['GH_TOKEN']
repo = os.environ['GH_REPO']
release_id = os.environ['RELEASE_ID']
release_tag = os.environ['RELEASE_TAG']

gh = {'Accept':'application/vnd.github+json','Authorization':'Bearer '+token}

# Step 1: Get release info + list all assets
release_url = f'https://api.github.com/repos/{repo}/releases/{release_id}'
rel = json.loads(urllib.request.urlopen(urllib.request.Request(release_url, headers=gh)).read())
assets = rel.get('assets', [])
upload_url_template = rel.get('upload_url', '')

existing_names = {a['name'] for a in assets}

# Step 2: Find .wav files not yet transcribed (no matching .txt/.srt)
wav_to_transcribe = []
for a in assets:
    name = a['name']
    if not name.endswith('.wav'):
        continue
    base = name.rsplit('.', 1)[0]
    if base + '.txt' not in existing_names and base + '.srt' not in existing_names:
        wav_to_transcribe.append(a)

if not wav_to_transcribe:
    print('No new audio files to transcribe')
    exit(0)

print(f'Found {len(wav_to_transcribe)} audio file(s) to transcribe')

# Step 3: Download model (once)
from funasr import AutoModel
print('Loading SenseVoiceSmall model...')
model = AutoModel(model='iic/SenseVoiceSmall', vad_model=None, punc_model=None,
                  spk_model=None, disable_update=True, device='cpu')
print('Model loaded')

# Step 4: Transcribe each
for asset in wav_to_transcribe:
    name = asset['name']
    base = name.rsplit('.', 1)[0]
    download_url = asset['browser_download_url']
    wav_path = f'/tmp/{name}'
    print(f'Downloading: {name} ({asset["size"]//1024//1024} MB)')
    urllib.request.urlretrieve(download_url, wav_path)

    print(f'Transcribing: {name}')
    result = model.generate(input=wav_path, cache={})
    text_lines = []
    srt_lines = []
    srt_idx = 1
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                txt = item.get('text', '') or item.get('sentence', '') or ''
                if txt.strip():
                    text_lines.append(txt.strip())
                    ts = item.get('timestamp', '')
                    if ts:
                        # FunASR format: [[start_ms, end_ms, text], ...]
                        if isinstance(ts, list) and len(ts) > 0:
                            for seg in ts:
                                if isinstance(seg, list) and len(seg) >= 3:
                                    st_ms, et_ms, seg_txt = int(seg[0]), int(seg[1]), seg[2]
                                    st_s = st_ms // 1000
                                    st_fmt = f'{st_s//3600:02d}:{(st_s%3600)//60:02d}:{st_s%60:02d},{st_ms%1000:03d}'
                                    et_s = et_ms // 1000
                                    et_fmt = f'{et_s//3600:02d}:{(et_s%3600)//60:02d}:{et_s%60:02d},{et_ms%1000:03d}'
                                    srt_lines.append(f'{srt_idx}\n{st_fmt} --> {et_fmt}\n{seg_txt}\n')
                                    srt_idx += 1
                else:
                    if isinstance(item, str) and item.strip():
                        text_lines.append(item.strip())
            elif isinstance(item, str) and item.strip():
                text_lines.append(item.strip())
    elif isinstance(result, dict):
        txt = result.get('text', '') or result.get('sentence', '') or ''
        if txt.strip():
            text_lines.append(txt.strip())

    if not text_lines:
        print(f'  No transcription text for {name}')
        continue

    # Upload TXT
    txt_name = base + '.txt'
    txt_text = '\n'.join(text_lines)
    upload_url = upload_url_template.replace('{?name,label}', f'?name={txt_name}')
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
        upload_url2 = upload_url_template.replace('{?name,label}', f'?name={srt_name}')
        req2 = urllib.request.Request(upload_url2,
            data=srt_text.encode('utf-8'),
            headers={**gh, 'Content-Type': 'text/plain; charset=utf-8'},
            method='POST')
        urllib.request.urlopen(req2, timeout=120)
        print(f'  Uploaded: {srt_name}')

    # Cleanup wav
    os.remove(wav_path)

print('Transcription complete')
