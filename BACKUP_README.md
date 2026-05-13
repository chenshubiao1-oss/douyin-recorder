# 抖音录制系统 v2.0.0 — 备份说明

## 标签
`v2.0.0-stable` — 2026-05-13 16:55 (UTC+7)

## v2.0.0 变更（相比 v1.1.0）
1. 文件名改用**主播昵称**代替房间号（get_anchor_name 从页面提取）
2. 支持 `test_duration` 输入参数
3. navigate_page 超时从 30s 提升到 60s + 自动重试
4. pip 安装加 `--retries 2` 避免无限重试
5. rooms.txt 恢复宽松版本号（缓存 key 兼容旧缓存）

## 当前监控房间（12个）
- 7819906986=微微在日本
- 225750654925=阿麟在东南亚
- 636171657211=夏天在越南
- 899183269388=YY
- 961019695933=麻花在泰国
- 215933010618=微微日本2
- 74481436171=麻花泰国2
- 89703986442=越南女婿
- 344763580=越南玉芬
- 988900296652=发哥钓鱼
- 823917924353=测试直播间
- 39134498648=新直播间

## 文件清单
- recorder.py — 主程序：Playwright→多房间录制→音频抽流→实时上传Release→自续
- transcriber.py — SenseVoiceSmall 转写：WAV→.txt/.srt
- requirements.txt — Python 依赖（宽松版）
- rooms.txt — 12 个直播间
- .github/workflows/continuous.yml — 主工作流
- .github/workflows/transcribe.yml — 定时转写
- .github/workflows/download_model.yml — 模型预缓存
- BACKUP_README.md — 本备份说明

## 缓存
- deps-v3-Linux-bcbd8d2b... 1.5GB (旧宽松版, key=旧requirements.txt)
- deps-v3-Linux-f2579e8f... 1.5GB (固定版, key=新requirements.txt, 已废弃)

## 已知注意事项
1. VAD 模型已移除（404，SenseVoiceSmall 无需 VAD）
2. 文件名非法字符自动替换为 `_`
3. 不用 `--no-deps`（缺 pyee 等依赖）
4. `playwright install --with-deps`（不是 `--deps`）
5. restore-keys 两级回退：`deps-v3-${{ runner.os }}-` → `deps-v3-`
6. 检测间隔 15s，页面刷新周期 5min，自续触发 350min
