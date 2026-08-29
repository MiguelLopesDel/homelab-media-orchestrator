#!/usr/bin/env python3
"""Conservative external PT-BR audio planning and publication.

This module proves that a dubbed source and the library target use the same
video timeline before delegating atomic sidecar rendering to
``external_audio_builder``.  It never modifies or remuxes the target video.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from external_audio_builder import (
    audio_streams,
    audio_streams,
    duration,
    probe,
    render_episode,
    verify_episode,
)
from timeline_alignment import TimelinePlan, plan_timeline


FRAME_WIDTH = 9
FRAME_HEIGHT = 8
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
FINGERPRINT_SAMPLE_COUNT = 16
MAX_DURATION_DELTA = 0.150
MAX_MEDIAN_DISTANCE = 8.0
MAX_FRAME_DISTANCE = 12
MIN_MATCH_RATIO = 0.90
PT_CODES = {"pt", "por", "pob", "pb", "ptbr", "pt-br", "pt_br"}
MIN_DIRECT_IMPORT_HEIGHT = 720


@dataclass(frozen=True)
class AlignmentReport:
    source: str
    target: str
    source_duration: float
    target_duration: float
    duration_delta: float
    source_audio_index: int | None
    source_audio_evidence: str | None
    fingerprint_samples: int
    fingerprint_match_ratio: float
    fingerprint_median_distance: float | None
    confidence: str
    reason: str
    alignment_method: str = "same-timeline"
    timeline_segments: tuple[dict[str, float | str], ...] = ()


def _is_portuguese(value: str) -> bool:
    normalized = value.strip().casefold().replace("_", "-")
    return normalized in PT_CODES or "portugu" in normalized or "brazil" in normalized


def portuguese_audio_index(probe_data: dict[str, Any]) -> int | None:
    matches = []
    for relative_index, stream in enumerate(audio_streams(probe_data)):
        tags = stream.get("tags", {})
        labels = [tags.get("language", ""), tags.get("title", "")]
        if any(_is_portuguese(label) for label in labels):
            matches.append(relative_index)
    return matches[0] if len(matches) == 1 else None


def video_height(probe_data: dict[str, Any]) -> int:
    """Return the first video stream's coded height, or zero when unknown."""
    for stream in probe_data.get("streams", []):
        if stream.get("codec_type") == "video":
            try:
                return int(stream.get("height") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def can_direct_import(source_probe: dict[str, Any]) -> bool:
    """A verified dubbed source is library-quality at 720p or above.

    Audio sidecars are a preservation fallback: they are reserved for a dubbed
    source obtained outside a torrent, or for a source below this threshold.
    """
    return video_height(source_probe) >= MIN_DIRECT_IMPORT_HEIGHT


def can_direct_import_dub(
    source_probe: dict[str, Any], *, trusted_single_audio_portuguese: bool = False,
) -> bool:
    """Whether the complete dubbed video can replace library media as-is.

    Replacing the whole MKV has no cross-file audio-sync problem.  Unlike the
    sidecar path, it deliberately does not compare video fingerprints with the
    old release; episode mapping, PT-BR track verification and source quality
    are the relevant invariants.
    """
    if not can_direct_import(source_probe):
        return False
    if portuguese_audio_index(source_probe) is not None:
        return True
    return trusted_single_audio_portuguese and len(audio_streams(source_probe)) == 1


def replace_library_with_hardlink(source: Path, target: Path) -> None:
    """Atomically point the library filename at a completed torrent member.

    ``source`` is never moved, remuxed or altered, so qBittorrent can keep
    seeding it.  The former library hardlink is merely unlinked after the new
    one is durable.  The source and target must share a filesystem; silently
    copying would defeat the seeding/storage invariant and is prohibited.
    """
    temporary = target.with_name(f".{target.name}.dub-import")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    os.link(source, temporary)
    os.replace(temporary, target)


def dhash(frame: bytes) -> int:
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"frame possui {len(frame)} bytes; esperado {FRAME_BYTES}")
    output = 0
    bit = 0
    for row in range(FRAME_HEIGHT):
        start = row * FRAME_WIDTH
        for column in range(FRAME_WIDTH - 1):
            if frame[start + column] > frame[start + column + 1]:
                output |= 1 << bit
            bit += 1
    return output


def fingerprint_video(path: Path, sample_count: int = FINGERPRINT_SAMPLE_COUNT) -> list[int]:
    """Hash point samples without decoding the complete episode twice.

    Input seeking lands before the requested timestamp and ffmpeg decodes forward
    to the exact frame.  Samples span the useful timeline, so an inserted or
    removed opening/cut makes all later hashes diverge.
    """
    if sample_count < 5:
        raise ValueError("são necessárias ao menos cinco amostras")
    media_duration = duration(probe(path))
    if media_duration <= 2:
        raise ValueError("vídeo curto demais para fingerprint")
    margin = min(30.0, media_duration * 0.05)
    span = media_duration - 2 * margin
    positions = [
        margin + span * index / (sample_count - 1)
        for index in range(sample_count)
    ]
    hashes = []
    for position in positions:
        command = [
            "ionice", "-c3", "nice", "-n", "19", "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{position:.6f}", "-i", str(path),
            "-frames:v", "1", "-an", "-sn",
            "-vf", f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ]
        raw = subprocess.run(command, check=True, capture_output=True).stdout
        if len(raw) != FRAME_BYTES:
            raise RuntimeError("ffmpeg não retornou exatamente um frame de fingerprint")
        hashes.append(dhash(raw))
    return hashes


