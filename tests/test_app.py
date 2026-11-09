"""Tests for claude-monitor that run on Linux and Windows, locally and in CI.

    python -m unittest discover -s tests -v

They never touch real sessions: each run gets its own config folder, tmux socket and host port.
On Windows, CLAUDE_SESSIONS_TEST_PROGRAM can name a built claude-sessions-windows.exe, whose
session host is then the one tested.
"""
import contextlib
import hashlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "claude-monitor")
ECHO = os.path.join(ROOT, "tests", "echo.js")
WINDOWS = sys.platform == "win32"


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


CONFIG = tempfile.mkdtemp(prefix="claude-sessions-test-")
os.environ["CLAUDE_CONFIG_DIR"] = CONFIG
os.environ["CLAUDE_SESSIONS_TMUX_SOCKET"] = "claude-sessions-test-" + secrets.token_hex(4)
os.environ["CLAUDE_SESSIONS_HOST_PORT"] = str(18000 + secrets.randbelow(2000))
app = load(SOURCE, "claude_monitor")


def wait_for(check, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    return check()


@unittest.skipUnless(shutil.which("node") and (WINDOWS or shutil.which("tmux")), "needs node, and tmux on Linux")
class TerminalTests(unittest.TestCase):
    """Starting, reading, typing into and ending a hosted terminal: tmux on Linux, the session host on Windows."""

    host = None

    @classmethod
    def setUpClass(cls):
        program = os.environ.get("CLAUDE_SESSIONS_TEST_PROGRAM")
        if WINDOWS and program:
            cls.host = subprocess.Popen([program, "--session-host"], env=os.environ.copy())
            assert wait_for(lambda: app.host_call("ping")), "the built program's session host didn't answer"

    @classmethod
    def tearDownClass(cls):
        if WINDOWS:
            for item in (app.host_call("list") or {}).get("sessions", []):
                app.kill_terminal(item["name"])
        else:
            app.tmux("kill-server")
        if cls.host:
            cls.host.kill()

    def setUp(self):
        self.name = "test-" + secrets.token_hex(4)
        started, error = app.new_terminal(self.name, ROOT, ["node", ECHO])
        self.assertTrue(started, error)
        self.addCleanup(app.kill_terminal, self.name)
        self.assertTrue(wait_for(lambda: "READY" in app.session_screen(self.name)), app.session_screen(self.name))

    def shows(self, text):
        return wait_for(lambda: text in app.session_screen(self.name), 10)

    def test_listed_with_its_process(self):
        self.assertIn(self.name, app.hosted_sessions())
        self.assertIn(self.name, app.hosted_panes().values())

    def test_keys(self):
        for key in ("Up", "Down", "Enter", "Escape", "1"):
            self.assertTrue(app.send_keys(self.name, key))
            time.sleep(0.15)  # Escape on its own is told apart from the start of a key by timing.
        screen = self.shows("TEXT 1") and app.session_screen(self.name)
        self.assertEqual(re.findall(r"(?:KEY|TEXT) \w+", screen), ["KEY up", "KEY down", "KEY enter", "KEY esc", "TEXT 1"], screen)

    def test_text(self):
        self.assertTrue(app.send_text(self.name, "hello there"))
        self.assertTrue(self.shows("TEXT hello there"), app.session_screen(self.name))

    def test_paste_keeps_line_breaks(self):
        self.assertTrue(app.paste_text(self.name, "first line\nsecond line"))
        self.assertTrue(self.shows('PASTE["first line\\rsecond line"]'), app.session_screen(self.name))

    def test_ended(self):
        self.assertTrue(app.kill_terminal(self.name))
        self.assertTrue(wait_for(lambda: self.name not in app.hosted_sessions()))


class UpdateTests(unittest.TestCase):
    """Finding a newer release and installing it over the app's own file, from a stand-in for GitHub."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="claude-sessions-update-")
        with open(SOURCE, encoding="utf-8") as handle:
            self.source = handle.read()
        self.files = {}
        folder = self.folder

        files = self.files

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        os.environ["CLAUDE_SESSIONS_RELEASES_URL"] = self.base + "/latest"
        self.installed = os.path.join(folder, "claude-monitor")
        with open(self.installed, "w", encoding="utf-8") as handle:
            handle.write(self.stamped("1.0.0"))
        self.old = load(self.installed, "installed_" + secrets.token_hex(4))

    def tearDown(self):
        self.server.shutdown()
        os.environ.pop("CLAUDE_SESSIONS_RELEASES_URL", None)
        shutil.rmtree(self.folder, ignore_errors=True)

    def stamped(self, version):
        return self.source.replace('APP_VERSION = "dev"', f'APP_VERSION = "{version}"', 1)

    def publish(self, version, body, digest=None):
        asset = self.old.RELEASE_ASSET
        self.files["/asset"] = body
        self.files["/sums"] = f"{digest or hashlib.sha256(body).hexdigest()}  {asset}\n".encode()
        self.files["/latest"] = json.dumps({
            "tag_name": "v" + version, "html_url": self.base + "/page",
            "assets": [{"name": asset, "browser_download_url": self.base + "/asset"},
                       {"name": "SHA256SUMS", "browser_download_url": self.base + "/sums"}],
        }).encode()

    def test_offers_and_installs_a_newer_release(self):
        self.publish("1.2.0", self.stamped("1.2.0").encode())
        self.old.releases.check()
        self.assertEqual(self.old.releases.offer()["version"], "1.2.0")
        succeeded, message = self.old.releases.install()
        self.assertTrue(succeeded, message)
        with open(self.installed, encoding="utf-8") as handle:
            self.assertIn('APP_VERSION = "1.2.0"', handle.read())

    def test_no_offer_for_the_same_or_an_older_version(self):
        for version in ("1.0.0", "0.9.9"):
            self.publish(version, self.stamped(version).encode())
            self.old.releases.check()
            self.assertIsNone(self.old.releases.offer())

    def test_dev_copies_never_update(self):
        self.publish("9.0.0", self.stamped("9.0.0").encode())
        real, app.RELEASES_URL = app.RELEASES_URL, self.base + "/latest"
        self.addCleanup(setattr, app, "RELEASES_URL", real)
        app.releases.check()
        self.assertEqual(app.releases.latest["version"], "9.0.0")
        self.assertIsNone(app.releases.offer())

    def test_refuses_a_download_that_doesnt_match_its_checksum(self):
        self.publish("1.2.0", self.stamped("1.2.0").encode(), digest="0" * 64)
        self.old.releases.check()
        succeeded, message = self.old.releases.install()
        self.assertFalse(succeeded)
        self.assertIn("checksum", message)
        with open(self.installed, encoding="utf-8") as handle:
            self.assertIn('APP_VERSION = "1.0.0"', handle.read())

    @unittest.skipIf(WINDOWS, "the Windows download is a program, not a script")
    def test_refuses_a_broken_script(self):
        self.publish("1.2.0", b"def broken(:\n")
        self.old.releases.check()
        succeeded, message = self.old.releases.install()
        self.assertFalse(succeeded)
        with open(self.installed, encoding="utf-8") as handle:
            self.assertIn('APP_VERSION = "1.0.0"', handle.read())


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.settings = os.path.join(CONFIG, "settings.json")
        with open(self.settings, "w") as handle:
            json.dump({"model": "opus", "statusLine": {"type": "command", "command": "mine"},
                       "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other --thing"}]}]}}, handle)

    def test_adds_hooks_once_and_keeps_everything_else(self):
        self.assertEqual(app.run_setup(verbose=False), 0)
        self.assertEqual(app.run_setup(verbose=False), 0)
        with open(self.settings) as handle:
            settings = json.load(handle)
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["statusLine"]["command"], "mine")
        for event in app.HOOK_EVENTS:
            ours = [h for g in settings["hooks"][event] for h in g["hooks"] if "--hook" in h["command"]]
            self.assertEqual(len(ours), 1, event)
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "other --thing")
        context = [h for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "--context" in h["command"]]
        self.assertEqual(len(context), 1)
        self.assertNotIn("async", context[0])  # Its output has to reach Claude.


class BackgroundTaskTests(unittest.TestCase):
    """Background commands are found in the transcript and end when Claude Code reports back."""

    def transcript(self, *entries):
        path = os.path.join(tempfile.mkdtemp(dir=CONFIG), "session.jsonl")
        with open(path, "w") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        transcript = app.Transcript("session", path)
        transcript.poll()
        return transcript

    @staticmethod
    def started(use_id, task, description, at="2026-11-08T14:08:15Z"):
        return [
            {"type": "assistant", "timestamp": at, "message": {"content": [
                {"type": "tool_use", "id": use_id, "name": "Bash",
                 "input": {"command": "sleep 600", "description": description, "run_in_background": True}}]}},
            {"type": "user", "timestamp": at, "toolUseResult": {"backgroundTaskId": task},
             "message": {"content": [{"type": "tool_result", "tool_use_id": use_id,
                                      "content": f"Command running in background with ID: {task}."}]}},
        ]

    @staticmethod
    def notification(task, status="completed"):
        return (f"<task-notification>\n<task-id>{task}</task-id>\n<status>{status}</status>\n"
                f"<summary>Background command finished</summary>\n</task-notification>")

    def test_running_until_notified(self):
        t = self.transcript(*self.started("u1", "b1", "Run the evaluation"), *self.started("u2", "b2", "Start the emulator"))
        self.assertEqual([x["description"] for x in t.background_tasks(None)], ["Run the evaluation", "Start the emulator"])
        with open(t.path, "a") as handle:
            handle.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "content": self.notification("b1")}) + "\n")
            handle.write(json.dumps({"type": "user", "message": {"content": self.notification("b2", "running")}}) + "\n")
        t.poll()
        self.assertEqual([x["id"] for x in t.background_tasks(None)], ["b2"])

    def test_stopped_by_claude(self):
        t = self.transcript(*self.started("u1", "b1", "Watch CI"), {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u3", "name": "TaskStop", "input": {"task_id": "b1"}}]}})
        self.assertEqual(t.background_tasks(None), [])

    def test_open_files_confirm_tasks(self):
        t = self.transcript(*self.started("u1", "b1", "Run the evaluation"))
        self.assertEqual(t.background_tasks(set()), [])  # Its process has gone.
        # A foreground command's file is open too, but the transcript doesn't call it a background task.
        self.assertEqual([x["id"] for x in t.background_tasks({"b1", "f1"})], ["b1"])

    def test_found_by_id_when_its_start_was_not_read(self):
        t = self.transcript(*self.started("u1", "b1", "Run the evaluation"))
        t.background.clear()
        t.read_whole = False
        found = t.background_tasks({"b1", "f1"})
        self.assertEqual([(x["id"], x["description"]) for x in found], [("b1", "Run the evaluation")])
        self.assertIn("f1", t.not_background)

    def test_dropped_when_older_than_the_process(self):
        t = self.transcript(*self.started("u1", "b1", "Old task", at="2026-11-08T10:00:00Z"))
        self.assertEqual(t.background_tasks(None, started_at=app.entry_time({"timestamp": "2026-11-08T11:00:00Z"})), [])


RULE = "─" * 60
BOX = "\n".join(["● Working on it", "", "✻ Running the tests… (1m 2s)", "", RULE + " Meal Planner ─", "❯\xa0{text}", RULE,
                  "  Opus 5.5 | Ctx 16%", "  ⏵⏵ auto mode on · 1 shell"])
FREE = "\n".join(["● Done. The watcher is running in the background.", "", RULE + " Meal Planner ─", "❯\xa0{text}", RULE,
                   "  Opus 5.5 | Ctx 16%", "  ⏵⏵ auto mode on · 1 shell"])
MENU = "\n".join(["● Bash(rm -rf build)", "", " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"])


class SendNowTests(unittest.TestCase):
    """Send now types a queued message into a busy session only while its input box is showing."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(dir=CONFIG)
        self.saved = {name: getattr(app, name) for name in
                      ("QUEUE_FILE", "MONITOR_DIR", "ATTENTION_DIR", "hosted_session", "session_screen", "paste_text", "send_keys")}
        app.MONITOR_DIR = self.folder
        app.ATTENTION_DIR = self.folder
        app.QUEUE_FILE = os.path.join(self.folder, "queue.json")
        self.session = {"id": "s1", "tmux": "s1", "status": "busy", "failure": None}
        app.hosted_session = lambda monitor, request: self.session
        self.screens = [BOX.format(text=""), BOX.format(text="check the logs too")]
        app.session_screen = lambda name: self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        self.typed = []
        app.paste_text = lambda name, text: self.typed.append(text) or True
        app.send_keys = lambda name, *keys: self.typed.extend(keys) or True
        self.monitor = type("M", (), {"human_messages": lambda self, sid: 3})()
        with app.queue_locked() as state:
            state["messages"]["s1"] = [{"id": "a", "text": "check the logs too"}, {"id": "b", "text": "/clear"}]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)

    def queued(self):
        return app.read_queue()["messages"].get("s1", [])

    def test_input_box(self):
        self.assertEqual(app.input_box(BOX.format(text="")), "")
        self.assertEqual(app.input_box(BOX.format(text="shut down the emulator")), "shut down the emulator")
        self.assertIsNone(app.input_box(MENU))
        self.assertIsNone(app.input_box("● Just output\nno box here"))

    def test_types_it_and_presses_enter(self):
        ok, message = app.send_queued_now(self.monitor, {"id": "s1", "item": "a"})
        self.assertTrue(ok, message)
        self.assertEqual(self.typed, ["check the logs too", "Enter", "C-x", "C-s"])
        self.assertEqual([i["id"] for i in self.queued()], ["b"])
        self.assertEqual(app.read_queue()["checks"]["s1"]["count"], 3)

    def test_text_left_in_the_box_gets_its_own_paragraph(self):
        # After an interrupt Claude Code puts the message back in the box, and the next one
        # mustn't run on from it ("fix issuesBefore this chat...").
        self.screens = [BOX.format(text="read notes and fix issues")]
        self.assertTrue(app.type_into(self.session, "Before this chat is cleared"))
        self.assertEqual(self.typed, ["C-j", "C-j", "Before this chat is cleared"])
        self.typed.clear()
        self.screens = [BOX.format(text="")]
        self.assertTrue(app.type_into(self.session, "Before this chat is cleared"))
        self.assertEqual(self.typed, ["Before this chat is cleared"])

    def test_a_command_interrupts_claude_then_runs(self):
        self.screens = [BOX.format(text=""), BOX.format(text=""), FREE.format(text=""), FREE.format(text="/clear")]
        ok, message = app.send_queued_now(self.monitor, {"id": "s1", "item": "b"})
        self.assertTrue(ok, message)
        self.assertEqual(self.typed, ["Escape", "/clear", "Enter"])
        self.assertEqual([i["id"] for i in self.queued()], ["a"])

    def test_a_command_isnt_typed_after_text_put_back_in_the_box(self):
        self.screens = [BOX.format(text=""), FREE.format(text="check the logs too")]
        ok, _ = app.send_queued_now(self.monitor, {"id": "s1", "item": "b"})
        self.assertFalse(ok)
        self.assertEqual(self.typed, ["Escape"])
        self.assertEqual([i["id"] for i in self.queued()], ["a", "b"])

    def test_busy_only_on_background_tasks_counts_as_free(self):
        session = {"sessionId": "s1", "status": "busy", "statusUpdatedAt": 1000_000}
        with open(os.path.join(self.folder, "s1.json"), "w") as handle:
            json.dump({"kind": "done", "at": 1001}, handle)  # Stop came after the status last changed.
        self.screens = [FREE.format(text="")]
        self.assertTrue(app.turn_ended(session, "s1"))
        self.assertFalse(app.turn_ended(session, None))  # Not the app's own terminal, so it can't be checked.
        self.screens = [BOX.format(text="")]
        self.assertFalse(app.turn_ended(session, "s1"))  # Still working on screen.
        self.screens = [FREE.format(text="")]
        self.assertFalse(app.turn_ended({**session, "statusUpdatedAt": 1002_000}, "s1"))  # A new turn since.
        with open(os.path.join(self.folder, "s1.json"), "w") as handle:
            json.dump({"kind": "permission", "at": 1001}, handle)
        self.assertFalse(app.turn_ended(session, "s1"))

    def test_no_enter_if_a_menu_appears_while_typing(self):
        self.screens = [BOX.format(text=""), MENU]
        ok, _ = app.send_queued_now(self.monitor, {"id": "s1", "item": "a"})
        self.assertFalse(ok)
        self.assertEqual(self.typed, ["check the logs too"])
        self.assertIn("error", self.queued()[0])

    def test_nothing_typed_while_a_menu_shows(self):
        self.screens = [MENU]
        ok, _ = app.send_queued_now(self.monitor, {"id": "s1", "item": "a"})
        self.assertFalse(ok)
        self.assertEqual(self.typed, [])

    def test_refuses_while_a_prompt_is_waiting(self):
        self.session["status"] = "waiting"
        self.assertFalse(app.send_queued_now(self.monitor, {"id": "s1", "item": "a"})[0])
        self.assertFalse(app.send_queued_now(self.monitor, {"id": "s1", "item": "b"})[0])
        self.assertEqual(self.typed, [])

    def test_queued_mid_turn_counts_as_arrived(self):
        path = os.path.join(self.folder, "t.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "check the logs too"}) + "\n")
        transcript = app.Transcript("t", path)
        transcript.poll()
        self.assertEqual(transcript.human_messages, 1)


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
                    "53de0000000c4944415408d763f8cfc000000301010018dd8db40000000049454e44ae426082")


