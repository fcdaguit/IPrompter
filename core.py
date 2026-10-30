"""TermPrompter core: text normalization and the voice-following matcher (no GUI, no audio)."""
import math
import re
import unicodedata
from enum import Enum


class Lang(Enum):
    ENGLISH = ("en", "English", "en")
    FILIPINO = ("fil", "Filipino", "tl")
    BISAYA = ("ceb", "Bisaya (Cebuano)", "ceb")

    def __init__(self, code, label, model_key):
        self.code = code
        self.label = label
        self.model_key = model_key


class Phrase:
    __slots__ = ("text", "tokens")

    def __init__(self, text, tokens):
        self.text = text
        self.tokens = tokens


_CUES = re.compile(r"\[[^\]]*\]")                 # [stage directions]: shown, never expected to be spoken
_WORD_SPLIT = re.compile(r"[^\w'’]+", re.UNICODE)
_REPEATED = re.compile(r"(.)\1+")
_SENTENCE = re.compile(r"(?<=[.!?;:…])\s+")
_COMMA = re.compile(r"(?<=,)\s+")


def normalize(raw, lang):
    """Make script words and recognizer output comparable.

    For Filipino/Bisaya, common spelling variants are folded together
    (o/u, e/i, c/k, qu/k, ph/f, doubled letters)."""
    w = unicodedata.normalize("NFD", raw.lower())
    w = "".join(ch for ch in w if not unicodedata.combining(ch))
    w = "".join(ch for ch in w if ch.isalnum())
    if lang != Lang.ENGLISH:
        w = w.replace("ph", "f").replace("qu", "k")
        w = w.replace("c", "k").replace("o", "u").replace("e", "i")
        w = _REPEATED.sub(r"\1", w)
    return w


def tokenize(text, lang):
    text = _CUES.sub(" ", text)
    out = []
    for part in _WORD_SPLIT.split(text):
        n = normalize(part, lang)
        if n:
            out.append(n)
    return out


def split_phrases(script, lang):
    """Per line, then per sentence; sentences over 100 chars are split again at commas."""
    out = []

    def add(s):
        s = s.strip()
        if s:
            out.append(Phrase(s, tokenize(s, lang)))

    for line in script.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        for sentence in _SENTENCE.split(line):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > 100:
                for piece in _COMMA.split(sentence):
                    add(piece)
            else:
                add(sentence)
    return out


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def close(a, b, max_edit):
    if a == b:
        return True
    if max_edit <= 0 or len(a) < 2 or len(b) < 2:
        return False
    if abs(len(a) - len(b)) > max_edit:
        return False
    return _lev(a, b) <= max_edit


class Profile:
    """tol is 1 (strict) .. 5 (very forgiving)."""

    def __init__(self, tol, lang):
        self.tol = max(1, min(5, tol))
        self.lang = lang
        self.skip = self.tol + 1                              # script words that may be skipped
        self.ratio = [0.9, 0.8, 0.7, 0.6, 0.5][self.tol - 1]  # share of a phrase that must be heard
        self.adlib_misses = self.tol + 2                      # consecutive unmatched words => ad-lib
        self.resync = 2 if self.tol >= 4 else 3               # words in a row to rejoin the script
        self.tail_slack = 2 if self.tol >= 3 else (1 if self.tol == 2 else 0)

    def max_edit(self, n):
        e = 0 if n <= 2 else 1 if n <= 4 else 2 if n <= 7 else 3
        if self.tol <= 2:
            e = max(0, e - 1)
        if self.lang == Lang.BISAYA and n >= 4:
            e += 1            # Bisaya is often decoded with a Filipino model: allow more spelling drift
        return e


