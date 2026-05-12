#!/usr/bin/env python3
"""
百度网盘自动上传脚本
流程: refresh_token → access_token → 预上传 → 分片上传
环境变量:
  BAIDU_APP_KEY, BAIDU_SECRET_KEY, BAIDU_REFRESH_TOKEN
  BAIDU_REMOTE_DIR (可选, 默认 /apps/自动上传/抖音录制)
"""
import os
import sys
import json
import time
import hashlib
import requests

APP_KEY = os.environ["BAIDU_APP_KEY"]
SECRET_KEY = os.environ["BAIDU_SECRET_KEY"]
REFRESH_TOKEN = os.environ["BAIDU_REFRESH_TOKEN"]
REMOTE_DIR = os.environ.get("BAIDU_REMOTE_DIR", "/apps/自动上传/抖音录制")
RECORDING_DIR = "/tmp/recordings"

TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
PRECREATE_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=precreate"
UPLOAD_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/file?method=upload"
CREATE_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=create"
FILEINFO_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=filemetas"

def log(msg): print(f"[Baidu] {msg}", flush=True)

def get_access_token():
    params = {"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN,              "client_id": APP_KEY, "client_secret": SECRET_KEY}
    resp = requests.post(TOKEN_URL, params=params, timeout=30)
    data = resp.json()
    if "access_token" not in data:
        log(f"获取 token 失败: {data}")
        return None
    return data["access_token"]

def ensure_remote_dir(at, remote_path):
    parts = remote_path.strip("/").split("/")
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        params = {"access_token": at, "path": current, "size": 0, "isdir": 1, "rtype": 1}
        try:
            requests.post(CREATE_URL, params=params, headers={"User-Agent": "pan.baidu.com"}, timeout=15)
        except Exception as e:
            pass
    return remote_path

def upload_to_baidu(at, local_path, remote_path):
    size = os.path.getsize(local_path)
    fname = os.path.basename(local_path)
    log(f"上传: {fname} ({size/1024/1024:.1f}MB)")
    block_list = json.dumps(["a"*40]*((size+4194304-1)//4194304))
    params = {"access_token": at, "path": remote_path, "size": size,              "isdir": 0, "rtype": 1, "block_list": block_list, "autoinit": 1}
    resp = requests.post(PRECREATE_URL, params=params, headers={"User-Agent": "pan.baidu.com"}, timeout=15)
    pre = resp.json()
    if "uploadid" not in pre:
        log(f"预创建失败: {pre}"); return False
    upl = pre["uploadid"]
    uparams = {"method": "upload", "access_token": at, "path": remote_path, "uploadid": upl}
    with open(local_path, "rb") as f:
        files = {"file": (fname, f)}
        resp = requests.post(UPLOAD_URL, params=uparams,                            headers={"User-Agent": "pan.baidu.com"},                            files=files, timeout=3600)
    result = resp.json()
    if "md5" in result:
        log(f"OK! md5={result[\"md5\"]}"); return True
    else:
        log(f"失败: {result}"); return False

def main():
    if not os.path.isdir(RECORDING_DIR):
        log(f"目录不存在: {RECORDING_DIR}"); return
    files = sorted(f for f in os.listdir(RECORDING_DIR) if f.endswith(".mp4"))
    if not files:
        log("没有找到录制文件，跳过上传"); return
    at = get_access_token()
    if not at: sys.exit(1)
    ensure_remote_dir(at, REMOTE_DIR)
    ok = 0
    for f in files:
        if upload_to_baidu(at, os.path.join(RECORDING_DIR, f), f"{REMOTE_DIR}/{f}"):
            ok += 1
    log(f"完成: {ok}/{len(files)} 成功")

if __name__ == "__main__":
    main()
