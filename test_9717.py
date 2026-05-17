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

# Extract URL: find flv_pull_url area
idx = html.find("flv_pull_url")
chunk = html[idx:idx+2000]
log(f"flv_pull_url block: {chunk[:500]}")

# Find first http URL
m = re.search(r'https?://[^\s"\'\\,}\]]+', chunk)
if not m:
    log("No URL found")
    sys.exit(1)
url = m.group(0)
log(f"URL: {url[:120]}")

# Step 3: curl with ALL possible headers + dump response body
log("=== curl with full browser imitation ===")
curl_cmd = ["curl", "-v", "-o", "/tmp/flv_body.txt", "-w", "%{http_code}",
            "--max-time", "10",
            "-H", "User-Agent: " + ua,
            "-H", "Referer: https://live.douyin.com/",
            "-H", "Origin: https://live.douyin.com",
            "-H", "Accept: */*",
            "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
            "-H", "Accept-Encoding: gzip, deflate",
            "-H", "Connection: keep-alive",
            "-H", "Sec-Fetch-Dest: empty",
            "-H", "Sec-Fetch-Mode: cors",
            "-H", "Sec-Fetch-Site: cross-site",
            "-H", "Pragma: no-cache",
            "-H", "Cache-Control: no-cache",
    ]
if cookie:
    curl_cmd += ["-H", "Cookie: " + cookie]

try:
    r = subprocess.run(curl_cmd + [url], capture_output=True, timeout=15)
    log(f"curl HTTP: {r.stdout.decode().strip()}")
    if os.path.exists("/tmp/flv_body.txt"):
        sz = os.path.getsize("/tmp/flv_body.txt")
        log(f"Response size: {sz}")
        with open("/tmp/flv_body.txt", "rb") as f:
            content = f.read(500)
        log(f"Response body (first 500 bytes): {repr(content)}")
    # Also show verbose output
    log(f"Verbose: {r.stderr.decode()[-2000:]}")
except Exception as e:
    log(f"curl error: {e}")

# Step 4: Try with ttwid in URL if present
log("=== Checking for signature/token in URL ===")
if "?" in url:
    log(f"URL params: {url.split('?')[1]}")
else:
    log("No URL params")
