import os, sys, urllib.request, json, urllib.parse, glob, traceback

token = os.environ.get("GH_TOKEN", "")
repo = os.environ.get("GH_REPO", "")
run_id = os.environ.get("GH_RUN_ID", "")

if not token or not repo:
    print("Missing GH_TOKEN or GH_REPO")
    sys.exit(1)

tag = "cleanup-" + run_id
hd = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

# Try to find existing release first, or create new one
release = None
try:
    req = urllib.request.Request(
        "https://api.github.com/repos/" + repo + "/releases/tags/" + tag,
        headers=hd
    )
    resp = urllib.request.urlopen(req, timeout=30)
    release = json.loads(resp.read())
    print("Found existing release:", tag)
except Exception as e:
    print("Creating new release:", tag)
    try:
        body = json.dumps({
            "tag_name": tag, "name": tag,
            "body": "Post-cancel transcription for run " + run_id,
            "target_commitish": "main"
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/repos/" + repo + "/releases",
            data=body, headers=hd, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=30)
        release = json.loads(resp.read())
    except Exception as e2:
        print("Failed to create release:", str(e2))
        # Try to get it again (race condition)
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/" + repo + "/releases/tags/" + tag,
                headers=hd
            )
            resp = urllib.request.urlopen(req, timeout=30)
            release = json.loads(resp.read())
        except:
            traceback.print_exc()
            sys.exit(1)

if not release:
    print("No release found or created")
    sys.exit(1)

upload_url_template = release.get("upload_url", "")
if not upload_url_template:
    print("Release has no upload_url")
    sys.exit(1)

files = sorted(glob.glob("/tmp/recordings/*.txt") + glob.glob("/tmp/recordings/*.srt"))
if not files:
    print("No txt/srt files found in /tmp/recordings/")
    # Also check /tmp/transcripts/
    files = sorted(glob.glob("/tmp/transcripts/*.txt") + glob.glob("/tmp/transcripts/*.srt"))
    if files:
        print("Found files in /tmp/transcripts/ instead")

for f in files:
    if not os.path.exists(f):
        continue
    n = os.path.basename(f)
    safe = urllib.parse.quote(n.encode("utf-8"))
    upload_url = upload_url_template.replace("{?name,label}", "?name=" + safe)
    try:
        with open(f, "rb") as fh:
            data = fh.read()
        uh = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
        ureq = urllib.request.Request(upload_url, data=data, headers=uh, method="POST")
        uresp = urllib.request.urlopen(ureq, timeout=300)
        print("Uploaded:", n, os.path.getsize(f))
    except Exception as e:
        print("Upload failed:", n, str(e))

print("All uploads done")
