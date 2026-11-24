# Utter — to do

Updated 2026-11-24. Done items move to the bottom.

## Decisions waiting
- [ ] **Meetings local-only:** saving to Supabase stays ON for now (Arun, 2026-11-24). Later: stop saving and drop the
      `meetings` table, so notes stay only in `~/.local/share/utter/meetings/` on the user's computer.
- [ ] **Saved dictations on the VPS:** turn off `UTTER_SAVE_DICTATIONS`, and decide whether to delete the ones already
      stored (permanent) or keep them.
- [ ] **Payment provider:** Paddle, Lemon Squeezy or Stripe; set Basic $3.99 and Pro $7.99 there.

## Launch
- [ ] **Landing page:** what Utter is (push-to-talk dictation, Hinglish, meeting notes with speakers), the plans and
      prices, download/install, privacy (what is and isn't kept).
- [ ] **Demo video:** for the landing page — dictating into a real app (Alt+D), the pill, music fading down while you
      talk, then a meeting (Alt+M, the shiny pill, notes opening).
- [ ] **Privacy policy and terms** (simpler once meetings and dictations are no longer stored).

## Code
- [ ] **Rename the app:** "Utter" is too common. Domains checked 2026-11-24 (/data/names): every-TLD-free picks
      bolkr, likhde, saybol (.com/.ai/.app/.io); likhdo, likhlo, kahdi, kahna (.ai/.app/.io). Check trademarks/app stores. Touches the package and command names, the systemd service, the GNOME
      shortcuts and the config folders (`~/.config/utter`, `~/.local/share/utter`), which need moving for existing users.
- [ ] **Payment webhook** (Supabase Edge Function): the provider's events -> `apply_subscription()` (built and tested),
      plus an upgrade checkout link. Needs the provider decision.
- [ ] **Website:** sign-up and login (Supabase Auth), keys (create, copy once, revoke), usage, plan and upgrade.
- [ ] **App: "Get a key" link** in Settings and a clearer first run.
- [ ] **Release fixes:** tagged builds that publish (`build.yml` runs on `v*` tags), and the update check if the repo
      stays private. 1.0.11 exists only on Arun's laptop.
- [ ] **Health alerts** to Discord: server down, failing usage/log writes, low Surplus or OpenRouter balance.
- [ ] **Provider privacy:** OpenRouter zero-data-retention, a stricter Surplus privacy level (requests go out with
      `x-si-privacy-level: none` today).
- [ ] Retention for saved dictations — only if saving stays on.

## Later
- [ ] **Interpreter, next steps** (v1 is built, 1.0.13): stream the voice so it starts sooner; live subtitles; a
      voice picker and your own cloned voice; echo cancelling so speakers work without headphones; pick their language by
      hand; an on-screen indicator; Mac and Windows. Ideas from github.com/wquguru/cicada (Apache-2.0, Python Linux app).
- [ ] Mac and Windows: meeting capture (ScreenCaptureKit / WASAPI loopback) and the volume fades.
- [ ] Keyterms for meetings (names like "SKD"), about $0.01/hour through Surplus.
- [ ] Per-app audio capture for meetings, so notification sounds and other apps stay out.
- [ ] Tune the unclear-transcript line (0.70, `UTTER_UNCLEAR_BELOW`) from `dictations.stt_confidence`.

## Not doing
- Paid emergency STT fallback: the backup stays Whisper on the free Groq key (OpenRouter rarely fails).

## Done (2026-11-22 to 24)
- Supabase accounts: hashed keys, plans with daily caps, usage per user with cost per part, analysis logs and
  `model_performance_daily`, RLS everywhere, provider costs hidden from users.
- Plan routing (Free/Basic Whisper on Groq, Pro AssemblyAI), the `cleanup_models` table, Pro cost cap (step-down
  ladder on a $4 budget), subscriptions + `apply_subscription()`, `delete_my_account()`, blocking, key last-used,
  log retention with a daily performance history.
- Cleanup: abandoned sentences dropped, numbers as digits, no cleanup under 3 words, unsure transcripts ask again.
- Meeting notes (Linux): mic + system audio, speaker labels (Scribe via Surplus), notes by Gemini 4 Argon, Alt+M with
  the meeting pill.
- App 1.0.8–1.0.11: short dictations no longer dropped, clear error messages, other audio fades down while dictating.
- App 1.0.17: the interpreter hears the call through Gemini 3.5 Live Translate (server mode=live, GEMINI_KEY on the free tier for now): English voice and subtitles ~3 s in, while they are still talking.
- App 1.0.16: live interpreter subtitles update every 0.8 s while someone talks, translated as they go.
- App 1.0.13: the live interpreter (Alt+I): the call spoken to you in English, your voice spoken to them in their
  language through a virtual "Utter Microphone"; server mode=interpret (Whisper on Groq, fast-model race, ElevenLabs
  Flash via Surplus, ~1.2 s a sentence after its pause).
- App 1.0.12: the meeting pill animates (rises in, breathing glow, red-dot ripple, grey when paused, dots while
  writing, sinks out) and has a pause button; Alt+P pauses/resumes a meeting, Alt+X cancels a dictation or meeting;
  the tray icon changes while a meeting is on or paused; notes are copied, no longer opened in an editor.
