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
os.environ["USERPROFILE"] = TMP_HOME  # Windows 上 Path.home() 读取 USERPROFILE
os.environ["APPDATA"] = os.path.join(TMP_HOME, "AppData")
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


IS_WIN = sys.platform == "win32"


def make_fake_cli(name: str, body: str) -> Path:
    """生成假的 CLI：记录 argv、stdin 与 CLAUDECODE 到 calls.jsonl，再执行 body。返回所在目录。"""
    bindir = Path(tempfile.mkdtemp(prefix="fakebin_"))
    log_path = bindir / "calls.jsonl"
    code = (
        "import json,os,sys,functools\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"  # 模拟真实 CLI：按 UTF-8 输出（Windows 默认 cp1252）
        "open=functools.partial(open,encoding='utf-8')\n"
        "stdin=sys.stdin.buffer.read().decode('utf-8')\n"
        f"open({str(log_path)!r},'a',encoding='utf-8').write(json.dumps({{'argv':sys.argv[1:],'stdin':stdin,"
        "'claudecode':os.environ.get('CLAUDECODE')}, ensure_ascii=False)+'\\n')\n" + body
    )
    if IS_WIN:
        (bindir / f"{name}.py").write_text(code, encoding="utf-8")
        (bindir / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="utf-8")
    else:
        fake = bindir / name
        fake.write_text(f"#!{sys.executable}\n" + code, encoding="utf-8")
        fake.chmod(0o755)
    return bindir


