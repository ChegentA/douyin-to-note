---
name: douyin-video-skill
description: 分析抖音视频并生成Obsidian笔记。当用户提供抖音视频链接、要求分析抖音视频或生成视频笔记时使用。自动三级降级获取视频信息，优先音频流下载转写，无需ffmpeg。
metadata:
  version: "1.5.0"
  last_reviewed: "2026-11-08"
---

# 抖音视频转Obsidian笔记

## 概述

把抖音视频转成 Obsidian 笔记：解析链接 → 获取视频信息（三级降级）→ 下载媒体（优先音频流）→ SiliconFlow SenseVoice 转写 → AI 生成笔记。

## 获取视频信息的三级降级链（全自动）

1. **web 接口**：直连抖音 detail 接口；403/Argus 身份校验类拦截不重试、立即降级，其它失败重试 3 次（指数退避）。
2. **yt-dlp**：`--dump-single-json -f bestaudio/best`；抖音常要求 fresh cookies，失败即自动降级。
3. **浏览器兜底（默认开启）**：用本机浏览器打开视频页，捕获页面自己发出的 detail 响应；签名（a_bogus 等）与 cookie 全由浏览器处理。
   - **浏览器选择**：默认 `auto`＝先 Edge（channel=msedge）后 Chrome（channel=chrome），两者都起不来会报出各自原因；可用 `--browser-channel msedge|chrome` 手动指定。
   - **cookie 持久化**：兜底拿到的 cookie（s_v_web_id、ttwid 等）保存到 `%USERPROFILE%\.douyin-skill\cookies.json`（可用 `DOUYIN_SKILL_HOME` 改目录；权限收紧到当前用户）。下次运行时若 cookie 未过期，**先用无头模式复用 cookie 直取**（不弹窗、跳过首页预热）；被风控拦住会自动重新预热并降级到有头窗口，行为不比原来差。日志只写 cookie 名称和数量，绝不写值。
   - 关闭：命令行加 `--no-browser`（例如想让全程无窗口时）。
   - 依赖：node + playwright-core；脚本按 `--playwright-module` 参数 → `DOUYIN_PLAYWRIGHT_MODULE` 环境变量 → npx 缓存 / 技能目录 / 全局 npm 目录的顺序查找，找不到时会打印所有尝试过的路径。
   - **预检（真实拉起 Edge 和 Chrome 各一次验证可用性）**：
     `node "<技能目录>/scripts/fetch_douyin_detail.js" --check`

## 使用步骤（AI 执行）

1. 运行分析脚本（标准调用，浏览器兜底默认开启）：

   ```
   python "<技能目录>/scripts/douyin_analyzer.py" "<抖音链接>"
   ```

   可选：`--no-browser` 关闭浏览器兜底；`--browser-channel msedge|chrome` 指定浏览器；`--keep` 成功后仍保留该视频的临时目录（默认自动清理，失败时始终保留并打印路径）。
2. 从脚本 stdout 的标记块读取结果（成功时临时目录已清理，这是唯一读取来源）：
   - `========== TRANSCRIPT BEGIN (video_id=...) ==========` 与 `========== TRANSCRIPT END ==========` 之间是转写全文
   - `========== VIDEO_INFO_JSON BEGIN ==========` 与 `========== VIDEO_INFO_JSON END ==========` 之间是视频信息 JSON
   - 若是 `--keep` 或失败保留的场景，也可直接读临时目录里的 `transcript.txt` / `video_info.json`
3. 分析转写内容，生成 Obsidian 笔记，保存到 `<笔记目录>\{标题}.md`
   - 笔记目录来源：环境变量 `DOUYIN_NOTES_DIR` > 配置文件 `notes_dir`；脚本成功结束时会在末尾打印 `笔记目录: ...`，直接用它。
   - 未配置时不要自行选盘符：先问用户要存哪里，再用 `--set-notes-dir "<目录>"` 保存（只需一次）。
   - 结构：YAML front matter（created、source、tags）+ 标题 + 一句话总结 + 分节正文（背景 / 核心概念 / 论证 / 金句 / 对读者的实际含义）
   - **结构一致优先**：同一条视频重跑，允许措辞不同，但 front matter 字段集、段落骨架必须一致；章节关闭时不得多出任何新行。
