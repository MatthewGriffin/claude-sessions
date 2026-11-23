"""Tests for claude-monitor that run on Linux and Windows, locally and in CI.

    python -m unittest discover -s tests -v

They never touch real sessions: each run gets its own config folder, tmux socket and host port.
On Windows, CLAUDE_SESSIONS_TEST_PROGRAM can name a built claude-sessions-windows.exe, whose
session host is then the one tested.
"""
import contextlib
import hashlib
import http.client
import http.server
import importlib.machinery
import importlib.util
import base64
import io
import datetime
import json
import types
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

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
os.environ["COPILOT_HOME"] = tempfile.mkdtemp(prefix="copilot-sessions-test-")  # So no test sees the real Copilot sessions.
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

    @unittest.skipUnless(WINDOWS, "attaching is for the Windows session host")
    def test_a_window_can_attach_and_type(self):
        token, port = open(app.HOST_TOKEN_FILE).read().split()[:2]
        with socket.create_connection(("127.0.0.1", int(port)), timeout=10) as connection:
            connection.sendall(json.dumps({"op": "attach", "name": self.name, "token": token, "columns": 100, "rows": 30}).encode() + b"\n")
            received = b""
            deadline = time.time() + 15
            while b"\n" not in received and time.time() < deadline:
                received += connection.recv(4096)
            self.assertTrue(json.loads(received.split(b"\n")[0])["ok"], received)
            connection.sendall(app.attach_frame(b"d", b"from the window"))
            deadline = time.time() + 15
            while b"TEXT from the window" not in received and time.time() < deadline:
                received += connection.recv(4096)
            self.assertIn(b"TEXT from the window", received)
        # The app still sees it, and the terminal goes back to the app's size when the window leaves.
        self.assertTrue(self.shows("TEXT from the window"))
        self.assertTrue(wait_for(lambda: app.host_call("screen", name=self.name)["screen"].count("\n") + 1 == app.TERM_ROWS, 10))

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
        os.environ["CLAUDE_SESSIONS_INSTALLERS_DIR"] = os.path.join(folder, "installers")  # Missing: beside the program.
        self.installed = os.path.join(folder, "claude-monitor")
        with open(self.installed, "w", encoding="utf-8") as handle:
            handle.write(self.stamped("1.0.0"))
        self.old = load(self.installed, "installed_" + secrets.token_hex(4))

    def tearDown(self):
        self.server.shutdown()
        os.environ.pop("CLAUDE_SESSIONS_RELEASES_URL", None)
        os.environ.pop("CLAUDE_SESSIONS_INSTALLERS_DIR", None)
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
        # Windows saves it as an installer to run, which then installs itself.
        with open(self.old.releases.staged if WINDOWS else self.installed, encoding="utf-8") as handle:
            self.assertIn('APP_VERSION = "1.2.0"', handle.read())

    def test_prefers_the_file_named_with_its_version(self):
        self.publish("1.2.0", b"plain")
        versioned = self.old.versioned_asset("1.2.0")
        listing = json.loads(self.files["/latest"])
        listing["assets"].append({"name": versioned, "browser_download_url": self.base + "/versioned"})
        self.files["/latest"] = json.dumps(listing).encode()
        self.old.releases.check()
        self.assertTrue(self.old.releases.latest["url"].endswith("/versioned"))
        if WINDOWS:
            self.assertEqual(versioned, "claude-sessions-windows-1.2.0.exe")

    def test_a_failed_check_says_why(self):
        self.old.releases.check_now()  # Nothing published: 404.
        self.assertIn("Couldn't reach GitHub", self.old.releases.problem)
        self.publish("1.2.0", b"x")
        self.assertEqual(self.old.releases.check_now(), (True, "A newer version is available."))
        self.assertEqual(self.old.releases.problem, "")

    def test_a_release_without_a_download_for_this_computer_says_so(self):
        self.publish("1.2.0", b"x")
        listing = json.loads(self.files["/latest"])
        listing["assets"] = []
        self.files["/latest"] = json.dumps(listing).encode()
        self.old.releases.check()
        self.assertIn("no download", self.old.releases.problem)

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


@unittest.skipUnless(WINDOWS and os.environ.get("CLAUDE_SESSIONS_TEST_PROGRAM"), "needs the built Windows program")
class BuiltHookTests(unittest.TestCase):
    """The built program, run as Claude Code runs a status line or hook: JSON in on stdin, text out."""

    def run_program(self, flag, payload):
        config = tempfile.mkdtemp(prefix="claude-sessions-built-hook-")
        self.addCleanup(shutil.rmtree, config, True)
        started = time.monotonic()
        result = subprocess.run([os.environ["CLAUDE_SESSIONS_TEST_PROGRAM"], flag], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                capture_output=True, timeout=60, env={**os.environ, "CLAUDE_CONFIG_DIR": config})
        print(f"{flag}: {time.monotonic() - started:.2f} s, exit {result.returncode}, stdout {result.stdout[:200]!r}, stderr {result.stderr[-300:]!r}")
        return result, os.path.join(config, "monitor")

    def test_status_line_saves_the_figures_and_prints(self):
        result, monitor = self.run_program("--statusline", {
            "session_id": "abc-123", "cwd": "C:/Users/Zoë/project", "model": {"display_name": "Opus 5.5"},
            "context_window": {"used_percentage": 42}, "rate_limits": {"five_hour": {"used_percentage": 10}}})
        self.assertIn(b"Ctx 42%", result.stdout)
        with open(os.path.join(monitor, "status", "abc-123.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["context_percent"], 42)

    def test_status_line_runs_the_users_own_after_saving(self):
        config = tempfile.mkdtemp(prefix="claude-sessions-built-status-")
        self.addCleanup(shutil.rmtree, config, True)
        os.makedirs(os.path.join(config, "monitor"))
        with open(os.path.join(config, "monitor", "statusline-after.json"), "w") as handle:
            json.dump({"command": "echo \"theirs $(cat | wc -c)\""}, handle)
        payload = json.dumps({"session_id": "abc-123", "context_window": {"used_percentage": 42}}).encode()
        started = time.monotonic()
        result = subprocess.run([os.environ["CLAUDE_SESSIONS_TEST_PROGRAM"], "--statusline"], input=payload,
                                capture_output=True, timeout=60, env={**os.environ, "CLAUDE_CONFIG_DIR": config})
        print(f"wrapped status line: {time.monotonic() - started:.2f} s, stdout {result.stdout!r}, stderr {result.stderr[-300:]!r}")
        self.assertEqual(result.stdout.strip(), f"theirs {len(payload)}".encode())
        with open(os.path.join(config, "monitor", "status", "abc-123.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["context_percent"], 42)

    def test_hook_records_a_finished_turn(self):
        result, monitor = self.run_program("--hook", {
            "session_id": "abc-123", "hook_event_name": "Stop", "cwd": "C:/Users/Zoë/project", "last_assistant_message": "Done – all good"})
        with open(os.path.join(monitor, "attention", "abc-123.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["kind"], "done")


@unittest.skipUnless(WINDOWS and os.environ.get("CLAUDE_SESSIONS_TEST_PROGRAM"), "needs the built Windows program")
class BuiltUpdateTests(unittest.TestCase):
    """The built program updates itself from a stand-in release and comes back, twice in a row."""

    NAME = "cs-update-test.exe"

    def setUp(self):
        # The long form: Windows reports process paths that way, while the temp folder may be RUNNER~1.
        self.folder = os.path.realpath(tempfile.mkdtemp(prefix="claude-sessions-built-update-"))
        self.program = os.path.join(self.folder, self.NAME)
        with open(os.environ["CLAUDE_SESSIONS_TEST_PROGRAM"], "rb") as handle:
            self.body = handle.read()
        with open(self.program, "wb") as handle:
            handle.write(self.body)
        files = self.files = {}

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
        self.port = 21000 + secrets.randbelow(2000)
        self.config = os.path.join(self.folder, "config")
        self.addCleanup(self.stop)

    def stop_programs(self):
        """Every process running from this test's folder, and nothing else."""
        script = "Get-Process | Where-Object { $_.Path -and $_.Path -like ($env:CS_FOLDER + '\\*') } | Stop-Process -Force"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "CS_FOLDER": self.folder}, capture_output=True, timeout=60)

    def stop(self):
        self.stop_programs()
        self.server.shutdown()
        time.sleep(1)
        shutil.rmtree(self.folder, ignore_errors=True)

    def publish(self, version):
        name = f"claude-sessions-windows-{version}.exe"
        self.files["/asset"] = self.body
        self.files["/sums"] = f"{hashlib.sha256(self.body).hexdigest()}  {name}\n".encode()
        self.files["/latest"] = json.dumps({"tag_name": "v" + version, "html_url": self.base + "/page", "assets": [
            {"name": name, "browser_download_url": self.base + "/asset"},
            {"name": "SHA256SUMS", "browser_download_url": self.base + "/sums"}]}).encode()

    def request(self, path, body=None):
        import http.client
        with open(os.path.join(self.config, "monitor", "token")) as handle:
            token = handle.read().strip()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": f"monitor_token={token}", "Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        connection.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")

    def answers(self):
        try:
            return self.request("/api/sessions")[0] == 200
        except (OSError, ValueError):
            return False

    def test_updates_and_restarts(self):
        # Like a real start from the Start menu: none of PyInstaller's variables in the environment.
        environment = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
        # The app won't start without Claude Code, so a stand-in that lists no sessions.
        tools = os.path.join(self.folder, "tools")
        installers = os.path.join(self.folder, "installers")  # Like C:\\installers on a locked-down laptop.
        local = os.path.join(self.folder, "local")
        for folder in (tools, installers, local):
            os.makedirs(folder)
        with open(os.path.join(tools, "claude.cmd"), "w") as handle:
            handle.write("@echo []\n")
        environment.update(CLAUDE_CONFIG_DIR=self.config, CLAUDE_SESSIONS_RELEASES_URL=self.base + "/latest",
                           CLAUDE_SESSIONS_INSTALLERS_DIR=installers, LOCALAPPDATA=local,
                           PATH=tools + os.pathsep + environment.get("PATH", ""),
                           # Its own session host port: the terminal tests' host may still hold theirs.
                           CLAUDE_SESSIONS_HOST_PORT=str(23000 + secrets.randbelow(2000)))
        environment.pop("CLAUDE_SESSIONS_NO_INSTALL", None)
        started = time.time()
        subprocess.run([self.program, "--statusline"], input=b"{}", env=environment, capture_output=True, timeout=60)
        print(f"\n  one status line run took {time.time() - started:.1f} s", file=sys.stderr)
        self.publish("998.0.0")
        first = subprocess.Popen([self.program, "--web", "--no-open", "--port", str(self.port)], env=environment)
        self.assertTrue(wait_for(self.answers, 60), "the program didn't start")
        # A session host keeps running from the program it started from, as it does while sessions run.
        host = subprocess.Popen([self.program, "--session-host"], env=environment)
        time.sleep(5)
        self.assertIsNone(host.poll(), "the session host didn't keep running")
        for attempt in ("first", "second"):
            def offered():
                status, state = self.request("/api/sessions")
                return status == 200 and (state.get("release") or {}).get("version") == "998.0.0"
            self.assertTrue(wait_for(offered, 30), f"the stand-in release wasn't offered before the {attempt} update")
            status, reply = self.request("/api/sessions/update", {})
            self.assertEqual(status, 200, reply)
            time.sleep(3)
            self.assertTrue(wait_for(self.answers, 60), f"the program didn't come back after the {attempt} update")
        self.assertIsNotNone(first.poll(), "the first copy should have handed over and exited")
        self.assertIsNone(host.poll(), "the session host should have kept running through both updates")
        # The second update saved beside the running first one; once it has exited, only the newest is kept.
        builds = lambda: sorted(n for n in os.listdir(installers) if n.startswith("claude-sessions-windows-"))  # noqa: E731
        self.assertTrue(wait_for(lambda: len(builds()) == 1, 60), builds())
        self.assertFalse(os.path.exists(os.path.join(local, "Programs", "Claude Sessions", "claude-sessions.exe")))
        self.assertTrue(os.path.exists(self.program), "a program not named like a release is left alone")
        host.kill()


@unittest.skipUnless(WINDOWS and os.environ.get("CLAUDE_SESSIONS_TEST_FOLDER"), "needs the built Windows programs")
class BuiltFolderTests(BuiltUpdateTests):
    """A one-file copy moves to the folder build of its own version, which then updates to a newer folder build."""

    def zipped(self, version):
        stem = f"claude-sessions-windows-{version}"
        source = os.environ["CLAUDE_SESSIONS_TEST_FOLDER"]
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, _, names in os.walk(source):
                for name in names:
                    path = os.path.join(root, name)
                    inside = os.path.relpath(path, source).replace(os.sep, "/")
                    if inside == "claude-sessions-windows.exe":
                        inside = stem + ".exe"
                    archive.write(path, f"{stem}/{inside}")
        return data.getvalue()

    def publish_folder(self, version):
        exe, folder = f"claude-sessions-windows-{version}.exe", f"claude-sessions-windows-{version}.zip"
        self.files["/asset"], self.files["/zip"] = self.body, self.zipped(version)
        self.files["/sums"] = (f"{hashlib.sha256(self.body).hexdigest()}  {exe}\n"
                               f"{hashlib.sha256(self.files['/zip']).hexdigest()}  {folder}\n").encode()
        self.files["/latest"] = json.dumps({"tag_name": "v" + version, "html_url": self.base + "/page", "assets": [
            {"name": exe, "browser_download_url": self.base + "/asset"},
            {"name": folder, "browser_download_url": self.base + "/zip"},
            {"name": "SHA256SUMS", "browser_download_url": self.base + "/sums"}]}).encode()

    def running_from(self, pattern):
        script = "Get-Process | Where-Object { $_.Path -and $_.Path -like $env:CS_PATTERN } | ForEach-Object { $_.Path }"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                env={**os.environ, "CS_PATTERN": pattern}, capture_output=True, text=True, timeout=60)
        return result.stdout.strip()

    def test_updates_and_restarts(self):
        version = subprocess.run([self.program, "--version"], capture_output=True, text=True, timeout=60).stdout.split()[-1]
        environment = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
        tools, installers, local = (os.path.join(self.folder, n) for n in ("tools", "installers", "local"))
        for folder in (tools, installers, local):
            os.makedirs(folder)
        with open(os.path.join(tools, "claude.cmd"), "w") as handle:
            handle.write("@echo []\n")
        environment.update(CLAUDE_CONFIG_DIR=self.config, CLAUDE_SESSIONS_RELEASES_URL=self.base + "/latest",
                           CLAUDE_SESSIONS_INSTALLERS_DIR=installers, LOCALAPPDATA=local,
                           PATH=tools + os.pathsep + environment.get("PATH", ""),
                           CLAUDE_SESSIONS_HOST_PORT=str(23000 + secrets.randbelow(2000)))
        environment.pop("CLAUDE_SESSIONS_NO_INSTALL", None)
        self.publish_folder(version)
        first = subprocess.Popen([self.program, "--web", "--no-open", "--port", str(self.port)], env=environment)
        self.assertTrue(wait_for(self.answers, 60), "the program didn't start")
        moved = os.path.join(installers, f"claude-sessions-windows-{version}", "*")
        self.assertTrue(wait_for(lambda: self.running_from(moved), 120), "the one-file copy didn't move to the folder build")
        self.assertTrue(wait_for(lambda: first.poll() is not None, 60), "the one-file copy should have handed over and exited")
        self.assertTrue(wait_for(self.answers, 60), "the folder build didn't take over")
        started = time.time()
        program = os.path.join(installers, f"claude-sessions-windows-{version}", f"claude-sessions-windows-{version}.exe")
        result = subprocess.run([program, "--statusline"], input=b"{}", env=environment, capture_output=True, timeout=60)
        print(f"\n  one status line run of the folder build took {time.time() - started:.2f} s", file=sys.stderr)
        self.assertEqual(result.returncode, 0)
        self.publish_folder("998.0.0")
        # It looks for releases as it starts, which was before this one, so it's restarted.
        status, reply = self.request("/api/sessions/restart-app", {})
        self.assertEqual(status, 200, reply)
        time.sleep(3)
        self.assertTrue(wait_for(self.answers, 60), "the folder build didn't come back after a restart")

        def offered():
            status, state = self.request("/api/sessions")
            return status == 200 and (state.get("release") or {}).get("version") == "998.0.0"
        self.assertTrue(wait_for(offered, 30), "the stand-in release wasn't offered")
        status, reply = self.request("/api/sessions/update", {})
        self.assertEqual(status, 200, reply)
        newest = os.path.join(installers, "claude-sessions-windows-998.0.0", "*")
        self.assertTrue(wait_for(lambda: self.running_from(newest), 120), "the newer folder build didn't start")
        self.assertTrue(wait_for(self.answers, 60), "the newer folder build didn't take over")
        # Once the earlier version has exited, only the newest folder is kept.
        left = lambda: sorted(n for n in os.listdir(installers) if n.startswith("claude-sessions-windows-"))  # noqa: E731
        self.assertTrue(wait_for(lambda: left() == ["claude-sessions-windows-998.0.0"], 90), left())


class PrinterControlTests(unittest.TestCase):
    """Printer controls become the messages Anycubic's own slicer sends, and silly values are refused."""

    def plan(self, control, value=None, state="idle", task=""):
        return app.printer_plan(control, value, state in ("printing", "paused"), state, task)

    def test_idle_heaters_fan_and_light(self):
        self.assertEqual(self.plan("nozzle", 210), ([("tempature", "set", {"type": 0, "target_hotbed_temp": 0, "target_nozzle_temp": 210})], None))
        self.assertEqual(self.plan("preheat", {"nozzle": 260, "bed": 100})[0],
                         [("tempature", "set", {"type": 1, "target_hotbed_temp": 100, "target_nozzle_temp": 0}),
                          ("tempature", "set", {"type": 0, "target_hotbed_temp": 0, "target_nozzle_temp": 260})])
        self.assertEqual([m[2] for m in self.plan("cool")[0]], [{"type": 1, "target_hotbed_temp": 0, "target_nozzle_temp": 0},
                                                                {"type": 0, "target_hotbed_temp": 0, "target_nozzle_temp": 0}])
        self.assertEqual(self.plan("fan", 50), ([("fan", "setSpeed", {"fan_speed_pct": 50})], None))
        self.assertEqual(self.plan("light", True), ([("light", "control", {"type": 3, "status": 1, "brightness": 100})], None))

    def test_while_printing(self):
        self.assertEqual(self.plan("bed", 95, "printing", "42"), ([("print", "update", {"taskid": "42", "settings": {"target_hotbed_temp": 95}})], None))
        self.assertEqual(self.plan("pause", state="printing", task="42"), ([("print", "pause", {"taskid": "42"})], None))
        self.assertEqual(self.plan("resume", state="paused", task="42"), ([("print", "resume", {"taskid": "42"})], None))
        self.assertEqual(self.plan("stop", state="paused", task="42"), ([("print", "stop", {"taskid": "42"})], None))
        self.assertTrue(self.plan("preheat", {"nozzle": 200, "bed": 60}, "printing", "42")[1])
        self.assertTrue(self.plan("cool", state="printing", task="42")[1])

    def test_refused(self):
        for control, value, state in [("nozzle", 301, "idle"), ("bed", 111, "idle"), ("bed", -1, "idle"), ("nozzle", True, "idle"),
                                      ("nozzle", "200", "idle"), ("fan", 101, "idle"), ("preheat", {"nozzle": 200}, "idle"),
                                      ("pause", None, "idle"), ("resume", None, "printing"), ("stop", None, "idle"), ("explode", None, "idle")]:
            plan, error = self.plan(control, value, state, "42")
            self.assertEqual(plan, [], (control, value))
            self.assertTrue(error, (control, value))

    def test_only_saved_printers(self):
        self.assertFalse(app.printer_control(None, {"host": "192.168.0.250", "control": "light", "value": True})[0])
        self.assertFalse(app.printer_control(None, {"host": "8.8.8.8", "control": "light", "value": True})[0])

    def test_status_keeps_the_light_and_reads_the_fan(self):
        camera = app.PrinterCamera("192.168.0.250")
        camera._take_report(b"x/info/report", json.dumps({"data": {"state": "free", "fan_speed_pct": 30, "temp": {"curr_nozzle_temp": 40}}}).encode())
        camera._take_report(b"x/light/report", json.dumps({"data": {"lights": [{"type": 3, "status": 1, "brightness": 100}]}}).encode())
        camera._take_report(b"x/info/report", json.dumps({"data": {"state": "free", "fan_speed_pct": 30}}).encode())
        self.assertEqual((camera.status["light"], camera.status["fan"]), (True, 30))
        camera._take_report(b"x/info/report", json.dumps({"data": {"state": "busy", "project": {"task_id": 77, "filename": "a.gcode"}}}).encode())
        self.assertEqual((camera.status["state"], camera.status["task"]), ("printing", "77"))


class WindowsVersionTests(unittest.TestCase):
    """Each Windows version runs as downloaded: settings follow it, and earlier versions go."""

    def test_commands_move_to_this_copy(self):
        settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"C:/old/claude-sessions-windows-0.1.1.exe" --hook'}]}],
                              "UserPromptSubmit": [{"hooks": [{"type": "command", "command": '"C:/old/claude-sessions.exe" --context'},
                                                              {"type": "command", "command": "other-tool --hook"}]}]},
                    "statusLine": {"type": "command", "command": "bash ~/.claude/statusline-command.sh"}}
        self.assertEqual(app.point_commands_here(settings), 2)
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], app.own_command_line("--hook"))
        self.assertEqual(settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], app.own_command_line("--context"))
        self.assertEqual(settings["hooks"]["UserPromptSubmit"][0]["hooks"][1]["command"], "other-tool --hook")
        self.assertEqual(settings["statusLine"]["command"], "bash ~/.claude/statusline-command.sh")
        self.assertEqual(app.point_commands_here(settings), 0)

    def test_finds_earlier_versions_only(self):
        folder = tempfile.mkdtemp(dir=CONFIG)
        local = os.path.join(folder, "local")
        os.makedirs(local)
        names = ["claude-sessions-windows-1.1.0.exe", "claude-sessions-windows-1.2.0.exe", "claude-sessions-windows-1.2.0-abc123.exe",
                 "claude-sessions-windows-1.3.0.exe", "claude-sessions-windows-1.2.0.exe.1a2b.download", "notes.txt", "other.exe"]
        for name in names:
            open(os.path.join(folder, name), "w").close()
        for name in ("claude-sessions.exe", "claude-sessions.exe.old", "readme.txt"):
            open(os.path.join(local, name), "w").close()
        saved = app.PROGRAM_PATH, app.APP_VERSION, app.WINDOWS_INSTALL_DIR, os.environ.get("CLAUDE_SESSIONS_INSTALLERS_DIR"), os.environ.get("LOCALAPPDATA")
        try:
            app.PROGRAM_PATH, app.APP_VERSION, app.WINDOWS_INSTALL_DIR = os.path.join(folder, names[2]), "1.2.0", local
            os.environ["CLAUDE_SESSIONS_INSTALLERS_DIR"], os.environ["LOCALAPPDATA"] = folder, folder
            found = sorted(os.path.basename(p) for p in app.older_windows_builds())
        finally:
            app.PROGRAM_PATH, app.APP_VERSION, app.WINDOWS_INSTALL_DIR = saved[:3]
            for key, value in (("CLAUDE_SESSIONS_INSTALLERS_DIR", saved[3]), ("LOCALAPPDATA", saved[4])):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(found, sorted(["claude-sessions-windows-1.1.0.exe", "claude-sessions-windows-1.2.0.exe",
                                        "claude-sessions-windows-1.2.0.exe.1a2b.download", "claude-sessions.exe", "claude-sessions.exe.old"]))


