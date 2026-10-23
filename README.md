# ntfy-remote-chat (`/ntfy`)

将任意 AI IDE 对话框（Antigravity、Codex、Claude Code、WorkBuddy、Cursor 等）一键生成专属的手机 `ntfy` 双向远程控制频道。自动完成 **160-bit 防扫库专属订阅连接**、**1 对 1 会话锁死防串台**、**跨平台防休眠联动**、**常驻自愈监听与断线补抓**、**全局总控面板管理**，并支持**电脑与手机双向直传图片、文档与视频预览**。

> [!CAUTION]
> **核心安全警示：切勿向任何人分享、转发或截图暴露你的二维码、Topic 名称与订阅链接！**
> 专属的 `ntfy` 订阅链接与二维码**等同于你电脑当前 AI 对话框的「免密远程控制密钥」**。任何拿到该二维码或订阅链接的人，不仅能看到 AI 发回手机的所有通知、图片与文档，还能**直接通过手机向你电脑上的 AI 发送指令、读写项目文件或执行终端命令**。若不慎在截图或群聊中暴露，请立即在手机端发送 `销毁频道`（或在电脑端执行 `/ntfy rm <序号>`）作废旧频道！

---

## 一行命令安装

```bash
npx -y skills add LionZLH/ntfy-remote-chat -g --all
```

---

## 快速配置教程（30 秒上手）

### 第一步：在电脑 AI IDE 对话框中一键开启
在任意正在进行的 AI IDE 对话框中直接输入：

```text
/ntfy
```
（或在支持 `$` 呼出的环境中输入 `$ntfy`）

AI 将自动为你完成：
1. 生成 160-bit 密码学随机专属 Topic（如 `proj_9xK2mP7vQ4nL8wR1tY5uI0oP3aS6`）；
2. 将该 Topic 与当前对话框 `conv_id` 及工作目录 **1 对 1 刚性绑定**（多开窗口互不串台）；
3. 启动系统级防休眠锁（macOS `caffeinate -dims` / Linux `systemd-inhibit` / Windows `SetThreadExecutionState`）与后台守护进程；
4. 向该频道发送首条连接就绪卡片。

### 第二步：手机端订阅该频道
1. 在手机安装免费开源的 **ntfy** App（iOS App Store / Android Google Play 或 F-Droid）；
2. 打开 App 点击右下角 **`+`** 号，粘贴电脑端生成的专属 **Topic 名称**（或直接点击电脑交付的 `ntfy://ntfy.sh/<topic>` 深链）；
3. 在手机输入框直接打字或点回形针上传图片/文件，即可随时随地遥控电脑端该对话框执行任务！

---

## 全局总控面板与常用命令速查表

### 1. 在 AI 对话框中直接使用的快捷指令

| 快捷指令 | 功能说明 |
| :--- | :--- |
| `/ntfy` | 为当前对话框生成并绑定专属手机远程频道，自动开启防休眠与后台常驻监听 |
| `/ntfy panel` | 打开 **全局远程对话总控面板**（带 `#1, #2` 序号展示所有已配置频道、绑定目录与电源状态） |
| `/ntfy rm 1` | 按总控面板序号（如 `1`）、频道名称或会话 ID **删除并解绑**指定对话服务 |
| `/ntfy pause 1` / `/ntfy resume 1` | 暂停或恢复指定序号频道的指令监听 |
| `/ntfy clear` | 一键清空并停止所有已绑定的对话频道（自动释放电脑防休眠锁） |

### 2. 在手机 `ntfy` 频道内直接发送的「0.2 秒瞬回元指令」（不消耗 AI Token）

| 手机端发送内容 | 电脑端秒级响应动作 |
| :--- | :--- |
| `状态` 或 `/status` | 0.2 秒返回电脑当前电量、电源状态、防休眠进程 PID、绑定会话 ID 与工作目录 |
| `暂停` 或 `/pause` | 暂停当前频道指令转发（防止误触） |
| `恢复` 或 `/resume` | 恢复当前频道指令监听 |
| `/sleep on` / `/sleep off` | 手机远程强制开启或关闭电脑防休眠锁 |
| `销毁频道` 或 `/delete` | 直接从手机端远程销毁解绑当前频道，后台立即停止监听该对话 |
| `帮助` 或 `/help` | 查看手机端支持的全部快捷指令说明 |

