# ntfy-remote-chat (`/ntfy`)

把 AI IDE 对话框（Claude Code、Codex、Antigravity）一键变成手机 `ntfy` 双向远程控制频道：手机发文字或图片 → 电脑上的 AI 执行 → 结果（文字、图片、文档、视频）推回手机。自带全局总控面板、跨平台防休眠、常驻自愈监听、断线补抓与失败回执。

> [!CAUTION]
> **核心安全警示：切勿向任何人分享、转发或截图暴露你的二维码、Topic 名称与订阅链接！**
>
> 专属的 `ntfy` 订阅链接和二维码**等同于你电脑上当前 AI 对话框的「免密远程控制密钥」**。拿到它的人不仅能看到 AI 发回手机的所有通知、图片与文档，还能**通过手机向你电脑上的 AI 发送指令**。
>
> 如果不慎在截图或群聊中暴露，请立即：
> 1. 在手机端发送 `销毁频道`，或在电脑端执行 `/ntfy rm <序号>`；
> 2. 再用 `bind --rotate` 换一个新的 Topic。
>
> 推荐使用带访问令牌的受保护 Topic（见下方「安全须知」）。

---

## 一行命令安装

```bash
npx -y skills add LionZLH/ntfy-remote-chat -g --all
```

---

## 快速配置教程（30 秒上手）

### 第一步：在电脑 AI IDE 对话框中一键开启

在正在进行的对话框中直接输入：

```text
/ntfy
```

（支持 `$` 呼出的环境可以输入 `$ntfy`。）

AI 会自动完成以下几步：

1. 生成 28 位随机专属 Topic，约 166 bits，不含用户名和项目名，例如 `nrc_9xK2mP7vQ4nL8wR1tY5uI0oP3aS6`；
2. 把该 Topic 与当前对话框的会话 ID、工作目录 **1 对 1 绑定**，多开窗口互不串台；
3. 启动后台守护进程，并开启防休眠：
   - macOS 用 `launchd` 守护 + `caffeinate -ims` 防休眠；
   - Linux 用 `systemd --user` 守护 + `systemd-inhibit` 防休眠；
   - Windows 用登录自启 + `SetThreadExecutionState` 防休眠；
4. 向该频道发送第一条"连接就绪"卡片，并提示你需要注意的安全事项。

### 第二步：手机端订阅该频道

先在手机上安装免费开源的 **ntfy** App（iOS App Store / Android Google Play 或 F-Droid），然后按手机系统选择订阅方式：

| 手机 | 订阅方式 |
| :--- | :--- |
| **Android** | 点击电脑给出的 `ntfy://ntfy.sh/<topic>?display=频道名` 链接（发到手机，在短信、备忘录、邮件等能点链接的地方点开），或扫描 **Android 二维码**，会**直接打开 ntfy App 并自动订阅**，频道名显示为中文标题 |
| **iPhone** | iOS 版 ntfy App 不支持用链接或二维码拉起，需要手动添加：打开 App → 点 `+` → 粘贴 **Topic 名称** → Subscribe（自建服务器还要打开「Use another server」并填入服务器地址） |

> - 微信等 App 的内置浏览器通常会拦截 `ntfy://` 链接；直接粘贴到浏览器地址栏也无效。
> - `https://ntfy.sh/<topic>` 网页链接在两种手机上都只会打开网页版，不会拉起 App。

订阅后，在输入框直接打字，或点回形针上传图片/文件，就能遥控电脑上的这个对话框执行任务。

---

## 宿主支持

| 宿主 | 手机唤醒方式 | 结果回传 |
| :--- | :--- | :--- |
| Claude Code | `claude --resume <会话ID> -p`（会话 ID 自动从 `CLAUDE_CODE_SESSION_ID` 读取） | 最终回复自动推回手机 |
| Codex | `codex exec resume <会话ID>`（失败则开新会话） | 最终回复自动推回手机 |
| Antigravity | `agentapi send-message` 投递到原对话 | AI 调用 `notify` 推回 |
| Cursor、WorkBuddy 等 | 暂不支持从手机唤醒 | 可用 `notify` 单向推送 |

- Claude Code 通过 `--resume` 在后台进程中执行。IDE 里正在显示的窗口不会实时刷新，结果以手机推送为准。
- **同一台电脑、多个 IDE、多个对话同时开启也不会串台。**
  - 每个对话框有自己独立的 Topic 和频道，手机发到哪个频道，就只交给那个频道绑定的会话执行。
  - 绑定时只按会话 ID 精确匹配；同一个对话重复绑定，会复用原来的频道。
  - 拿不到会话 ID 时（比如在别的终端里手动运行），会为这个频道新建一个**专属会话**，之后的指令都接着这个会话执行，不会落进同目录下的其他对话。
- `notify` 推送时请用频道 ID，不要用面板序号，因为删除频道后序号会变化。
- 同一频道的指令排队逐条执行，单条默认最长 30 分钟（`--task-timeout` 可调）。
- 以下情况都会回执告诉手机原因，不会静默丢弃：执行失败、宿主不支持、电脑休眠期间发来的过期指令（超过 300 秒）。