class WindowLifetimeTests(unittest.TestCase):
    """The app keeps serving after its browser process ends, while the window's page still calls."""

    def test_no_window_calling_means_stop(self):
        app.page_seen["at"] = 0.0
        started = time.monotonic()
        app.wait_while_page_open(handover=1, idle=0.5)
        self.assertLess(time.monotonic() - started, 4)

    def test_a_handed_over_window_keeps_it_going(self):
        app.page_seen["at"] = 0.0
        stop = threading.Event()

        def page():  # Starts calling a moment after the browser process ended, then the window closes.
            time.sleep(0.5)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                app.page_seen["at"] = time.monotonic()
                time.sleep(0.2)
            stop.set()

        threading.Thread(target=page, daemon=True).start()
        started = time.monotonic()
        app.wait_while_page_open(handover=2, idle=1)
        self.assertTrue(stop.is_set(), "it stopped while the page was still calling")
        self.assertLess(time.monotonic() - started, 7)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.settings = os.path.join(CONFIG, "settings.json")
        with open(self.settings, "w") as handle:
            json.dump({"model": "opus", "statusLine": {"type": "command", "command": "mine"},
                       "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other --thing"}]}]}}, handle)
        folder = tempfile.mkdtemp(dir=CONFIG)
        saved = app.STATUS_DIR, app.STATUS_LINE_AFTER_FILE
        app.STATUS_DIR, app.STATUS_LINE_AFTER_FILE = os.path.join(folder, "status"), os.path.join(folder, "statusline-after.json")
        self.addCleanup(lambda: (setattr(app, "STATUS_DIR", saved[0]), setattr(app, "STATUS_LINE_AFTER_FILE", saved[1])))

    def test_adds_hooks_once_and_keeps_everything_else(self):
        original = app.CONTEXT_HOOK
        app.CONTEXT_HOOK = True  # Left out on Windows; see the next test.
        try:
            self.assertEqual(app.run_setup(verbose=False), 0)
            self.assertEqual(app.run_setup(verbose=False), 0)
        finally:
            app.CONTEXT_HOOK = original
        with open(self.settings) as handle:
            settings = json.load(handle)
        self.assertEqual(settings["model"], "opus")
        # Their own status line runs after this app's, which saves the figures first.
        self.assertEqual(settings["statusLine"]["command"], app.own_command_line("--statusline"))
        self.assertEqual(app.status_line_after(), "mine")
        for event in app.HOOK_EVENTS:
            ours = [h for g in settings["hooks"][event] for h in g["hooks"] if "--hook" in h["command"]]
            self.assertEqual(len(ours), 1, event)
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "other --thing")
        context = [h for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "--context" in h["command"]]
        self.assertEqual(len(context), 1)
        self.assertNotIn("async", context[0])  # Its output has to reach Claude.

    def test_a_status_line_that_already_feeds_the_app_stays(self):
        os.makedirs(app.STATUS_DIR)
        with open(os.path.join(app.STATUS_DIR, "abc.json"), "w") as handle:
            json.dump({"context_percent": 5}, handle)  # Saved by the user's own script.
        self.assertEqual(app.run_setup(verbose=False), 0)
        with open(self.settings) as handle:
            self.assertEqual(json.load(handle)["statusLine"]["command"], "mine")
        self.assertIsNone(app.status_line_after())

    def test_windows_leaves_out_the_hook_claude_code_waits_for(self):
        original = app.CONTEXT_HOOK
        try:
            app.CONTEXT_HOOK = True  # As set up by an earlier version.
            self.assertEqual(app.run_setup(verbose=False), 0)
            with open(self.settings) as handle:
                settings = json.load(handle)
            settings["hooks"]["UserPromptSubmit"][-1]["hooks"].append({"type": "command", "command": "someone --else"})
            with open(self.settings, "w") as handle:
                json.dump(settings, handle)
            app.CONTEXT_HOOK = False
            self.assertEqual(app.run_setup(verbose=False), 0)
            self.assertEqual(app.run_setup(verbose=False), 0)
        finally:
            app.CONTEXT_HOOK = original
        with open(self.settings) as handle:
            commands = [h["command"] for g in json.load(handle)["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertFalse(any("--context" in c for c in commands))
        self.assertIn("someone --else", commands)  # Other hooks in the same group stay.
        self.assertEqual(sum("--hook" in c for c in commands), 1)


class FolderBuildTests(unittest.TestCase):
    """The one-folder Windows build: unpacked from its zip, and earlier versions tidied away by folder."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="claude-sessions-folder-")
        os.environ["CLAUDE_SESSIONS_INSTALLERS_DIR"] = self.folder

    def tearDown(self):
        os.environ.pop("CLAUDE_SESSIONS_INSTALLERS_DIR", None)
        shutil.rmtree(self.folder, ignore_errors=True)

    def zipped(self, stem, extra=()):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(f"{stem}/{stem}.exe", b"program")
            archive.writestr(f"{stem}/_internal/base_library.zip", b"library")
            for name in extra:
                archive.writestr(name, b"x")
        return data.getvalue()

    def test_unpacks_beside_the_same_version(self):
        stem = "claude-sessions-windows-1.2.0"
        program = app.stage_folder_build(self.zipped(stem), stem + ".zip")
        self.assertEqual(program, os.path.join(self.folder, stem, stem + ".exe"))
        self.assertTrue(os.path.exists(os.path.join(self.folder, stem, "_internal", "base_library.zip")))
        again = app.stage_folder_build(self.zipped(stem), stem + ".zip")
        self.assertNotEqual(os.path.dirname(again), os.path.dirname(program))
        self.assertTrue(os.path.exists(again))
        self.assertEqual(sorted(os.listdir(self.folder))[0], stem)  # No unpacking folder left over.
        self.assertEqual(len(os.listdir(self.folder)), 2)

    def test_refuses_unexpected_contents(self):
        stem = "claude-sessions-windows-1.2.0"
        for data, name in ((self.zipped(stem, ["elsewhere.txt"]), stem + ".zip"), (self.zipped("other"), "other.zip"),
                           (self.zipped(stem), "claude-sessions-windows-1.3.0.zip")):
            with self.assertRaises(ValueError):
                app.stage_folder_build(data, name)
        self.assertEqual(os.listdir(self.folder), [])

    def test_prefers_the_folder_build_on_windows(self):
        assets = {n: "http://127.0.0.1/" + n for n in ("claude-sessions-windows-1.2.0.exe", "claude-sessions-windows-1.2.0.zip",
                                                         "claude-monitor", "SHA256SUMS")}
        listing = {"tag_name": "v1.2.0", "assets": [{"name": n, "browser_download_url": u} for n, u in assets.items()]}
        saved = app.fetch
        app.fetch = lambda url, limit, timeout=30: json.dumps(listing).encode()
        try:
            releases = app.Releases()
            releases.check()
        finally:
            app.fetch = saved
        self.assertEqual(releases.latest["asset"], "claude-sessions-windows-1.2.0.zip" if WINDOWS else "claude-monitor")

    def test_folder_build_finds_earlier_versions(self):
        names = {"claude-sessions-windows-1.1.0": "dir", "claude-sessions-windows-1.2.0": "dir", "claude-sessions-windows-1.3.0": "dir",
                 "claude-sessions-windows-1.2.0-abc123": "mine", "claude-sessions-windows-1.2.0.1a2b.download": "dir",
                 "claude-sessions-windows-1.1.0.exe": "file", "claude-sessions-windows-1.2.0.exe": "file", "other": "dir"}
        for name, kind in names.items():
            if kind == "file":
                open(os.path.join(self.folder, name), "w").close()
            else:
                os.makedirs(os.path.join(self.folder, name))
        mine = os.path.join(self.folder, "claude-sessions-windows-1.2.0-abc123", "claude-sessions-windows-1.2.0.exe")
        open(mine, "w").close()
        saved = app.PROGRAM_PATH, app.APP_VERSION, app.ONE_FOLDER
        try:
            app.PROGRAM_PATH, app.APP_VERSION, app.ONE_FOLDER = mine, "1.2.0", True
            found = sorted(os.path.basename(p) for p in app.older_windows_builds())
            app.ONE_FOLDER = False  # A one-file copy leaves folders alone.
            found_by_file = sorted(os.path.basename(p) for p in app.older_windows_builds())
        finally:
            app.PROGRAM_PATH, app.APP_VERSION, app.ONE_FOLDER = saved
        self.assertEqual(found, ["claude-sessions-windows-1.1.0", "claude-sessions-windows-1.1.0.exe", "claude-sessions-windows-1.2.0",
                                 "claude-sessions-windows-1.2.0.1a2b.download", "claude-sessions-windows-1.2.0.exe"])
        self.assertNotIn("claude-sessions-windows-1.1.0", found_by_file)


class SessionFileTests(unittest.TestCase):
    """Sessions are listed from Claude Code's own files, asking `claude agents` only when they come or go."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="claude-sessions-files-")
        self.saved = app.SESSIONS_DIR, app.subprocess.run, app.hosted_panes, app.Transcript.poll
        app.SESSIONS_DIR = self.folder
        app.hosted_panes = lambda: {}
        app.Transcript.poll = lambda transcript: None
        self.agents = []
        self.calls = 0

        def run(command, *args, **kwargs):
            self.calls += 1
            return subprocess.CompletedProcess(command, 0, json.dumps(self.agents), "")

        app.subprocess.run = run

    def tearDown(self):
        app.SESSIONS_DIR, app.subprocess.run, app.hosted_panes, app.Transcript.poll = self.saved
        shutil.rmtree(self.folder, ignore_errors=True)

    def write(self, pid, **entry):
        with open(os.path.join(self.folder, f"{pid}.json"), "w") as handle:
            json.dump({"pid": pid, **entry}, handle)

    def test_status_from_files(self):
        self.agents = [{"pid": 11, "sessionId": "aaa", "status": "idle", "name": "one", "startedAt": 1, "cwd": "/"}]
        self.write(11, sessionId="aaa", status="idle", name="one")
        monitor = app.Monitor()
        monitor.refresh()
        self.assertEqual(self.calls, 1)
        self.write(11, sessionId="aaa", status="shell", name="renamed")
        monitor.refresh()
        self.assertEqual(self.calls, 1)
        self.assertEqual([(s["status"], s["name"]) for s in monitor.sessions], [("busy", "renamed")])
        self.write(11, sessionId="bbb", status="waiting")  # After /clear.
        monitor.refresh()
        self.assertEqual(self.calls, 1)
        self.assertEqual([(s["sessionId"], s["status"]) for s in monitor.sessions], [("bbb", "waiting")])

    def test_asks_again_when_sessions_change(self):
        self.write(11, sessionId="aaa", status="idle")
        monitor = app.Monitor()
        monitor.refresh()
        self.write(12, sessionId="ccc", status="busy")
        self.agents = [{"pid": 12, "sessionId": "ccc", "status": "busy", "startedAt": 2, "cwd": "/"}]
        monitor.refresh()
        self.assertEqual(self.calls, 2)
        self.write(12, sessionId="ccc", status="something new")
        monitor.refresh()
        self.assertEqual(self.calls, 3)
        monitor.agents_at -= app.AGENTS_RECHECK_SECONDS + 1
        self.write(12, sessionId="ccc", status="idle")
        monitor.refresh()
        self.assertEqual(self.calls, 4)
        shutil.rmtree(self.folder)
        monitor.refresh()
        self.assertEqual(self.calls, 5)


class NewSessionModelTests(unittest.TestCase):
    def setUp(self):
        saved = app.KNOWN_FILE, app.launch_session
        app.KNOWN_FILE = os.path.join(tempfile.mkdtemp(dir=CONFIG), "known.json")
        self.addCleanup(lambda: (setattr(app, "KNOWN_FILE", saved[0]), setattr(app, "launch_session", saved[1])))
        self.given = []
        app.launch_session = lambda monitor, session_id, command, cwd, trust: (self.given.append((session_id, command)), (True, "Started."))[1]
        self.monitor = types.SimpleNamespace(lock=threading.RLock(), current=lambda sid: None)

    def start(self, **fields):
        self.given.clear()
        result = app.start_session(self.monitor, {"cwd": CONFIG, "prompt": "hello", **fields})
        return result, self.given[0][1] if self.given else None

    def test_resume_uses_the_model_it_was_started_with(self):
        self.start(model="sonnet")
        session_id = self.given[0][0]
        known = app.read_known()
        known["sessions"][session_id].update(cwd=CONFIG, mode="default", path=__file__)
        app.write_known(known)
        app.resume_session(self.monitor, {"id": session_id})
        self.assertIn("--model=sonnet", self.given[-1][1])
        self.assertEqual(self.given[-1][1][:2], ["claude", "--resume"])

    def test_chrome_is_passed_on_and_kept_for_resume(self):
        self.assertNotIn("--chrome", self.start()[1])
        self.assertNotIn("--chrome", self.start(chrome="yes")[1])  # Only a real true counts.
        self.assertIn("--chrome", self.start(chrome=True)[1])
        session_id = self.given[0][0]
        monitor = app.Monitor()
        monitor.transcripts[session_id] = types.SimpleNamespace(path=__file__, permission_mode="default", resume_model=None)
        monitor._update_known([{"id": session_id, "name": "t", "cwd": CONFIG, "title": ""}])  # Must not drop it.
        app.resume_session(self.monitor, {"id": session_id})
        self.assertIn("--chrome", self.given[-1][1])
        self.assertEqual(self.given[-1][1][:2], ["claude", "--resume"])

    def test_model_picked_with_slash_model(self):
        self.assertEqual([app.model_alias(n) for n in ("Opus 5.5", "Sonnet 5.5 (1M context)", "Default (recommended)", "Opus Plan Mode", "Something new")],
                         ["opus", "sonnet[1m]", "", "opusplan", None])
        self.assertEqual(app.resume_model_flag("sonnet[1m]"), ["--model=sonnet[1m]"])
        self.assertEqual(app.resume_model_flag("--evil"), [])
        self.assertEqual(app.resume_model_flag(""), [])
        transcript = app.Transcript("abc")
        transcript._ingest({"type": "system", "content": "Set model to `Haiku 5.5`"})
        transcript._ingest({"type": "system", "content": "Switched to Sonnet 5.5 due to high demand."})  # Not the user's choice.
        self.assertEqual(transcript.resume_model, "haiku")

    def test_opus_plan_is_named(self):
        self.assertEqual(app.model_label("Opus 5.5", "opusplan"), "Opus Plan")
        self.assertEqual(app.model_label("Sonnet 5.5", "opusplan"), "Opus Plan")
        self.assertEqual(app.model_label("Sonnet 5.5", "sonnet"), "Sonnet 5.5")
        self.assertEqual(app.model_label("Opus 5.5", ""), "Opus 5.5")
        self.start(model="opusplan")
        session_id = self.given[0][0]
        monitor = app.Monitor()
        monitor.transcripts[session_id] = types.SimpleNamespace(path=__file__, permission_mode="plan", resume_model=None)
        monitor._update_known([{"id": session_id, "name": "test", "cwd": CONFIG, "title": ""}])
        self.assertEqual(monitor.chosen_models[session_id], "opusplan")
        monitor.transcripts[session_id].resume_model = "sonnet"  # Changed with /model.
        monitor._update_known([{"id": session_id, "name": "test", "cwd": CONFIG, "title": ""}])
        self.assertEqual(monitor.chosen_models[session_id], "sonnet")

    def test_the_chosen_model_is_passed_on(self):
        self.assertIn("--model=sonnet", self.start(model="sonnet")[1])
        self.assertIn("--model=opusplan", self.start(model="opusplan")[1])
        self.assertFalse(any(a.startswith("--model") for a in self.start()[1]))  # Claude Code's own setting.
        self.assertEqual(self.start(model="--dangerously-skip-permissions"), ((False, "Model not available here: --dangerously-skip-permissions"), None))
        command = self.start(model="haiku")[1]
        self.assertLess(command.index("--model=haiku"), command.index("--"))  # Before the prompt, not part of it.


class MessageTimeTests(unittest.TestCase):
    def test_messages_say_when_and_tools_how_long(self):
        path = os.path.join(tempfile.mkdtemp(dir=CONFIG), "t.jsonl")
        with open(path, "w") as handle:
            for entry in (
                {"type": "user", "uuid": "1", "timestamp": "2026-11-10T10:00:00.000Z", "message": {"role": "user", "content": "check it"}},
                {"type": "assistant", "uuid": "2", "timestamp": "2026-11-10T10:00:05.000Z", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Checking"}, {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "sleep 12"}}]}},
                {"type": "user", "uuid": "3", "timestamp": "2026-11-10T10:00:17.500Z", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "done"}]}},
            ):
                handle.write(json.dumps(entry) + "\n")
        history = app.HistoryReader(path)
        history.poll()
        start = datetime.datetime(2026, 11, 10, 10, tzinfo=datetime.timezone.utc).timestamp()
        you, claude, tool = history.messages
        self.assertEqual((you["at"], claude["at"]), (start, start + 5))
        self.assertEqual((tool["at"], tool["done"]), (start + 5, start + 17.5))


