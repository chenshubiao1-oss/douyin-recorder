#!/usr/bin/env python3
"""抖音搜索发现 - 独立脚本，搜索关键词直播间并更新rooms.txt

   搜索原理：
   1. 打开抖音搜索页 https://www.douyin.com/search/{keyword}?type=live
   2. 注入反检测JS绕过headless检测
   3. 提取页面上直播间卡片 → 逐个打开检查在线人数
   4. 在线人数 > 阈值则收录到 rooms.txt
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
MAX_ROOMS_PER_KEYWORD = 15

ANTI_DETECT_JS = """
// 绕过 headless 检测
Object.defineProperty(navigator, 'webdriver', { get: () => false });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
// 覆盖 chrome 对象
window.chrome = { runtime: {} };
// 覆盖权限查询
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);
"""

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
    added = 0
    for rid, line in new_rooms.items():
        if rid not in content:
            content += line + "\n"
            added += 1
    if added == 0:
        log("没有新房间需要添加")
        return True
    
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    commit = json.dumps({
        "message": f"searcher: 新增 {added} 个直播间",
        "content": b64, "sha": sha
    }).encode()
    
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/contents/rooms.txt",
        data=commit,
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json"},
        method="PUT")
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
        log(f"rooms.txt更新成功: {resp['commit']['sha'][:7]} (新增{added}个)")
        return True
    except Exception as e:
        log(f"更新rooms.txt失败: {e}")
        return False

def search_one_keyword(context, keyword, min_watchers):
    """搜索一个关键词，返回 {(room_id, anchor_name, watchers)}"""
    page = context.new_page()
    try:
        # 注入反检测脚本
        page.add_init_script(ANTI_DETECT_JS)
        
        url = f"https://www.douyin.com/search/{keyword}?type=live"
        log(f"搜索: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        
        # 等待并检查是否有验证
        title = page.title()
        log(f"标题: {title}")
        if "验证" in title or "中间页" in title:
            log("⚠ 触发了验证！尝试等待+重试")
            time.sleep(5)
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                title2 = page.title()
                log(f"重试后标题: {title2}")
                if "验证" in title2 or "中间页" in title2:
                    log("无法绕过验证，跳过此关键词")
                    return []
            except:
                log("重试失败")
                return []
        
        # 滚动加载更多
        for s in range(10):
            try:
                page.evaluate(f"window.scrollBy(0, 1500)")
                time.sleep(1.5)
            except:
                pass
        
        # 页面文本分析
        try:
            body_text = page.evaluate("() => document.body.innerText")
            log(f"页面文本长度: {len(body_text)}字符")
            if len(body_text) < 50:
                log(f"页面似乎为空，内容: {repr(body_text[:100])}")
                return []
            log(f"文本预览(前300): {body_text[:300]}")
        except Exception as e:
            log(f"获取页面文本失败: {e}")
            return []
        
        # 提取所有直播间链接（多层选择器兜底）
        room_ids = set()
        selectors = [
            'a[href*="live.douyin.com/"]',
            '[href*="live.douyin.com/"]',
            'a[href*="/live/"]',
            '[data-room-id]',
            '[data-id*="live"]',
        ]
        
        for sel in selectors:
            try:
                elems = page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('{sel}');
                    const hrefs = [];
                    items.forEach(el => {{
                        const h = el.href || el.getAttribute('href') || el.getAttribute('data-room-id') || el.getAttribute('data-id') || '';
                        if (h.startsWith('/')) h = 'https://www.douyin.com' + h;
                        hrefs.push(h);
                    }});
                    return hrefs;
                }}""")
                for e in elems:
                    match = re.search(r'live\.douyin\.com/(\d+)', e)
                    if match:
                        room_ids.add(match.group(1))
            except:
                pass
        
        log(f"选择器找到 {len(room_ids)} 个房间ID")
        if room_ids:
            log(f"IDs: {list(room_ids)[:10]}")
        
        if not room_ids:
            # 最终方法：从页面文本中提取所有数字ID（可能包含房间ID）
            try:
                # 尝试匹配所有可能的房间ID（通常是很长的数字）
                ids = set(re.findall(r'(\d{12,20})', body_text))
                log(f"页面中找到 {len(ids)} 个长数字")
                for rid in list(ids)[:10]:
                    log(f"  {rid}")
            except:
                pass
        
        try: page.close()
        except: pass
        return list(room_ids)
    
    except Exception as e:
        log(f"搜索出错: {e}")
        tb.print_exc()
        try: page.close()
        except: pass
        return []

