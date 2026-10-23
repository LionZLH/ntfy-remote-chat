---
name: ntfy
description: 将任意 AI IDE 对话框（Antigravity、Codex、Claude Code、WorkBuddy、Cursor 等）一键生成专属的手机 ntfy 双向远程控制频道。自动完成 160-bit 防扫库专属订阅连接、1 对 1 会话锁死防串台、跨平台防休眠联动、常驻自愈监听、断线补抓，并支持电脑与手机双向直传图片、文档与视频预览。当用户输入 /ntfy、$ntfy，或要求为当前对话框生成 ntfy、配置手机远程控制、防止休眠并监听手机指令、向手机发送图片文档或查看管理 ntfy 频道时使用。
metadata:
  dbs-handoff: |
    - 用户输入 /ntfy、$ntfy 或要求为当前对话框生成/绑定 ntfy 远程对话 → 识别当前会话 ID、工作目录与宿主类型，运行 scripts/ntfy_ctl.py bind 生成 160-bit 高熵 Topic、激活防休眠与常驻守护、发送测试卡片，并交付订阅链接与电源防休眠状态
    - 收到来自手机 ntfy 频道的远程指令（或手机上传的图片/文件） → 读取指令及 .ntfy_inbox/ 下的附件，在绑定工作目录下执行任务，完成后调用 scripts/ntfy_ctl.py notify（若有图片/文件产出则加 --file）将结果推回手机
    - 用户要求查看、暂停、恢复、解绑频道或开关防休眠 → 运行 scripts/ntfy_ctl.py status / unbind / sleep 并汇报实际状态
    - 不交出：跨对话框扫描串台的通配路由、将私密 Topic 泄露给第三方外网接口的默认行为
---

# ntfy：跨平台、跨 AI IDE 手机 ntfy 双向远程对话控制中心

将当前正在进行的 AI 对话框一键转化为可通过手机 `ntfy` 随时双向沟通的远程控制室。支持直接输入 `/ntfy` 或 `$ntfy` 一键触发，自动处理「防电脑休眠 $\rightarrow$ 多路常驻监听与断线补抓 $\rightarrow$ 生成 160-bit 高安全专属订阅连接 $\rightarrow$ 跨 IDE 精准唤醒 $\rightarrow$ 文字/图片/文档/视频双向收发」全链路。

---

## 不变量

1. **严格 1 对 1 物理隔离（防串台）**：一个 `ntfy` 频道唯一锁死一个会话标识（`conv_id`）与工作目录（`workspace`），严禁扫描其他活跃对话框。（依据：Capability-based Security 能力安全模型）
2. **隐私优先与极简排版**：
   - Topic 本身等同于 160-bit 免密访问令牌，默认仅离线生成二维码（不把私密 Topic 发送给任何第三方二维码生成网站）；
   - 默认收到指令后静默唤醒 AI，且发回手机的通知采用干净的 Markdown 分段排版并自动剔除装饰性 Emoji（可通过 `--ack` 与 `--keep-emoji` 按需开启）。
3. **防休眠与监听生命周期自动绑定**：只要存在至少 1 个活跃监听频道，后台守护进程自动维持防休眠锁（macOS `caffeinate -dims` / Linux `systemd-inhibit` / Windows `SetThreadExecutionState`）；全部解绑后自动释放。（依据：Lease Mechanism 租约机制）
4. **断线补抓与防重放**：持久化记录每个频道的 `last_event_id`，网络重连时携带 `?since=` 补抓闪断期间的消息，并丢弃超过 300 秒的陈旧历史消息。（依据：幂等接收与游标检查点）

---

## 核心脚本入口

所有操作通过本 Skill 目录下的 [scripts/ntfy_ctl.py](scripts/ntfy_ctl.py) 完成（仅依赖 Python 3 标准库，脚本安装后位于 `~/.agents/skills/ntfy/scripts/ntfy_ctl.py` 或当前 Skill 相对路径 `scripts/ntfy_ctl.py`）：

```bash
# 1. 为当前对话框一键生成并绑定专属 ntfy 频道（含防休眠、常驻守护与首条测试推送）
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py bind \
  --conv-id "<当前对话ID>" \
  --workspace "<当前工作目录绝对路径>" \
  --alias "<英文短别名>" \
  --title "<业务/项目中文名称>"

# 2. 任务执行完毕后，向手机推回纯文字摘要
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py notify \
  --channel "<频道ID或对话ID或Topic>" \
  --title "<项目名称> | 任务完成" \
  --msg "<极简纯文字分段Markdown摘要>"

# 3. 任务产出了图片、截图、PDF、Markdown 或视频，直接连带文件推回手机预览/下载
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py notify \
  --channel "<频道ID或对话ID或Topic>" \
  --title "<项目名称> | 产出文件预览" \
  --msg "**生成结果**\n已生成成品图与文档，请直接在下方预览或点击打开。" \
  --file "/path/to/result_image.png" \
  --file "/path/to/report.md"

# 4. 查看全局防休眠、电源状态与所有已绑定频道
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py status

# 5. 解绑指定对话框的频道（若无剩余活跃频道将自动释放防休眠锁）
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py unbind --channel "<频道ID或对话ID>"

# 6. 手动切换电脑防休眠策略（auto / on / off）
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py sleep --mode <auto|on|off>
```

