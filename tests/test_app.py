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

    def test_hook_records_a_finished_turn(self):
        result, monitor = self.run_program("--hook", {
            "session_id": "abc-123", "hook_event_name": "Stop", "cwd": "C:/Users/Zoë/project", "last_assistant_message": "Done – all good"})
        with open(os.path.join(monitor, "attention", "abc-123.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["kind"], "done")


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
        self.assertEqual(settings["statusLine"]["command"], "mine")
        for event in app.HOOK_EVENTS:
            ours = [h for g in settings["hooks"][event] for h in g["hooks"] if "--hook" in h["command"]]
            self.assertEqual(len(ours), 1, event)
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "other --thing")
        context = [h for g in settings["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "--context" in h["command"]]
        self.assertEqual(len(context), 1)
        self.assertNotIn("async", context[0])  # Its output has to reach Claude.

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
        self.assertEqual(history.messages[-1], {"role": "system", "note": True, "text": "Kept model as Sonnet 5.5", "seq": len(history.messages)})
        self.assertFalse(any(m["role"] == "you" and "local-command" in m["text"] for m in history.messages))

    def test_current_model(self):
        path = self.transcript([self.reply("claude-opus-5-5"),
                                self.said("<local-command-stdout>Set model to `Sonnet 5.5`</local-command-stdout>")])
        transcript = app.Transcript("t", path)
        transcript.poll()
        self.assertEqual(transcript.model, "Sonnet 5.5")


if __name__ == "__main__":
    unittest.main()
