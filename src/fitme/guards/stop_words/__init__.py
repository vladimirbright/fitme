"""A§7 `scan`: deterministic, pre-LLM stop-word matching (AGENTS.md §2 "Stop words halt the
session").

Matching is deliberately broad (A§7.1): "no pain today" halts too. That's an accepted cost,
not a bug — the guard has to be deterministic and impossible to argue around, and the
precheck/check-in flows use buttons, not free text, so a false positive here stays rare in
practice. `scan` checks every supported language's list, not just `lang`, because users mix
languages.

One scan of one language runs in four stages, in this order:

1. **normalize** (`normalize`): strip Unicode format characters (category `Cf`: zero-width
   spaces, soft hyphens) *before* punctuation, so an invisible character can neither fuse two
   words nor split one; fold `ё` -> `е`; casefold; fold every "can't"/"couldn't" spelling to
   one token (apostrophe stripping alone would leave "can t" vs "cant" as different token
   sequences); strip punctuation to spaces; collapse whitespace.
2. **allowlist strip** (`_strip_allowlisted`): remove the exercise/training phrases that
   legitimately mention the chest or heart ("chest press", "день груди", "heart rate", and a
   bare chest token followed by a sets x reps pattern such as "chest 4x10").
3. **chest/heart deny-by-default with a co-occurrence override** (`_chest_or_heart_hit`,
   A§7.1): any chest/heart token (`chest`, `heart*`, `sternum`, `arrhythm*`, `груд*`,
   `сердц*`, `аритми*`) left after the allowlist strip halts; and a chest/heart token
   *anywhere* in the text (allowlisted or not) plus a *symptom* descriptor ("tight",
   "давит", ...) halts too, so "after chest press my chest feels tight" can't hide behind the
   allowlist. Effort words ("heavy", "тяжело") are not symptom descriptors: "chest day, bench
   felt heavy" passes.
4. **list matching** (`_list_hit`): the per-language term lists (`en.txt`, `ru.txt`),
   shipped as package data and read through `importlib.resources` (A§4.8), so they're found
   the same way from a checkout or an installed wheel. A line ending in `*` is a *stem* that
   matches as a prefix of any word ("растян*" matches "растянул"); anything else is an exact,
   whole-word phrase. Every Russian stem has a paired negative test in
   `tests/guards/test_stop_words.py` proving it doesn't fire on an unrelated word that shares
   the prefix.

**Emoji** are matched against the *raw* input before normalization, which would otherwise
strip them as punctuation (`_EMOJI_CATEGORIES`).

**Latin transliteration** (`_transliterate_latin_to_cyrillic`): a lifter with a Russian
interface typing on a Latin keyboard ("bolit", "golova kruzhitsya") is scanned a second time,
transliterated to Cyrillic, with the soft/hard signs ignored on both sides (informal
transliteration usually drops them: "bolno" for "больно"). This pass runs only when the
requested `lang` is Russian: for an English-language user, Latin text is English, and
transliterating it only manufactures spurious Cyrillic matches ("boli", "zatek").
"""

from __future__ import annotations

import importlib.resources
import re
import unicodedata
from dataclasses import dataclass
from functools import cache

from fitme.domain.guard_types import GuardVerdict

_STOP_WORDS_PACKAGE = "fitme.guards.stop_words"
_RULE = "stop_words.scan"
SUPPORTED_LANGS: tuple[str, ...] = ("en", "ru")

_CHEST_OR_HEART_CATEGORY = "chest_or_heart"

_CATEGORY_HEADER_RE = re.compile(r"^#\s*category:\s*(?P<category>\S+)\s*$")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
# Folds every common "can't"/"couldn't" spelling (with or without an apostrophe, straight or
# curly, with or without a space) to the single token "cant", applied after casefold but
# before punctuation stripping. Past tense folds to the same token on purpose: "couldn't
# breathe" must halt exactly like "can't breathe" (A§7.1).
_CANT_RE = re.compile(r"\b(?:can|couldn?)\s*['’]?\s*(?:not|t)\b")

