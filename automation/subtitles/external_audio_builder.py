#!/usr/bin/env python3
"""Build Jellyfin external audio tracks from an explicit edit manifest.

The video is never rewritten. Each manifest segment selects a time range from
either the dubbed source or the target video's original audio. This handles
different distributor bumpers and eyecatches without breaking torrent hashes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


MAX_DURATION_ERROR = 0.150


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,channels:stream_tags=language",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def duration(probe_data: dict[str, Any]) -> float:
    return float(probe_data["format"]["duration"])


def audio_streams(probe_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stream for stream in probe_data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]


def selected_audio_stream(
    probe_data: dict[str, Any], relative_index: int
) -> dict[str, Any]:
    streams = audio_streams(probe_data)
    if relative_index < 0 or relative_index >= len(streams):
        raise ValueError(f"índice de áudio inválido: {relative_index}")
    return streams[relative_index]


def validate_episode(
    episode: dict[str, Any]
) -> tuple[Path, Path, Path, float, int, int]:
    source = Path(episode["source"])
    target = Path(episode["target"])
    output = Path(episode["output"])
    for path in (source, target):
        if not path.is_file():
            raise ValueError(f"input ausente: {path}")

    source_probe = probe(source)
    target_probe = probe(target)
    if not audio_streams(source_probe) or not audio_streams(target_probe):
        raise ValueError("source e target precisam conter áudio")
    source_audio_index = int(episode.get("source_audio_index", 0))
    target_audio_index = int(episode.get("target_audio_index", 0))
    source_audio = selected_audio_stream(source_probe, source_audio_index)
    selected_audio_stream(target_probe, target_audio_index)
    required_language = episode.get("require_source_language")
    if required_language:
        tags = source_audio.get("tags", {})
        actual_language = tags.get("language", "")
        labels = f"{actual_language} {tags.get('title', '')}".casefold()
        if not any(marker in labels for marker in (
            str(required_language).casefold(), "pt-br", "portugu", "brazil"
        )):
            raise ValueError(
                f"áudio fonte não está marcado como {required_language}: "
                f"{actual_language or 'und'}"
            )

    # Some MKVs carry subtitle streams that extend well beyond the last audio
    # and video packet (some Blu-ray editions are substantially longer at
    # container level). The reviewed media timeline therefore lives in the manifest.
    target_duration = float(episode["target_duration"])
    rendered_duration = 0.0
    limits = {"source": duration(source_probe), "target": target_duration}
    for segment in episode["segments"]:
        origin = segment["input"]
        start = float(segment["start"])
        end = float(segment["end"])
        if origin not in limits or start < 0 or end <= start or end > limits[origin] + 0.100:
            raise ValueError(f"segmento inválido: {segment}")
        rendered_duration += end - start
    if abs(rendered_duration - target_duration) > MAX_DURATION_ERROR:
        raise ValueError(
            f"timeline soma {rendered_duration:.3f}s; target possui {target_duration:.3f}s"
        )
    return (
        source, target, output, target_duration,
        source_audio_index, target_audio_index,
    )


def render_episode(episode: dict[str, Any], replace: bool) -> None:
    (
        source, target, output, target_duration,
        source_audio_index, target_audio_index,
    ) = validate_episode(episode)
    if output.exists() and not replace:
        raise FileExistsError(f"saída já existe: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".partial" + output.suffix)
    if temporary.exists():
        temporary.unlink()

    labels: list[str] = []
    filters: list[str] = []
    for index, segment in enumerate(episode["segments"]):
        input_index = 0 if segment["input"] == "source" else 1
        audio_index = (
            source_audio_index if segment["input"] == "source"
            else target_audio_index
        )
        label = f"a{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{input_index}:a:{audio_index}]atrim=start={float(segment['start']):.6f}:"
            f"end={float(segment['end']):.6f},asetpts=PTS-STARTPTS[{label}]"
        )
    filters.append(
        "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    )

    command = [
        "ionice", "-c3", "nice", "-n", "19", "ffmpeg",
        "-hide_banner", "-loglevel", "error", "-threads", "1",
        "-i", str(source), "-i", str(target),
        "-filter_complex", ";".join(filters), "-map", "[out]",
        "-c:a", "aac", "-b:a", "192k",
        "-metadata:s:a:0", "language=por",
        "-metadata:s:a:0", "title=Português (Brasil) — dublagem externa sincronizada",
        "-movflags", "+faststart", "-y", str(temporary),
    ]
    try:
        subprocess.run(command, check=True)
        result = probe(temporary)
        result_duration = duration(result)
        streams = audio_streams(result)
        language = streams[0].get("tags", {}).get("language") if streams else None
        if not streams or language != "por":
            raise RuntimeError("saída não contém uma faixa de áudio marcada como por")
        if abs(result_duration - target_duration) > MAX_DURATION_ERROR:
            raise RuntimeError(
                f"saída possui {result_duration:.3f}s; esperado {target_duration:.3f}s"
            )
        os.replace(temporary, output)
        print(f"OK {output} duration={result_duration:.3f}s")
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def verify_episode(episode: dict[str, Any]) -> None:
    _, target, output, target_duration, _, _ = validate_episode(episode)
    if not output.is_file():
        raise ValueError(f"sidecar ausente: {output}")
    result = probe(output)
    streams = audio_streams(result)
    if not streams or streams[0].get("tags", {}).get("language") != "por":
        raise ValueError(f"sidecar sem idioma por: {output}")
    output_duration = duration(result)
    if abs(output_duration - target_duration) > MAX_DURATION_ERROR:
        raise ValueError(
            f"duração divergente: {output_duration:.3f}s vs {target_duration:.3f}s"
        )
    if target.stat().st_size != int(episode["target_size"]):
        raise ValueError(f"vídeo mudou de tamanho: {target}")
    print(f"OK {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("render", "verify"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--episode", type=int)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    episodes = manifest["episodes"]
    if args.episode is not None:
        episodes = [episode for episode in episodes if int(episode["episode"]) == args.episode]
        if not episodes:
            raise SystemExit(f"episódio {args.episode} não consta no manifesto")
    for episode in episodes:
        if args.command == "render":
            render_episode(episode, args.replace)
        else:
            verify_episode(episode)


if __name__ == "__main__":
    main()
