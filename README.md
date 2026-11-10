# Claude Sessions

A window that shows every Claude Code session running on your computer. You can watch them, reply to
them, answer their questions and permission prompts, queue messages for when they're free, and start
new ones. It also shows each session's changed files, subagents, usage, and any Android emulator or
browser a session is testing with.

It's one Python file (`claude-monitor`) that serves its page on `127.0.0.1` and opens it in a
Chromium-family browser's app window.

## Install

### Linux

Needs Python 3.10 or newer, `tmux`, Claude Code (`claude`) and Chromium, Chrome or Brave.

```sh
curl -fLo ~/.local/bin/claude-monitor https://github.com/MatthewGriffin/claude-sessions/releases/latest/download/claude-monitor
chmod +x ~/.local/bin/claude-monitor
claude-monitor --setup
```

`--setup` adds the app's hooks to `~/.claude/settings.json` (backing it up first), adds a status line if
you don't have one, and puts **Claude Sessions** in your applications menu.

Sessions can see the previews shown beside their chat (an emulator, a browser, a 3D printer camera):
`claude-monitor --preview` saves a current picture of each and prints where, and a hook tells Claude
about it the first time a preview is linked to its session.

### Windows

Needs Claude Code's native install (`claude.exe`) and Microsoft Edge, Chrome or Brave.

Download `claude-sessions-windows-<version>.exe` from the [latest release](https://github.com/MatthewGriffin/claude-sessions/releases/latest)
and open it. It runs from where it is, adds **Claude Sessions** to the Start menu, sets up Claude Code's
hooks and opens. A few seconds later it moves to the faster one-folder build of the same version
(`claude-sessions-windows-<version>.zip` in the release), which starts in about 0.2 s instead of a
second or more. That matters because Claude Code starts the program for every hook and status line.

**Update** unpacks the new version's folder beside it (or in `C:\installers`, if that folder exists, for
workplaces that only allow programs from there) and runs it. The new version points the Start menu and Claude Code's
hooks at itself, and deletes earlier versions once no session started before the update is still running.

Windows has no tmux, so sessions started from the app run in a small background session host
(the same program, with `--session-host`), which keeps them going while the app restarts or updates.
It exits on its own once it has no sessions left. Sessions started from the app can only be used
from the app; **Open in terminal** isn't available for them. Desktop notifications aren't built yet.

## 3D printer cameras

An Anycubic printer's built-in camera (tested on a Kobra X) can show beside sessions doing 3D printing
work. Turn on LAN mode in the printer's network settings, then add it under **Devices**, by its IP
address or with **Find on my network**. The camera is only on while you're watching it, and needs
`ffmpeg` installed. A session gets the camera when its commands or files involve slicing or printing
(`.gcode`, `.3mf`, `.stl`, slicers, the printer's address), when you mention 3D printing to it, or
when its name is about printing.

## Updates

When a newer release is out, an **Update to x.y.z** button appears at the top of the app. It downloads
the right version for your computer, checks it against the release's `SHA256SUMS`, replaces the app
and restarts it. Your sessions keep running. Copies not made by a release (version `dev`) never update
themselves.

## Other ways to run it

```text
claude-monitor                     terminal view (Linux)
claude-monitor --web [--port N]    browser view in your normal browser
claude-monitor --app               its own window
claude-monitor --once              print one snapshot
claude-monitor --version
```

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests start real terminals (tmux on Linux, the session host on Windows) running a stand-in
program (`tests/echo.js`, needs Node), and install updates from a stand-in for GitHub. They use their
own config folder, tmux socket and port, so they don't touch your sessions.

To release, push a tag such as `v1.2.3`. The **Build** workflow tests on Linux and Windows, writes the
version into the app, builds the Windows program with PyInstaller and publishes the release.

## Licence

MIT. See [LICENSE](LICENSE).
