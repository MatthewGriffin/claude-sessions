"""Tests for claude-monitor that run on Linux and Windows, locally and in CI.

    python -m unittest discover -s tests -v

They never touch real sessions: each run gets its own config folder, tmux socket and host port.
On Windows, CLAUDE_SESSIONS_TEST_PROGRAM can name a built claude-sessions-windows.exe, whose
session host is then the one tested.
"""
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


if __name__ == "__main__":
    unittest.main()
