#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ntfy_ctl.py
-----------
跨平台、跨 AI IDE 通用手机 ntfy 双向远程对话控制中心
(Multi-Channel Hub + Cross-OS Sleep Guard + Multi-IDE Adapter + Bidirectional File/Image Transfer)。

核心特性：
1. 严格 1 对 1 隔离：每个对话框生成独立的高熵随机 Topic（不含用户名/项目名），绝不扫描其他会话。
2. 双向图片与文件传输：
   - 电脑 -> 手机：`notify --file <path>` 推送图片（大图预览）、文本文档（内联展示）、PDF/Office/压缩包/视频（直链卡片）。
   - 手机 -> 电脑：手机上传的附件（仅限同一 ntfy 服务器的附件链接）自动下载至工作目录 `.ntfy_inbox/`。
3. 跨平台防休眠联动：macOS `caffeinate -ims` / Linux `systemd-inhibit` / Windows `SetThreadExecutionState`；
   电池低电量时自动释放；全部解绑后自动释放。
4. 断线补抓与防重放：持久化 `last_event_id`，重连时 `?since=` 补抓；超过 300 秒的陈旧指令不执行，并回执告知手机。
5. 每个频道串行执行：同一频道的指令排队逐条执行，带超时；执行失败或宿主不支持时回执告知手机。
6. 手机端秒级元指令：`/status`、`/help`、`/pause`、`/resume`、`/sleep on|off|auto`、`/delete`。
   电脑端总控面板：`panel`、`rm <序号>`、`pause/resume <序号>`、`clear`。
7. 跨 IDE 投递适配器：Antigravity (`agentapi`)、Claude Code (`claude -p`)、Codex (`codex exec`)。
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import queue
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

VERSION = "1.2.0"

HOME = Path.home()
STATE_DIR = HOME / ".agents" / "state" / "ntfy-remote-chat"
LEGACY_HUB_DIR = HOME / ".gemini" / "config" / "ntfy_hub"
REGISTRY_PATH = STATE_DIR / "registry.json"
REGISTRY_LOCK_PATH = STATE_DIR / "registry.lock"
DAEMON_LOCK_PATH = STATE_DIR / "daemon.lock"
ENV_CACHE_PATH = STATE_DIR / "agy_env_cache.json"
PID_FILE_PATH = STATE_DIR / "daemon.pid"
INHIBITOR_PID_PATH = STATE_DIR / "inhibitor.pid"

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WIN = sys.platform == "win32"

LOG_PATH = (HOME / "Library" / "Logs" / "ntfy_hub.log") if IS_MAC else (STATE_DIR / "ntfy_hub.log")
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
PLIST_LABEL = "io.agentskills.ntfy_hub"
OLD_PLIST_LABEL = "com.evan.ntfy_hub"  # 旧版本标签，仅用于自动迁移清理
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{PLIST_LABEL}.plist"
OLD_PLIST_PATH = LAUNCH_AGENTS_DIR / f"{OLD_PLIST_LABEL}.plist"
SYSTEMD_UNIT_NAME = "ntfy-remote-chat.service"
SYSTEMD_UNIT_PATH = HOME / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
WIN_STARTUP_PATH = (
    Path(os.environ.get("APPDATA", str(HOME)))
    / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "ntfy_remote_chat.cmd"
)
AGENTAPI_BIN = HOME / ".gemini" / "antigravity" / "bin" / "agentapi"
USER_AGENT = f"NtfyRemoteChat-Skill/{VERSION}"

DEFAULT_SERVER = "https://ntfy.sh"
MAX_NTFY_FREE_BYTES = 14 * 1024 * 1024  # 14MB 安全阈值（ntfy.sh 免费版单附件上限 15MB）
MAX_INCOMING_BYTES = 50 * 1024 * 1024  # 手机上传附件的下载上限
NTFY_MSG_MAX_BYTES = 3800  # ntfy 正文上限 4096 字节，超出会被服务器转成附件，留出余量
STALE_SECONDS = 300
DEFAULT_TASK_TIMEOUT = 1800
DEFAULT_MIN_BATTERY = 20
DEFAULT_CLAUDE_PERMISSION_MODE = "acceptEdits"
DEFAULT_CODEX_SANDBOX = "workspace-write"

# 敏感文件名：notify / NTFY_FILE 默认拒绝推送
SENSITIVE_NAME_PATTERN = re.compile(
    r"(^\.env($|\.)|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$"
    r"|credential|secret|\.netrc$|\.npmrc$|\.pypirc$)",
    re.IGNORECASE,
)

# 装饰性 Emoji：只匹配真正的 Emoji 区段与少量常见装饰符号（不误删 ✓ ★ → 等正文符号），
# 连同后面的修饰符 / ZWJ 组合序列和紧随其后的一个空格一起移除，不改动其它空白（保护 Markdown 缩进）。
_EMOJI_BASE = (
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "✅❌❎✨⭐⭕⌚⌛⏰-⏳"
    "⚠⚡❓❗❤"
)
EMOJI_PATTERN = re.compile(
    f"[{_EMOJI_BASE}](?:[️⃣\U0001F3FB-\U0001F3FF]|‍[{_EMOJI_BASE}♀♂⚕⚖✈])*[ ]?"
)


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def strip_emojis(text: str) -> str:
    """移除装饰性 Emoji，保留干净的 Markdown 文本（不压缩空白，保护代码缩进）"""
    if not text:
        return ""
    cleaned = EMOJI_PATTERN.sub("", text).replace("️", "")
    return cleaned.strip()


def decode_cli_text(text: str) -> str:
    """命令行 `--msg "a\\nb"` 在 bash 双引号里传进来的是字面 `\\n`；若正文没有真实换行，则把它转成换行。"""
    if not text:
        return ""
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text


def truncate_utf8(text: str, max_bytes: int, suffix: str = "") -> str:
    """按 UTF-8 字节截断（中文 3 字节/字），避免超出 ntfy 正文上限"""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    budget = max(0, max_bytes - len(suffix.encode("utf-8")))
    return raw[:budget].decode("utf-8", errors="ignore") + suffix


def is_sensitive_file(path: Path) -> bool:
    return bool(SENSITIVE_NAME_PATTERN.search(path.name))


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 文件锁与注册表（多线程 + 多进程安全）
# ---------------------------------------------------------------------------


class FileLock:
    """跨平台独占文件锁（Unix: fcntl.flock / Windows: msvcrt.locking）"""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self, blocking: bool = True) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt

                self.fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                        return True
                    except OSError:
                        if not blocking:
                            raise
                        time.sleep(0.05)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(self.fh.fileno(), flags)
                return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if IS_WIN:
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


_REGISTRY_THREAD_LOCK = threading.RLock()


