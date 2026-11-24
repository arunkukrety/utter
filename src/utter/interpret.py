"""The interpreter (Linux for now): talk across languages in any call.

Two lanes run while it is on, each cutting live audio into sentences and sending each one to the Utter server
(mode=interpret: Whisper hears it, a fast model translates it, ElevenLabs speaks it):
  them -> you: what your speakers play (the call) is spoken to you in English, the call turned down underneath. By
               default this lane is live (Gemini Live Translate through the server, mode=live): the English comes while
               they are still talking, not after each sentence. Setting interpret_live=false goes back to sentences.
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
CALL = "utter_call"  # the call's sound goes through here: heard for translation, passed on to your speakers
RESTORE = os.path.join(core.DATA, "interpreter-mic")  # the default mic to put back, while the interpreter is on
RESTORE_OUT = os.path.join(core.DATA, "interpreter-speakers")  # and the default speakers
FRAME = 640  # 20 ms of 16 kHz 16-bit mono
START_FRAMES, END_FRAMES, MAX_FRAMES = 3, 30, 500  # speech starts after 60 ms, ends after a 0.6 s pause, cut at 10 s
PRE_ROLL = 15  # 0.3 s kept from before the start, so the first syllable is not clipped
MIN_SPEECH = 15  # under 0.3 s of speech is a click or a cough: not sent
QUIET_AFTER_S = 0.3  # listening resumes this long after a translated voice stops
PEEK_FRAMES = 40  # while a sentence is still being said, its words so far are shown every 0.8 s


def _pactl(*args) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout.strip()


def cleanup() -> None:
    """Remove the virtual microphone and put the user's own back: after an off, or at start after a crash."""
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) > 2 and any(f"{k}={v}" in parts[2] for k, v in (
                ("sink_name", SINK), ("source_name", SOURCE), ("sink_name", CALL), ("source", CALL + ".monitor"))):
            _pactl("unload-module", parts[0])
    for path, command in ((RESTORE, "set-default-source"), (RESTORE_OUT, "set-default-sink")):
        try:
            with open(path) as f:
                device = f.read().strip()
            if device:
                _pactl(command, device)
            os.remove(path)
        except OSError:
            pass


def _ogg(pcm: bytes) -> bytes:
    return subprocess.run([core.FFMPEG, "-v", "error", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                           "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"], input=pcm, capture_output=True).stdout


def _connect():
    return websockets.sync.client.connect((core.setting("server_url") or core.SERVER_URL) + "?mode=interpret",
                                          additional_headers={"Authorization": f"Bearer {core.read_token()}"},
                                          max_size=None, open_timeout=10)


# Gemini names languages by code; the sentence lane's translator and Whisper use names
LANGUAGES = {"zh": "chinese", "cmn": "chinese", "yue": "cantonese", "hi": "hindi", "en": "english", "es": "spanish",
             "fr": "french", "de": "german", "ja": "japanese", "ko": "korean", "ru": "russian", "pt": "portuguese",
             "ar": "arabic", "it": "italian", "ur": "urdu", "bn": "bengali", "ta": "tamil", "te": "telugu",
             "mr": "marathi", "gu": "gujarati", "pa": "punjabi", "tr": "turkish", "vi": "vietnamese",
             "id": "indonesian", "th": "thai", "nl": "dutch", "pl": "polish", "uk": "ukrainian", "fa": "persian"}


ENDS = set(".!?。！？।")



def _level(chunk: bytes) -> float:
    samples = array.array("h", chunk)
    return math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))


