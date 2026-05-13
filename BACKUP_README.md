# 抖音直播录制系统 - 备份说明

## GitHub 仓库（主备份）
- **仓库**: https://github.com/tangwei880620-rgb/douyin-recorder
- **分支**: main
- **备份方式**: 所有代码已提交到 GitHub，仓库本身即为备份
- **最新提交**: 见 `git log -1` 或 GitHub 仓库

## 本地工作副本
- **路径**: C:\Users\Administrator\lobsterai\project\douyin-cloud-recorder\
- **备份方式**: GitHub 仓库是主存储，本地是工作副本

## 系统文件清单

### 核心代码
| 文件 | 说明 |
|------|------|
| `recorder.py` | 主程序：Playwright 监控 → 多房间录制 → 音频抽流 → 实时上传 Release → 自续 |
| `transcriber.py` | SenseVoiceSmall 转写：WAV → .txt / .srt |
| `requirements.txt` | Python 依赖 |

### 配置
| 文件 | 说明 |
|------|------|
| `rooms.txt` | 直播房间列表（roomId=anchorName） |
| `.github/workflows/continuous.yml` | 主工作流：缓存 → 安装依赖 → 录制 → 上传 |
| `.github/workflows/transcribe.yml` | 定时转写工作流 |
| `.github/workflows/download_model.yml` | 模型预缓存工作流 |
| `.github/workflows/monitor.yml` | 辅助监控工作流 |

### 文档
| 文件 | 说明 |
|------|------|
| `README.md` | 项目说明 |

## 关键配置参数

### GitHub Secrets（必须设置）
- 不需要单独设置 `GITHUB_TOKEN` — Actions 自动提供，需 `permissions: { contents: write, actions: write }`

### 环境变量
- `CHECK_INTERVAL`: 检查间隔（秒），默认 15
- `MAX_DURATION`: 单次录制最长（秒），默认 5h
- `OUTPUT_DIR`: 录制输出目录，默认 `/tmp/recordings`
- `TRANSCRIBE_DIR`: 转写输出目录，默认 `/tmp/transcripts`

### 工作流配置
- 定时触发: `0 1 * * *`（UTC 1:00 = 泰国 8:00）
- 超时: 355 分钟（< 6h Actions 限制）
- 自续: 350 分钟触发下一次
- 缓存 key: `deps-v3-${{ runner.os }}-${{ hashFiles('requirements.txt') }}` 回退 `deps-v3-${{ runner.os }}-` → `deps-v3-`

## 部署说明（从零克隆）
```bash
git clone https://github.com/tangwei880620-rgb/douyin-recorder.git
cd douyin-recorder
# 推送到 GitHub 即完成，Actions 自动运行
```

## 已知注意事项
1. PyTorch 需从 CPU index 安装（`--index-url https://download.pytorch.org/whl/cpu`）
2. SenseVoiceSmall 模型 ~893MB，首次运行下载到 `~/.cache/modelscope/hub/models/iic/SenseVoiceSmall`
3. 缓存 ~1.5GB（pip + torch + modelscope + playwright）
4. VAD 模型 `iic/speech_fsmn_vad_zh-cn_16k-common-pytorch` 已失效（404），已从代码移除
5. 转写结果返回 dict 格式（不是 object），代码已兼容两种格式
6. Release tag 固定为 `rec-YYYYMMDD`，每天自动创建
7. 房间动态加载：每 5 分钟从 GitHub API 重读 `rooms.txt`
8. 自续链：第 350 分钟通过 API 触发下一次 workflow
