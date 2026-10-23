---
name: ntfy
description: 将 AI IDE 对话框（Claude Code、Codex、Antigravity）一键生成专属的手机 ntfy 双向远程控制频道。自动完成高熵专属 Topic、1 对 1 会话绑定、跨平台防休眠联动、常驻自愈监听、断线补抓与过期回执，并支持电脑与手机双向直传图片、文档与视频预览。当用户输入 /ntfy、$ntfy，或要求为当前对话框生成 ntfy、配置手机远程控制、防止休眠并监听手机指令、向手机发送图片文档或查看管理 ntfy 频道时使用。
metadata:
  dbs-handoff: |
    - 用户输入 /ntfy、$ntfy 或要求为当前对话框生成/绑定 ntfy 远程对话 → 识别宿主类型、当前会话 ID 与工作目录，运行 scripts/ntfy_ctl.py bind，交付订阅链接、防休眠状态，并原样转告输出中的 warnings
    - 收到来自手机 ntfy 频道的远程指令（或手机上传的图片/文件） → 读取指令及 .ntfy_inbox/ 下的附件，在绑定工作目录下执行任务，按指令里的说明把结果推回手机
    - 用户输入 /ntfy panel、查看所有已配置对话服务、删除某个频道（/ntfy rm 1）、暂停/恢复、清空、开关防休眠或卸载 → 运行 scripts/ntfy_ctl.py panel / rm / pause / resume / clear / sleep / uninstall 并直接展示结果
    - 不交出：跨对话框扫描串台的通配路由、将私密 Topic 泄露给第三方外网接口的默认行为、推送密钥/凭证文件
---

# ntfy：跨平台、跨 AI IDE 手机 ntfy 双向远程对话

把当前 AI 对话框变成可以用手机 `ntfy` App 随时双向沟通的远程控制室：手机发文字/图片 → 电脑上的 AI 执行 → 结果（文字、图片、文档、视频）推回手机。

---

## 不变量

1. **1 对 1 隔离**：一个频道只绑定一个会话（`conv_id`）与一个工作目录（`workspace`），不扫描其他对话框。
2. **Topic 即凭证**：Topic 是高熵随机串（不含用户名、项目名），默认只离线生成二维码，不发给任何第三方二维码网站。没有配置 `--token` 时，拿到 Topic 的人能读历史消息并向电脑发指令——绑定输出里的 `warnings` 必须原样告诉用户。
3. **最小权限执行**：Claude Code 默认 `--permission-mode acceptEdits`（可改文件、不能跑命令），Codex 默认 `--sandbox workspace-write`。只有用户明确要求时才放宽，并提醒先做访问控制。
4. **防休眠与监听生命周期绑定**：有活跃频道时维持防休眠（不强制亮屏）；电池电量低于阈值（默认 20%）时自动释放；全部解绑后自动释放。
5. **不静默丢指令**：断线后用 `?since=` 补抓；超过 300 秒的指令不执行但会回执告知手机；宿主不支持或执行失败也会回执；同一频道的指令按顺序逐条执行，单条默认超时 1800 秒。

---

## 核心脚本

脚本位于本 Skill 目录下的 [scripts/ntfy_ctl.py](scripts/ntfy_ctl.py)（仅依赖 Python 3.8+ 标准库）。下文用 `NTFY` 代指 `python3 <本 Skill 目录>/scripts/ntfy_ctl.py`（Windows 上用 `python`/`py`）。

```bash
# 1. 绑定当前对话框（显式传 --host；会话 ID 能拿到就传，拿不到可省略，脚本会读取宿主环境变量）
NTFY bind --host <claude|codex|antigravity> \
  --conv-id "<当前会话ID>" \
  --workspace "<当前工作目录绝对路径>" \
  --title "<项目中文名>"
#   可选：--server https://你的ntfy  --token tk_xxx  （强烈建议用受保护 Topic）
#         --permission-mode auto|bypassPermissions （Claude，放宽权限前先做访问控制）
#         --codex-sandbox read-only|workspace-write|danger-full-access
#         --task-timeout 3600   --ack   --keep-emoji   --rotate（轮换新 Topic）

# 2. 推送文字（多行正文用 --msg-file - 从标准输入读，避免引号和 \n 转义问题）
NTFY notify --channel "<频道ID/会话ID/Topic>" --title "<项目> | 任务完成" --msg-file - <<'EOF'
**结果**
已完成 xxx。
EOF

# 3. 推送图片/文档/视频（可传多个 --file；.env、私钥、凭证类文件会被拒绝）
NTFY notify --channel "<频道ID>" --title "<项目> | 产出文件" --msg "已生成成品图与报告" \
  --file "/abs/path/result.png" --file "/abs/path/report.md"

# 4. 全局总控面板（带 #1, #2 序号的 Markdown 表格）；status 输出 JSON
NTFY panel
NTFY status

# 5. 按序号 / 名称 / 会话 ID / Topic 管理频道
NTFY rm 1                 # 删除并解绑（同 unbind / delete），会向手机发"频道已销毁"
NTFY pause 1 ; NTFY resume 1
NTFY clear                # 清空全部频道

# 6. 防休眠 / 卸载后台服务
NTFY sleep --mode <auto|on|off> [--min-battery 20]
NTFY uninstall [--purge]
```

