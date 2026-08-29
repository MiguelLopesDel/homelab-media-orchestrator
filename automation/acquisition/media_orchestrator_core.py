"""Pure reconciliation policy for media acquisitions.

The public interface is intentionally small: ``reconcile_scope`` decides the
durable continuation for one requested scope, and ``select_safe_imports``
filters Sonarr manual-import previews.  Network and persistence adapters live
outside this module.

The plan deliberately represents the whole request lifecycle, rather than
only download recovery.  A request is complete only after its explicitly
requested episodes are imported *and* the per-episode language policy has
been checked.  Jellyseerr availability is useful evidence, but it is not a
completion authority: it may be updated before Sonarr imports every file or
before sidecars have been published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


def scope_is_available(request: dict[str, Any], season_number: int) -> bool:
    """Return Seerr availability for one requested TV season.

    Seerr may mark the media as a whole available once a regular season is
    present while a separately requested Specials season is still processing.
    Prefer the per-request-season status whenever the API provides it.
    """
    statuses = request.get("requested_season_statuses")
    if isinstance(statuses, dict):
        status = statuses.get(season_number, statuses.get(str(season_number)))
        if status is not None:
            return status == 5
    return request.get("media_status") == 5


def satisfied_queue_ids(
    missing_episodes: int,
    queued_items: Iterable[dict[str, Any]],
    torrents_by_hash: dict[str, dict[str, Any]],
    *,
    available_episode_ids: set[int] | None = None,
) -> list[int]:
    """Find stale Sonarr warnings for a fully imported, completed artifact.

    These queue rows may be detached from Sonarr while leaving the download
    client untouched.  Any missing episode or incomplete torrent makes the
    operation unsafe.
    """
    available_episode_ids = available_episode_ids or set()
    output: set[int] = set()
    for item in queued_items:
        episode_ids = {
            episode_id for episode_id in item.get("episode_ids", [])
            if isinstance(episode_id, int)
        }
        item_is_satisfied = missing_episodes == 0 or (
            bool(episode_ids) and episode_ids.issubset(available_episode_ids)
        )
        if not item_is_satisfied:
            continue
        download_id = str(item.get("download_id") or "").casefold()
        torrent = torrents_by_hash.get(download_id)
        if (
            isinstance(item.get("id"), int)
            and str(item.get("status") or "").casefold() == "completed"
            and str(item.get("tracked_download_state") or "").casefold() == "importblocked"
            and torrent is not None
            and float(torrent.get("progress") or 0) == 1
        ):
            output.add(item["id"])
    return sorted(output)


def release_title_starts_with_series(title: str, aliases: Iterable[str]) -> bool:
    """Require the release's actual title to begin with a known series alias."""
    stripped = re.sub(r"^\s*(?:(?:\[[^]]+\]|\([^)]*\))\s*)+", "", title)
    normalized_release = " ".join(re.findall(r"[a-z0-9]+", stripped.casefold()))
    normalized_aliases = (
        " ".join(re.findall(r"[a-z0-9]+", alias.casefold())) for alias in aliases
    )
    return any(
        normalized_release.startswith(alias)
        for alias in normalized_aliases if len(alias) >= 6
    )


def dead_near_empty_candidate(
    torrent: dict[str, Any],
    *,
    stalled_samples: int,
    stalled_seconds: int,
    minimum_stall_seconds: int = 10 * 60,
) -> bool:
    """Return whether a torrent is proven dead enough to replace safely.

    Sample counts alone exaggerate the observed duration when a torrent is
    first seen between polling boundaries.  Require both repeated samples and
    elapsed wall-clock time with no progress before allowing removal.
    """
    return (
        int(torrent.get("downloaded") or 0) <= 1_048_576
        and int(torrent.get("dlspeed") or 0) == 0
        and float(torrent.get("availability") or 0) <= 0
        and stalled_samples >= 2
        and stalled_seconds >= minimum_stall_seconds
    )


@dataclass(frozen=True)
class ScopeFacts:
    request_id: int
    series_id: int
    season_number: int
    missing_episodes: int
    queue_episodes: int
    import_blocked: bool = False
    active_search: bool = False
    safe_import_count: int = 0
    cached_candidate_count: int = 0
    satisfied_by_seerr: bool = False
    language_pending_episodes: int = 0
    language_unscanned_episodes: int = 0


@dataclass(frozen=True)
class PreviousPlan:
    lifecycle_state: str
    action: str | None
    next_action_at: int | None
    attempt_count: int
    last_action_at: int | None


@dataclass(frozen=True)
class ReconciliationPlan:
    lifecycle_state: str
    action: str | None
    next_action_at: int | None
    reason: str
    attempt_count: int


