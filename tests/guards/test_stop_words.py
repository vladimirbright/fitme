"""`guards.stop_words.scan` (A§7, AGENTS.md §2 "Stop words halt the session"; A§7.4: "each
stop-word category in each language"; A§7.1: "no pain today" MUST halt; soreness words must
NOT halt on their own; chest/heart deny-by-default with an allowlist and a co-occurrence
override in both languages; curated stems with negative tests; everyday-logging false
positives that must NOT halt; and the normalization pass (Cf stripping, "can't"/"couldn't"
folding, emoji, Latin transliteration)).

The four corpora below are the regression suite: every phrase from the reviewer's probe
scripts (stop.py, stop2.py, stop3.py) and from the A§7.1 rulings is here, on the side the
ruling puts it."""

from __future__ import annotations

import pytest

from fitme.guards.layering import combine_with_llm_signal
from fitme.guards.stop_words import scan, to_verdict

# --- Regression corpus: every one of these must halt. ---

_EN_MUST_HALT = (
    # chest/heart: deny-by-default (A§7.1)
    "chest feels tight",
    "my chest feels tight",
    "pressure in my chest",
    "chest is tight",
    "my CHEST is TIGHT",
    "Chest-feels-tight!!!",
    "tightness in my chest",
    "tightness across my chest",
    "chest hurts",
    "chest feels heavy",
    "burning in my chest",
    "my chest!!",
    "I have some chest discomfort",
    "heart racing",
    "my heart is pounding",
    "something's wrong with my heart",
    "palpitations",
    "my chest burned",
    "my chest feels heavy",
    "irregular heartbeat",
    "heartbeat racing",
    "heartbeat weird",
    "pressure behind my sternum",
    "arrhythmia",
    # chest/heart: co-occurrence override beats the allowlist
    "after chest press my chest feels tight",
    "chest day but my chest feels tight",
    "chest press then pressure in my chest",
    "heart rate 150 and my heart hurts",
    "after chest press my chest feels heavy",
    "chest day but chest feels tight",
    "chest day chest day but chest hurts",
    # breathing
    "can't breathe",
    "cant breathe",
    "cannot breathe",
    "can not breathe",
    "couldn't breathe",
    "could not breathe",
    "couldnt breathe",
    "hard to breathe",
    "struggling to breathe",
    "trouble breathing",
    # pain
    "no pain today",
    "sharp pain in my shoulder",
    "my shoulder is killing me",
    "my knee is killing me",
    "stabbing sensation",
    "it's tender",
    "throbbing",
    "burning sensation in my arm",
    "pian in knee",
    "hurst",
    "pain💥",
    "tweaked my back",
    "tweak in my back",
    "tweaked it",
    "just tweaked something",
    "felt a twinge",
    "twinge in my knee",
    "twinged",
    "sharp twinge",
    "strained my hamstring",
    "strain in hamstring",
    "sprained my ankle",
    "sprain",
    "pulled a muscle",
    "pulled my hamstring",
    "felt a pull in my hamstring",
    "rolled my ankle",
    # injury
    "injured my shoulder",
    "injury",
    "aggravated my knee",
    "knee is swollen",
    "swelling in my ankle",
    # dizziness
    "light-headed",
    "light headed",
    "lightheaded",
    "woozy",
    "room is spinning",
    "vertigo",
    "seeing stars",
    "blacked out",
    "went black",
    "nearly fainted",
    "felt faint",
    "feel faint-ish",
    "nearly passed out",
    "almost passed out",
    "feel like passing out",
    "vision went blurry",
    "ears ringing and dizzy",
    "dizy",
    # numbness
    "pins and needles",
    "my arm went numb",
    "fingers are tingly",
    "tingly hands",
    "can't feel my fingers",
    "cant feel my fingers",
    "lost feeling in my leg",
    "lost sensation",
    "arm went dead",
    "hand fell asleep",
    "my arm feels heavy and weird",
    "numbb",
    # weakness: limb-specific only
    "arm went weak",
    "my leg gave way",
    "my knee gave way",
    # something popped
    "felt a pop in my knee",
    "heard a pop",
    "heard it pop",
    "felt it pop",
    "it went pop",
    "knee popped",
    "shoulder popped",
    "my back popped",
    "it popped",
    "shoulder popped out",
    "hip popped out of place",
    "elbow snapped",
    "something went in my back",
    "my back went",
    "my lower back gave out",
    "back spasm",
    "my knee buckled",
    "heard a crack",
    "felt a crack",
    "something cracked",
    "cracked my knee",
    "something clicked in my shoulder",
    "dislocated",
    # nausea
    "threw up",
    "vomited",
    "nauseous",
    # emoji
    "\U0001f915",  # 🤕
    "\U0001f635‍\U0001f4ab",  # 😵‍💫
    "\U0001f494",  # 💔
    "\U0001fac0",  # 🫀
)

