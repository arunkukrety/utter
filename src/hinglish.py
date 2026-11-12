"""Devanagari -> Roman Hinglish, the way people type Hindi in chats ("कल मीटिंग है" -> "kal meeting hai").

Offline and rule-based: a letter table plus Hindi schwa deletion (the silent "a" that makes कमरा "kamra", not
"kamara"). It is a heuristic, so the odd word comes out spelled unusually; text that is not Devanagari passes through
unchanged. Run this file to check it: python3 hinglish.py
"""
import re

VOWELS = {  # independent vowels
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o",
    "औ": "au", "ऑ": "o", "ॲ": "e",
}
MATRAS = {  # vowel signs after a consonant
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o",
    "ॅ": "e",
}
CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "फ़": "f", "ड़": "r", "ढ़": "rh",
    "य़": "y",
}
NUKTA_PAIRS = {"क": "क़", "ख": "ख़", "ग": "ग़", "ज": "ज़", "फ": "फ़", "ड": "ड़", "ढ": "ढ़", "य": "य़"}
VIRAMA, NUKTA, ANUSVARA, CHANDRABINDU, VISARGA = "्", "़", "ं", "ँ", "ः"
PUNCT = {"।": ".", "॥": ".", "॰": "."}
DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
DEVANAGARI_WORD = re.compile(r"[ऀ-ॿ]+")


def _syllables(word: str) -> list:
    """Split a word into [consonant cluster, vowel] units. vowel is None for the inherent schwa, '' for none."""
    units, i = [], 0
    while i < len(word):
        ch = word[i]
        if i + 1 < len(word) and word[i + 1] == NUKTA and ch in NUKTA_PAIRS:
            ch, i = NUKTA_PAIRS[ch], i + 1
        if ch in CONSONANTS:
            cons = CONSONANTS[ch]
            nxt = word[i + 1] if i + 1 < len(word) else ""
            if nxt == VIRAMA:  # half letter: joins the next consonant
                units.append([cons, ""]); i += 2; continue
            if nxt in MATRAS:
                units.append([cons, MATRAS[nxt]]); i += 2; continue
            units.append([cons, None]); i += 1; continue
        if ch in VOWELS:
            units.append(["", VOWELS[ch]])
        elif ch in (ANUSVARA, CHANDRABINDU):
            units.append(["~", ""])  # nasal, resolved below
        elif ch == VISARGA:
            units.append([":", ""])  # its own marker, so it is never mistaken for the letter ह
        elif ch in DIGITS:
            units.append([DIGITS[ch], ""])
        elif ch in PUNCT:
            units.append([PUNCT[ch], ""])
        # anything else (stray signs) is dropped
        i += 1
    return units


def _word(word: str) -> str:
    units = _syllables(word)
    sounding = [k for k, u in enumerate(units) if u[0] not in ("~", ":")]  # letters that can carry a vowel
    schwas = [k for k, u in enumerate(units) if u[1] is None]
    # schwa deletion, right to left: drop the final one (unless the word is one syllable or ends in a cluster),
    # and drop a medial one between two full vowels (kamara -> kamra), never two in a row
    for k in reversed(schwas):
        last = k == max(sounding, default=k)
        prev_full = k > 0 and units[k - 1][1] not in ("",) and units[k - 1][0] != "~"
        nxt = units[k + 1] if k + 1 < len(units) else None
        if last:
            one_syllable = sum(1 for u in units if u[1] is not None and u[1] != "") + len(schwas) <= 1
            cluster = k > 0 and units[k - 1][1] == "" and units[k - 1][0] not in ("~", ":")  # a half letter
            units[k][1] = "a" if (one_syllable or cluster) else ""
        elif prev_full and nxt is not None and nxt[1] not in (None, "") and nxt[0] != "~":
            units[k][1] = ""
        else:
            units[k][1] = "a"
    out = []
    for k, (cons, vowel) in enumerate(units):
        if cons == "~":
            nxt = units[k + 1][0] if k + 1 < len(units) else ""
            out.append("m" if nxt[:1] in ("p", "b", "m") else "n")
        elif cons == ":":
            out.append("h" if k + 1 == len(units) else "")  # silent before a consonant: दुःख is "dukh"
        elif cons == "ch" and vowel == "" and k + 1 < len(units) and units[k + 1][0] == "chh":
            continue  # च्छ is written "chh" in chats: achha, not achchha
        else:
            out.append(cons + (vowel or ""))
    return "".join(out)


def romanize(text: str) -> str:
    """Romanize every Devanagari word in text; Latin text, numbers and punctuation pass through unchanged."""
    return DEVANAGARI_WORD.sub(lambda m: _word(m.group(0)), text)


if __name__ == "__main__":
    cases = {
        "कल मीटिंग है": "kal miting hai",
        "क्या हाल है": "kya hal hai",
        "मुझे नहीं पता": "mujhe nahin pata",
        "आप कैसे हो": "aap kaise ho",
        "कमरा": "kamra",
        "समझना": "samajhna",
        "हम घर जा रहे हैं": "ham ghar ja rahe hain",
        "मैं ठीक हूँ।": "main thik hun.",
        "ज़रूर": "zarur",
        "Please send the agenda": "Please send the agenda",
        "कल meeting है, can you send it?": "kal meeting hai, can you send it?",
        "न": "na",
        "पत्र": "patra",
        "हिंदी": "hindi",
        "संबंध": "sambandh",
        "यह बहुत अच्छा है": "yah bahut achha hai",
        "दुःख": "dukh",
    }
    bad = [(src, want, romanize(src)) for src, want in cases.items() if romanize(src) != want]
    for src, want, got in bad:
        print(f"MISMATCH {src!r}: want {want!r}, got {got!r}")
    print(f"{len(cases) - len(bad)}/{len(cases)} ok")
    raise SystemExit(1 if bad else 0)