class Lane:
    """One direction: capture -> sentences -> server -> a voice played somewhere."""

    def __init__(self, session, name: str, device: str, play_to) -> None:
        self.session, self.name, self.device, self.play_to = session, name, device, play_to
        self.sentences, self.voices, self.peeks = queue.Queue(), queue.Queue(), queue.Queue()
        self.proc = None
        self.seq, self.done = 0, 0  # the sentence being said, and the last one whose final words are shown

    def start(self) -> None:
        self.proc = subprocess.Popen(["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le",
                                      f"--device={self.device}"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for target in (self._listen, self._send, self._play, self._peek):
            threading.Thread(target=target, name=f"interpret-{self.name}", daemon=True).start()

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
        self.sentences.put(None)
        self.voices.put(None)
        self.peeks.put(None)

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
                    self.seq += 1
                continue
            current.append(chunk)
            speech += loud
            quiet = 0 if loud else quiet + 1
            if quiet >= END_FRAMES or len(current) >= MAX_FRAMES:
                if speech >= MIN_SPEECH:
                    self.sentences.put((self.seq, b"".join(current)))
                before, current, speech, quiet, loud_run = [], [], 0, 0, 0
            elif len(current) % PEEK_FRAMES == 0 and speech >= MIN_SPEECH and quiet < 10:
                self.peeks.put((self.seq, b"".join(current)))

    def _peek(self) -> None:
        """The live subtitle: the sentence so far, on a connection of its own so the final words never wait for it.
        Only the newest snapshot is sent; one that is overtaken by its sentence's final words is dropped."""
        ws = None
        while (item := self.peeks.get()) is not None:
            while not self.peeks.empty():
                if (newer := self.peeks.get_nowait()) is None:
                    return ws and ws.close()
                item = newer
            seq, pcm = item
            if seq <= self.done:
                continue
            try:
                ws = ws or _connect()
                to = "English" if self.name == "them" else self.session.their_language()
                ws.send(json.dumps({"to": to, "lane": self.name}))
                ws.send(_ogg(pcm))
                ws.send("peek")
                reply = json.loads(ws.recv(timeout=10))
            except Exception as exc:
                core.LOG(f"interpret {self.name} peek: {type(exc).__name__} {str(exc)[:100]}")
                try:
                    ws and ws.close()
                except Exception:
                    pass
                ws = None
                continue
            if reply.get("heard") and seq > self.done:
                self.session.on_line(self.name, reply["heard"], "" if reply.get("same") else reply["text"], True)
        if ws is not None:
            ws.close()

    def _send(self) -> None:
        """One connection for the whole session; sentences go one at a time, in order."""
        ws = None
        while (item := self.sentences.get()) is not None:
            seq, pcm = item
            ogg = _ogg(pcm)
            for attempt in (1, 2):
                try:
                    if ws is None:
                        ws = _connect()
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
            self.done = seq
            if reply.get("heard"):
                core.LOG(f"interpret {self.name} ({reply.get('language')}): {reply['heard']} -> {reply['text']}")
                if self.name == "them":
                    self.session.heard_language(reply.get("language", ""))
                self.session.on_line(self.name, reply["heard"], "" if reply.get("same") else reply["text"], False)
            else:  # nothing worth showing after all: the live draft goes
                self.session.on_line(self.name, "", "", False)
            if pcm_out:
                self.voices.put((pcm_out, reply.get("rate", 24000)))
            elif reply.get("same") and self.name == "me":  # already in their language: they hear you, as you said it
                self.voices.put((pcm, 16000))
        if ws is not None:
            ws.close()

    def _play(self) -> None:
        # only a voice played to you, out loud, pauses listening: yours goes into the virtual mic, which nothing here
        # hears, and pausing for it dropped what you said next (2026-11-24 Discord test)
        audible = self.name == "them"
        while (item := self.voices.get()) is not None:
            pcm, rate = item
            if audible:
                self.session.playing(+1, duck=True)
            try:
                subprocess.run(["pacat", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le",
                                *self.play_to], input=pcm, stderr=subprocess.DEVNULL)
            finally:
                if audible:
                    self.session.playing(-1, duck=True)


class LiveLane:
    """Them -> you through Gemini Live Translate (server mode=live): the call's sound streams up as it plays, and the
    English voice and words come back while they are still talking, about a second behind them. Speech already in
    English is shown, not spoken again. The session is reopened when it ends (Google closes it every ~10 minutes)."""
    name = "them"

    def __init__(self, session, device: str, speakers: str) -> None:
        self.session, self.device, self.speakers = session, device, speakers
        self.running, self.proc, self.ws = False, None, None

    def start(self) -> None:
        self.running = True
        threading.Thread(target=self._run, name="interpret-live", daemon=True).start()

    def stop(self) -> None:
        self.running = False
        for close in (lambda: self.proc.terminate(), lambda: self.ws.close()):
            try:
                close()
            except Exception:
                pass

    def _run(self) -> None:
        while self.running:
            try:
                self._session()
            except Exception as exc:
                if self.running:
                    core.LOG(f"interpret live: {type(exc).__name__} {str(exc)[:100]}")
            finally:
                if self.proc:
                    self.proc.terminate()
            if self.running:
                time.sleep(1)

    def _feed(self, ws, proc) -> None:
        try:
            while chunk := proc.stdout.read(3200):  # 100 ms, as Google asks
                ws.send(chunk)
        except Exception:
            pass

    def _session(self) -> None:
        url = (core.setting("server_url") or core.SERVER_URL) + "?mode=live&to=en"
        self.ws = ws = websockets.sync.client.connect(url, max_size=None, open_timeout=10, additional_headers={
            "Authorization": f"Bearer {core.read_token()}"})
        with ws:
            first = json.loads(ws.recv(timeout=15))
            if first.get("notice"):
                core.notify(first["notice"])
                return self.session.stop()
            self.proc = subprocess.Popen(["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le",
                                          "--latency-msec=50", f"--device={self.device}"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            threading.Thread(target=self._feed, args=(ws, self.proc), daemon=True).start()
            player = subprocess.Popen(["pacat", "--raw", "--rate=24000", "--channels=1", "--format=s16le",
                                       f"--device={self.speakers}", "--latency-msec=60"], stdin=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
            heard = text = ""
            speaking, last_voice, last_heard, english = False, 0.0, 0.0, False
            try:
                while self.running:
                    try:
                        msg = ws.recv(timeout=0.3)
                    except TimeoutError:
                        msg = None
                    if speaking and time.monotonic() - last_voice > 0.6:  # its voice has stopped: the call comes back up
                        speaking = False
                        self.session.playing(-1, duck=True)
                    # a pause of a second, and the English has caught up: the line is finished
                    if (msg is None and heard.strip() and time.monotonic() - last_heard > 1.0
                            and heard.rstrip()[-1:] in ENDS and (english or text.rstrip()[-1:] in ENDS)):
                        self.session.on_line("them", heard.strip(), text.strip(), False)
                        core.LOG(f"interpret live: {heard.strip()} -> {text.strip()}")
                        heard = text = ""
                    if msg is None:
                        continue
                    if isinstance(msg, bytes):
                        if _level(msg) > 200:  # Google streams silence too: only real speech counts as its voice
                            if not speaking:
                                speaking = True
                                self.session.playing(+1, duck=True)
                            last_voice = time.monotonic()
                        player.stdin.write(msg)
                        player.stdin.flush()
                        continue
                    m = json.loads(msg)
                    if m.get("notice"):
                        core.notify(m["notice"])
                        return self.session.stop()
                    if m.get("heard"):
                        last_heard = time.monotonic()
                    heard += m.get("heard", "")
                    text += m.get("text", "")
                    if m.get("language"):
                        code = m["language"].split("-")[0].lower()
                        english = code == "en"  # nothing to translate: the line ends with what they said
                        self.session.heard_language(LANGUAGES.get(code, code))
                    # a line ends with the turn, or once both what they said and its English end a sentence
                    done = m.get("done") or len(heard) > 240 and heard.rstrip()[-1:] in ENDS
                    if heard.strip() or text.strip():
                        self.session.on_line("them", heard.strip(), text.strip(), not done)
                    if done:
                        if heard.strip():
                            core.LOG(f"interpret live: {heard.strip()} -> {text.strip()}")
                        heard = text = ""
            finally:
                if speaking:
                    self.session.playing(-1, duck=True)
                player.stdin.close()
                player.terminate()


class Interpreter:
    def __init__(self) -> None:
        self.lanes, self.on = [], False
        self.language = ""  # what they have been speaking, as Whisper names it
        self.voices, self.quiet_until, self.ducker = 0, 0.0, None
        self.lock = threading.Lock()
        self.on_state = lambda on: None  # told True/False when it starts or stops; may be called from any thread
        # each sentence, for the subtitles (english "" if no change); draft=True while it is still being said
        self.on_line = lambda lane, heard, english, draft: None

    def their_language(self) -> str:
        return self.language.title() if self.language else "Chinese"

    def heard_language(self, language: str) -> None:
        # they speak Hindi or English: what you say (Hindi, Hinglish or English) goes to them in English
        target = "english" if language in ("english", "hindi") else language
        if target and target != self.language:
            self.language = target
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
        live = core.setting("interpret_live") is not False  # Gemini Live Translate; False = the sentence-by-sentence lane
        if live:
            # the call plays into utter_call, which is heard for translation and passed on to your speakers; the
            # translated voice goes straight to the speakers, so it is never heard and translated again
            speakers = _pactl("get-default-sink")
            _pactl("load-module", "module-null-sink", f"sink_name={CALL}", "sink_properties=device.description=Utter-call")
            _pactl("load-module", "module-loopback", f"source={CALL}.monitor", f"sink={speakers}", "latency_msec=30",
                   "source_dont_move=true", "sink_dont_move=true")
            with open(RESTORE_OUT, "w") as f:
                f.write(speakers)
            _pactl("set-default-sink", CALL)
            them = LiveLane(self, f"{CALL}.monitor", speakers)
        else:
            them = Lane(self, "them", "@DEFAULT_MONITOR@", [])
        self.lanes = [them, Lane(self, "me", own, [f"--device={SINK}"])]
        for lane in self.lanes:
            lane.start()
        self.on = True
        core.LOG(f"interpret: on (live: {live}) (your mic {own}; the call's mic is now Utter Microphone)")
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