class PictureTests(unittest.TestCase):
    """Pictures are kept by the app, then each path is pasted on its own so Claude Code attaches it."""

    def setUp(self):
        self.saved = {name: getattr(app, name) for name in ("MONITOR_DIR", "QUEUE_FILE", "paste_text", "send_keys", "hosted_session")}
        app.MONITOR_DIR = tempfile.mkdtemp(dir=CONFIG)
        app.QUEUE_FILE = os.path.join(app.MONITOR_DIR, "queue.json")
        self.typed = []
        app.paste_text = lambda name, text: self.typed.append(text) or True
        app.send_keys = lambda name, *keys: self.typed.extend(keys) or True

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)

    def upload(self, data=PNG):
        import base64
        return app.save_upload(None, {"data": base64.b64encode(data).decode()})

    def test_keeps_only_pictures(self):
        ok, saved = self.upload()
        self.assertTrue(ok, saved)
        self.assertRegex(saved["name"], r"^[0-9a-f]{32}\.png$")
        self.assertTrue(app.upload_path(saved["name"]))
        self.assertFalse(self.upload(b"#!/bin/sh\necho hi\n")[0])
        self.assertFalse(app.save_upload(None, {"data": "not base64!"})[0])
        for bad in ("../queue.json", "x.png", saved["name"].replace(".png", ".sh")):
            self.assertIsNone(app.upload_path(bad))

    def test_text_then_each_path_on_its_own(self):
        names = [self.upload()[1]["name"], self.upload()[1]["name"]]
        ok, _ = app.type_message({"tmux": "s1"}, "What is this?", names)
        self.assertTrue(ok)
        folder = app.uploads_dir()
        self.assertEqual(self.typed, ["What is this?", "C-j", os.path.join(folder, names[0]), os.path.join(folder, names[1]), "Enter"])

    def test_picture_alone(self):
        name = self.upload()[1]["name"]
        app.type_message({"tmux": "s1"}, "", [name])
        self.assertEqual(self.typed, [os.path.join(app.uploads_dir(), name), "Enter"])

    def test_queued_with_its_pictures(self):
        name = self.upload()[1]["name"]
        app.hosted_session = lambda monitor, request: {"id": "s1", "tmux": "s1", "status": "busy"}
        ok, _ = app.send_reply(None, {"id": "s1", "text": "", "images": [name]})
        self.assertTrue(ok)
        self.assertEqual(app.read_queue()["messages"]["s1"][0]["images"], [name])
        self.assertFalse(app.send_reply(None, {"id": "s1", "text": "hi", "images": ["../../etc/passwd"]})[0])
        self.assertFalse(app.send_reply(None, {"id": "s1", "text": "/clear", "images": [name]})[0])

    def test_heic_converted_or_explained(self):
        import base64
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
        self.assertTrue(app.is_heif(heic))
        self.assertFalse(app.is_heif(PNG))
        saved = app.heif_to_jpeg
        try:
            app.heif_to_jpeg = lambda data: b"\xff\xd8\xff\xe0 a jpeg"
            ok, result = app.save_upload(None, {"data": base64.b64encode(heic).decode()})
            self.assertTrue(ok, result)
            self.assertTrue(result["name"].endswith(".jpg"))
            app.heif_to_jpeg = lambda data: None
            ok, result = app.save_upload(None, {"data": base64.b64encode(heic).decode()})
            self.assertFalse(ok)
            self.assertIn("HEIC", result)
        finally:
            app.heif_to_jpeg = saved

    def test_shown_in_the_chat(self):
        name = self.upload()[1]["name"]
        path = os.path.join(app.MONITOR_DIR, "t.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "user", "uuid": "1", "message": {"content": [
                {"type": "text", "text": "What is this?\n[Image #1]"}, {"type": "image", "source": {}}]}}) + "\n")
            handle.write(json.dumps({"type": "user", "uuid": "2", "isMeta": True, "message": {"content": [
                {"type": "text", "text": f"[Image: source: {os.path.join(app.uploads_dir(), name)}]"}]}}) + "\n")
        history = app.HistoryReader(path)
        history.poll()
        self.assertEqual([m.get("picture") for m in history.messages if m.get("picture")], [name])


