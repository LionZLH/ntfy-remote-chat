#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ntfy_ctl.py
-----------
跨平台、跨 AI IDE 通用手机 ntfy 双向远程对话控制中心
(Multi-Channel Hub + Cross-OS Sleep Guard + Multi-IDE Adapter + Bidirectional File/Image Transfer)。

核心特性：
1. 严格 1 对 1 物理隔离：每个对话框生成独立的 160-bit 高熵 Topic，绝不扫描其他会话，杜绝串台。
2. 双向图片与文件传输：
   - 电脑 -> 手机：`notify --file <path>` 支持直接把 AI 产出的图片、PDF、Markdown、视频、压缩包推到手机 ntfy（图片直接大图预览，文件一键下载打开；超 14MB 视频自动调用 ffmpeg 压制手机预览版）。
   - 手机 -> 电脑：手机在 ntfy App 上传图片/文件时，守护进程自动下载至工作目录 `.ntfy_inbox/` 并唤醒 AI 读取处理。
3. 跨平台防休眠联动：支持 macOS (`caffeinate -dims`)、Linux (`systemd-inhibit`) 与 Windows (`SetThreadExecutionState`)；只要存在活跃监听频道自动维持防休眠，全部解绑后自动释放。
4. 断线补抓与防重放：持久化每个频道的 `last_event_id` 与时间戳，重连时通过 `?since=` 补抓闪断期间消息，并过滤超过 300 秒的陈旧消息。
5. 手机端秒级元指令：内置 `/status` (`状态`)、`/help` (`帮助`)、`/pause` (`暂停`)、`/resume` (`恢复`)、`/sleep on|off`，0.2 秒直接响应。
6. 跨 IDE 投递适配器：支持 Antigravity (`agentapi`)、Codex (`codex exec`)、Claude Code (`claude`) 等主流宿主。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".agents" / "state" / "ntfy-remote-chat"
LEGACY_HUB_DIR = HOME / ".gemini" / "config" / "ntfy_hub"
REGISTRY_PATH = STATE_DIR / "registry.json"
ENV_CACHE_PATH = STATE_DIR / "agy_env_cache.json"
PID_FILE_PATH = STATE_DIR / "daemon.pid"

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WIN = sys.platform == "win32"

LOG_PATH = (HOME / "Library" / "Logs" / "ntfy_hub.log") if IS_MAC else (STATE_DIR / "ntfy_hub.log")
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
PLIST_LABEL = "io.agentskills.ntfy_hub"
OLD_PLIST_LABEL = "com.evan.ntfy_hub"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{PLIST_LABEL}.plist"
OLD_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{OLD_PLIST_LABEL}.plist"
AGENTAPI_BIN = HOME / ".gemini" / "antigravity" / "bin" / "agentapi"
USER_AGENT = "NtfyRemoteChat-Skill/1.1"
MAX_NTFY_FREE_BYTES = 14 * 1024 * 1024  # 14MB 安全阈值（ntfy.sh 免费版限制 15MB）

# 常见 Emoji 范围正则，用于默认保持极简纯文字分段
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U00002600-\U000027BF"
    "\U0001FA00-\U0001FAFF"
    "\U0000FE00-\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emojis(text: str) -> str:
    """移除装饰性 Emoji，保留干净的 Markdown 文本"""
    if not text:
        return ""
    cleaned = EMOJI_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    old_reg = LEGACY_HUB_DIR / "registry.json"
    if not REGISTRY_PATH.is_file() and old_reg.is_file():
        try:
            shutil.copy2(old_reg, REGISTRY_PATH)
        except Exception:
            pass


def load_registry() -> dict:
    ensure_state_dir()
    if REGISTRY_PATH.is_file():
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "channels" in data:
                    return data
        except Exception:
            pass
    return {
        "version": 1,
        "sleep_guard": "auto",  # auto | on | off
        "channels": {},
    }


def save_registry(reg: dict) -> None:
    ensure_state_dir()
    tmp_path = REGISTRY_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    tmp_path.replace(REGISTRY_PATH)


def slugify_alias(raw: str) -> str:
    """将工作目录名或别名转为短标签"""
    ascii_only = re.sub(r"[^a-zA-Z0-9]+", "", raw).lower()
    if ascii_only:
        return ascii_only[:10]
    return "chat"


def generate_topic(alias: str) -> str:
    """生成 >=160 bits 熵值的防扫描专属 Topic"""
    try:
        user_prefix = slugify_alias(getpass.getuser())[:6] or "agt"
    except Exception:
        user_prefix = "agt"
    slug = slugify_alias(alias)
    alphabet = string.ascii_letters + string.digits
    rand_part = "".join(secrets.choice(alphabet) for _ in range(28))
    return f"{user_prefix}_{slug}_{rand_part}"


def detect_power_and_sleep_status() -> dict:
    """跨平台检测当前电源状态、防休眠进程与合盖睡眠风险"""
    info = {
        "platform": sys.platform,
        "power_source": "Unknown",
        "battery_percent": "Unknown",
        "sleep_guard_pids": [],
        "disablesleep": "0",
        "sleep_note": "",
    }
    if IS_MAC:
        try:
            batt_out = subprocess.check_output(["pmset", "-g", "batt"], text=True, stderr=subprocess.DEVNULL)
            if "AC Power" in batt_out:
                info["power_source"] = "AC Power (已插电源)"
            elif "Battery Power" in batt_out:
                info["power_source"] = "Battery Power (电池供电)"
            m_pct = re.search(r"(\d+)%", batt_out)
            if m_pct:
                info["battery_percent"] = f"{m_pct.group(1)}%"
        except Exception:
            pass

        try:
            pm_out = subprocess.check_output(["pmset", "-g"], text=True, stderr=subprocess.DEVNULL)
            m_ds = re.search(r"disablesleep\s+(\d+)", pm_out)
            if m_ds:
                info["disablesleep"] = m_ds.group(1)
        except Exception:
            pass

        try:
            pgrep_out = subprocess.check_output(["pgrep", "-fl", "caffeinate"], text=True, stderr=subprocess.DEVNULL)
            for line in pgrep_out.splitlines():
                parts = line.strip().split(None, 1)
                if parts:
                    info["sleep_guard_pids"].append(parts[0])
        except Exception:
            pass

        if info["disablesleep"] == "0":
            info["sleep_note"] = (
                "当前已开启防空闲休眠（不碰电脑不会休眠）；"
                "注意：若拔掉外接显示器并物理合上 MacBook 盖子，系统仍会进入硬件合盖睡眠。"
                "如需合盖运行，请保持插电外接显示器，或将屏幕亮度调至最低开盖放置。"
            )
        else:
            info["sleep_note"] = "系统已开启强制禁止睡眠 (disablesleep=1)，支持合盖运行。"
    elif IS_LINUX:
        try:
            pgrep_out = subprocess.check_output(
                ["pgrep", "-fl", "systemd-inhibit"], text=True, stderr=subprocess.DEVNULL
            )
            for line in pgrep_out.splitlines():
                parts = line.strip().split(None, 1)
                if parts:
                    info["sleep_guard_pids"].append(parts[0])
        except Exception:
            pass
        info["sleep_note"] = "Linux 环境通过 systemd-inhibit 阻止空闲与挂起睡眠。"
    elif IS_WIN:
        info["sleep_note"] = "Windows 环境通过 SetThreadExecutionState 阻止系统与屏幕休眠。"

    return info


