import subprocess, sys, re, os, time, json

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
    sys.stdout.flush()

token = os.environ.get("GH_TOKEN", "")
rid = "97171913600"
ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
cookie = os.environ.get("DOUYIN_COOKIE", "")

log(f"Testing room {rid}")

import subprocess
result = subprocess.run(["curl", "-s", "-L", "--max-time", "25",
       "-H", "User-Agent: " + ua,
       "https://live.douyin.com/" + rid], capture_output=True, timeout=30)
html = result.stdout.decode("utf-8", errors="replace")
log(f"HTML size: {len(html)}")

if "flv_pull_url" not in html:
    log("NOT LIVE")
    sys.exit(0)

log("LIVE!")

# Simple extraction: just search for the string fragment
idx = html.find('"FULL_HD1"')
if idx < 0:
    idx = html.find('"HD1"')
if idx < 0:
    idx = html.find('flv_pull_url')
    # Search around that area
    chunk = html[idx:idx+500]
    # find first http 
    import re
    m = re.search(r'https?://[^\s"\'\\,}\]]+', chunk)
    if m:
        url = m.group(0)
    else:
        log("Could not find URL")
        sys.exit(1)
else:
    # find the URL after FULL_HD1
    chunk = html[idx:idx+300]
    m = re.search(r'https?://[^\s"\'\\,}\]]+', chunk)
    if m:
        url = m.group(0)
    else:
        log("Could not find URL in chunk")
        sys.exit(1)

log(f"URL: {url[:120]}")

# Step 3: Test curl
log("=== curl test ===")
curl_headers = [
    "-H", "User-Agent: " + ua,
    "-H", "Referer: https://live.douyin.com/",
    "-H", "Origin: https://live.douyin.com",
]
if cookie:
    curl_headers += ["-H", "Cookie: " + cookie]

try:
    r = subprocess.run(["curl", "-s", "-o", "/tmp/flv_data.bin", "-w", "%{http_code}",
                        "--max-time", "10"] + curl_headers + [url], capture_output=True, timeout=15)
    http_code = r.stdout.decode().strip()
    sz = os.path.getsize("/tmp/flv_data.bin") if os.path.exists("/tmp/flv_data.bin") else 0
    log(f"curl result: HTTP {http_code}, size={sz}")
    if sz > 0 and sz < 1000:
        log("First bytes:" + repr(open("/tmp/flv_data.bin","rb").read(100)))
except Exception as e:
    log(f"curl error: {e}")

# Step 4: Try ffmpeg
log("=== ffmpeg with -user_agent + headers ===")
outfile = "/tmp/test_9717_ff.mp4"
logfile = "/tmp/test_9717_ff.log"
cookie_hdr = "Cookie: " + cookie + "\\r\\n" if cookie else ""
headers_arg = "User-Agent: " + ua + "\\r\\n" + "Referer: https://live.douyin.com/\\r\\n" + cookie_hdr
proc = subprocess.Popen(
    ["ffmpeg", "-y", "-loglevel", "info", "-headers", headers_arg, "-user_agent", ua,
     "-i", url, "-c", "copy", "-t", "30", "-f", "mp4", outfile],
    stdout=subprocess.DEVNULL, stderr=open(logfile, "w"))
proc.wait(35)
with open(logfile) as f:
    print(f.read()[-2000:])
if os.path.exists(outfile):
    log(f"ffmpeg output: {os.path.getsize(outfile)} bytes")

# Step 5: Try HTTPS
log("=== ffmpeg with HTTPS URL ===")
https_url = url.replace("http://", "https://")
proc2 = subprocess.Popen(
    ["ffmpeg", "-y", "-loglevel", "info", "-headers", headers_arg, "-user_agent", ua,
     "-i", https_url, "-c", "copy", "-t", "15", "-f", "mp4", "/tmp/test_9717_https.mp4"],
    stdout=subprocess.DEVNULL, stderr=open("/tmp/ff_https.log", "w"))
proc2.wait(20)
if os.path.exists("/tmp/test_9717_https.mp4"):
    log(f"HTTPS output: {os.path.getsize('/tmp/test_9717_https.mp4')} bytes")
else:
    log("HTTPS failed")