4. 无需手动清理：脚本成功结束已自动删除该视频的临时目录并在日志里说明清理了什么。

## 临时目录与防串味

- 中间文件在 `%LOCALAPPDATA%\douyin-skill\<视频id>\`（每个视频一个目录，不在 Obsidian 库内，不会被同步带走）。
- 转写产物为 `transcript.txt` + 元信息 `transcript.meta.json`（video_id、标题、生成时间、模型）。读取前必校验 video_id：
  - 匹配：日志明确写「复用同一视频（id=...）的既有转写」，跳过下载与转写（同一条视频重跑时省时间）；
  - 元信息缺失或不匹配：按不存在处理并移除该文件，重新转写，**绝不静默复用别的视频的转写**。
- 成功后自动清理该视频的临时目录；失败时保留整目录并打印路径供排查。
- 旧版目录（如从旧版本升级且仍有残留）可用环境变量 `DOUYIN_LEGACY_TEMP_DIR` 指给脚本，它只提示手动删除，不自动动库内文件。

## 下载与转写说明

- 媒体选择：候选链 = 音频流（`bit_rate_audio` 等）→ 最低码率视频档 → `play_addr`；下载按顺序自动换下一个候选（含各地址的镜像），日志打印每个候选的尝试结果。13 分钟视频的音频流约 20 MB，整片约 130+ MB。
- 下载请求头使用桌面 Chrome UA + `Referer: https://www.douyin.com/`（iPhone UA 会被 403）。
- 若下载返回的是网页（Content-Type: text/html），日志会明确指出「地址被拒」。

## 平台字幕与章节（v1.5.0）

- [2/4] 拿到 detail 后先做字幕探测，**三条判据同时满足**才走字幕：
  1. `caption_infos` 存在且非空、文本非空；
  2. 单位归一后，末条 `end_time / duration >= 0.9`；
  3. 字幕字数 / 末条 end_time 秒数 `>= 2`。
- 单位自适应：默认毫秒；仅当按毫秒算覆盖率 < 0.3、按秒算 ≤ 1.2 时判为秒并乘 1000（兜底）。
- 命中：跳过 [3/4][4/4]，转写全文来自字幕；`transcript.meta.json` 的 `provider` 记 `platform_subtitle`，`model` 字段不动。
- 未命中：日志必打一行 `[字幕探测] 未命中：raw_last_end=... duration_ms=... coverage=... density=... 单位=...｜判否原因：...`，然后按原链路下载 + 转写，绝不静默退回。
- 首次命中会原样打印 `caption_infos` 前两条（确认单位用）；此后单位推断只作兜底，走兜底时打 `WARN`。
- 每次探测都追加一行统计到 `%USERPROFILE%\.douyin-skill\stats\subtitle_probe.jsonl`（aweme_id / aweme_type / duration / last_end / coverage / density / result / unit / reason / sample_tag），不依赖 stdout。
- `--sample-tag <标签>`：可选，仅写入上述统计，供以后分层采样时人工打标，不做自动分类。

## 章节锚点（写笔记时）

- `VIDEO_INFO_JSON` 的 `chapters` 是毫秒数组（title / start_ms / end_ms）；`chapter_anchors` 是已格式化的 `mm:ss 标题` 字符串，**直接使用，不要自己换算时间**。
- 正文锚点行格式固定：`> 04:12 · 章节标题`；标题照抄创作者原话，**不使用标题层级**。
- 无标题章节保留为纯时间码锚点 `> 04:12`（不写「无标题」等占位词，避免被误当成原话）；`desc` 为空时标题可能取自 `detail` 并已截到 20 字，超长会以 `…` 结尾。
- 章节丢弃/截断/同秒重复都会打 `WARN` 并记入 `chapter_stats`（raw_count / kept / dropped / warnings），不会静默处理。
- 章节边界是内容分隔，不是时间轴装饰；**不必逐章均匀展开**；**禁止把章节压成「小标题 + 两条要点」**。
- 封面/章节文案明显残缺时，只能在正文过渡句里补成分句；锚点行本身保持原样，让读者能看出哪部分是原话。
- 没有 `chapters` 时退回现有单段结构，正文**不得多出任何新行**（不加空锚点、不加占位说明）。