def reconcile_scope(
    facts: ScopeFacts,
    previous: PreviousPlan | None,
    now: int,
    *,
    retry_delays: tuple[int, ...] = (30 * 60, 2 * 60 * 60, 6 * 60 * 60, 24 * 60 * 60),
) -> ReconciliationPlan:
    """Return exactly one durable continuation for a requested scope."""
    attempts = previous.attempt_count if previous else 0
    # Seerr's status is intentionally never a terminal authority.  It can say
    # ``available`` for a show while an explicitly requested season/special is
    # still missing, and cannot verify external PT-BR sidecars.
    if facts.missing_episodes == 0:
        if facts.language_unscanned_episodes:
            return ReconciliationPlan(
                "verifying_language", "refresh_language_audit", now,
                f"{facts.language_unscanned_episodes} episódio(s) importado(s) ainda não foram auditados por áudio/legenda.",
                attempts,
            )
        if facts.language_pending_episodes:
            return ReconciliationPlan(
                "remediating_language", "await_language_pipeline", now + 15 * 60,
                f"{facts.language_pending_episodes} episódio(s) aguardam áudio PT-BR ou legenda PT-BR conforme a política.",
                attempts,
            )
        return ReconciliationPlan(
            "complete", None, None,
            "Todos os episódios solicitados foram importados e passaram pela política de idioma.",
            attempts,
        )
    if facts.safe_import_count:
        return ReconciliationPlan(
            "resolving_import", "import_safe_files", now,
            f"{facts.safe_import_count} arquivo(s) já baixado(s) possuem mapeamento inequívoco.", attempts,
        )
    if facts.import_blocked:
        return ReconciliationPlan(
            "resolving_import", "reconcile_scope", now,
            "Há download bloqueado; tentar importação segura sem interromper os demais episódios ausentes.", attempts,
        )
    if facts.queue_episodes:
        return ReconciliationPlan("downloading", None, None, "Há itens ativos na fila do Sonarr.", attempts)
    if facts.active_search:
        return ReconciliationPlan("searching", None, None, "Uma busca limitada já está em execução.", attempts)

    if previous and previous.next_action_at and previous.next_action_at > now:
        return ReconciliationPlan(
            previous.lifecycle_state, previous.action, previous.next_action_at,
            "Aguardando o cooldown persistido antes da próxima tentativa.", attempts,
        )

    if facts.cached_candidate_count:
        return ReconciliationPlan(
            "retry_scheduled", "try_cached_candidate", now,
            "Há candidatos cifrados no cache; tentar um antes de consultar indexadores.", attempts,
        )

    delay = retry_delays[min(attempts, len(retry_delays) - 1)]
    return ReconciliationPlan(
        "retry_scheduled", "reconcile_scope", now,
        f"Inspecionar artefatos e cache; somente se ambos estiverem vazios, buscar um episódio e depois aguardar {delay} segundos.",
        attempts,
    )


def _allowed_folder_rejection(reason: str) -> bool:
    normalized = reason.casefold()
    folder_mismatch = "was unexpected considering the" in normalized and "folder name" in normalized
    history_id_match = (
        "found matching series via grab history" in normalized
        and "matched to series by id" in normalized
    )
    return folder_mismatch or history_id_match


def select_safe_imports(
    previews: Iterable[dict[str, Any]], requested_missing_episode_ids: set[int]
) -> list[dict[str, Any]]:
    """Select non-overwriting one-file/one-episode imports Sonarr already mapped.

    The only overridden rejections are Sonarr's pack-folder-name mismatch and
    its equivalent grab-history/series-ID safeguard. Parser failures, samples,
    invalid episodes and existing-file collisions stay out.
    """
    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in previews:
        episodes = item.get("episodes") or []
        if len(episodes) != 1:
            continue
        episode = episodes[0]
        episode_id = episode.get("id")
        if episode_id not in requested_missing_episode_ids or episode.get("hasFile"):
            continue
        quality = item.get("quality") or {}
        quality_value = quality.get("quality") or {}
        if not quality_value.get("id") or not item.get("path"):
            continue
        rejections = item.get("rejections") or []
        reasons = [r.get("reason", "") if isinstance(r, dict) else str(r) for r in rejections]
        if any(not _allowed_folder_rejection(reason) for reason in reasons):
            continue
        candidates.append((episode_id, item))

    counts: dict[int, int] = {}
    for episode_id, _ in candidates:
        counts[episode_id] = counts.get(episode_id, 0) + 1
    return [item for episode_id, item in candidates if counts[episode_id] == 1]