_EN_MUST_NOT_HALT = (
    # everyday logging
    "8,8,6 at 42.5",
    "done",
    "ok done",
    "all good",
    "all sets done",
    "good session",
    "3x8 @ 60",
    "easy",
    "hard but done",
    "felt strong",
    "heavy today",
    "last set was a grind",
    "note: add weight next time",
    "bolt on the rack was loose",
    "lopsided bar",
    "travel day",
    "polo shirt",
    "kolaches after gym",
    "painted my room",
    "stretched after",
    "incline bench felt great",
    "pec deck",
    # chest/heart allowlist (A§7.1)
    "chest press felt good",
    "chest press 3x8 at 40",
    "chest press 3x10 at 40",
    "time for some chest press supersets",
    "chest day done",
    "chest and triceps",
    "chest workout was great",
    "did chest today",
    "chest 4x10",
    "upper chest focus",
    "heart rate 150",
    "did heart rate zone 2",
    "resting heart rate",
    "resting heart rate 55",
    "heart rate zone 2",
    "HR 150",
    "resting HR 55",
    "chest day chest day",
    # effort words next to an allowlisted mention are training talk, not symptoms
    "chest day, bench felt heavy",
    "chest day done, heavy session",
    "did chest, went heavy today",
    "heavy bench, chest day",
    "chest and triceps, great burn",
    "chest press 3x10, last set burned",
    "heart rate 150 on the bike, legs burning",
    # soreness / fatigue / cramps / exertional breathlessness
    "it's sore",
    "sore legs",
    "doms",
    "tight hamstrings",
    "felt weak today",
    "tired",
    "cramped up",
    "calf cramp",
    "short of breath",
    "shortness of breath",
    "out of breath after sprints",
    "can't catch my breath",
    # vague nausea / slang
    "felt sick",
    "felt sick of lunges",
    "sick set",
    "sick workout",
    "killer workout",
    "killer sets",
    "I killed it",
    "tore through the workout",
    "tore it up",
    "ripped",
    "ripped (felt ripped lol)",
    "popcorn",
    # stems replaced by phrases (A§7.1: plan-editing language)
    "tweak the plan",
    "tweak the plan: fewer lunges",
    "twinkies",
    "throwback",
    "crackers after training",
    "cracked the 100kg plateau",
    # Latin text that transliterates to a Russian stem must not halt for an English user
    "rvo",
    "zatek",
    "mutiny",
    "noetic",
    "boli",
    "kroll",
    "hrustle",
)

