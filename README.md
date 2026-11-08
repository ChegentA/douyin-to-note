# douyin-to-note

把单条抖音视频链接转成 Markdown 笔记：解析链接、获取视频信息、优先复用平台字幕，没有字幕时下载音频并调用 SiliconFlow 转写，最后输出结构化的转写与视频信息，供人或 Agent 整理成 Obsidian 笔记。

## 依赖与前提

| 依赖 | 版本 / 说明 | 是否必需 |
| --- | --- | --- |
| Python | ≥ 3.8 | 必需 |
| `requests` | 唯一的第三方 Python 包，用于上传转写 | 必需 |
| Node.js | ≥ 18 | 浏览器兜底与预检需要 |
| `playwright-core` | 只驱动本机浏览器，不下载内核 | 同上 |
| Microsoft Edge 或 Google Chrome | 已安装即可，默认先 Edge 后 Chrome | 同上 |
| yt-dlp | 可选，作为二级数据源 | 可选 |
| SiliconFlow API Key | 平台字幕未命中时用于转写 | 转写必需 |
| ffmpeg / ffprobe | **不需要**，代码中没有任何调用 | — |

Windows 上脚本通过 WSL 或 Git Bash 运行；`install.sh` 会自动从注册表补全 Windows PATH，并优先调用 `python.exe` / `node.exe`。

## 三步安装

**第一步：拿到代码并进入目录**

```bash
git clone <仓库地址> douyin-to-note
cd douyin-to-note
```

**第二步：跑安装脚本**

```bash
bash scripts/install.sh          # 已装过 playwright-core 时加 --force 可强制重装
```

脚本依次检查 Python 与 `requests`、Node ≥ 18、安装 `playwright-core` 到本目录 `node_modules/`，最后真实无头拉起 Edge 与 Chrome 各一次做预检。任一步失败会以 `[FAIL]` 中止并说明原因。

**第三步：配置 Key 与笔记目录，并自检**

```bash
# Key：省略值则终端隐藏输入，不落命令历史
python scripts/douyin_analyzer.py --set-key

# 笔记目录：换成你自己的路径
python scripts/douyin_analyzer.py --set-notes-dir "D:\notes\douyin"

# 浏览器预检（不会访问抖音）
node scripts/fetch_douyin_detail.js --check
```

预检通过时输出形如：

```
CHECK ok module=...\node_modules\playwright-core\index.js version=1.65.0 msedge=ok(157.0.4341.42) chrome=ok(156.0.8078.81)
```

## 一条命令的用法

```bash
python scripts/douyin_analyzer.py "https://www.douyin.com/video/<视频id>"
```

成功时 stdout 尾部输出两个标记块：

```
========== TRANSCRIPT BEGIN (video_id=...) ==========
...
========== TRANSCRIPT END ==========

========== VIDEO_INFO_JSON BEGIN ==========
{...}
========== VIDEO_INFO_JSON END ==========
```

脚本本身不写笔记，只负责产出转写与视频信息（含整理好的 `chapter_anchors`）；由人或 Agent 据此写到 `<笔记目录>\{标题}.md`。结束时还会打印本次解析到的笔记目录。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `url` | 单条抖音视频链接（`v.douyin.com` 短链或含 `video/<id>` 的链接） |
| `--no-browser` | 关闭浏览器兜底（默认开启） |
| `--use-browser` | 兼容参数，强制开启浏览器兜底 |
| `--browser-channel {auto,msedge,chrome}` | 浏览器选择，默认 `auto` |
| `--playwright-module PATH` | 显式指定 playwright-core 模块路径 |
| `--sample-tag TAG` | 写入字幕探测统计的采样标签，不做自动分类 |
| `--keep` | 成功后保留该视频的临时目录（默认自动清理，失败时始终保留） |
| `--set-key [KEY]` | 保存 / 替换 API Key；省略值则隐藏输入 |
| `--check-key` | 只检查 Key 来源，不显示内容 |
| `--set-notes-dir DIR` | 保存笔记输出目录 |

## 配置优先级

**API Key**（从高到低）：

1. 环境变量 `SILICONFLOW_API_KEY`
2. 配置文件 `%USERPROFILE%\.douyin-skill\config.json` 的 `siliconflow_api_key`
3. 首次运行交互输入（仅终端 TTY），输入后自动写入该配置文件

非交互环境（stdin 不是终端或设置了 `DOUYIN_SKILL_NO_PROMPT=1`）且未配置 Key 时直接报错，并提示 `--set-key` 或环境变量两种做法。