# --- Stage 2/3 data: chest/heart deny-by-default (A§7.1) -------------------------------------
#
# Tokens (A§7.1): an exact word or a word prefix (`*`-terminated, same notation as the list
# files). `heart*` covers heartbeat/heartburn; Russian is inflected (груди, грудью, грудина,
# грудиной; сердце, сердцем, сердцебиение; аритмия, аритмией).
_CHEST_OR_HEART_TOKENS: dict[str, tuple[str, ...]] = {
    "en": ("chest", "heart*", "sternum", "arrhythm*"),
    "ru": ("груд*", "сердц*", "аритми*"),
}
# Exercise and training phrases that mention the chest/heart without describing a symptom.
# Removed from the text before the deny-by-default check. Every phrase here must contain a
# chest/heart token, otherwise it does nothing.
_CHEST_OR_HEART_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "en": (
        "chest press",
        "chest presses",
        "chest fly",
        "chest flys",
        "chest flyes",
        "chest flies",
        "chest dip",
        "chest dips",
        "chest supported",
        "chest to bar",
        "chest day",
        "chest and triceps",
        "chest and tris",
        "chest and biceps",
        "chest and back",
        "chest and shoulders",
        "chest workout",
        "chest session",
        "did chest",
        "upper chest",
        "lower chest",
        "heart rate",
    ),
    "ru": (
        "жим груди",
        "день груди",
        "тренировка груди",
        "тренировку груди",
        "тренировки груди",
        "качал грудь",
        "качала грудь",
        "тренировал грудь",
        "тренировала грудь",
        "сделал грудь",
        "сделала грудь",
        "грудь и трицепс",
        "грудь и бицепс",
        "грудь и спину",
        "грудь и плечи",
        "грудные мышцы",
        "грудные",
        "грудных",
        "грудными",
        "мышцы груди",
        "мышц груди",
        "на грудь",
        "грудак",
        "грудака",
        "грудаки",
        "грудаком",
        "верх груди",
        "низ груди",
    ),
}
# A bare chest token followed by a sets x reps pattern is a set log ("chest 4x10",
# "грудь 3 по 8"), not a symptom. Only the token is removed; the numbers stay.
_CHEST_LOG_LINE_RE: dict[str, re.Pattern[str]] = {
    "en": re.compile(r"\bchest(?= \d+ ?[x×х] ?\d)"),
    "ru": re.compile(r"\bгруд\w*(?= \d+ ?(?:[x×х] ?\d|по \d))"),
}
# Symptom descriptors for the co-occurrence override (A§7.1), same `*`-stem notation. Only
# *symptom* words are here: effort words ("heavy", "burn", "тяжело", "жжение") are ordinary
# training talk next to an allowlisted mention ("chest day, bench felt heavy", "жим груди,
# тяжело") and must not override the allowlist. They need no entry to halt next to a
# non-allowlisted token: "my chest burned"/"тяжесть в груди" halt on the bare token alone.
# Russian lists the pain forms instead of a bare `бол*` stem: "большой вес" is everyday
# logging and must not turn "жим груди, большой вес" into a halt.
_SYMPTOM_DESCRIPTORS: dict[str, tuple[str, ...]] = {
    "en": (
        "tight*",
        "pressure",
        "pain*",
        "hurt*",
        "squeez*",
        "ache",
        "aching",
        "discomfort",
        "pounding",
        "racing",
        "flutter*",
        "palpitat*",
    ),
    "ru": (
        "давит",
        "давлен*",
        "сжим*",
        "сдавл*",
        "колет",
        "кольн*",
        "ноет",
        "щем*",
        "стеснен*",
        "боль",
        "болит",
        "больно",
        "болят",
        "боли",
        "болью",
        "болел*",
        "болезнен*",
        "дискомфорт",
        "колотит*",
        "перебои",
    ),
}