class ToolPictureTests(unittest.TestCase):
    def test_a_picture_claude_opened_is_served(self):
        path = os.path.join(tempfile.mkdtemp(dir=CONFIG), "t.jsonl")
        jpeg = b"\xff\xd8\xff\xe0 a camera frame"
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "1", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/tmp/frame.jpg"}}]}}) + "\n")
            handle.write(json.dumps({"type": "user", "uuid": "2", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}]}}) + "\n")
        history = app.HistoryReader(path)
        history.poll()
        tool = next(m for m in history.messages if m.get("role") == "tool")
        self.assertEqual(tool["images"], 1)
        self.assertEqual(history.tool_image("toolu_1", 0), ("image/jpeg", jpeg))
        self.assertIsNone(history.tool_image("toolu_1", 1))
        self.assertIsNone(history.tool_image("toolu_9", 0))


class StatusLineTests(unittest.TestCase):
    def test_saves_the_figures_into_a_new_folder(self):
        folder = tempfile.mkdtemp(dir=CONFIG)
        saved = app.STATUS_DIR, sys.stdin, sys.stdout
        app.STATUS_DIR = os.path.join(folder, "status")
        sys.stdin, sys.stdout = io.StringIO(json.dumps({"session_id": "abc-123", "context_window": {"used_percentage": 42}})), io.StringIO()
        try:
            app.run_statusline()
            printed = sys.stdout.getvalue()
        finally:
            app.STATUS_DIR, sys.stdin, sys.stdout = saved
        self.assertIn("Ctx 42%", printed)
        with open(os.path.join(folder, "status", "abc-123.json")) as handle:
            self.assertEqual(json.load(handle)["context_percent"], 42)

    def test_runs_the_users_own_status_line_after_saving(self):
        folder = tempfile.mkdtemp(dir=CONFIG)
        saved = app.STATUS_DIR, app.STATUS_LINE_AFTER_FILE, sys.stdin, sys.stdout
        app.STATUS_DIR, app.STATUS_LINE_AFTER_FILE = os.path.join(folder, "status"), os.path.join(folder, "after.json")
        app.write_json_file(app.STATUS_LINE_AFTER_FILE, {"command": "cat; echo ' mine'"})  # Through Git Bash on Windows, as Claude Code does.
        given = json.dumps({"session_id": "abc-123", "context_window": {"used_percentage": 42}, "cwd": "C:/Zoë"}, ensure_ascii=False)
        sys.stdin, sys.stdout = io.StringIO(given), io.StringIO()
        try:
            code = app.run_statusline()
            printed = sys.stdout.getvalue()
        finally:
            app.STATUS_DIR, app.STATUS_LINE_AFTER_FILE, sys.stdin, sys.stdout = saved
        self.assertEqual(code, 0)
        self.assertEqual(printed.strip(), given + " mine")  # Theirs, given the same input, and not this app's.
        with open(os.path.join(folder, "status", "abc-123.json")) as handle:
            figures = json.load(handle)
        self.assertEqual((figures["context_percent"], figures["by"]), (42, app.STATUS_BY))


class RequestTests(unittest.TestCase):
    """Requests Claude's shell commands send, to this computer or anywhere else."""

    def test_finds_requests_anywhere(self):
        self.assertEqual(app.shell_requests('curl -s "https://api.example.com/v1/items?id=3" | jq .'), [("GET", "https://api.example.com/v1/items?id=3")])
        self.assertEqual(app.shell_requests("curl -X POST http://localhost:8080/api -d '{}'"), [("POST", "http://localhost:8080/api")])
        self.assertEqual(app.shell_requests("curl localhost:3000/health && echo ok"), [("GET", "localhost:3000/health")])
        self.assertEqual(app.shell_requests("curl http://[::1]:9000/x"), [("GET", "http://[::1]:9000/x")])

    def test_leaves_out_what_isnt_sent(self):
        self.assertEqual(app.shell_requests("cat <<EOF > f\ncurl https://no.example\nEOF"), [])
        self.assertEqual(app.shell_requests("curl ${BASE}/x"), [])
        self.assertEqual(app.shell_requests("git clone https://github.com/x/y"), [])
        self.assertEqual(app.shell_requests("echo http://example.com"), [])

    def test_says_where_each_went(self):
        self.assertEqual(app.request_place(["http://127.0.0.1:8766/x", "localhost:3000"]), "computer")
        self.assertEqual(app.request_place(["http://10.0.2.2:8000/api"]), "computer")  # The emulator's name for this computer.
        self.assertEqual(app.request_place(["http://192.168.0.60:8090/info", "http://localhost:1"]), "network")
        self.assertEqual(app.request_place(["http://printer.local/x"]), "network")
        self.assertEqual(app.request_place(["https://api.example.com", "http://localhost:1"]), "internet")
        self.assertEqual(app.request_place(["https://localhost.example.com/"]), "internet")


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

    def test_files_kept_under_the_first_id_after_clear(self):
        first = "32fe1e95-7071-43e6-ae8b-ff8c96e5a8b4"
        entries = self.started("u1", "b1", "Run the evaluation")
        entries[1]["message"]["content"][0]["content"] += f" Output is being written to: /tmp/claude-1000/x/{first}/tasks/b1.output"
        saved = app.open_background_tasks
        try:
            app.open_background_tasks = lambda: {first: {"b1"}}
            for whole in (True, False):
                t = self.transcript(*entries)
                if not whole:  # Only the end of a long transcript was read.
                    t.read_whole, t.task_folders = False, {"session"}
                state = app.Monitor.background_state(None, {"sessionId": "session", "startedAt": None}, t)
                self.assertEqual([x["id"] for x in state], ["b1"])
        finally:
            app.open_background_tasks = saved


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

    def test_write_notes_can_go_now_and_the_rest_follows_next(self):
        app.save_and_refresh(self.monitor, {"id": "s1"})
        steps = [i for i in self.queued() if i.get("refresh")]
        for later in steps[1:]:  # Clearing the chat waits for the notes to be confirmed.
            self.assertFalse(app.send_queued_now(self.monitor, {"id": "s1", "item": later["id"]})[0])
        self.assertEqual(self.typed, [])
        ok, message = app.send_queued_now(self.monitor, {"id": "s1", "item": steps[0]["id"]})
        self.assertTrue(ok, message)
        self.assertEqual(self.typed[-3:], ["Enter", "C-x", "C-s"])
        self.assertEqual([i["id"] for i in self.queued()], [steps[1]["id"], steps[2]["id"], "a", "b"])

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

    def test_shell_status_counts_as_free(self):
        # Claude Code says "shell" once its turn is over (or interrupted) while background shells run.
        # An interrupt fires no Stop hook, so the file's word is all there is.
        monitor = app.Monitor()
        files = {7: {"sessionId": "s9", "status": "shell", "name": "x"}}
        listed = [{"pid": 7, "sessionId": "s9", "status": "busy", "name": "x", "cwd": self.folder}]
        with mock.patch.object(app, "read_session_files", lambda: files), \
                mock.patch.object(monitor, "_from_session_files", lambda found: [dict(s) for s in listed]), \
                mock.patch.object(app, "hosted_panes", lambda: {}):
            monitor.refresh()
            state = monitor.session_state(monitor.sessions[0])
            self.assertEqual((state["status"], state["heldBusy"]), ("idle", True))
            files[7]["status"] = "busy"
            monitor.refresh()
            self.assertEqual(monitor.session_state(monitor.sessions[0])["status"], "busy")

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

    def test_times_each_step_of_sending(self):
        app.hosted_session = lambda monitor, request: {"id": "s1", "tmux": "s1", "status": "idle"}
        monitor = type("M", (), {"human_messages": lambda self, sid: 0})()
        app.send_timing.steps = []
        try:
            ok, message = app.send_reply(monitor, {"id": "s1", "text": "hello"})
            steps = [name for name, _ in app.send_timing.steps]
        finally:
            app.send_timing.steps = None
        self.assertEqual((ok, message), (True, "Sent."))
        self.assertEqual(steps, ["waiting for the queue", "waiting for the queue", "typing", "pressing Enter"])

    def test_queue_lock_is_handed_over_quickly(self):
        held = threading.Event()

        def hold():
            with app.queue_locked():
                held.set()
                time.sleep(0.3)

        threading.Thread(target=hold).start()
        held.wait(5)
        started = time.monotonic()
        with app.queue_locked():
            waited = time.monotonic() - started
        self.assertGreater(waited, 0.1)  # It did wait for the other thread...
        self.assertLess(waited, 0.6)  # ...but not in whole seconds, as Windows' own retries would.

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
        with contextlib.suppress(OSError):
            os.remove(app.CAMERAS_FILE)

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
            self.assertEqual(app.printer_settings(None, {"action": "add", "host": "192.168.0.66"}), (True, {"printers": ["192.168.0.66"], "shares": {}, "cameras": []}))
            self.assertEqual(app.read_printers(), ["192.168.0.66"])
            self.assertEqual(app.printer_settings(None, {"action": "remove", "host": "192.168.0.66"}), (True, {"printers": [], "shares": {}, "cameras": []}))
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