class PhoneTests(unittest.TestCase):
    """Phone access has its own token, and only answers to an address, never a domain name."""

    def setUp(self):
        self.saved = {name: getattr(app, name) for name in ("MONITOR_DIR", "set_phone_listening")}
        app.MONITOR_DIR = tempfile.mkdtemp(dir=CONFIG)
        self.listening = []
        app.set_phone_listening = lambda on: self.listening.append(on) or ""

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)

    def test_hosts(self):
        port = app.PHONE_PORT
        for host in (f"192.168.0.11:{port}", f"10.0.0.2:{port}", f"[fe80::1]:{port}", f"{app.socket.gethostname()}.local:{port}"):
            self.assertTrue(app.phone_host_ok(host), host)
        for host in (f"evil.example:{port}", "192.168.0.11:80", f"192.168.0.11:{port + 1}", "", None, f":{port}"):
            self.assertFalse(app.phone_host_ok(host), host)

    def test_on_off_and_new_link(self):
        self.assertFalse(app.phone_settings()["enabled"])
        self.assertTrue(app.set_phone(None, {"action": "on"})[0])
        first = app.phone_settings()
        self.assertTrue(first["enabled"])
        self.assertGreaterEqual(len(first["token"]), 32)
        if not WINDOWS:
            self.assertEqual(os.stat(os.path.join(app.MONITOR_DIR, "phone.json")).st_mode & 0o777, 0o600)
        app.set_phone(None, {"action": "renew"})
        self.assertNotEqual(app.phone_settings()["token"], first["token"])
        app.set_phone(None, {"action": "off"})
        self.assertFalse(app.phone_settings()["enabled"])
        self.assertEqual(self.listening, [True, True, False])
        self.assertFalse(app.set_phone(None, {"action": "explode"})[0])