### 各宿主获取会话 ID

| 宿主 | `--host` | 会话 ID 来源 | 手机指令如何执行 |
|---|---|---|---|
| Claude Code | `claude` | 环境变量 `CLAUDE_CODE_SESSION_ID`（脚本自动读取） | `claude --resume <id> -p`，最终回复自动推回手机 |
| Codex | `codex` | `CODEX_THREAD_ID`（若有），否则省略 | `codex exec resume <id>`，失败则开新会话，最终回复自动推回手机 |
| Antigravity | `antigravity` | `Conversation ID` / `ANTIGRAVITY_CONVERSATION_ID` | `agentapi send-message` 投递到原对话，由 AI 自己调用 notify |
| 其他（Cursor、WorkBuddy 等） | — | — | 暂不支持从手机唤醒，只能用 `notify` 单向推送 |

拿不到会话 ID 时脚本会退回"该目录最近的会话"，并在 `warnings` 里说明，需转告用户。

---

## 标准执行流程

### 场景一：用户输入 `/ntfy` 或要求"给当前对话框生成一个 ntfy"

1. 不追问，直接确定参数：`--host`（你自己是哪个宿主）、`--conv-id`（能拿到就传）、`--workspace`（项目根目录）、`--title`（简短中文名）。
2. 运行 `bind`。
3. 向用户交付：
   - 订阅信息：Topic、手机深链 `ntfy://<server>/<topic>`、网页链接 `https://<server>/<topic>`，以及二维码路径（如已生成）；
   - 守护进程与防休眠状态，MacBook 合盖会睡眠的提醒；
   - 手机端用法：直接发文字或图片/文件；`状态` `/status`、`暂停` `/pause`、`恢复` `/resume`、`销毁频道` `/delete`、`帮助` `/help`、`/sleep on|off|auto`；
   - **安全警告（每次绑定必带）**：转述输出中的 `security_warning` 与全部 `warnings`——切勿分享、转发或截图暴露二维码、Topic 与订阅链接；泄露后立即发送 `销毁频道` 或执行 `/ntfy rm <序号>`，再 `bind --rotate`。

### 场景三：用户输入 `/ntfy panel`、`/ntfy rm 1`、`/ntfy pause 1`、`/ntfy clear`

运行对应子命令；`panel` 的输出是 Markdown 表格，直接原样展示给用户。

### 场景二：收到手机发来的远程指令

1. 若指令里有「手机随附上传的文件/图片已自动下载至本地」，先读取该文件；
2. 在绑定的工作目录下完成任务；不读取、不外发密钥文件；
3. 按指令末尾的说明回传结果：
   - Claude Code / Codex：直接写最终回复即可（会被自动推回手机）；要发文件，在回复末尾每个文件单独一行写 `NTFY_FILE: /绝对路径`（仅限工作目录内文件）；
   - Antigravity：调用指令中给出的 `notify` 命令，有产物时加 `--file`。

---

## 安全边界与免责声明

- 不读取、不输出、不通过 ntfy 上传任何密钥文件（`.env`、私钥、凭证等）。
- 默认不向第三方二维码 API 发送 Topic（`--online-qr` 需用户明确同意）。
- 手机附件只接受同一 ntfy 服务器托管的附件链接，下载到 `.ntfy_inbox/`（目录内自带 `.gitignore`）。
- 用户只要求查看状态（如 `/ntfy status`）时，只运行 `status`，不修改注册表。
- 不要把 Topic 贴到公开位置；怀疑泄露时用 `bind --rotate` 轮换。
- **免责声明**：本 Skill 用于开发者通过个人手机远程操控自己信任的本地 AI 会话。手机指令会由本地 AI 在绑定的工作目录中直接执行；因分享或泄露订阅链接/二维码、在公共信道传输机密数据或下达破坏性指令造成的损失，由使用者自行承担。
