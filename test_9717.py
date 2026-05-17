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

# Extract flv_pull_url block exactly as in JSON (with \\u escapes preserved)
idx = html.find("flv_pull_url")
chunk = html[idx:idx+2000]

# Find FULL_HD1 URL and extract the ENTIRE value including \\u0026 params
m = re.search(r'FULL_HD1["\\\\]*\s*[:=]\s*["\\\\]*(http[^"\\\\]+?)["\\\\]', chunk)
if not m:
    log("Could not extract FULL_HD1 URL")
    sys.exit(1)

raw_url = m.group(1)
# Convert \\u0026 -> &, \\u003d -> =, \\/ -> /
raw_url = raw_url.replace("\\u0026", "&").replace("\\u003d", "=").replace("\\/", "/").replace("\\\\", "")
log(f"Full decoded URL: {raw_url[:200]}")

# Step 3: curl with the REAL full URL (with wsSecret & wsTime)
log("=== curl with FULL URL (including signature) ===")
curl_cmd = ["curl", "-s", "-o", "/tmp/flv_data.bin", "-w", "%{http_code}",
            "--max-time", "10",
            "-H", "User-Agent: " + ua,
            "-H", "Referer: https://live.douyin.com/",
    ]
if cookie:
    curl_cmd += ["-H", "Cookie: " + cookie]

try:
    r = subprocess.run(curl_cmd + [raw_url], capture_output=True, timeout=15)
    http_code = r.stdout.decode().strip()
    sz = os.path.getsize("/tmp/flv_data.bin") if os.path.exists("/tmp/flv_data.bin") else 0
    log(f"curl result: HTTP {http_code}, size={sz}")
    if sz > 0:
        log(f"First 100 bytes hex: {repr(open('/tmp/flv_data.bin','rb').read(100))}")
except Exception as e:
    log(f"curl error: {e}")

# Step 4: ffmpeg with the full decoded URL
log("=== ffmpeg with full URL (including signature) ===")
outfile = "/tmp/test_9717_full.mp4"
logfile = "/tmp/test_9717_full.log"
cookie_hdr = "Cookie: " + cookie + "\\r\\n" if cookie else ""
headers_arg = "User-Agent: " + ua + "\\r\\nReferer: https://live.douyin.com/\\r\\n" + cookie_hdr
proc = subprocess.Popen(
    ["ffmpeg", "-y", "-loglevel", "info",
     "-headers", headers_arg,
     "-user_agent", ua,
     "-i", raw_url,
     "-c", "copy", "-t", "30", "-f", "mp4", outfile],
    stdout=subprocess.DEVNULL, stderr=open(logfile, "w"))
proc.wait(35)
with open(logfile) as f:
    log_lines = f.read()
    log(log_lines[-1500:])
if os.path.exists(outfile):
    log(f"ffmpeg output: {os.path.getsize(outfile)} bytes")
else:
    log("ffmpeg no output")
