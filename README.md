# ntfy-remote-chat

把 AI IDE 对话框（Claude Code、Codex、Antigravity）一键变成手机 `ntfy` 双向远程控制频道：手机发文字或图片 → 电脑上的 AI 执行 → 结果（文字、图片、文档、视频）推回手机。

---

## 一行命令安装

```bash
npx -y skills add LionZLH/ntfy-remote-chat -g --all
```

安装后在对话框里输入 `/ntfy`（或 `$ntfy`），或者直接说：

> 给这个对话框生成一个 ntfy 手机远程控制频道

---

## 核心特性

1. **1 对 1 隔离**：每个对话框生成独立的 28 位随机 Topic（约 166 bits，不含用户名和项目名），与会话 ID、工作目录绑定。
2. **双向收发图片、文件与视频**
   - **电脑 → 手机**：图片在 App 内直接大图预览；`.md` / `.txt` / `.json` 等文本内联展示；PDF、Office、压缩包显示为可点击的下载卡片；超过 14MB 的视频自动用 `ffmpeg` 压成 480p 预览版。
   - **手机 → 电脑**：点输入框旁的回形针上传，守护进程下载到工作目录的 `.ntfy_inbox/`（自带 `.gitignore`，不会被提交），再唤醒 AI 处理。
3. **跨平台防休眠**：macOS `caffeinate -ims`、Linux `systemd-inhibit`、Windows `SetThreadExecutionState`。有活跃频道才开启，不强制亮屏；电池低于 20% 时自动释放（可调）。
4. **常驻与自愈**：macOS 用 `launchd`，Linux 用 `systemd --user`，Windows 用登录自启。同一时间只运行一个守护进程，Skill 更新后自动重启加载新代码。
5. **不静默丢指令**
   - 断线重连后用 `?since=` 补抓漏掉的消息；
   - 超过 300 秒的旧指令（比如电脑休眠期间发的）不执行，但会回执告诉手机；
   - 宿主不支持或执行失败，也会回执说明原因；
   - 同一频道的指令排队逐条执行，单条默认最长 30 分钟。
6. **手机端元指令（不消耗 AI Token）**：`状态` `/status`、`暂停` `/pause`、`恢复` `/resume`、`帮助` `/help`、`/sleep on|off|auto`。

## 宿主支持

| 宿主 | 手机唤醒方式 | 结果回传 |
|---|---|---|
| Claude Code | `claude --resume <会话ID> -p` | 最终回复自动推回手机 |
| Codex | `codex exec resume <会话ID>`（失败则开新会话） | 最终回复自动推回手机 |
| Antigravity | `agentapi send-message` 投递到原对话 | AI 调用 `notify` 推回 |
| Cursor、WorkBuddy 等 | 暂不支持 | 可用 `notify` 单向推送 |

> Claude Code 用 `--resume` 打开的是一个新的后台进程。IDE 里正在显示的那个窗口不会实时刷新，结果以手机推送为准。

---

## 安全须知（请务必阅读）

- **Topic 就是密码。** 在公共 `ntfy.sh` 上，任何知道 Topic 的人都能读取频道里的历史消息，也能向你的电脑发指令。不要截图或公开 Topic；怀疑泄露时运行 `bind --rotate` 换一个新的。
- **强烈建议开启访问控制。** 用自建 ntfy 服务器，或 ntfy Pro 的受保护 Topic，然后通过 `bind --server <地址> --token tk_xxx` 绑定。没有配置 token 时，绑定结果里会给出安全提示。
- **默认最小权限。**
  - Claude Code 默认 `acceptEdits`：可以改文件，不能执行命令。需要更多能力时用 `--permission-mode auto` 或 `bypassPermissions`。
  - Codex 默认 `workspace-write` 沙箱。
  - 放宽权限前，请先做好上面的访问控制。
- **附件。** 只接受同一 ntfy 服务器托管的附件，外部链接一律拒绝，避免 token 泄露或被诱导下载任意内容。`.env`、私钥、凭证类文件默认禁止推送。
- **本地数据。** 注册表保存在 `~/.agents/state/ntfy-remote-chat/`，目录权限为 700、文件为 600。二维码默认离线生成，不会把 Topic 发给第三方。

## ntfy.sh 免费版限制

- 单个附件最大 15MB；附件链接约 **3 小时后失效**，请及时保存。
- 单条消息正文最大 4096 字节（中文约 1300 字）。超出部分脚本会自动截断，长文请以文件形式发送。
- 匿名用户有每日消息数量和附件总量的配额。频繁使用建议自建服务器。

---

## 命令参考

```bash
NTFY=~/.agents/skills/ntfy/scripts/ntfy_ctl.py

python3 $NTFY bind --host claude --workspace "$PWD" --title "我的项目"   # 绑定
python3 $NTFY notify --channel <频道ID> --msg-file - <<'EOF'              # 多行文字
**结果**
已完成
EOF
python3 $NTFY notify --channel <频道ID> --msg "看图" --file ./out.png    # 发文件
python3 $NTFY status                                                     # 状态
python3 $NTFY unbind --channel <频道ID>                                  # 解绑
python3 $NTFY sleep --mode auto --min-battery 20                         # 防休眠策略
python3 $NTFY uninstall [--purge]                                        # 停止并移除后台服务
```

- Linux：`systemd --user` 服务默认在注销登录后停止。如需一直运行，请执行 `loginctl enable-linger $USER`。
- macOS：防休眠无法阻止物理合盖睡眠（除非插电并外接显示器）。

## 运行依赖

- Python 3.8+（仅标准库）
- 可选：`ffmpeg`（超大视频压制预览版）、`qrencode` 或 Python `qrcode`（离线生成订阅二维码）

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用本地假 ntfy 服务器，不访问外网。
