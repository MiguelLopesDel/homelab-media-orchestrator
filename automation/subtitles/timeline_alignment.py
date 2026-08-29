#!/usr/bin/env python3
"""Plan conservative piecewise alignment between two video editions.

The public interface is ``plan_timeline(source, target)``.  Extraction,
offset discovery, sequence smoothing and subtitle-aware safety checks stay
inside this module.  It returns data only; rendering remains owned by
``external_audio_builder``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from external_audio_builder import duration, probe


FRAME_WIDTH = 9
FRAME_HEIGHT = 8
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
SAMPLE_RATE = 4
MAX_OFFSET_SECONDS = 120
STRONG_DISTANCE = 8
GOOD_DISTANCE = 12
MAX_CANDIDATE_OFFSETS = 12
MIN_OFFSET_SUPPORT_SECONDS = 8
MIN_SOURCE_RUN_SECONDS = 3
MAX_BRIDGED_VISUAL_GAP_SECONDS = 10
MAX_TARGET_GAP_SECONDS = 60
MAX_TARGET_GAP_WITHOUT_SUBTITLES = 0.5
MAX_EDGE_GAP_SECONDS = 1.0
TRANSITION_TO_GAP_COST = 10
TRANSITION_BETWEEN_OFFSETS_COST = 48
GAP_EMISSION_COST = 15
TEXT_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"}
TEXT_SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".vtt"}
PT_CODES = {"pt", "por", "pob", "pb", "ptbr", "pt-br", "pt_br"}
EN_CODES = {"en", "eng", "en-us", "en-gb"}
CACHE_ROOT = Path(os.environ.get(
    "TIMELINE_CACHE_ROOT", "/srv/homelab/config/torrent-registry/timeline-fingerprints"
))


class SystemBusyError(RuntimeError):
    """The host is too busy for a bounded background decode right now."""


@dataclass(frozen=True)
class TimelinePlan:
    confidence: str
    reason: str
    segments: tuple[dict[str, float | str], ...]
    sample_rate: int
    source_samples: int
    target_samples: int
    candidate_offsets: tuple[float, ...]
    aligned_coverage: float
    fingerprint_match_ratio: float
    fingerprint_median_distance: float | None
    unmatched_target_seconds: float
    subtitle_evidence: str


def dhash(frame: bytes) -> int:
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"frame possui {len(frame)} bytes; esperado {FRAME_BYTES}")
    value = 0
    bit = 0
    for row in range(FRAME_HEIGHT):
        start = row * FRAME_WIDTH
        for column in range(FRAME_WIDTH - 1):
            if frame[start + column] > frame[start + column + 1]:
                value |= 1 << bit
            bit += 1
    return value


def _low_priority_prefix() -> list[str]:
    command = ["ionice", "-c3", "nice", "-n", "19"]
    if shutil.which("taskset"):
        command += ["taskset", "-c", "0"]
    return command


def ensure_safe_load(
    load_average: float | None = None, cpu_count: int | None = None
) -> None:
    load_average = os.getloadavg()[0] if load_average is None else load_average
    cpu_count = (os.cpu_count() or 1) if cpu_count is None else cpu_count
    limit = max(1.5, cpu_count * 0.75)
    if load_average > limit:
        raise SystemBusyError(
            f"host ocupado para alinhamento: load1={load_average:.2f}, limite={limit:.2f}"
        )


def extract_fingerprints(path: Path, sample_rate: int = SAMPLE_RATE) -> list[int]:
    """Decode once on one CPU and return tiny perceptual hashes."""
    if sample_rate <= 0:
        raise ValueError("sample_rate precisa ser positivo")
    stat = path.stat()
    signature = hashlib.sha256(
        f"v1\0{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{sample_rate}".encode()
    ).hexdigest()
    cache_path = CACHE_ROOT / f"{signature}.json"
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text())
        if isinstance(cached, list) and cached and all(isinstance(item, int) for item in cached):
            return cached
    command = _low_priority_prefix() + [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn",
        "-vf", (
            f"fps={sample_rate},"
            f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray"
        ),
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    result = subprocess.run(command, check=True, capture_output=True, timeout=20 * 60)
    raw = result.stdout
    if not raw or len(raw) % FRAME_BYTES:
        raise RuntimeError("ffmpeg retornou fingerprints vazios ou truncados")
    hashes = [
        dhash(raw[index:index + FRAME_BYTES])
        for index in range(0, len(raw), FRAME_BYTES)
    ]
    if CACHE_ROOT.parent.is_dir():
        CACHE_ROOT.mkdir(exist_ok=True)
        temporary = cache_path.with_name(cache_path.name + f".{os.getpid()}.partial")
        try:
            temporary.write_text(json.dumps(hashes, separators=(",", ":")))
            os.replace(temporary, cache_path)
        finally:
            temporary.unlink(missing_ok=True)
    return hashes


def _language(tags: dict[str, Any]) -> str:
    return str(tags.get("language", "")).strip().casefold().replace("_", "-")


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(".", ",").split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _subtitle_intervals_from_command(command: list[str]) -> list[tuple[float, float]]:
    text = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=5 * 60
    ).stdout
    pattern = re.compile(
        r"(?m)^(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
        r"(\d{2}:\d{2}:\d{2}[,.]\d{3})"
    )
    return [(_srt_seconds(start), _srt_seconds(end)) for start, end in pattern.findall(text)]


def _external_text_subtitle(path: Path) -> tuple[Path, str] | None:
    prefix = path.stem + "."
    candidates = []
    for sidecar in path.parent.iterdir():
        if (
            not sidecar.is_file()
            or not sidecar.name.startswith(prefix)
            or sidecar.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS
            or sidecar.stat().st_size <= 0
        ):
            continue
        middle = sidecar.name[len(prefix):-len(sidecar.suffix)].casefold()
        tokens = set(re.split(r"[._ -]+", middle))
        language = "por" if tokens & PT_CODES or "portugu" in middle else (
            "eng" if tokens & EN_CODES or "english" in tokens else "und"
        )
        if language == "und":
            continue
        if re.search(r"sign|song|karaoke|forced", middle):
            continue
        priority = 0 if language == "por" else 1
        candidates.append((priority, sidecar.name, sidecar, language))
    if not candidates:
        return None
    _, _, selected, language = min(candidates)
    return selected, language


def extract_dialogue_intervals(path: Path) -> tuple[list[tuple[float, float]] | None, str]:
    """Return cue intervals from the best embedded PT/EN text subtitle."""
    external = _external_text_subtitle(path)
    if external:
        sidecar, language = external
        command = _low_priority_prefix() + [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
            "-i", str(sidecar), "-f", "srt", "-",
        ]
        return _subtitle_intervals_from_command(command), f"external-text-subtitle:{language}"
    media = probe(path)
    choices: list[tuple[int, int, str]] = []
    relative_index = 0
    for stream in media.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        language = _language(stream.get("tags", {}))
        codec = str(stream.get("codec_name", "")).casefold()
        if codec in TEXT_SUBTITLE_CODECS:
            priority = 0 if language in PT_CODES else 1 if language in EN_CODES else 2
            if priority == 2:
                relative_index += 1
                continue
            choices.append((priority, relative_index, language or "und"))
        relative_index += 1
    if not choices:
        return None, "no-text-subtitle"
    _, selected, language = min(choices)
    command = _low_priority_prefix() + [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-i", str(path), "-map", f"0:s:{selected}", "-f", "srt", "-",
    ]
    return _subtitle_intervals_from_command(command), f"embedded-text-subtitle:{language}"


def _offset_scores(source: Sequence[int], target: Sequence[int], max_steps: int) -> dict[int, int]:
    scores: dict[int, int] = {}
    for offset in range(-max_steps, max_steps + 1):
        target_start = max(0, -offset)
        target_end = min(len(target), len(source) - offset)
        if target_end <= target_start:
            continue
        score = 0
        for target_index in range(target_start, target_end):
            if (target[target_index] ^ source[target_index + offset]).bit_count() <= STRONG_DISTANCE:
                score += 1
        scores[offset] = score
    return scores


def _candidate_offsets(
    source: Sequence[int], target: Sequence[int], sample_rate: int
) -> list[int]:
    scores = _offset_scores(source, target, MAX_OFFSET_SECONDS * sample_rate)
    minimum = MIN_OFFSET_SUPPORT_SECONDS * sample_rate
    peaks = []
    for offset, score in scores.items():
        if score < minimum:
            continue
        if score < scores.get(offset - 1, -1) or score < scores.get(offset + 1, -1):
            continue
        peaks.append((score, offset))
    peaks.sort(reverse=True)
    selected: list[int] = []
    for _, offset in peaks:
        if any(abs(offset - existing) <= 1 for existing in selected):
            continue
        selected.append(offset)
        if len(selected) >= MAX_CANDIDATE_OFFSETS:
            break
    return sorted(selected)


def _emission(source: Sequence[int], target_hash: int, target_index: int, offset: int) -> int:
    source_index = target_index + offset
    if source_index < 0 or source_index >= len(source):
        return 64
    return min(48, (target_hash ^ source[source_index]).bit_count())


def _viterbi_labels(
    source: Sequence[int], target: Sequence[int], offsets: Sequence[int]
) -> list[int | None]:
    states: list[int | None] = [None, *offsets]
    previous = [0 if state is None else TRANSITION_TO_GAP_COST for state in states]
    backtrace: list[bytearray] = []
    for target_index, target_hash in enumerate(target):
        current = [0] * len(states)
        choices = bytearray(len(states))
        for state_index, state in enumerate(states):
            emission = GAP_EMISSION_COST if state is None else _emission(
                source, target_hash, target_index, state
            )
            best_cost = 1 << 60
            best_previous = 0
            for previous_index, previous_state in enumerate(states):
                if state == previous_state:
                    transition = 0
                elif state is None or previous_state is None:
                    transition = TRANSITION_TO_GAP_COST
                else:
                    transition = TRANSITION_BETWEEN_OFFSETS_COST
                cost = previous[previous_index] + transition + emission
                if cost < best_cost:
                    best_cost, best_previous = cost, previous_index
            current[state_index] = best_cost
            choices[state_index] = best_previous
        baseline = min(current)
        previous = [cost - baseline for cost in current]
        backtrace.append(choices)
    state_index = min(range(len(states)), key=previous.__getitem__)
    labels: list[int | None] = [None] * len(target)
    for target_index in range(len(target) - 1, -1, -1):
        labels[target_index] = states[state_index]
        state_index = backtrace[target_index][state_index]
    return labels


def _runs(labels: Sequence[int | None]) -> list[tuple[int | None, int, int]]:
    if not labels:
        return []
    output = []
    start = 0
    state = labels[0]
    for index in range(1, len(labels)):
        next_state = labels[index]
        if next_state != state:
            output.append((state, start, index))
            start, state = index, next_state
    output.append((state, start, len(labels)))
    return output


def _clean_short_runs(labels: list[int | None], sample_rate: int) -> list[int | None]:
    minimum = MIN_SOURCE_RUN_SECONDS * sample_rate
    cleaned = list(labels)
    for state, start, end in _runs(labels):
        if state is not None and end - start < minimum:
            cleaned[start:end] = [None] * (end - start)
    return cleaned


def _bridge_short_same_offset_gaps(
    labels: list[int | None], sample_rate: int
) -> list[int | None]:
    bridged = list(labels)
    maximum = MAX_BRIDGED_VISUAL_GAP_SECONDS * sample_rate
    for state, start, end in _runs(labels):
        if state is not None or end - start > maximum or start == 0 or end == len(labels):
            continue
        before, after = labels[start - 1], labels[end]
        if before is not None and before == after:
            bridged[start:end] = [before] * (end - start)
    return bridged


def _overlaps_dialogue(
    start: float, end: float, intervals: Sequence[tuple[float, float]]
) -> bool:
    return any(cue_end > start + 0.10 and cue_start < end - 0.10 for cue_start, cue_end in intervals)


def _coalesce_segments(
    segments: Sequence[dict[str, float | str]]
) -> list[dict[str, float | str]]:
    output: list[dict[str, float | str]] = []
    for segment in segments:
        current = dict(segment)
        if (
            output
            and output[-1]["input"] == current["input"]
            and abs(float(output[-1]["end"]) - float(current["start"])) <= 0.001
        ):
            output[-1]["end"] = current["end"]
        else:
            output.append(current)
    return output


def _rejection(
    reason: str,
    *,
    sample_rate: int,
    source_samples: int,
    target_samples: int,
    offsets: Sequence[int] = (),
    subtitle_evidence: str = "not-checked",
) -> TimelinePlan:
    return TimelinePlan(
        "review-required", reason, (), sample_rate, source_samples, target_samples,
        tuple(offset / sample_rate for offset in offsets), 0.0, 0.0, None, 0.0,
        subtitle_evidence,
    )


def plan_from_fingerprints(
    source: Sequence[int],
    target: Sequence[int],
    source_duration: float,
    target_duration: float,
    *,
    dialogue_intervals: Sequence[tuple[float, float]] | None,
    subtitle_evidence: str,
    sample_rate: int = SAMPLE_RATE,
) -> TimelinePlan:
    offsets = _candidate_offsets(source, target, sample_rate)
    if not offsets:
        return _rejection(
            "nenhum offset visual recebeu suporte suficiente",
            sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
            subtitle_evidence=subtitle_evidence,
        )
    labels = _viterbi_labels(source, target, offsets)
    labels = _bridge_short_same_offset_gaps(labels, sample_rate)
    labels = _clean_short_runs(labels, sample_rate)
    raw_runs = _runs(labels)
    segments: list[dict[str, float | str]] = []
    distances: list[int] = []
    aligned_samples = 0
    previous_source_end = 0.0
    gap_seconds = 0.0
    for state, start_index, end_index in raw_runs:
        target_start = start_index / sample_rate
        target_end = min(end_index / sample_rate, target_duration)
        if target_end <= target_start:
            continue
        if state is None:
            gap_seconds += target_end - target_start
            segments.append({"input": "target", "start": target_start, "end": target_end})
            continue
        source_start = target_start + state / sample_rate
        source_end = source_start + (target_end - target_start)
        if source_start < -0.001 or source_end > source_duration + 0.150:
            return _rejection(
                "segmento alinhado sairia dos limites da fonte",
                sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
                offsets=offsets, subtitle_evidence=subtitle_evidence,
            )
        source_start, source_end = max(0.0, source_start), min(source_duration, source_end)
        aligned_start_index = start_index
        if source_start < previous_source_end:
            # Repeated eyecatch/opening frames can make both offsets visually
            # valid around a cut. Preserve the ambiguous target audio until
            # source time is monotonic again; subtitle gates below still veto
            # this gap if it contains dialogue.
            trim = previous_source_end - source_start
            if trim >= target_end - target_start:
                gap_seconds += target_end - target_start
                segments.append({"input": "target", "start": target_start, "end": target_end})
                continue
            gap_end = target_start + trim
            gap_seconds += trim
            segments.append({"input": "target", "start": target_start, "end": gap_end})
            target_start = gap_end
            source_start = previous_source_end
            aligned_start_index = min(end_index, start_index + round(trim * sample_rate))
        previous_source_end = source_end
        segments.append({"input": "source", "start": source_start, "end": source_end})
        for target_index in range(aligned_start_index, min(end_index, len(target))):
            source_index = target_index + state
            if 0 <= source_index < len(source):
                distances.append((target[target_index] ^ source[source_index]).bit_count())
                aligned_samples += 1

    if not segments:
        return _rejection(
            "alinhamento não produziu segmentos",
            sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
            offsets=offsets, subtitle_evidence=subtitle_evidence,
        )
    # fps can emit a final sample slightly beyond the authoritative duration.
    if segments[-1]["input"] == "target":
        segments[-1]["end"] = target_duration
    elif float(segments[-1]["end"]) > source_duration:
        segments[-1]["end"] = source_duration

    segments = _coalesce_segments(segments)

    target_gaps = [segment for segment in segments if segment["input"] == "target"]
    if gap_seconds > MAX_TARGET_GAP_SECONDS:
        return _rejection(
            f"lacunas no alvo somam {gap_seconds:.3f}s",
            sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
            offsets=offsets, subtitle_evidence=subtitle_evidence,
        )
    significant_gaps = [
        segment for segment in target_gaps
        if float(segment["end"]) - float(segment["start"]) > MAX_TARGET_GAP_WITHOUT_SUBTITLES
        and not (
            (
                float(segment["start"]) <= 0.001
                or float(segment["end"]) >= target_duration - 0.001
            )
            and float(segment["end"]) - float(segment["start"]) <= MAX_EDGE_GAP_SECONDS
        )
    ]
    if significant_gaps and dialogue_intervals is None:
        return _rejection(
            "há lacunas no alvo, mas nenhuma legenda textual prova ausência de diálogo",
            sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
            offsets=offsets, subtitle_evidence=subtitle_evidence,
        )
    if dialogue_intervals is not None:
        for segment in significant_gaps:
            if _overlaps_dialogue(
                float(segment["start"]), float(segment["end"]), dialogue_intervals
            ):
                return _rejection(
                    "uma lacuna da edição cruza diálogo legendado",
                    sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
                    offsets=offsets, subtitle_evidence=subtitle_evidence,
                )
    coverage = aligned_samples / max(1, min(len(target), int(target_duration * sample_rate)))
    good_ratio = sum(distance <= GOOD_DISTANCE for distance in distances) / max(1, len(distances))
    median = float(statistics.median(distances)) if distances else None
    if coverage < 0.85 or good_ratio < 0.90 or median is None or median > STRONG_DISTANCE:
        return _rejection(
            f"prova visual insuficiente: cobertura={coverage:.3f}, match={good_ratio:.3f}",
            sample_rate=sample_rate, source_samples=len(source), target_samples=len(target),
            offsets=offsets, subtitle_evidence=subtitle_evidence,
        )
    return TimelinePlan(
        "high",
        "offsets por blocos, continuidade e lacunas sem diálogo foram comprovados",
        tuple(segments), sample_rate, len(source), len(target),
        tuple(offset / sample_rate for offset in offsets), coverage, good_ratio,
        median, gap_seconds, subtitle_evidence,
    )


def plan_timeline(
    source: Path,
    target: Path,
    *,
    fingerprint_extractor: Callable[[Path, int], list[int]] = extract_fingerprints,
    dialogue_extractor: Callable[[Path], tuple[list[tuple[float, float]] | None, str]] = extract_dialogue_intervals,
) -> TimelinePlan:
    ensure_safe_load()
    source_probe, target_probe = probe(source), probe(target)
    source_duration, target_duration = duration(source_probe), duration(target_probe)
    if abs(source_duration - target_duration) > MAX_OFFSET_SECONDS:
        return _rejection(
            "diferença de duração excede o limite automático de 120 segundos",
            sample_rate=SAMPLE_RATE, source_samples=0, target_samples=0,
        )
    source_hashes = fingerprint_extractor(source, SAMPLE_RATE)
    target_hashes = fingerprint_extractor(target, SAMPLE_RATE)
    dialogue, evidence = dialogue_extractor(target)
    return plan_from_fingerprints(
        source_hashes, target_hashes, source_duration, target_duration,
        dialogue_intervals=dialogue, subtitle_evidence=evidence,
        sample_rate=SAMPLE_RATE,
    )
