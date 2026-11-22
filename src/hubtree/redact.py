"""Secret masking for the text that hubtree shows (last steps, agent lines, the local page).

It finds things that look like keys, tokens or personal ID numbers and replaces them with a mark. The value
itself is never printed. Library: find_hits(text) -> [(kind, offset)], find_spans(text) -> [(kind, start, end)],
redact(text) -> (text, marks).
"""
import gzip
import hashlib
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


PATTERNS = {
    "github token": r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "openai key": r"\bsk-(proj-)?[A-Za-z0-9]{32,}",
    "aws key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----",
    "google key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    # Also GITHUB_TOKEN=..., DB_PASSWORD=... and JSON "password": "..." (the name may end in _ and a quote).
    "key assignment": r"(?i)(?:\b|_)(api[_-]?key|apikey|secret|token|password|passwd)['\"]?\s*[:=]\s*['\"]?"
                      r"[A-Za-z0-9_\-]{16,}",
    "url key param": r"(?i)[?&](api[_-]?key|apikey|token)=[A-Za-z0-9_\-]{16,}",
    # Added in version 1.0 of the privacy guards (2026-10-31): cloud and service keys.
    "jwt": r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    "stripe key": r"\b[sr]k_(live|test)_[A-Za-z0-9]{20,}",
    "huggingface token": r"\bhf_[A-Za-z0-9]{30,}",
    "npm token": r"\bnpm_[A-Za-z0-9]{36}\b",
    "gitlab token": r"\bglpat-[A-Za-z0-9_-]{20,}",
    "sendgrid key": r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b",
    "twilio key": r"\bSK[0-9a-f]{32}\b",
    "telegram bot token": r"(?<!\d)\d{8,10}:AA[A-Za-z0-9_-]{33}\b",
    "azure storage key": r"(?i)\bAccountKey=[A-Za-z0-9+/]{40,}={0,2}",
    "gcp service account": r"\"private_key_id\"\s*:\s*\"[0-9a-f]{40}\"",
    "aws secret key": r"(?i)aws_?secret_?access_?key['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])",
    "bearer token": r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}=*",
    "url credentials": r"\b[a-z][a-z0-9+.-]{1,15}://[^\s:/@'\"<>]{1,64}:[^\s:/@'\"<>]{6,}@[A-Za-z0-9.-]+\.[a-z]{2,}",
    # "password is X" in prose: the value needs a letter and a digit, so "password was changed" does not hit.
    "password phrase": r"(?i)\b(password|passcode|passwd|pwd)\s+(is|was|is now|=|:)\s+['\"`]?"
                       r"(?=[^\s'\"`]*\d)(?=[^\s'\"`]*[A-Za-z])[^\s'\"`]{6,}",
    # Personal ID numbers. Each pattern needs a fixed shape or a label word, so plain numbers in data do not hit.
    "us ssn or itin": r"(?<![\w-])(?!000|666)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\w-])",
    "passport number": r"(?i)\bpassport\b[^\n]{0,40}?(?<![\w-])[A-Z]{0,2}\d{7,9}(?![\w-])",
    "india pan": r"(?<![\w+/])[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z](?![\w+/])",
    "uscis number": r"(?<![\w+/])(?:IOE|EAC|WAC|LIN|SRC|MSC|NBC|YSC)\d{10}(?![\w+/])|(?<![\w+/])N00\d{8}(?![\w+/])",
    "uscis a-number": r"(?i)\b(a-number|alien (registration )?number|uscis number)\b[^\n]{0,30}?(?<!\d)A?-?\d{8,9}(?!\d)",
}
COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}
NEEDS = {"passport number": ("passport",), "uscis a-number": ("a-number", "alien", "uscis number"),
         "password phrase": ("pass", "pwd"), "aws secret key": ("secret",), "bearer token": ("bearer",),
         "azure storage key": ("accountkey",), "gcp service account": ("private_key_id",)}  # label words
# Kinds outside PATTERNS, found by code with a check digit or a word list.
CODE_KINDS = ("card number", "india aadhaar", "recovery phrase")
KINDS = tuple(PATTERNS) + CODE_KINDS

# A number inside a decimal (3.1234... or 1234....5) is not a card; a number before a period or in a CSV cell is.
NUM_START = r"(?<!\w)(?<!\d\.)"
NUM_END = r"(?!\w|\.\d)"
CARD = re.compile(NUM_START + r"(\d{4}([ -]?)\d{4}\2\d{4}\2\d{4}|3[47]\d{2}([ -]?)\d{6}\3\d{5})" + NUM_END)
AADHAAR = re.compile(NUM_START + r"([2-9]\d{3} \d{4} \d{4})" + NUM_END)
AADHAAR_LABEL = re.compile(r"(?i)\b(aadhaa?r|uidai)\b[^\n]{0,30}?" + NUM_START + r"([2-9]\d{3}[ -]?\d{4}[ -]?\d{4})"
                           + NUM_END)
