import subprocess, concurrent.futures, time
rooms = ['215933010618','31463759296','332875188236','344763580','636171657211','673814862790','74481436171','7819906986','877992805909','893399285676','961019695933','97171913600']
def check(rid):
    cmd = ['curl', '-s', '-L', '--max-time', '15', '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', '-H', 'Referer: https://www.douyin.com/', '-H', 'Accept: text/html,application/xhtml+xml', 'https://live.douyin.com/' + rid]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=20)
    html = r.stdout.decode('utf-8', errors='replace')
    return (rid, len(html), 'flv_pull_url' in html, 'nickname' in html, time.time()-t0)
t0 = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    results = list(ex.map(check, rooms))
print('PARALLEL TEST - Total: %.1fs' % (time.time()-t0))
print('Room,Len,flv?,nick?,Time')
for rid,l,f,n,t in results:
    print('%s,%d,%s,%s,%.1f' % (rid,l,'Y' if f else 'N','Y' if n else 'N',t))