### 3. 命令行 CLI 完整参考（`ntfy_ctl.py`）

```bash
# 1. 打开可视化 Markdown 全局总控面板（查看所有对话服务及 #1, #2 序号）
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py panel

# 2. 为指定对话框生成并绑定专属频道
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py bind \
  --conv-id "<对话ID>" \
  --workspace "<工作目录绝对路径>" \
  --alias "<英文短别名>" \
  --title "<频道中文名>"

# 3. 向手机发送文字通知，或附带图片/文档/视频直传手机预览
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py notify \
  --channel "1" \
  --title "任务完成" \
  --msg "**执行结果**\n已完成处理，请查收下方图片与报告。" \
  --file "/path/to/preview.png" \
  --file "/path/to/report.md"

# 4. 按序号（如 1）、别名、conv_id 或 Topic 删除指定频道
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py rm 1

# 5. 暂停 / 恢复指定序号频道
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py pause 1
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py resume 1

# 6. 一键清空所有已绑定频道
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py clear

# 7. 控制电脑防休眠模式 (auto / on / off / status)
python3 ~/.agents/skills/ntfy/scripts/ntfy_ctl.py sleep --mode auto
```

---

## 双向收发「图片、文档与视频」兼容机制

- **图片（`.png` / `.jpg` / `.webp` / `.gif`）**：二进制 `PUT` 直传主频道，iOS / Android / Web 直接在消息卡片内渲染高清大图。
- **文本文档（`.md` / `.txt` / `.csv` / `.json`）**：自动在消息气泡内直接按 Markdown 排版展开全文（无需跳出 App 即可滑动阅读），同时底部附带一键打开原文件链接。
- **二进制文档与视频（`.pdf` / `.pptx` / `.docx` / `.mp4` / `.zip`）**：自动上传至 `<topic>_files` 寄存通道提取直链，并在主频道生成带 `click` 跳转与蓝字下载链接的卡片（解决 iOS `ntfy` App 不显示非图片附件下载按钮的限制）；超出 14MB 的大视频自动调用 `ffmpeg` 压制轻量预览版。
- **手机上传文件至电脑**：在手机 `ntfy` 点击输入框旁回形针上传图片或文档，守护进程自动下载至项目工作目录 `.ntfy_inbox/` 并附带绝对路径唤醒 AI 处理。

---

## 安全警示与免责声明（必读 / Disclaimer）

1. **严禁分享二维码、Topic 与订阅链接（最高权限凭证）**：
   - 本工具生成的专属 Topic、订阅链接（`https://ntfy.sh/<topic>` / `ntfy://...`）及二维码，**等同于你电脑当前 AI 对话框的「免密远程控制密钥」**。
   - **切勿在社交平台截图（如小红书、朋友圈、群聊）、录屏演示或公开代码仓库中露出未打码的二维码与订阅链接**。
   - 一旦怀疑泄露或误发截图，请立即在手机端发送 `销毁频道`（或在电脑端运行 `/ntfy rm <序号>` / `ntfy_ctl.py bind --rotate`）立刻销毁作废旧频道并轮换新凭证。
2. **隐私与数据生命周期**：
   - **160-bit 密码学随机 Topic**：默认采用 `secrets.token_urlsafe(20)` 生成 28 位高熵随机频道名，公共服务器上无法被枚举或暴力撞库。
   - **文件缓存自动销毁**：通过公网 `ntfy.sh` 传输的图片或文档附件在服务器缓存仅保留 **3 小时（10,800 秒）**，到期由服务器自动物理销毁；严禁通过公共频道传输密钥文件（`.env`、`.pem`、私钥、核心商业机密等），高敏感场景请配合自建私有 `ntfy` 实例（`--server` + `--token`）使用。
   - **零外部二维码泄露**：默认仅使用本机离线工具生成二维码，绝不向任何第三方外部二维码 API 发送私密 Topic。
3. **免责声明（Disclaimer）**：
   - 本开源工具旨在方便开发者通过个人手机远程查看并操控自己受信任的本地 AI IDE 会话。
   - 开启监听后，手机端发出的指令将由本地 AI 编程助手在所绑定的工作目录中直接执行。因使用者主动分享或意外泄露订阅链接/二维码、在公共信道传输机密数据、或通过手机下达破坏性指令所导致的任何数据泄露、文件损毁及安全风险，均由使用者本人自行承担全部责任，项目作者不承担任何连带责任。
