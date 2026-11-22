"""Meeting notes (Linux for now): record a call, then turn it into a transcript and notes.

Two tracks are recorded side by side: your mic ("You") and what your speakers play ("Others", PipeWire's monitor of
the default output). Each is encoded to Opus and cut into one-minute files on disk while the meeting runs, so a crash
loses at most a minute. When you stop, every minute of both tracks is transcribed by the Utter server (mode=meeting,
the plan's speech-to-text, no cleanup), the lines are merged by minute, and the server writes notes from that
(mode=notes). Transcript and notes are saved as Markdown in DATA/meetings/<when>/ and the notes are opened; the audio
is deleted once the notes exist. The server allows this only on plans with meeting notes (Pro) and for the owner key.

Headphones work best: on speakers your mic also hears the others, and their words then show up under "You" too.
"""
import asyncio, glob, json, os, re, subprocess, threading, time

import websockets

from . import core

SEGMENT_S = 60
TRACKS = {"you": "You", "others": "Others"}
PARALLEL = 4  # minutes transcribed at once


class Meeting:
    def __init__(self) -> None:
        self.dir, self.started, self.procs = None, None, []

    @property
    def recording(self) -> bool:
        return bool(self.procs)

    def start(self) -> None:
        self.started = time.time()
        self.dir = os.path.join(core.DATA, "meetings", time.strftime("%Y-%m-%d %H%M", time.localtime(self.started)))
        os.makedirs(self.dir, exist_ok=True)
        os.chmod(self.dir, 0o700)
        from .plat import linux
        mic = linux._mic_args(core.setting("mic"))  # the mic chosen in Settings, else the default
        for track, device in (("you", mic), ("others", ["--device=@DEFAULT_MONITOR@"])):
            rec = subprocess.Popen(["parecord", "--raw", f"--rate={core.RATE}", "--channels=1", "--format=s16le",
                                    *device], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            enc = subprocess.Popen([core.FFMPEG, "-v", "error", "-f", "s16le", "-ar", str(core.RATE), "-ac", "1",
                                    "-i", "pipe:0", "-c:a", "libopus", "-b:a", "16k", "-application", "voip",
                                    "-f", "segment", "-segment_time", str(SEGMENT_S), "-segment_format", "ogg",
                                    "-reset_timestamps", "1", os.path.join(self.dir, f"{track}-%04d.ogg")],
                                   stdin=rec.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rec.stdout.close()  # the encoder owns the pipe now
            self.procs += [rec, enc]
        core.LOG(f"meeting: recording to {self.dir}")
        core.notify("Recording meeting notes. Stop it from the Utter menu.")

    def stop(self) -> None:
        """Stop recording and write the notes in the background."""
        for p in self.procs[::2]:  # the recorders; each encoder then finishes its last file and exits
            p.terminate()
        for p in self.procs[1::2]:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []
        folder, started = self.dir, self.started
        core.notify("Meeting stopped. Writing the notes…")
        threading.Thread(target=lambda: asyncio.run(_finish(folder, started)), name="meeting", daemon=True).start()

    def toggle(self) -> bool:
        """Start or stop; True while recording."""
        self.stop() if self.recording else self.start()
        return self.recording


async def _ask(url: str, token: str, first, timeout: float) -> dict:
    """One request on its own connection: send `first` (audio bytes, then "end"; or a JSON text frame), read the reply."""
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=None,
                                  open_timeout=15) as ws:
        if isinstance(first, bytes):
            for i in range(0, len(first), 32768):
                await ws.send(first[i:i + 32768])
            await ws.send("end")
        else:
            await ws.send(first)
        return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _finish(folder: str, started: float) -> None:
    try:
        base = core.setting("server_url") or core.SERVER_URL
        language = "multi" if core.setting("language") == "multi" else "en"
        script = "devanagari" if core.setting("hindi") == "devanagari" else "roman"
        url = f"{base}?language={language}&script={script}"
        token = core.read_token()
        files = sorted(glob.glob(os.path.join(folder, "*-*.ogg")))
        gate = asyncio.Semaphore(PARALLEL)

        async def one(path: str):
            track, minute = re.match(r"(\w+)-(\d+)\.ogg$", os.path.basename(path)).groups()
            if os.path.getsize(path) < 2000:  # a near-empty last file
                return int(minute), track, ""
            async with gate:
                for attempt in (1, 2):
                    try:
                        reply = await _ask(url + "&mode=meeting", token, open(path, "rb").read(), 90)
                        if reply.get("notice"):
                            raise PermissionError(reply["notice"])
                        return int(minute), track, (reply.get("text") or "").strip()
                    except PermissionError:
                        raise
                    except Exception as exc:
                        if attempt == 2:
                            core.LOG(f"meeting: {os.path.basename(path)} failed ({str(exc)[:100]})")
                            return int(minute), track, "[this minute could not be transcribed]"

        parts = await asyncio.gather(*(one(f) for f in files))
        lines = []
        for minute, track, text in sorted(parts, key=lambda p: (p[0], p[1] != "you")):
            if text:
                stamp = time.strftime("%H:%M", time.localtime(started + minute * SEGMENT_S))
                lines.append(f"[{stamp}] {TRACKS[track]}: {text}")
        transcript = "\n".join(lines)
        when = time.strftime("%A %d %B %Y, %H:%M", time.localtime(started))
        with open(os.path.join(folder, "transcript.md"), "w") as f:
            f.write(f"# Meeting transcript — {when}\n\n" + (transcript or "_Nothing was transcribed._") + "\n")
        for attempt in (1, 2):  # a reply that never comes is retried once on a fresh connection
            try:
                reply = await _ask(url + "&mode=notes", token, json.dumps({"transcript": transcript}), 120)
                break
            except (asyncio.TimeoutError, OSError, websockets.ConnectionClosed) as exc:
                core.LOG(f"meeting: notes attempt {attempt} failed ({type(exc).__name__})")
                if attempt == 2:
                    raise
        if reply.get("notice") or reply.get("error"):
            raise RuntimeError(reply.get("notice") or reply["error"])
        notes_path = os.path.join(folder, "notes.md")
        with open(notes_path, "w") as f:
            f.write(f"# Meeting notes — {when}\n\n{reply['notes']}\n")
        for path in files:  # the notes exist: the recording is no longer needed
            os.remove(path)
        core.LOG(f"meeting: notes written ({len(lines)} transcript lines) to {notes_path}")
        core.notify("Meeting notes are ready.")
        subprocess.Popen(["xdg-open", notes_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except PermissionError as exc:
        core.notify(str(exc))
        core.LOG(f"meeting: refused ({exc}); the recording stays in {folder}")
    except Exception as exc:
        core.notify("Couldn't write the meeting notes. The recording is kept.")
        core.LOG(f"meeting: notes failed ({str(exc)[:200]}); the recording stays in {folder}")


MEETING = Meeting()
