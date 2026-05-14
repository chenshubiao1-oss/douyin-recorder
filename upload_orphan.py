import os, sys, urllib.request, json, urllib.parse, glob

token = os.environ.get('GH_TOKEN', '')
repo = os.environ.get('GH_REPO', '')
run_id = os.environ.get('GH_RUN_ID', '')

for f in glob.glob('/tmp/recordings/*.txt') + glob.glob('/tmp/recordings/*.srt'):
    if not os.path.exists(f):
        continue
    n = os.path.basename(f)
    safe = urllib.parse.quote(n.encode('utf-8'))
    tag = 'cleanup-' + run_id
    hd = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            'https://api.github.com/repos/' + repo + '/releases',
            data=json.dumps({'tag_name': tag, 'name': tag, 'body': 'post-cancel', 'target_commitish': 'main'}).encode(),
            headers=hd, method='POST'), timeout=30).read())
    except:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(
            'https://api.github.com/repos/' + repo + '/releases/tags/' + tag, headers=hd), timeout=30).read())
    u = r['upload_url'].replace('{?name,label}', '?name=' + safe)
    with open(f, 'rb') as fh:
        data = fh.read()
    urllib.request.urlopen(urllib.request.Request(u, data=data,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream'},
        method='POST'), timeout=300)
    print('Uploaded:', n, os.path.getsize(f))
