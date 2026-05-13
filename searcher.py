#!/usr/bin/env python3
"""抖音搜索发现 - 纯API版（无需Playwright）
   使用抖音内部搜索API，只需要requests+cookie
"""
import os, sys, json, time, re, base64, urllib.request, urllib.error, traceback as tb
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
    added = 0
    for rid, line in new_rooms.items():
        if rid not in content:
            content += line + "\n"
            added += 1
    if added == 0:
        log("没有新房间需要添加")
        return True
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    commit = json.dumps({"message": f"searcher: 新增 {added} 个直播间",
        "content": b64, "sha": sha}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
        data=commit, headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
        method="PUT")
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
        log(f"rooms.txt更新成功: {resp['commit']['sha'][:7]} (新增{added}个)")
        return True
    except Exception as e:
        log(f"更新rooms.txt失败: {e}")
        return False

def search_douyin_api(keyword, cookie_dict):
    """直接用API搜索抖音直播间"""
    # 构建cookie字符串
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookie_dict])
    
    # 尝试多个搜索API端点
    apis = [
        f"https://www.douyin.com/aweme/v1/web/live/search/?keyword={keyword}&type=live&offset=0&count=20",
        f"https://www.douyin.com/search/{keyword}?type=live",
    ]
    
    url = apis[0]
    log(f"请求API: {url}")
    
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
    })
    
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
        log(f"API返回: {json.dumps(data)[:500]}")
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"API请求失败: {e.code} {body[:500]}")
        return None
    except Exception as e:
        log(f"API请求异常: {e}")
        return None

def main():
    if not DOUYIN_COOKIE:
        log("需要 DOUYIN_COOKIE 环境变量")
        return
    if not GH_REPO or not GH_TOKEN:
        log("需要 GH_REPO 和 GH_TOKEN")
        return
    
    # 解析cookie
    try:
        cookie_dict = json.loads(base64.b64decode(DOUYIN_COOKIE).decode("utf-8"))
        log(f"Cookie: {len(cookie_dict)}条")
    except Exception as e:
        log(f"Cookie解析失败: {e}")
        return
    
    content, existing_rooms, sha = get_rooms()
    if not sha:
        log("无法获取rooms.txt")
        return
    log(f"当前房间数: {len(existing_rooms)}")
    
    all_new = {}
    
    for keyword, min_watchers in SEARCH_KEYWORDS:
        log(f"\n===== 搜索: {keyword} =====")
        data = search_douyin_api(keyword, cookie_dict)
        
        if data is None:
            log(f"'{keyword}' API请求失败")
            continue
        
        # 解析不同格式
        rooms_found = []
        
        # 格式1: aweme/v1/web/live/search/ 返回格式
        if isinstance(data, dict):
            # 尝试多种路径
            items = data.get("data", {}).get("data", []) or data.get("data", {}).get("items", []) or data.get("data", {}).get("results", []) or data.get("items", [])
            
            if not items and isinstance(data.get("data"), list):
                items = data["data"]
            
            for item in items:
                room_id = item.get("room_id") or item.get("id") or item.get("live_room_id") or item.get("aweme_id", "")
                user_count = item.get("user_count") or item.get("watch_count") or item.get("total_user_count") or 0
                anchor = item.get("anchor_name") or item.get("nickname") or item.get("nick", "") or ""
                
                if room_id:
                    rooms_found.append((str(room_id), anchor, int(user_count)))
        
        log(f"解析到 {len(rooms_found)} 个直播间")
        
        for rid, anchor, watchers in rooms_found:
            if rid in existing_rooms or rid in all_new:
                continue
            if not rid.isdigit():
                continue
            aname = anchor or rid
            if watchers >= min_watchers:
                all_new[rid] = f"{rid}={aname}(搜索:{keyword})"
                log(f"  ✅ 收录: {rid} = {aname} 在线={watchers}")
            else:
                log(f"  ⏭ 人数不足: {rid} = {aname} 在线={watchers} < {min_watchers}")
    
    if all_new:
        log(f"\n共发现 {len(all_new)} 个新直播间")
        update_rooms(content, all_new, sha)
    else:
        log("\n没有发现新直播间")

if __name__ == "__main__":
    main()
