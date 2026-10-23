#!/usr/bin/env python3
"""联网测试：用真实的 ntfy.sh 服务器验证「连发多条」与「断线补收」。

1. 实时连发：监听线程连上后，一次性连发 5 条消息，检查是否全部及时收到；
2. 断线补收：监听停止期间发 3 条消息，重新连接后用 since 游标补收，检查是否全部补回。

使用随机 Topic，消息里没有任何敏感内容。需要能访问 https://ntfy.sh。
运行：python3 tests/live_burst.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_ntfy_ctl as T  # noqa: E402  导入时会把 HOME 指向临时目录

ntfy = T.ntfy
SERVER = "https://ntfy.sh"


def publish(topic: str, text: str) -> None:
    req = urllib.request.Request(
        f"{SERVER}/{topic}", data=text.encode("utf-8"), method="POST",
        headers={"User-Agent": "ntfy-remote-chat-ci"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    topic = "nrc_ci_" + secrets.token_hex(12)
    cid = "ch_live"
    with ntfy.registry_txn() as reg:
        reg["channels"] = {cid: {
            "channel_id": cid, "title": "CI", "topic": topic, "server": SERVER, "host": "claude",
            "conv_id": "ci", "conv_id_auto": True, "workspace": T.TMP_HOME, "status": "active",
            "auth_token": "", "keep_emoji": False, "progress_interval": 0,
        }}

    received: list[tuple[str, float]] = []

    def record(c, ch, m, attachment_path=None):
        received.append((m, time.time()))
        return True, ""

    failures = []
    # 回复消息会真实发到 ntfy.sh 的随机 Topic：无害，但这里直接屏蔽，减少请求
    with mock.patch.object(ntfy, "dispatch_command_to_host", side_effect=record), \
            mock.patch.object(ntfy, "send_ntfy_message", return_value=True):
        # 1. 实时连发
        w = ntfy.ChannelWorker(cid, ntfy.load_registry()["channels"][cid])
        w.start()
        time.sleep(4)  # 等待长连接建立
        sent_at = {}
        for i in range(5):
            text = f"连发-{i}"
            sent_at[text] = time.time()
            publish(topic, text)
        T.wait_for(lambda: len(received) >= 5, 30)
        got = [m for m, _ in received]
        print("实时连发收到：", got)
        for m, t in received:
            print(f"  {m} 延迟 {t - sent_at.get(m, t):.2f}s")
        if sorted(got) != sorted(sent_at):
            failures.append(f"实时连发：发了 5 条，收到 {len(got)} 条")
        elif max(t - sent_at[m] for m, t in received) > 10:
            failures.append("实时连发：有消息延迟超过 10 秒")
        w.stop()
        time.sleep(1)

        # 2. 断线补收
        received.clear()
        offline = [f"离线-{i}" for i in range(3)]
        for text in offline:
            publish(topic, text)
        time.sleep(1)
        w2 = ntfy.ChannelWorker(cid, ntfy.load_registry()["channels"][cid])
        w2.start()
        T.wait_for(lambda: len(received) >= 3, 30)
        got2 = [m for m, _ in received]
        print("断线补收：", got2)
        if sorted(got2) != sorted(offline):
            failures.append(f"断线补收：离线期间发了 3 条，补收到 {len(got2)} 条")
        w2.stop()

    if failures:
        print("\nLIVE FAILED:\n- " + "\n- ".join(failures))
        return 1
    print("\nLIVE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
