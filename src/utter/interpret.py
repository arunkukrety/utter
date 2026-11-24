"""The interpreter (Linux for now): talk across languages in any call.

Two lanes run while it is on, each cutting live audio into sentences and sending each one to the Utter server
(mode=interpret: Whisper hears it, a fast model translates it, ElevenLabs speaks it):
  them -> you: what your speakers play (the call) is spoken to you in English, the call turned down underneath.
  you -> them: your microphone is spoken in the language they have been speaking (Chinese until they speak), into a
               virtual microphone, "Utter Microphone", made the default input so the call app hears it.
Both lanes stop listening while a translated voice plays, so neither hears the other's output. Headphones are
needed: on speakers your mic hears them too. Turning it off, quitting, or the next start after a crash puts your own
microphone back.
"""
import array, json, math, os, queue, subprocess, threading, time

import websockets.sync.client

from . import core

SINK, SOURCE = "utter_voice", "utter_mic"
RESTORE = os.path.join(core.DATA, "interpreter-mic")  # the default mic to put back, while the interpreter is on
FRAME = 640  # 20 ms of 16 kHz 16-bit mono
START_FRAMES, END_FRAMES, MAX_FRAMES = 3, 30, 500  # speech starts after 60 ms, ends after a 0.6 s pause, cut at 10 s
PRE_ROLL = 15  # 0.3 s kept from before the start, so the first syllable is not clipped
MIN_SPEECH = 15  # under 0.3 s of speech is a click or a cough: not sent
QUIET_AFTER_S = 0.3  # listening resumes this long after a translated voice stops


def _pactl(*args) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def cleanup() -> None:
    """Remove the virtual microphone and put the user's own back: after an off, or at start after a crash."""
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) > 2 and (f"sink_name={SINK}" in parts[2] or f"source_name={SOURCE}" in parts[2]):
            _pactl("unload-module", parts[0])
    try:
        with open(RESTORE) as f:
            mic = f.read().strip()
        if mic:
            _pactl("set-default-source", mic)
        os.remove(RESTORE)
    except OSError:
        pass


def _level(chunk: bytes) -> float:
    samples = array.array("h", chunk)
    return math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))