_RU_MUST_HALT = (
    # chest/heart: deny-by-default (A§7.1), `груд*` incl. `грудин*`, `сердц*`
    "грудь давит",
    "давит грудь",
    "в груди давит",
    "в груди колет",
    "колет в груди",
    "тяжесть в груди",
    "в груди тяжело",
    "сдавливает грудь",
    "сжимает грудь",
    "грудь сжимает",
    "жжет в груди",
    "жжёт в груди",
    "в груди жжет",
    "в груди жжёт",
    "за грудиной давит",
    "стеснение в груди",
    "щемит в груди",
    "давит в груди",
    "боль в груди",
    "дискомфорт в груди",
    "сердце колет",
    "сердце колотится",
    "сердце бьется неровно",
    "перебои в сердце",
    "аритмия",
    "в груди тяжело",
    "тяжесть в груди",
    # chest/heart: co-occurrence override beats the allowlist
    "после жима груди давит в груди",
    "день груди, в груди давит",
    # breathing
    "не могу дышать",
    "задыхаюсь",
    "задыхался",
    "задохнулся",
    "трудно дышать",
    "тяжело дышать",
    "не хватает воздуха",
    "нечем дышать",
    # pain
    "спина болит",
    "болит спина",
    "БОЛИТ СПИНА",
    "спина-болит",
    "болит",
    "больно",
    "ооочень больно",
    "боляче",
    "болить",
    "нет боли",
    "болью отдает",
    "поясницу прихватило",
    "прихватило спину",
    "потянул спину",
    "потянул мышцу",
    "растянул связки",
    "подвернул ногу",
    "стреляет в колене",
    "ноет колено",
    "колено ноет",
    "тянет в пояснице",
    "побаливает",
    "заболело колено",
    "разболелась спина",
    "колет в колене",
    "колет в боку",
    # dizziness
    "голова кружится",
    "кружится голова",
    "закружилась голова",
    "кружилась голова",
    "голова кружилась",
    "в глазах потемнело",
    "потемнело в глазах",
    "темнеет в глазах",
    "голова поплыла",
    "чуть не упал в обморок",
    "чуть не потерял сознание",
    "вырубился",
    "штормит",
    "повело",
    # nausea
    "мутит",
    "тошнит",
    "тошнило",
    "тошнота",
    "подташнивает",
    "вырвало",
    "стошнило",
    # numbness
    "онемела рука",
    "рука онемела",
    "немеют пальцы",
    "пальцы немеют",
    "затекла рука",
    "отнялась нога",
    "занемела рука",
    "свело ногу",
    "мурашки в руке",
    "покалывает в пальцах",
    "не чувствую пальцы",
    "немота в руке",
    "рука не чувствует",
    # weakness: limb-specific only
    "слабость в руке",
    "слабость в ноге",
    "ватные ноги",
    # something popped
    "что-то щёлкнуло в колене",
    "что-то щёлкнуло",
    "щелкнуло в плече",
    "щелкает колено",
    "колено щелкает",
    "хрустнуло колено",
    "что-то хрустнуло",
    "в колене что-то хрустнуло",
    "хруст в колене",
    "хрустит плечо",
    "щелчок в колене",
    "почувствовал щелчок",
    "лопнуло",
    "что-то порвалось",
    "порвалось",
    "растяжение",
    "травмировал плечо",
    "травма",
    # Latin keyboard
    "bolit",
    "bolno",
    "golova kruzhitsya",
    "kolet v grudi",
    "бoлит (latin o)",
)

_RU_MUST_NOT_HALT = (
    # everyday logging
    "легко",
    "тяжело но сделал",
    "норм",
    "отлично",
    "сделал все",
    "сделал все подходы",
    "8,8,6 на 42.5",
    "жим лежа 60 кг",
    "жим лежа",
    "пульс 150",
    "разминка",
    "растяжка",
    "кардио на дорожке",
    "сделай план полегче",
    # chest/heart allowlist (A§7.1: chest-day slang is everyday logging)
    "жим груди норм",
    "жим груди 3 по 8",
    "жим груди 3х10",
    "сделал грудь и трицепс",
    "день груди",
    "качал грудь",
    "тренировка груди",
    "грудь и трицепс",
    "грудь и бицепс",
    "грудные мышцы",
    "мышцы груди",
    "на грудь",
    "грудных",
    "грудак",
    "сердечно легочная выносливость",
    # effort words next to an allowlisted mention are training talk, not symptoms
    "день груди, тяжело но сделал",
    "жим груди, тяжело",
    "жим груди 3х10, тяжело шло",
    "тренировка груди, жжение в мышцах",
    # soreness / fatigue / exertional breathlessness
    "крепатура",
    "небольшая крепатура после тренировки",
    "забитые мышцы",
    "одышка",
    "одышка после бега",
    "устал",
    "слабость",
    "свело икру немного, но норм",
    # innocent words sharing a prefix with a term (see also the stem negative tests)
    "болото",
    "болтали",
    "болельщики",
    "большой вес",
    "боль\xadшой вес",
    "больше не могу",
    "приболел немного но норм",
    "прихватил гантели",
    "колеса",
    "колени в стороны",
    "повелся",
    "лопатки сведены",
    "кружка протеина",
    "травка",
    # Latin keyboard: ordinary Russian logging
    "legko",
    "normalno",
    "sdelal vse",
    "zhim lezha 60",
    "bolshoy ves",
    "bolshe ne mogu",
    "vse ok",
    "poteli",
    "krasota",
    "kolesa",
    "travka",
    "lopata",
)