**笔记目录**：环境变量 `DOUYIN_NOTES_DIR` > 配置文件 `notes_dir`。都未配置时脚本会提示未配置，不会替用户选盘符。

其他环境变量：

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DOUYIN_SKILL_HOME` | 用户级资源目录 | `~\.douyin-skill` |
| `DOUYIN_TEMP_ROOT` | 临时目录根 | `%LOCALAPPDATA%\douyin-skill` |
| `DOUYIN_LEGACY_TEMP_DIR` | 旧版临时目录，仅用于残留提示 | 空 |
| `DOUYIN_PLAYWRIGHT_MODULE` | playwright-core 模块路径 | 空 |
| `DOUYIN_SKILL_NO_PROMPT` | 设为 `1` 禁止交互式输入 Key | 空 |

目录约定：

```
<技能目录>\
├── scripts\                  # douyin_analyzer.py / fetch_douyin_detail.js / install.sh
├── tests\                    # 纯函数测试与 fixture
├── config.example.json       # 配置样例（不含 Key）
└── node_modules\             # install.sh 安装的 playwright-core

%USERPROFILE%\.douyin-skill\
├── config.json               # Key 与笔记目录（不入库）
├── cookies.json              # 浏览器兜底 cookie（不入库）
├── transcripts\<视频id>.txt   # 转写副本
├── stats\subtitle_probe.jsonl # 字幕探测统计
└── fixtures_raw\             # 原始 detail 留档

%LOCALAPPDATA%\douyin-skill\<视频id>\   # 临时媒体，成功自动清理
```

## 已知限制与故障排除

- **仅在 Windows 验证过**：Edge `157.0.4341.42`、Chrome `156.0.8078.81`。未在 macOS / Linux 上测试，不承诺可用。
- **平台字幕命中路径未在真实视频上验证过**：目前用构造 fixture 覆盖三条判据（时间覆盖、密度、非空），真实视频若命中，请以日志和 `stats\subtitle_probe.jsonl` 为准。
- **只处理单条链接**：一次只处理一条，不提供队列或多链接模式。
- 依赖浏览器兜底时，可能需要本机已登录的 Edge/Chrome；遇风控会降级为有头窗口。
- `ffmpeg` 不是依赖；媒体候选链优先音频流，其次最低码率视频档，最后 `play_addr`。

| 现象 | 处理 |
| --- | --- |
| 解析链接失败 | 确认是 `v.douyin.com` 短链或含 `video/<id>` 的链接 |
| 获取视频信息失败 | 日志会标明停在哪一级（web / yt-dlp / 浏览器）；先跑浏览器预检 |
| 浏览器自检 `fail` | 确认已装 Edge 或 Chrome，或用 `--browser-channel msedge\|chrome` 锁定一个 |
| 找不到 playwright-core | 跑 `bash scripts/install.sh`，或用 `--playwright-module` / `DOUYIN_PLAYWRIGHT_MODULE` 指定 |
| 下载返回网页 / 地址被拒 | 所选流地址过期，重跑一次 |
| 转写失败 | `--check-key` 确认来源，`--set-key` 更换，确认走的是音频流 |
| 未找到 API Key | 设置环境变量或用 `--set-key` 写入配置文件 |

失败时该视频的临时目录会保留，里面有 `detail_raw.json`、`video.mp4` 等可供排查。

## 作为 Agent Skill 使用

仓库同时是一个 Agent Skill：`SKILL.md` 定义了触发条件、执行步骤与笔记结构，`AGENTS.md` 记录了实现约定。把整个目录放进 Agent 的技能目录（或按你的 Agent 约定挂载）后，Agent 会：

1. 解析链接并调用 `scripts/douyin_analyzer.py`；
2. 读取 stdout 的 `TRANSCRIPT` 与 `VIDEO_INFO_JSON`；
3. 使用其中现成的 `chapter_anchors` 作为正文锚点，写入配置好的笔记目录。

Agent 应优先使用脚本输出的笔记目录与章节锚点，不自行换算时间码；未配置笔记目录时先询问用户，再用 `--set-notes-dir` 保存一次。

## 免责声明与许可

本工具仅用于备份你自己收藏或有权访问的内容；请遵守抖音用户协议与相关法律法规；自动化访问可能触发平台风控，账号风险由使用者自行承担。

MIT License，见 [LICENSE](LICENSE)。