class Lane:
    """One direction: capture -> sentences -> server -> a voice played somewhere."""

    def __init__(self, session, name: str, device: str, play_to) -> None:
        self.session, self.name, self.device, self.play_to = session, name, device, play_to
        self.sentences, self.voices = queue.Queue(), queue.Queue()
        self.proc = None

    def start(self) -> None:
        self.proc = subprocess.Popen(["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le",
                                      f"--device={self.device}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for target in (self._listen, self._send, self._play):
            threading.Thread(target=target, name=f"interpret-{self.name}", daemon=True).start()

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
        self.sentences.put(None)
        self.voices.put(None)

    def _listen(self) -> None:
        """Cut sentences by loudness: the room's own noise is tracked slowly, speech is clearly above it."""
        floor, before, current, speech, quiet, loud_run = 300.0, [], [], 0, 0, 0
        while chunk := self.proc.stdout.read(FRAME):
            if self.session.voice_playing():  # a translated voice is out loud: hear nothing, start afresh after it
                before, current, speech, quiet, loud_run = [], [], 0, 0, 0
                continue
            level = _level(chunk)
            floor = min(level, floor * 1.002) if level < floor * 3 else floor * 1.0005  # creeps up, drops at once
            loud = level > max(floor * 2.5, 250)
            if not current:
                before = (before + [chunk])[-PRE_ROLL:]
                loud_run = loud_run + 1 if loud else 0
                if loud_run >= START_FRAMES:
                    current, speech, quiet = list(before), loud_run, 0
                continue
            current.append(chunk)
            speech += loud
            quiet = 0 if loud else quiet + 1
            if quiet >= END_FRAMES or len(current) >= MAX_FRAMES:
                if speech >= MIN_SPEECH:
                    self.sentences.put(b"".join(current))
                before, current, speech, quiet, loud_run = [], [], 0, 0, 0

    def _send(self) -> None:
        """One connection for the whole session; sentences go one at a time, in order."""
        ws = None
        while (pcm := self.sentences.get()) is not None:
            ogg = subprocess.run([core.FFMPEG, "-v", "error", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                                  "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"],
                                 input=pcm, capture_output=True).stdout
            for attempt in (1, 2):
                try:
                    if ws is None:
                        ws = websockets.sync.client.connect(
                            (core.setting("server_url") or core.SERVER_URL) + "?mode=interpret",
                            additional_headers={"Authorization": f"Bearer {core.read_token()}"}, max_size=None,
                            open_timeout=10)
                    to = "English" if self.name == "them" else self.session.their_language()
                    ws.send(json.dumps({"to": to, "lane": self.name}))
                    ws.send(ogg)
                    ws.send("end")
                    reply = json.loads(ws.recv(timeout=20))
                    pcm_out = ws.recv(timeout=20) if reply.get("audio") else b""
                    break
                except Exception as exc:
                    core.LOG(f"interpret {self.name}: {type(exc).__name__} {str(exc)[:100]}")
                    try:
                        ws and ws.close()
                    except Exception:
                        pass
                    ws, reply, pcm_out = None, {}, b""
            if reply.get("notice"):
                core.notify(reply["notice"])
                self.session.stop()
                return
            if reply.get("heard"):
                core.LOG(f"interpret {self.name} ({reply.get('language')}): {reply['heard']} -> {reply['text']}")
                if self.name == "them":
                    self.session.heard_language(reply.get("language", ""))
            if pcm_out:
                self.voices.put((pcm_out, reply.get("rate", 24000)))
        if ws is not None:
            ws.close()

    def _play(self) -> None:
        while (item := self.voices.get()) is not None:
            pcm, rate = item
            self.session.playing(+1, duck=self.name == "them")
            try:
                subprocess.run(["pacat", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le",
                                *self.play_to], input=pcm, stderr=subprocess.DEVNULL)
            finally:
                self.session.playing(-1, duck=self.name == "them")


class Interpreter:
    def __init__(self) -> None:
        self.lanes, self.on = [], False
        self.language = ""  # what they have been speaking, as Whisper names it
        self.voices, self.quiet_until, self.ducker = 0, 0.0, None
        self.lock = threading.Lock()
        self.on_state = lambda on: None  # told True/False when it starts or stops; may be called from any thread

    def their_language(self) -> str:
        return self.language.title() if self.language else "Chinese"

    def heard_language(self, language: str) -> None:
        if language and language not in ("english", "hindi") and language != self.language:
            self.language = language
            core.LOG(f"interpret: they speak {language}; your voice goes out in {self.their_language()}")

    def voice_playing(self) -> bool:
        return self.voices > 0 or time.monotonic() < self.quiet_until

    def playing(self, change: int, duck: bool) -> None:
        with self.lock:
            self.voices += change
            if change > 0 and duck and self.ducker is None and hasattr(core.plat, "Ducker"):
                self.ducker = core.plat.Ducker(0.25)  # the call goes quiet under their translated voice
            if self.voices == 0:
                self.quiet_until = time.monotonic() + QUIET_AFTER_S
                if self.ducker is not None:
                    self.ducker.release()
                    self.ducker = None

    def start(self) -> None:
        cleanup()
        mic = _pactl("get-default-source")
        from .plat import linux
        chosen = linux._mic_args(core.setting("mic"))  # the mic chosen in Settings, else the default as it was
        own = chosen[0].split("=", 1)[1] if chosen else mic
        _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
               "sink_properties=device.description=Utter-voice")
        _pactl("load-module", "module-remap-source", f"master={SINK}.monitor", f"source_name={SOURCE}",
               "source_properties=device.description=Utter-Microphone")
        with open(RESTORE, "w") as f:
            f.write(mic)
        _pactl("set-default-source", SOURCE)  # call apps on the default mic now hear the translated voice
        self.lanes = [Lane(self, "them", "@DEFAULT_MONITOR@", []),
                      Lane(self, "me", own, [f"--device={SINK}"])]
        for lane in self.lanes:
            lane.start()
        self.on = True
        core.LOG(f"interpret: on (your mic {own}; the call's mic is now Utter Microphone)")
        core.notify("Interpreter on. Wear headphones. In your call app, choose \"Utter Microphone\" as the mic "
                    "if it did not switch by itself.")
        self.on_state(True)

    def stop(self) -> None:
        if not self.on:
            return
        self.on = False
        for lane in self.lanes:
            lane.stop()
        self.lanes = []
        cleanup()
        with self.lock:
            if self.ducker is not None:
                self.ducker.release()
                self.ducker = None
        core.LOG("interpret: off")
        core.notify("Interpreter off. Your own microphone is back.")
        self.on_state(False)

    def toggle(self) -> bool:
        self.stop() if self.on else self.start()
        return self.on


INTERPRETER = Interpreter()