@pytest.mark.parametrize("phrase", _EN_MUST_HALT)
def test_english_corpus_halts(phrase: str) -> None:
    assert scan(phrase, "en") is not None, phrase


@pytest.mark.parametrize("phrase", _EN_MUST_NOT_HALT)
def test_english_corpus_does_not_halt(phrase: str) -> None:
    assert scan(phrase, "en") is None, phrase


@pytest.mark.parametrize("phrase", _RU_MUST_HALT)
def test_russian_corpus_halts(phrase: str) -> None:
    assert scan(phrase, "ru") is not None, phrase


@pytest.mark.parametrize("phrase", _RU_MUST_NOT_HALT)
def test_russian_corpus_does_not_halt(phrase: str) -> None:
    assert scan(phrase, "ru") is None, phrase


# Latin-keyboard Russian ("bolit", "бoлит" with a Latin "o") is only transliterated for
# `lang="ru"`, so the cross-language check below covers the Cyrillic phrases only.
_RU_MUST_HALT_CYRILLIC = tuple(
    phrase for phrase in _RU_MUST_HALT if not any("a" <= ch <= "z" for ch in phrase.casefold())
)
# Contrived Latin strings that only exist to probe the `lang="en"` transliteration path.
_EN_LATIN_PROBES = ("rvo", "zatek", "mutiny", "noetic", "boli", "kroll", "hrustle")
_EN_MUST_NOT_HALT_ORDINARY = tuple(p for p in _EN_MUST_NOT_HALT if p not in _EN_LATIN_PROBES)


@pytest.mark.parametrize("phrase", _RU_MUST_HALT_CYRILLIC)
def test_russian_corpus_halts_when_the_interface_language_is_english(phrase: str) -> None:
    """Users mix languages: Cyrillic stop words halt under `lang="en"` too."""
    assert scan(phrase, "en") is not None, phrase


@pytest.mark.parametrize("phrase", _EN_MUST_NOT_HALT_ORDINARY)
def test_english_logging_does_not_halt_when_the_interface_language_is_russian(
    phrase: str,
) -> None:
    """A Russian-interface user typing ordinary English logging text: the transliteration
    pass must not turn it into a Russian stop word."""
    assert scan(phrase, "ru") is None, phrase


# --- Ordinary logging sweep: the exact list from the M2 acceptance criteria. ---


@pytest.mark.parametrize(
    ("phrase", "lang"),
    [
        ("8,8,6 at 42.5", "en"),
        ("done", "en"),
        ("all good", "en"),
        ("3x8 @ 60", "en"),
        ("легко", "ru"),
        ("тяжело но сделал", "ru"),
        ("норм", "ru"),
        ("пульс 150", "ru"),
        ("растяжка", "ru"),
        ("крепатура", "ru"),
        ("одышка после бега", "ru"),
        ("tweak the plan: fewer lunges", "en"),
        ("сделай план полегче", "ru"),
    ],
)
def test_ordinary_logging_text_does_not_halt(phrase: str, lang: str) -> None:
    assert scan(phrase, lang) is None, phrase


# --- Category smoke tests (one clear example per category, per language). ---


def test_no_pain_today_halts_in_english() -> None:
    """A§7.1: matching is deliberately broad. This is the canonical example."""
    hit = scan("no pain today", "en")
    assert hit is not None
    assert hit.category == "pain"


def test_net_boli_halts_in_russian() -> None:
    hit = scan("нет боли", "ru")
    assert hit is not None
    assert hit.category == "pain"


def test_english_dizziness_category() -> None:
    hit = scan("I feel really dizzy right now", "en")
    assert hit is not None
    assert hit.category == "dizziness"


def test_english_numbness_category() -> None:
    hit = scan("my hand feels numb", "en")
    assert hit is not None
    assert hit.category == "numbness"


def test_english_chest_or_heart_category() -> None:
    hit = scan("I have some chest discomfort", "en")
    assert hit is not None
    assert hit.category == "chest_or_heart"


def test_english_something_popped_category() -> None:
    hit = scan("something popped in my knee", "en")
    assert hit is not None
    assert hit.category == "something_popped"


