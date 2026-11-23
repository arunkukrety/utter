"""Meeting notes (Linux for now): record a call, then turn it into a transcript and notes.

Two tracks are recorded side by side: your mic ("You") and what your speakers play ("Others", PipeWire's monitor of
the default output). Each is encoded to Opus and cut into one-minute files on disk while the meeting runs, so a crash
loses at most a minute. When you stop, each track is joined into parts of up to 25 minutes and transcribed by the
Utter server (mode=meeting: ElevenLabs Scribe through the Surplus key market, with speaker labels on the others' track),
the lines are merged by their real timestamps, and the server writes notes from that (mode=notes). Transcript and notes are saved as Markdown in DATA/meetings/<when>/ and the notes are opened; the audio
is deleted once the notes exist. The server allows this only on plans with meeting notes (Pro) and for the owner key.

Headphones work best: on speakers your mic also hears the others, and their words then show up under "You" too.
"""
import asyncio, glob, json, os, re, subprocess, threading, time

import websockets

from . import core

SEGMENT_S = 60
TRACKS = {"you": "You", "others": "Others"}
PART_MINUTES = 25  # minutes joined into one transcription request (the server's speech-to-text takes up to 30)
PARALLEL = 4  # parts transcribed at once


def _join(paths: list) -> bytes:
    """One-minute Opus files -> one Ogg Opus stream, in order."""
    lst = os.path.join(os.path.dirname(paths[0]), ".join.txt")
    with open(lst, "w") as f:
        f.writelines("file '%s'\n" % p.replace("'", "'\\''") for p in paths)
    try:
        return subprocess.run([core.FFMPEG, "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c:a", "libopus",
                               "-b:a", "16k", "-application", "voip", "-f", "ogg", "pipe:1"], capture_output=True).stdout
    finally:
        os.remove(lst)


class Meeting:
    def __init__(self) -> None:
        self.dir, self.started, self.procs = None, None, []
        # told "rec" (recording), "writing" (stopped, notes on their way) and "done"; may be called from any thread
        self.on_state = lambda state: None

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
        self.on_state("rec")

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
        self.on_state("writing")

        def finish() -> None:
            try:
                asyncio.run(_finish(folder, started))
            finally:
                self.on_state("done")

        threading.Thread(target=finish, name="meeting", daemon=True).start()

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

        async def one(track: str, index: int, minutes: list):
            """One part (up to PART_MINUTES one-minute files) of one track, joined, -> (start offset s, track, segments)."""
            offset = int(re.search(r"-(\d+)\.ogg$", minutes[0]).group(1)) * SEGMENT_S
            audio = _join(minutes)
            if len(audio) < 2000:  # nothing recorded
                return offset, track, []
            q = f"&mode=meeting&diarize={1 if track == 'others' else 0}"  # speakers are told apart on the others' track
            async with gate:
                for attempt in (1, 2):
                    try:
                        reply = await _ask(url + q, token, audio, 300)
                        if reply.get("notice"):
                            raise PermissionError(reply["notice"])
                        if reply.get("error"):
                            raise RuntimeError(reply["error"])
                        return offset, track, reply.get("segments") or []
                    except PermissionError:
                        raise
                    except Exception as exc:
                        if attempt == 2:
                            core.LOG(f"meeting: {track} part {index + 1} failed ({str(exc)[:100]})")
                            return offset, track, [{"speaker": None, "start": 0, "end": 0,
                                                    "text": "[this part could not be transcribed]"}]

        jobs = []
        for track in TRACKS:
            minutes = sorted(f for f in files if os.path.basename(f).startswith(track + "-"))
            for i in range(0, len(minutes), PART_MINUTES):
                jobs.append(one(track, i // PART_MINUTES, minutes[i:i + PART_MINUTES]))
        lines = []
        for offset, track, segs in await asyncio.gather(*jobs):
            for s in segs:
                who = "You" if track == "you" else (s.get("speaker") or "Others")
                lines.append((offset + s["start"], f"{who}: {s['text']}"))
        lines.sort(key=lambda x: x[0])
        lines = [f"[{time.strftime('%H:%M:%S', time.localtime(started + at))}] {text}" for at, text in lines]
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