## 错误处理

| 阶段 | 典型现象 | 处理 |
| --- | --- | --- |
| [1/4] 解析链接失败 | 链接格式不对 | 确认是 v.douyin.com 短链或含 video/<id> 的链接 |
| [2/4] 获取视频信息失败 | 日志标明停在哪一级（web / yt-dlp / 浏览器兜底）及浏览器具体阶段（启动失败 / 页面加载失败或超时 / 未捕获到目标响应 / 响应 JSON 解析失败 / aweme_detail 为空） | 把该行日志反馈，或稍后重试一次；先用 `--check` 预检浏览器 |
| [3/4] 下载失败 | 返回网页、地址被拒 | 通常是所选流地址过期，重跑一次 |
| [4/4] 转写失败 | API 报错 / 文件过大 | 用 `--check-key` 确认 Key 来源（环境变量 / 配置文件）；需要换 Key 用 `--set-key`；再确认走的是音频流 |
| [0/4] 未找到 API Key | 非交互环境且未配置 Key | 设置环境变量 `SILICONFLOW_API_KEY`，或运行 `--set-key` 写入配置文件 |

失败时该视频的临时目录会保留并打印路径（里面有 `detail_raw.json`、`video.mp4` 等可供排查）；成功后已自动清理。

## 输出文件

- 临时文件（脚本运行期间生成，位于 `%LOCALAPPDATA%\douyin-skill\<视频id>\`，成功后自动清理）：
  - `video.mp4`（下载的媒体，多为音频流）
  - `transcript.txt` + `transcript.meta.json`（转写及归属元信息）
  - `video_info.json`
  - `detail_raw.json`（捕获到的原始详情，用于核对媒体流结构）
- 持久化文件（用户目录，长期保留；可用 `DOUYIN_SKILL_HOME` 改根目录）：
  - `%USERPROFILE%\.douyin-skill\config.json`（API Key、笔记目录）
  - `%USERPROFILE%\.douyin-skill\cookies.json`（浏览器兜底 cookie，只含名称可读的日志，值不落日志）
  - `%USERPROFILE%\.douyin-skill\transcripts\<视频id>.txt`（转写副本，防止临时目录清理后丢失）
  - `%USERPROFILE%\.douyin-skill\stats\subtitle_probe.jsonl`（字幕探测统计，一行一条）
  - `%USERPROFILE%\.douyin-skill\fixtures_raw\`（原始 detail 响应留档，供日后核对字段结构）
- 最终笔记：`<笔记目录>\{标题}.md`（由 AI 按使用步骤第 3 步生成；目录见 `notes_dir` / `DOUYIN_NOTES_DIR`）

## 依赖

- Python 3.8+ 与 requests（转写上传，timeout 600 秒）
- SiliconFlow API Key（**不再硬编码在脚本里**），按以下优先级读取：
  1. 环境变量 `SILICONFLOW_API_KEY`
  2. 用户配置文件 `%USERPROFILE%\.douyin-skill\config.json` 中的 `siliconflow_api_key`
  3. 首次运行交互式输入（仅终端 TTY 下），输入后自动写入该配置文件并尽量收紧权限（Windows 下用 `icacls` 只保留当前用户）
- 配置文件放在用户目录而不是技能目录，分享/同步技能目录时不会带出 Key。
- **更换 Key**：`python "<技能目录>/scripts/douyin_analyzer.py" --set-key`（省略值则终端隐藏输入，不进命令历史）；也可直接编辑上述配置文件。
- **查看来源**（不显示 Key 内容）：同一命令加 `--check-key`。
- **笔记目录**：`--set-notes-dir "<目录>"` 写入配置，或环境变量 `DOUYIN_NOTES_DIR` 覆盖。
- 非交互调用（stdin 不是终端，或设置了 `DOUYIN_SKILL_NO_PROMPT=1`）且未配置 Key 时，脚本会报错并给出环境变量与 `--set-key` 两种做法，不会静默失败。
- node + playwright-core（仅浏览器兜底需要）；浏览器用本机 Edge（优先）或 Chrome，不下载内核。
- 无需 ffmpeg / ffprobe

## 相关文档

激活触发与流程概览见 [AGENTS.md](AGENTS.md)。
