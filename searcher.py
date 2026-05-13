#!/usr/bin/env python3
"""抖音搜索发现 - 独立脚本，搜索关键词直播间并更新rooms.txt"""
import os, sys, json, time, re, base64, urllib.request, urllib.error
from datetime import datetime

GH_REPO = os.environ.get("GH_REPO", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
DOUYIN_COOKIE = os.environ.get("DOUYIN_COOKIE", "")
SEARCH_KEYWORDS = [
    ("泰国", 1000),
    ("美国", 1000),
    ("日本", 1000),
    ("越南", 1000),
]

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def get_rooms():
    """从 GitHub 获取当前 rooms.txt"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        content = base64.b64decode(data["content"]).decode("utf-8")
        rooms = {}
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                rid = line.split("=")[0].strip()
            else:
                rid = line.split()[0].strip()
            if rid.isdigit():
                rooms[rid] = line
        return content, rooms, data["sha"]
    except Exception as e:
        log(f"获取rooms.txt失败: {e}")
        return "", {}, ""

def update_rooms(content, new_rooms, sha):
    """追加新房间到 rooms.txt"""
    log(f"新增 {len(new_rooms)} 个房间")
    for rid, line in new_rooms.items():
        if line not in content:
            content += line + "\n"
    
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    commit = json.dumps({
        "message": f"searcher: 新增 {len(new_rooms)} 个直播间",
        "content": b64, "sha": sha
    }).encode()
    
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
        data=commit,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
        method="PUT")
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
        log(f"rooms.txt更新成功: {resp['commit']['sha'][:7]}")
        return True
    except Exception as e:
        log(f"更新rooms.txt失败: {e}")
        return False

def search_keyword(context, keyword, min_watchers):
    """搜索关键词，返回符合条件的 (room_id, anchor_name) 列表"""
    page = context.new_page()
    try:
        url = f"https://www.douyin.com/search/{keyword}?type=live"
        log(f"打开搜索: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        
        # 提取房间链接
        hrefs = page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="live.douyin.com/"]');
            return Array.from(new Set(Array.from(links).map(a => a.href)));
        }""", timeout=15000)
        
        results = []
        for href in hrefs:
            match = re.search(r'live\.douyin\.com/(\d+)', href)
            if not match:
                continue
            rid = match.group(1)
            
            # 从搜索卡片取主播名
            anchor = page.evaluate(f"""() => {{
                const cards = document.querySelectorAll('a[href*="{rid}"]');
                for (const c of cards) {{
                    const text = c.textContent || '';
                    const match = text.match(/@([^\s]+)/);
                    if (match) return match[1];
                    const title = c.querySelector('[class*="title"]');
                    if (title) return title.textContent.trim();
                }}
                return '';
            }}""", timeout=10000)
            
            results.append((rid, anchor or rid))
        
        total = len(results)
        log(f"'{keyword}' 搜索到 {total} 个直播间")
        
        try:
            page.close()
        except:
            pass
        return results
    except Exception as e:
        log(f"'{keyword}' 搜索出错: {e}")
        try:
            page.close()
        except:
            pass
        return []

def main():
    if not DOUYIN_COOKIE:
        log("需要 DOUYIN_COOKIE 环境变量才能搜索")
        return
    if not GH_REPO or not GH_TOKEN:
        log("需要 GH_REPO 和 GH_TOKEN")
        return
    
    # 获取当前 rooms.txt
    content, existing_rooms, sha = get_rooms()
    if not sha:
        log("无法获取 rooms.txt，退出")
        return
    
    log(f"当前房间数: {len(existing_rooms)}")
    
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 720})
        
        # 注入 Cookie
        try:
            cookies = json.loads(base64.b64decode(DOUYIN_COOKIE).decode("utf-8"))
            context.add_cookies(cookies)
            log(f"Cookie注入成功 ({len(cookies)}条)")
        except Exception as e:
            log(f"Cookie注入失败: {e}")
            browser.close()
            return
        
        new_rooms = {}
        for keyword, min_watchers in SEARCH_KEYWORDS:
            results = search_keyword(context, keyword, min_watchers)
            for rid, anchor in results:
                if rid not in existing_rooms and rid not in new_rooms:
                    new_rooms[rid] = f"{rid}={anchor}(搜索:{keyword})"
        
        browser.close()
    
    if new_rooms:
        log(f"发现 {len(new_rooms)} 个新直播间")
        update_rooms(content, new_rooms, sha)
    else:
        log("没有发现新直播间")

if __name__ == "__main__":
    main()