---

## 全局总控面板与常用命令速查表

### 1. 在 AI 对话框中直接使用的快捷指令

| 快捷指令 | 功能说明 |
| :--- | :--- |
| `/ntfy` | 为当前对话框生成并绑定专属手机远程频道，开启防休眠与后台常驻监听 |
| `/ntfy panel` | 打开**全局远程对话总控面板**（用 `#1, #2` 序号列出所有频道、宿主、绑定目录与电源状态） |
| `/ntfy rm 1` | 按面板序号（如 `1`）、频道名称或会话 ID **删除并解绑**指定频道 |
| `/ntfy pause 1` / `/ntfy resume 1` | 暂停或恢复指定频道的指令监听 |
| `/ntfy clear` | 清空所有已绑定的频道（防休眠锁随之自动释放） |

### 2. 在手机 `ntfy` 频道内发送的元指令（不唤醒 AI，不消耗 Token）

| 手机端发送内容 | 电脑端动作 |
| :--- | :--- |
| `状态` 或 `/status` | 返回**当前任务**（正在执行什么、已执行多久、排队几条）、电量、防休眠状态、绑定会话与工作目录；AI 生成期间也能秒回 |
| `取消` 或 `/cancel` | 终止正在执行的 AI 任务（连同它启动的子进程），并清空排队的指令 |
| `暂停` 或 `/pause` | 暂停当前频道的指令转发（防止误触） |
| `恢复` 或 `/resume` | 恢复当前频道的指令监听 |
| `/sleep on` / `/sleep off` / `/sleep auto` | 强制开启 / 关闭 / 自动管理电脑防休眠 |
| `销毁频道` 或 `/delete` | 从手机端直接解绑当前频道，后台立即停止监听 |
| `帮助` 或 `/help` | 查看全部手机端指令 |

### 发出指令后，怎么知道电脑收到了？

AI 生成可能要几分钟，为了不让你干等：

| 你在手机上看到 | 说明 |
| :--- | :--- |
| 几秒内直接收到结果 | 任务很快，已完成 |
| 约 15 秒后收到静音通知「正在执行（已 15 秒）」 | 电脑已收到，AI 还在生成；之后每 5 分钟推一次进度，完成时通知带耗时 |
| 收到「已排队」 | 前面还有任务在执行，会按顺序处理 |
| 发出 25 秒以上，既没有结果也没有任何回执 | 电脑**没收到**（可能休眠、离线或后台服务没运行），发 `状态` 确认；电脑恢复后，5 分钟内的指令会补执行，更早的会提示已过期 |
| 想放弃当前任务 | 发 `取消` |

- 进度间隔可用 `bind --progress-interval <秒>` 调整，设为 `0` 关闭进度回执。
- Antigravity 是在 IDE 里异步执行的，投递成功后会立即收到「已投递」回执，结果由 AI 完成后推送。

### 3. 命令行参考（`ntfy_ctl.py`）

```bash
NTFY=~/.agents/skills/ntfy/scripts/ntfy_ctl.py   # Windows 用 py -3 代替 python3

# 总控面板（Markdown 表格）；status 输出 JSON
python3 $NTFY panel
python3 $NTFY status

# 绑定（--host 可省略自动识别；建议配合受保护 Topic 使用 --server/--token）
python3 $NTFY bind --host claude --workspace "$PWD" --title "我的项目"
#   可选：--conv-id <会话ID>  --server https://你的ntfy  --token tk_xxx  --rotate
#         --permission-mode auto|bypassPermissions   --codex-sandbox read-only|workspace-write|danger-full-access
#         --task-timeout 3600   --ack   --keep-emoji

# 推送多行文字：用 --msg-file - 从标准输入读取，避免引号转义问题
python3 $NTFY notify --channel <频道ID> --title "任务完成" --msg-file - <<'EOF'
**执行结果**
已完成处理，请查收图片与报告。
EOF

# 推送图片/文档/视频（可传多个 --file；.env、私钥、凭证类文件会被拒绝）
python3 $NTFY notify --channel <频道ID> --msg "请查收" --file ./preview.png --file ./report.md

# 管理频道
python3 $NTFY rm 1            # 同 unbind / delete；加 --quiet 不通知手机
python3 $NTFY pause 1
python3 $NTFY resume 1
python3 $NTFY clear

# 防休眠策略；auto 模式下，电池低于 --min-battery 时自动释放
python3 $NTFY sleep --mode auto --min-battery 20

# 停止守护进程并移除开机自启；--purge 同时删除本地状态
python3 $NTFY uninstall [--purge]
```

平台补充：

- **Linux**：`systemd --user` 服务默认在注销登录后停止。如需一直运行，请执行 `loginctl enable-linger $USER`。
- **macOS**：防休眠无法阻止物理合盖睡眠。要合盖运行，需要插电并外接显示器。

---

## 双向收发「图片、文档与视频」

