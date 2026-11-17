<p align="center"><img src="data/utter.svg" width="96" alt="Utter icon"></p>

<h1 align="center">Utter</h1>

<p align="center">Push-to-talk dictation for Linux, macOS and Windows. Press a key, speak, and your words are pasted into whatever app you're in.</p>

---

- **Press, speak, press.** Alt+D (Option+D on a Mac) starts and stops a dictation, or hold Alt (Option) on its own, speak, and let go.
- **Clean text.** Filler words and false starts are dropped and your corrections applied ("five, no, six" → "six").
- **Nothing gets lost.** Every dictation is also copied to the clipboard and saved to a local History.
- **English, or English + Hindi.** In mixed mode it follows you between the two mid-sentence, and writes Hindi as Roman Hinglish ("kal meeting hai") or Devanagari, your choice.
- **Small and quiet.** A tiny pill at the bottom of the screen shows a recording dot and live voice bars.

Utter needs an **Utter key**: paste it in Settings the first time it opens.

## Install

**Ubuntu / Debian** (64-bit):

```bash
curl -fsSL https://arunkukrety.github.io/utter/setup.sh | sudo sh   # once: adds Utter's repository
sudo apt install utter
```

Updates then arrive with your normal `sudo apt upgrade`. Open **Utter** from your apps; it binds Alt+D (if nothing else uses it) and starts at every login.

**macOS** (Apple silicon): download `Utter-<version>-mac.dmg` from the [latest release](https://github.com/arunkukrety/utter/releases/latest), open it and drag Utter to Applications. The first time, right-click Utter and choose **Open**, then allow Microphone, Accessibility and Input Monitoring when macOS asks.

**Windows**: download `Utter-Setup-<version>.exe` from the [latest release](https://github.com/arunkukrety/utter/releases/latest) and run it. If SmartScreen warns, choose **More info → Run anyway**.

## Use

| | |
|---|---|
| **Alt+D** (⌥D) | start dictating; press again to stop |
| **Hold Alt** (⌥) | hold on its own for half a second, speak, release to finish |
| Utter icon in the top bar / menu bar / tray | History, copy the last dictation, Settings, quit |

**Settings** has your Utter key, the shortcut, hold to talk, start at login, the language and the microphone.

On Linux, hold-to-talk needs a one-time helper that watches the Alt key: `sudo /usr/lib/utter/install-hold.sh`.

## Privacy

- Audio is streamed over an encrypted connection to the Utter server while you dictate, and only then.
- The server keeps each dictation (the recording and its text, before and after cleanup) to improve accuracy.
- Your key, settings and History stay on your machine, readable only by you.

## Troubleshooting (Linux)

- **Nothing gets pasted.** Utter pastes through `ydotool`, which needs its service: `systemctl --user enable --now ydotool`. Your text is still on the clipboard either way.
- **No top-bar icon on GNOME.** Install the *AppIndicator and KStatusNotifierItem Support* extension. Ubuntu ships it enabled.
- **See what it's doing:** `journalctl --user -u utter -f` (macOS and Windows: `utter.log` in Utter's data folder).

## Uninstall

```bash
sudo apt remove utter
```

Your key, settings and History stay in `~/.config/utter` and `~/.local/share/utter`; delete those folders to remove them.

## Building

`.github/workflows/build.yml` builds the three installers (each on its own OS) and, for a `v*` tag, publishes them as a release and updates the apt repository. Locally: `pip install -r packaging/requirements.txt && pyinstaller packaging/utter.spec`, then `packaging/build-deb.sh` on Linux.

## License

MIT