class PrinterTests(unittest.TestCase):
    """3D printer cameras: the printer's login, which addresses are allowed, and which sessions show it."""

    def setUp(self):
        with contextlib.suppress(OSError):
            os.remove(app.PRINTERS_FILE)

    def test_aes_matches_the_standard(self):
        # NIST SP 800-38A, F.2.2 (CBC-AES128 decrypt), first two blocks.
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        cipher = bytes.fromhex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2")
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51")
        self.assertEqual(app.aes128_cbc_decrypt(cipher, key, iv, unpad=False), plain)
        with self.assertRaises(ValueError):
            app.aes128_cbc_decrypt(cipher, key, iv)  # Not padded.

    def test_only_local_network_addresses(self):
        for good in ("192.168.0.66", " 10.1.2.3 ", "172.16.0.9"):
            self.assertEqual(app.printer_address(good), good.strip())
        for bad in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "192.168.0.66:80", "printer.local", "fd00::1", "", None):
            self.assertIsNone(app.printer_address(bad), bad)

    def test_stream_only_from_the_printer_itself(self):
        camera = app.PrinterCamera("192.168.0.66")
        self.assertEqual(camera._checked_stream("http://192.168.0.66:18088/live/Ab12"), "http://192.168.0.66:18088/live/Ab12")
        for bad in ("http://192.168.0.67:18088/live/x", "file:///etc/passwd", "rtmp://192.168.0.66/live", "http://192.168.0.66/x", None):
            self.assertIsNone(camera._checked_stream(bad), bad)

    def test_frames_split_from_ffmpeg(self):
        first, second = b"\xff\xd8" + b"one" + b"\xff\xd9", b"\xff\xd8" + b"two" + b"\xff\xd9"
        class Output:
            chunks = [first[:4], first[4:] + second[:3], second[3:], b""]
            def read1(self, size):
                return self.chunks.pop(0)
        camera = app.PrinterCamera("192.168.0.66")
        camera._read_frames(type("Process", (), {"stdout": Output()})())
        self.assertEqual(camera.image, second)

    def test_settings(self):
        self.assertFalse(app.printer_settings(None, {"action": "add", "host": "8.8.8.8"})[0])
        original = app.printer_info
        try:
            app.printer_info = lambda host, timeout=3: None
            self.assertFalse(app.printer_settings(None, {"action": "add", "host": "192.168.0.66"})[0])
            app.printer_info = lambda host, timeout=3: {"token": "x" * 32, "modelName": "Anycubic Kobra X"}
            self.assertEqual(app.printer_settings(None, {"action": "add", "host": "192.168.0.66"}), (True, {"printers": ["192.168.0.66"], "shares": {}}))
            self.assertEqual(app.read_printers(), ["192.168.0.66"])
            self.assertEqual(app.printer_settings(None, {"action": "remove", "host": "192.168.0.66"}), (True, {"printers": [], "shares": {}}))
        finally:
            app.printer_info = original

    def test_print_details(self):
        # As the Kobra X reported a real print on 2026-11-09.
        report = {"state": "busy", "temp": {"curr_hotbed_temp": 100, "curr_nozzle_temp": 260, "target_hotbed_temp": 100, "target_nozzle_temp": 260},
                  "project": {"curr_layer": 2, "filename": "water-tray_plate(01)_ASA_0.2_11h54m32s.gcode", "pause": 0, "print_status": 1,
                              "print_time": 69, "progress": 9, "remain_time": 645, "total_layers": 500}}
        self.assertEqual(app.printer_status(report), {
            "state": "printing", "file": "water-tray_plate(01)_ASA_0.2_11h54m32s", "progress": 9, "layer": 2, "layers": 500,
            "elapsed": 69, "remaining": 645, "nozzle": 260, "nozzle_target": 260, "bed": 100, "bed_target": 100})
        self.assertEqual(app.printer_status({"state": "free", "temp": {"curr_nozzle_temp": 25}})["state"], "idle")
        self.assertEqual(app.printer_status({"state": "busy", "project": {**report["project"], "pause": 1}})["state"], "paused")

    def test_shared_page_only_under_its_secret(self):
        import urllib.request, urllib.error
        server = app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.ShareHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        camera = app.printer_camera("192.168.0.77")
        camera.status, camera.status_at = {"state": "printing", "progress": 40}, time.monotonic()
        with app.share_state["lock"]:
            app.share_state["shares"]["192.168.0.77"] = "secret-token"
        self.addCleanup(lambda: app.share_state["shares"].pop("192.168.0.77", None))
        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        status, page = get("/secret-token/")
        self.assertEqual(status, 200)
        self.assertIn(b"printSummary", page)
        status, body = get("/secret-token/status")
        self.assertEqual(json.loads(body)["print"]["progress"], 40)
        for path in ("/", "/wrong-token/", "/secret-token/../api/sessions", "/api/sessions", "/secret-token/status/x"):
            self.assertEqual(get(path)[0], 404, path)
        app.stop_share("192.168.0.77")
        self.assertEqual(get("/secret-token/")[0], 404)

    def uses(self, *entries):
        return BackgroundTaskTests.transcript(self, *entries).device_uses

    @staticmethod
    def tool(name, tool_input, at="2026-11-08T18:00:00Z"):
        return {"type": "assistant", "timestamp": at, "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]}}

    def test_linked_by_printing_work(self):
        self.assertIn("printer", self.uses(self.tool("Write", {"file_path": "/home/me/models/clip.3mf", "content": ""})))
        self.assertIn("printer", self.uses(self.tool("Bash", {"command": "prusa-slicer --export-gcode part.stl"})))
        self.assertIn("printer", self.uses({"type": "user", "timestamp": "2026-11-08T18:00:00Z", "message": {"content": "Can you help me 3D print a hook?"}}))
        self.assertIn("printer", self.uses(self.tool("Bash", {"command": "cat /media/SSD3/Printables/spacer/HANDOFF.md"})))
        self.assertIn("printer-192.168.0.66", self.uses(self.tool("Bash", {"command": "curl http://192.168.0.66:18910/info"})))
        self.assertEqual(self.uses(self.tool("Bash", {"command": "npm test"}), self.tool("Read", {"file_path": "/src/app.py"})), {})


    def test_session_named_for_printing(self):
        monitor = app.Monitor()
        monitor.devices = [{"serial": "printer-192.168.0.66", "label": "Kobra X camera", "state": "device", "kind": "printer",
                            "starter": "", "starter_pid": None, "started": 0}]
        sessions = [{"id": "a", "name": "3D Printing", "pid": 1}, {"id": "b", "name": "Meal Planner", "pid": 2}]
        self.assertEqual([l["id"] for l in monitor.linked_devices(sessions)[0]["sessions"]], ["a"])


class QuestionTests(unittest.TestCase):
    """A question Claude asks shows in the chat with its options, and then the answers given."""

    def test_questions_and_answers(self):
        asked = app.asked_questions({"questions": [
            {"question": "Which colour?", "header": "Colour", "options": [{"label": "Red"}, {"label": "Blue"}]},
            {"question": "Which size?", "header": "Size", "multiSelect": True, "options": [{"label": "S"}, {"label": "L"}]}]})
        self.assertEqual(asked[0], {"header": "Colour", "question": "Which colour?", "options": ["Red", "Blue"], "multi": False})
        self.assertTrue(asked[1]["multi"])
        self.assertEqual(app.given_answers(asked, {"answers": {"Which colour?": "Blue", "Which size?": "S, L"}}, "", False), {"list": ["Blue", "S, L"]})
        # Without the structured result, the answers are read from the text.
        text = 'User has answered your questions: "Which colour?"="Green, please", "Which size?"="L".'
        self.assertEqual(app.given_answers(asked, None, text, False), {"list": ["Green, please", "L"]})
        self.assertEqual(app.given_answers(asked, None, "The user declined to answer.", True)["note"], "Not answered: The user declined to answer.")


class PreviewTests(unittest.TestCase):
    """A session can see the previews beside its chat, and is told about them once."""

    def test_print_summary(self):
        status = {"state": "printing", "progress": 20, "layer": 12, "layers": 500, "remaining": 565, "file": "tray",
                  "nozzle": 260, "nozzle_target": 260, "bed": 100, "bed_target": 100}
        self.assertEqual(app.print_summary(status), "Printing 20% \u00b7 layer 12 of 500 \u00b7 9h 25m left \u00b7 file tray \u00b7 nozzle 260/260\u00b0C \u00b7 bed 100/100\u00b0C")
        self.assertEqual(app.print_summary(None), "")

    def test_context_said_once_per_set_of_previews(self):
        import io
        saved = {name: getattr(app, name) for name in ("ask_running_app", "MONITOR_DIR")}
        app.MONITOR_DIR = tempfile.mkdtemp(dir=CONFIG)
        devices = [{"serial": "printer-192.168.0.66", "label": "Kobra X camera", "kind": "printer"}]
        app.ask_running_app = lambda path, timeout: (200, {"session": "3D Printing", "devices": devices})
        stdin, stdout = sys.stdin, sys.stdout
        try:
            said = []
            for _ in range(2):
                sys.stdin, sys.stdout = io.StringIO(json.dumps({"session_id": "0" * 8 + "-1111-2222-3333-" + "4" * 12})), io.StringIO()
                app.run_context_hook()
                said.append(sys.stdout.getvalue())
            context = json.loads(said[0])["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Kobra X camera", context)
            self.assertIn("--preview", context)
            self.assertEqual(said[1], "")
            app.ask_running_app = lambda path, timeout: None  # The app isn't running: say nothing.
            sys.stdin, sys.stdout = io.StringIO(json.dumps({"session_id": "5" * 8 + "-1111-2222-3333-" + "4" * 12})), io.StringIO()
            self.assertEqual(app.run_context_hook(), 0)
            self.assertEqual(sys.stdout.getvalue(), "")
        finally:
            sys.stdin, sys.stdout = stdin, stdout
            for name, value in saved.items():
                setattr(app, name, value)


class PortTests(unittest.TestCase):
    """Another program on the app's port: the app moves to the next one, not open that program's page."""

    def setUp(self):
        self.saved = {name: getattr(app, name) for name in ("run_queue_delivery", "serving_port")}
        self.saved_watch = app.releases.watch
        app.run_queue_delivery = lambda monitor: None
        app.releases.watch = lambda: None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)
        app.releases.watch = self.saved_watch

    def foreign_server(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"404 page not found\n"  # What a Go program says.
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        for _ in range(20):
            try:
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 20000 + secrets.randbelow(5000)), Handler)
                break
            except OSError:
                continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_moves_past_another_program(self):
        taken = self.foreign_server()
        self.assertFalse(app.app_answers(taken))
        self.assertFalse(app.host_answers(taken))
        threading.Thread(target=app.run_web, args=(None, taken, False), kwargs={"fixed_port": False}, daemon=True).start()
        moved = taken + 10000
        self.assertTrue(wait_for(lambda: app.app_answers(moved), 10))
        self.assertEqual(app.serving_port, moved)
        # A second copy finds the first rather than starting another server.
        with self.assertRaises(SystemExit) as raised:
            app.run_web(None, taken, False, fixed_port=False)
        self.assertIn(f"127.0.0.1:{moved}/", str(raised.exception.code))
        # Asked for that port by name, it says so instead of moving.
        with self.assertRaises(SystemExit) as raised:
            app.run_web(None, taken, False)
        self.assertIn("cannot use port", str(raised.exception.code))

    def test_choices_are_fixed(self):
        self.assertEqual(app.port_choices(8766), [8766, 18766, 28766, 38766, 48766])
        self.assertEqual(app.port_choices(30000), [30000, 40000, 50000, 60000])


