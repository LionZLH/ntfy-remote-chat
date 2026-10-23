#!/usr/bin/env python3
"""ntfy_ctl.py 测试：纯标准库，使用本地假 ntfy 服务器，不访问外网。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "ntfy" / "scripts" / "ntfy_ctl.py"

# 在导入前把 HOME 指到临时目录，避免污染真实状态
TMP_HOME = tempfile.mkdtemp(prefix="ntfy_test_home_")
os.environ["HOME"] = TMP_HOME
os.environ.pop("CLAUDECODE", None)
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

spec = importlib.util.spec_from_file_location("ntfy_ctl", SCRIPT)
ntfy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ntfy)


class FakeNtfy:
    """极简 ntfy 服务器：记录发布请求，/topic/json 推送队列里的事件，/file/* 返回附件"""

    def __init__(self):
        self.published: list[dict] = []
        self.streams: dict[str, queue.Queue] = {}
        self.files: dict[str, bytes] = {}
        self.auth_seen: list[str] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                payload = json.loads(self._body() or b"{}")
                owner.published.append({"kind": "json", **payload})
                self._json({"id": "x"})

            def do_PUT(self):
                from urllib.parse import parse_qs, urlparse

                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                body = self._body()
                topic = u.path.strip("/")
                name = q.get("filename", "file.bin")
                fid = f"{len(owner.files)}_{name}"
                owner.files[fid] = body
                url = f"http://{self.headers['Host']}/file/{fid}"
                owner.published.append({"kind": "put", "topic": topic, "query": q, "size": len(body), "url": url})
                self._json({"id": "p", "attachment": {"name": name, "url": url}})

            def do_GET(self):
                owner.auth_seen.append(self.headers.get("Authorization", ""))
                if self.path.startswith("/file/"):
                    data = owner.files.get(self.path[len("/file/"):], b"")
                    self.send_response(200 if data else 404)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                topic = self.path.split("/")[1]
                q = owner.streams.setdefault(topic, queue.Queue())
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        try:
                            ev = q.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        line = (json.dumps(ev) + "\n").encode()
                        self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def push(self, topic: str, **ev):
        ev.setdefault("event", "message")
        ev.setdefault("time", int(time.time()))
        ev.setdefault("id", f"m{time.time_ns()}")
        self.streams.setdefault(topic, queue.Queue()).put(ev)

    def json_msgs(self):
        return [p for p in self.published if p["kind"] == "json"]

    def close(self):
        self.httpd.shutdown()


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def make_channel(server: str, workspace: str, **extra) -> tuple[str, dict]:
    cid = f"ch_test_{time.time_ns()}"
    ch = {
        "channel_id": cid, "title": "测试", "topic": f"nrc_t{time.time_ns()}", "server": server,
        "conv_id": "claude-test", "conv_id_auto": True, "workspace": workspace, "host": "claude",
        "status": "active", "auth_token": "", "keep_emoji": False,
    }
    ch.update(extra)
    with ntfy.registry_txn() as reg:
        reg["channels"][cid] = ch
    return cid, ch


class TextHelpers(unittest.TestCase):
    def test_strip_emoji_keeps_symbols_and_indent(self):
        src = "✅ 完成 ✓ ★\n    indented: 1\n👨‍💻 code ⚠️ 注意"
        self.assertEqual(ntfy.strip_emojis(src), "完成 ✓ ★\n    indented: 1\ncode 注意")

    def test_decode_cli_text(self):
        self.assertEqual(ntfy.decode_cli_text("a\\nb"), "a\nb")
        self.assertEqual(ntfy.decode_cli_text("a\nb\\n"), "a\nb\\n")  # 已有真实换行时不改动

    def test_truncate_utf8(self):
        s = "中" * 2000  # 6000 字节
        out = ntfy.truncate_utf8(s, 3800, "...")
        self.assertLessEqual(len(out.encode()), 3800)
        self.assertTrue(out.endswith("..."))

    def test_topic_has_no_identity(self):
        t = ntfy.generate_topic()
        self.assertTrue(t.startswith("nrc_"))
        self.assertEqual(len(t), 32)
        self.assertNotIn(os.environ.get("USER", "zzzz"), t)

    def test_trusted_attachment(self):
        self.assertTrue(ntfy.is_trusted_attachment_url("https://ntfy.sh/file/abc.png", "https://ntfy.sh"))
        self.assertFalse(ntfy.is_trusted_attachment_url("https://evil.com/file/abc.png", "https://ntfy.sh"))
        self.assertFalse(ntfy.is_trusted_attachment_url("http://ntfy.sh/file/a", "https://ntfy.sh"))

    def test_sensitive(self):
        for n in (".env", ".env.local", "id_rsa", "server.pem", "aws_credentials.json"):
            self.assertTrue(ntfy.is_sensitive_file(Path(n)), n)
        self.assertFalse(ntfy.is_sensitive_file(Path("report.pdf")))

    def test_pid_alive(self):
        self.assertTrue(ntfy.is_pid_alive(os.getpid()))
        self.assertFalse(ntfy.is_pid_alive(2 ** 22 + 12345))

    def test_sleep_policy(self):
        f = ntfy.should_inhibit_sleep
        self.assertTrue(f("auto", True, False, 10, 20))
        self.assertFalse(f("auto", True, True, 10, 20))  # 电池低电量释放
        self.assertTrue(f("on", False, True, 5, 20))
        self.assertFalse(f("auto", False, False, 90, 20))
        self.assertFalse(f("off", True, False, 90, 20))


class RegistryTests(unittest.TestCase):
    def test_concurrent_updates_not_lost(self):
        cid, _ = make_channel("http://x", TMP_HOME)

        def work(i):
            ntfy.update_channel(cid, **{f"k{i}": i})

        threads = [threading.Thread(target=work, args=(i,)) for i in range(30)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ch = ntfy.load_registry()["channels"][cid]
        for i in range(30):
            self.assertEqual(ch[f"k{i}"], i)

    def test_update_does_not_resurrect(self):
        cid, _ = make_channel("http://x", TMP_HOME)
        with ntfy.registry_txn() as reg:
            reg["channels"].pop(cid)
        self.assertIsNone(ntfy.update_channel(cid, last_event_id="abc"))
        self.assertNotIn(cid, ntfy.load_registry()["channels"])

    def test_registry_private(self):
        make_channel("http://x", TMP_HOME)
        mode = os.stat(ntfy.REGISTRY_PATH).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeNtfy()
        self.ws = tempfile.mkdtemp(prefix="ntfy_ws_")

    def tearDown(self):
        self.srv.close()

    def test_message_byte_limit(self):
        ok = ntfy.send_ntfy_message(self.srv.url, "t", "标题", "中" * 3000)
        self.assertTrue(ok)
        msg = self.srv.json_msgs()[-1]
        self.assertLessEqual(len(msg["message"].encode()), ntfy.NTFY_MSG_MAX_BYTES)
        self.assertIn("robot", msg["tags"])

    def test_text_doc_inline_keeps_indent(self):
        f = Path(self.ws) / "conf.yaml"
        f.write_text("root:\n    child: 1  # ✅\n", encoding="utf-8")
        self.assertTrue(ntfy.send_ntfy_file(self.srv.url, "t", f, title="t", message="看看"))
        put = [p for p in self.srv.published if p["kind"] == "put"][-1]
        self.assertEqual(put["topic"], "t_files")
        msg = self.srv.json_msgs()[-1]
        self.assertIn("    child: 1  # ✅", msg["message"])
        self.assertEqual(msg["click"], put["url"])

    def test_image_goes_to_main_topic(self):
        f = Path(self.ws) / "a.png"
        f.write_bytes(b"\x89PNG fake")
        self.assertTrue(ntfy.send_ntfy_file(self.srv.url, "t", f, title="图", message="说明"))
        put = [p for p in self.srv.published if p["kind"] == "put"][-1]
        self.assertEqual(put["topic"], "t")
        self.assertEqual(put["query"]["tags"], "robot")

    def test_notify_cli_rules(self):
        cid, ch = make_channel(self.srv.url, self.ws)
        # 未知频道且没有 --raw-topic：拒绝
        self.assertEqual(ntfy.main(["notify", "--channel", "typo-conversation-id", "--msg", "x"]), 1)
        # 敏感文件：拒绝
        env = Path(self.ws) / ".env"
        env.write_text("SECRET=1")
        self.assertEqual(ntfy.main(["notify", "--channel", cid, "--file", str(env)]), 1)
        # 字面 \n 转换
        self.assertEqual(ntfy.main(["notify", "--channel", cid, "--msg", "第一行\\n第二行"]), 0)
        self.assertEqual(self.srv.json_msgs()[-1]["message"], "第一行\n第二行")

    def test_worker_flow(self):
        cid, ch = make_channel(self.srv.url, self.ws)
        topic = ch["topic"]
        calls = []
        with mock.patch.object(ntfy, "dispatch_command_to_host",
                               side_effect=lambda c, chh, m, attachment_path=None: (calls.append((m, attachment_path)) or (True, ""))):
            w = ntfy.ChannelWorker(cid, ch)
            w.start()
            self.srv.push(topic, message="旧指令", time=int(time.time()) - 3600)
            self.srv.push(topic, message="机器人", tags=["robot"])
            self.srv.push(topic, message="真指令")
            self.srv.push(topic, message="/status")
            self.srv.push(topic, message="外部附件", attachment={"name": "x.png", "url": "https://evil.example/file/x.png"})
            self.srv.files["9_up.txt"] = b"hello"
            self.srv.push(topic, message="up.txt", attachment={"name": "up.txt", "url": f"{self.srv.url}/file/9_up.txt"})
            self.assertTrue(wait_for(lambda: len(calls) >= 3))
            w.stop()

        msgs = [c[0] for c in calls]
        self.assertEqual(msgs[0], "真指令")
        self.assertNotIn("旧指令", msgs)
        self.assertNotIn("机器人", msgs)
        self.assertEqual(calls[1], ("外部附件", None))  # 外部附件被拒绝，仅文字照常执行
        self.assertIn("up.txt", msgs[2])
        self.assertEqual(calls[2][1].read_text(), "hello")
        self.assertTrue((Path(self.ws) / ".ntfy_inbox" / ".gitignore").is_file())
        titles = [m["title"] for m in self.srv.json_msgs()]
        self.assertTrue(any("已过期未执行" in t for t in titles))
        self.assertTrue(any("系统状态" in t for t in titles))
        self.assertTrue(any("附件未接收" in t for t in titles))
        self.assertTrue(ntfy.load_registry()["channels"][cid]["last_event_id"])

    def test_worker_reports_failure(self):
        cid, ch = make_channel(self.srv.url, self.ws, host="gui")
        w = ntfy.ChannelWorker(cid, ch)
        w.start()
        self.srv.push(ch["topic"], message="做点事")
        ok = wait_for(lambda: any("未能执行" in m["title"] for m in self.srv.json_msgs()))
        w.stop()
        self.assertTrue(ok)

    def test_claude_dispatch(self):
        bindir = Path(tempfile.mkdtemp(prefix="fakebin_"))
        argv_log = bindir / "argv.json"
        out_img = Path(self.ws) / "out.png"
        out_img.write_bytes(b"\x89PNG")
        fake = bindir / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json,sys\n"
            f"json.dump(sys.argv[1:], open({str(argv_log)!r},'w'))\n"
            f"print('完成了\\nNTFY_FILE: {out_img}\\nNTFY_FILE: /etc/passwd')\n"
        )
        fake.chmod(0o755)
        cid, ch = make_channel(self.srv.url, self.ws)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}"}):
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "画图")
        self.assertTrue(ok, detail)
        argv = json.loads(argv_log.read_text())
        self.assertIn("--continue", argv)  # conv_id 是自动生成的，不能 --resume
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        puts = [p for p in self.srv.published if p["kind"] == "put"]
        self.assertEqual(len(puts), 1)  # 只推送了工作目录内的图片
        final = self.srv.json_msgs()[-1]["message"]
        self.assertIn("完成了", final)
        self.assertNotIn("NTFY_FILE", final)
        self.assertIn("已拒绝工作目录外的文件", final)


class PromptTests(unittest.TestCase):
    def test_prompt_example_not_parsed_as_file(self):
        ch = {"topic": "t", "workspace": TMP_HOME, "title": "x"}
        prompt = ntfy.build_prompt("cid", ch, "do", None, auto_reply=True)
        self.assertEqual(ntfy.NTFY_FILE_RE.findall(prompt), [])


class DaemonTests(unittest.TestCase):
    def test_single_instance(self):
        env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="ntfy_daemon_home_"))
        p1 = subprocess.Popen([sys.executable, str(SCRIPT), "daemon"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.5)
            p2 = subprocess.run([sys.executable, str(SCRIPT), "daemon"], env=env,
                                capture_output=True, text=True, timeout=10)
            self.assertEqual(p2.returncode, 0)
            self.assertIn("HUB_SKIP", p2.stdout)
            self.assertIsNone(p1.poll())
        finally:
            p1.terminate()
            p1.wait(timeout=10)

    def test_bind_dry_run_warnings(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "bind", "--host", "claude", "--workspace", TMP_HOME, "--dry-run"],
            capture_output=True, text=True, timeout=30, env=dict(os.environ),
        )
        out = json.loads(r.stdout)
        self.assertTrue(out["channel"]["conv_id_auto"])
        self.assertTrue(any("安全提示" in w for w in out["warnings"]))
        self.assertTrue(out["channel"]["topic"].startswith("nrc_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
