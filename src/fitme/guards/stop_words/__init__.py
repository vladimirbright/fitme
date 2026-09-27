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
   sequences); strip punctuation to spaces; collapse whitespace. The text is also split into
   **clauses** on sentence punctuation (`.;!?` and newlines) *before* that punctuation is
   stripped, for stage 3.
2. **allowlist strip** (`_strip_allowlisted`): remove exercise/training phrases that mention
   the chest or heart ("chest press", "жим груди", "heart rate") from each clause, so an
   effort word next to exercise wording ("жим груди, тяжело", "chest day, bench felt heavy")
   can't pair with the token in stage 3.
3. **chest/heart + symptom co-occurrence** (`_chest_or_heart_hit`, A§7.1, operator decision
   2026-09-27): a bare chest/heart mention never halts — chest is everyday training text ("к
   верху груди", "на уровне груди", "chest day"). A chest/heart token (`chest`, `heart*`,
   `sternum`, `груд*`, `сердц*`) halts only together with a *symptom descriptor* ("tight",
   "давит", "тяжело", ...) in the same clause and within a few words of it. Explicit cardiac
   terms ("palpitations", "аритмия", "irregular heartbeat", "за грудиной") halt on their own
   through the ordinary lists in stage 4.
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
# Stage 3: how far apart (in words) a chest/heart token and a symptom descriptor may be within
# one clause and still count as the same mention.
_CO_OCCURRENCE_WINDOW = 5

_CATEGORY_HEADER_RE = re.compile(r"^#\s*category:\s*(?P<category>\S+)\s*$")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+")
# Folds every common "can't"/"couldn't" spelling (with or without an apostrophe, straight or
# curly, with or without a space) to the single token "cant", applied after casefold but
# before punctuation stripping. Past tense folds to the same token on purpose: "couldn't
# breathe" must halt exactly like "can't breathe" (A§7.1).
_CANT_RE = re.compile(r"\b(?:can|couldn?)\s*['’]?\s*(?:not|t)\b")

# --- Stage 2/3 data: chest/heart + symptom (A§7.1) ------------------------------------------
#
# Tokens: an exact word or a word prefix (`*`-terminated, same notation as the list files).
# `heart*` covers heartbeat/heartburn; Russian is inflected (груди, грудью, грудина,
# грудиной; сердце, сердцем, сердцебиение). Single words only: stage 3 matches word by word.
_CHEST_OR_HEART_TOKENS: dict[str, tuple[str, ...]] = {
    "en": ("chest", "heart*", "sternum"),
    "ru": ("груд*", "сердц*"),
}
# Exercise and training phrases that mention the chest/heart. Removed from each clause before
# stage 3, so effort words next to exercise wording ("chest press 3x10, last set burned",
# "жим груди, тяжело") don't pair with the token. Every phrase here must contain a
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
# Symptom descriptors (A§7.1), same `*`-stem notation, single words only. Effort words
# ("heavy", "тяжело", "burn") are here too: "в груди тяжело" and "my chest burned" are
# symptom reports, while "жим груди, тяжело" is protected by the allowlist strip in stage 2.
# Russian lists the pain forms instead of a bare `бол*` stem: "большой вес" is everyday
# logging and must not turn "грудь, большой вес" into a halt.
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
        "burn*",
        "heavy",
        "heaviness",
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
        "жж*",
        "тяжест*",
        "тяжело",
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

    def matches_word(self, word: str) -> bool:
        """Single-word variant for stage 3's word-window matching."""
        return word.startswith(self.text) if self.is_stem else word == self.text


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
    symptom_descriptors: tuple[_Term, ...]
    terms: tuple[_CategorizedTerm, ...]


@dataclass(frozen=True, slots=True)
class _NormalizedText:
    """Stage 1's output: the whole text (for stages 2/4's phrase matching) and its clauses
    (for stage 3's co-occurrence check), all normalized the same way."""

    whole: str
    clauses: tuple[str, ...]


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