class PasteTests(unittest.TestCase):
    """Replies are typed, so Claude Code only wraps what was really pasted, and the chat folds that."""

    def setUp(self):
        self.saved = {name: getattr(app, name) for name in ("send_keys", "paste_text", "TYPE_PAUSE")}
        self.sent = []
        app.send_keys = lambda target, *keys: self.sent.extend(("key", k) for k in keys) or True
        app.paste_text = lambda target, text: self.sent.append(("paste", text)) or True
        app.TYPE_PAUSE = 0

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)

    def test_lines_typed_with_ctrl_j(self):
        self.assertTrue(app.type_text("s", "one\r\ntwo\n\n" + "x" * 1200))
        self.assertEqual(self.sent, [("paste", "one"), ("key", "C-j"), ("paste", "two"), ("key", "C-j"), ("key", "C-j"),
                                     ("paste", "x" * 500), ("paste", "x" * 500), ("paste", "x" * 200)])

    def test_real_paste_sent_whole(self):
        log = "a\nb\nc\nd"
        self.assertTrue(app.type_text("s", "look:\n" + log + "\nthanks", [log]))
        self.assertEqual(self.sent, [("paste", "look:"), ("key", "C-j"), ("paste", log), ("key", "C-j"), ("paste", "thanks")])

    def test_long_paste_folded_short_one_not(self):
        log = "\n".join(f"line {i}" for i in range(10))
        plain, parts = app.pasted_parts(f'look:\n\n<pasted_content id="ab12">\n{log}\n</pasted_content id="ab12">\n\nthanks')
        self.assertEqual(parts, [{"text": "look:"}, {"paste": log, "lines": 10}, {"text": "thanks"}])
        self.assertIn("line 9", plain)
        self.assertNotIn("pasted_content", plain)
        plain, parts = app.pasted_parts('\n\n<pasted_content id="d23c">\nyes\nclose it\n</pasted_content id="d23c">\n')
        self.assertEqual((plain, parts), ("yes\nclose it", None))
        self.assertEqual(app.pasted_parts("plain"), ("plain", None))


if __name__ == "__main__":
    unittest.main()