---

## 双向收发「图片与文档」机制（完美兼容 iOS / Android / Web）

### 1. 电脑 $\rightarrow$ 手机（AI 产出有图片或文件怎么回复？）
当任务产出了图片、文档或视频时，调用 `notify` 子命令并传入一个或多个 `--file "<文件绝对路径>"`：
- **图片（`.png` / `.jpg` / `.webp` / `.gif`）**：通过二进制 `PUT` 直传主频道，手机 `ntfy` App 和网页端**直接在消息气泡内渲染高清大图预览**。
- **文本文档（`.md` / `.txt` / `.csv` / `.json` / `.log`）**：直接将文档内容按 Markdown 排版**内联展开在消息气泡中**（手机无需跳出 App 即可上下滑动阅读全文），并在消息底部附上 **`[点击在浏览器打开/下载原文件]`** 的直链。
- **二进制文档与压缩包（`.pdf` / `.pptx` / `.docx` / `.xlsx` / `.zip`）**：自动上传至 `<topic>_files` 寄存通道提取直链，向主频道发送带可点击蓝字链接与 `click` 跳转属性的卡片，在 iPhone 上点一下即可呼出 Safari 预览 PDF 或存入「文件」App（完美解决 iOS `ntfy` App 不显示非图片附件按钮的问题）。
- **视频文件（`.mp4` / `.mov`）**：若 $\le 14\text{ MB}$ 直接上传；若 $> 14\text{ MB}$ 且装有 `ffmpeg`，自动压制一份 $<10\text{ MB}$ 的 480p 轻量手机预览版并生成一键播放链接。

### 2. 手机 $\rightarrow$ 电脑（用户在手机上发图片或文件给 AI）
用户在手机 `ntfy` App 输入框左侧点击**回形针（上传图片/拍照/选文件）**发送至频道时：
- 守护进程自动将附件下载至绑定工作目录下的 `.ntfy_inbox/<时间戳>_<文件名>`；
- 唤醒 AI 时自动在指令中附带该文件的本地绝对路径，AI 直接读取该图片或文档即可处理。

---

## 标准执行流程

### 场景一：用户输入 `/ntfy`、`$ntfy` 或要求「给当前对话框生成一个 ntfy」

1. **提取上下文参数**（若用户只输入 `/ntfy` 无其他说明，无需追问，直接按默认参数执行绑定）：
   - **会话 ID (`--conv-id`)**：在 Antigravity 中读取 `Conversation ID`；在 Claude Code / Codex / WorkBuddy 中传入当前 Session ID 或 `<ide>-<项目名>`。
   - **工作目录 (`--workspace`)**：当前项目的根目录绝对路径。
   - **频道标题 (`--title`)**：根据当前项目目录或任务起一个简短的中文名。
2. **执行一键绑定**：运行 `ntfy_ctl.py bind`。
3. **向用户交付完整结果卡片**：
   - **专属订阅信息**：Topic 名称、手机一键唤起深链（`ntfy://ntfy.sh/<topic>`）与网页链接（`https://ntfy.sh/<topic>`）。
   - **防休眠与守护状态**：常驻服务 PID、防休眠锁 PID、电源状态，并提醒 MacBook 拔掉外接显示器物理合盖时的硬件睡眠边界。
   - **手机端用法与秒级元指令**：说明支持直接发文字、发图片/文件，以及发送 `状态` (`/status`)、`暂停` (`/pause`)、`恢复` (`/resume`)、`帮助` (`/help`) 秒级响应。

### 场景二：收到手机发来的远程指令（含图片/附件）

1. 若指令中包含 `【手机随附上传的文件/图片已自动下载至本地】`，先读取该文件；
2. 在绑定的工作目录下完成任务；
3. 调用 `ntfy_ctl.py notify` 将结果推回手机：若任务产出了新图片、新文档或新视频，**务必加上 `--file <产物路径>`**。

---

## 停止与安全边界

- 不读取、不输出、不通过 `ntfy` 上传任何密钥文件（`.env`、私钥 `.pem`、凭证等）。
- 默认不向任何第三方二维码 API 发送 Topic 字符串，确保 160-bit 频道凭证仅在本地与 `ntfy` 服务器之间流转。
- 用户仅要求查看状态（如 `/ntfy status`）时，只运行 `status` 子命令，不修改注册表。