def test_english_breathing_category() -> None:
    hit = scan("I can't breathe", "en")
    assert hit is not None
    assert hit.category == "breathing"


def test_english_nausea_category() -> None:
    """Nausea covers only unambiguous terms (A§7.1): "threw up", "vomited", "nauseous"; the
    vague "felt sick" is not one of them ("felt sick of lunges" is ordinary complaining)."""
    hit = scan("I threw up after that set", "en")
    assert hit is not None
    assert hit.category == "nausea"
    nauseous = scan("felt nauseous", "en")
    assert nauseous is not None
    assert nauseous.category == "nausea"
    assert scan("felt sick", "en") is None


def test_english_injury_category() -> None:
    hit = scan("injured my shoulder", "en")
    assert hit is not None
    assert hit.category == "injury"


def test_english_weakness_is_limb_specific_only() -> None:
    """A§7.1: "arm went weak"/"leg gave way" halt; bare "weak"/"tired" and cramps do not."""
    hit = scan("my arm went weak", "en")
    assert hit is not None
    assert hit.category == "weakness"
    assert scan("felt weak today", "en") is None
    assert scan("so tired", "en") is None
    assert scan("calf cramp", "en") is None


def test_russian_dizziness_category() -> None:
    hit = scan("голова кружится немного", "ru")
    assert hit is not None
    assert hit.category == "dizziness"


def test_russian_numbness_category() -> None:
    hit = scan("рука онемела после подхода", "ru")
    assert hit is not None
    assert hit.category == "numbness"


def test_russian_chest_or_heart_category() -> None:
    """Both chest (`груд*`) and heart (`сердц*`) are deny-by-default in Russian (A§7.1)."""
    heart = scan("сердце колотится", "ru")
    assert heart is not None
    assert heart.category == "chest_or_heart"
    chest = scan("давит в груди", "ru")
    assert chest is not None
    assert chest.category == "chest_or_heart"
    sternum = scan("за грудиной давит", "ru")
    assert sternum is not None
    assert sternum.category == "chest_or_heart"


def test_russian_something_popped_category() -> None:
    hit = scan("в колене что-то хрустнуло", "ru")
    assert hit is not None
    assert hit.category == "something_popped"


def test_russian_breathing_category() -> None:
    hit = scan("не могу дышать после подхода", "ru")
    assert hit is not None
    assert hit.category == "breathing"


def test_russian_nausea_category() -> None:
    hit = scan("меня тошнит", "ru")
    assert hit is not None
    assert hit.category == "nausea"


def test_russian_weakness_is_limb_specific_only() -> None:
    hit = scan("слабость в ногах", "ru")
    assert hit is not None
    assert hit.category == "weakness"
    assert scan("слабость", "ru") is None
    assert scan("устал", "ru") is None


# --- Soreness must not halt on its own (A§7.1). ---


def test_sore_does_not_halt_on_its_own_in_english() -> None:
    assert scan("my legs are sore today", "en") is None
    assert scan("feeling a bit stiff after yesterday", "en") is None


def test_krepatura_does_not_halt_on_its_own_in_russian() -> None:
    assert scan("небольшая крепатура после тренировки", "ru") is None


def test_no_match_returns_none() -> None:
    assert scan("let's do 3 sets of 8 reps", "en") is None


def test_scan_also_checks_the_other_supported_language() -> None:
    """Users mix languages: a Russian stop word must halt even when `lang="en"` (A§7 table)."""
    hit = scan("нет боли", "en")
    assert hit is not None
    assert hit.lang == "ru"


def test_unknown_language_falls_back_to_every_list() -> None:
    assert scan("no pain today", "de") is not None
    assert scan("нет боли", "de") is not None
    assert scan("all good", "de") is None


def test_matching_is_case_and_punctuation_insensitive() -> None:
    assert scan("NO PAIN, TODAY!!", "en") is not None


def test_scan_never_raises_on_empty_text() -> None:
    assert scan("", "en") is None
    assert scan("   ", "en") is None
    assert scan("!!!", "en") is None


# --- Chest/heart deny-by-default: the three-part A§7.1 design, spelled out. ---


def test_bare_chest_or_heart_token_halts() -> None:
    for phrase, lang in (("my chest", "en"), ("my heart", "en"), ("грудь", "ru"), ("сердце", "ru")):
        hit = scan(phrase, lang)
        assert hit is not None, phrase
        assert hit.category == "chest_or_heart", phrase


