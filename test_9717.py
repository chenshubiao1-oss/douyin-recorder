import subprocess, sys, re, os, time, json, urllib.request as u, base64 as b

def log(msg):
    print(f"[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}")
    sys.stdout.flush()

token = os.environ.get("GH_TOKEN", "")
rid = "97171913600"
ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
cookie = os.environ.get("DOUYIN_COOKIE", "")

log(f"Testing room {rid}")

# Step 1: Get HTML
result = subprocess.run(["curl", "-s", "-L", "--max-time", "25",
       "-H", "User-Agent: " + ua,
       "https://live.douyin.com/" + rid], capture_output=True, timeout=30)
html = result.stdout.decode("utf-8", errors="replace")
log(f"HTML size: {len(html)}")

if "flv_pull_url" not in html:
    log("NOT LIVE")
    sys.exit(0)

log("LIVE!")

# Step 2: Extract URL
priority = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}
found = []
for m in re.finditer(r'["\\\\]+(FULL_HD1|HD1|SD1|SD2)["\\\\]+\\s*[:=]\\s*["\\\\]+(https?://[^"\\\\\\s,}\}\]>]+)', html):
    curl_url = m.group(2).replace("\\\\/", "/").replace("\\\\u0026", "&").replace("\\\\u003d", "=")
    if curl_url.startswith("http"):
        found.append((m.group(1), curl_url))

if not found:
    log("No flv URL found")
    sys.exit(1)

best = max(found, key=lambda x: priority.get(x[0], 0))
url = best[1]
log(f"URL ({best[0]}): {url[:120]}")

# Step 3: Test curl with browser-like headers (just 1 byte)
log("=== Trying curl with browser headers ===")
curl_headers = [
    "-H", "User-Agent: " + ua,
    "-H", "Referer: https://live.douyin.com/",
    "-H", "Origin: https://live.douyin.com",
]
if cookie:
    curl_headers += ["-H", "Cookie: " + cookie]

# Try with range request (just first byte)
try:
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10"]
                       + curl_headers + ["-r", "0-0", url], capture_output=True, timeout=15)
    http_code = r.stdout.decode().strip()
    log(f"Range byte test: HTTP {http_code}")
    # Also try without range
    r2 = subprocess.run(["curl", "-s", "-o", "/tmp/flv_test", "-w", "%{http_code}", "--max-time", "10"]
                        + curl_headers + ["--max-filesize", "65536", url], capture_output=True, timeout=15)
    http_code2 = r2.stdout.decode().strip()
    sz = os.path.getsize("/tmp/flv_test") if os.path.exists("/tmp/flv_test") else 0
    log(f"Direct fetch test: HTTP {http_code2}, size={sz}")
except Exception as e:
    log(f"Curl test error: {e}")

# Step 4: Try ffmpeg with user_agent argument as an alternative
log("=== Trying ffmpeg with -user_agent ===")
outfile = "/tmp/test_9717_ua.mp4"
logfile = "/tmp/test_9717_ffmpeg_ua.log"
cookie_hdr = "Cookie: " + cookie + "\r\n" if cookie else ""
headers_arg = "User-Agent: " + ua + "\r\n"
headers_arg += "Referer: https://live.douyin.com/\r\n"
headers_arg += "Origin: https://live.douyin.com\r\n"
headers_arg += "Host: pull-flv-l1.douyincdn.com\r\n"
headers_arg += cookie_hdr
proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "info",
            "-headers", headers_arg,
            "-user_agent", ua,
            "-i", url,
            "-c", "copy", "-t", "30", "-f", "mp4", outfile],
           stdout=subprocess.DEVNULL, stderr=open(logfile, "w"))

proc.wait(35)
log("=== Last 20 lines of ffmpeg log ===")
with open(logfile) as f:
    lines = f.readlines()
    for l in lines[-20:]:
        print(l.rstrip())

if os.path.exists(outfile):
    sz = os.path.getsize(outfile)
    log(f"Output: {sz} bytes")
else:
    log("No output")

# Step 5: Try HTTPS version of the URL
https_url = url.replace("http://", "https://")
log("=== Trying ffmpeg with HTTPS URL ===")
proc2 = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "info",
            "-headers", headers_arg,
            "-user_agent", ua,
            "-i", https_url,
            "-c", "copy", "-t", "15", "-f", "mp4", "/tmp/test_9717_https.mp4"],
           stdout=subprocess.DEVNULL, stderr=open("/tmp/ffmpeg_https.log", "w"))
proc2.wait(20)
if os.path.exists("/tmp/test_9717_https.mp4"):
    sz = os.path.getsize("/tmp/test_9717_https.mp4")
    log(f"HTTPS output: {sz} bytes")
else:
    log("HTTPS no output")
