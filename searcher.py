#!/usr/bin/env python3
"""抖音搜索发现 - 独立脚本，搜索关键词直播间并更新rooms.txt
   
   搜索原理：
   1. 打开抖音搜索页 https://www.douyin.com/search/{keyword}?type=live
   2. 等待搜索结果渲染，滚动页面确保前N个直播间加载
   3. 提取页面上所有房间卡片 → 拿到 room_id 
   4. 逐个打开直播间页面，获取在线人数
   5. 在线人数 > 阈值则收录到 rooms.txt
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
MAX_ROOMS_PER_KEYWORD = 15  # 每个关键词最多检查前15个

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
        if line not in content and rid not in content:
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

def wait_and_extract(page, keyword):
    """等待搜索结果渲染，提取直播间卡片信息"""
    results = {}  # room_id -> {"anchor": str, "watchers": int}
    
    try:
        # 等待页面加载
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        time.sleep(5)
        
        # 滚动页面强制懒加载
        for scroll in range(5):
            try:
                page.evaluate("window.scrollBy(0, 2000)")
                time.sleep(2)
            except:
                pass
        
        # 方法1: 尝试从页面文本提取所有直播间信息
        # 抖音搜索页的直播间卡片通常有 "xxx人在看" 的文本
        try:
            html = page.content()
            body_text = page.evaluate("() => document.body.innerText")
            log(f"页面文本长度: {len(body_text)}字符")
            # 打印前500字符看看页面结构
            log(f"页面头部文本: {body_text[:500]}")
        except:
            pass
        
        # 方法2: 通过JS提取所有房间卡片
        try:
            cards = page.evaluate("""() => {
                const items = document.querySelectorAll('[class*="room"], [class*="card"], [class*="live"], [class*="search"]');
                const data = [];
                items.forEach(el => {
                    const text = el.textContent || '';
                    const href = el.closest('a')?.href || el.querySelector('a')?.href || '';
                    const match = href.match(/live\\.douyin\\.com\\/(\\d+)/);
                    if (match) {
                        data.push({href, text: text.substring(0, 200)});
                    }
                });
                return data;
            }""")
            log(f"方法2: 找到 {len(cards)} 个含live.douyin.com的元素")
            for c in cards[:5]:
                log(f"  href={c['href']}")
        except Exception as e:
            log(f"方法2出错: {e}")
        
        # 方法3: 尝试获取所有href中包含live.douyin.com的链接
        try:
            links = page.evaluate("""() => {
                const all = document.querySelectorAll('[href*="live.douyin.com/"]');
                const hrefs = Array.from(all).map(a => a.href || a.getAttribute('href') || '');
                return [...new Set(hrefs)];
            }""")
            log(f"方法3: 找到 {len(links)} 个链接")
            for l in links[:5]:
                log(f"  链接: {l}")
        except Exception as e:
            log(f"方法3出错: {e}")
        
        # 方法4: 获取页面上所有数字格式的文本（可能包含直播间人数）
        try:
            nums = page.evaluate("""() => {
                const texts = document.body.innerText.match(/[\\d,.]+[人看]/g) || [];
                return texts.slice(0, 20);
            }""")
            if nums:
                log(f"方法4: 人数文本: {nums}")
        except:
            pass
        
        # 方法5: 从搜索页面API数据中提取
        # 抖音搜索页的数据通常在script标签或有专门的API响应
        try:
            scripts = page.evaluate("""() => {
                const result = [];
                document.querySelectorAll('script').forEach(s => {
                    const t = (s.textContent || '').substring(0, 500);
                    if (t.includes('room_id') || t.includes('live.douyin')) {
                        result.push(t);
                    }
                });
                return result;
            }""")
            log(f"方法5: 找到 {len(scripts)} 个包含room_id的script")
            for s in scripts[:3]:
                log(f"  script: {s[:300]}")
        except:
            pass
        
        return results
    except Exception as e:
        log(f"提取搜索页出错: {e}")
        tb.print_exc()
        return results

def check_room_live(page, rid):
    """打开直播间页面，检查是否在直播并获取人数"""
    try:
        url = f"https://live.douyin.com/{rid}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        
        # 检查是否直播
        try:
            live_text = page.evaluate("""() => {
                const el = document.querySelector('[class*="live"], [class*="living"], .is-live-pc');
                if (el) return true;
                // 检查标题是否包含直播
                const title = document.title || '';
                return title.includes('直播间') && !title.includes('暂无') && !title.includes('不存在');
            }""")
        except:
            live_text = False
        
        # 获取在线人数
        watchers = 0
        try:
            body = page.evaluate("() => document.body.innerText")
            # 找 "xxx人在看" 或 "xxx人" 模式
            matches = re.findall(r'([\d.]+)\s*万?\s*人[在正]?[看直]', body)
            if matches:
                watchers = parse_watchers(matches[0])
            else:
                # 找纯数字+人在看
                matches2 = re.findall(r'(\d[\d,]*)\s*[人着][在正]?[看直]', body)
                if matches2:
                    watchers = int(matches2[0].replace(',', ''))
                else:
                    # debug:打印页面文本前300字
                    log(f"  页面文本(前300): {body[:300]}")
        except:
            pass
        
        return live_text, watchers
    except Exception as e:
        log(f"检查直播间{rid}出错: {e}")
        return False, 0

def parse_watchers(s):
    """解析人数字符串"""
    s = s.strip().replace(',', '')
    if '万' in s:
        try:
            return int(float(s.replace('万', '')) * 10000)
        except:
            return 0
    try:
        return int(s)
    except:
        return 0

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
        
        all_new_rooms = {}
        
        for keyword, min_watchers in SEARCH_KEYWORDS:
            log(f"\n===== 搜索关键词: {keyword} (最低{min_watchers}人) =====")
            try:
                search_page = context.new_page()
                url = f"https://www.douyin.com/search/{keyword}?type=live"
                log(f"打开搜索页: {url}")
                search_page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                
                # 滚动页面，让搜索结果充分加载
                log("滚动页面加载更多...")
                for s in range(8):
                    try:
                        search_page.evaluate(f"window.scrollBy(0, 2000)")
                        time.sleep(2)
                    except:
                        pass
                
                # 分析页面结构
                log("分析页面结构...")
                try:
                    text = search_page.evaluate("() => document.body.innerText")
                    log(f"页面文本长度: {len(text)}字符")
                    log(f"页面开头(前500): {text[:500]}")
                except:
                    pass
                
                # 尝试多种解析方式获取直播间信息
                all_cards = []
                
                # 方式A: 提取所有链接
                try:
                    links = search_page.evaluate("""() => {
                        const hrefs = [];
                        document.querySelectorAll('a[href]').forEach(a => {
                            const h = a.href || '';
                            if (h.includes('live.douyin.com/')) {
                                hrefs.push(h);
                            }
                        });
                        return [...new Set(hrefs)];
                    }""")
                    log(f"方式A: 找到 {len(links)} 个直播间链接")
                    if links:
                        for l in links[:5]:
                            log(f"  {l}")
                except Exception as e:
                    log(f"方式A出错: {e}")
                    links = []
                
                # 方式B: 提取所有script标签中的room_id
                try:
                    scripts = search_page.evaluate("""() => {
                        const result = [];
                        document.querySelectorAll('script[id], script[type]').forEach(s => {
                            const t = (s.textContent || '').substring(0, 1000);
                            if (t.includes('room_id')) {
                                result.push(t);
                            }
                        });
                        return result;
                    }""")
                    log(f"方式B: 找到 {len(scripts)} 个含room_id的script")
                    for s in scripts[:3]:
                        log(f"  script: {s[:400]}")
                except:
                    pass
                
                # 方式C: 从links里提取room_id，逐个点击进入直播间检查
                room_ids_found = set()
                for href in links:
                    match = re.search(r'live\.douyin\.com/(\d+)', href)
                    if match:
                        room_ids_found.add(match.group(1))
                
                log(f"共发现 {len(room_ids_found)} 个未重复直播间ID")
                
                if room_ids_found:
                    log(f"房间ID: {list(room_ids_found)[:10]}")
                    
                    # 逐个打开检查
                    count = 0
                    for rid in list(room_ids_found)[:MAX_ROOMS_PER_KEYWORD]:
                        if rid in existing_rooms or rid in all_new_rooms:
                            continue
                        if rid.startswith("0") or not rid:
                            continue
                        
                        log(f"  检查房间 {rid}...")
                        try:
                            lp = context.new_page()
                            live, watchers = check_room_live(lp, rid)
                            try: lp.close()
                            except: pass
                            
                            if live and watchers >= min_watchers:
                                all_new_rooms[rid] = f"{rid}={rid}(搜索:{keyword})"
                                log(f"  ✅ 收录: {rid} 在线人数={watchers}")
                            elif live:
                                log(f"  ⏭ 人数不足: {rid} 在线人数={watchers} < {min_watchers}")
                            else:
                                log(f"  ❌ 不在直播: {rid}")
                        except Exception as e:
                            log(f"  检查{rid}异常: {e}")
                        
                        count += 1
                        if count >= MAX_ROOMS_PER_KEYWORD:
                            break
                else:
                    log("没有找到任何直播间链接，尝试方法D: 直接搜索页面文本中的room_id...")
                    try:
                        body = search_page.evaluate("() => document.body.innerText")
                        # 在文本中找可能包含的直播间信息
                        # 打印更多页面内容帮助调试
                        log(f"页面全文(前1500字符): {body[:1500]}")
                    except:
                        pass
                
                try: search_page.close()
                except: pass
                
            except Exception as e:
                log(f"关键词'{keyword}'搜索异常: {e}")
                tb.print_exc()
                try: search_page.close()
                except: pass
        
        browser.close()
    
    if all_new_rooms:
        log(f"\n===== 共发现 {len(all_new_rooms)} 个新直播间 =====")
        update_rooms(content, all_new_rooms, sha)
    else:
        log("\n没有发现新直播间")

if __name__ == "__main__":
    main()