def compare_fingerprints(source: list[int], target: list[int]) -> tuple[int, float, float | None]:
    samples = min(len(source), len(target))
    if not samples:
        return 0, 0.0, None
    distances = [(source[index] ^ target[index]).bit_count() for index in range(samples)]
    ratio = sum(distance <= MAX_FRAME_DISTANCE for distance in distances) / samples
    return samples, ratio, float(statistics.median(distances))


def analyse_episode(
    source: Path,
    target: Path,
    *,
    probe_media: Callable[[Path], dict[str, Any]] = probe,
    fingerprints: Callable[[Path], list[int]] = fingerprint_video,
    timeline_planner: Callable[[Path, Path], TimelinePlan] = plan_timeline,
    trusted_single_audio_portuguese: bool = False,
) -> AlignmentReport:
    source_probe, target_probe = probe_media(source), probe_media(target)
    source_duration, target_duration = duration(source_probe), duration(target_probe)
    delta = target_duration - source_duration
    audio_index = portuguese_audio_index(source_probe)
    audio_evidence = "stream-metadata" if audio_index is not None else None
    if (
        audio_index is None
        and trusted_single_audio_portuguese
        and len(audio_streams(source_probe)) == 1
    ):
        audio_index = 0
        audio_evidence = "trusted-dub-release-with-single-audio"
    if audio_index is None:
        return AlignmentReport(
            str(source), str(target), source_duration, target_duration, delta,
            None, None, 0, 0.0, None, "blocked",
            "a fonte não contém exatamente uma faixa de áudio PT-BR identificável",
        )
    if abs(delta) > MAX_DURATION_DELTA:
        plan = timeline_planner(source, target)
        return AlignmentReport(
            str(source), str(target), source_duration, target_duration, delta,
            audio_index, audio_evidence, min(plan.source_samples, plan.target_samples),
            plan.fingerprint_match_ratio, plan.fingerprint_median_distance,
            plan.confidence, plan.reason, "piecewise-visual", plan.segments,
        )
    source_fingerprints, target_fingerprints = fingerprints(source), fingerprints(target)
    samples, ratio, median = compare_fingerprints(source_fingerprints, target_fingerprints)
    minimum_samples = max(5, int(FINGERPRINT_SAMPLE_COUNT * 0.75))
    if samples < minimum_samples:
        confidence, reason = "review-required", "amostras de vídeo insuficientes"
    elif ratio < MIN_MATCH_RATIO or median is None or median > MAX_MEDIAN_DISTANCE:
        confidence, reason = (
            "review-required",
            "as edições não provaram a mesma timeline pelo fingerprint de vídeo",
        )
    else:
        confidence, reason = (
            "high",
            "duração e fingerprints confirmam a mesma timeline; publicação automática permitida",
        )
    return AlignmentReport(
        str(source), str(target), source_duration, target_duration, delta,
        audio_index, audio_evidence, samples, ratio, median, confidence, reason,
    )


def episode_spec(report: AlignmentReport, output: Path | None = None) -> dict[str, Any]:
    if report.confidence != "high" or report.source_audio_index is None:
        raise ValueError(f"alinhamento não permite publicação: {report.reason}")
    target = Path(report.target)
    output = output or target.with_suffix(".por.default.m4a")
    source_end = min(report.source_duration, report.target_duration)
    segments = list(report.timeline_segments) or [
        {"input": "source", "start": 0.0, "end": source_end}
    ]
    spec = {
        "episode": 0,
        "source": report.source,
        "target": report.target,
        "target_size": target.stat().st_size,
        "target_duration": report.target_duration,
        "output": str(output),
        "source_audio_index": report.source_audio_index,
        "target_audio_index": 0,
        "segments": segments,
    }
    if report.source_audio_evidence == "stream-metadata":
        spec["require_source_language"] = "por"
    return spec


def publish_episode(
    source: Path,
    target: Path,
    *,
    output: Path | None = None,
    replace: bool = False,
    trusted_single_audio_portuguese: bool = False,
) -> tuple[AlignmentReport, Path]:
    report = analyse_episode(
        source, target,
        trusted_single_audio_portuguese=trusted_single_audio_portuguese,
    )
    spec = episode_spec(report, output)
    render_episode(spec, replace)
    verify_episode(spec)
    return report, Path(spec["output"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analyse = commands.add_parser("analyse")
    analyse.add_argument("source", type=Path)
    analyse.add_argument("target", type=Path)
    analyse.add_argument("--trust-single-audio-portuguese", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("source", type=Path)
    publish.add_argument("target", type=Path)
    publish.add_argument("--output", type=Path)
    publish.add_argument("--replace", action="store_true")
    publish.add_argument("--trust-single-audio-portuguese", action="store_true")
    args = parser.parse_args()
    if args.command == "analyse":
        print(json.dumps(asdict(analyse_episode(
            args.source, args.target,
            trusted_single_audio_portuguese=args.trust_single_audio_portuguese,
        )), indent=2))
    else:
        report, output = publish_episode(
            args.source, args.target, output=args.output, replace=args.replace,
            trusted_single_audio_portuguese=args.trust_single_audio_portuguese,
        )
        print(json.dumps({"report": asdict(report), "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