# Matched against the *raw*, pre-normalization text: normalization's punctuation stripping
# would otherwise remove these entirely (emoji aren't `\w`/`\s`). A ZWJ sequence and the bare
# emoji nested inside it both map to the same category, so listing both is harmless.
_EMOJI_CATEGORIES: dict[str, str] = {
    "\U0001f915": "pain",  # 🤕 face with head-bandage
    "\U0001f635‍\U0001f4ab": "dizziness",  # 😵‍💫 face with spiral eyes
    "\U0001f635": "dizziness",  # 😵 dizzy face (bare, without the ZWJ sequence)
    "\U0001fac0": _CHEST_OR_HEART_CATEGORY,  # 🫀 anatomical heart
    "\U0001f494": _CHEST_OR_HEART_CATEGORY,  # 💔 broken heart
    "\U0001f922": "nausea",  # 🤢 nauseated face
    "\U0001f92e": "nausea",  # 🤮 vomiting face
}

# A simple phonetic Latin -> Cyrillic transliteration, longest digraphs first, for scanning
# text typed in Latin script ("bolit", "golova kruzhitsya"). Approximate by design: it only
# needs to round-trip the common transliteration of the words in ru.txt, not be a general
# transliteration system.
_TRANSLIT_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("shch", "щ"),
    ("yo", "е"),
    ("zh", "ж"),
    ("kh", "х"),
    # No "ts" -> "ц" digraph: "ts" is at least as often two separate sounds across a
    # morpheme boundary (e.g. "kruzhitsya" -> "кружится", the reflexive "-ся" suffix) as it
    # is the single letter "ц" ("tsentr" -> "центр"). Falling back to "т" + "с" separately
    # is the safer default for lifting logs, where reflexive verbs are common.
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
)
_TRANSLIT_SINGLES: dict[str, str] = {
    "a": "а",
    "b": "б",
    "v": "в",
    "g": "г",
    "d": "д",
    "e": "е",
    "z": "з",
    "i": "и",
    "y": "й",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "f": "ф",
    "h": "х",
    "c": "к",
    "x": "кс",
    "j": "й",
    "w": "в",
    "q": "к",
}


@dataclass(frozen=True, slots=True)
class StopHit:
    """One stop-word match."""

    category: str  # e.g. "pain", "dizziness", "numbness", "chest_or_heart", "something_popped"
    term: str  # the normalized term (or stem, or emoji) that matched
    lang: str  # which language's list matched (may differ from the requested `lang`)


@dataclass(frozen=True, slots=True)
class _Term:
    """One matchable entry: an exact whole-word phrase, or (`is_stem`) a word prefix."""

    text: str  # already normalized
    is_stem: bool

    def matches(self, padded: str, words: tuple[str, ...]) -> bool:
        if self.is_stem:
            return any(word.startswith(self.text) for word in words)
        return f" {self.text} " in padded


@dataclass(frozen=True, slots=True)
class _CategorizedTerm:
    category: str
    term: _Term


@dataclass(frozen=True, slots=True)
class _LangRules:
    """Everything one language's scan matches against, normalized once and cached."""

    lang: str
    chest_or_heart_tokens: tuple[_Term, ...]
    chest_or_heart_allowlist: re.Pattern[str] | None  # every allowlisted phrase, whole-word
    chest_log_line: re.Pattern[str] | None
    symptom_descriptors: tuple[_Term, ...]
    terms: tuple[_CategorizedTerm, ...]


# --- Stage 1: normalization --------------------------------------------------------------