def fake_calls(bindir: Path) -> list:
    return [json.loads(l) for l in (bindir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


def with_path(bindir: Path) -> str:
    return f"{bindir}{os.pathsep}{os.environ['PATH']}"


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

    def test_android_app_link(self):
        url = ntfy.build_app_url("https://ntfy.sh", "nrc_abc", "我的项目")
        self.assertEqual(url, "ntfy://ntfy.sh/nrc_abc?display=%E6%88%91%E7%9A%84%E9%A1%B9%E7%9B%AE")
        self.assertEqual(ntfy.build_app_url("http://10.0.0.2:8080", "t", ""), "ntfy://10.0.0.2:8080/t?secure=false")
        self.assertEqual(ntfy.build_app_url("https://x.com/ntfy", "t", "p"), "")  # 带路径前缀 App 不支持
        guide = ntfy.build_subscribe_guide("https://my.host", "t1", "")
        self.assertTrue(any("my.host" in g for g in guide["iphone"]))
        self.assertTrue(all("ntfy://" not in g for g in guide["android"]))

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

    @unittest.skipIf(IS_WIN, "POSIX 文件权限")
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
        env.write_text("SECRET=1", encoding="utf-8")
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
        self.assertEqual(calls[2][1].read_text(encoding="utf-8"), "hello")
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
        out_img = Path(self.ws) / "out.png"
        out_img.write_bytes(b"\x89PNG")
        outside = Path(TMP_HOME) / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        reply = f"完成了\nNTFY_FILE: {out_img}\nNTFY_FILE: {outside}"
        bindir = make_fake_cli("claude", f"print({reply!r})\n")
        cid, ch = make_channel(self.srv.url, self.ws)
        env = {"PATH": with_path(bindir), "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env):
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "画图 & \"引号\"")
        self.assertTrue(ok, detail)
        call = fake_calls(bindir)[0]
        argv = call["argv"]
        # 拿不到会话 ID：新建频道专属会话，而不是 --continue 接上同目录里最近的（可能是别人的）会话
        self.assertNotIn("--continue", argv)
        own = argv[argv.index("--session-id") + 1]
        saved = ntfy.load_registry()["channels"][cid]
        self.assertEqual(saved["own_session_id"], own)
        self.assertTrue(saved["own_session_created"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[-1], "-p")  # 提示词不在命令行参数里
        self.assertIn("画图 & \"引号\"", call["stdin"])  # 而是经标准输入原样传入
        self.assertIsNone(call["claudecode"])  # 不继承当前会话标记
        puts = [p for p in self.srv.published if p["kind"] == "put"]
        self.assertEqual(len(puts), 1)  # 只推送了工作目录内的图片
        final = self.srv.json_msgs()[-1]["message"]
        self.assertIn("完成了", final)
        self.assertNotIn("NTFY_FILE", final)
        self.assertIn("已拒绝工作目录外的文件", final)
        with mock.patch.dict(os.environ, env):
            ntfy.dispatch_command_to_host(cid, saved, "第二条")
        argv2 = fake_calls(bindir)[1]["argv"]
        self.assertEqual(argv2[argv2.index("--resume") + 1], own)  # 第二条指令续接同一个专属会话

    def test_claude_found_via_bind_path(self):
        bindir = make_fake_cli("claude", "print('ok')\n")
        cid, ch = make_channel(self.srv.url, self.ws, env_path=str(bindir), conv_id="real-id", conv_id_auto=False)
        with mock.patch.dict(os.environ, {"PATH": str(Path(TMP_HOME) / "empty_bin")}):  # 模拟 launchd 精简 PATH
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "hi")
        self.assertTrue(ok, detail)
        self.assertEqual(fake_calls(bindir)[0]["argv"][:2], ["--resume", "real-id"])

    def test_codex_resume_fallback_rules(self):
        # 会话不存在：退回新会话
        bindir = make_fake_cli(
            "codex",
            "if 'resume' in sys.argv:\n    sys.stderr.write('Error: no rollout found for thread id x'); sys.exit(1)\n"
            "out=sys.argv[sys.argv.index('--output-last-message')+1]\nopen(out,'w').write('新会话完成')\n",
        )
        cid, ch = make_channel(self.srv.url, self.ws, host="codex", conv_id="t-1", conv_id_auto=False)
        with mock.patch.dict(os.environ, {"PATH": with_path(bindir)}):
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "任务")
        self.assertTrue(ok, detail)
        calls = fake_calls(bindir)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["argv"][-3:], ["resume", "t-1", "-"])
        self.assertEqual(calls[1]["argv"][-1], "-")
        self.assertIn("任务", calls[1]["stdin"])
        self.assertIn("新会话完成", self.srv.json_msgs()[-1]["message"])

        # 其他失败：直接上报，不重复执行
        bindir2 = make_fake_cli("codex", "sys.stderr.write('model error'); sys.exit(1)\n")
        with mock.patch.dict(os.environ, {"PATH": with_path(bindir2)}):
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "任务")
        self.assertFalse(ok)
        self.assertIn("model error", detail)
        self.assertEqual(len(fake_calls(bindir2)), 1)

    def test_codex_own_session(self):
        bindir = make_fake_cli(
            "codex",
            "out=sys.argv[sys.argv.index('--output-last-message')+1]\nopen(out,'w').write('完成')\n"
            "if '--json' in sys.argv:\n    print(json.dumps({'type':'thread.started','thread_id':'th-own-1'}))\n",
        )
        cid, ch = make_channel(self.srv.url, self.ws, host="codex")  # conv_id 自动生成
        with mock.patch.dict(os.environ, {"PATH": with_path(bindir)}):
            ok, detail = ntfy.dispatch_command_to_host(cid, ch, "一")
            self.assertTrue(ok, detail)
            saved = ntfy.load_registry()["channels"][cid]
            self.assertEqual(saved["own_session_id"], "th-own-1")
            ok, detail = ntfy.dispatch_command_to_host(cid, saved, "二")
        calls = fake_calls(bindir)
        self.assertNotIn("resume", calls[0]["argv"])
        self.assertEqual(calls[1]["argv"][-3:], ["resume", "th-own-1", "-"])
        self.assertEqual(self.srv.json_msgs()[-1]["message"], "完成")

    def test_legacy_channel_auto_conv_id(self):
        self.assertTrue(ntfy.is_auto_conv_id({"host": "claude", "alias": "proj", "conv_id": "claude-proj"}))
        self.assertFalse(ntfy.is_auto_conv_id({"host": "claude", "alias": "proj", "conv_id": "3f2a-uuid"}))
        self.assertFalse(ntfy.is_auto_conv_id({"conv_id": "claude-proj", "conv_id_auto": False}))


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeNtfy()
        with ntfy.registry_txn() as reg:
            reg["channels"] = {}
        self.c1, _ = make_channel(self.srv.url, TMP_HOME, title="项目甲", conv_id="conv-a", conv_id_auto=False)
        self.c2, _ = make_channel(self.srv.url, TMP_HOME, title="项目乙", conv_id="conv-b", conv_id_auto=False)

    def tearDown(self):
        self.srv.close()

    def test_find_by_index_and_title(self):
        reg = ntfy.load_registry()
        self.assertEqual(ntfy.find_channel(reg, "#2")[0], self.c2)
        self.assertEqual(ntfy.find_channel(reg, "1")[0], self.c1)
        self.assertEqual(ntfy.find_channel(reg, "项目乙")[0], self.c2)
        self.assertEqual(ntfy.find_channel(reg, "")[0], None)

    def test_pause_resume_rm_clear(self):
        self.assertEqual(ntfy.main(["pause", "1"]), 0)
        self.assertEqual(ntfy.load_registry()["channels"][self.c1]["status"], "paused")
        self.assertEqual(ntfy.main(["resume", "#1"]), 0)
        self.assertEqual(ntfy.load_registry()["channels"][self.c1]["status"], "active")
        self.assertEqual(ntfy.main(["rm", "2"]), 0)
        self.assertNotIn(self.c2, ntfy.load_registry()["channels"])
        self.assertTrue(any("频道已销毁" in m["title"] for m in self.srv.json_msgs()))
        self.assertEqual(ntfy.main(["unbind", "--channel", "conv-a", "--quiet"]), 0)
        self.assertEqual(ntfy.load_registry()["channels"], {})
        make_channel(self.srv.url, TMP_HOME)
        self.assertEqual(ntfy.main(["clear", "--quiet"]), 0)
        self.assertEqual(ntfy.load_registry()["channels"], {})

    def test_panel_output(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(ntfy.main(["panel"]), 0)
        out = buf.getvalue()
        self.assertIn("**`#1`**", out)
        self.assertIn("项目乙", out)

    def test_phone_delete(self):
        ch = ntfy.load_registry()["channels"][self.c1]
        self.assertTrue(ntfy.handle_meta_command(self.c1, ch, "销毁频道。"))
        self.assertNotIn(self.c1, ntfy.load_registry()["channels"])


class PromptTests(unittest.TestCase):
    def test_prompt_example_not_parsed_as_file(self):
        ch = {"topic": "t", "workspace": TMP_HOME, "title": "x"}
        prompt = ntfy.build_prompt("cid", ch, "do", None, auto_reply=True)
        self.assertEqual(ntfy.NTFY_FILE_RE.findall(prompt), [])


class DaemonTests(unittest.TestCase):
    def test_single_instance(self):
        home = tempfile.mkdtemp(prefix="ntfy_daemon_home_")
        env = dict(os.environ, HOME=home, USERPROFILE=home)
        p1 = subprocess.Popen([sys.executable, str(SCRIPT), "daemon"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        try:
            time.sleep(1.5)
            p2 = subprocess.run([sys.executable, str(SCRIPT), "daemon"], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(p2.returncode, 0)
            self.assertIn("HUB_SKIP", p2.stdout)
            self.assertIsNone(p1.poll())
        finally:
            p1.terminate()
            p1.wait(timeout=10)

    def test_multi_dialog_binds_do_not_merge(self):
        """同一项目的多个对话框（含拿不到会话 ID 的）各自独立；会话 ID 恰好等于别的频道名称时也不会认错"""
        home = tempfile.mkdtemp(prefix="ntfy_multi_home_")
        ws = tempfile.mkdtemp(prefix="proj_")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "ANTIGRAVITY"))}
        base.update(HOME=home, USERPROFILE=home)

        def bind(extra, *args):
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "bind", "--no-test", "--dry-run", "--host", "claude",
                 "--workspace", ws, *args],
                capture_output=True, text=True, encoding="utf-8", timeout=30, env=dict(base, **extra),
            )
            return json.loads(r.stdout)["channel"]

        # dry-run 不写注册表，这里用真实写入来检验去重逻辑
        def bind_real(extra, *args):
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "bind", "--no-test", "--server", "http://127.0.0.1:9", "--host", "claude",
                 "--workspace", ws, *args],
                capture_output=True, text=True, encoding="utf-8", timeout=60, env=dict(base, **extra),
            )
            return json.loads(r.stdout)["channel"]

        try:
            a = bind_real({"CLAUDE_CODE_SESSION_ID": "sess-a"}, "--title", "sess-b")
            b = bind_real({"CLAUDE_CODE_SESSION_ID": "sess-b"})
            c = bind_real({})
            d = bind_real({})
            a2 = bind_real({"CLAUDE_CODE_SESSION_ID": "sess-a"})
            ids = [x["channel_id"] for x in (a, b, c, d)]
            self.assertEqual(len(set(ids)), 4)
            self.assertEqual(len({x["topic"] for x in (a, b, c, d)}), 4)
            self.assertTrue(c["conv_id_auto"] and d["conv_id_auto"])
            self.assertEqual(a2["channel_id"], a["channel_id"])  # 同一对话重复绑定：复用原频道
            self.assertEqual(a2["topic"], a["topic"])
            self.assertIsNotNone(bind)
        finally:
            subprocess.run([sys.executable, str(SCRIPT), "uninstall", "--purge"], env=base,
                           capture_output=True, timeout=60)

    def test_bind_dry_run_warnings(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "bind", "--host", "claude", "--workspace", TMP_HOME, "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", timeout=30, env=dict(os.environ),
        )
        out = json.loads(r.stdout)
        self.assertTrue(out["channel"]["conv_id_auto"])
        self.assertTrue(any("安全提示" in w for w in out["warnings"]))
        self.assertTrue(out["channel"]["topic"].startswith("nrc_"))
        self.assertTrue(out["channel"]["app_url"].startswith("ntfy://ntfy.sh/nrc_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
