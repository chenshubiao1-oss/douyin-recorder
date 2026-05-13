# 抖音录制系统 v1.1.0 — 备份说明

## 标签
`v1.1.0-stable` — 2026-05-13 16:21 (UTC+7)

## v1.1.0 新增功能
- 文件命名改用**主播昵称**代替房间号（`get_anchor_name()` 从页面提取）
- 支持 `test_duration` 参数（workflow_dispatch 可传）
- `restore-keys` 加 `deps-v3-` 回退，兼容缓存 key 变化

## 文件清单
| 文件 | 说明 |
|------|------|
| `recorder.py` | 主程序：Playwright → 多房间录制 → 音频抽流 → 实时上传 Release → 自续 |
| `transcriber.py` | SenseVoiceSmall 转写：WAV → .txt / .srt |
| `requirements.txt` | Python 依赖（宽松版） |
| `rooms.txt` | 10 个直播间 |
| `.github/workflows/continuous.yml` | 主工作流 |
| `.github/workflows/transcribe.yml` | 定时转写 |
| `.github/workflows/download_model.yml` | 模型预缓存 |
| `README.md` | 项目说明 |
| `BACKUP_README.md` | 备份说明文档 |

## 已知注意事项
1. VAD 模型已移除（404）
2. 缓存 ~1.5GB，两个版本同时存在（旧宽松版 + 固定版）
3. 文件名含有 `/ \ : * ? " < > |` 时自动替换为 `_`
4. **不用 `--no-deps`**（会缺 pyee 等依赖）
5. **不用 `--deps`**（用 `--with-deps`）
6. `restore-keys` 有两个回退级：`deps-v3-${{ runner.os }}-` → `deps-v3-`