def check_room(page, rid):
    """检查直播间是否在播及在线人数"""
    try:
        page.goto(f"https://live.douyin.com/{rid}", wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        
        # 获取在线人数
        body = page.evaluate("() => document.body.innerText")
        watchers = 0
        for pattern in [r'([\d.]+)\s*万?\s*人[在正]', r'([\d,]+)\s*[人着]']:
            m = re.search(pattern, body)
            if m:
                s = m.group(1).replace(',', '')
                if '万' in s:
                    watchers = int(float(s.replace('万', '')) * 10000)
                else:
                    watchers = int(s)
                break
        
        # 判断是否在直播
        is_live = watchers > 0 or '直播间' in body or ('直播' in body and '暂无' not in body and '不存在' not in body)
        
        return is_live, watchers
    except Exception as e:
        log(f"检查房间{rid}出错: {e}")
        return False, 0

def main():
    if not DOUYIN_COOKIE:
        log("需要 DOUYIN_COOKIE 环境变量才能搜索")
        return
    if not GH_REPO or not GH_TOKEN:
        log("需要 GH_REPO 和 GH_TOKEN")
        return
    
    content, existing_rooms, sha = get_rooms()
    if not sha:
        log("无法获取 rooms.txt，退出")
        return
    
    log(f"当前房间数: {len(existing_rooms)}")
    
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        # 使用更完善的浏览器配置
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ])
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            geolocation={"latitude": 39.9042, "longitude": 116.4074},
            permissions=["geolocation"],
        )
        
        # 注入 Cookie
        try:
            cookies = json.loads(base64.b64decode(DOUYIN_COOKIE).decode("utf-8"))
            context.add_cookies(cookies)
            log(f"Cookie注入成功 ({len(cookies)}条)")
        except Exception as e:
            log(f"Cookie注入失败: {e}")
            browser.close()
            return
        
        all_new_rooms = {}
        
        for keyword, min_watchers in SEARCH_KEYWORDS:
            log(f"\n===== 搜索: {keyword} (最低{min_watchers}人) =====")
            room_ids = search_one_keyword(context, keyword, min_watchers)
            
            if not room_ids:
                log(f"'{keyword}' 没有找到房间")
                continue
            
            # 逐个检查
            checked = 0
            for rid in room_ids:
                if rid in existing_rooms or rid in all_new_rooms:
                    continue
                if not rid.isdigit() or len(rid) < 10:
                    continue
                
                log(f"检查房间 {rid}...")
                lp = context.new_page()
                try:
                    lp.add_init_script(ANTI_DETECT_JS)
                    live, watchers = check_room(lp, rid)
                    if live and watchers >= min_watchers:
                        all_new_rooms[rid] = f"{rid}={rid}(搜索:{keyword})"
                        log(f"  ✅ 收录: {rid} 在线={watchers}")
                    elif live:
                        log(f"  ⏭ 人数不足: {rid} 在线={watchers} < {min_watchers}")
                    else:
                        log(f"  ❌ 不在直播: {rid}")
                except Exception as e:
                    log(f"  检查{rid}异常: {e}")
                finally:
                    try: lp.close()
                    except: pass
                
                checked += 1
                if checked >= MAX_ROOMS_PER_KEYWORD:
                    break
        
        browser.close()
    
    if all_new_rooms:
        log(f"\n===== 共发现 {len(all_new_rooms)} 个新直播间 =====")
        update_rooms(content, all_new_rooms, sha)
    else:
        log("\n没有发现新直播间")

if __name__ == "__main__":
    main()
