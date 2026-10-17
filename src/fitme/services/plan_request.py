"""ADR 0017: what a plan request says about the plan itself, read deterministically from its
words before anything is retrieved or generated.

A request such as "легкую беговую по субботам на свежем воздухе" or "bjj два раза в неделю"
used to reach the planner with the profile's frame (three gym sessions, focus on strength,
the lifts with history) and only the request's quality words ("размин" -> mobility) on top,
so the profile won. This module reads the rest: the activity it is about, the qualities that
activity asks for, the number of sessions, the weekdays, the session length and whether it
is outdoors. When it names an activity or a quality, the request is *understood*, and the
planner gets the profile's physical data only (`planning.apply_request`): age, weight,
experience, screening and the training done elsewhere, but no focus, no preferences and no
history that serves none of the requested qualities.

Small and explicit on purpose, like `exercise_search._QUALITY_WORDS`: words the owner uses,
not a language model. Only the affirmative part of the request counts
(`exercise_search.positive_request`): "не на улице" is not outdoors.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from fitme.domain.enums import Activity, Quality
from fitme.services import exercise_search

# Word stems (prefixes of the request's folded words) that name an activity. "box" alone
# would match "box jump"; "run" alone is fine ("shuttle run" is still running).
_ACTIVITY_WORDS: dict[Activity, tuple[str, ...]] = {
    Activity.RUNNING: ("бег", "пробеж", "побега", "run", "jog", "джог"),
    Activity.CYCLING: ("велос", "велотрен", "cycl", "bike", "biking"),
    Activity.SWIMMING: ("плаван", "поплава", "swim", "бассейн"),
    Activity.BOXING: (
        "бокс", "кикбокс", "boxing", "boxer", "kickbox", "muay", "тайск", "mma", "мма",
    ),
    Activity.GRAPPLING: (
        "bjj", "джиу", "jiu", "grappl", "грэпл", "грэппл", "грапл", "борьб", "борец",
        "wrestl", "самбо", "sambo", "дзюдо", "judo",
    ),
    Activity.CLIMBING: ("скалолаз", "climb", "болдер", "boulder"),
}  # fmt: skip

# An activity the catalog can carry itself (easy jogs, interval runs): with no other quality
# named, the sessions *are* that activity — warm-up, the activity, cool-down.
_PRACTICE_QUALITIES: dict[Activity, tuple[Quality, ...]] = {
    Activity.RUNNING: (Quality.CONDITIONING, Quality.MOBILITY),
}
# The catalog's own exercises for an activity it can carry: always offered to the planner
# (the pool's per-quality picks are otherwise alphabetical among the conditioning work, and
# "беговую" doesn't match "Лёгкий бег" lexically). Only those the user may do reach it.
ACTIVITY_EXERCISES: dict[Activity, tuple[str, ...]] = {
    Activity.RUNNING: (
        "easy_jog",
        "outdoor_interval_jog",
        "short_stride_run",
        "brisk_walk",
        "mobility_runners_stretch",
    ),
}
# Supporting work for an activity done elsewhere (its skills, rounds and sparring are not in
# the catalog). Running, cycling, swimming, climbing and boxing as in ADR 0009's
# `_SPORT_QUALITIES`; grappling: pulling and grip strength, strength that lasts through
# rounds, a strong trunk and hips that move.
_SUPPORT_QUALITIES: dict[Activity, tuple[Quality, ...]] = {
    Activity.RUNNING: (Quality.STRENGTH, Quality.CORE_STABILITY, Quality.MOBILITY),
    Activity.CYCLING: (Quality.STRENGTH, Quality.CORE_STABILITY, Quality.MOBILITY),
    Activity.SWIMMING: (Quality.STRENGTH, Quality.CORE_STABILITY, Quality.MOBILITY),
    Activity.CLIMBING: (Quality.STRENGTH, Quality.CORE_STABILITY, Quality.MOBILITY),
    Activity.BOXING: (
        Quality.POWER,
        Quality.ROTATIONAL_POWER,
        Quality.AGILITY,
        Quality.CORE_STABILITY,
        Quality.CONDITIONING,
    ),
    Activity.GRAPPLING: (
        Quality.STRENGTH,
        Quality.MUSCULAR_ENDURANCE,
        Quality.CORE_STABILITY,
        Quality.MOBILITY,
        Quality.CONDITIONING,
    ),
}

_NUMBERS: dict[str, int] = {
    "один": 1, "одну": 1, "одна": 1, "one": 1,
    "два": 2, "две": 2, "two": 2,
    "три": 3, "three": 3,
    "четыре": 4, "four": 4,
    "пять": 5, "five": 5,
    "шесть": 6, "six": 6,
}  # fmt: skip
_NUMBER = r"(?P<n>[1-9]|" + "|".join(_NUMBERS) + r")"
_WEEK = r"(?:в|per|a|each)\s+нед\w*|(?:per|a|each)\s+week|weekly|/\s*нед\w*"
# "2 раза в неделю", "три дня в неделю", "two days a week", "2x в неделю".
_SESSIONS_RE = re.compile(
    rf"\b{_NUMBER}\s*(?:раз\w*|дн\w*|ден\w*|трениров\w*|занят\w*|x|х|times?|days?|"
    rf"sessions?|workouts?)\s+(?:{_WEEK})",
    re.UNICODE,
)
_ONCE_RE = re.compile(rf"\b(?:разок|раз|once)\s+(?:{_WEEK})", re.UNICODE)
_WORD_SESSIONS: dict[str, int] = {"дважды": 2, "twice": 2, "трижды": 3}
_WORD_SESSIONS_RE = re.compile(rf"\b(?P<w>дважды|twice|трижды)\s+(?:{_WEEK})", re.UNICODE)

_WEEKDAYS: tuple[tuple[int, str], ...] = (
    (0, r"понедельн\w*|mondays?"),
    (1, r"вторн\w*|tuesdays?"),
    (2, r"сред(?:а|у|ы|е|ам)|wednesdays?"),  # not "средний"
    (3, r"четверг\w*|thursdays?"),
    (4, r"пятниц\w*|fridays?"),
    (5, r"суббот\w*|saturdays?"),
    (6, r"воскресень\w*|sundays?"),
)
_WEEKDAY_RES = tuple((day, re.compile(rf"\b(?:{words})\b", re.UNICODE)) for day, words in _WEEKDAYS)

_MINUTES_RE = re.compile(r"\b(?P<n>\d{2,3})\s*(?:мин\w*|min\w*)", re.UNICODE)
_HOURS_RE = re.compile(r"\b(?P<n>[1-3](?:[.,]5)?)\s*(?:час\w*|h|hours?|hrs?)\b", re.UNICODE)
_HALF_HOUR_RE = re.compile(r"\b(?:полчаса|half an hour)\b", re.UNICODE)
_HOUR_AND_HALF_RE = re.compile(r"\b(?:полтора часа|an hour and a half)\b", re.UNICODE)
# "часок", "час", "an hour" — not "часто" (often) or "часы".
_ONE_HOUR_RE = re.compile(r"\b(?:час|часок|часик|an hour|one hour)\b", re.UNICODE)
_MIN_MINUTES = 15
_MAX_MINUTES = 180
_MAX_SESSIONS = 6

_OUTDOOR_RE = re.compile(
    r"\b(?:на улиц\w*|на свеж\w* воздух\w*|на природ\w*|в парк\w*|на стадион\w*|outdoors?|"
    r"outside|in the park)\b",
    re.UNICODE,
)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """What one request text says about the plan. Every field is empty when the text says
    nothing about it; `understood` is whether it names what the plan is *for*."""

    qualities: tuple[Quality, ...] = ()
    activity: Activity | None = None
    # The activity is the sessions' main work (`_PRACTICE_QUALITIES`), not supporting work.
    practice: bool = False
    sessions_per_week: int | None = None
    weekdays: tuple[int, ...] = ()  # 0 = Monday
    session_minutes: int | None = None
    outdoor: bool = False

    @property
    def understood(self) -> bool:
        return bool(self.qualities) or self.activity is not None

    @property
    def exercises(self) -> tuple[str, ...]:
        """`ACTIVITY_EXERCISES` when the sessions are the activity itself."""
        if self.activity is None or not self.practice:
            return ()
        return ACTIVITY_EXERCISES.get(self.activity, ())

    @property
    def sessions(self) -> int | None:
        """The sessions per week it asks for: the number it names, else its weekdays."""
        if self.sessions_per_week is not None:
            return self.sessions_per_week
        return len(self.weekdays) or None


def _activity(positive: str) -> Activity | None:
    """The activity the request names first."""
    words = [(match.start(), match.group()) for match in re.finditer(r"\w+", positive)]
    found = [
        (start, order, activity)
        for order, (activity, stems) in enumerate(_ACTIVITY_WORDS.items())
        for start, word in words
        if any(word.startswith(stem) for stem in stems)
    ]
    return min(found)[2] if found else None


def _sessions(positive: str) -> int | None:
    match = _SESSIONS_RE.search(positive)
    if match is not None:
        raw = match.group("n")
        count = int(raw) if raw.isdigit() else _NUMBERS[raw]
        return count if 1 <= count <= _MAX_SESSIONS else None
    word = _WORD_SESSIONS_RE.search(positive)
    if word is not None:
        return _WORD_SESSIONS[word.group("w")]
    if _ONCE_RE.search(positive) is not None:
        return 1
    return None


def _minutes(positive: str) -> int | None:
    minutes: int | None = None
    if (match := _MINUTES_RE.search(positive)) is not None:
        minutes = int(match.group("n"))
    elif (match := _HOURS_RE.search(positive)) is not None:
        minutes = round(float(match.group("n").replace(",", ".")) * 60)
    elif _HOUR_AND_HALF_RE.search(positive) is not None:
        minutes = 90
    elif _HALF_HOUR_RE.search(positive) is not None:
        minutes = 30
    elif _ONE_HOUR_RE.search(positive) is not None:
        minutes = 60
    if minutes is None or not _MIN_MINUTES <= minutes <= _MAX_MINUTES:
        return None
    return minutes


def read(text: str | None) -> PlanRequest:
    """Read one request (`None` or blank: an empty `PlanRequest`)."""
    if not text or not text.strip():
        return PlanRequest()
    positive = exercise_search.positive_request(text)
    activity = _activity(positive)
    named = exercise_search.requested_qualities(text)
    # The activity itself is the main work unless the request names another goal: a
    # warm-up or cool-down word ("размяться", mobility) doesn't make a run a strength day.
    own = _PRACTICE_QUALITIES.get(activity) if activity is not None else None
    practice = own is not None and set(named) <= set(own)
    if activity is None:
        qualities = list(named)
    elif own is not None and practice:
        qualities = list(own)
    else:
        qualities = [*named, *_SUPPORT_QUALITIES[activity]]
    qualities = list(dict.fromkeys(qualities))
    return PlanRequest(
        qualities=tuple(qualities),
        activity=activity,
        practice=practice,
        sessions_per_week=_sessions(positive),
        weekdays=tuple(day for day, pattern in _WEEKDAY_RES if pattern.search(positive)),
        session_minutes=_minutes(text.casefold()),  # "1,5 часа": the comma is no clause
        outdoor=_OUTDOOR_RE.search(positive) is not None,
    )


def plan_qualities(plan_qualities: Sequence[Sequence[Quality]]) -> tuple[Quality, ...]:
    """The qualities a plan's workouts declare, main objectives first: every workout's first,
    then the rest, without repeats. ADR 0017: what a revision that names no goal of its own
    keeps working toward, instead of the profile's focus."""
    ordered: list[Quality] = []
    for position in range(max((len(q) for q in plan_qualities), default=0)):
        for qualities in plan_qualities:
            if position < len(qualities) and qualities[position] not in ordered:
                ordered.append(qualities[position])
    return tuple(ordered)