def test_allowlisted_phrases_suppress_the_deny_by_default_rule() -> None:
    """A bare "chest" would halt every mention of the "chest press" exercise; the allowlist
    exempts it, so only an actual discomfort phrase halts."""
    assert scan("time for some chest press supersets", "en") is None
    assert scan("chest day done", "en") is None
    assert scan("heart rate 150", "en") is None
    assert scan("день груди", "ru") is None
    assert scan("жим груди 3 по 8", "ru") is None


def test_chest_token_followed_by_a_set_log_is_allowlisted() -> None:
    """ "chest 4x10" is a bare exercise log, not a symptom (A§7.1 ruling)."""
    assert scan("chest 4x10", "en") is None
    assert scan("chest 3 x 12", "en") is None
    assert scan("грудь 3 по 8", "ru") is None
    assert scan("грудь 4х10", "ru") is None


def test_co_occurrence_override_halts_despite_an_allowlisted_phrase() -> None:
    """A chest/heart token plus a discomfort descriptor halts even when an allowlisted phrase
    is present (A§7.1 part 3)."""
    en = scan("after chest press my chest feels tight", "en")
    assert en is not None
    assert en.category == "chest_or_heart"
    ru = scan("после жима груди давит в груди", "ru")
    assert ru is not None
    assert ru.category == "chest_or_heart"


def test_repeated_allowlisted_phrases_are_all_stripped() -> None:
    """Back-to-back repeats share a space; a naive replace would leave the second one."""
    assert scan("chest day chest day", "en") is None
    assert scan("chest press chest press chest press", "en") is None
    assert scan("день груди день груди", "ru") is None


def test_effort_words_do_not_override_the_allowlist_but_symptom_words_do() -> None:
    """A§7.1: "heavy"/"burn"/"тяжело"/"жж*" are effort talk next to an allowlisted mention;
    they still halt next to a non-allowlisted token, since that token halts by itself."""
    assert scan("chest day, bench felt heavy", "en") is None
    assert scan("жим груди, тяжело", "ru") is None
    assert scan("chest day but chest feels tight", "en") is not None
    assert scan("after chest press my chest feels heavy", "en") is not None
    assert scan("my chest burned", "en") is not None
    assert scan("в груди тяжело", "ru") is not None
    assert scan("тяжесть в груди", "ru") is not None


def test_heart_stem_and_sternum_tokens_halt_but_heart_rate_stays_allowlisted() -> None:
    for phrase in ("irregular heartbeat", "heartbeat racing", "pressure behind my sternum"):
        hit = scan(phrase, "en")
        assert hit is not None, phrase
        assert hit.category == "chest_or_heart"
    ru = scan("аритмия", "ru")
    assert ru is not None
    assert ru.category == "chest_or_heart"
    assert scan("heart rate 150", "en") is None
    assert scan("resting heart rate 55", "en") is None
    assert scan("did heart rate zone 2", "en") is None


def test_a_descriptor_alone_does_not_halt() -> None:
    """The descriptor list only matters together with a chest/heart token: "heavy today" and
    "тяжело но сделал" are ordinary logging."""
    assert scan("heavy today", "en") is None
    assert scan("felt some pressure to perform", "en") is None
    assert scan("тяжело но сделал", "ru") is None


# --- Breathing (A§7.1): inability to breathe halts in every tense and spelling; exertional
# breathlessness does not. ---


@pytest.mark.parametrize(
    "phrase",
    [
        "can't breathe",
        "cant breathe",
        "cannot breathe",
        "can not breathe",
        "can’t breathe",
        "couldn't breathe",
        "couldnt breathe",
        "could not breathe",
        "hard to breathe",
        "struggling to breathe",
        "trouble breathing",
    ],
)
def test_breathing_distress_halts_in_every_spelling(phrase: str) -> None:
    hit = scan(phrase, "en")
    assert hit is not None, phrase
    assert hit.category == "breathing"


@pytest.mark.parametrize(
    ("phrase", "lang"),
    [
        ("out of breath", "en"),
        ("short of breath", "en"),
        ("can't catch my breath", "en"),
        ("немного одышка после кардио", "ru"),
    ],
)
def test_exertional_breathlessness_does_not_halt(phrase: str, lang: str) -> None:
    assert scan(phrase, lang) is None, phrase