WORD = re.compile(r"[A-Za-z]+")
GAP_CHARS = r"\s,;.:)(\[\]\"'`*#\d-"  # what may stand between two words of a recovery phrase: space, numbers, JSON, bullets
GAP = re.compile(r"[%s]{1,12}" % GAP_CHARS)
PHRASE_MIN = 12  # wallets use 12, 15, 18, 21 or 24 words
# Fast first pass: 12 or more words of 3 to 8 letters in a row (every word-list word has 3 to 8 letters).
RUN = re.compile(r"(?i)(?<![a-z])[a-z]{3,8}(?:[%s]{1,12}[a-z]{3,8}(?![a-z])){%d,}" % (GAP_CHARS, PHRASE_MIN - 1))

with gzip.open(os.path.join(HERE, "bip39_english.txt.gz"), "rt", encoding="utf-8") as _f:  # the public BIP-39 list
    BIP39 = {w.strip() for w in _f if w.strip()}

# Verhoeff check digit tables (Aadhaar uses it).
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def _verhoeff_ok(digits):
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def _luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card_brand_ok(d):
    if len(d) == 15:
        return d[:2] in ("34", "37")
    return (d[0] == "4" or 51 <= int(d[:2]) <= 55 or 2221 <= int(d[:4]) <= 2720
            or d.startswith("6011") or d.startswith("65"))


def _phrase_spans(text):
    """(start, end) of 12 or more word-list words in a row (a wallet recovery phrase)."""
    out = []
    for cand in RUN.finditer(text):
        run, start, end, prev_end = 0, 0, 0, None
        for m in WORD.finditer(text, cand.start(), cand.end()):
            w = m.group(0).lower()
            joined = prev_end is not None and GAP.fullmatch(text, prev_end, m.start()) is not None
            if w in BIP39:
                if run and joined:
                    run += 1
                else:
                    if run >= PHRASE_MIN:
                        out.append((start, end))
                    run, start = 1, m.start()
                end = m.end()
            else:
                if run >= PHRASE_MIN:
                    out.append((start, end))
                run = 0
            prev_end = m.end()
        if run >= PHRASE_MIN:
            out.append((start, end))
    return out


def _phrases(text):
    """Start offsets of 12 or more word-list words in a row (the old secret_scan interface)."""
    return [a for a, _ in _phrase_spans(text)]


def find_spans(text):
    """[(kind, start, end)] for every hit. The one source of the patterns for the scan, the hooks and redact()."""
    low = text.lower()
    spans = []
    for k, rx in COMPILED.items():
        if k in NEEDS and not any(w in low for w in NEEDS[k]):
            continue
        spans += [(k, m.start(), m.end()) for m in rx.finditer(text)]
    for m in CARD.finditer(text):
        d = re.sub(r"\D", "", m.group(1))
        if _card_brand_ok(d) and _luhn_ok(d):
            spans.append(("card number", m.start(1), m.end(1)))
    found = [(m.start(1), m.end(1), m.group(1)) for m in AADHAAR.finditer(text)]
    if "aadha" in low or "uidai" in low:
        found += [(m.start(2), m.end(2), m.group(2)) for m in AADHAAR_LABEL.finditer(text)]
    spans += [("india aadhaar", a, b) for a, b, g in found if _verhoeff_ok(re.sub(r"\D", "", g))]
    spans += [("recovery phrase", a, b) for a, b in _phrase_spans(text)]
    return spans


def find_hits(text):
    """(kind, offset) for each hit in the text. Callers print the kind and a line number, never the match."""
    return [(k, a) for k, a, _ in find_spans(text)]


def hash4(value):
    """The first 4 hex characters of the SHA-256 of a value: enough to tell two hits apart, nothing to rebuild."""
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:4]


def redact(text, token="[REDACTED:{kind}]"):
    """(text with each hit replaced by the token, [[kind, offset in the new text, hash4]]). The one redaction
    function of the hub (mem/redact.py and tiers.py call it). Overlapping hits merge into one token."""
    if not text:
        return text, []
    spans = sorted(find_spans(text), key=lambda s: (s[1], -s[2]))
    merged = []
    for k, a, b in spans:
        if merged and a < merged[-1][2]:
            merged[-1][2] = max(merged[-1][2], b)
        else:
            merged.append([k, a, b])
    out, pos, marks, n = [], 0, [], 0
    for k, a, b in merged:
        out.append(text[pos:a])
        n += a - pos
        marks.append([k, n, hash4(text[a:b])])
        tok = token.format(kind=k, hash4=hash4(text[a:b]))
        out.append(tok)
        n += len(tok)
        pos = b
    out.append(text[pos:])
    return "".join(out), marks