def _strip_format_characters(text: str) -> str:
    """Remove Unicode category `Cf` characters (zero-width space, soft hyphen, ...) — deleted
    outright, not replaced with a space, so an invisible character inserted mid-word can
    neither fuse two words together nor split one word into two. Must run *before*
    punctuation stripping (which would otherwise turn e.g. a soft hyphen into a word-splitting
    space first)."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def normalize(text: str) -> str:
    """Stage 1 (see the module docstring). Both the scanned text and every list term are
    normalized the same way, so matching is on plain, space-separated words."""
    text = _strip_format_characters(text)
    text = text.replace("ё", "е").replace("Ё", "Е")
    text = text.casefold()
    text = _CANT_RE.sub("cant", text)
    text = _PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_soft_hard_signs(text: str) -> str:
    return text.replace("ь", "").replace("ъ", "")


def _transliterate_latin_to_cyrillic(text: str) -> str:
    """Best-effort phonetic transliteration of a Latin-script run to Cyrillic, leaving
    anything already non-Latin-lowercase (spaces, digits, Cyrillic) untouched. `text` is
    expected to already be normalized (casefolded)."""
    result: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        matched = False
        for latin, cyrillic in _TRANSLIT_DIGRAPHS:
            if text.startswith(latin, i):
                result.append(cyrillic)
                i += len(latin)
                matched = True
                break
        if matched:
            continue
        single = _TRANSLIT_SINGLES.get(text[i])
        if single is not None:
            result.append(single)
        else:
            result.append(text[i])
        i += 1
    return "".join(result)


# --- Rule loading ------------------------------------------------------------------------


def _parse_term(raw: str, *, fold_signs: bool) -> _Term:
    """`"растян*"` -> a stem; anything else -> an exact phrase. Normalized like the text."""
    is_stem = raw.endswith("*")
    text = normalize(raw[:-1] if is_stem else raw)
    if fold_signs:
        text = _strip_soft_hard_signs(text)
    return _Term(text=text, is_stem=is_stem)


def _load_list_terms(lang: str, *, fold_signs: bool) -> tuple[_CategorizedTerm, ...]:
    """Load one language's list from `guards/stop_words/<lang>.txt` (package data)."""
    resource = importlib.resources.files(_STOP_WORDS_PACKAGE) / f"{lang}.txt"
    text = resource.read_text(encoding="utf-8")

    entries: list[_CategorizedTerm] = []
    category = "uncategorized"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _CATEGORY_HEADER_RE.match(line)
        if header is not None:
            category = header.group("category")
            continue
        if line.startswith("#"):
            continue
        term = _parse_term(line, fold_signs=fold_signs)
        if term.text:
            entries.append(_CategorizedTerm(category=category, term=term))
    return tuple(entries)


@cache
def _rules(lang: str, *, fold_signs: bool) -> _LangRules:
    """One language's rules. `fold_signs=True` is the variant for scanning transliterated
    text, with `ь`/`ъ` removed from every term (the transliteration never produces them)."""

    def fold(phrase: str) -> str:
        return _strip_soft_hard_signs(normalize(phrase)) if fold_signs else normalize(phrase)

    return _LangRules(
        lang=lang,
        chest_or_heart_tokens=tuple(
            _parse_term(raw, fold_signs=fold_signs) for raw in _CHEST_OR_HEART_TOKENS.get(lang, ())
        ),
        chest_or_heart_allowlist=_allowlist_pattern(
            tuple(fold(phrase) for phrase in _CHEST_OR_HEART_ALLOWLIST.get(lang, ()))
        ),
        chest_log_line=_CHEST_LOG_LINE_RE.get(lang),
        symptom_descriptors=tuple(
            _parse_term(raw, fold_signs=fold_signs) for raw in _SYMPTOM_DESCRIPTORS.get(lang, ())
        ),
        terms=_load_list_terms(lang, fold_signs=fold_signs),
    )


# --- Stages 2-4: one language's scan -----------------------------------------------------


def _allowlist_pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regex matching any allowlisted phrase as whole words. Lookarounds (not consumed
    spaces) mean back-to-back repeats ("chest day chest day") are all stripped in one `sub`;
    longer phrases go first so "chest press" can't pre-empt "chest presses"."""
    if not phrases:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<= )(?:{alternatives})(?= )")


def _strip_allowlisted(padded: str, rules: _LangRules) -> str:
    """Stage 2: remove every allowlisted phrase (and the token of a "chest 4x10"-style set
    log) so a chest/heart token that only appears inside one doesn't reach stage 3."""
    result = padded
    if rules.chest_or_heart_allowlist is not None:
        result = rules.chest_or_heart_allowlist.sub("", result)
    if rules.chest_log_line is not None:
        result = rules.chest_log_line.sub(" ", result)
    return result


def _first_match(terms: tuple[_Term, ...], padded: str) -> _Term | None:
    words = tuple(padded.split())
    return next((term for term in terms if term.matches(padded, words)), None)


