# ntfy-remote-chat

将任意 AI IDE 对话框（Antigravity、Codex、Claude Code、WorkBuddy、Cursor 等）一键生成专属的手机 `ntfy` 双向远程控制频道。自动完成 **160-bit 防扫库专属订阅连接**、**1 对 1 会话锁死防串台**、**跨平台防休眠联动**、**常驻自愈监听与断线补抓**，并支持**电脑与手机双向直传图片、文档与视频预览**。

---

## 一行命令安装

```bash
npx -y skills add LionZLH/ntfy-remote-chat -g --all
```

---

## 核心特性

1. **严格 1 对 1 物理隔离（防串台）**：
   每个对话框生成独立的 28 位密码学高熵随机 Topic（熵值 $>160\text{ bits}$，在公共服务器上无法被枚举或暴力扫描），并与唯一会话 ID 和工作目录刚性锁死，多开项目互不串台。
2. **双向收发「图片、文件与视频预览」**：
   - **电脑 $\rightarrow$ 手机**：AI 产出的图片（`.png` / `.jpg` / `.webp`）直接在手机 `ntfy` App 渲染为高清大图预览；产出的文档（`.pdf` / `.md` / `.pptx` / `.zip`）显示为一键下载卡片；超出 14MB 的大视频（`.mp4` / `.mov`）自动调用 `ffmpeg` 压制轻量手机预览版直推手机。
   - **手机 $\rightarrow$ 电脑**：在手机 `ntfy` App 点击输入框旁回形针上传图片或文件，守护进程自动下载至工作目录 `.ntfy_inbox/` 并唤醒 AI 读取处理。
3. **跨平台防休眠生命周期联动**：
   - **macOS**：自动托管 `caffeinate -dims` + `launchd` (`LaunchAgents`) 常驻自愈；
   - **Linux**：自动调用 `systemd-inhibit`；
   - **Windows**：自动调用 `SetThreadExecutionState`；
   - 只要存在至少 1 个活跃监听频道自动维持防休眠，全部解绑后自动释放。
4. **断线补抓与防重放**：
   持久化记录每个频道的 `last_event_id`，网络重连时携带 `?since=` 补抓闪断期间的指令，并自动丢弃超过 300 秒的陈旧历史消息。
5. **手机端 0.2 秒响应的内置元指令**：
   - 发送 `状态` 或 `/status`：秒回电脑电量、防休眠进程 PID、绑定目录与会话状态（不消耗 AI Token）；
   - 发送 `暂停` / `恢复`（`/pause` / `/resume`）：随时暂停或恢复该频道指令监听；
   - 发送 `/sleep on` / `/sleep off`：手机远程开关电脑防休眠锁。

---

## 使用方式

安装后，在任何 AI IDE 对话框中直接输入：

> 给这个对话框生成一个 ntfy 手机远程控制频道

或显式调用：

> 使用 $ntfy-remote-chat 为当前对话框生成专属的 ntfy 手机双向远程控制频道，并开启防休眠与后台常驻监听。

---

## 运行依赖

- 支持 Agent Skills 的客户端（Antigravity、Codex、Claude Code、WorkBuddy、Cursor、Gemini CLI 等）
- Python 3.8+（仅使用标准库，零第三方强依赖）
- 可选：`ffmpeg`（用于超大视频自动生成手机预览版）、`qrencode`（用于纯离线生成订阅二维码）

## 隐私与安全说明

- 本仓库只发布运行该 Skill 所需的通用代码，不包含任何个人配置、运行日志或测试记录。
- 频道 Topic 默认仅在本地与 `ntfy` 服务器之间通信，默认不向任何第三方外部二维码 API 发送私密 Topic。