- **图片**（`.png` / `.jpg` / `.webp` / `.gif`）：直接上传到主频道，iOS、Android、网页端都会在消息卡片里显示大图。
- **文本文档**（`.md` / `.txt` / `.csv` / `.json` / `.yaml` / `.log`）：
  - 全文在消息气泡里直接展开，结构化文本放进代码块，保留缩进；
  - 底部附上原文件链接；
  - 超出正文上限时自动截断。
- **二进制文档与视频**（`.pdf` / `.pptx` / `.docx` / `.mp4` / `.zip`）：
  - 上传到 `<topic>_files` 子频道取得直链，再在主频道发一张可点击的下载卡片（解决 iOS App 不显示非图片附件按钮的问题）；
  - 超过 14MB 的视频会用 `ffmpeg` 压成 480p 预览版。
- **手机上传到电脑**：点回形针上传，守护进程把文件下载到工作目录的 `.ntfy_inbox/`（目录内自带 `.gitignore`，不会被提交），并把文件路径交给 AI。
  - 只接受同一 ntfy 服务器托管的附件，外部链接一律拒绝。
- **Claude Code / Codex 回传文件**：AI 在回复末尾为每个文件写一行 `NTFY_FILE: /绝对路径`，就会被推到手机。只允许工作目录内的文件。

---

## 安全须知与免责声明（必读）

1. **不要分享二维码、Topic 与订阅链接，它们是最高权限凭证。**
   - 订阅链接（`https://ntfy.sh/<topic>` / `ntfy://...`）和二维码**等同于你电脑上对应 AI 对话框的「免密远程控制密钥」**。
   - 在公共 `ntfy.sh` 上，知道 Topic 的人可以读取频道的历史消息，也可以向电脑发指令。
   - **不要在社交平台截图（小红书、朋友圈、群聊等）、录屏演示或公开仓库中露出未打码的二维码与链接。**
   - 一旦怀疑泄露，立即在手机端发送 `销毁频道`（或运行 `/ntfy rm <序号>`），再用 `bind --rotate` 轮换新 Topic。
2. **强烈建议开启访问控制。** 使用自建 ntfy 服务器，或 ntfy Pro 的受保护 Topic，并通过 `bind --server <地址> --token tk_xxx` 绑定。没有配置 token 时，绑定结果会给出安全提示。
3. **默认最小权限。**
   - Claude Code 默认 `acceptEdits`：可以改文件，不能执行终端命令。需要更多能力时用 `--permission-mode auto` 或 `bypassPermissions`。
   - Codex 默认 `workspace-write` 沙箱。
   - 放宽权限前，请先做好访问控制。
4. **隐私与数据生命周期。**
   - Topic 用 `secrets` 模块生成，在公共服务器上无法被枚举或暴力撞库。
   - 注册表保存在 `~/.agents/state/ntfy-remote-chat/`，目录权限为 700、文件为 600；`status` 输出中的 token 会被打码。
   - 二维码默认离线生成，不会把 Topic 发给任何第三方二维码接口。
   - 不要通过公共频道传输密钥文件（`.env`、`.pem`、私钥）或商业机密；脚本默认也会拒绝推送这类文件。
5. **ntfy.sh 免费版限制。**
   - 单个附件最大 15MB；附件在服务器上约 **3 小时后自动删除**，链接随之失效，请及时保存。
   - 单条消息正文最大 4096 字节（中文约 1300 字），超出部分会被截断，长文请以文件形式发送。
   - 匿名用户有每日消息数量和附件总量的配额。
6. **免责声明（Disclaimer）。**
   - 本开源工具用于开发者通过个人手机远程查看并操控自己信任的本地 AI IDE 会话。
   - 开启监听后，手机端发出的指令会由本地 AI 编程助手在绑定的工作目录中直接执行。
   - 因使用者主动分享或意外泄露订阅链接/二维码、在公共信道传输机密数据、或通过手机下达破坏性指令所导致的任何数据泄露、文件损毁及安全风险，均由使用者本人承担全部责任，项目作者不承担任何连带责任。

---

## 运行依赖

- Python 3.8+（仅使用标准库）
  - **Windows：建议用 Python 3.10 及以上运行后台服务**（3.9 已停止维护，但实测也能正常工作）。用 `py -3` 运行脚本即可；如果用旧版本运行 `bind`，脚本会通过 Windows 自带的 `py` 启动器自动选用本机安装的新版 Python，找不到时会给出建议。
  - macOS 自带的 `python3`（3.9）可以直接使用。
- 可选：`ffmpeg`（压制超大视频的预览版）、`qrencode` 或 Python `qrcode`（离线生成订阅二维码）

## 开发与测试

```bash
python3 -m unittest discover -s tests -v   # 单元测试
python3 tests/smoke_daemon.py              # 冒烟测试：真实安装后台服务 → 手机发指令 → 推回结果 → 卸载
```

- 测试使用本地假 ntfy 服务器和假 claude，不访问外网，也不消耗 AI 额度。
- GitHub Actions 会在 Ubuntu、macOS、Windows 三个系统上，各用 Python 3.9 和 3.13 自动跑上面两项测试。

## 许可证

本项目采用 [MIT License](LICENSE) 开源。软件按"原样"提供，不附带任何担保；使用风险见上方「安全须知与免责声明」。
