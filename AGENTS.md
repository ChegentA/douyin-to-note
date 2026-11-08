# AGENTS.md - douyin-video-skill

## Purpose

分析抖音视频并生成Obsidian笔记的AI Agent技能。

## Activation Triggers

- 用户提供抖音视频链接
- 用户要求分析抖音视频
- 用户要求生成视频笔记
- 用户提到"抖音"、"视频分析"、"视频总结"、"视频笔记"

## Usage Instructions

### 基本用法

```
/douyin-video-skill <抖音链接>
```

### 完整流程

1. 解析抖音短链接获取视频ID
2. 获取视频元信息（标题、作者、时长；web → yt-dlp → 浏览器兜底，Edge 优先）
3. 下载媒体到 `%LOCALAPPDATA%\douyin-skill\<视频id>\`（优先音频流，不在 Obsidian 库内）
4. 使用SiliconFlow API转写音频（产物带 video_id 元信息校验，防串味）
5. 从脚本 stdout 的 TRANSCRIPT / VIDEO_INFO_JSON 标记块读取结果
6. 分析转写内容，提取核心概念
7. 生成Obsidian格式笔记（临时目录由脚本成功时自动清理，失败时保留并打印路径）

### 输出位置

笔记保存到 `<笔记目录>\{标题}.md`。笔记目录由用户决定：环境变量 `DOUYIN_NOTES_DIR` > 配置文件 `notes_dir`；脚本成功结束时会打印 `笔记目录: ...`。未配置时先询问用户，再用 `--set-notes-dir "<目录>"` 保存一次。示例：`D:\notes\douyin`。

### 配置（API Key）

API Key 不再写在脚本或本文件里，按以下优先级读取：

1. 环境变量 `SILICONFLOW_API_KEY`
2. 用户配置文件 `%USERPROFILE%\.douyin-skill\config.json`（字段 `siliconflow_api_key`）
3. 首次运行交互输入（仅终端 TTY），自动写入该配置文件

更换 Key：
`python "<技能目录>/scripts/douyin_analyzer.py" --set-key`

查看来源（不显示 Key 内容）：同一命令加 `--check-key`。

其他运行参数（model、temp_root、浏览器 channel、cookie 路径等）在脚本 `DouyinAnalyzer.__init__` 的 `self.config` 中调整；常用项可用环境变量覆盖（`DOUYIN_SKILL_HOME`、`DOUYIN_TEMP_ROOT`、`DOUYIN_NOTES_DIR`、`DOUYIN_LEGACY_TEMP_DIR`）；命令行可用 `--browser-channel msedge|chrome`、`--playwright-module <路径>`、`--keep`（成功后保留临时目录）。浏览器预检：`node scripts/fetch_douyin_detail.js --check`。

## Dependencies

- Python 3.8+
- requests
- SiliconFlow API Key（免费，从环境变量或用户配置文件读取，见上文）

## Reference

完整文档请参考 [SKILL.md](SKILL.md)