class CameraTests(unittest.TestCase):
    """Other cameras: rtsp:// addresses on this network, shown beside printing sessions like a printer's camera."""

    URL = "rtsp://admin:secret@192.168.0.82:554/1"

    def setUp(self):
        with contextlib.suppress(OSError):
            os.remove(app.CAMERAS_FILE)
        original = app.socket.create_connection
        self.addCleanup(setattr, app.socket, "create_connection", original)
        self.answering = True
        def connect(address, timeout=None):
            if not self.answering:
                raise OSError("refused")
            return contextlib.nullcontext()
        app.socket.create_connection = connect

    def test_only_rtsp_on_this_network(self):
        for good in (self.URL, " rtsp://192.168.0.82/live ", "rtsp://10.0.0.5:8554/stream?channel=1"):
            self.assertEqual(app.camera_url(good), good.strip())
        for bad in ("http://192.168.0.82/1", "rtsp://8.8.8.8/1", "rtsp://127.0.0.1/1", "rtsp://cam.local/1",
                    "rtsp://192.168.0.82:99999/1", "rtsp://192.168.0.82/a b", "-i rtsp://192.168.0.82/1", "file:///etc/passwd", "", None):
            self.assertIsNone(app.camera_url(bad), bad)

    def test_password_never_shown(self):
        self.assertEqual(app.camera_shown_url(self.URL), "rtsp://admin:\u2022\u2022\u2022@192.168.0.82:554/1")
        self.assertEqual(app.camera_shown_url("rtsp://192.168.0.82/1"), "rtsp://192.168.0.82/1")

    def test_settings(self):
        self.assertFalse(app.camera_settings(None, {"action": "add", "url": "rtsp://8.8.8.8/1"})[0])
        self.answering = False
        self.assertFalse(app.camera_settings(None, {"action": "add", "url": self.URL})[0])
        self.answering = True
        ok, view = app.camera_settings(None, {"action": "add", "url": self.URL, "name": "Enclosure"})
        self.assertTrue(ok)
        [camera] = view["cameras"]
        self.assertEqual((camera["name"], camera["url"], camera["crop"]), ("Enclosure", "rtsp://admin:\u2022\u2022\u2022@192.168.0.82:554/1", None))
        self.assertNotIn("secret", json.dumps(view))
        self.assertFalse(app.camera_settings(None, {"action": "add", "url": self.URL})[0])  # Already there.
        self.assertEqual(app.read_cameras()[0]["url"], self.URL)
        if not WINDOWS:
            self.assertEqual(os.stat(app.CAMERAS_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(app.camera_settings(None, {"action": "remove", "id": camera["id"]})[1]["cameras"], [])

    def test_zoom(self):
        self.assertIsNone(app.camera_crop(None))
        self.assertIsNone(app.camera_crop([0, 0, 1, 1]))  # The whole picture is no zoom.
        for bad in ([0.5, 0.5, 0.01, 0.5], [-0.1, 0, 0.5, 0.5], [0.8, 0, 0.5, 0.5], ["x", 0, 1, 1], [0, 0, 1], "abc"):
            self.assertIsNone(app.camera_crop(bad), bad)
        self.assertEqual(app.camera_crop([0.21354, 0.1157, 0.5208, 0.6944]), [0.2135, 0.1157, 0.5208, 0.6944])
        self.assertEqual(app.camera_filter(None), "fps=10")
        self.assertEqual(app.camera_filter([0.25, 0.1, 0.5, 0.75]),
                         "crop=trunc(iw*0.5/2)*2:trunc(ih*0.75/2)*2:trunc(iw*0.25):trunc(ih*0.1),fps=10")
        [camera] = app.camera_settings(None, {"action": "add", "url": self.URL, "name": "Enclosure"})[1]["cameras"]
        self.assertFalse(app.camera_settings(None, {"action": "zoom", "id": camera["id"], "crop": [0, 0, 0.01, 0.01]})[0])
        self.assertEqual(app.camera_settings(None, {"action": "zoom", "id": camera["id"], "crop": [0.25, 0.1, 0.5, 0.75]})[1]["cameras"][0]["crop"], [0.25, 0.1, 0.5, 0.75])
        feed = app.network_camera(camera["id"])
        self.assertEqual(feed.crop, [0.25, 0.1, 0.5, 0.75])
        app.camera_settings(None, {"action": "zoom", "id": camera["id"], "crop": None})
        self.assertIsNot(app.network_camera(camera["id"]), feed)  # A new zoom starts a new stream.
        self.assertEqual(feed.wanted_at, 0)
        self.assertIsNone(app.read_cameras()[0]["crop"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_zoom_filter_runs_in_ffmpeg(self):
        done = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=21",
                               "-frames:v", "1", "-vf", app.camera_filter([0.2135, 0.1157, 0.5208, 0.6944]), "-c:v", "mjpeg", "-f", "image2pipe", "-"],
                              capture_output=True, timeout=30)
        self.assertEqual(done.stdout[:2], b"\xff\xd8", done.stderr)

    def test_listed_and_linked_like_a_printer_camera(self):
        app.camera_settings(None, {"action": "add", "url": self.URL, "name": "Enclosure"})
        app.camera_devices()  # Starts the background check.
        [device] = wait_for(lambda: [d for d in app.camera_devices() if d["state"] != "checking"])
        self.assertEqual((device["kind"], device["label"]), ("camera", "Enclosure"))
        self.assertIn(device["state"], ("device", "no ffmpeg"))
        self.assertIsInstance(app.network_camera(device["serial"][len("camera-"):]), app.NetworkCamera)
        self.assertIsNone(app.network_camera("nothing"))
        monitor = app.Monitor()
        monitor.devices = [device]
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
        # A rejected question's result is plain text, not a dict.
        self.assertIn("note", app.given_answers(asked, "Error: The user doesn't want to proceed", "rejected", True))


class TickBoxAnswerTests(unittest.TestCase):
    """Typed answers on tick-box questions: typed into "Type something", then Next or Submit is pressed."""

    def screen(self, here, typed, ticked, step="Next"):
        rows = [("1", " ", "Apple"), ("2", " ", "Pear"), ("3", "✔" if ticked else " ", typed or "Type something")]
        lines = ["←  ☐ Fruit  ☐ Veg  ✔ Submit  →", "", "Which fruits?", ""]
        for n, (key, tick, label) in enumerate(rows):
            lines.append(("❯ " if here == n else "  ") + f"{key}. [{tick}] {label}")
            if n < 2:
                lines.append("         " + label)
        lines.append(("❯    " if here == 3 else "     ") + step)
        lines += ["─" * 40, "  4. Chat about this", "", "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"]
        return "\n".join(lines)

    def test_highlight_on_next_still_reads_the_menu(self):
        options = app.menu_options(self.screen(3, "all of them", True))
        self.assertEqual([o["label"] for o in options], ["Apple", "Pear", "all of them", "Next", "Chat about this"])
        self.assertTrue(options[3]["highlighted"])
        self.assertEqual(app.menu_options(self.screen(3, "", True, "Submit"))[3]["label"], "Submit")

    def test_typed_answer_moves_on_to_next(self):
        state = {"here": 0, "typed": "", "ticked": True, "pressed": []}  # "Type something" ticked but empty.

        def keys(target, *pressed):
            for key in pressed:
                state["pressed"].append(key)
                if key == "Down":
                    state["here"] = min(state["here"] + 1, 3)
                elif key == "Up":
                    state["here"] = max(state["here"] - 1, 0)
                elif key == "Enter":
                    state["entered_on"] = state["here"]
                else:
                    self.fail(f"pressed {key!r}, which would tick or untick an answer")
            return True

        def text(target, typed):
            state["typed"] += typed
            state["ticked"] = True
            return True

        screen = lambda target: self.screen(state["here"], state["typed"], state["ticked"])
        with mock.patch.object(app, "session_screen", screen), mock.patch.object(app, "send_keys", keys), \
                mock.patch.object(app, "send_text", text), mock.patch.object(app.time, "sleep"):
            answer = next(o for o in app.menu_options(screen(None)) if o["label"] == "Type something")
            self.assertTrue(app.choose_answer("t", answer, "all of them"))
        self.assertEqual((state["typed"], state["ticked"], state["entered_on"]), ("all of them", True, 3))


class SideQuestionTests(unittest.TestCase):
    """/btw answers are read off Claude Code's screen, since it saves them nowhere."""

    RULE = "▔" * 60
    ANSWERING = "\n".join(["● Hi there", "", RULE, "", "    /btw what is 2+2?", "    /btw capital of Peru?", "",
                            "      ✢ Answering…", "", "    ⇧←/→ to browse · x to clear history · Esc to close"])
    DONE = "\n".join(["● Hi there", "", RULE, "", "    /btw what is 2+2?", "    /btw capital of Peru?", "",
                       "      The capital of Peru is Lima.", "        - on the coast", "",
                       "    ⇧←/→ to browse · c to copy · f to fork · x to clear history · Esc to close", ""])

    def test_panel(self):
        self.assertEqual(app.btw_panel(self.ANSWERING), {"question": "capital of Peru?", "answer": "", "done": False})
        self.assertEqual(app.btw_panel(self.DONE),
                         {"question": "capital of Peru?", "answer": "The capital of Peru is Lima.\n  - on the coast", "done": True})
        self.assertIsNone(app.btw_panel("❯ \n" + "─" * 40 + "\n  Haiku 5.5 | Ctx 4%"))
        self.assertTrue(app.same_question("list the 30 largest countries by area", "list the 30 largest countries"))
        self.assertFalse(app.same_question("capital of Peru?", "capital"))

    def test_answer_is_read_then_panel_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            screens = [self.DONE]
            pressed = []
            with mock.patch.object(app, "BTW_FILE", os.path.join(folder, "btw.json")), \
                    mock.patch.object(app, "session_screen", lambda target: screens[-1]), \
                    mock.patch.object(app, "whole_screen", lambda target: screens[-1]), \
                    mock.patch.object(app, "send_keys", lambda target, *keys: pressed.extend(keys) or True), \
                    mock.patch.object(app, "hosted_sessions", lambda: {"t"}):
                app.note_btw({"id": "s1", "tmux": "t"}, "/btw  capital of\nPeru?")
                app.note_btw({"id": "s1", "tmux": "t"}, "not a side question")
                self.assertEqual([e["state"] for e in app.btw_for("s1")], ["answering"])
                app.watch_btw(None)
                entry = app.btw_for("s1")[0]
                self.assertEqual((entry["state"], entry["answer"].splitlines()[0]), ("answered", "The capital of Peru is Lima."))
                self.assertEqual(pressed, ["Escape"])
                app.move_btw([("s1", "s2")])
                self.assertEqual(len(app.btw_for("s2")), 1)


class CommandPanelTests(unittest.TestCase):
    """Panels opened by slash commands, worked from the app, and the context check."""

    RULE = "▔" * 60
    MODEL = "\n".join(["● Done", "", RULE, "   Select model", "   Switch between Claude models.", "",
                        "     1.  Default (recommended)  Sonnet 5.5 · Efficient", "     2.  Opus 5.5               For complex work",
                        "   ❯ 3.  Haiku 5.5 ✔            Fastest", "     ▸   More models", "",
                        "   Enter to set as default · s to use this session only · Esc to cancel"])
    RESUME = "\n".join([RULE, "   Resume session", "   ❯ Fruit selection question", "     2 hours ago · HEAD · 335.2KB",
                         "     Ctrl+A to show all projects · Ctrl+B to only show current branch · Space to preview · Ctrl+R to rename · Type to",
                         "     search · Esc to cancel"])
    IDLE = "\n".join(["● Done", "", "─" * 60, "❯ ", "─" * 60, "  Haiku 5.5 | Ctx 4%"])

    def test_panel_and_its_keys(self):
        panel = app.command_panel(self.MODEL)
        self.assertEqual(panel["title"], "Select model")
        self.assertEqual([k["keys"] for k in panel["keys"]], [["Enter"], ["s"], ["Escape"]])
        resume = app.command_panel(self.RESUME)
        self.assertEqual([k["keys"] for k in resume["keys"]], [["C-a"], ["C-b"], ["Space"], ["C-r"], ["Escape"]])
        self.assertIsNone(app.command_panel(self.IDLE))
        self.assertEqual(app.hint_keys("⇧←/→ to browse · ↑/↓ to scroll · Type to filter")[0]["keys"], ["S-Left", "S-Right"])
        self.assertEqual(app.hint_keys("↑/↓ to scroll")[0]["keys"], ["Up", "Down"])

    def test_only_dialog_waits_count(self):
        self.assertTrue(app.panel_allowed({"status": "waiting", "waitingFor": "dialog open"}))
        self.assertFalse(app.panel_allowed({"status": "waiting", "waitingFor": "input needed"}))
        self.assertTrue(app.panel_allowed({"status": "idle"}))

    def test_keys_are_checked_against_the_panel(self):
        pressed, typed = [], []
        session = {"status": "waiting", "waitingFor": "dialog open", "tmux": "t"}
        with mock.patch.object(app, "session_screen", lambda target: self.MODEL), \
                mock.patch.object(app, "send_keys", lambda target, *keys: pressed.extend(keys) or True), \
                mock.patch.object(app, "send_text", lambda target, text: typed.append(text) or True):
            self.assertEqual(app.panel_action(session, "panelkey", {"key": "s"}), (True, "Sent."))
            self.assertFalse(app.panel_action(session, "panelkey", {"key": "C-c"})[0])
            self.assertFalse(app.panel_action({**session, "waitingFor": "input needed"}, "panelkey", {"key": "Enter"})[0])
        self.assertEqual((pressed, typed), ([], ["s"]))

    def test_picking_an_option_only_moves_to_it(self):
        state = {"here": 2, "pressed": []}
        lines = self.MODEL.split("\n")

        def screen(target):
            shown = [line.replace("❯", " ") if "❯" in line else line for line in lines]
            row = [i for i, line in enumerate(shown) if line.strip().startswith(f"{state['here'] + 1}.")][0]
            shown[row] = "   ❯" + shown[row][4:]
            return "\n".join(shown)

        def keys(target, *pressed):
            for key in pressed:
                state["pressed"].append(key)
                state["here"] += {"Up": -1, "Down": 1}.get(key, 0)
            return True
        session = {"status": "waiting", "waitingFor": "dialog open", "tmux": "t"}
        with mock.patch.object(app, "session_screen", screen), mock.patch.object(app, "send_keys", keys), \
                mock.patch.object(app.time, "sleep"):
            label = app.command_panel(screen(None))["options"][1]["label"]
            self.assertEqual(app.panel_action(session, "panelchoose", {"key": "2", "label": label}), (True, "Sent."))
        self.assertEqual(state["pressed"], ["Up"])  # No number and no Enter: /model would save a default.

    def test_context_markdown(self):
        text = """## Context Usage

**Model:** claude-haiku-5-5  
**Tokens:** 45.8k / 1m (5%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 3.8k | 0.4% |
| System tools (deferred) | 14.1k | 1.4% |
| Messages | 33.6k | 3.4% |
| Free space | 921.2k | 92.1% |

### Skills

| Skill | Source | Tokens |
|-------|--------|--------|
| hf-cli | User | ~340 |
"""
        parsed = app.parse_context(text)
        self.assertEqual((parsed["used"], parsed["window"], parsed["percent"], parsed["model"]), ("45.8k", "1m", 5.0, "claude-haiku-5-5"))
        self.assertEqual([c["name"] for c in parsed["categories"]], ["System prompt", "System tools (deferred)", "Messages", "Free space"])
        self.assertTrue(parsed["details"].startswith("### Skills"))
        self.assertIsNone(app.parse_context("Unknown command"))

    def test_helper_runs_are_not_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            for pid in (11, 12):
                with open(os.path.join(folder, f"{pid}.json"), "w") as handle:
                    json.dump({"pid": pid, "sessionId": "x"}, handle)
            with mock.patch.object(app, "SESSIONS_DIR", folder), mock.patch.object(app, "helper_pids", {12}):
                self.assertEqual(set(app.read_session_files()), {11})


class StartedSessionTests(unittest.TestCase):
    """A session started from another session's shell shows in that session's office."""

    def test_links(self):
        sessions = [{"id": "lead", "name": "Lead", "pid": 10, "status": "busy", "hosted": True},
                    {"id": "test", "name": "Test", "pid": 20, "status": "idle", "hosted": False, "actions": ["Read a file"]},
                    {"id": "mine", "name": "Mine", "pid": 30, "status": "idle", "hosted": True}]
        links = {20: (10, "old-id"), 30: (10, "lead")}  # 30 is the app's own: never counted.
        with mock.patch.object(app, "started_by", lambda pid: links.get(pid)):
            app.link_started_sessions(sessions)
        self.assertEqual(sessions[1]["startedBy"], {"id": "lead", "name": "Lead"})
        self.assertEqual([(t["id"], t["status"], t["last"]) for t in sessions[0]["started"]], [("test", "idle", "Read a file")])
        self.assertIsNone(sessions[2]["startedBy"])

    def test_never_remembered(self):
        monitor = app.Monitor()
        saved = {}
        known = {"remember": True, "sessions": {"test": {"id": "test", "seen": time.time()}}}
        monitor.transcripts = {"lead": types.SimpleNamespace(path="/x/lead.jsonl", permission_mode="", resume_model=None),
                               "test": types.SimpleNamespace(path="/x/test.jsonl", permission_mode="", resume_model=None)}
        sessions = [{"id": "lead", "name": "Lead", "cwd": "/x", "title": "", "startedBy": None},
                    {"id": "test", "name": "Test", "cwd": "/x", "title": "", "startedBy": {"id": "lead", "name": "Lead"}}]
        with mock.patch.object(app, "read_known", lambda: known), mock.patch.object(app, "write_known", saved.update):
            monitor._update_known(sessions)
        self.assertEqual(set(saved["sessions"]), {"lead"})

    @unittest.skipIf(sys.platform == "win32", "reads /proc")
    def test_reads_this_process(self):
        # This test isn't a Claude Code child unless run from one, but it must never fail to read itself.
        link = app.started_by(os.getpid())
        self.assertTrue(link is None or isinstance(link, tuple))


class InputBoxTests(unittest.TestCase):
    """Text in a session's own input box: read as Claude Code shows it, and replaced on send."""

    RULE = "─" * 60
    BOX = "\n".join(["● Done", RULE, "❯ line one", "  line two", "  line three", RULE, "  Haiku 5.5 | Ctx 4%", "  ⏸ manual mode on"])

    def test_several_lines_are_no_menu(self):
        self.assertEqual(app.menu_options(self.BOX), [])
        self.assertEqual(app.input_box(self.BOX), "line one\nline two\nline three")
        trust = "\n".join(["Is this a project you trust?", "", " ❯ No, exit", "   Yes, I trust this folder", "", " Enter to confirm"])
        self.assertEqual([o["label"] for o in app.menu_options(trust)], ["No, exit", "Yes, I trust this folder"])

    def test_clearing_line_by_line(self):
        lines = ["line one", "line two", "line three"]
        screen = lambda target: "\n".join(["● Done", self.RULE, "❯ " + "\n  ".join(lines) if lines else "❯ ", self.RULE, "  Haiku"])

        def keys(target, *pressed):
            for key in pressed:
                if key == "C-u" and lines:
                    lines[-1] = ""
                elif key == "BSpace" and lines and not lines[-1]:
                    lines.pop()
            return True
        with mock.patch.object(app, "session_screen", screen), mock.patch.object(app, "send_keys", keys), \
                mock.patch.object(app.time, "sleep"):
            self.assertTrue(app.clear_input_box("t"))
        self.assertIn(lines, ([], [""]))

    def test_send_replaces_only_the_text_seen(self):
        cleared, typed = [], []
        session = {"id": "s1", "tmux": "t"}
        with mock.patch.object(app, "session_screen", lambda target: "\n".join([self.RULE, "❯ old message", self.RULE])), \
                mock.patch.object(app, "clear_input_box", lambda target: cleared.append(target) or True), \
                mock.patch.object(app, "type_into", lambda session, text, images, pasted, in_box=None: typed.append((text, in_box)) or True), \
                mock.patch.object(app, "send_keys", lambda *a: True):
            app.type_message(session, "new", replaces="old message")
            app.type_message(session, "new", replaces="something else")
        self.assertEqual(cleared, ["t"])
        self.assertEqual(typed, [("new", ""), ("new", None)])  # Changed since: added after it, as typing there would.


class CommandOptionTests(unittest.TestCase):
    """Values offered after a command's name, from Claude Code's own hints or known lists."""

    def test_hints(self):
        cases = {"[on|off]": ["on", "off"], "[auto|<tokens>]": ["auto"], "[all]": ["all"], "<model>": [], "[name]": [],
                 "[reconnect|enable|disable [<server>|all]]": ["reconnect", "enable", "disable"],
                 "[<condition> | clear]": ["clear"], "[codex|gemini|cursor] [--dry-run]": ["codex", "gemini", "cursor"]}
        for hint, words in cases.items():
            self.assertEqual(app.hint_alternatives(hint), words, hint)

    def test_known_lists(self):
        with tempfile.TemporaryDirectory() as folder:
            styles = os.path.join(folder, ".claude", "output-styles")
            os.makedirs(styles)
            with open(os.path.join(styles, "terse.md"), "w") as handle:
                handle.write("---\nname: Terse\ndescription: Short answers\n---\nBe brief.")
            self.assertIn({"value": "Terse", "description": "Short answers"}, app.command_options("output-style", "[style]", folder))
        self.assertEqual([o["value"] for o in app.command_options("model", "<model>", "/")][:3], ["default", "opus", "sonnet"])
        self.assertEqual([o["value"] for o in app.command_options("effort", app.BUILTIN_HINTS["effort"], "/")],
                         ["low", "medium", "high", "xhigh", "max", "auto"])


class ChatOutputTests(unittest.TestCase):
    """What the terminal shows reaches the chat: live output in its last row, other records folded."""

    def test_live_output_without_the_frame(self):
        screen = "\n".join([" ▐▛███▜▌   Claude Code v2.1.325", "▝▜█████▛▘  Haiku 5.5 · Claude Pro", "  ▘▘ ▝▝    /tmp/x",
                             "● Printing lines · 21s", "  ⎿  $ for i in 1 2 3 (9s · 10 lines)", " " * 60 + "◐ medium · /effort",
                             "✶ Fiddle-faddling… (11s · ↓ 131 tokens)", "─" * 60, "❯ ", "─" * 60])
        live = app.live_activity(screen)
        self.assertEqual(live["head"], "✶ Fiddle-faddling… (11s · ↓ 131 tokens)")
        self.assertEqual(live["lines"], ["● Printing lines · 21s", "  ⎿  $ for i in 1 2 3 (9s · 10 lines)"])

    def test_hook_records_are_folded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.jsonl")
            with open(path, "w") as handle:
                for entry in [{"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["Previews: phone"]}},
                              {"type": "system", "subtype": "stop_hook_summary", "hookErrors": ["exit 1: boom"]},
                              {"type": "system", "subtype": "bridge_status", "content": "/remote-control is active"}]:
                    handle.write(json.dumps(entry) + "\n")
            reader = app.HistoryReader(path)
            reader.poll()
        shown = [(m["role"], m.get("title"), m["text"]) for m in reader.messages]
        self.assertEqual(shown, [("detail", "Context a hook added", "Previews: phone"), ("detail", "Hook error", "exit 1: boom"),
                                 ("system", None, "/remote-control is active")])


class HookPingTests(unittest.TestCase):
    """Desktop pings from the hook: none for sessions another session started, labelled for subagents."""

    SID = "c6813322-ec0b-465a-b695-0e83efdbde7b"

    def run_hook(self, payload, started, hosted=()):
        pings = []
        folder = tempfile.mkdtemp()
        env = {"CLAUDE_PID": str(os.getpid())}  # Any process that has a /proc entry.
        with mock.patch.object(app, "ATTENTION_DIR", folder), mock.patch.object(app, "WINDOWS", False), \
                mock.patch.object(app.sys, "stdin", io.StringIO(json.dumps({"session_id": self.SID, "cwd": "/x", **payload}))), \
                mock.patch.object(app, "started_by", lambda pid: (1, "parent") if started else None), \
                mock.patch.object(app, "hosted_panes", lambda: {os.getpid(): "x", os.getppid(): "x"} if hosted else {}), \
                mock.patch.object(app, "session_name", lambda sid, cwd: "Test"), \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(app, "desktop_notify", lambda title, body, urgent, replaces=None: pings.append(title) or 7):
            app.run_hook()
        with open(os.path.join(folder, self.SID + ".json")) as handle:
            record = json.load(handle)
        shutil.rmtree(folder)
        return pings, record

    def test_started_sessions_are_silent_but_still_recorded(self):
        request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "touch x"}}
        pings, record = self.run_hook(request, started=True)
        self.assertEqual(pings, [])
        self.assertEqual(record["kind"], "permission")  # The app still shows that it needs you.
        pings, _ = self.run_hook({"hook_event_name": "Stop", "last_assistant_message": "Done"}, started=True)
        self.assertEqual(pings, [])

    def test_other_sessions_still_ping(self):
        request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "touch x"}}
        self.assertEqual(self.run_hook(request, started=False)[0], ["Test needs permission"])
        self.assertEqual(self.run_hook(request, started=True, hosted=True)[0], ["Test needs permission"])  # The app's own.

    def test_a_subagent_ping_says_so(self):
        request = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "touch x"},
                   "agent_id": "a23870a219a70e3ee", "agent_type": "general-purpose"}
        self.assertEqual(self.run_hook(request, started=False)[0], ["Test subagent (general-purpose) needs permission"])


