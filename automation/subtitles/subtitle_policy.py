"""Pure per-episode language policy for the media acquisition stack.

This module deliberately knows nothing about Sonarr, Bazarr, ffprobe, Discord,
or translation providers.  Adapters collect facts and execute at most one
action returned here.  Keeping the policy pure makes retries and quota limits
predictable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


TranslationStatus = Literal["queued", "running", "completed", "failed"]
EpisodeAction = Literal[
    "materialize_portuguese_subtitle",
    "materialize_english_subtitle",
    "search_bazarr",
    "queue_text_translation",
    "queue_ocr_translation",
]
EpisodeAlert = Literal["dub_available_but_missing", "missing_portuguese"]


@dataclass(frozen=True)
class EpisodeLanguageFacts:
    age_seconds: int
    has_pt_audio: bool
    has_pt_subtitle: bool
    has_pt_external_subtitle: bool
    has_english_text_subtitle: bool
    has_english_bitmap_subtitle: bool
    has_english_external_subtitle: bool
    dub_available: bool
    bazarr_attempts: int
    seconds_since_bazarr_attempt: int | None
    translation_status: TranslationStatus | None


@dataclass(frozen=True)
class LanguagePolicy:
    initial_bazarr_grace_seconds: int = 24 * 3600
    bazarr_retry_seconds: int = 12 * 3600
    max_bazarr_attempts: int = 2
    translation_delay_seconds: int = 48 * 3600
    alert_delay_seconds: int = 24 * 3600


@dataclass(frozen=True)
class EpisodeDecision:
    state: str
    action: EpisodeAction | None = None
    alerts: tuple[EpisodeAlert, ...] = ()


def evaluate_episode(
    facts: EpisodeLanguageFacts, policy: LanguagePolicy
) -> EpisodeDecision:
    """Return the stable state, next action, and currently true alert conditions."""

    if facts.has_pt_audio:
        return EpisodeDecision(state="dubbed")

    # A confirmed dub changes the fulfilment target for the episode.  A
    # subtitle (existing or obtainable) does not satisfy that target and must
    # not spend Bazarr/translation quota.  Fresh episodes are only reported
    # after the normal alert grace period.
    if facts.dub_available:
        alerts: tuple[EpisodeAlert, ...] = (
            ("dub_available_but_missing",)
            if facts.age_seconds >= policy.alert_delay_seconds
            else ()
        )
        return EpisodeDecision(state="missing_dub", alerts=alerts)

    if facts.has_pt_subtitle:
        return EpisodeDecision(state="pt_subtitled")

    if facts.has_pt_external_subtitle:
        return EpisodeDecision(
            state="portuguese_subtitle_source",
            action="materialize_portuguese_subtitle",
        )

    if facts.has_english_external_subtitle:
        alerts_list: list[EpisodeAlert] = []
        if facts.age_seconds >= policy.alert_delay_seconds:
            alerts_list.append("missing_portuguese")
        return EpisodeDecision(
            state="english_subtitle_source",
            action="materialize_english_subtitle",
            alerts=tuple(alerts_list),
        )

    alerts_list: list[EpisodeAlert] = []
    if facts.age_seconds >= policy.alert_delay_seconds:
        alerts_list.append("missing_portuguese")
    alerts = tuple(alerts_list)

    if facts.translation_status in {"queued", "running", "completed", "failed"}:
        return EpisodeDecision(
            state=f"translation_{facts.translation_status}", alerts=alerts
        )

    if facts.age_seconds < policy.initial_bazarr_grace_seconds:
        return EpisodeDecision(state="bazarr_grace", alerts=alerts)

    if facts.bazarr_attempts < policy.max_bazarr_attempts:
        retry_due = (
            facts.bazarr_attempts == 0
            or facts.seconds_since_bazarr_attempt is None
            or facts.seconds_since_bazarr_attempt >= policy.bazarr_retry_seconds
        )
        if retry_due:
            return EpisodeDecision(
                state="missing_portuguese", action="search_bazarr", alerts=alerts
            )
        return EpisodeDecision(state="wait_bazarr", alerts=alerts)

    if facts.age_seconds < policy.translation_delay_seconds:
        return EpisodeDecision(state="wait_translation", alerts=alerts)

    if facts.has_english_text_subtitle:
        return EpisodeDecision(
            state="missing_portuguese",
            action="queue_text_translation",
            alerts=alerts,
        )

    if facts.has_english_bitmap_subtitle:
        return EpisodeDecision(
            state="missing_portuguese",
            action="queue_ocr_translation",
            alerts=alerts,
        )

    return EpisodeDecision(state="blocked_no_source", alerts=alerts)
