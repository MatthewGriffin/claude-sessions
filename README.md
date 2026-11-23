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
It exits on its own once it has no sessions left. On Windows, **Open in terminal** ends the copy in
the app and resumes the session by id in a console window. Desktop notifications aren't built yet.

**Terminal to app:** `--setup` adds a `/to-app` skill. Typed in a terminal session, it runs
`claude-monitor --adopt`, which waits for the turn to end, closes the terminal copy and resumes the
same session inside the app.

## GitHub Copilot CLI sessions

Running `copilot` sessions are listed beside Claude's, each marked with a **Claude** or **Copilot** badge
(and a coloured edge on its card) so you can tell them apart at a glance, in the list and in the session
view. They are read from the files Copilot keeps in `~/.copilot` (or `$COPILOT_HOME`), so nothing needs
installing: you see what each one is doing, when it is waiting for a permission, a question or a plan review,
its model, context and mode, the conversation, the files it changed, its subagents, and any command it left
running in the background. `copilot` must be on your PATH to start or resume sessions from the app. Copilot's
log format and screen are internal, so fields may go blank after an update.

**Usage** is shown in the session you have open, not in the bar at the top: a Claude session shows the
account's 5-hour and 7-day limits, and a Copilot session shows what it has used itself (AI credits, requests,
tokens and lines changed), since Copilot doesn't report account limits to other programs.

**Started from the app** (**New session**, then **GitHub Copilot CLI**: ask first, plan first, allow everything,
or autopilot, with a model of your choice), a Copilot session can be driven like Claude's:

- Reply (with pictures, which Copilot attaches), queue messages, interrupt, **Send now**, rename, and end it.
  The first message is checked against Copilot's own log and typed again if it didn't arrive.
- Approve or deny a permission, review a plan, and answer its questions (including with text of your own).
  Switch on **Allow all** or **Autopilot**.
- The reply box offers Copilot's own commands and the skills it can run. `/ask` works as `/btw` does for Claude.
  The panels other commands open (`/model`, `/permissions`, `/session`, `/tasks`, `/rewind`, `/limits`, `/help`)
  are shown live with a button for each key their hint line offers, and what one showed is kept under Commands
  once it closes. Nothing is typed into the session while a panel is open; messages wait instead.
- **Fresh start** saves progress to the project's notes, clears the chat with `/clear`, and carries on from the
  notes. The session keeps its name and its queue across the new id.
- The **Context** tab shows how full its window is, and **Check now** types `/context` into the session to show
  what fills it.

**Earlier sessions** appear under Previous sessions and can be resumed (Copilot brings back the conversation,
model and permissions). Forgetting one only hides it from the list.

**Started in your own terminal**, a session is shown read-only. If it was started by the Copilot CLI and is
idle, **Move into app** ends the copy in its terminal and carries on with the same session in the app, as
`/to-app` does for Claude Code. Sessions of the Copilot app or an editor are never ended from here.

Not done for Copilot: the **started by** links between sessions (Claude's don't work on Windows either, and
Copilot sets nothing for them to read), and the guard that stops a session launching an emulator without
`-no-window`, which is a Claude Code hook.

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
