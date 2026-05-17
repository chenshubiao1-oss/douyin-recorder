import subprocess, sys, re, os, time, json, urllib.request as u, base64 as b

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")
    sys.stdout.flush()

token = os.environ.get("GH_TOKEN", "")
rid = "97171913600"
ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

log(f"Testing room {rid}")

import subprocess
cmd = ["curl", "-s", "-L", "--max-time", "25",
       "-H", "User-Agent: " + ua,
       "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
       "https://live.douyin.com/" + rid]
result = subprocess.run(cmd, capture_output=True, timeout=30)
html = result.stdout.decode("utf-8", errors="replace")
log(f"HTML size: {len(html)}")

if "flv_pull_url" not in html:
    log("NOT LIVE")
    sys.exit(0)

log("LIVE!")

# Extract URL same as http_check_live
priority = {"FULL_HD1": 4, "HD1": 3, "SD1": 2, "SD2": 1}
found = []
for m in re.finditer(r'["\\]+(FULL_HD1|HD1|SD1|SD2)["\\]+\s*[:=]\s*["\\]+(https?://[^"\\\s,}\]>]+)', html):
    curl = m.group(2).replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
    if curl.startswith("http"):
        found.append((m.group(1), curl))

if not found:
    log("No flv URL found in HTML")
    sys.exit(1)

best = max(found, key=lambda x: priority.get(x[0], 0))
url = best[1]
log(f"URL ({best[0]}): {url[:120]}")

# Try recording for 90 seconds
outfile = "/tmp/test_9717.mp4"
logfile = "/tmp/test_9717_ffmpeg.log"
cookie_val = os.environ.get("DOUYIN_COOKIE", "")
cookie_hdr = "Cookie: " + cookie_val + "\r\n" if cookie_val else ""
ff_headers = ["-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\nReferer: https://live.douyin.com/\r\nOrigin: https://live.douyin.com\r\nAccept: */*\r\nHost: pull-flv-l1.douyincdn.com\r\n" + cookie_hdr]
proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "info"] + ff_headers + ["-i", url,
                          "-c", "copy", "-t", "90", "-f", "mp4", outfile],
                         stdout=subprocess.DEVNULL, stderr=open(logfile, "w"))

log(f"ffmpeg PID: {proc.pid}")
start_ts = time.time()
check_interval = 5

while True:
    time.sleep(check_interval)
    elapsed = time.time() - start_ts
    ret = proc.poll()
    if ret is not None:
        log(f"ffmpeg exited after {elapsed:.0f}s with code {ret}")
        break
    
    if elapsed >= 90:
        log(f"90s reached, stopping...")
        proc.terminate()
        proc.wait(10)
        break
    
    log(f"ffmpeg still running at {elapsed:.0f}s")

# Show ffmpeg log
log("=== Last 50 lines of ffmpeg log ===")
with open(logfile) as f:
    lines = f.readlines()
    for l in lines[-50:]:
        print(l.rstrip())

# Check file
if os.path.exists(outfile):
    sz = os.path.getsize(outfile)
    log(f"Output file: {sz} bytes")
else:
    log("Output file does not exist!")