def _chest_or_heart_hit(padded: str, rules: _LangRules) -> StopHit | None:
    """Stage 3: deny-by-default. A chest/heart token halts unless the allowlist removed it;
    a token plus a *symptom* descriptor halts regardless of the allowlist (A§7.1's
    co-occurrence override: "after chest press my chest feels tight"). Effort words are not
    descriptors, so "chest day, bench felt heavy" passes while "my chest feels heavy" still
    halts on its non-allowlisted token."""
    token = _first_match(rules.chest_or_heart_tokens, padded)
    if token is None:
        return None
    descriptor = _first_match(rules.symptom_descriptors, padded)
    if descriptor is not None:
        return StopHit(
            category=_CHEST_OR_HEART_CATEGORY,
            term=f"{token.text} + {descriptor.text}",
            lang=rules.lang,
        )
    remaining = _first_match(rules.chest_or_heart_tokens, _strip_allowlisted(padded, rules))
    if remaining is None:
        return None
    return StopHit(category=_CHEST_OR_HEART_CATEGORY, term=remaining.text, lang=rules.lang)


def _list_hit(padded: str, rules: _LangRules) -> StopHit | None:
    """Stage 4: the plain per-language term list, first match wins."""
    words = tuple(padded.split())
    for entry in rules.terms:
        if entry.term.matches(padded, words):
            return StopHit(category=entry.category, term=entry.term.text, lang=rules.lang)
    return None


def _scan_language(normalized: str, lang: str, *, fold_signs: bool = False) -> StopHit | None:
    padded = f" {normalized} "
    rules = _rules(lang, fold_signs=fold_signs)
    return _chest_or_heart_hit(padded, rules) or _list_hit(padded, rules)


def _scan_transliterated_russian(normalized: str) -> StopHit | None:
    """The Latin-keyboard reading of the text, checked against the Russian rules with the
    soft/hard signs ignored on both sides."""
    translit = _transliterate_latin_to_cyrillic(normalized)
    if not translit or translit == normalized:
        return None
    return _scan_language(_strip_soft_hard_signs(translit), "ru", fold_signs=True)


def _emoji_hit(raw_text: str, lang: str) -> StopHit | None:
    for emoji, category in _EMOJI_CATEGORIES.items():
        if emoji in raw_text:
            return StopHit(category=category, term=emoji, lang=lang)
    return None


def _languages_to_scan(lang: str) -> tuple[str, ...]:
    """The requested language first, then every other supported one (users mix languages).
    An unrecognized `lang` falls back to scanning every list we have."""
    if lang in SUPPORTED_LANGS:
        return (lang, *(other for other in SUPPORTED_LANGS if other != lang))
    return SUPPORTED_LANGS


def scan(text: str, lang: str) -> StopHit | None:
    """Scan `text` for a stop word: emoji first (checked against the raw text), then `lang`'s
    rules, then every other supported language's rules (users mix languages). With
    `lang="ru"`, a Latin-transliterated reading of the text is also checked, so "bolit" halts
    the same as "болит". Returns the first match found, or `None` for the ordinary case of no
    match. Never raises.
    """
    emoji_hit = _emoji_hit(text, lang)
    if emoji_hit is not None:
        return emoji_hit

    normalized = normalize(text)
    if not normalized:
        return None

    for candidate_lang in _languages_to_scan(lang):
        hit = _scan_language(normalized, candidate_lang)
        if hit is not None:
            return hit
    if lang == "ru":
        return _scan_transliterated_russian(normalized)
    return None


def to_verdict(hit: StopHit | None) -> GuardVerdict:
    """Turn a `scan()` result into a `GuardVerdict` (A§7): `hit is None` is the only passing
    case. Feeds straight into `guards.layering.combine_with_llm_signal` alongside an LLM
    `safety_signal` (A§7.2, A§7.4: "safety_signal=false does not clear a stop-word hit")."""
    if hit is None:
        return GuardVerdict(rule=_RULE, ok=True, detail="no stop word matched")
    return GuardVerdict(
        rule=_RULE, ok=False, detail=f"matched '{hit.term}' ({hit.category}, {hit.lang})"
    )