class Matcher:
    """Follows the speaker through the script.

    Advances only once the current phrase is (nearly) fully read, is driven by words heard
    (not by a timer), and enters ad-lib mode (no advancing, keeps listening) when several words
    in a row do not match. It resumes after the speaker reads a few script words in a row again,
    and then moves on only when that phrase is completed."""

    FILLERS = {"uh", "um", "ah", "er", "hm", "mm", "ahm", "eh"}

    def __init__(self):
        self.profile = Profile(3, Lang.ENGLISH)
        self.cur = 0
        self.adlib = False
        self.finished = False
        self._g = []
        self._p_start = []
        self._p_end = []
        self._tok_phrase = []
        self._count = 0
        self._pos = 0
        self._hits = 0
        self._miss = 0
        self._consumed = 0
        self._run = 0
        self._run_last = -1

    def load(self, phrases):
        self._count = len(phrases)
        self._p_start, self._p_end, self._g, self._tok_phrase = [], [], [], []
        for i, p in enumerate(phrases):
            self._p_start.append(len(self._g))
            for t in p.tokens:
                self._g.append(t)
                self._tok_phrase.append(i)
            self._p_end.append(len(self._g))
        self.reset()

    def reset(self):
        self.cur = self._next_non_empty(0)
        self.finished = self.cur >= self._count
        self._pos = 0 if self.finished else self._p_start[self.cur]
        self._hits = self._miss = self._consumed = self._run = 0
        self._run_last = -1
        self.adlib = False

    def end_utterance(self):
        self._consumed = 0

    def feed(self, words):
        """Full word list of the current utterance. Returns True if cur/adlib changed."""
        if self.finished:
            return False
        if len(words) < self._consumed:
            self._consumed = len(words)       # hypothesis was revised
        changed = False
        for i in range(self._consumed, len(words)):
            if self.finished:
                break
            if self._step(words[i]):
                changed = True
        self._consumed = len(words)
        return changed

    def _next_non_empty(self, frm):
        i = frm
        while i < self._count and self._p_start[i] == self._p_end[i]:
            i += 1
        return i

    def _step(self, w):
        if w in self.FILLERS or not self._g:
            return False
        return self._step_adlib(w) if self.adlib else self._step_tracking(w)

    def _step_tracking(self, w):
        pr = self.profile
        limit = min(len(self._g), self._pos + pr.skip + 1)
        k = -1
        for j in range(self._pos, limit):
            # Skipping ahead is trusted only for content words (4+ letters); short common words
            # appear everywhere and would let ad-lib speech jump the script.
            if j > self._pos + 1 and len(self._g[j]) < 4:
                continue
            if close(w, self._g[j], pr.max_edit(len(self._g[j]))):
                k = j
                break
        if k < 0:
            self._miss += 1
            if self._miss >= pr.adlib_misses:
                self.adlib = True
                self._run = 0
                self._run_last = -1
                return True
            return False
        self._miss = 0
        return self._apply(k)

    def _apply(self, k):
        changed = False
        pk = self._tok_phrase[k]
        if pk > self.cur:
            self.cur = pk
            self._hits = 0
            changed = True
        self._hits += 1
        self._pos = k + 1
        if self._is_complete():
            self.cur = self._next_non_empty(self.cur + 1)
            self._hits = 0
            changed = True
            if self.cur >= self._count:
                self.finished = True
            elif self._pos < self._p_start[self.cur]:
                self._pos = self._p_start[self.cur]
        return changed

    def _is_complete(self):
        n = self._p_end[self.cur] - self._p_start[self.cur]
        if n == 0:
            return True
        need = max(1, math.ceil(n * self.profile.ratio))
        slack = 0 if n <= 4 else self.profile.tail_slack
        return self._pos >= self._p_end[self.cur] - slack and self._hits >= need

    def _step_adlib(self, w):
        pr = self.profile
        nxt = self._next_non_empty(self.cur + 1)
        last = self.cur if nxt >= self._count else nxt
        frm, to = self._p_start[self.cur], self._p_end[last]
        k = -1
        for j in range(frm, to):
            if close(w, self._g[j], pr.max_edit(len(self._g[j]))):
                if self._run > 0 and (self._run_last + 1) <= j <= (self._run_last + 1 + pr.skip):
                    k = j
                    break
                if k < 0:
                    k = j
        if k < 0:
            self._run = 0
            self._run_last = -1
            return False
        if self._run > 0 and (self._run_last + 1) <= k <= (self._run_last + 1 + pr.skip):
            self._run += 1
        else:
            self._run = 1
        self._run_last = k
        if self._run < pr.resync:
            return False
        self.adlib = False
        self._miss = 0
        pk = self._tok_phrase[k]
        if pk != self.cur:
            self.cur = pk
            self._hits = 0
        self._hits = max(self._hits, self._run)
        self._pos = k + 1
        if self._is_complete():
            self.cur = self._next_non_empty(self.cur + 1)
            self._hits = 0
            if self.cur >= self._count:
                self.finished = True
            elif self._pos < self._p_start[self.cur]:
                self._pos = self._p_start[self.cur]
        return True