def discover_antigravity_env() -> dict | None:
    """动态从系统中探测运行中的 Antigravity 实例与桥接凭证（支持重启后秒级自愈）"""
    if IS_WIN:
        return None
    try:
        ps = subprocess.check_output("ps aux | grep -i language_server", shell=True, text=True)
        for line in ps.splitlines():
            if "language_server" in line and "--csrf_token" in line:
                m_csrf = re.search(r"--csrf_token\s+([a-zA-Z0-9\-]+)", line)
                parts = line.split()
                if len(parts) < 2:
                    continue
                pid = parts[1]
                lsof_out = subprocess.check_output(f"lsof -nP -p {pid} | grep LISTEN", shell=True, text=True)
                ports = re.findall(r"127\.0\.0\.1:(\d+)", lsof_out)
                if ports and m_csrf:
                    return {
                        "ANTIGRAVITY_LS_ADDRESS": f"localhost:{ports[-1]}",
                        "ANTIGRAVITY_CSRF_TOKEN": m_csrf.group(1),
                    }
    except Exception:
        pass
    return None


def get_agy_env() -> dict:
    """获取或自愈恢复 Antigravity 桥接环境变量"""
    ensure_state_dir()
    env = os.environ.copy()
    discovered = discover_antigravity_env()
    if discovered:
        for k, v in discovered.items():
            env[k] = v
        try:
            with open(ENV_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(discovered, f)
        except Exception:
            pass
        return env

    if ENV_CACHE_PATH.is_file():
        try:
            with open(ENV_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            for k, v in cached.items():
                if v and not env.get(k):
                    env[k] = v
        except Exception:
            pass
    return env


def detect_host_ide(preferred: str = "auto") -> str:
    """自动检测当前所在的 AI IDE / CLI 宿主环境"""
    if preferred and preferred != "auto":
        return preferred.lower()
    if os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or discover_antigravity_env() or AGENTAPI_BIN.is_file():
        return "antigravity"
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    return "gui"


def send_ntfy_message(
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: int = 4,
    auth_token: str = "",
    keep_emoji: bool = False,
    click_url: str = "",
) -> bool:
    """向指定 ntfy 频道发送极简 Markdown 纯文本通知"""
    clean_title = title.strip() if keep_emoji else strip_emojis(title)
    clean_msg = message.strip() if keep_emoji else strip_emojis(message)

    payload: dict = {
        "topic": topic,
        "title": clean_title or "远程对话通知",
        "message": clean_msg,
        "markdown": True,
        "priority": priority,
        "tags": ["robot"],
    }
    if click_url:
        payload["click"] = click_url

    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(
        server.rstrip("/"),
        data=data_bytes,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status in (200, 201, 202)
    except Exception as e:
        print(f"[WARN] ntfy 推送失败 ({topic}): {e}", flush=True)
        return False


def compress_video_for_preview(src_path: Path) -> Path | None:
    """当视频文件超过 ntfy 15MB 限制时，自动调用 ffmpeg 压制一份 <10MB 的轻量手机预览版"""
    if not shutil.which("ffmpeg"):
        return None
    tmp_dir = Path(tempfile.gettempdir()) / "ntfy_preview"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"preview_{src_path.stem}.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src_path),
        "-vf",
        "scale='min(480,iw)':-2",
        "-c:v",
        "libx264",
        "-crf",
        "28",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size <= MAX_NTFY_FREE_BYTES:
        return out_path
    return None


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TEXT_DOC_SUFFIXES = {".md", ".txt", ".csv", ".json", ".log", ".yaml", ".yml"}


def upload_to_ntfy_file_store(
    server: str,
    topic: str,
    upload_path: Path,
    auth_token: str = "",
) -> str:
    """将非图片文件上传至寄存子通道 `<topic>_files` 并返回可直接点击打开的附件 URL"""
    query_params = {
        "filename": upload_path.name,
    }
    put_url = f"{server.rstrip('/')}/{topic}_files?{urllib.parse.urlencode(query_params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/octet-stream",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    data_bytes = upload_path.read_bytes()
    req = urllib.request.Request(put_url, data=data_bytes, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return (body.get("attachment") or {}).get("url", "")


def send_ntfy_file(
    server: str,
    topic: str,
    file_path: Path,
    title: str = "",
    message: str = "",
    priority: int = 4,
    auth_token: str = "",
    keep_emoji: bool = False,
) -> bool:
    """
    向指定 ntfy 频道发送本地图片、文档或视频（完美兼容 iOS / Android / Web）：
    1. 图片 (.png/.jpg/.webp/.gif)：直接通过二进制 PUT 推送至主频道，手机 App 直接渲染高清大图预览；
    2. 文本类文档 (.md/.txt/.csv/.json)：直接将文档内容以 Markdown 排版内联展示在消息气泡中（手机无需跳转即可阅读全文），并在底部附上原文件直链；
    3. 二进制文档或视频 (.pdf/.pptx/.docx/.mp4/.zip)：先上传至 `<topic>_files` 获取 `https://ntfy.sh/file/...` 直链，再发送带可点击链接与 `click` 跳转的卡片（解决 iOS ntfy App 不显示非图片附件按钮的问题）。
    """
    if not file_path.is_file():
        print(f"[WARN] 附件文件不存在: {file_path}", file=sys.stderr)
        return False

    suffix = file_path.suffix.lower()
    upload_path = file_path
    extra_note = ""
    file_size = file_path.stat().st_size

    if file_size > MAX_NTFY_FREE_BYTES:
        if suffix in (".mp4", ".mov", ".mkv", ".avi"):
            compressed = compress_video_for_preview(file_path)
            if compressed:
                upload_path = compressed
                orig_mb = file_size / (1024 * 1024)
                prev_mb = compressed.stat().st_size / (1024 * 1024)
                extra_note = f"\n\n*(原视频 {orig_mb:.1f}MB 超过免费通道上限，已自动生成 {prev_mb:.1f}MB 手机预览版)*"
            else:
                print(f"[WARN] 文件超过 14MB 且无法自动压缩: {file_path}", file=sys.stderr)
                return False
        else:
            print(f"[WARN] 文件大小 ({file_size / (1024*1024):.1f}MB) 超过 ntfy.sh 免费单文件上限", file=sys.stderr)
            return False

    clean_title = (title.strip() if keep_emoji else strip_emojis(title)) or f"文件推送 | {file_path.name}"
    full_msg = (message + extra_note).strip()
    clean_msg = full_msg if keep_emoji else strip_emojis(full_msg)

    # 情况 A：图片文件 -> 直接 PUT 到主频道，iOS/Android 原生展示大图预览
    if suffix in IMAGE_SUFFIXES:
        query_params = {
            "filename": upload_path.name,
            "title": clean_title,
            "priority": str(priority),
            "tags": "robot",
        }
        if clean_msg:
            query_params["message"] = clean_msg

        put_url = f"{server.rstrip('/')}/{topic}?{urllib.parse.urlencode(query_params)}"
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        try:
            data_bytes = upload_path.read_bytes()
            req = urllib.request.Request(put_url, data=data_bytes, headers=headers, method="PUT")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status in (200, 201, 202)
        except Exception as e:
            print(f"[WARN] ntfy 图片上传失败 ({file_path.name}): {e}", flush=True)
            return False

    # 情况 B：非图片文件（文档/PDF/视频/压缩包）
    try:
        file_url = upload_to_ntfy_file_store(server, topic, upload_path, auth_token=auth_token)
    except Exception as e:
        print(f"[WARN] ntfy 文档上传失败 ({file_path.name}): {e}", flush=True)
        file_url = ""

    size_kb = upload_path.stat().st_size / 1024.0
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024.0:.2f} MB"

    # 若是 .md / .txt 等文本文档，直接把正文内容内联排版展示，并在末尾附上原文件下载链接
    if suffix in TEXT_DOC_SUFFIXES:
        try:
            doc_text = upload_path.read_text(encoding="utf-8", errors="ignore").strip()
            if not keep_emoji:
                doc_text = strip_emojis(doc_text)
            if len(doc_text) > 2800:
                doc_text = doc_text[:2800] + "\n\n...(长文已截断，请点击下方链接查看完整原文件)..."
        except Exception:
            doc_text = ""

        parts = []
        if clean_msg:
            parts.append(clean_msg)
        if doc_text:
            parts.append(f"---\n{doc_text}")
        if file_url:
            parts.append(f"---\n**[点击在浏览器打开/下载原文件：{upload_path.name} ({size_str})]({file_url})**")

        combined_msg = "\n\n".join(parts)
        return send_ntfy_message(
            server=server,
            topic=topic,
            title=clean_title,
            message=combined_msg,
            priority=priority,
            auth_token=auth_token,
            keep_emoji=keep_emoji,
            click_url=file_url,
        )

    # 若是 PDF / 视频 / Office / 压缩包等二进制文件，发送带可点击链接与 click 跳转的卡片
    parts = []
    if clean_msg:
        parts.append(clean_msg)
    if file_url:
        parts.append(
            f"**文件附件**\n"
            f"- 文件名：`{upload_path.name}` ({size_str})\n"
            f"- **[点击此处直接打开或下载文件]({file_url})**"
        )
    combined_msg = "\n\n".join(parts)
    return send_ntfy_message(
        server=server,
        topic=topic,
        title=clean_title,
        message=combined_msg,
        priority=priority,
        auth_token=auth_token,
        keep_emoji=keep_emoji,
        click_url=file_url,
    )


def download_incoming_attachment(att: dict, workspace: str, auth_token: str = "") -> Path | None:
    """当用户在手机 ntfy App 上传图片/文件时，自动下载至工作目录 `.ntfy_inbox/` 供 AI 读取"""
    att_url = att.get("url", "")
    if not att_url:
        return None
    raw_name = att.get("name") or "attachment.bin"
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fa5]+", "_", raw_name)
    inbox_dir = Path(workspace) / ".ntfy_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = inbox_dir / f"{ts}_{safe_name}"

    headers = {"User-Agent": USER_AGENT}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        req = urllib.request.Request(att_url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status == 200:
                dest_path.write_bytes(resp.read())
                return dest_path
    except Exception as e:
        print(f"[WARN] 下载手机端附件失败 ({att_url}): {e}", flush=True)
    return None


def generate_qr_code(url: str, out_path: Path, allow_online: bool = False) -> tuple[bool, str]:
    """
    生成订阅链接二维码：
    - 默认仅使用本机离线工具 (`qrencode` CLI 或 Python `qrcode` 模块)，绝不把私密 Topic 发给第三方服务器；
    - 仅当用户显式指定 `--online-qr` 时才请求外部二维码接口。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("qrencode"):
        res = subprocess.run(
            ["qrencode", "-o", str(out_path), "-s", "8", "-m", "2", url],
            capture_output=True,
        )
        if res.returncode == 0 and out_path.is_file():
            return True, "offline:qrencode"

    try:
        import qrcode  # type: ignore

        img = qrcode.make(url)
        img.save(str(out_path))
        if out_path.is_file():
            return True, "offline:python-qrcode"
    except Exception:
        pass

    if allow_online:
        encoded = urllib.parse.quote(url, safe="")
        qr_api = f"https://api.qrserver.com/v1/create-qr-code/?size=360x360&margin=12&data={encoded}"
        req = urllib.request.Request(qr_api, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    out_path.write_bytes(resp.read())
                    return True, "online:qrserver"
        except Exception:
            pass

    return False, "skipped:offline-privacy-default"


def find_channel(reg: dict, selector: str) -> tuple[str | None, dict | None]:
    """支持按序号 (1, #1)、channel_id、conv_id、alias、title 或 topic 查找频道配置"""
    channels = reg.get("channels", {})
    sel = selector.strip()
    if not sel:
        return None, None
    if sel in channels:
        return sel, channels[sel]

    # 支持按总控表序号 (如 "1", "#1") 查找
    idx_str = sel.lstrip("#")
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        items = list(channels.items())
        if 0 <= idx < len(items):
            return items[idx]

    for cid, ch in channels.items():
        if sel in (
            ch.get("conv_id"),
            ch.get("alias"),
            ch.get("title"),
            ch.get("topic"),
        ):
            return cid, ch
    return None, None


def dispatch_command_to_host(
    channel_id: str,
    ch: dict,
    clean_cmd: str,
    attachment_path: Path | None = None,
) -> bool:
    """将手机发来的业务指令（及手机上传的图片/文件）投递给对应绑定的 AI IDE 对话框"""
    host = ch.get("host", "antigravity")
    conv_id = ch.get("conv_id", "")
    workspace = ch.get("workspace", str(HOME))
    title = ch.get("title") or ch.get("alias") or "远程对话"
    keep_emoji_flag = " --keep-emoji" if ch.get("keep_emoji") else ""
    script_path = os.path.abspath(__file__)

    if ch.get("send_ack"):
        send_ntfy_message(
            server=ch.get("server", "https://ntfy.sh"),
            topic=ch["topic"],
            title=f"{title} | 已收到指令",
            message=f"正在执行：`{clean_cmd}`",
            priority=3,
            auth_token=ch.get("auth_token", ""),
            keep_emoji=bool(ch.get("keep_emoji")),
        )

    att_block = ""
    if attachment_path:
        att_block = (
            f"\n【手机随附上传的文件/图片已自动下载至本地】：\n"
            f"`{attachment_path}`\n"
            f"请直接读取/查看该文件并结合指令处理。\n"
        )

    style_rule = (
        "注意：排版保持极简分段、绝对不要使用任何 Emoji"
        if not ch.get("keep_emoji")
        else "注意：采用清晰的 Markdown 分段排版"
    )
    wrapped_prompt = (
        f"【收到来自手机 ntfy 专属频道 ({title}) 的远程指令】：\n"
        f"「{clean_cmd}」\n"
        f"{att_block}\n"
        f"请在工作目录 `{workspace}` 下立即按照该指令执行任务。\n"
        f"重要规范：任务执行完毕后，请务必调用以下命令将结果推送回手机（{style_rule}）：\n"
        f"- 纯文字汇报：`python3 {script_path} notify --channel \"{channel_id}\" --title \"{title} | 任务完成\" --msg \"<分段结果摘要>\"{keep_emoji_flag}`\n"
        f"- 若产出了图片/文档/视频需发给手机查看，追加 `--file \"/绝对路径/产物.png\"`（可传多个 `--file`）即可直接在手机 ntfy 渲染大图或下载附件！"
    )

    if host == "antigravity" and AGENTAPI_BIN.is_file():
        env = get_agy_env()
        if conv_id and conv_id != "new":
            cmd = [
                str(AGENTAPI_BIN),
                "send-message",
                "--title",
                "手机远程指令",
                conv_id,
                wrapped_prompt,
            ]
            res = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if res.returncode == 0:
                print(f"[OK] 已精准投递至 Antigravity 会话 ({conv_id}): {clean_cmd}", flush=True)
                return True
            print(f"[WARN] 会话 {conv_id} 投递返回非 0 ({res.stderr.strip()})，尝试拉起新会话...", flush=True)

        cmd_new = [
            str(AGENTAPI_BIN),
            "new-conversation",
            "--title",
            f"{title}: {clean_cmd[:18]}",
            "--",
            wrapped_prompt,
        ]
        res2 = subprocess.run(cmd_new, env=env, capture_output=True, text=True)
        return res2.returncode == 0

    if host == "codex" and shutil.which("codex"):
        def _run_codex():
            cmd = ["codex", "exec", "--cd", workspace, wrapped_prompt]
            res = subprocess.run(cmd, capture_output=True, text=True)
            out_tail = (res.stdout or res.stderr or "执行完成").strip()[-600:]
            send_ntfy_message(
                server=ch.get("server", "https://ntfy.sh"),
                topic=ch["topic"],
                title=f"{title} | Codex 执行完成",
                message=f"**指令**\n`{clean_cmd}`\n\n**输出摘要**\n{out_tail}",
                auth_token=ch.get("auth_token", ""),
                keep_emoji=bool(ch.get("keep_emoji")),
            )
        threading.Thread(target=_run_codex, daemon=True).start()
        return True

    if host == "claude" and shutil.which("claude"):
        def _run_claude():
            cmd = ["claude"]
            if conv_id and conv_id not in ("auto", "new"):
                cmd.extend(["--resume", conv_id])
            else:
                cmd.append("--continue")
            cmd.extend(["-p", wrapped_prompt])
            res = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True)
            out_tail = (res.stdout or res.stderr or "执行完成").strip()[-600:]
            send_ntfy_message(
                server=ch.get("server", "https://ntfy.sh"),
                topic=ch["topic"],
                title=f"{title} | Claude 执行完成",
                message=f"**指令**\n`{clean_cmd}`\n\n**输出摘要**\n{out_tail}",
                auth_token=ch.get("auth_token", ""),
                keep_emoji=bool(ch.get("keep_emoji")),
            )
        threading.Thread(target=_run_claude, daemon=True).start()
        return True

    if AGENTAPI_BIN.is_file():
        env = get_agy_env()
        cmd_new = [
            str(AGENTAPI_BIN),
            "new-conversation",
            "--title",
            f"{title}: {clean_cmd[:18]}",
            "--",
            wrapped_prompt,
        ]
        res = subprocess.run(cmd_new, env=env, capture_output=True, text=True)
        return res.returncode == 0

    return False


def handle_meta_command(channel_id: str, ch: dict, message: str) -> bool:
    """处理手机端发来的秒级系统元指令（不唤醒 AI，0.2 秒直接响应）"""
    norm = re.sub(r"\s+", " ", message.strip().lower()).strip("。！!.,，")
    server = ch.get("server", "https://ntfy.sh")
    topic = ch["topic"]
    token = ch.get("auth_token", "")
    keep_emoji = bool(ch.get("keep_emoji"))
    title_prefix = ch.get("title") or ch.get("alias") or "远程对话"

    if norm in ("/status", "status", "状态", "查看状态"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        pwr = detect_power_and_sleep_status()
        caff_str = f"运行中 (PID {', '.join(pwr['sleep_guard_pids'])})" if pwr["sleep_guard_pids"] else "未开启"
        st_cn = "正常运行 (active)" if ch.get("status", "active") == "active" else "已暂停 (paused)"
        msg = (
            f"**频道状态**\n"
            f"- 绑定名称：{title_prefix}\n"
            f"- 运行状态：{st_cn}\n"
            f"- 宿主引擎：{ch.get('host', 'antigravity')}\n"
            f"- 绑定会话：`{ch.get('conv_id', '')}`\n"
            f"- 工作目录：`{ch.get('workspace', '')}`\n\n"
            f"**电脑与防休眠**\n"
            f"- 电源状态：{pwr['power_source']} ({pwr['battery_percent']})\n"
            f"- 防休眠锁：{caff_str}\n"
            f"- 最近指令：{ch.get('last_cmd_at') or '暂无'}"
        )
        send_ntfy_message(server, topic, f"{title_prefix} | 系统状态", msg, auth_token=token, keep_emoji=keep_emoji)
        return True

    if norm in ("/help", "help", "帮助", "指令"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        msg = (
            f"**直接发送文字或图片/文件**\n"
            f"- 直接打字发送指令，或点输入框旁回形针上传图片/文档，将唯一投递至绑定对话框（`{ch.get('conv_id', '')[:8]}...`）。\n"
            f"- AI 产出的图片、PDF、视频等也会直接作为预览大图或附件发回本频道。\n\n"
            f"**内置秒级元指令（0.2秒瞬回，不耗 Token）**\n"
            f"- `状态` 或 `/status`：查看电脑电量、防休眠与绑定状态\n"
            f"- `暂停` 或 `/pause`：暂停本频道的远程指令接收\n"
            f"- `恢复` 或 `/resume`：恢复本频道的远程指令接收\n"
            f"- `/sleep on` / `/sleep off`：远程强制开启或关闭电脑防休眠\n"
            f"- `销毁频道` 或 `/delete`：从电脑总控台解绑并停用本频道"
        )
        send_ntfy_message(server, topic, f"{title_prefix} | 指令帮助", msg, auth_token=token, keep_emoji=keep_emoji)
        return True

    if norm in ("/pause", "pause", "暂停", "暂停监听"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        reg = load_registry()
        if channel_id in reg.get("channels", {}):
            reg["channels"][channel_id]["status"] = "paused"
            save_registry(reg)
        send_ntfy_message(
            server,
            topic,
            f"{title_prefix} | 已暂停",
            "本频道已暂停转发业务指令。发送 `恢复` 或 `/resume` 可随时重新激活。",
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        return True

    if norm in ("/resume", "resume", "恢复", "恢复监听"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        reg = load_registry()
        if channel_id in reg.get("channels", {}):
            reg["channels"][channel_id]["status"] = "active"
            save_registry(reg)
        send_ntfy_message(
            server,
            topic,
            f"{title_prefix} | 已恢复",
            "本频道已恢复正常监听，现在可以直接发送指令。",
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        return True

    if norm in ("/sleep on", "sleep on", "开启防休眠", "防休眠开启"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        reg = load_registry()
        reg["sleep_guard"] = "on"
        save_registry(reg)
        send_ntfy_message(
            server,
            topic,
            f"{title_prefix} | 防休眠已强制开启",
            "已将电脑全局防休眠锁设为强制开启 (`on`)，守护进程将在 1-3 秒内挂载防休眠进程。",
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        return True

    if norm in ("/sleep off", "sleep off", "关闭防休眠", "防休眠关闭"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        reg = load_registry()
        reg["sleep_guard"] = "off"
        save_registry(reg)
        send_ntfy_message(
            server,
            topic,
            f"{title_prefix} | 防休眠已关闭",
            "已释放电脑防休眠锁 (`off`)，允许系统按默认电源计划休眠。",
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        return True

    if norm in ("/delete", "/rm", "销毁频道", "删除频道"):
        print(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}", flush=True)
        reg = load_registry()
        if channel_id in reg.get("channels", {}):
            reg["channels"].pop(channel_id, None)
            save_registry(reg)
        send_ntfy_message(
            server,
            topic,
            f"{title_prefix} | 频道已销毁",
            "本频道已从电脑总控台中解绑移除，后台不再监听此频道。你现在可以在手机 ntfy App 中长按删除此订阅。",
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        return True

    return False


class ChannelWorker(threading.Thread):
    """单个频道的常驻流式监听线程，支持断线补抓、防重放与手机附件下载"""

    def __init__(self, channel_id: str, ch_config: dict):
        super().__init__(daemon=True)
        self.channel_id = channel_id
        self.ch_config = ch_config
        self.stop_event = threading.Event()
        self.seen_ids: set[str] = set()
        self.start_ts = time.time()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        server = self.ch_config.get("server", "https://ntfy.sh").rstrip("/")
        topic = self.ch_config["topic"]
        token = self.ch_config.get("auth_token", "")
        print(
            f"[WORKER_START] 频道 {self.channel_id} ({topic}) -> 会话 {self.ch_config.get('conv_id')}",
            flush=True,
        )

        while not self.stop_event.is_set():
            try:
                reg = load_registry()
                curr_ch = reg.get("channels", {}).get(self.channel_id)
                if not curr_ch:
                    break
                self.ch_config = curr_ch
                last_id = curr_ch.get("last_event_id", "")

                stream_url = f"{server}/{topic}/json"
                if last_id:
                    stream_url += f"?since={urllib.parse.quote(last_id)}"

                headers = {"User-Agent": USER_AGENT}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                req = urllib.request.Request(stream_url, headers=headers)
                with urllib.request.urlopen(req, timeout=90) as resp:
                    for raw_line in resp:
                        if self.stop_event.is_set():
                            break
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except Exception:
                            continue

                        if ev.get("event") != "message":
                            continue

                        msg_id = ev.get("id", "")
                        if not msg_id or msg_id in self.seen_ids:
                            continue
                        self.seen_ids.add(msg_id)

                        had_last_id = bool(last_id)
                        reg_latest = load_registry()
                        if self.channel_id in reg_latest.get("channels", {}):
                            reg_latest["channels"][self.channel_id]["last_event_id"] = msg_id
                            save_registry(reg_latest)
                            self.ch_config = reg_latest["channels"][self.channel_id]
                            last_id = msg_id

                        # 过滤超过 300 秒的陈旧历史消息；若首次绑定无游标，跳过启动前的历史消息
                        ev_time = float(ev.get("time", 0) or 0)
                        if ev_time:
                            if abs(time.time() - ev_time) > 300:
                                continue
                            if not had_last_id and ev_time < (self.start_ts - 2):
                                continue

                        tags = ev.get("tags") or []
                        title = ev.get("title") or ""
                        message = (ev.get("message") or "").strip()
                        attachment = ev.get("attachment")

                        # 过滤由脚本自身发出的通知（带 robot 标签）
                        if "robot" in tags:
                            continue
                        # 若无附件且带系统标题，也视为外部自动通知而非人工输入
                        if title and not attachment:
                            continue
                        if not message and not attachment:
                            continue

                        if not attachment and handle_meta_command(self.channel_id, self.ch_config, message):
                            continue

                        if self.ch_config.get("status", "active") == "paused":
                            print(f"[PAUSED] 忽略已暂停频道 {self.channel_id} 的指令: {message}", flush=True)
                            continue

                        saved_att_path: Path | None = None
                        if isinstance(attachment, dict) and attachment.get("url"):
                            saved_att_path = download_incoming_attachment(
                                attachment,
                                workspace=self.ch_config.get("workspace", str(HOME)),
                                auth_token=token,
                            )
                            if not message or message.startswith("attachment"):
                                message = f"请查看并处理我从手机上传的文件：{attachment.get('name', '附件')}"

                        print(f"[CMD_RECV] [{self.channel_id}] 收到手机远程指令: {message}", flush=True)
                        reg_latest = load_registry()
                        if self.channel_id in reg_latest.get("channels", {}):
                            reg_latest["channels"][self.channel_id]["last_cmd_at"] = datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            reg_latest["channels"][self.channel_id]["last_cmd_text"] = message[:80]
                            save_registry(reg_latest)
                            self.ch_config = reg_latest["channels"][self.channel_id]

                        dispatch_command_to_host(
                            self.channel_id,
                            self.ch_config,
                            message,
                            attachment_path=saved_att_path,
                        )

            except Exception:
                if self.stop_event.is_set():
                    break
                time.sleep(3)

        print(f"[WORKER_STOP] 频道 {self.channel_id} 停止监听", flush=True)


def start_sleep_inhibitor() -> subprocess.Popen | None:
    """按操作系统启动防休眠子进程或状态锁"""
    if IS_MAC and Path("/usr/bin/caffeinate").is_file():
        return subprocess.Popen(
            ["/usr/bin/caffeinate", "-dims"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if IS_LINUX and shutil.which("systemd-inhibit"):
        return subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=ntfy-remote-chat",
                "--why=Active ntfy remote chat channel",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
        except Exception:
            pass
    return None


def stop_sleep_inhibitor(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass


def detect_other_ntfy_agents() -> list[dict]:
    """通用扫描系统中其他独立运行的 ntfy LaunchAgent 服务"""
    services = []
    if not IS_MAC or not LAUNCH_AGENTS_DIR.is_dir():
        return services
    for plist in sorted(LAUNCH_AGENTS_DIR.glob("*ntfy*.plist")):
        if plist.name in (f"{PLIST_LABEL}.plist", f"{OLD_PLIST_LABEL}.plist"):
            continue
        services.append(
            {
                "service_label": plist.stem,
                "plist_path": str(plist),
            }
        )
    return services


def run_daemon_loop() -> None:
    """统一多路复用守护进程主循环：动态热加载频道注册表 + 托管防休眠"""
    ensure_state_dir()
    PID_FILE_PATH.write_text(str(os.getpid()), encoding="utf-8")
    print(f"[HUB_INIT] ntfy_hub 守护进程已启动 (PID={os.getpid()})", flush=True)
    workers: dict[str, tuple[str, ChannelWorker]] = {}
    inhibitor_proc: subprocess.Popen | None = None

    def _cleanup(*_args):
        nonlocal inhibitor_proc
        for _, (_, w) in workers.items():
            w.stop()
        stop_sleep_inhibitor(inhibitor_proc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    while True:
        try:
            reg = load_registry()
            channels = reg.get("channels", {})
            sleep_mode = reg.get("sleep_guard", "auto")

            active_cids = set()
            for cid, ch in channels.items():
                if ch.get("status", "active") in ("active", "paused") and ch.get("topic"):
                    active_cids.add(cid)
                    topic = ch["topic"]
                    if cid not in workers or workers[cid][0] != topic or not workers[cid][1].is_alive():
                        if cid in workers:
                            workers[cid][1].stop()
                        w = ChannelWorker(cid, ch)
                        w.start()
                        workers[cid] = (topic, w)

            for cid in list(workers.keys()):
                if cid not in active_cids:
                    workers[cid][1].stop()
                    del workers[cid]

            has_any_active = (len(active_cids) > 0) or (len(detect_other_ntfy_agents()) > 0)
            should_inhibit = (sleep_mode == "on") or (sleep_mode == "auto" and has_any_active)

            if should_inhibit:
                if (IS_MAC or IS_LINUX) and (inhibitor_proc is None or inhibitor_proc.poll() is not None):
                    inhibitor_proc = start_sleep_inhibitor()
                    if inhibitor_proc:
                        print(f"[SLEEP_GUARD_ON] 已激活防休眠锁 (PID={inhibitor_proc.pid})", flush=True)
                elif IS_WIN:
                    start_sleep_inhibitor()
            else:
                if inhibitor_proc is not None and inhibitor_proc.poll() is None:
                    print(f"[SLEEP_GUARD_OFF] 已释放防休眠锁 (PID={inhibitor_proc.pid})", flush=True)
                    stop_sleep_inhibitor(inhibitor_proc)
                    inhibitor_proc = None

        except Exception as e:
            print(f"[HUB_WARN] 守护循环异常: {e}", flush=True)

        time.sleep(3)


def ensure_background_daemon() -> dict:
    """跨平台确保后台常驻守护服务正在运行"""
    ensure_state_dir()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    script_abs = os.path.abspath(__file__)

    if IS_MAC:
        LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        if OLD_PLIST_PATH.is_file():
            subprocess.run(["launchctl", "unload", str(OLD_PLIST_PATH)], capture_output=True)
            try:
                OLD_PLIST_PATH.unlink()
            except Exception:
                pass

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{PLIST_LABEL}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{sys.executable}</string>
\t\t<string>{script_abs}</string>
\t\t<string>daemon</string>
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>KeepAlive</key>
\t<true/>
\t<key>StandardOutPath</key>
\t<string>{LOG_PATH}</string>
\t<key>StandardErrorPath</key>
\t<string>{LOG_PATH}</string>
\t<key>WorkingDirectory</key>
\t<string>{STATE_DIR}</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
\t</dict>
\t<key>ThrottleInterval</key>
\t<integer>5</integer>
</dict>
</plist>
"""
        PLIST_PATH.write_text(plist_content, encoding="utf-8")

        uid = os.getuid()
        check_res = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{PLIST_LABEL}"],
            capture_output=True,
            text=True,
        )
        if check_res.returncode != 0:
            subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
            subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True)
            time.sleep(0.8)

        hub_pid = None
        if PID_FILE_PATH.is_file():
            hub_pid = PID_FILE_PATH.read_text(encoding="utf-8").strip()
        return {
            "manager": "launchd",
            "service_config": str(PLIST_PATH),
            "log_path": str(LOG_PATH),
            "hub_pid": hub_pid,
        }

    if PID_FILE_PATH.is_file():
        try:
            old_pid = int(PID_FILE_PATH.read_text(encoding="utf-8").strip())
            os.kill(old_pid, 0)
            return {
                "manager": "background-process",
                "log_path": str(LOG_PATH),
                "hub_pid": str(old_pid),
            }
        except Exception:
            pass

    log_f = open(LOG_PATH, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, script_abs, "daemon"],
        stdout=log_f,
        stderr=log_f,
        start_new_session=True,
    )
    time.sleep(0.5)
    return {
        "manager": "background-process",
        "log_path": str(LOG_PATH),
        "hub_pid": str(proc.pid),
    }


def cmd_bind(args: argparse.Namespace) -> int:
    workspace = os.path.abspath(args.workspace or os.getcwd())
    folder_name = os.path.basename(workspace.rstrip("/\\")) or "workspace"
    alias = args.alias or slugify_alias(folder_name)
    title = args.title or folder_name
    host = detect_host_ide(args.host)
    conv_id = (args.conv_id or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
    if not conv_id:
        conv_id = f"{host}-{alias}"

    get_agy_env()

    reg = load_registry()
    channels = reg.setdefault("channels", {})

    existing_cid, existing_ch = find_channel(reg, conv_id)
    channel_id = existing_cid or f"ch_{slugify_alias(alias)}_{conv_id[:8]}"

    if existing_ch and not args.rotate and not args.topic:
        topic = existing_ch["topic"]
    elif args.topic:
        topic = args.topic.strip()
    else:
        topic = generate_topic(alias)

    server = (args.server or "https://ntfy.sh").rstrip("/")
    web_url = f"{server}/{topic}"
    app_url = f"ntfy://{server.replace('https://', '').replace('http://', '')}/{topic}"

    qr_dir = Path(args.qr_dir).expanduser().resolve() if args.qr_dir else STATE_DIR / "qr"
    qr_path = qr_dir / f"ntfy_qr_{channel_id}.png"

    entry = {
        "channel_id": channel_id,
        "alias": alias,
        "title": title,
        "topic": topic,
        "server": server,
        "web_url": web_url,
        "app_url": app_url,
        "conv_id": conv_id,
        "workspace": workspace,
        "host": host,
        "auth_token": args.token or (existing_ch.get("auth_token", "") if existing_ch else ""),
        "send_ack": bool(args.ack),
        "keep_emoji": bool(args.keep_emoji),
        "status": "active",
        "created_at": existing_ch.get("created_at") if existing_ch else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_event_id": existing_ch.get("last_event_id", "") if existing_ch else "",
        "qr_path": str(qr_path),
    }

    if args.dry_run:
        print(
            json.dumps(
                {"dry_run": True, "channel": entry, "power": detect_power_and_sleep_status()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    channels[channel_id] = entry
    save_registry(reg)

    qr_ok, qr_mode = generate_qr_code(web_url, qr_path, allow_online=args.online_qr)
    daemon_info = ensure_background_daemon()
    time.sleep(1.0)
    power_info = detect_power_and_sleep_status()

    test_sent = False
    if not args.no_test:
        welcome_msg = (
            f"**绑定状态**\n"
            f"已一对一锁死绑定至当前对话，与其他频道彻底隔离。\n\n"
            f"**绑定详情**\n"
            f"- 频道名称：{title}\n"
            f"- 宿主环境：{host}\n"
            f"- 会话标识：`{conv_id}`\n"
            f"- 工作目录：`{workspace}`\n\n"
            f"**使用方法**\n"
            f"- 直接打字或点回形针发图片/文件即可唤醒电脑执行；\n"
            f"- 电脑产出的图片、PDF、视频等也会直接发回本频道预览；\n"
            f"- 发送 `状态` 查看电脑电量与防休眠状态。"
        )
        test_sent = send_ntfy_message(
            server=server,
            topic=topic,
            title=f"{title} | 远程对话已就绪",
            message=welcome_msg,
            auth_token=entry["auth_token"],
            keep_emoji=entry["keep_emoji"],
        )

    result = {
        "ok": True,
        "channel": entry,
        "qr_generated": qr_ok,
        "qr_mode": qr_mode,
        "test_notification_sent": test_sent,
        "daemon": daemon_info,
        "power_and_sleep": power_info,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    reg = load_registry()
    _, ch = find_channel(reg, args.channel)
    if not ch:
        # 允许直接把 --channel 作为临时 topic 发送
        if args.channel.startswith(("http://", "https://")) or len(args.channel) >= 12:
            ch = {
                "server": "https://ntfy.sh",
                "topic": args.channel.split("/")[-1],
                "title": "远程对话",
                "auth_token": "",
                "keep_emoji": args.keep_emoji,
            }
        else:
            print(f"ERROR: 未找到匹配的频道: {args.channel}", file=sys.stderr)
            return 1

    keep_emoji = args.keep_emoji or bool(ch.get("keep_emoji"))
    server = ch.get("server", "https://ntfy.sh")
    topic = ch["topic"]
    token = ch.get("auth_token", "")
    title = args.title or f"{ch.get('title', '远程对话')} | 任务完成"
    msg = args.msg or ""

    files = [Path(f).expanduser().resolve() for f in (args.file or [])]
    if not files:
        ok = send_ntfy_message(
            server=server,
            topic=topic,
            title=title,
            message=msg or "任务执行完毕。",
            priority=args.priority,
            auth_token=token,
            keep_emoji=keep_emoji,
            click_url=args.click_url,
        )
        if ok:
            print(f"[OK] 手机文字通知已送达 (Topic: {topic})")
            return 0
        return 1

    # 含有图片或文件时，逐个通过二进制 PUT 直传（第一份文件附带完整正文说明）
    all_ok = True
    for idx, fpath in enumerate(files):
        item_title = title if len(files) == 1 else f"{title} ({idx + 1}/{len(files)})"
        item_msg = msg if idx == 0 else f"附件 ({idx + 1}/{len(files)}): `{fpath.name}`"
        ok = send_ntfy_file(
            server=server,
            topic=topic,
            file_path=fpath,
            title=item_title,
            message=item_msg,
            priority=args.priority,
            auth_token=token,
            keep_emoji=keep_emoji,
        )
        if ok:
            print(f"[OK] 手机附件/图片已送达: {fpath.name} (Topic: {topic})")
        else:
            all_ok = False
    return 0 if all_ok else 1


def cmd_status(_args: argparse.Namespace) -> int:
    reg = load_registry()
    power = detect_power_and_sleep_status()
    other_agents = detect_other_ntfy_agents()
    channels_with_idx = []
    for idx, ch in enumerate(reg.get("channels", {}).values(), start=1):
        item = dict(ch)
        item["index"] = idx
        channels_with_idx.append(item)
    out = {
        "sleep_guard_mode": reg.get("sleep_guard", "auto"),
        "power_and_sleep": power,
        "hub_channels": channels_with_idx,
        "other_ntfy_services": other_agents,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_panel(_args: argparse.Namespace) -> int:
    reg = load_registry()
    power = detect_power_and_sleep_status()
    channels = list(reg.get("channels", {}).values())
    sleep_mode = reg.get("sleep_guard", "auto")
    pids = power.get("sleep_guard_pids") or []
    caff_str = f"运行中 (PID: {', '.join(pids)})" if pids else "未开启"

    lines = [
        "### ntfy 全局远程对话总控面板",
        "",
        f"- **电脑电源**：{power.get('power_source', 'unknown')} ({power.get('battery_percent', 'unknown')})",
        f"- **防休眠锁**：{caff_str}（策略：`{sleep_mode}`）",
        f"- **守护进程**：`io.agentskills.ntfy_hub` | 活跃频道数：**{len(channels)}**",
        "",
    ]

    if not channels:
        lines.append("> 当前暂无绑定的远程对话频道。在任意对话框输入 `/ntfy` 即可一键生成并绑定。")
    else:
        lines.extend(
            [
                "| 序号 | 频道名称 | 状态 | 专属 Topic | 绑定会话 ID (`conv_id`) | 工作目录 | 最近指令 |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for idx, ch in enumerate(channels, start=1):
            st = ch.get("status", "active")
            status_str = "运行中" if st == "active" else "已暂停"
            title = ch.get("title") or ch.get("alias") or ch.get("channel_id")
            topic = ch.get("topic", "")
            conv_id = ch.get("conv_id") or "GUI焦点模式"
            ws = ch.get("workspace") or "-"
            last_cmd = (ch.get("last_cmd_text") or "-").replace("|", "\\|").replace("\n", " ")
            if len(last_cmd) > 24:
                last_cmd = last_cmd[:24] + "..."
            lines.append(
                f"| **`#{idx}`** | **{title}** | `{status_str}` | `{topic}` | `{conv_id}` | `{ws}` | {last_cmd} |"
            )

        lines.extend(
            [
                "",
                "**快捷管理指令：**",
                "- 删除指定频道：`/ntfy rm 1`（或 `python3 ~/.gemini/config/skills/ntfy/scripts/ntfy_ctl.py rm 1`）",
                "- 暂停/恢复频道：`/ntfy pause 1` / `/ntfy resume 1`（或在手机端直接发 `暂停` / `恢复`）",
                "- 手机端自毁当前频道：在手机 ntfy 频道内直接发送 `销毁频道` 或 `/delete`",
                "- 清空全部频道：`/ntfy clear`",
            ]
        )

    print("\n".join(lines))
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    reg = load_registry()
    if getattr(args, "all", False):
        count = len(reg.get("channels", {}))
        reg["channels"] = {}
        save_registry(reg)
        print(json.dumps({"ok": True, "cleared_count": count}, ensure_ascii=False, indent=2))
        return 0

    query = getattr(args, "target", "") or getattr(args, "channel", "")
    if not query:
        print("ERROR: 请提供要删除的频道序号（如 1）、名称、会话 ID 或 Topic，或使用 --all 清空全部。", file=sys.stderr)
        return 1

    cid, ch = find_channel(reg, query)
    if not cid or not ch:
        print(f"ERROR: 未找到频道: {query}", file=sys.stderr)
        return 1

    removed = reg["channels"].pop(cid)
    save_registry(reg)

    if not getattr(args, "quiet", False):
        try:
            send_ntfy_message(
                server=removed.get("server", "https://ntfy.sh"),
                topic=removed["topic"],
                title=f"{removed.get('title', '远程对话')} | 频道已销毁",
                message=(
                    f"该对话的远程监听服务已从总控中心移除，后续消息将不再触发电脑端任务。\n"
                    f"- **移除频道**：`{removed.get('title', cid)}`\n"
                    f"- **原会话 ID**：`{removed.get('conv_id') or '未指定'}`"
                ),
                priority=3,
                auth_token=removed.get("auth_token", ""),
                keep_emoji=bool(removed.get("keep_emoji")),
            )
        except Exception:
            pass

    print(json.dumps({"ok": True, "unbound": removed}, ensure_ascii=False, indent=2))
    return 0


def cmd_pause_resume(args: argparse.Namespace, enable: bool) -> int:
    reg = load_registry()
    query = getattr(args, "target", "") or getattr(args, "channel", "")
    cid, ch = find_channel(reg, query)
    if not cid or not ch:
        print(f"ERROR: 未找到频道: {query}", file=sys.stderr)
        return 1
    new_status = "active" if enable else "paused"
    reg["channels"][cid]["status"] = new_status
    save_registry(reg)
    state_label = "已恢复运行" if enable else "已暂停监听"
    print(
        json.dumps(
            {"ok": True, "channel_id": cid, "title": ch.get("title"), "status": new_status, "label": state_label},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_sleep(args: argparse.Namespace) -> int:
    reg = load_registry()
    if args.mode in ("on", "off", "auto"):
        reg["sleep_guard"] = args.mode
        save_registry(reg)
        ensure_background_daemon()
        time.sleep(1.0)
    print(
        json.dumps(
            {"sleep_guard_mode": reg.get("sleep_guard", "auto"), "power": detect_power_and_sleep_status()},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跨平台、跨 AI IDE 手机 ntfy 双向远程对话控制中心")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_bind = sub.add_parser("bind", help="为当前对话框生成专属 ntfy 频道并开启防休眠与常驻监听")
    p_bind.add_argument("--conv-id", type=str, default="", help="锁死的对话框/会话 ID")
    p_bind.add_argument("--workspace", type=str, default="", help="绑定的工作目录路径")
    p_bind.add_argument("--alias", type=str, default="", help="频道英文短别名")
    p_bind.add_argument("--title", type=str, default="", help="手机通知显示的频道标题")
    p_bind.add_argument("--host", type=str, default="auto", choices=["auto", "antigravity", "codex", "claude", "gui"])
    p_bind.add_argument("--topic", type=str, default="", help="自定义 Topic（默认自动生成 160-bit 随机串）")
    p_bind.add_argument("--rotate", action="store_true", help="强制轮换生成全新的随机 Topic")
    p_bind.add_argument("--token", type=str, default="", help="可选的 ntfy Access Token (tk_...)")
    p_bind.add_argument("--server", type=str, default="https://ntfy.sh", help="ntfy 服务器地址")
    p_bind.add_argument("--qr-dir", type=str, default="", help="二维码 PNG 保存目录")
    p_bind.add_argument("--online-qr", action="store_true", help="允许请求外部二维码 API 生成图片（默认仅离线生成以保护 Topic 隐私）")
    p_bind.add_argument("--ack", action="store_true", help="收到手机指令时发送一条确认回执（默认静默唤醒）")
    p_bind.add_argument("--keep-emoji", action="store_true", help="保留通知中的 Emoji（默认自动剥离保持极简纯文字）")
    p_bind.add_argument("--no-test", action="store_true", help="绑定时不发送初始测试通知")
    p_bind.add_argument("--dry-run", action="store_true", help="仅预览配置，不写入也不启动守护进程")

    p_notify = sub.add_parser("notify", help="向绑定的手机 ntfy 频道发送文字、图片或文件结果通知")
    p_notify.add_argument("--channel", type=str, required=True, help="频道序号(#1)、频道 ID、会话 ID、别名或 Topic")
    p_notify.add_argument("--title", type=str, default="", help="通知标题")
    p_notify.add_argument("--msg", type=str, default="", help="通知正文 (Markdown)")
    p_notify.add_argument("--file", action="append", default=[], help="要直接发送给手机的图片或文件路径（可多次传入发送多图/多文件）")
    p_notify.add_argument("--click-url", type=str, default="", help="点击通知卡片跳转的外部链接")
    p_notify.add_argument("--priority", type=int, default=4, help="通知优先级 (1-5)")
    p_notify.add_argument("--keep-emoji", action="store_true", help="保留正文中的 Emoji（默认自动剥离）")

    sub.add_parser("status", help="输出 JSON 格式的防休眠、电源与所有频道状态")
    sub.add_parser("list", help="输出 JSON 格式的所有已绑定频道状态")
    sub.add_parser("panel", help="输出 Markdown 表格版全局总控面板（含 #1, #2 序号与管理指令）")

    for cmd_name in ("unbind", "rm", "delete"):
        p_rm = sub.add_parser(cmd_name, help="解绑并删除指定对话框的 ntfy 监听服务（支持序号如 1、别名、conv_id 或 Topic）")
        p_rm.add_argument("target", nargs="?", default="", help="要删除的频道序号(如 1 或 #1)、名称、会话 ID 或 Topic")
        p_rm.add_argument("--channel", type=str, default="", help="要删除的频道标识（兼容旧参数）")
        p_rm.add_argument("--all", action="store_true", help="清空所有已绑定的频道")
        p_rm.add_argument("--quiet", action="store_true", help="静默解绑，不向手机发送销毁通知")

    p_clear = sub.add_parser("clear", help="一键清空并停止所有已绑定的 ntfy 对话框频道")
    p_clear.add_argument("--quiet", action="store_true", help="静默清空")

    p_pause = sub.add_parser("pause", help="暂停指定频道的指令监听（支持序号 1、名称或 Topic）")
    p_pause.add_argument("target", nargs="?", default="", help="频道序号(1)、名称、会话 ID 或 Topic")
    p_pause.add_argument("--channel", type=str, default="", help="频道标识")

    p_resume = sub.add_parser("resume", help="恢复指定频道的指令监听（支持序号 1、名称或 Topic）")
    p_resume.add_argument("target", nargs="?", default="", help="频道序号(1)、名称、会话 ID 或 Topic")
    p_resume.add_argument("--channel", type=str, default="", help="频道标识")

    p_sleep = sub.add_parser("sleep", help="控制电脑防休眠模式 (auto/on/off)")
    p_sleep.add_argument("--mode", type=str, default="status", choices=["auto", "on", "off", "status"])

    sub.add_parser("daemon", help="启动后台多路复用常驻守护循环")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "bind":
        return cmd_bind(args)
    if args.subcommand == "notify":
        return cmd_notify(args)
    if args.subcommand in ("status", "list"):
        return cmd_status(args)
    if args.subcommand == "panel":
        return cmd_panel(args)
    if args.subcommand in ("unbind", "rm", "delete"):
        return cmd_unbind(args)
    if args.subcommand == "clear":
        args.all = True
        return cmd_unbind(args)
    if args.subcommand == "pause":
        return cmd_pause_resume(args, enable=False)
    if args.subcommand == "resume":
        return cmd_pause_resume(args, enable=True)
    if args.subcommand == "sleep":
        return cmd_sleep(args)
    if args.subcommand == "daemon":
        run_daemon_loop()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