def _normalize_with_clauses(text: str) -> _NormalizedText:
    """`normalize` the whole text, and separately each clause of it (split on sentence
    punctuation and newlines *before* normalization strips that punctuation)."""
    stripped = _strip_format_characters(text)
    clauses = tuple(
        clause
        for clause in (normalize(part) for part in _CLAUSE_SPLIT_RE.split(stripped))
        if clause
    )
    return _NormalizedText(whole=normalize(stripped), clauses=clauses)


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


def _allowlist_pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    """One regex matching any allowlisted phrase as whole words. Lookarounds (not consumed
    spaces) mean back-to-back repeats ("chest day chest day") are all stripped in one `sub`;
    longer phrases go first so "chest press" can't pre-empt "chest presses"."""
    if not phrases:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<= )(?:{alternatives})(?= )")


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
        symptom_descriptors=tuple(
            _parse_term(raw, fold_signs=fold_signs) for raw in _SYMPTOM_DESCRIPTORS.get(lang, ())
        ),
        terms=_load_list_terms(lang, fold_signs=fold_signs),
    )


# --- Stages 2-4: one language's scan -----------------------------------------------------


def _strip_allowlisted(padded: str, rules: _LangRules) -> str:
    """Stage 2: remove every allowlisted exercise/training phrase, so the chest/heart token
    inside it can't pair with a nearby effort word in stage 3."""
    if rules.chest_or_heart_allowlist is None:
        return padded
    return rules.chest_or_heart_allowlist.sub("", padded)


def _positions(terms: tuple[_Term, ...], words: tuple[str, ...]) -> list[tuple[int, _Term]]:
    """Index and matching term of every word in `words` that one of `terms` matches."""
    found: list[tuple[int, _Term]] = []
    for index, word in enumerate(words):
        for term in terms:
            if term.matches_word(word):
                found.append((index, term))
                break
    return found


def _chest_or_heart_hit(clauses: tuple[str, ...], rules: _LangRules) -> StopHit | None:
    """Stage 3: a chest/heart token halts only with a symptom descriptor in the same clause
    and within `_CO_OCCURRENCE_WINDOW` words of it (A§7.1). A bare mention never halts."""
    for clause in clauses:
        words = tuple(_strip_allowlisted(f" {clause} ", rules).split())
        tokens = _positions(rules.chest_or_heart_tokens, words)
        if not tokens:
            continue
        for token_index, token in tokens:
            for descriptor_index, descriptor in _positions(rules.symptom_descriptors, words):
                if abs(token_index - descriptor_index) <= _CO_OCCURRENCE_WINDOW:
                    return StopHit(
                        category=_CHEST_OR_HEART_CATEGORY,
                        term=f"{token.text} + {descriptor.text}",
                        lang=rules.lang,
                    )
    return None


def _list_hit(padded: str, rules: _LangRules) -> StopHit | None:
    """Stage 4: the plain per-language term list, first match wins."""
    words = tuple(padded.split())
    for entry in rules.terms:
        if entry.term.matches(padded, words):
            return StopHit(category=entry.category, term=entry.term.text, lang=rules.lang)
    return None


def _scan_language(text: _NormalizedText, lang: str, *, fold_signs: bool = False) -> StopHit | None:
    rules = _rules(lang, fold_signs=fold_signs)
    return _chest_or_heart_hit(text.clauses, rules) or _list_hit(f" {text.whole} ", rules)


def _scan_transliterated_russian(text: _NormalizedText) -> StopHit | None:
    """The Latin-keyboard reading of the text, checked against the Russian rules with the
    soft/hard signs ignored on both sides."""
    translit_whole = _transliterate_latin_to_cyrillic(text.whole)
    if not translit_whole or translit_whole == text.whole:
        return None
    translit = _NormalizedText(
        whole=_strip_soft_hard_signs(translit_whole),
        clauses=tuple(
            _strip_soft_hard_signs(_transliterate_latin_to_cyrillic(clause))
            for clause in text.clauses
        ),
    )
    return _scan_language(translit, "ru", fold_signs=True)


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

    normalized = _normalize_with_clauses(text)
    if not normalized.whole:
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