# --- "killing me" pain slang (coordinator decision). ---


def test_killing_me_halts() -> None:
    hit = scan("this workout is killing me", "en")
    assert hit is not None
    assert hit.category == "pain"


# --- Russian stems: each has a negative test against an unrelated word that shares the same
# first letters (A§7.1: "each stem with negative tests against innocent words"). ---


@pytest.mark.parametrize(
    ("stem_word", "innocent_word"),
    [
        ("головокружение", "голова"),  # головокруж*: bare "head" isn't dizziness
        ("обморок", "оборот"),  # обморок*: "turn/revolution" is unrelated
        ("покалывает", "калина"),  # покалыва*: a berry/plant name is unrelated
        ("онемение", "оценка"),  # онеме*: "a rating/score" is unrelated
        ("растянул", "растение"),  # растян*: "a plant" is unrelated (растЕние vs растЯн)
        ("надорвал", "надоел"),  # надорв*: "annoyed/bored" is unrelated
        ("прихватило", "привет"),  # прихватило*: "hello" is unrelated
        ("прихватывает", "привет"),  # прихватыва*: "hello" is unrelated
        ("сердце", "серебро"),  # сердц* (chest/heart deny-default): "silver" is unrelated
        ("грудина", "груз"),  # груд* (chest/heart deny-default): "cargo/load" is unrelated
        ("побаливает", "побаловать"),  # побалива*: "to indulge/spoil" is unrelated
        ("заболело", "забота"),  # заболе*: "care/concern" is unrelated
        ("разболелась", "разбор"),  # разболе*: "an analysis/breakdown" is unrelated
        ("кольнуло", "кольцо"),  # кольн*: "a ring" is unrelated
        ("кружилась", "кружка"),  # кружил*: "a mug/cup" is unrelated
        ("занемела", "занести"),  # занеме*: "to bring in" is unrelated
        ("затекла", "затея"),  # затек*: "an undertaking/scheme" is unrelated
        ("отнялась", "отношение"),  # отнял*: "a relation/attitude" is unrelated
        ("хрустнуло", "хрупкий"),  # хруст*: "fragile" is unrelated
        ("щелчок", "щель"),  # щелч*: "a crack/gap" is unrelated
        ("щелкает", "щелочь"),  # щелк*: "alkali" is unrelated
        ("лопнуло", "лопата"),  # лопн*: "a shovel" is unrelated
        ("травмировал", "трава"),  # травм*: "grass" is unrelated
        ("задыхался", "задача"),  # задыха*: "a task/problem" is unrelated
        ("задохнулся", "задор"),  # задохн*: "enthusiasm/zest" is unrelated
    ],
)
def test_russian_stem_matches_the_concerning_word_and_not_the_innocent_one(
    stem_word: str, innocent_word: str
) -> None:
    assert scan(stem_word, "ru") is not None, stem_word
    assert scan(innocent_word, "ru") is None, innocent_word


def test_prihvatil_gantely_does_not_halt() -> None:
    """ "прихватил гантели" ("grabbed the dumbbells") is ordinary logging, not "my back
    seized up". прихват* was narrowed to прихватило*/прихватывает specifically so this stays
    unmatched."""
    assert scan("прихватил гантели", "ru") is None


# --- EN stems and the phrases that replaced the colliding ones (A§7.1). ---


@pytest.mark.parametrize(
    "phrase",
    [
        "twinging a bit",
        "sprained",
        "straining hard",
        "throbbed a little",
        "dislocating",
        "palpitating",
        "injuries",
    ],
)
def test_english_stems_match_their_inflected_forms(phrase: str) -> None:
    assert scan(phrase, "en") is not None, phrase


def test_tweak_is_a_phrase_not_a_stem() -> None:
    """`tweak*` collided with plan-editing language, so only the injury phrases halt."""
    for phrase in ("tweaked my back", "tweak in my back", "tweaked it", "just tweaked"):
        hit = scan(phrase, "en")
        assert hit is not None, phrase
        assert hit.category == "pain"
    assert scan("tweak the plan", "en") is None
    assert scan("tweaking my program", "en") is None