def _chmod_private(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _chmod_private(STATE_DIR, 0o700)
    old_reg = LEGACY_HUB_DIR / "registry.json"
    if not REGISTRY_PATH.is_file() and old_reg.is_file():
        try:
            shutil.copy2(old_reg, REGISTRY_PATH)
        except Exception:
            pass
    if REGISTRY_PATH.is_file():
        _chmod_private(REGISTRY_PATH)


def _default_registry() -> dict:
    return {"version": 1, "sleep_guard": "auto", "min_battery": DEFAULT_MIN_BATTERY, "channels": {}}


def load_registry() -> dict:
    """只读加载（写入是原子替换，读不需要加锁）"""
    ensure_state_dir()
    if REGISTRY_PATH.is_file():
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "channels" in data:
                return data
        except Exception:
            pass
    return _default_registry()


def _write_registry(reg: dict) -> None:
    ensure_state_dir()
    fd, tmp_name = tempfile.mkstemp(dir=str(STATE_DIR), prefix="registry.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        _chmod_private(Path(tmp_name))
        os.replace(tmp_name, REGISTRY_PATH)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def registry_txn():
    """读-改-写事务：持有跨进程文件锁，避免并发写丢失更新或"复活"已解绑频道"""
    ensure_state_dir()
    with _REGISTRY_THREAD_LOCK, FileLock(REGISTRY_LOCK_PATH):
        reg = load_registry()
        yield reg
        _write_registry(reg)


def update_channel(channel_id: str, **fields) -> dict | None:
    """仅当频道仍存在时更新字段，返回更新后的配置"""
    with registry_txn() as reg:
        ch = reg.get("channels", {}).get(channel_id)
        if ch is None:
            return None
        ch.update(fields)
        return dict(ch)


def slugify_alias(raw: str) -> str:
    """将工作目录名或别名转为短标签（仅用于本地 channel_id，不进入 Topic）"""
    ascii_only = re.sub(r"[^a-zA-Z0-9]+", "", raw).lower()
    return ascii_only[:10] if ascii_only else "chat"


def generate_topic() -> str:
    """生成 ~166 bits 熵值的随机 Topic，不含用户名/项目名，避免在公共服务器上暴露身份"""
    alphabet = string.ascii_letters + string.digits
    return "nrc_" + "".join(secrets.choice(alphabet) for _ in range(28))


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "****" if len(value) > 8 else "****"


def is_ntfy_sh(server: str) -> bool:
    return urllib.parse.urlparse(server).netloc.lower() in ("ntfy.sh", "www.ntfy.sh")


# ---------------------------------------------------------------------------
# 进程 / 电源检测
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """跨平台判断进程是否存活。注意：Windows 上 os.kill(pid, 0) 会直接杀掉进程，不能用。"""
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def get_battery_info() -> tuple[bool | None, int | None]:
    """返回 (是否电池供电, 电量百分比)；未知时为 None"""
    on_battery: bool | None = None
    percent: int | None = None
    try:
        if IS_MAC:
            out = subprocess.check_output(["pmset", "-g", "batt"], text=True, stderr=subprocess.DEVNULL, timeout=5)
            if "Battery Power" in out:
                on_battery = True
            elif "AC Power" in out:
                on_battery = False
            m = re.search(r"(\d+)%", out)
            if m:
                percent = int(m.group(1))
        elif IS_LINUX:
            for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
                cap = (bat / "capacity").read_text(encoding="ascii").strip()
                status = (bat / "status").read_text(encoding="ascii").strip().lower()
                percent = int(cap)
                on_battery = status == "discharging"
                break
        elif IS_WIN:
            import ctypes

            class _SPS(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            sps = _SPS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
                if sps.BatteryFlag != 128 and sps.ACLineStatus in (0, 1):
                    on_battery = sps.ACLineStatus == 0
                if sps.BatteryLifePercent != 255:
                    percent = int(sps.BatteryLifePercent)
    except Exception:
        pass
    return on_battery, percent


def detect_power_and_sleep_status() -> dict:
    """跨平台检测当前电源状态、本 Skill 的防休眠锁与合盖睡眠风险"""
    on_battery, pct = get_battery_info()
    info = {
        "platform": sys.platform,
        "power_source": "Unknown" if on_battery is None else ("Battery Power (电池供电)" if on_battery else "AC Power (已插电源)"),
        "battery_percent": f"{pct}%" if pct is not None else "Unknown",
        "sleep_guard_pids": [],
        "daemon_pid": None,
        "sleep_note": "",
    }
    daemon_pid = read_pid(PID_FILE_PATH)
    if daemon_pid and is_pid_alive(daemon_pid):
        info["daemon_pid"] = str(daemon_pid)
    inh_pid = read_pid(INHIBITOR_PID_PATH)
    if inh_pid and is_pid_alive(inh_pid):
        info["sleep_guard_pids"].append(str(inh_pid))

    if IS_MAC:
        disablesleep = "0"
        try:
            pm_out = subprocess.check_output(["pmset", "-g"], text=True, stderr=subprocess.DEVNULL, timeout=5)
            m_ds = re.search(r"disablesleep\s+(\d+)", pm_out)
            if m_ds:
                disablesleep = m_ds.group(1)
        except Exception:
            pass
        info["disablesleep"] = disablesleep
        if disablesleep == "0":
            info["sleep_note"] = (
                "防休眠锁只阻止空闲睡眠；若拔掉外接显示器并物理合上 MacBook 盖子，系统仍会进入合盖睡眠。"
                "如需合盖运行，请插电并外接显示器，或开盖放置。"
            )
        else:
            info["sleep_note"] = "系统已开启强制禁止睡眠 (disablesleep=1)，支持合盖运行。"
    elif IS_LINUX:
        info["sleep_note"] = "Linux 通过 systemd-inhibit 阻止空闲与挂起；合盖行为由 logind 的 HandleLidSwitch 决定。"
    elif IS_WIN:
        info["sleep_note"] = "Windows 通过 SetThreadExecutionState 阻止系统空闲睡眠；合盖行为由电源计划决定。"
    return info


# ---------------------------------------------------------------------------
# 宿主 IDE 探测
# ---------------------------------------------------------------------------


def discover_antigravity_env() -> dict | None:
    """动态从系统中探测运行中的 Antigravity 实例与桥接凭证（支持重启后秒级自愈）"""
    if IS_WIN:
        return None
    try:
        ps = subprocess.check_output(["ps", "aux"], text=True, timeout=10)
        for line in ps.splitlines():
            if "language_server" in line and "--csrf_token" in line:
                m_csrf = re.search(r"--csrf_token\s+([a-zA-Z0-9\-]+)", line)
                parts = line.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                lsof_out = subprocess.run(
                    ["lsof", "-nP", "-p", parts[1]], capture_output=True, text=True, timeout=10
                ).stdout
                ports = re.findall(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lsof_out)
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
        env.update(discovered)
        try:
            ENV_CACHE_PATH.write_text(json.dumps(discovered), encoding="utf-8")
            _chmod_private(ENV_CACHE_PATH)
        except Exception:
            pass
        return env
    if ENV_CACHE_PATH.is_file():
        try:
            cached = json.loads(ENV_CACHE_PATH.read_text(encoding="utf-8"))
            for k, v in cached.items():
                if v and not env.get(k):
                    env[k] = v
        except Exception:
            pass
    return env


def detect_host_ide(preferred: str = "auto") -> str:
    """检测当前宿主：优先看当前进程的环境变量（谁在调用本脚本），再看本机安装情况"""
    if preferred and preferred != "auto":
        return preferred.lower()
    env = os.environ
    if env.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "antigravity"
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    if any(k.startswith("CODEX_") for k in env):
        return "codex"
    if discover_antigravity_env() or AGENTAPI_BIN.is_file():
        return "antigravity"
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    return "gui"


def detect_conv_id(host: str) -> str:
    env = os.environ
    if host == "antigravity":
        return env.get("ANTIGRAVITY_CONVERSATION_ID", "")
    if host == "claude":
        return env.get("CLAUDE_CODE_SESSION_ID", "")
    if host == "codex":
        return env.get("CODEX_THREAD_ID", "") or env.get("CODEX_SESSION_ID", "")
    return ""


# ---------------------------------------------------------------------------
# ntfy 发送
# ---------------------------------------------------------------------------


def _auth_headers(auth_token: str) -> dict:
    return {"Authorization": f"Bearer {auth_token}"} if auth_token else {}


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
    """向指定 ntfy 频道发送 Markdown 文本通知（自动按字节截断，避免被服务器转成附件）"""
    clean_title = title.strip() if keep_emoji else strip_emojis(title)
    clean_msg = message.strip() if keep_emoji else strip_emojis(message)
    clean_msg = truncate_utf8(clean_msg, NTFY_MSG_MAX_BYTES, "\n\n...(内容过长已截断)")

    payload: dict = {
        "topic": topic,
        "title": truncate_utf8(clean_title or "远程对话通知", 250),
        "message": clean_msg,
        "markdown": True,
        "priority": priority,
        "tags": ["robot"],
    }
    if click_url:
        payload["click"] = click_url

    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}
    headers.update(_auth_headers(auth_token))
    req = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status in (200, 201, 202)
    except Exception as e:
        print(f"[WARN] ntfy 推送失败 ({topic}): {e}", file=sys.stderr, flush=True)
        return False


def compress_video_for_preview(src_path: Path) -> Path | None:
    """视频超过上限时，用 ffmpeg 压制 480p 手机预览版（-fs 保证不超过上限，超长视频只保留前段）"""
    if not shutil.which("ffmpeg"):
        return None
    tmp_dir = Path(tempfile.gettempdir()) / "ntfy_preview"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"preview_{src_path.stem}.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-vf", "scale='min(480,iw)':-2",
        "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-fs", str(MAX_NTFY_FREE_BYTES - 512 * 1024),
        str(out_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=900)
    except Exception:
        return None
    if res.returncode == 0 and out_path.is_file() and 0 < out_path.stat().st_size <= MAX_NTFY_FREE_BYTES:
        return out_path
    return None


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TEXT_DOC_SUFFIXES = {".md", ".txt", ".csv", ".json", ".log", ".yaml", ".yml"}
CODE_FENCE_SUFFIXES = {".csv", ".json", ".log", ".yaml", ".yml"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi"}


def upload_to_ntfy_file_store(server: str, topic: str, upload_path: Path, auth_token: str = "") -> str:
    """将非图片文件上传至寄存子通道 `<topic>_files` 并返回附件直链"""
    put_url = f"{server.rstrip('/')}/{topic}_files?{urllib.parse.urlencode({'filename': upload_path.name})}"
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/octet-stream"}
    headers.update(_auth_headers(auth_token))
    req = urllib.request.Request(put_url, data=upload_path.read_bytes(), headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return (body.get("attachment") or {}).get("url", "")


def _expiry_note(server: str) -> str:
    return "\n*(ntfy.sh 免费附件链接约 3 小时后失效，请及时保存)*" if is_ntfy_sh(server) else ""


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
    向指定 ntfy 频道发送本地图片、文档或视频（兼容 iOS / Android / Web）：
    1. 图片：PUT 到主频道，手机直接渲染大图；
    2. 文本文档：正文内联展示（按字节截断）+ 原文件直链；
    3. 二进制文档或视频：上传到 `<topic>_files` 取直链，发送可点击卡片（iOS App 不显示非图片附件按钮）。
    """
    if not file_path.is_file():
        print(f"[WARN] 附件文件不存在: {file_path}", file=sys.stderr)
        return False

    suffix = file_path.suffix.lower()
    upload_path = file_path
    extra_note = ""
    file_size = file_path.stat().st_size

    if file_size > MAX_NTFY_FREE_BYTES:
        if suffix in VIDEO_SUFFIXES:
            compressed = compress_video_for_preview(file_path)
            if not compressed:
                print(f"[WARN] 视频超过 14MB 且无法压缩（需安装 ffmpeg）: {file_path}", file=sys.stderr)
                return False
            upload_path = compressed
            extra_note = (
                f"\n\n*(原视频 {file_size / 1048576:.1f}MB 超过免费通道上限，"
                f"已生成 {compressed.stat().st_size / 1048576:.1f}MB 手机预览版，超长视频只保留前段)*"
            )
        else:
            print(f"[WARN] 文件大小 ({file_size / 1048576:.1f}MB) 超过 ntfy 单文件上限", file=sys.stderr)
            return False

    clean_title = (title.strip() if keep_emoji else strip_emojis(title)) or f"文件推送 | {file_path.name}"
    full_msg = (message + extra_note).strip()
    clean_msg = full_msg if keep_emoji else strip_emojis(full_msg)

    # 情况 A：图片 -> 直接 PUT 到主频道
    if suffix in IMAGE_SUFFIXES:
        query_params = {
            "filename": upload_path.name,
            "title": truncate_utf8(clean_title, 250),
            "priority": str(priority),
            "tags": "robot",
            "markdown": "yes",
        }
        if clean_msg:
            query_params["message"] = truncate_utf8(clean_msg, 1500, "...")
        put_url = f"{server.rstrip('/')}/{topic}?{urllib.parse.urlencode(query_params)}"
        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/octet-stream"}
        headers.update(_auth_headers(auth_token))
        try:
            req = urllib.request.Request(put_url, data=upload_path.read_bytes(), headers=headers, method="PUT")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status in (200, 201, 202)
        except Exception as e:
            print(f"[WARN] ntfy 图片上传失败 ({file_path.name}): {e}", file=sys.stderr, flush=True)
            return False

    # 情况 B：非图片
    try:
        file_url = upload_to_ntfy_file_store(server, topic, upload_path, auth_token=auth_token)
    except Exception as e:
        print(f"[WARN] ntfy 文档上传失败 ({file_path.name}): {e}", file=sys.stderr, flush=True)
        file_url = ""

    size_kb = upload_path.stat().st_size / 1024.0
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024.0:.2f} MB"

    tail_parts = []
    if file_url:
        tail_parts.append(
            f"---\n**[点击在浏览器打开/下载：{upload_path.name} ({size_str})]({file_url})**{_expiry_note(server)}"
        )
    elif suffix not in TEXT_DOC_SUFFIXES:
        tail_parts.append(f"*(文件 `{upload_path.name}` 上传失败)*")

    head = clean_msg
    if suffix in TEXT_DOC_SUFFIXES:
        try:
            doc_text = upload_path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            doc_text = ""
        if doc_text:
            # 文档正文属于用户内容：不剥离 Emoji、不改空白；结构化文本放进代码块保留缩进
            fence = suffix in CODE_FENCE_SUFFIXES
            used = len(head.encode("utf-8")) + sum(len(p.encode("utf-8")) for p in tail_parts) + 120
            budget = max(200, NTFY_MSG_MAX_BYTES - used)
            doc_text = truncate_utf8(doc_text, budget, "\n...(长文已截断，请点击下方链接查看完整原文件)")
            body = f"```\n{doc_text}\n```" if fence else doc_text
            head = f"{head}\n\n---\n{body}" if head else body
    elif file_url:
        head = (head + "\n\n" if head else "") + f"**文件附件**\n- 文件名：`{upload_path.name}` ({size_str})"

    combined = "\n\n".join([p for p in [head] + tail_parts if p])
    if not file_url and suffix not in TEXT_DOC_SUFFIXES:
        send_ntfy_message(server, topic, clean_title, combined, priority, auth_token, keep_emoji=True)
        return False
    return send_ntfy_message(
        server=server,
        topic=topic,
        title=clean_title,
        message=combined,
        priority=priority,
        auth_token=auth_token,
        keep_emoji=True,  # 前面已按需剥离过，避免二次处理文档正文
        click_url=file_url,
    )


def is_trusted_attachment_url(url: str, server: str) -> bool:
    """只信任与频道同一 ntfy 服务器托管的附件；拒绝 `X-Attach` 外部链接，避免泄露 token 或被诱导下载任意内容"""
    try:
        u = urllib.parse.urlparse(url)
        s = urllib.parse.urlparse(server)
    except Exception:
        return False
    return u.scheme == s.scheme and u.netloc.lower() == s.netloc.lower() and u.path.startswith("/file/")


def download_incoming_attachment(att: dict, workspace: str, server: str, auth_token: str = "") -> Path | None:
    """手机上传的附件下载到工作目录 `.ntfy_inbox/`（目录内自带 .gitignore，不会被提交）"""
    att_url = att.get("url", "")
    if not att_url or not is_trusted_attachment_url(att_url, server):
        log(f"[WARN] 拒绝下载非本服务器托管的附件: {att_url}")
        return None
    raw_name = att.get("name") or "attachment.bin"
    safe_name = re.sub(r"[^\w.\-一-龥]+", "_", raw_name).lstrip(".") or "attachment.bin"
    inbox_dir = Path(workspace) / ".ntfy_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    gi = inbox_dir / ".gitignore"
    if not gi.is_file():
        with contextlib.suppress(Exception):
            gi.write_text("*\n", encoding="utf-8")
    dest_path = inbox_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"

    headers = {"User-Agent": USER_AGENT}
    headers.update(_auth_headers(auth_token))
    try:
        req = urllib.request.Request(att_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                return None
            total = 0
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_INCOMING_BYTES:
                        raise ValueError("附件超过 50MB 下载上限")
                    f.write(chunk)
            return dest_path
    except Exception as e:
        log(f"[WARN] 下载手机端附件失败 ({att_url}): {e}")
        with contextlib.suppress(Exception):
            dest_path.unlink()
    return None


def generate_qr_code(url: str, out_path: Path, allow_online: bool = False) -> tuple[bool, str]:
    """生成订阅二维码：默认仅离线生成；只有显式 `--online-qr` 才请求外部接口"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("qrencode"):
        res = subprocess.run(["qrencode", "-o", str(out_path), "-s", "8", "-m", "2", url], capture_output=True)
        if res.returncode == 0 and out_path.is_file():
            return True, "offline:qrencode"
    try:
        import qrcode  # type: ignore

        qrcode.make(url).save(str(out_path))
        if out_path.is_file():
            return True, "offline:python-qrcode"
    except Exception:
        pass
    if allow_online:
        qr_api = f"https://api.qrserver.com/v1/create-qr-code/?size=360x360&margin=12&data={urllib.parse.quote(url, safe='')}"
        try:
            with urllib.request.urlopen(urllib.request.Request(qr_api, headers={"User-Agent": USER_AGENT}), timeout=10) as resp:
                if resp.status == 200:
                    out_path.write_bytes(resp.read())
                    return True, "online:qrserver"
        except Exception:
            pass
    return False, "skipped:offline-privacy-default"


def find_channel(reg: dict, selector: str) -> tuple[str | None, dict | None]:
    """支持按序号 (1, #1)、channel_id、conv_id、alias、title 或 topic 查找频道配置"""
    channels = reg.get("channels", {})
    sel = (selector or "").strip()
    if not sel:
        return None, None
    if sel in channels:
        return sel, channels[sel]
    for cid, ch in channels.items():
        if sel in (ch.get("conv_id"), ch.get("topic")):
            return cid, ch
    # 总控面板序号（如 "1"、"#1"）
    idx_str = sel.lstrip("#")
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        items = list(channels.items())
        if 0 <= idx < len(items):
            return items[idx]
    for cid, ch in channels.items():
        if sel in (ch.get("alias"), ch.get("title")):
            return cid, ch
    return None, None


# ---------------------------------------------------------------------------
# 投递到宿主 IDE
# ---------------------------------------------------------------------------

NTFY_FILE_RE = re.compile(r"^\s*NTFY_FILE:\s*(.+?)\s*$", re.MULTILINE)


def _ch_send(ch: dict, title: str, message: str, priority: int = 4, click_url: str = "") -> bool:
    return send_ntfy_message(
        server=ch.get("server", DEFAULT_SERVER),
        topic=ch["topic"],
        title=title,
        message=message,
        priority=priority,
        auth_token=ch.get("auth_token", ""),
        keep_emoji=bool(ch.get("keep_emoji")),
        click_url=click_url,
    )


def build_prompt(channel_id: str, ch: dict, clean_cmd: str, attachment_path: Path | None, auto_reply: bool) -> str:
    title = ch.get("title") or ch.get("alias") or "远程对话"
    workspace = ch.get("workspace", str(HOME))
    style_rule = "排版保持极简分段，不要使用 Emoji" if not ch.get("keep_emoji") else "采用清晰的 Markdown 分段排版"
    att_block = ""
    if attachment_path:
        att_block = (
            f"\n【手机随附上传的文件/图片已自动下载至本地】：\n`{attachment_path}`\n"
            f"请直接读取/查看该文件并结合指令处理。\n"
        )
    head = (
        f"【收到来自手机 ntfy 专属频道 ({title}) 的远程指令】：\n「{clean_cmd}」\n{att_block}\n"
        f"请在工作目录 `{workspace}` 下按照该指令执行任务。"
        f"不要读取或外发密钥文件（.env、私钥、凭证等）。\n"
    )
    if auto_reply:
        return head + (
            f"你的最终回复会被自动推送到用户手机（{style_rule}，控制在 2000 字以内）。\n"
            f"如需把工作目录内生成的图片/文档/视频发到手机，在回复末尾为每个文件单独写一行，"
            f"格式为 `NTFY_FILE: /绝对路径/产物.png`（不带反引号）。"
        )
    py = sys.executable or "python3"
    script_path = os.path.abspath(__file__)
    keep_emoji_flag = " --keep-emoji" if ch.get("keep_emoji") else ""
    return head + (
        f"任务执行完毕后，请调用以下命令将结果推送回手机（{style_rule}）：\n"
        f"- 文字汇报（正文多行时用 --msg-file - 从标准输入读取，避免引号转义问题）：\n"
        f"  `\"{py}\" \"{script_path}\" notify --channel \"{channel_id}\" --title \"{title} | 任务完成\" --msg-file -{keep_emoji_flag} <<'EOF'`\n"
        f"  （正文）\n  `EOF`\n"
        f"- 若产出了图片/文档/视频，追加 `--file \"/绝对路径/产物.png\"`（可传多个）。"
    )


# 从当前 IDE 会话继承来的标记变量：传给子进程会让 CLI 误以为自己嵌套在别的会话里
_INHERITED_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SSE_PORT", "CODEX_THREAD_ID", "CODEX_SESSION_IDS", "CODEX_TOOL_CALL_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
)


def clean_child_env(ch: dict | None = None) -> dict:
    """子进程环境：去掉会话标记变量；补上绑定时记录的 PATH（launchd/systemd 的 PATH 很精简，nvm 装的 CLI 会找不到）"""
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_SESSION_VARS}
    bind_path = (ch or {}).get("env_path", "")
    if bind_path:
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        for d in bind_path.split(os.pathsep):
            if d and d not in parts:
                parts.append(d)
        env["PATH"] = os.pathsep.join(parts)
    return env


def is_auto_conv_id(ch: dict) -> bool:
    """会话 ID 是否为自动生成的占位值（兼容 1.1 版本没有 conv_id_auto 字段的旧频道）"""
    if "conv_id_auto" in ch:
        return bool(ch["conv_id_auto"])
    conv_id = ch.get("conv_id", "")
    return not conv_id or conv_id in ("auto", "new") or conv_id == f"{ch.get('host', '')}-{ch.get('alias', '')}"


def resolve_host_bin(ch: dict, name: str) -> str | None:
    """优先用绑定时记录的绝对路径，其次在（补全后的）PATH 中查找"""
    saved = ch.get("host_bin", "")
    if saved and Path(saved).is_file():
        return saved
    return shutil.which(name, path=clean_child_env(ch).get("PATH"))


def _run_with_timeout(
    cmd: list[str], cwd: str | None, timeout: int, env: dict | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """提示词经标准输入传入：避免 Windows .cmd 包装器破坏含换行/引号/& 的参数，也不受命令行长度限制"""
    kwargs: dict = {"stdin": subprocess.DEVNULL} if input_text is None else {"input": input_text}
    return subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, **kwargs,
    )


def run_claude(ch: dict, prompt: str, timeout: int) -> tuple[bool, str]:
    exe = resolve_host_bin(ch, "claude")
    if not exe:
        return False, "后台进程找不到 claude 命令，请在装有 claude 的终端里重新运行 bind"
    workspace = ch.get("workspace", str(HOME))
    conv_id = ch.get("conv_id", "")
    cmd = [exe]
    if conv_id and not is_auto_conv_id(ch):
        cmd += ["--resume", conv_id]
    else:
        cmd += ["--continue"]
    cmd += ["--permission-mode", ch.get("permission_mode") or DEFAULT_CLAUDE_PERMISSION_MODE, "-p"]
    res = _run_with_timeout(cmd, workspace, timeout, env=clean_child_env(ch), input_text=prompt)
    out = (res.stdout or "").strip()
    if res.returncode != 0:
        return False, (res.stderr or out or f"claude 退出码 {res.returncode}").strip()[-1500:]
    return True, out or "执行完成（无文字输出）"


def run_codex(ch: dict, prompt: str, timeout: int) -> tuple[bool, str]:
    exe = resolve_host_bin(ch, "codex")
    if not exe:
        return False, "后台进程找不到 codex 命令，请在装有 codex 的终端里重新运行 bind"
    workspace = ch.get("workspace", str(HOME))
    conv_id = ch.get("conv_id", "")
    env = clean_child_env(ch)
    fd, last_msg_path = tempfile.mkstemp(prefix="ntfy_codex_", suffix=".txt")
    os.close(fd)
    base = [
        exe, "exec", "--cd", workspace, "--skip-git-repo-check",
        "--sandbox", ch.get("codex_sandbox") or DEFAULT_CODEX_SANDBOX,
        "--output-last-message", last_msg_path,
    ]
    try:
        res = None
        if conv_id and not is_auto_conv_id(ch):
            res = _run_with_timeout(base + ["resume", conv_id, "-"], workspace, timeout, env=env, input_text=prompt)
            err = (res.stderr or "").lower()
            # 只有"会话不存在"或参数不被接受时才改开新会话；其他失败直接上报，避免同一任务被执行两遍
            if res.returncode != 0 and (res.returncode == 2 or "no rollout found" in err or "not found" in err):
                log(f"[WARN] Codex 会话 {conv_id} 无法续接，改为新会话执行")
                res = None
        if res is None:
            res = _run_with_timeout(base + ["-"], workspace, timeout, env=env, input_text=prompt)
        try:
            last_msg = Path(last_msg_path).read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            last_msg = ""
        out = last_msg or (res.stdout or "").strip()[-3000:]
        if res.returncode != 0:
            return False, (res.stderr or out or f"codex 退出码 {res.returncode}").strip()[-1500:]
        return True, out or "执行完成（无文字输出）"
    finally:
        with contextlib.suppress(Exception):
            os.unlink(last_msg_path)


def run_antigravity(ch: dict, prompt: str, clean_cmd: str) -> tuple[bool, str]:
    if not AGENTAPI_BIN.is_file():
        return False, f"未找到 Antigravity agentapi：{AGENTAPI_BIN}"
    env = get_agy_env()
    conv_id = ch.get("conv_id", "")
    title = ch.get("title") or ch.get("alias") or "远程对话"
    if conv_id and not is_auto_conv_id(ch):
        res = _run_with_timeout(
            [str(AGENTAPI_BIN), "send-message", "--title", "手机远程指令", conv_id, prompt], None, 120, env
        )
        if res.returncode == 0:
            return True, ""
        log(f"[WARN] 会话 {conv_id} 投递失败 ({res.stderr.strip()})，尝试拉起新会话")
    res2 = _run_with_timeout(
        [str(AGENTAPI_BIN), "new-conversation", "--title", f"{title}: {clean_cmd[:18]}", "--", prompt], None, 120, env
    )
    if res2.returncode == 0:
        return True, ""
    return False, (res2.stderr or res2.stdout or "agentapi 投递失败").strip()[-800:]


def push_reply_files(ch: dict, text: str) -> tuple[str, list[str]]:
    """解析回复中的 `NTFY_FILE:` 行并推送文件；仅允许工作目录内、非敏感文件。返回 (去掉标记的正文, 问题列表)"""
    workspace = Path(ch.get("workspace", str(HOME)))
    problems: list[str] = []
    files: list[Path] = []
    for m in NTFY_FILE_RE.finditer(text):
        p = Path(m.group(1).strip().strip("`\"'")).expanduser()
        if not p.is_absolute():
            p = workspace / p
        if not is_within(p, workspace):
            problems.append(f"已拒绝工作目录外的文件：`{p}`")
        elif is_sensitive_file(p):
            problems.append(f"已拒绝敏感文件：`{p.name}`")
        elif not p.is_file():
            problems.append(f"文件不存在：`{p}`")
        else:
            files.append(p)
    body = NTFY_FILE_RE.sub("", text).strip()
    title = ch.get("title") or "远程对话"
    for idx, f in enumerate(files):
        ok = send_ntfy_file(
            server=ch.get("server", DEFAULT_SERVER),
            topic=ch["topic"],
            file_path=f,
            title=f"{title} | 产出文件 ({idx + 1}/{len(files)})",
            auth_token=ch.get("auth_token", ""),
            keep_emoji=bool(ch.get("keep_emoji")),
        )
        if not ok:
            problems.append(f"文件推送失败：`{f.name}`")
    return body, problems


def dispatch_command_to_host(
    channel_id: str,
    ch: dict,
    clean_cmd: str,
    attachment_path: Path | None = None,
) -> tuple[bool, str]:
    """将手机指令同步投递给绑定的宿主并（对 CLI 宿主）把结果推回手机。返回 (是否成功, 失败原因)"""
    host = ch.get("host", "antigravity")
    title = ch.get("title") or ch.get("alias") or "远程对话"
    timeout = int(ch.get("task_timeout") or DEFAULT_TASK_TIMEOUT)

    if host in ("claude", "codex"):
        prompt = build_prompt(channel_id, ch, clean_cmd, attachment_path, auto_reply=True)
        runner = run_claude if host == "claude" else run_codex
        try:
            ok, out = runner(ch, prompt, timeout)
        except subprocess.TimeoutExpired:
            return False, f"执行超过 {timeout} 秒已被终止"
        if not ok:
            return False, out
        body, problems = push_reply_files(ch, out)
        if problems:
            body += "\n\n" + "\n".join(f"- {p}" for p in problems)
        _ch_send(ch, f"{title} | {'Claude' if host == 'claude' else 'Codex'} 执行完成", body or "执行完成。")
        return True, ""

    if host == "antigravity":
        prompt = build_prompt(channel_id, ch, clean_cmd, attachment_path, auto_reply=False)
        try:
            return run_antigravity(ch, prompt, clean_cmd)
        except subprocess.TimeoutExpired:
            return False, "agentapi 投递超时"

    return False, (
        f"当前宿主 `{host}` 不支持从手机自动唤醒（目前支持 antigravity / claude / codex）。"
        f"可在电脑上用 `bind --host claude|codex|antigravity` 重新绑定。"
    )


def handle_meta_command(channel_id: str, ch: dict, message: str) -> bool:
    """处理手机端秒级系统元指令（不唤醒 AI）"""
    norm = re.sub(r"\s+", " ", message.strip().lower()).strip("。！!.,，")
    title_prefix = ch.get("title") or ch.get("alias") or "远程对话"

    def _log_meta() -> None:
        log(f"[META_CMD] [{channel_id}] 响应手机元指令: {norm}")

    if norm in ("/status", "status", "状态", "查看状态"):
        _log_meta()
        pwr = detect_power_and_sleep_status()
        reg = load_registry()
        caff_str = f"运行中 (PID {', '.join(pwr['sleep_guard_pids'])})" if pwr["sleep_guard_pids"] else "未开启"
        msg = (
            f"**频道状态**\n"
            f"- 绑定名称：{title_prefix}\n"
            f"- 运行状态：{'正常运行 (active)' if ch.get('status', 'active') == 'active' else '已暂停 (paused)'}\n"
            f"- 宿主引擎：{ch.get('host', 'antigravity')}\n"
            f"- 绑定会话：`{ch.get('conv_id', '')}`\n"
            f"- 工作目录：`{ch.get('workspace', '')}`\n\n"
            f"**电脑与防休眠**\n"
            f"- 电源状态：{pwr['power_source']} ({pwr['battery_percent']})\n"
            f"- 防休眠模式：{reg.get('sleep_guard', 'auto')}，锁：{caff_str}\n"
            f"- 最近指令：{ch.get('last_cmd_at') or '暂无'}"
        )
        _ch_send(ch, f"{title_prefix} | 系统状态", msg)
        return True

    if norm in ("/help", "help", "帮助", "指令"):
        _log_meta()
        msg = (
            f"**直接发送文字或图片/文件**\n"
            f"- 直接打字发送指令，或点回形针上传图片/文档，投递至绑定对话（`{ch.get('conv_id', '')[:8]}...`）。\n"
            f"- 同一频道的指令按顺序逐条执行。\n\n"
            f"**内置秒级元指令（不唤醒 AI，不耗 Token）**\n"
            f"- `状态` / `/status`：电脑电量、防休眠与绑定状态\n"
            f"- `暂停` / `/pause`、`恢复` / `/resume`：暂停/恢复本频道\n"
            f"- `/sleep on|off|auto`：强制开启 / 关闭 / 自动防休眠\n"
            f"- `销毁频道` / `/delete`：从电脑总控台解绑并停用本频道"
        )
        _ch_send(ch, f"{title_prefix} | 指令帮助", msg)
        return True

    if norm in ("/pause", "pause", "暂停", "暂停监听"):
        _log_meta()
        update_channel(channel_id, status="paused")
        _ch_send(ch, f"{title_prefix} | 已暂停", "本频道已暂停转发业务指令。发送 `恢复` 或 `/resume` 可重新激活。")
        return True

    if norm in ("/resume", "resume", "恢复", "恢复监听"):
        _log_meta()
        update_channel(channel_id, status="active")
        _ch_send(ch, f"{title_prefix} | 已恢复", "本频道已恢复正常监听，现在可以直接发送指令。")
        return True

    sleep_map = {
        "/sleep on": "on", "sleep on": "on", "开启防休眠": "on", "防休眠开启": "on",
        "/sleep off": "off", "sleep off": "off", "关闭防休眠": "off", "防休眠关闭": "off",
        "/sleep auto": "auto", "sleep auto": "auto", "自动防休眠": "auto",
    }
    if norm in sleep_map:
        _log_meta()
        mode = sleep_map[norm]
        with registry_txn() as reg:
            reg["sleep_guard"] = mode
        desc = {"on": "强制开启", "off": "已关闭（电脑可正常休眠）", "auto": "自动（有活跃频道且电量充足时开启）"}[mode]
        _ch_send(ch, f"{title_prefix} | 防休眠：{mode}", f"全局防休眠模式已设为 {desc}。")
        return True

    if norm in ("/delete", "/rm", "销毁频道", "删除频道"):
        _log_meta()
        with registry_txn() as reg:
            reg.get("channels", {}).pop(channel_id, None)
        _ch_send(
            ch,
            f"{title_prefix} | 频道已销毁",
            "本频道已从电脑总控台解绑移除，后台不再监听。现在可以在手机 ntfy App 中长按删除此订阅。",
        )
        return True

    return False


class ChannelWorker(threading.Thread):
    """单个频道的常驻流式监听线程 + 串行执行队列"""

    def __init__(self, channel_id: str, ch_config: dict):
        super().__init__(daemon=True)
        self.channel_id = channel_id
        self.ch_config = ch_config
        self.stop_event = threading.Event()
        self.seen_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.tasks: queue.Queue = queue.Queue()
        self.busy = False
        self.stale_notices = 0
        self.executor = threading.Thread(target=self._execute_loop, daemon=True)

    def stop(self) -> None:
        self.stop_event.set()

    def is_idle(self) -> bool:
        return not self.busy and self.tasks.empty()

    def _remember(self, msg_id: str) -> bool:
        if msg_id in self.seen_ids:
            return False
        self.seen_ids[msg_id] = None
        while len(self.seen_ids) > 500:
            self.seen_ids.popitem(last=False)
        return True

    def _execute_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message, att_path = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            self.busy = True
            try:
                ch = load_registry().get("channels", {}).get(self.channel_id)
                if not ch or ch.get("status", "active") != "active":
                    continue
                title = ch.get("title") or ch.get("alias") or "远程对话"
                try:
                    ok, detail = dispatch_command_to_host(self.channel_id, ch, message, attachment_path=att_path)
                except Exception as e:
                    ok, detail = False, f"内部错误：{e!r}"
                if ok:
                    log(f"[CMD_DONE] [{self.channel_id}] {message[:60]}")
                else:
                    log(f"[CMD_FAIL] [{self.channel_id}] {detail}")
                    _ch_send(ch, f"{title} | 指令未能执行", f"**指令**\n`{message[:200]}`\n\n**原因**\n{detail}", priority=4)
            finally:
                self.busy = False

    def _handle_event(self, ev: dict) -> None:
        msg_id = ev.get("id", "")
        if not msg_id or not self._remember(msg_id):
            return
        # 先推进游标，避免重连时反复重放同一条消息
        ch = update_channel(self.channel_id, last_event_id=msg_id)
        if ch is None:
            self.stop_event.set()
            return
        self.ch_config = ch

        tags = ev.get("tags") or []
        title = ev.get("title") or ""
        message = (ev.get("message") or "").strip()
        attachment = ev.get("attachment")

        if "robot" in tags:  # 本脚本自己发出的通知
            return
        if title and not attachment:  # 带标题的视为外部自动通知，而非手机手动输入
            return
        if not message and not attachment:
            return

        ch_title = ch.get("title") or ch.get("alias") or "远程对话"
        ev_time = float(ev.get("time") or 0)
        if ev_time and time.time() - ev_time > STALE_SECONDS:
            log(f"[STALE] [{self.channel_id}] 丢弃陈旧指令: {message[:60]}")
            self.stale_notices += 1
            if self.stale_notices <= 3:
                sent_at = datetime.fromtimestamp(ev_time).strftime("%m-%d %H:%M")
                _ch_send(
                    ch,
                    f"{ch_title} | 指令已过期未执行",
                    f"这条指令发送于 {sent_at}（电脑可能休眠或离线），已超过 {STALE_SECONDS} 秒，"
                    f"为防止重放没有执行。如仍需执行请重新发送：\n\n`{message[:200]}`",
                )
            return

        if not attachment and handle_meta_command(self.channel_id, ch, message):
            return

        if ch.get("status", "active") == "paused":
            log(f"[PAUSED] 忽略已暂停频道 {self.channel_id} 的指令: {message[:60]}")
            return

        saved_att_path: Path | None = None
        if isinstance(attachment, dict) and attachment.get("url"):
            saved_att_path = download_incoming_attachment(
                attachment,
                workspace=ch.get("workspace", str(HOME)),
                server=ch.get("server", DEFAULT_SERVER),
                auth_token=ch.get("auth_token", ""),
            )
            if saved_att_path is None:
                _ch_send(ch, f"{ch_title} | 附件未接收", "附件下载失败或不是本 ntfy 服务器托管的附件，已忽略。")
                if not message or message == attachment.get("name"):
                    return
            elif not message or message == attachment.get("name") or message.startswith("attachment"):
                message = f"请查看并处理我从手机上传的文件：{attachment.get('name', '附件')}"

        log(f"[CMD_RECV] [{self.channel_id}] {message[:80]}")
        ch = update_channel(
            self.channel_id,
            last_cmd_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_cmd_text=message[:80],
        ) or ch

        pending = self.tasks.qsize() + (1 if self.busy else 0)
        if pending:
            _ch_send(ch, f"{ch_title} | 已排队", f"前面还有 {pending} 条指令在执行，本条已排队：`{message[:100]}`", priority=3)
        elif ch.get("send_ack"):
            _ch_send(ch, f"{ch_title} | 已收到指令", f"正在执行：`{message[:200]}`", priority=3)
        self.tasks.put((message, saved_att_path))

    def run(self) -> None:
        self.executor.start()
        server = self.ch_config.get("server", DEFAULT_SERVER).rstrip("/")
        topic = self.ch_config["topic"]
        log(f"[WORKER_START] 频道 {self.channel_id} -> 会话 {self.ch_config.get('conv_id')}")
        backoff = 3
        while not self.stop_event.is_set():
            try:
                curr_ch = load_registry().get("channels", {}).get(self.channel_id)
                if not curr_ch or curr_ch.get("topic") != topic:
                    break
                self.ch_config = curr_ch
                self.stale_notices = 0
                stream_url = f"{server}/{topic}/json"
                last_id = curr_ch.get("last_event_id", "")
                if last_id:
                    stream_url += f"?since={urllib.parse.quote(last_id)}"
                headers = {"User-Agent": USER_AGENT}
                headers.update(_auth_headers(curr_ch.get("auth_token", "")))
                req = urllib.request.Request(stream_url, headers=headers)
                with urllib.request.urlopen(req, timeout=90) as resp:
                    backoff = 3
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
                        if ev.get("event") == "message":
                            self._handle_event(ev)
            except Exception as e:
                if self.stop_event.is_set():
                    break
                log(f"[WORKER_WARN] [{self.channel_id}] 连接中断，{backoff}s 后重连: {e!r}")
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
        self.stop_event.set()
        log(f"[WORKER_STOP] 频道 {self.channel_id} 停止监听")


# ---------------------------------------------------------------------------
# 防休眠与守护进程
# ---------------------------------------------------------------------------


def start_sleep_inhibitor() -> subprocess.Popen | None:
    """按系统启动防休眠子进程（不加 -d，屏幕可以正常熄灭省电）"""
    if IS_MAC and Path("/usr/bin/caffeinate").is_file():
        return subprocess.Popen(["/usr/bin/caffeinate", "-ims"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if IS_LINUX and shutil.which("systemd-inhibit"):
        return subprocess.Popen(
            [
                "systemd-inhibit", "--what=idle:sleep", "--who=ntfy-remote-chat",
                "--why=Active ntfy remote chat channel", "sleep", "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if IS_WIN:
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    return None


def stop_sleep_inhibitor(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
    if IS_WIN:
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    with contextlib.suppress(Exception):
        INHIBITOR_PID_PATH.unlink()


def detect_other_ntfy_agents() -> list[dict]:
    """扫描其他独立运行的 ntfy LaunchAgent（仅用于 status 展示，不影响防休眠）"""
    if not IS_MAC or not LAUNCH_AGENTS_DIR.is_dir():
        return []
    return [
        {"service_label": p.stem, "plist_path": str(p)}
        for p in sorted(LAUNCH_AGENTS_DIR.glob("*ntfy*.plist"))
        if p.name not in (f"{PLIST_LABEL}.plist", f"{OLD_PLIST_LABEL}.plist")
    ]


def should_inhibit_sleep(mode: str, has_active: bool, on_battery: bool | None, pct: int | None, min_battery: int) -> bool:
    if mode == "on":
        return True
    if mode == "off" or not has_active:
        return False
    if on_battery and pct is not None and pct <= min_battery:
        return False  # auto 模式下电池低电量时释放，避免把电耗光
    return True


def is_daemon_running() -> bool:
    lock = FileLock(DAEMON_LOCK_PATH)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def run_daemon_loop() -> int:
    """统一多路复用守护进程：单实例锁 + 热加载注册表 + 托管防休眠 + 脚本更新后自动重启"""
    ensure_state_dir()
    daemon_lock = FileLock(DAEMON_LOCK_PATH)
    if not daemon_lock.acquire(blocking=False):
        log("[HUB_SKIP] 已有守护进程在运行，本实例退出")
        return 0
    PID_FILE_PATH.write_text(str(os.getpid()), encoding="utf-8")
    script_path = Path(os.path.abspath(__file__))
    script_mtime = script_path.stat().st_mtime
    log(f"[HUB_INIT] ntfy_hub v{VERSION} 守护进程已启动 (PID={os.getpid()})")
    workers: dict[str, tuple[str, ChannelWorker]] = {}
    inhibitor_proc: subprocess.Popen | None = None
    win_inhibiting = False
    inhibitor_started_at = 0.0
    inhibitor_retry_at = 0.0  # 防休眠子进程启动即退出（如无 logind）时，暂停重试，避免每 3 秒刷屏
    battery_cache: tuple[float, bool | None, int | None] = (0.0, None, None)

    def _shutdown() -> None:
        for _, (_, w) in workers.items():
            w.stop()
        stop_sleep_inhibitor(inhibitor_proc)
        daemon_lock.release()

    def _on_signal(*_args):
        _shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

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

            now = time.time()
            if now - battery_cache[0] > 60:
                battery_cache = (now, *get_battery_info())
            inhibit = should_inhibit_sleep(
                sleep_mode, bool(active_cids), battery_cache[1], battery_cache[2],
                int(reg.get("min_battery", DEFAULT_MIN_BATTERY)),
            )

            if inhibit:
                if IS_WIN:
                    if not win_inhibiting:
                        start_sleep_inhibitor()
                        win_inhibiting = True
                        INHIBITOR_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
                        log("[SLEEP_GUARD_ON] 已激活防休眠 (SetThreadExecutionState)")
                elif (inhibitor_proc is None or inhibitor_proc.poll() is not None) and now >= inhibitor_retry_at:
                    if inhibitor_proc is not None and now - inhibitor_started_at < 10:
                        inhibitor_retry_at = now + 300
                        inhibitor_proc = None
                        log("[SLEEP_GUARD_UNAVAILABLE] 防休眠进程启动后立即退出，本机可能不支持，5 分钟后重试")
                    else:
                        inhibitor_proc = start_sleep_inhibitor()
                        inhibitor_started_at = now
                        if inhibitor_proc:
                            INHIBITOR_PID_PATH.write_text(str(inhibitor_proc.pid), encoding="utf-8")
                            log(f"[SLEEP_GUARD_ON] 已激活防休眠锁 (PID={inhibitor_proc.pid})")
            else:
                if inhibitor_proc is not None or win_inhibiting:
                    log("[SLEEP_GUARD_OFF] 已释放防休眠锁")
                    stop_sleep_inhibitor(inhibitor_proc)
                    inhibitor_proc = None
                    win_inhibiting = False

            # Skill 更新后：等所有频道空闲再原地重启，加载新代码
            try:
                new_mtime = script_path.stat().st_mtime
            except OSError:
                new_mtime = script_mtime
            if new_mtime != script_mtime and all(w.is_idle() for _, w in workers.values()):
                log("[HUB_UPGRADE] 检测到脚本更新，重启守护进程以加载新版本")
                _shutdown()
                os.execv(sys.executable, [sys.executable, str(script_path), "daemon"])
        except Exception as e:
            log(f"[HUB_WARN] 守护循环异常: {e!r}")

        time.sleep(3)


def _daemon_python() -> str:
    """Windows 优先用 pythonw.exe，避免弹出控制台窗口"""
    if IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    return sys.executable


def _spawn_background_daemon(script_abs: str) -> int:
    log_f = open(LOG_PATH, "a", encoding="utf-8")
    kwargs: dict = {"stdout": log_f, "stderr": log_f, "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([_daemon_python(), script_abs, "daemon"], env=clean_child_env(), **kwargs)
    return proc.pid


def _systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def ensure_background_daemon() -> dict:
    """跨平台确保后台常驻守护服务正在运行，并配置开机/登录自启"""
    ensure_state_dir()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    script_abs = os.path.abspath(__file__)
    info: dict = {"log_path": str(LOG_PATH)}

    if IS_MAC:
        LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        if OLD_PLIST_PATH.is_file():
            subprocess.run(["launchctl", "unload", str(OLD_PLIST_PATH)], capture_output=True)
            with contextlib.suppress(Exception):
                OLD_PLIST_PATH.unlink()
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
\t<dict>
\t\t<key>SuccessfulExit</key>
\t\t<false/>
\t</dict>
\t<key>StandardOutPath</key>
\t<string>{LOG_PATH}</string>
\t<key>StandardErrorPath</key>
\t<string>{LOG_PATH}</string>
\t<key>WorkingDirectory</key>
\t<string>{STATE_DIR}</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>HOME</key>
\t\t<string>{HOME}</string>
\t\t<key>PYTHONIOENCODING</key>
\t\t<string>utf-8</string>
\t\t<key>PATH</key>
\t\t<string>{HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
\t</dict>
\t<key>ThrottleInterval</key>
\t<integer>5</integer>
</dict>
</plist>
"""
        changed = not PLIST_PATH.is_file() or PLIST_PATH.read_text(encoding="utf-8") != plist_content
        if changed:
            PLIST_PATH.write_text(plist_content, encoding="utf-8")
        loaded = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{PLIST_LABEL}"], capture_output=True, text=True
        ).returncode == 0
        if changed or not loaded or not is_daemon_running():
            subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
            subprocess.run(["launchctl", "load", str(PLIST_PATH)], capture_output=True)
            time.sleep(1.0)
        info.update({"manager": "launchd", "service_config": str(PLIST_PATH)})

    elif IS_LINUX and _systemd_user_available():
        unit = (
            "[Unit]\nDescription=ntfy remote chat hub\nAfter=network-online.target\n\n"
            "[Service]\n"
            f"Environment=\"HOME={HOME}\" \"PYTHONIOENCODING=utf-8\"\n"
            f"ExecStart=\"{sys.executable}\" \"{script_abs}\" daemon\n"
            "Restart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        SYSTEMD_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        changed = not SYSTEMD_UNIT_PATH.is_file() or SYSTEMD_UNIT_PATH.read_text(encoding="utf-8") != unit
        if changed:
            SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME], capture_output=True)
        if changed:
            subprocess.run(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME], capture_output=True)
        elif not is_daemon_running():
            subprocess.run(["systemctl", "--user", "start", SYSTEMD_UNIT_NAME], capture_output=True)
        time.sleep(1.0)
        info.update({
            "manager": "systemd-user",
            "service_config": str(SYSTEMD_UNIT_PATH),
            "note": "如需注销登录后仍保持运行，请执行：loginctl enable-linger $USER",
        })

    else:
        if IS_WIN:
            with contextlib.suppress(Exception):
                WIN_STARTUP_PATH.parent.mkdir(parents=True, exist_ok=True)
                WIN_STARTUP_PATH.write_text(
                    f'@echo off\r\nchcp 65001 >nul\r\nstart "" "{_daemon_python()}" "{script_abs}" daemon\r\n',
                    encoding="utf-8",
                )
                info["service_config"] = str(WIN_STARTUP_PATH)
        if not is_daemon_running():
            _spawn_background_daemon(script_abs)
            time.sleep(1.0)
        info["manager"] = "background-process"

    if info.get("manager") in ("launchd", "systemd-user"):
        for _ in range(20):
            if is_daemon_running():
                break
            time.sleep(0.25)
        if not is_daemon_running():
            _spawn_background_daemon(script_abs)
            time.sleep(1.0)
            info["note"] = f"{info['manager']} 未能拉起守护进程，已改为普通后台进程运行（重启电脑后需重新 bind）"
            info["manager"] = "background-process"

    pid = read_pid(PID_FILE_PATH)
    info["hub_pid"] = str(pid) if pid and is_pid_alive(pid) else None
    info["running"] = is_daemon_running()
    return info


def stop_background_daemon() -> dict:
    """停止守护进程并移除自启配置"""
    removed = []
    if IS_MAC:
        for p in (PLIST_PATH, OLD_PLIST_PATH):
            if p.is_file():
                subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
                p.unlink()
                removed.append(str(p))
    if IS_LINUX and SYSTEMD_UNIT_PATH.is_file():
        subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME], capture_output=True)
        SYSTEMD_UNIT_PATH.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        removed.append(str(SYSTEMD_UNIT_PATH))
    if IS_WIN and WIN_STARTUP_PATH.is_file():
        WIN_STARTUP_PATH.unlink()
        removed.append(str(WIN_STARTUP_PATH))
    pid = read_pid(PID_FILE_PATH)
    if pid and is_pid_alive(pid) and is_daemon_running():
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not is_daemon_running():
                break
            time.sleep(0.25)
    inh = read_pid(INHIBITOR_PID_PATH)
    if inh and is_pid_alive(inh) and not IS_WIN:
        with contextlib.suppress(Exception):
            os.kill(inh, signal.SIGTERM)
    return {"removed_service_files": removed, "daemon_running": is_daemon_running()}


# ---------------------------------------------------------------------------
# CLI 子命令
# ---------------------------------------------------------------------------


def _public_channel(ch: dict) -> dict:
    out = dict(ch)
    if out.get("auth_token"):
        out["auth_token"] = mask_secret(out["auth_token"])
    return out


def cmd_bind(args: argparse.Namespace) -> int:
    workspace = os.path.abspath(args.workspace or os.getcwd())
    folder_name = os.path.basename(workspace.rstrip("/\\")) or "workspace"
    alias = args.alias or slugify_alias(folder_name)
    title = args.title or folder_name
    host = detect_host_ide(args.host)
    conv_id = (args.conv_id or detect_conv_id(host)).strip()
    conv_id_auto = not conv_id
    warnings: list[str] = []
    if conv_id_auto:
        conv_id = f"{host}-{alias}"
        warnings.append(
            "未能获取当前会话 ID：手机指令将投递到该目录下最近的会话（claude --continue / codex 新会话），"
            "同一项目开多个对话框会共用一个频道。建议显式传入 --conv-id。"
        )
    if host == "gui":
        warnings.append("未识别到可自动唤醒的宿主（antigravity / claude / codex），手机指令将无法执行，只能用 notify 推送结果。")

    reg = load_registry()
    existing_cid, existing_ch = find_channel(reg, conv_id)
    existing_ch = existing_ch or {}
    channel_id = existing_cid or f"ch_{slugify_alias(alias)}_{secrets.token_hex(3)}"

    if existing_ch.get("topic") and not args.rotate and not args.topic:
        topic = existing_ch["topic"]
    elif args.topic:
        topic = args.topic.strip()
    else:
        topic = generate_topic()

    server = (args.server or existing_ch.get("server") or DEFAULT_SERVER).rstrip("/")
    web_url = f"{server}/{topic}"
    app_url = f"ntfy://{server.replace('https://', '').replace('http://', '')}/{topic}"
    qr_dir = Path(args.qr_dir).expanduser().resolve() if args.qr_dir else STATE_DIR / "qr"
    qr_path = qr_dir / f"ntfy_qr_{channel_id}.png"

    def pick(arg_val, key, default):
        return arg_val if arg_val is not None else existing_ch.get(key, default)

    entry = {
        "channel_id": channel_id,
        "alias": alias,
        "title": title,
        "topic": topic,
        "server": server,
        "web_url": web_url,
        "app_url": app_url,
        "conv_id": conv_id,
        "conv_id_auto": conv_id_auto,
        "workspace": workspace,
        "host": host,
        "auth_token": args.token or existing_ch.get("auth_token", ""),
        "send_ack": bool(pick(args.ack, "send_ack", False)),
        "keep_emoji": bool(pick(args.keep_emoji, "keep_emoji", False)),
        "permission_mode": pick(args.permission_mode, "permission_mode", DEFAULT_CLAUDE_PERMISSION_MODE),
        "codex_sandbox": pick(args.codex_sandbox, "codex_sandbox", DEFAULT_CODEX_SANDBOX),
        "task_timeout": int(pick(args.task_timeout, "task_timeout", DEFAULT_TASK_TIMEOUT)),
        "host_bin": shutil.which(host) or "" if host in ("claude", "codex") else "",
        "env_path": os.environ.get("PATH", ""),
        "status": "active",
        "created_at": existing_ch.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_event_id": existing_ch.get("last_event_id", "") if topic == existing_ch.get("topic") else "",
        "qr_path": str(qr_path),
    }

    if not entry["auth_token"]:
        warnings.append(
            "安全提示：当前频道没有访问控制，任何拿到 Topic 的人都能读取历史消息并向电脑发指令。"
            "建议使用自建 ntfy 或 ntfy Pro 的受保护 Topic，并通过 --token 传入访问令牌；"
            "不要把 Topic 截图或贴到公开场合。"
        )
    if host == "claude" and entry["permission_mode"] == "bypassPermissions":
        warnings.append("Claude 权限模式为 bypassPermissions：手机指令可执行任意命令，请确保频道已做访问控制。")
    if host == "codex" and entry["codex_sandbox"] == "danger-full-access":
        warnings.append("Codex 沙箱为 danger-full-access：手机指令可执行任意命令，请确保频道已做访问控制。")

    if args.dry_run:
        print(json.dumps({"dry_run": True, "channel": _public_channel(entry), "warnings": warnings,
                          "power": detect_power_and_sleep_status()}, ensure_ascii=False, indent=2))
        return 0

    with registry_txn() as reg_w:
        reg_w.setdefault("channels", {})[channel_id] = entry

    qr_ok, qr_mode = generate_qr_code(web_url, qr_path, allow_online=args.online_qr)
    daemon_info = ensure_background_daemon()
    power_info = detect_power_and_sleep_status()

    test_sent = False
    if not args.no_test:
        welcome_msg = (
            f"**绑定状态**\n已一对一绑定至当前对话，与其他频道隔离。\n\n"
            f"**绑定详情**\n"
            f"- 频道名称：{title}\n"
            f"- 宿主环境：{host}\n"
            f"- 会话标识：`{conv_id}`\n"
            f"- 工作目录：`{workspace}`\n\n"
            f"**使用方法**\n"
            f"- 直接打字或点回形针发图片/文件即可唤醒电脑执行（按顺序逐条执行）；\n"
            f"- 电脑产出的图片、PDF、视频等会直接发回本频道；\n"
            f"- 发送 `状态` 查看电脑状态，`帮助` 查看全部指令，`销毁频道` 立即解绑。\n\n"
            f"**安全警示**\n"
            f"请勿向任何人分享、转发或截图暴露本频道的二维码、Topic 与订阅链接（持有即可读取消息并远程控制当前对话）。"
        )
        test_sent = send_ntfy_message(server, topic, f"{title} | 远程对话已就绪", welcome_msg,
                                      auth_token=entry["auth_token"], keep_emoji=entry["keep_emoji"])

    print(json.dumps({
        "ok": True,
        "channel": _public_channel(entry),
        "warnings": warnings,
        "security_warning": (
            "严禁分享二维码、Topic 或订阅链接！它等同于当前 AI 对话框的免密远程控制密钥，"
            "泄露后他人可查看通知并向电脑发送指令。若不慎暴露，请在手机发送「销毁频道」或运行 rm 作废，"
            "并用 bind --rotate 换新 Topic。"
        ),
        "qr_generated": qr_ok,
        "qr_mode": qr_mode,
        "test_notification_sent": test_sent,
        "daemon": daemon_info,
        "power_and_sleep": power_info,
    }, ensure_ascii=False, indent=2))
    return 0


def _read_msg(args: argparse.Namespace) -> str:
    if args.msg_file:
        if args.msg_file == "-":
            return sys.stdin.read()
        return Path(args.msg_file).expanduser().read_text(encoding="utf-8")
    return decode_cli_text(args.msg or "")


def cmd_notify(args: argparse.Namespace) -> int:
    reg = load_registry()
    _, ch = find_channel(reg, args.channel)
    if not ch:
        if not args.raw_topic:
            known = [f"{cid} ({c.get('title', '')})" for cid, c in reg.get("channels", {}).items()]
            print(
                f"ERROR: 未找到匹配的频道: {args.channel}\n"
                f"已绑定频道: {', '.join(known) or '无'}\n"
                f"如确实要发到一个未绑定的 Topic，请加 --raw-topic。",
                file=sys.stderr,
            )
            return 1
        ch = {
            "server": (args.server or DEFAULT_SERVER).rstrip("/"),
            "topic": args.channel.rstrip("/").split("/")[-1],
            "title": "远程对话",
            "auth_token": args.token or "",
            "keep_emoji": args.keep_emoji,
        }

    keep_emoji = args.keep_emoji or bool(ch.get("keep_emoji"))
    server = ch.get("server", DEFAULT_SERVER)
    topic = ch["topic"]
    token = ch.get("auth_token", "")
    title = args.title or f"{ch.get('title', '远程对话')} | 任务完成"
    msg = _read_msg(args)

    files = [Path(f).expanduser().resolve() for f in (args.file or [])]
    blocked = [f for f in files if is_sensitive_file(f)]
    if blocked and not args.force:
        print(f"ERROR: 拒绝推送疑似密钥/凭证文件: {', '.join(f.name for f in blocked)}（确需推送请加 --force）", file=sys.stderr)
        return 1

    if not files:
        ok = send_ntfy_message(server, topic, title, msg or "任务执行完毕。", priority=args.priority,
                               auth_token=token, keep_emoji=keep_emoji, click_url=args.click_url)
        if ok:
            print(f"[OK] 手机文字通知已送达 (Topic: {topic})")
            return 0
        return 1

    all_ok = True
    for idx, fpath in enumerate(files):
        item_title = title if len(files) == 1 else f"{title} ({idx + 1}/{len(files)})"
        item_msg = msg if idx == 0 else f"附件 ({idx + 1}/{len(files)}): `{fpath.name}`"
        ok = send_ntfy_file(server, topic, fpath, title=item_title, message=item_msg, priority=args.priority,
                            auth_token=token, keep_emoji=keep_emoji)
        if ok:
            print(f"[OK] 手机附件/图片已送达: {fpath.name} (Topic: {topic})")
        else:
            all_ok = False
    return 0 if all_ok else 1


def cmd_status(_args: argparse.Namespace) -> int:
    reg = load_registry()
    print(json.dumps({
        "version": VERSION,
        "sleep_guard_mode": reg.get("sleep_guard", "auto"),
        "min_battery": reg.get("min_battery", DEFAULT_MIN_BATTERY),
        "daemon_running": is_daemon_running(),
        "power_and_sleep": detect_power_and_sleep_status(),
        "hub_channels": [
            {"index": i, **_public_channel(c)} for i, c in enumerate(reg.get("channels", {}).values(), start=1)
        ],
        "other_ntfy_services": detect_other_ntfy_agents(),
        "log_path": str(LOG_PATH),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_panel(_args: argparse.Namespace) -> int:
    """输出 Markdown 表格版全局总控面板（带 #1, #2 序号）"""
    reg = load_registry()
    power = detect_power_and_sleep_status()
    channels = list(reg.get("channels", {}).values())
    pids = power.get("sleep_guard_pids") or []
    caff_str = f"运行中 (PID: {', '.join(pids)})" if pids else "未开启"
    daemon_str = f"运行中 (PID {power['daemon_pid']})" if power.get("daemon_pid") else ("运行中" if is_daemon_running() else "未运行")
    lines = [
        "### ntfy 全局远程对话总控面板",
        "",
        f"- **电脑电源**：{power.get('power_source', 'Unknown')} ({power.get('battery_percent', 'Unknown')})",
        f"- **防休眠锁**：{caff_str}（策略：`{reg.get('sleep_guard', 'auto')}`，电池低于 {reg.get('min_battery', DEFAULT_MIN_BATTERY)}% 自动释放）",
        f"- **守护进程**：{daemon_str} | 已绑定频道数：**{len(channels)}**",
        "",
    ]
    if not channels:
        lines.append("> 当前暂无绑定的远程对话频道。在任意对话框输入 `/ntfy` 即可一键生成并绑定。")
    else:
        lines += [
            "| 序号 | 频道名称 | 状态 | 宿主 | 专属 Topic | 绑定会话 ID | 工作目录 | 最近指令 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for idx, ch in enumerate(channels, start=1):
            status_str = "运行中" if ch.get("status", "active") == "active" else "已暂停"
            title = ch.get("title") or ch.get("alias") or ch.get("channel_id")
            conv_id = ch.get("conv_id") or "-"
            if is_auto_conv_id(ch):
                conv_id += "（自动）"
            last_cmd = (ch.get("last_cmd_text") or "-").replace("|", "\\|").replace("\n", " ")
            if len(last_cmd) > 24:
                last_cmd = last_cmd[:24] + "..."
            lines.append(
                f"| **`#{idx}`** | **{title}** | `{status_str}` | {ch.get('host', '-')} | `{ch.get('topic', '')}` "
                f"| `{conv_id}` | `{ch.get('workspace') or '-'}` | {last_cmd} |"
            )
        lines += [
            "",
            "**快捷管理指令：**",
            "- 删除指定频道：`/ntfy rm 1`（或 `ntfy_ctl.py rm 1`）",
            "- 暂停/恢复频道：`/ntfy pause 1` / `/ntfy resume 1`（或在手机端直接发 `暂停` / `恢复`）",
            "- 手机端自毁当前频道：在手机 ntfy 频道内发送 `销毁频道` 或 `/delete`",
            "- 清空全部频道：`/ntfy clear`；彻底移除后台服务：`ntfy_ctl.py uninstall`",
            "",
            "> **安全警示**：上表中的「专属 Topic」及对应二维码/订阅链接等同于对应 AI 对话框的**免密远程控制密钥**，"
            "切勿分享或在截图中暴露。若不慎泄露，请立即执行 `/ntfy rm <序号>` 或在手机发送 `销毁频道` 作废。",
        ]
    print("\n".join(lines))
    return 0


def _notify_destroyed(ch: dict, cid: str) -> None:
    with contextlib.suppress(Exception):
        send_ntfy_message(
            server=ch.get("server", DEFAULT_SERVER),
            topic=ch["topic"],
            title=f"{ch.get('title', '远程对话')} | 频道已销毁",
            message=(
                f"该对话的远程监听已从总控中心移除，后续消息不再触发电脑端任务。\n"
                f"- **移除频道**：`{ch.get('title', cid)}`\n"
                f"- **原会话 ID**：`{ch.get('conv_id') or '未指定'}`"
            ),
            priority=3,
            auth_token=ch.get("auth_token", ""),
            keep_emoji=bool(ch.get("keep_emoji")),
        )


def _remove_qr(ch: dict) -> None:
    with contextlib.suppress(Exception):
        if ch.get("qr_path"):
            Path(ch["qr_path"]).unlink()


def cmd_unbind(args: argparse.Namespace) -> int:
    quiet = bool(getattr(args, "quiet", False))
    if getattr(args, "all", False):
        with registry_txn() as reg:
            removed_all = list(reg.get("channels", {}).items())
            reg["channels"] = {}
        for cid, ch in removed_all:
            _remove_qr(ch)
            if not quiet:
                _notify_destroyed(ch, cid)
        print(json.dumps({
            "ok": True,
            "cleared_count": len(removed_all),
            "note": "已无活跃频道，防休眠锁会在数秒内自动释放；如需彻底移除后台服务，运行 uninstall。",
        }, ensure_ascii=False, indent=2))
        return 0

    query = getattr(args, "target", "") or getattr(args, "channel", "")
    if not query:
        print("ERROR: 请提供要删除的频道序号（如 1）、名称、会话 ID 或 Topic，或使用 --all 清空全部。", file=sys.stderr)
        return 1
    with registry_txn() as reg:
        cid, ch = find_channel(reg, query)
        if not cid or not ch:
            print(f"ERROR: 未找到频道: {query}", file=sys.stderr)
            return 1
        removed = reg["channels"].pop(cid)
        remaining = len(reg["channels"])
    _remove_qr(removed)
    if not quiet:
        _notify_destroyed(removed, cid)
    out = {"ok": True, "unbound": _public_channel(removed), "remaining_channels": remaining}
    if remaining == 0:
        out["note"] = "已无活跃频道，防休眠锁会在数秒内自动释放；如需彻底移除后台服务，运行 uninstall。"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_pause_resume(args: argparse.Namespace, enable: bool) -> int:
    query = getattr(args, "target", "") or getattr(args, "channel", "")
    new_status = "active" if enable else "paused"
    with registry_txn() as reg:
        cid, ch = find_channel(reg, query)
        if not cid or not ch:
            print(f"ERROR: 未找到频道: {query}", file=sys.stderr)
            return 1
        ch["status"] = new_status
    print(json.dumps({
        "ok": True, "channel_id": cid, "title": ch.get("title"), "status": new_status,
        "label": "已恢复运行" if enable else "已暂停监听",
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_sleep(args: argparse.Namespace) -> int:
    if args.mode in ("on", "off", "auto") or args.min_battery is not None:
        with registry_txn() as reg:
            if args.mode in ("on", "off", "auto"):
                reg["sleep_guard"] = args.mode
            if args.min_battery is not None:
                reg["min_battery"] = max(0, min(100, args.min_battery))
        ensure_background_daemon()
        time.sleep(3.5)  # 等守护进程下一轮循环生效
    reg = load_registry()
    print(json.dumps({
        "sleep_guard_mode": reg.get("sleep_guard", "auto"),
        "min_battery": reg.get("min_battery", DEFAULT_MIN_BATTERY),
        "power": detect_power_and_sleep_status(),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    out = stop_background_daemon()
    if args.purge:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        out["purged_state_dir"] = str(STATE_DIR)
    print(json.dumps({"ok": True, **out}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跨平台、跨 AI IDE 手机 ntfy 双向远程对话控制中心")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_bind = sub.add_parser("bind", help="为当前对话框生成专属 ntfy 频道并开启防休眠与常驻监听")
    p_bind.add_argument("--conv-id", type=str, default="", help="锁死的对话框/会话 ID（默认从宿主环境变量读取）")
    p_bind.add_argument("--workspace", type=str, default="", help="绑定的工作目录路径")
    p_bind.add_argument("--alias", type=str, default="", help="频道英文短别名")
    p_bind.add_argument("--title", type=str, default="", help="手机通知显示的频道标题")
    p_bind.add_argument("--host", type=str, default="auto", choices=["auto", "antigravity", "codex", "claude", "gui"])
    p_bind.add_argument("--topic", type=str, default="", help="自定义 Topic（默认自动生成高熵随机串）")
    p_bind.add_argument("--rotate", action="store_true", help="强制轮换生成全新的随机 Topic")
    p_bind.add_argument("--token", type=str, default="", help="ntfy 访问令牌 (tk_...)，配合受保护 Topic 使用")
    p_bind.add_argument("--server", type=str, default="", help="ntfy 服务器地址（默认 https://ntfy.sh）")
    p_bind.add_argument("--qr-dir", type=str, default="", help="二维码 PNG 保存目录")
    p_bind.add_argument("--online-qr", action="store_true", help="允许请求外部二维码 API（默认仅离线生成）")
    p_bind.add_argument("--ack", action="store_true", default=None, help="收到手机指令时发送确认回执")
    p_bind.add_argument("--keep-emoji", action="store_true", default=None, help="保留通知中的 Emoji")
    p_bind.add_argument(
        "--permission-mode", type=str, default=None,
        choices=["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"],
        help=f"Claude Code 执行手机指令时的权限模式（默认 {DEFAULT_CLAUDE_PERMISSION_MODE}：可改文件，不能执行命令）",
    )
    p_bind.add_argument(
        "--codex-sandbox", type=str, default=None, choices=["read-only", "workspace-write", "danger-full-access"],
        help=f"Codex 执行手机指令时的沙箱（默认 {DEFAULT_CODEX_SANDBOX}）",
    )
    p_bind.add_argument("--task-timeout", type=int, default=None, help=f"单条指令最长执行秒数（默认 {DEFAULT_TASK_TIMEOUT}）")
    p_bind.add_argument("--no-test", action="store_true", help="绑定时不发送初始测试通知")
    p_bind.add_argument("--dry-run", action="store_true", help="仅预览配置，不写入也不启动守护进程")

    p_notify = sub.add_parser("notify", help="向绑定的手机 ntfy 频道发送文字、图片或文件")
    p_notify.add_argument("--channel", type=str, required=True, help="频道序号(#1)、频道 ID、会话 ID、别名或 Topic")
    p_notify.add_argument("--title", type=str, default="", help="通知标题")
    p_notify.add_argument("--msg", type=str, default="", help="通知正文 (Markdown)；字面 \\n 会被转成换行")
    p_notify.add_argument("--msg-file", type=str, default="", help="从文件读取正文，传 - 表示从标准输入读取")
    p_notify.add_argument("--file", action="append", default=[], help="要发送的图片或文件路径（可多次传入）")
    p_notify.add_argument("--click-url", type=str, default="", help="点击通知卡片跳转的外部链接")
    p_notify.add_argument("--priority", type=int, default=4, choices=[1, 2, 3, 4, 5], help="通知优先级 (1-5)")
    p_notify.add_argument("--keep-emoji", action="store_true", help="保留正文中的 Emoji")
    p_notify.add_argument("--raw-topic", action="store_true", help="--channel 未匹配已绑定频道时，直接当作 Topic 发送")
    p_notify.add_argument("--server", type=str, default="", help="配合 --raw-topic 使用的服务器地址")
    p_notify.add_argument("--token", type=str, default="", help="配合 --raw-topic 使用的访问令牌")
    p_notify.add_argument("--force", action="store_true", help="允许推送疑似密钥/凭证文件")

    sub.add_parser("status", help="输出 JSON 格式的防休眠、电源与所有频道状态")
    sub.add_parser("list", help="输出 JSON 格式的所有已绑定频道状态")
    sub.add_parser("panel", help="输出 Markdown 表格版全局总控面板（含 #1, #2 序号与管理指令）")

    for cmd_name in ("unbind", "rm", "delete"):
        p_rm = sub.add_parser(cmd_name, help="解绑并删除指定频道（支持序号如 1、名称、conv_id 或 Topic）")
        p_rm.add_argument("target", nargs="?", default="", help="频道序号(如 1 或 #1)、名称、会话 ID 或 Topic")
        p_rm.add_argument("--channel", type=str, default="", help="频道标识（兼容旧参数）")
        p_rm.add_argument("--all", action="store_true", help="清空所有已绑定的频道")
        p_rm.add_argument("--quiet", action="store_true", help="静默解绑，不向手机发送销毁通知")

    p_clear = sub.add_parser("clear", help="一键清空所有已绑定的频道")
    p_clear.add_argument("--quiet", action="store_true", help="静默清空")

    for cmd_name, verb in (("pause", "暂停"), ("resume", "恢复")):
        p_pr = sub.add_parser(cmd_name, help=f"{verb}指定频道的指令监听（支持序号 1、名称或 Topic）")
        p_pr.add_argument("target", nargs="?", default="", help="频道序号(1)、名称、会话 ID 或 Topic")
        p_pr.add_argument("--channel", type=str, default="", help="频道标识")

    p_sleep = sub.add_parser("sleep", help="控制电脑防休眠模式 (auto/on/off)")
    p_sleep.add_argument("--mode", type=str, default="status", choices=["auto", "on", "off", "status"])
    p_sleep.add_argument("--min-battery", type=int, default=None, help="auto 模式下电池电量低于该百分比时释放防休眠")

    p_uninstall = sub.add_parser("uninstall", help="停止守护进程并移除开机自启配置")
    p_uninstall.add_argument("--purge", action="store_true", help="同时删除所有频道注册表与日志等本地状态")

    sub.add_parser("daemon", help="启动后台多路复用常驻守护循环")
    return parser


def _force_utf8_stdio() -> None:
    """输出统一 UTF-8：英文 Windows 上管道输出默认 cp1252，打印中文会直接报错"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            if stream and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
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
    if args.subcommand == "uninstall":
        return cmd_uninstall(args)
    if args.subcommand == "daemon":
        return run_daemon_loop()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