class EmulatorGuardTests(unittest.TestCase):
    """Headless emulator launches that would crash are caught; everything else is left alone."""

    def test_launches_that_crash(self):
        crashing = {
            "emulator -avd Medium_Phone -no-window": "default",
            "ANDROID_AVD_HOME=$PWD/avd ANDROID_SDK_ROOT=$PWD/SDK SDK/emulator/emulator -avd Medium_Phone -no-window -no-audio "
            "-no-snapshot-save -no-boot-anim > \"/tmp/x/emulator.log\" 2>&1 &": "default",
            "cd /sdk && nohup timeout 600 /sdk/emulator/emulator @Pixel -no-window -no-audio &": "default",
            "emulator -avd X -no-window -gpu swiftshader_indirect": "swiftshader_indirect",
            "emulator -avd X -no-window -gpu auto": "auto",
            "echo start; emulator -avd X -no-window && adb wait-for-device": "default",
        }
        for command, using in crashing.items():
            self.assertEqual(app.emulator_problem(command), using, command)

    def test_everything_else_runs(self):
        fine = [
            "emulator -avd X -no-window -gpu swangle_indirect", "emulator -avd X -no-window -gpu host",
            "emulator -avd X -gpu swiftshader_indirect",  # With a window, which is fine.
            "emulator -list-avds", "emulator -version", "adb emu kill", 'echo "emulator -avd X -no-window"',
            "grep -n emulator emulator.log", "ls SDK/emulator", 'pgrep -f "emulator -avd"', "git log --oneline", "",
        ]
        for command in fine:
            self.assertIsNone(app.emulator_problem(command), command)

    def test_hook_output(self):
        def run(payload):
            out = io.StringIO()
            with mock.patch.object(app.sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                app.run_emulator_guard()
            return out.getvalue()
        blocked = json.loads(run({"tool_name": "Bash", "tool_input": {"command": "emulator -avd X -no-window -gpu swiftshader_indirect"}}))
        output = blocked["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("-gpu swangle_indirect", output["permissionDecisionReason"])
        self.assertIn("Replace the `-gpu swiftshader_indirect`", output["permissionDecisionReason"])
        self.assertEqual(run({"tool_name": "Bash", "tool_input": {"command": "emulator -avd X -no-window -gpu host"}}), "")
        self.assertEqual(run({"tool_name": "Read", "tool_input": {"file_path": "emulator -avd X -no-window"}}), "")
        self.assertEqual(run({"nonsense": True}), "")

    def test_setup_installs_it_once(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "settings.json"), "w", encoding="utf-8") as handle:
                handle.write('{"note": "a \u2014 b"}\n')
            with mock.patch.object(app, "SETTINGS_FILE", os.path.join(folder, "settings.json")), \
                    mock.patch.object(app, "CONFIG_DIR", folder), mock.patch.object(app, "WINDOWS", False), \
                    mock.patch.object(app.os.path, "exists", lambda path: True if "applications" in str(path) else os.path.lexists(path)):
                for _ in range(2):
                    app.run_setup(verbose=False)
                with open(os.path.join(folder, "settings.json"), encoding="utf-8") as handle:
                    text = handle.read()
                groups = json.loads(text)["hooks"]["PreToolUse"]
        self.assertIn("a \u2014 b", text)  # Kept as written, not turned into an escape.
        guards = [h for g in groups for h in g["hooks"] if "--emulator-guard" in h["command"]]
        self.assertEqual(len(guards), 1)
        self.assertEqual((guards[0]["if"], groups[0]["matcher"]), ("Bash(*emulator*)", "Bash"))


class SkillTrimmingTests(unittest.TestCase):
    """Skills a CLI session can't use are switched off, and back on when that changes, without touching the user's own."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        synced = os.path.join(self.folder, "skills", "synced", "org_user")
        for name, description in {"desk": "Use in the desktop app, whose tools are named mcp__Desk__* there.",
                                  "chrome-browser": "Uses tools named mcp__claude-in-chrome__*.",
                                  "plain": "Reads PDFs.", "google-workspace": "Google files."}.items():
            os.makedirs(os.path.join(synced, name))
            with open(os.path.join(synced, name, "SKILL.md"), "w") as handle:
                handle.write(f"---\nname: {name}\ndescription: \"{description}\"\n---\nBody")
        self.patches = [mock.patch.object(app, "CONFIG_DIR", self.folder), mock.patch.object(app, "SETTINGS_FILE", os.path.join(self.folder, "settings.json")),
                        mock.patch.object(app, "SKILLS_FILE", os.path.join(self.folder, "skills_off.json")),
                        mock.patch.object(app, "connected_connectors", lambda: ["claude.ai Claude Docs"])]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def report(self, tools=()):
        names = ["anthropic-skills:" + n for n in ("desk", "chrome-browser", "plain", "google-workspace")]
        return {"skills": names, "tools": ["Bash", "Read"] + list(tools), "mcp_servers": []}

    def test_which_are_unusable(self):
        off = app.unusable_skills(self.report())
        self.assertEqual(sorted(off), ["anthropic-skills:desk", "anthropic-skills:google-workspace"])  # Not plain, nor chrome-browser (--chrome).
        self.assertEqual(app.unusable_skills(self.report(["mcp__Desk__open"])).keys(), {"anthropic-skills:google-workspace"})
        with mock.patch.object(app, "connected_connectors", lambda: ["claude.ai Google Drive"]):
            self.assertEqual(app.unusable_skills(self.report(["mcp__Desk__open"])), {})

    def test_settings_change_only_what_is_the_apps(self):
        path = os.path.join(self.folder, "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"theme": "dark \u2014 x", "skillOverrides": {"mine": "name-only"}}, handle)
        read = lambda: json.load(open(path, encoding="utf-8"))
        self.assertEqual(app.apply_skill_overrides({"a": "why", "mine": "why"}), (["a"], []))
        self.assertEqual(read()["skillOverrides"], {"mine": "name-only", "a": "off"})  # The user's own is left as it was.
        self.assertEqual(app.apply_skill_overrides({"a": "why"}), ([], []))  # Nothing new.
        self.assertEqual(app.apply_skill_overrides({}), ([], ["a"]))  # Usable again: back on, and only that one.
        self.assertEqual(read()["skillOverrides"], {"mine": "name-only"})
        self.assertIn("\u2014", open(path, encoding="utf-8").read())
        # Changed by hand since: no longer the app's to undo.
        app.apply_skill_overrides({"b": "why"})
        settings = read()
        settings["skillOverrides"]["b"] = "name-only"
        json.dump(settings, open(path, "w", encoding="utf-8"))
        self.assertEqual(app.apply_skill_overrides({}), ([], []))
        self.assertEqual(read()["skillOverrides"]["b"], "name-only")

    def test_a_second_check_changes_nothing(self):
        # Once skills are switched off Claude Code's report no longer lists them, so they must still be judged.
        path = os.path.join(self.folder, "settings.json")
        with open(path, "w") as handle:
            json.dump({"theme": "dark"}, handle)
        full, listed = self.report(), self.report()
        listed["skills"] = ["anthropic-skills:plain", "anthropic-skills:chrome-browser"]  # As after the first check.
        reports = iter([full, listed, listed])
        with mock.patch.object(app, "skills_report", lambda: next(reports)):
            self.assertEqual(app.trim_skills(), (["anthropic-skills:desk", "anthropic-skills:google-workspace"], []))
            self.assertEqual(app.trim_skills(), ([], []))
            self.assertEqual(app.trim_skills(), ([], []))
        self.assertEqual(sorted(json.load(open(path))["skillOverrides"]), ["anthropic-skills:desk", "anthropic-skills:google-workspace"])
        # And one whose connector arrives is switched back on, though the report doesn't list it.
        listed["tools"].append("mcp__claude_ai_Google_Drive__search")
        with mock.patch.object(app, "skills_report", lambda: listed):
            self.assertEqual(app.trim_skills(), ([], ["anthropic-skills:google-workspace"]))

    def test_nothing_left_removes_the_setting(self):
        path = os.path.join(self.folder, "settings.json")
        with open(path, "w") as handle:
            json.dump({"theme": "dark"}, handle)
        app.apply_skill_overrides({"a": "why"})
        app.apply_skill_overrides({})
        self.assertEqual(json.load(open(path)), {"theme": "dark"})


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

    def test_a_reopened_window_gets_a_single_use_code(self):
        port = 21000 + secrets.randbelow(4000)
        threading.Thread(target=app.run_web, args=(None, port, False), kwargs={"fixed_port": True}, daemon=True).start()
        self.assertTrue(wait_for(lambda: app.app_answers(port), 10))
        token = app.access_token()

        def sign_in(code):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/?t=" + code)
            response = connection.getresponse()
            response.read()
            return response.status

        code = app.window_code(port, token)
        self.assertTrue(code)
        self.assertEqual(sign_in(code), 302)
        self.assertEqual(sign_in(code), 403)  # Used up.
        self.assertIsNone(app.window_code(port, "not the token"))

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


class InstalledCommandTests(unittest.TestCase):
    """The built-in commands are read from the installed Claude Code's program."""

    PROGRAM = (b'var a=1;Qx={type:"local-jsx",name:"theme",description:"Change the theme",call:()=>{}},'
               b'Rz={name:"remote-control",aliases:["rc"],type:"local-jsx",get description(){return x()?"A":"B"},isEnabled:()=>!0},'
               b'Kb={type:"local",name:"keybindings",description:"Open your keyboard shortcuts file"},'
               b'In={type:"prompt",name:"init",get description(){return y},progressMessage:"x"},'
               b'Hd={type:"local",name:"heapdump",description:"Dump the heap",isHidden:!0,call:z},'
               b'Ad={type:"local",name:"add-dir",description:"Add a new working directory",argumentHint:"<path>"},'
               b'Fn={type:"local",name:"function",get description(){return q}}')

    def test_reads_names_hints_and_descriptions(self):
        found = app.scan_commands(self.PROGRAM)
        self.assertEqual(found["theme"], ("", "Change the theme", False))
        self.assertEqual(found["add-dir"], ("<path>", "Add a new working directory", False))
        self.assertEqual(found["remote-control"], ("", "", False))  # Worked out while running.
        self.assertEqual(found["init"][1], "")  # Not taken from the next command along.
        self.assertTrue(found["heapdump"][2])

    def test_merges_with_the_apps_own_list(self):
        saved = app.installed_commands
        try:
            scanned = app.scan_commands(self.PROGRAM)
            scanned.update({f"extra-{n}": ("", f"Extra {n}", False) for n in range(20)})
            app.installed_commands = lambda: scanned
            commands = {name: (hint, text) for name, hint, text in app.builtin_commands()}
        finally:
            app.installed_commands = saved
        self.assertEqual(commands["theme"], ("", "Change the theme"))
        self.assertIn("remote-control", commands)  # Described by the app's own list.
        self.assertEqual(commands["init"][1], dict((n, t) for n, _, t in app.BUILTIN_COMMANDS)["init"])
        self.assertNotIn("heapdump", commands)
        self.assertNotIn("function", commands)  # Nothing describes it, so it may not be a command.
        self.assertIn("compact", commands)  # Kept from the app's list though the scan missed it.

    def test_falls_back_when_it_cant_be_read(self):
        saved = app.installed_commands
        try:
            app.installed_commands = lambda: None
            self.assertEqual(app.builtin_commands(), app.BUILTIN_COMMANDS)
        finally:
            app.installed_commands = saved


class ModelTests(unittest.TestCase):
    """The model in use, and changes to it shown in the chat."""

    def transcript(self, entries):
        path = os.path.join(tempfile.mkdtemp(dir=CONFIG), "t.jsonl")
        with open(path, "w") as handle:
            for number, entry in enumerate(entries):
                handle.write(json.dumps({"uuid": str(number), "timestamp": f"2026-11-09T10:{number:02d}:00Z", **entry}) + "\n")
        return path

    @staticmethod
    def reply(model, text="ok"):
        return {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": text}]}}

    @staticmethod
    def said(text):
        return {"type": "user", "message": {"content": text}}

    def test_names(self):
        for model, name in [("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-opus-4-1-20250805", "Opus 4.1"),
                            ("claude-3-5-sonnet-20241022", "Sonnet 3.5"), ("claude-opus-5-5[1m]", "Opus 5.5"), ("other", "other")]:
            self.assertEqual(app.model_name(model), name)
        self.assertEqual(app.model_change("Set model to `Sonnet 5.5` and saved as your default for new sessions"), ("you", "Sonnet 5.5"))
        self.assertEqual(app.model_change("Switched to Sonnet 5.5 due to high demand for Opus 5.5"), ("auto", "Sonnet 5.5"))
        self.assertIsNone(app.model_change("Kept model as Opus 5.5"))

    def test_changes_in_the_chat(self):
        path = self.transcript([
            self.reply("claude-opus-5-5"),
            self.reply("claude-sonnet-5-5"),  # Changed with nothing said: automatic.
            self.said("<command-name>/model</command-name>\n<command-args>haiku</command-args>"),
            self.said("<local-command-stdout>Set model to `Haiku 5.5` and saved as your default for new sessions</local-command-stdout>"),
            self.reply("claude-haiku-5-5"),  # The user's own switch is already shown.
            {"type": "system", "subtype": "informational", "content": "Switched to Sonnet 5.5 due to high demand for Haiku 5.5"},
            self.reply("claude-sonnet-5-5"),
            self.reply("<synthetic>"),
            self.reply("claude-sonnet-5-5"),
            self.said("<local-command-stdout>Kept model as Sonnet 5.5</local-command-stdout>"),
        ])
        history = app.HistoryReader(path)
        history.poll()
        changes = [(m["model"]["by"], m["model"]["to"], m["model"]["was"]) for m in history.messages if m.get("model")]
        self.assertEqual(changes, [("auto", "Sonnet 5.5", "Opus 5.5"), ("you", "Haiku 5.5", ""), ("auto", "Sonnet 5.5", "Haiku 5.5")])
        self.assertEqual({k: v for k, v in history.messages[-1].items() if k != "at"},
                         {"role": "system", "note": True, "text": "Kept model as Sonnet 5.5", "seq": len(history.messages)})
        self.assertFalse(any(m["role"] == "you" and "local-command" in m["text"] for m in history.messages))

    def test_current_model(self):
        path = self.transcript([self.reply("claude-opus-5-5"),
                                self.said("<local-command-stdout>Set model to `Sonnet 5.5`</local-command-stdout>")])
        transcript = app.Transcript("t", path)
        transcript.poll()
        self.assertEqual(transcript.model, "Sonnet 5.5")


@unittest.skipUnless(shutil.which("node"), "needs node")
class MarkdownTests(unittest.TestCase):
    """Claude's replies drawn from Markdown, run in node with a small stand-in for the page."""

    STUB = """
class Node{constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.dataset={};this.style={};this.classList={add:c=>this.className=(this.className?this.className+' ':'')+c}}
 append(...xs){for(const x of xs)this.children.push(typeof x==='string'?x:x)}appendChild(x){this.children.push(x);return x}
 setAttribute(k,v){this.attrs[k]=v}set textContent(t){this.children=[String(t)]}}
const document={createElement:t=>new Node(t)};
function show(n){if(typeof n==='string')return n;const inner=n.children.map(show).join('');
 const extra=(n.href?' href='+n.href:'')+(n.start?' start='+n.start:'');return '<'+n.tag+extra+'>'+inner+'</'+n.tag+'>'}
"""

    def render(self, text):
        page = app.PAGE
        start = page.index("const MD_FENCE=")
        code = page[page.index("function el("):].split("\n", 1)[0] + "\n" + page[start:page.index("let youAt=0;", start)]
        script = self.STUB + code + f"\nconst r=new Node('div');markdown({json.dumps(text)},r);console.log(r.children.map(show).join(''));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_blocks_and_inline(self):
        self.assertEqual(self.render("## Plan\nSome **bold**, *it*, `x<y>`, 2*3*4 and snake_case.\n\n- a\n  - b\n1. c\n"),
                         "<div>Plan</div><p>Some <strong>bold</strong>, <em>it</em>, <code>x<y></code>, 2*3*4 and snake_case.</p>"
                         "<ul><li>a<ul><li>b</li></ul></li></ul><ol><li>c</li></ol>")
        self.assertEqual(self.render("```\n**not bold**\n"), "<pre><code>**not bold**</code></pre>")  # Cut off before it closes.
        self.assertEqual(self.render("| a | b |\n|---|--:|\n| 1 | 2 |"),
                         "<div><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table></div>")

    def test_links(self):
        self.assertEqual(self.render("See https://example.com/a. and [x](https://e.org) and [f](src/app.py:3) and [j](javascript:alert(1))"),
                         "<p>See <a href=https://example.com/a>https://example.com/a</a>. and <a href=https://e.org>x</a> and "
                         "<span>f</span> and <span>j</span>)</p>")


if __name__ == "__main__":
    unittest.main()


class FolderApprovalTests(unittest.TestCase):
    def test_an_approval_matches_however_the_path_was_typed(self):
        app = load(SOURCE, "folders_" + secrets.token_hex(4))
        home = os.path.expanduser("~")
        self.assertTrue(app.same_folder(home, "~"))
        self.assertTrue(app.same_folder(home + os.sep, home))
        self.assertFalse(app.same_folder(home, os.path.join(home, "other")))
        self.assertFalse(app.same_folder("", home))
        self.assertFalse(app.same_folder(None, home))

    def test_the_trust_menu_is_read_with_either_pointer(self):
        app = load(SOURCE, "trust_" + secrets.token_hex(4))
        for pointer in ("❯", ">"):
            screen = f" {pointer} 1. Yes, I trust this folder\n   2. No, exit\n"
            self.assertTrue(app.TRUST_YES_SELECTED.search(screen), pointer)
        self.assertFalse(app.TRUST_YES_SELECTED.search("   1. Yes, I trust this folder\n > 2. No, exit"))
        self.assertTrue(app.TRUST_YES_NUMBERED.search("   1. Yes, I trust this folder"))


class AdoptTests(unittest.TestCase):
    """Moving a terminal session into the app: the /to-app skill, and the helper that resumes it there."""

    def test_the_skill_is_written_and_kept_current_but_not_over_the_users_own(self):
        module = load(SOURCE, "adopt_skill_" + secrets.token_hex(4))
        folder = tempfile.mkdtemp(prefix="claude-sessions-skill-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        module.ADOPT_SKILL = os.path.join(folder, "skills", "to-app", "SKILL.md")
        self.assertTrue(module.install_adopt_skill())
        self.assertFalse(module.install_adopt_skill())
        with open(module.ADOPT_SKILL, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("--adopt", text)
        self.assertTrue(text.startswith("---\nname: to-app\n"))
        with open(module.ADOPT_SKILL, "w", encoding="utf-8") as handle:
            handle.write("my own skill")
        self.assertFalse(module.install_adopt_skill())

    @unittest.skipUnless(shutil.which("tmux") and not WINDOWS, "needs tmux on Linux")
    def test_a_terminal_session_is_closed_and_resumed_in_the_app(self):
        folder = tempfile.mkdtemp(prefix="claude-sessions-adopt-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        config = os.path.join(folder, "config")
        os.makedirs(os.path.join(config, "sessions"))
        bin_dir = os.path.join(folder, "bin")
        os.makedirs(bin_dir)
        marker = os.path.join(folder, "resumed")
        with open(os.path.join(bin_dir, "claude"), "w") as handle:  # Stands in for Claude Code.
            handle.write(f'#!/bin/sh\necho "$@" > "{marker}"\nsleep 60\n')
        os.chmod(os.path.join(bin_dir, "claude"), 0o755)
        session_id = "11111111-2222-3333-4444-555555555555"
        old = subprocess.Popen(["sleep", "60"])  # The terminal's Claude Code.
        self.addCleanup(old.kill)
        with open(os.path.join(config, "sessions", f"{old.pid}.json"), "w") as handle:
            json.dump({"pid": old.pid, "sessionId": session_id, "cwd": folder, "status": "idle"}, handle)
        environment = {**os.environ, "CLAUDE_CONFIG_DIR": config, "PATH": bin_dir + os.pathsep + os.environ["PATH"],
                       "CLAUDE_SESSIONS_TMUX_SOCKET": "adopt-test-" + secrets.token_hex(4), "CLAUDE_SESSIONS_NO_WINDOW": "1",
                       "CLAUDE_PID": str(old.pid), "CLAUDE_CODE_SESSION_ID": session_id}
        self.addCleanup(subprocess.run, ["tmux", "-L", environment["CLAUDE_SESSIONS_TMUX_SOCKET"], "kill-server"], capture_output=True)
        first = subprocess.run([sys.executable, SOURCE, "--adopt"], env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Moving this session", first.stdout)
        self.assertTrue(wait_for(lambda: os.path.exists(marker), 30), "the session was not resumed")
        with open(marker) as handle:
            self.assertIn(f"--resume {session_id}", handle.read())
        self.assertIsNotNone(wait_for(lambda: old.poll() is not None or None, 10), "the terminal copy was not closed")
        again = subprocess.run([sys.executable, SOURCE, "--adopt"], env=environment, capture_output=True, text=True, timeout=30)
        self.assertIn("already running in the app", again.stdout)


class AttachProtocolTests(unittest.TestCase):
    """What the session host does when a console window joins, against a stand-in terminal (the real one needs Windows)."""

    class Fake:
        def __init__(self):
            self.subscribers, self.typed, self.sizes, self.up = [], [], [], True

        def subscribe(self, updates, columns, rows):
            self.subscribers.append(updates)
            self.sizes.append((columns, rows))

        def unsubscribe(self, updates):
            self.subscribers.remove(updates)
            self.sizes.append("left")

        def resize(self, columns, rows, repaint=False):
            self.sizes.append((columns, rows))

        def write(self, text):
            self.typed.append(text)
            return True

        def alive(self):
            return self.up

    def test_output_flows_out_and_typing_flows_in(self):
        terminal = self.Fake()
        host_side, window_side = socket.socketpair()
        self.addCleanup(window_side.close)
        thread = threading.Thread(target=app.serve_attach, args=(host_side.makefile("rb"), host_side, terminal, 100, 30), daemon=True)
        thread.start()
        window = window_side.makefile("rb")
        self.assertTrue(json.loads(window.readline())["ok"])
        self.assertEqual(terminal.sizes[0], (100, 30))
        terminal.subscribers[0].put("hello \u00e9")
        self.assertEqual(window.read(len("hello é".encode())), "hello é".encode())
        window_side.sendall(app.attach_frame(b"d", "typed é".encode()) + app.attach_frame(b"r", b"90,25"))
        self.assertTrue(wait_for(lambda: terminal.typed == ["typed é"] and (90, 25) in terminal.sizes))
        window_side.shutdown(socket.SHUT_WR)  # The window closes.
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(terminal.subscribers, [])
        self.assertEqual(terminal.sizes[-1], "left")

    def test_the_window_is_told_when_the_session_ends(self):
        terminal = self.Fake()
        host_side, window_side = socket.socketpair()
        self.addCleanup(window_side.close)
        threading.Thread(target=app.serve_attach, args=(host_side.makefile("rb"), host_side, terminal, 80, 24), daemon=True).start()
        window = window_side.makefile("rb")
        window.readline()
        terminal.subscribers[0].put("last words")
        terminal.up = False
        self.assertEqual(window.read(), b"last words")  # Then the connection closes.


class TaskNotificationTextTests(unittest.TestCase):
    """A finished background task reads as a line of its own, with the rest folded away, not as raw tags."""

    def read(self, *contents):
        path = os.path.join(tempfile.mkdtemp(dir=CONFIG), "t.jsonl")
        with open(path, "w") as handle:
            for n, content in enumerate(contents):
                handle.write(json.dumps({"type": "user", "uuid": str(n), "message": {"role": "user", "content": content}}) + "\n")
        history = app.HistoryReader(path)
        history.poll()
        return history.messages

    def test_shown_as_a_note_and_not_as_something_you_said(self):
        note = ("<task-notification>\n<task-id>b33</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
                "<output-file>/tmp/x/tasks/b33.output</output-file>\n<status>completed</status>\n"
                "<summary>Background command \"python3 -I - &lt;&lt;'EOF'\nimport re\nEOF\" completed (exit code 0)</summary>\n"
                "</task-notification>")
        messages = self.read(note, note)  # Recorded twice, shown once.
        self.assertEqual([m["role"] for m in messages], ["system", "detail"])
        self.assertTrue(messages[0]["note"])
        self.assertIn("<<'EOF'", messages[1]["text"])
        self.assertIn("Output: /tmp/x/tasks/b33.output", messages[1]["text"])
        self.assertFalse(any("task-notification" in m["text"] for m in messages))

    def test_text_beside_a_notification_is_still_a_message(self):
        note = "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n<summary>Done</summary>\n</task-notification>"
        messages = self.read(note + "\n\nand then stop")
        self.assertEqual([m["role"] for m in messages], ["system", "you"])
        self.assertEqual(messages[0]["text"], "Done")
        self.assertEqual(messages[1]["text"], "and then stop")


class CopilotSessionTests(unittest.TestCase):
    """GitHub Copilot CLI sessions, read from the files Copilot keeps under ~/.copilot/session-state."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="copilot-state-")
        self.saved = app.COPILOT_STATE_DIR, app.copilot_process, app.subprocess.run, app.SESSIONS_DIR, app.hosted_panes
        app.COPILOT_STATE_DIR = self.state
        app.SESSIONS_DIR = tempfile.mkdtemp(prefix="claude-sessions-files-")
        app.hosted_panes = lambda: {}
        app.subprocess.run = lambda command, *a, **k: subprocess.CompletedProcess(command, 0, "[]", "")
        self.processes = {}  # pid -> (program, start time); a pid that isn't here isn't running.
        app.copilot_process = lambda pid: self.processes.get(pid)
        app._copilot_scan["at"] = 0
        self.clock = 0

    def tearDown(self):
        app.COPILOT_STATE_DIR, app.copilot_process, app.subprocess.run, app.SESSIONS_DIR, app.hosted_panes = self.saved
        app._copilot_scan["at"] = 0
        shutil.rmtree(self.state, ignore_errors=True)

    def session(self, session_id, pid=None, program="copilot.exe", events=(), name="", cwd="C:\\work\\app"):
        folder = os.path.join(self.state, session_id)
        os.makedirs(folder)
        with open(os.path.join(folder, "workspace.yaml"), "w") as handle:
            handle.write(f"id: {session_id}\ncwd: {cwd}\nname: {name}\ncreated_at: 2026-11-20T15:08:34.448Z\n")
        if pid:
            lock = os.path.join(folder, f"inuse.{pid}.lock")
            with open(lock, "w") as handle:
                handle.write(str(pid))
            self.processes[pid] = (program, os.path.getmtime(lock) - 30)
        if events:
            self.log(session_id, *events)
        return folder

    def log(self, session_id, *events):
        with open(os.path.join(self.state, session_id, "events.jsonl"), "a", encoding="utf-8") as handle:
            for kind, data in events:
                self.clock += 1
                stamp = f"2026-11-20T15:{self.clock // 60:02d}:{self.clock % 60:02d}.000Z"
                handle.write(json.dumps({"type": kind, "data": data, "id": str(self.clock), "timestamp": stamp}) + "\n")

    def transcript(self, session_id):
        transcript = app.CopilotTranscript(session_id)
        transcript.poll()
        return transcript

    def test_lists_only_sessions_with_a_running_copilot(self):
        self.session("live", pid=101)
        self.session("crashed", pid=102)
        del self.processes[102]  # Its lock file stayed behind.
        self.session("reused", pid=103, program="chrome.exe")  # Its process id now belongs to something else.
        self.session("node-hosted", pid=104, program="node.exe")
        self.session("ended")
        self.assertEqual([s["sessionId"] for s in app.copilot_live_sessions()], ["live", "node-hosted"])

    def test_a_process_that_started_after_the_lock_is_not_its_owner(self):
        folder = self.session("later", pid=105)
        self.processes[105] = ("copilot.exe", os.path.getmtime(os.path.join(folder, "inuse.105.lock")) + 600)
        self.assertEqual(app.copilot_live_sessions(), [])

    def test_listing_says_what_a_session_is(self):
        self.session("abc", pid=101, name="Fix the build", cwd="C:\\repos\\api")
        (listed,) = app.copilot_live_sessions()
        self.assertEqual((listed["provider"], listed["name"], listed["cwd"], listed["pid"]), ("copilot", "Fix the build", "C:\\repos\\api", 101))
        self.assertGreater(listed["startedAt"], 0)

    def test_state_follows_the_log(self):
        self.session("s", pid=101, events=[("session.start", {"selectedModel": "gpt-6-luna"}), ("user.message", {"content": "do it"})])
        self.assertEqual(self.transcript("s").state, "idle")
        self.log("s", ("assistant.turn_start", {}), ("tool.execution_start", {"toolCallId": "t1", "toolName": "powershell", "arguments": {"command": "git status"}}))
        working = self.transcript("s")
        self.assertEqual((working.state, list(working.pending.values())), ("working", ["powershell: git status"]))
        self.log("s", ("permission.requested", {"requestId": "r1", "permissionRequest": {"kind": "shell", "intention": "Check the repo"}}))
        waiting = self.transcript("s")
        self.assertEqual((waiting.state, {k: v for k, v in waiting.attention().items() if k != "at"}), ("waiting", {"kind": "permission", "message": "Check the repo"}))
        self.assertGreater(waiting.attention()["at"], 0)
        self.log("s", ("permission.completed", {"requestId": "r1", "result": {"kind": "approved"}}),
                 ("tool.execution_complete", {"toolCallId": "t1", "success": True, "result": {"content": "clean"}}),
                 ("assistant.turn_end", {}))
        done = self.transcript("s")
        self.assertEqual((done.state, done.attention(), done.pending), ("idle", None, {}))

    def test_a_question_is_waiting_too(self):
        self.session("s", pid=101, events=[("assistant.turn_start", {}), ("tool.execution_start", {"toolCallId": "q1", "toolName": "ask_user", "arguments": {"message": "Which folder?"}})])
        transcript = self.transcript("s")
        wanted = transcript.attention()
        self.assertEqual((transcript.state, wanted["kind"], wanted["message"]), ("waiting", "question", "Which folder?"))
        self.assertGreater(wanted["at"], 0)  # When it was raised, so an answer can be tied to it.

    def test_a_session_that_shut_down_is_not_left_waiting(self):
        self.session("s", events=[("assistant.turn_start", {}), ("permission.requested", {"requestId": "r", "permissionRequest": {"kind": "shell"}}), ("session.shutdown", {})])
        self.assertEqual(self.transcript("s").state, "idle")

    def test_model_and_context_come_from_the_log(self):
        self.session("s", events=[
            ("assistant.message", {"model": "gpt-6-luna", "content": "Hello"}),
            ("session.usage_record", {"usage": {"inputTokens": 20000, "cacheReadTokens": 5000, "maxPromptTokens": 250000}}),
        ])
        transcript = self.transcript("s")
        self.assertEqual((transcript.model, transcript.text, transcript.context, transcript.context_percent), ("gpt-6-luna", "Hello", 25000, 10))

    def test_logs_are_read_as_they_grow(self):
        self.session("s", events=[("user.message", {"content": "one"})])
        transcript = self.transcript("s")
        self.log("s", ("user.message", {"content": "two"}))
        transcript.poll()
        self.assertEqual((transcript.human_messages, transcript.prompt), (2, "two"))

    def test_sessions_say_which_program_they_are(self):
        self.session("cop", pid=101, name="copilot work", events=[("user.message", {"content": "hi"})])
        app.subprocess.run = lambda command, *a, **k: subprocess.CompletedProcess(
            command, 0, json.dumps([{"pid": 7, "sessionId": "cla", "status": "idle", "name": "claude work", "startedAt": 1, "cwd": "/"}]), "")
        monitor = app.Monitor()
        monitor.refresh()
        states = {s["id"]: s for s in map(monitor.session_state, monitor.sessions)}
        self.assertEqual({k: v["provider"] for k, v in states.items()}, {"cla": "claude", "cop": "copilot"})
        self.assertFalse(states["cop"]["hosted"])
        self.assertEqual(states["cop"]["status"], "idle")

    def test_waiting_shows_as_needing_you(self):
        self.session("cop", pid=101, events=[("assistant.turn_start", {}), ("permission.requested", {"requestId": "r", "permissionRequest": {"kind": "shell", "intention": "Write a file"}})])
        monitor = app.Monitor()
        monitor.refresh()
        (state,) = map(monitor.session_state, monitor.sessions)
        self.assertEqual(state["status"], "waiting")
        self.assertEqual((state["attention"]["kind"], state["attention"]["message"]), ("permission", "Write a file"))

    def test_copilot_sessions_are_never_offered_for_claude_to_resume(self):
        self.session("cop", pid=101, events=[("user.message", {"content": "hi"})])
        monitor = app.Monitor()
        monitor.refresh()
        monitor._update_known([monitor.session_state(s) for s in monitor.sessions])
        self.assertEqual(app.read_known()["sessions"], {})
        self.assertEqual(monitor.previous, [])

    def test_actions_are_refused_for_a_session_the_app_does_not_host(self):
        self.session("cop", pid=101)
        monitor = app.Monitor()
        monitor.refresh()
        self.assertEqual(app.end_session(monitor, {"id": "cop"}), (False, app.NOT_HOSTED))

    def read_history(self, *events):
        self.session("h", events=list(events))
        reader = app.CopilotHistoryReader(os.path.join(self.state, "h", "events.jsonl"))
        reader.poll()
        return reader

    def test_history_reads_the_conversation(self):
        reader = self.read_history(
            ("session.start", {"selectedModel": "gpt-6-luna", "context": {"cwd": "C:\\work"}}),
            ("user.message", {"content": "list files"}),
            ("assistant.message", {"content": "Looking.", "model": "gpt-6-luna"}),
            ("tool.execution_start", {"toolCallId": "t1", "toolName": "powershell", "arguments": {"command": "ls", "description": "List"}}),
            ("tool.execution_complete", {"toolCallId": "t1", "success": True, "result": {"content": "a.txt\nb.txt"}}),
            ("assistant.message", {"content": "Two files."}),
        )
        self.assertEqual([m["role"] for m in reader.messages], ["you", "claude", "tool", "claude"])
        tool = reader.messages[2]
        self.assertEqual((tool["text"], tool["pending"], tool["error"], tool["result"]), ("powershell: List", False, False, "a.txt\nb.txt"))
        self.assertEqual(reader.tool_details("t1")["result"], "a.txt\nb.txt")

    def test_a_failed_tool_is_marked_failed(self):
        reader = self.read_history(
            ("tool.execution_start", {"toolCallId": "t1", "toolName": "powershell", "arguments": "exit 3"}),
            ("tool.execution_complete", {"toolCallId": "t1", "success": False, "error": {"message": "exit code 3"}}),
        )
        self.assertEqual((reader.messages[0]["error"], reader.messages[0]["result"]), (True, "exit code 3"))

    def test_changed_files_come_from_the_diff_copilot_reports(self):
        diff = ("\ndiff --git a/C:/work/app.py b/C:/work/app.py\nindex 1..2 100644\n--- a/C:/work/app.py\n+++ b/C:/work/app.py\n"
                "@@ -10,3 +10,4 @@ def run():\n     keep\n-    old\n+    new\n+    more\n     tail\n")
        reader = self.read_history(
            ("user.message", {"content": "change it"}),
            ("tool.execution_start", {"toolCallId": "e1", "toolName": "edit", "arguments": {"path": "C:\\work\\app.py", "old_str": "old", "new_str": "new"}}),
            ("tool.execution_complete", {"toolCallId": "e1", "success": True, "fileEdits": [{"path": "C:\\work\\app.py", "kind": "edit"}],
                                         "result": {"content": "Modified 1 file(s)", "detailedContent": diff}}),
        )
        (change,) = reader.changes()["files"]
        self.assertEqual((change["path"], change["added"], change["removed"], change["created"]), ("C:/work/app.py", 2, 1, False))
        self.assertEqual(change["hunks"][0]["newStart"], 10)

    def test_a_patch_given_as_text_still_counts_when_no_diff_is_reported(self):
        patch = "*** Begin Patch\n*** Add File: notes.md\n+hello\n+world\n*** End Patch"
        reader = self.read_history(
            ("session.start", {"context": {"cwd": self.state}}),
            ("tool.execution_start", {"toolCallId": "p1", "toolName": "apply_patch", "arguments": patch}),
            ("tool.execution_complete", {"toolCallId": "p1", "success": True, "result": {"content": "ok"}}),
        )
        (change,) = reader.changes()["files"]
        self.assertEqual((change["path"], change["created"], change["added"]), (os.path.join(self.state, "notes.md"), True, 2))

    def test_failed_edits_change_nothing(self):
        reader = self.read_history(
            ("tool.execution_start", {"toolCallId": "e1", "toolName": "create", "arguments": {"path": "x.txt", "file_text": "hi"}}),
            ("tool.execution_complete", {"toolCallId": "e1", "success": False, "error": {"message": "denied"}}),
        )
        self.assertEqual(reader.changes()["files"], [])

    def test_the_page_marks_every_session_with_its_program(self):
        for expected in ("const PROVIDERS=", "function provBadge(", "provBadge(c.prov,s)", 'id="histProv"', "provBadge($('histProv'),s)"):
            self.assertIn(expected, app.PAGE)
        self.assertIn("'GitHub Copilot CLI session'", app.PAGE)
        self.assertIn("'Claude Code session'", app.PAGE)


SCREENS = os.path.join(ROOT, "tests", "screens")


def screen_of(name):
    """A screen captured from the real program, as the app reads it."""
    with open(os.path.join(SCREENS, name), encoding="utf-8") as handle:
        return app.HISTORY_CONTROL_CHARS.sub(" ", handle.read()).strip()


class CopilotScreenTests(unittest.TestCase):
    """Reading GitHub Copilot CLI's terminal, from screens captured from the real program."""

    def test_its_screens_are_told_from_claudes(self):
        for name in ("copilot-idle.txt", "copilot-permission.txt", "copilot-question.txt", "copilot-trust.txt", "copilot-working.txt"):
            self.assertTrue(app.is_copilot_screen(screen_of(name)), name)
        claude = "● Done.\n\n────────────\n❯ \n────────────\n  ? for shortcuts"
        self.assertFalse(app.is_copilot_screen(claude))
        self.assertEqual(app.input_box(claude), "")  # Still read as Claude Code's.

    def test_a_conversation_that_quotes_a_question_is_not_taken_for_copilot(self):
        quoted = "● It asks: Do you trust the files in this folder?\n  and shows Copilot needs information.\n\n────────────\n❯ \n────────────"
        self.assertFalse(app.is_copilot_screen(quoted))

    def test_the_input_box(self):
        self.assertEqual(app.input_box(screen_of("copilot-idle.txt")), "")
        self.assertEqual(app.input_box(screen_of("copilot-working.txt")), "")
        typed = screen_of("copilot-idle.txt").replace("┃\n", "┃ half a thought\n", 1)
        self.assertEqual(app.input_box(typed), "half a thought")
        for name in ("copilot-permission.txt", "copilot-question.txt", "copilot-trust.txt"):
            self.assertIsNone(app.input_box(screen_of(name)), name)  # A question replaces the box.

    def test_a_permission_menu(self):
        options = app.menu_options(screen_of("copilot-permission.txt"))
        self.assertEqual([(o["key"], o["label"][:3], o["highlighted"]) for o in options], [("1", "Yes", True), ("2", "Yes", False), ("3", "No,", False)])

    def test_the_folder_trust_menu(self):
        options = app.menu_options(screen_of("copilot-trust.txt"))
        self.assertEqual([o["label"] for o in options], ["Yes", "Yes, and remember this folder for future sessions", "No (Esc)"])
        self.assertTrue(app.COPILOT_TRUST.search(screen_of("copilot-trust.txt")))
        self.assertFalse(app.COPILOT_TRUST.search(screen_of("copilot-idle.txt")))

    def test_a_question_it_asks(self):
        options = app.menu_options(screen_of("copilot-question.txt"))
        self.assertEqual([(o["key"], o["label"], o["highlighted"]) for o in options],
                         [("", "red", True), ("", "blue", False), ("", "Other (type your answer)", False)])

    def test_no_menu_on_screens_without_one(self):
        for name in ("copilot-idle.txt", "copilot-working.txt", "copilot-allowall.txt"):
            self.assertEqual(app.menu_options(screen_of(name)), [], name)

    def test_its_mode_comes_from_the_footer(self):
        self.assertEqual(app.copilot_mode(screen_of("copilot-idle.txt")), {"run": "interactive", "approval": "manual"})
        self.assertEqual(app.copilot_mode(screen_of("copilot-allowall.txt")), {"run": "interactive", "approval": "allow-all"})
        self.assertEqual(app.copilot_mode(screen_of("copilot-autopilot.txt")), {"run": "autopilot", "approval": "allow-all"})
        self.assertIsNone(app.copilot_mode(screen_of("copilot-working.txt")))  # The footer is the working line then.

    def test_working_shows_as_activity(self):
        self.assertIn("Working", app.live_activity(screen_of("copilot-working.txt"))["head"])
        self.assertEqual(app.live_activity(screen_of("copilot-idle.txt"))["head"], "")

    def test_the_host_answers_the_questions_it_waits_on(self):
        asked = "\x1b]11;?\x1b\\\x1b]4;3;?\x07\x1b[14t\x1b[?996n\x1b[?2026$p\x1b[?u\x1b[>q"
        answers = "".join(app.terminal_answers(asked))
        for expected in ("\x1b]11;rgb:", "\x1b]4;3;rgb:", "\x1b[4;800;1200t", "\x1b[?997;1n", "\x1b[?2026;2$y", "\x1b[?0u"):
            self.assertIn(expected, answers)
        self.assertEqual(app.terminal_answers("plain output"), [])

    def test_the_host_can_press_the_keys_copilot_uses(self):
        self.assertEqual((app.TERMINAL_KEYS["C-c"], app.TERMINAL_KEYS["C-u"]), ("\x03", "\x15"))


class CopilotHostedTests(unittest.TestCase):
    """Copilot sessions the app started: shown as hosted, started, approved and interrupted."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="copilot-state-")
        self.saved = {n: getattr(app, n) for n in ("COPILOT_STATE_DIR", "copilot_process", "subprocess", "SESSIONS_DIR", "hosted_panes", "session_screen",
                                                   "new_terminal", "kill_terminal", "hosted_sessions", "type_into", "send_keys", "choose_answer", "hosted_session")}
        app.COPILOT_STATE_DIR = self.state
        app.SESSIONS_DIR = tempfile.mkdtemp(prefix="claude-sessions-files-")
        app.subprocess = types.SimpleNamespace(**{k: getattr(subprocess, k) for k in dir(subprocess) if not k.startswith("_")})
        app.subprocess.run = lambda command, *a, **k: subprocess.CompletedProcess(command, 0, "[]", "")
        app.copilot_process = lambda pid: ("copilot.exe", 0)
        app._copilot_scan["at"] = 0
        self.screen = screen_of("copilot-idle.txt")
        app.session_screen = lambda target: self.screen
        folder = os.path.join(self.state, "cop")
        os.makedirs(folder)
        with open(os.path.join(folder, "workspace.yaml"), "w") as handle:
            handle.write("id: cop\ncwd: C:\\work\nname: hosted one\ncreated_at: 2026-11-20T15:08:34.448Z\n")
        with open(os.path.join(folder, "inuse.101.lock"), "w") as handle:
            handle.write("101")
        self.keys = []
        app.send_keys = lambda target, *keys: self.keys.append((target, keys)) or True

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)
        app._copilot_scan["at"] = 0
        shutil.rmtree(self.state, ignore_errors=True)

    def state_of(self, panes):
        app.hosted_panes = lambda: panes
        monitor = app.Monitor()
        monitor.refresh()
        return monitor, [monitor.session_state(s) for s in monitor.sessions][0]

    def test_a_session_in_the_hosts_terminal_is_hosted(self):
        _, state = self.state_of({101: "term-1"})
        self.assertEqual((state["hosted"], state["tmux"], state["provider"]), (True, "term-1", "copilot"))
        _, state = self.state_of({})
        self.assertEqual((state["hosted"], state["tmux"]), (False, None))

    def test_the_mode_is_read_from_the_screen(self):
        self.screen = screen_of("copilot-autopilot.txt")
        _, state = self.state_of({101: "term-1"})
        self.assertEqual(state["mode"], "Autopilot · allow all")
        monitor, _ = self.state_of({101: "term-1"})
        self.screen = screen_of("copilot-working.txt")  # No footer while it works: what was read stays.
        monitor.copilot_modes["cop"] = (0, monitor.copilot_modes["cop"][1])
        self.assertEqual(monitor.session_state(monitor.sessions[0])["mode"], "Autopilot · allow all")

    def test_a_plain_session_shows_no_mode(self):
        _, state = self.state_of({101: "term-1"})
        self.assertEqual(state["mode"], "")

    def waiting(self):
        folder = os.path.join(self.state, "cop")
        with open(os.path.join(folder, "events.jsonl"), "w") as handle:
            for n, (kind, data) in enumerate([("assistant.turn_start", {}),
                                              ("permission.requested", {"requestId": "r", "permissionRequest": {"kind": "shell", "intention": "Write a file"}})]):
                handle.write(json.dumps({"type": kind, "data": data, "timestamp": f"2026-11-20T15:10:0{n}.000Z"}) + "\n")
        self.screen = screen_of("copilot-permission.txt")

    def test_a_prompt_is_tied_to_when_it_was_raised(self):
        self.waiting()
        monitor, state = self.state_of({101: "term-1"})
        first = state["attention"]["at"]
        monitor.cached = None
        self.assertEqual(monitor.session_state(monitor.sessions[0])["attention"]["at"], first)  # Stable while it waits.

    def test_approving_picks_yes_by_moving_to_it(self):
        self.waiting()
        picked = []
        monitor, state = self.state_of({101: "term-1"})
        app.hosted_session = lambda m, request: state
        app.choose_answer = lambda target, answer, text=None, by_number=True, press=True: picked.append((target, answer["label"], by_number)) or True
        result = app.press_key(monitor, {"id": "cop", "action": "approve", "prompt": state["attention"]["at"]})
        self.assertEqual(result, (True, "Sent."))
        self.assertEqual(picked, [("term-1", "Yes", False)])

    def test_denying_and_interrupting(self):
        self.waiting()
        monitor, state = self.state_of({101: "term-1"})
        app.hosted_session = lambda m, request: state
        self.assertEqual(app.press_key(monitor, {"id": "cop", "action": "deny", "prompt": state["attention"]["at"]}), (True, "Sent."))
        self.assertEqual(self.keys[-1], ("term-1", ("Escape",)))
        busy = {**state, "status": "busy", "attention": None}
        app.hosted_session = lambda m, request: busy
        self.assertEqual(app.press_key(monitor, {"id": "cop", "action": "interrupt"}), (True, "Sent."))
        self.assertEqual(self.keys[-1], ("term-1", ("C-c",)))

    def start(self, **request):
        sent = {}
        app.new_terminal = lambda session_id, cwd, command: sent.update(id=session_id, cwd=cwd, command=command) or (False, "stop here")
        result = app.start_session(app.Monitor(), {"provider": "copilot", "cwd": self.state, "prompt": "do it", **request})
        return result, sent

    def test_start_checks_its_request(self):
        self.assertFalse(app.start_session(app.Monitor(), {"provider": "copilot", "cwd": "nowhere", "prompt": "x"})[0])
        self.assertEqual(app.start_session(app.Monitor(), {"provider": "copilot", "cwd": self.state, "prompt": " "}), (False, "Enter what the session should do."))
        self.assertIn("Permission mode", app.start_session(app.Monitor(), {"provider": "copilot", "cwd": self.state, "prompt": "x", "mode": "acceptEdits"})[1])

    @unittest.skipUnless(shutil.which("copilot"), "needs copilot on the PATH")
    def test_start_builds_copilots_command_line(self):
        (ok, message), sent = self.start(name="My & <work>|^%", mode="autopilot")
        self.assertFalse(ok)
        self.assertEqual(sent["command"][0], "copilot")
        self.assertEqual(sent["command"][1:3], ["--session-id", sent["id"]])
        self.assertIn("--allow-all", sent["command"])
        self.assertIn("--autopilot", sent["command"])
        name = sent["command"][sent["command"].index("--name") + 1]
        self.assertEqual(name, "My  work")  # Nothing a command line could read as more.
        (_, _), plain = self.start()
        self.assertEqual(plain["command"][3:], [])  # Ask first: no flags at all, and the message is typed, not passed.

    @unittest.skipUnless(shutil.which("copilot"), "needs copilot on the PATH")
    def test_an_untrusted_folder_asks_before_anything_is_accepted(self):
        killed, started = [], []

        def new_terminal(session_id, cwd, command):
            started.append(session_id)
            return True, ""

        app.new_terminal = new_terminal
        app.hosted_sessions = lambda: started  # It is running, and showing the trust question.
        app.kill_terminal = lambda name: killed.append(name) or True
        self.screen = screen_of("copilot-trust.txt")
        ok, answer = app.start_session(app.Monitor(), {"provider": "copilot", "cwd": self.state, "prompt": "x"})
        self.assertFalse(ok)
        self.assertEqual((answer["needsTrust"], answer["cwd"]), (True, self.state))
        self.assertEqual(killed, started)  # Nothing was accepted, and nothing is left waiting.

    def test_a_window_of_its_own_resumes_with_copilot_not_claude(self):
        opened = []
        folder = tempfile.mkdtemp(prefix="copilot-folder-")
        self.addCleanup(shutil.rmtree, folder, True)
        saved = app.kill_terminal, app.subprocess.Popen
        app.kill_terminal = lambda name: True
        app.subprocess.Popen = lambda command, *a, **k: opened.append(command)
        self.addCleanup(lambda: (setattr(app, "kill_terminal", saved[0]), setattr(app.subprocess, "Popen", saved[1])))
        session = {"id": "abc-123", "tmux": "term", "cwd": folder, "status": "idle", "provider": "copilot"}
        self.assertTrue(app.hand_to_console(app.Monitor(), session)[0])
        self.assertEqual(opened, [["cmd.exe", "/k", "copilot", "--resume=abc-123"]])

    def test_the_page_offers_both_programs(self):
        self.assertIn("$('histRefresh').hidden=!s.hosted||s.provider==='copilot'", app.PAGE)  # Fresh start is Claude Code's.
        for expected in ('id="newProg"', "const NEW_MODES=", "provider:$('newProg').value", 'id="histAllow"', "'/allow-all'", "'/autopilot'"):
            self.assertIn(expected, app.PAGE)


class CopilotAskTests(unittest.TestCase):
    """Copilot's /ask, which answers a side question in a panel, as Claude Code's /btw does."""

    def setUp(self):
        self.saved = {n: getattr(app, n) for n in ("session_screen", "send_keys", "resize_terminal", "restore_terminal", "hosted_sessions", "hosted_session", "send_btw_now", "BTW_FILE")}
        app.BTW_FILE = os.path.join(tempfile.mkdtemp(prefix="ask-"), "commands.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(app.BTW_FILE), True)
        self.keys, self.screen = [], screen_of("copilot-idle.txt")
        app.session_screen = lambda target: self.screen
        app.send_keys = lambda target, *keys: self.keys.append(keys) or True
        app.hosted_sessions = lambda: ["term"]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(app, name, value)

    def test_each_state_of_the_panel(self):
        thinking = app.copilot_ask_panel(screen_of("copilot-ask-thinking.txt"))
        self.assertEqual((thinking["question"], thinking["done"], thinking["answer"]), ("what is 3 plus 3? answer in one word", False, ""))
        streaming = app.copilot_ask_panel(screen_of("copilot-ask-streaming.txt"))
        self.assertFalse(streaming["done"])  # Lines are arriving, but it isn't finished.
        done = app.copilot_ask_panel(screen_of("copilot-ask-done.txt"))
        self.assertEqual((done["done"], done["failed"], done["answer"]), (True, False, "Six"))

    def test_an_error_is_told_from_an_answer(self):
        failed = app.copilot_ask_panel(screen_of("copilot-ask-failed.txt"))
        self.assertEqual((failed["done"], failed["failed"]), (True, True))
        self.assertTrue(failed["answer"].startswith("Auto routing is unavailable"))

    def test_a_long_answer_is_cut_until_expanded(self):
        collapsed = app.copilot_ask_panel(screen_of("copilot-ask-collapsed.txt"))["answer"].splitlines()
        self.assertEqual((len(collapsed), collapsed[0].strip(), collapsed[-1].strip()), (10, "31. Passion fruit", "40. Kumquat"))  # The scrollbar isn't text.
        expanded = app.copilot_ask_panel(screen_of("copilot-ask-expanded.txt"))["answer"].splitlines()
        self.assertEqual((len(expanded), expanded[-1].strip()), (34, "40. Watermelon"))

    def test_other_screens_have_no_panel(self):
        for name in ("copilot-idle.txt", "copilot-working.txt", "copilot-permission.txt", "copilot-question.txt"):
            self.assertIsNone(app.copilot_ask_panel(screen_of(name)), name)
        self.assertIsNone(app.command_panel(screen_of("copilot-ask-done.txt")))  # Not one of Claude Code's panels.

    def test_the_side_question_command_depends_on_the_program(self):
        copilot, claude = {"provider": "copilot"}, {"provider": "claude"}
        self.assertEqual(app.side_question(copilot, "/ask  what   is this?"), "what is this?")
        self.assertIsNone(app.side_question(copilot, "/btw what is this?"))
        self.assertEqual(app.side_question(claude, "/btw what is this?"), "what is this?")
        self.assertIsNone(app.side_question(claude, "/ask what is this?"))
        self.assertIsNone(app.side_question(copilot, "/ask"))

    def test_a_question_is_noted_with_the_command_it_used(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask why?")
        app.note_btw({"id": "c2", "tmux": "term2", "provider": "claude"}, "/btw why?")
        app.note_btw({"id": "c3", "tmux": "term3", "provider": "claude"}, "/ask nothing")  # Not a command for Claude Code.
        self.assertEqual(app.btw_for("c1")[0]["verb"], "/ask")
        self.assertEqual(app.btw_for("c2")[0]["verb"], "/btw")
        self.assertEqual(app.btw_for("c3"), [])

    def test_a_busy_session_answers_it_without_being_interrupted(self):
        sent = []
        app.send_btw_now = lambda session, text: sent.append(text) or (True, "Sent.")
        busy = {"id": "c1", "tmux": "term", "provider": "copilot", "status": "busy"}
        app.hosted_session = lambda monitor, request: busy
        self.assertEqual(app.send_reply(None, {"id": "c1", "text": "/ask how is it going?"}), (True, "Sent."))
        self.assertEqual(sent, ["/ask how is it going?"])
        # For Claude Code that isn't a side question, so it waits its turn.
        app.hosted_session = lambda monitor, request: {**busy, "provider": "claude"}
        self.assertEqual(app.send_reply(None, {"id": "c1", "text": "/ask how is it going?"}), (True, "Queued."))
        self.assertEqual(sent, ["/ask how is it going?"])

    def watch(self, screens):
        """Run the watcher once with the screen showing each of `screens` in turn."""
        shown = iter(screens)
        last = [None]

        def session_screen(target):
            last[0] = next(shown, last[0])
            return last[0]

        app.session_screen = session_screen
        app.watch_btw(None)

    def test_the_answer_is_read_and_the_panel_closed(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask what is 3 plus 3? answer in one word")
        sizes = []
        app.resize_terminal = lambda session_id, columns, rows: sizes.append((columns, rows)) or (120, 40)
        app.restore_terminal = lambda session_id, size: sizes.append(size)
        done = screen_of("copilot-ask-done.txt")
        self.watch([done, done, done, done, done])
        (entry,) = app.btw_for("c1")
        self.assertEqual((entry["state"], entry["answer"]), ("answered", "Six"))
        self.assertIn(("C-e",), self.keys)  # Expanded, to read all of it.
        self.assertIn(("Escape",), self.keys)  # And closed, so the input box shows again.
        self.assertEqual(sizes[-1], (120, 40))  # The terminal was put back as it was.

    def test_a_long_answer_is_read_whole(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask list 40 different fruits, one per line, numbered, no other text")
        app.resize_terminal = lambda session_id, columns, rows: (120, 40)
        app.restore_terminal = lambda session_id, size: None
        self.watch([screen_of("copilot-ask-collapsed.txt"), screen_of("copilot-ask-expanded.txt"), screen_of("copilot-ask-expanded.txt"),
                    screen_of("copilot-ask-expanded.txt"), screen_of("copilot-ask-expanded.txt")])
        (entry,) = app.btw_for("c1")
        self.assertEqual(entry["state"], "answered")
        self.assertEqual(len(entry["answer"].splitlines()), 34)

    def test_an_answer_still_coming_is_waited_for(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask what is 3 plus 3? answer in one word")
        self.watch([screen_of("copilot-ask-thinking.txt")])
        self.assertEqual(app.btw_for("c1")[0]["state"], "answering")
        self.assertNotIn(("Escape",), self.keys)  # Esc now would cancel it.

    def test_a_question_that_isnt_the_one_sent_is_left_alone(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask something else entirely, so different")
        self.watch([screen_of("copilot-ask-done.txt")])
        self.assertEqual(app.btw_for("c1")[0]["state"], "answering")
        self.assertEqual(self.keys, [])

    def test_a_failed_question_shows_why(self):
        app.note_btw({"id": "c1", "tmux": "term", "provider": "copilot"}, "/ask what is 2 plus 2? answer in one word")
        app.resize_terminal = lambda *a: None  # Not resizable: what is showing is used.
        failed = screen_of("copilot-ask-failed.txt")
        self.watch([failed, failed, failed])
        (entry,) = app.btw_for("c1")
        self.assertEqual(entry["state"], "answered")
        self.assertIn("Auto routing is unavailable", entry["answer"])

    def test_nothing_is_typed_while_the_panel_is_open(self):
        self.screen = screen_of("copilot-ask-thinking.txt")
        self.assertTrue(app.panel_open({"tmux": "term"}))
        self.screen = screen_of("copilot-idle.txt")
        self.assertFalse(app.panel_open({"tmux": "term"}))

    def test_the_session_counts_as_having_a_panel_open_and_nothing_is_kept_of_it(self):
        monitor = types.SimpleNamespace(lock=threading.Lock(), panels={}, drafts={}, snapshot=lambda: {"error": "", "sessions": [
            {"id": "c1", "hosted": True, "status": "idle", "tmux": "term", "waitingFor": "", "provider": "copilot"}]})
        self.screen = screen_of("copilot-ask-thinking.txt")
        app.watch_panels(monitor)
        self.assertTrue(monitor.panels.get("c1"))
        self.screen = screen_of("copilot-idle.txt")
        app.watch_panels(monitor)
        self.assertEqual(monitor.panels, {})
        self.assertEqual(app.btw_for("c1"), [])  # A side question has its own entry, so it isn't saved as a panel too.

    def test_the_host_can_expand_and_resize(self):
        self.assertEqual(app.TERMINAL_KEYS["C-e"], "\x05")

    def test_the_page_names_the_command_it_was_asked_with(self):
        self.assertIn("(q.verb||'/btw')+' '+q.question", app.PAGE)
        self.assertIn("Side questions (/ask)", app.PAGE)