def test_crack_is_a_phrase_not_a_stem() -> None:
    """`crack*` collided with "cracked the plateau"/"crackers", so only the injury phrases
    halt."""
    for phrase in ("heard a crack", "felt a crack", "something cracked", "cracked my knee"):
        hit = scan(phrase, "en")
        assert hit is not None, phrase
        assert hit.category == "something_popped"
    assert scan("cracked the 100kg plateau", "en") is None
    assert scan("crackers after training", "en") is None
    assert scan("cracking my knuckles", "en") is None


def test_bare_popped_halts() -> None:
    """A§7.1: bare "popped" halts; "popped a PR" halting is an accepted false positive."""
    for phrase in ("shoulder popped", "my back popped", "it popped", "heard it pop"):
        hit = scan(phrase, "en")
        assert hit is not None, phrase
        assert hit.category == "something_popped"


# --- Normalization (A§7.1): Cf character stripping, "can't" folding, emoji, transliteration. ---


def test_zero_width_space_inside_a_word_does_not_dodge_a_match() -> None:
    """A zero-width space (U+200B, category Cf) inserted mid-word must be removed before
    punctuation stripping, not turned into a word-splitting space."""
    hit = scan("бо​лит", "ru")
    assert hit is not None
    assert hit.category == "pain"


def test_soft_hyphen_inside_a_word_does_not_create_a_false_match() -> None:
    """A soft hyphen (U+00AD, category Cf) inserted inside "большой" ("big") must not split
    it into "боль" + "шой", which would falsely match the pain term "боль"."""
    assert scan("боль\xadшой вес", "ru") is None


def test_cant_variants_all_normalize_to_one_form() -> None:
    for phrase in ("can't breathe", "cant breathe", "cannot breathe", "can not breathe"):
        hit = scan(phrase, "en")
        assert hit is not None, phrase
        assert hit.category == "breathing"


@pytest.mark.parametrize(
    ("emoji", "category"),
    [
        ("\U0001f915", "pain"),  # 🤕
        ("\U0001f635‍\U0001f4ab", "dizziness"),  # 😵‍💫
        ("\U0001fac0", "chest_or_heart"),  # 🫀
        ("\U0001f494", "chest_or_heart"),  # 💔
        ("\U0001f922", "nausea"),  # 🤢
        ("\U0001f92e", "nausea"),  # 🤮
    ],
)
def test_emoji_map_to_their_category(emoji: str, category: str) -> None:
    hit = scan(f"training update {emoji}", "en")
    assert hit is not None
    assert hit.category == category


@pytest.mark.parametrize(
    "phrase",
    ["bolit", "golova kruzhitsya", "onemela ruka", "kolet v grudi", "davit v grudi"],
)
def test_latin_transliteration_halts_the_russian_equivalent(phrase: str) -> None:
    hit = scan(phrase, "ru")
    assert hit is not None, phrase
    assert hit.lang == "ru"


def test_latin_transliteration_only_runs_for_a_russian_interface() -> None:
    """For an English-language user, Latin text is English: "boli"/"zatek" must not become
    Russian stop words. The same strings do halt for a Russian-interface user, where a
    Latin keyboard is the scenario the pass exists for."""
    assert scan("boli", "en") is None
    assert scan("bolit", "en") is None
    assert scan("bolit", "ru") is not None


# --- stop_words.to_verdict and the real A§7.4 "safety_signal=false does not clear a
# stop-word hit" path. ---


def test_to_verdict_on_no_hit_is_ok() -> None:
    verdict = to_verdict(scan("3 sets of 8 reps", "en"))
    assert verdict.ok is True
    assert verdict.rule == "stop_words.scan"


def test_to_verdict_on_a_hit_is_not_ok() -> None:
    verdict = to_verdict(scan("no pain today", "en"))
    assert verdict.ok is False
    assert verdict.rule == "stop_words.scan"


def test_llm_safety_signal_false_does_not_clear_a_real_stop_word_hit() -> None:
    """A§7.4, through the real path: scan finds a hit, `to_verdict` turns it into a blocking
    `GuardVerdict`, and `combine_with_llm_signal` with `llm_safety_signal=False` must not
    clear it."""
    verdict = to_verdict(scan("no pain today", "en"))
    combined = combine_with_llm_signal(verdict, llm_safety_signal=False)
    assert combined.ok is False
