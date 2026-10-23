#!/usr/bin/env python3
"""冒烟测试：真实安装后台服务 → 手机发指令 → 假 claude 执行 → 结果推回 → 卸载。

覆盖各平台的守护进程安装路径（macOS launchd / Linux systemd 或后台进程 / Windows 登录自启 + 后台进程）。
使用本地假 ntfy 服务器与假 claude，不访问外网、不消耗 AI 额度。HOME 指向临时目录，不影响真实配置。

运行：python3 tests/smoke_daemon.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ntfy_ctl as T  # noqa: E402  导入时会把 HOME / USERPROFILE 指向临时目录


def run(args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(T.SCRIPT), *args],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 默认 cp1252，打印中文日志会报错
        stream.reconfigure(encoding="utf-8", errors="replace")
    srv = T.FakeNtfy()
    ws = Path(T.tempfile.mkdtemp(prefix="smoke_ws_"))
    bindir = T.make_fake_cli("claude", "print('smoke ok：已收到指令')\n")
    env = dict(os.environ, PATH=T.with_path(bindir))
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        env.pop(k, None)

    failures: list[str] = []
    try:
        r = run(["bind", "--host", "claude", "--workspace", str(ws), "--server", srv.url, "--title", "冒烟测试"], env)
        print("bind stdout:\n", r.stdout, "\nbind stderr:\n", r.stderr)
        out = json.loads(r.stdout)
        daemon = out["daemon"]
        print("daemon:", daemon)
        if not daemon.get("running"):
            failures.append("守护进程未运行")
        topic = out["channel"]["topic"]
        if not any("远程对话已就绪" in m["title"] for m in srv.json_msgs()):
            failures.append("没有收到绑定就绪通知")

        time.sleep(4)  # 等守护进程下一轮循环拉起频道监听
        srv.push(topic, message="你好，电脑")
        ok = T.wait_for(lambda: any("执行完成" in m["title"] or "未能执行" in m["title"] for m in srv.json_msgs()), 90)
        for m in srv.json_msgs():
            print("--", m["title"], "|", m["message"][:200].replace("\n", " / "))
        if not ok or not any("smoke ok" in m["message"] for m in srv.json_msgs()):
            failures.append("手机指令没有被执行并推回结果")
        calls = T.fake_calls(bindir) if (bindir / "calls.jsonl").is_file() else []
        if calls and "你好，电脑" not in calls[0]["stdin"]:
            failures.append("提示词没有通过标准输入传给 claude")

        panel = run(["panel"], env)
        print(panel.stdout)
        if "冒烟测试" not in panel.stdout:
            failures.append("panel 没有列出频道")
    except Exception as e:  # noqa: BLE001
        failures.append(f"异常: {e!r}")
    finally:
        un = run(["uninstall"], env)
        print("uninstall:", un.stdout, un.stderr)
        try:
            if json.loads(un.stdout).get("daemon_running"):
                failures.append("uninstall 后守护进程仍在运行")
        except Exception:
            failures.append("uninstall 输出无法解析")
        log_candidates = list(Path(os.environ["HOME"]).rglob("ntfy_hub.log"))
        for lp in log_candidates:
            print(f"==== {lp} ====")
            print(lp.read_text(encoding="utf-8", errors="replace")[-4000:])
        srv.close()

    if failures:
        print("\nSMOKE FAILED:\n- " + "\n- ".join(failures))
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
