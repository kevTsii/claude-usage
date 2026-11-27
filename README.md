# Claude Usage

A small GNOME app to keep an eye on your Claude plan usage without going back to claude.ai
(Settings > Usage): a compact window and an indicator in the top bar.

> Personal project, not affiliated with Anthropic. It relies on an undocumented endpoint
> (see [Limitations](#limitations)). The interface is in French.

## Features

- **GTK4 / libadwaita window** that mirrors the web page:
  - current session (5 h) and weekly usage, with their reset times;
  - a pace message ("On track..." or a warning if the weekly limit will be reached before the
    reset);
  - monthly spend limit (credits);
  - weekly usage by product (Claude Code, Chat, Cowork, Other);
  - last update time.
- **GNOME top bar indicator**: a ring and a percentage per value, for example
  `◯ 54 % · ◯ 48 %` (session, then week). A click opens a menu with the details, a
  double-click opens the window.
- **Same colors as the web interface**: green, orange from 75 %, red from 90 %.
- **One-click sign-in**: the button runs `claude auth login` and opens the browser, just like
  `/login` in Claude Code.
- **"Start with Ubuntu" checkbox** (in the window menu and the tray menu) to launch the app at
  login, minimized to the top bar.
- **Console mode** (`--text`) for a one-off print in the terminal.

## Requirements

- Ubuntu with GNOME (developed and tested on Wayland: Python 3.12, GTK 4.14, libadwaita 1.5).
- [Claude Code](https://claude.com/claude-code) installed and signed in with a Claude plan
  (tested with Pro). The app reuses its session and never asks for credentials.
- The GNOME "AppIndicator" extension for the top bar icon (enabled by default on Ubuntu).
  Without it, the app still works with the window alone.

System dependencies: `python3-gi`, `python3-cairo`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`.

## Installation

Build the Debian package, then install it:

```bash
./build-deb.sh
sudo apt install ./dist/claude-usage_1.0.0_all.deb
```

The leading `./` is required: without it, `apt` looks for a package in its repositories. The
app then shows up as "Utilisation Claude" in the applications grid, and the `claude-usage`
command is available. Once the package is installed, the project folder is no longer needed.

For another version: `./build-deb.sh 1.0.1`, then install the new file with `sudo apt install`,
which replaces the previous version.

### Run without installing

```bash
python3 claude_usage.py
```

## Usage

| Command                 | Effect                                            |
|-------------------------|---------------------------------------------------|
| `claude-usage`          | Window and top bar indicator                      |
| `claude-usage --hidden` | Starts minimized in the top bar, no window        |
| `claude-usage --text`   | Prints the usage in the terminal, once            |

- Closing the window minimizes it to the top bar; "Quitter" is in the indicator menu.
- The app is single-instance: running the command again brings back the existing window.
- Data is refreshed every 2 minutes and when the window regains focus (at least 30 seconds
  between two calls). The refresh button forces an update.
- Right-clicking the icon in the applications grid offers "Démarrer dans la barre" (start in
  the top bar).

### Signing in

The app reads the Claude Code session. The sign-in token lasts about 8 hours and is renewed by
Claude Code whenever you use it. If the session has expired, the app shows a screen with a
sign-in button: the browser opens and the app resumes by itself afterwards. You do not need to
start Claude Code.

## How it works and privacy

- Data comes from `GET https://api.anthropic.com/api/oauth/usage`, the same service behind the
  Usage page on claude.ai, called with the `anthropic-beta: oauth-2025-04-20` header.
- The token is read from `~/.claude/.credentials.json` (or from `$CLAUDE_CONFIG_DIR` if set).
  It stays in memory: the app does not display it, copy it or refresh it.
- No network request is made other than the one to `api.anthropic.com`. Nothing is sent
  anywhere else.
- The top bar indicator is an image generated in `~/.cache/claude-usage/` (old images are
  cleaned up automatically).

## Limitations

- **Undocumented endpoint**: it may change without notice and break the app.
- Not displayed, because the server response does not contain them: the "full reset" with its
  expiration date, and the "auto-reload" state. The "reset for free" button is deliberately not
  implemented. An "Ouvrir sur claude.ai" link points to the web page for these items.
- The top bar image is drawn for a display scale of 1: on a HiDPI screen it is scaled up and
  looks less sharp.
- Tested on Ubuntu with GNOME only.

## Uninstall

```bash
sudo apt remove claude-usage
```

If "Start with Ubuntu" was checked, uncheck it first, or delete
`~/.config/autostart/local.ClaudeUsage.desktop`. You can also delete `~/.cache/claude-usage/`.

## Project layout

| File               | Role                                                                    |
|--------------------|-------------------------------------------------------------------------|
| `claude_usage.py`  | The app: data access, GTK4 window, sign-in                              |
| `status_tray.py`   | Top bar indicator (StatusNotifierItem and DBus menu, no GTK3)           |
| `claude-usage.svg` | App icon                                                                |
| `build-deb.sh`     | Builds the Debian package into `dist/`                                  |
