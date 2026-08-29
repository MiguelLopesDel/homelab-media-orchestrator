#!/usr/bin/env python3
"""Inspect, extract, retime and verify external text subtitles.

The video files are read-only inputs.  Subtitle extraction writes an atomic
sidecar next to the target (or at an explicit path); retiming is driven by a
reviewed edit manifest so distributor bumpers and cuts are never guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


TEXT_CODECS = {"ass": ".ass", "ssa": ".ass", "subrip": ".srt", "srt": ".srt"}
LANGUAGE_ALIASES = {
    "pt": {"pt", "por", "pt-br", "pt_br", "pob", "brazilian"},
    "en": {"en", "eng", "english"},
}
LIKELY_COMPATIBLE_DELTA = 0.250


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    line: str
    start_field: int
    end_field: int


@dataclass(frozen=True)
class SubtitleSummary:
    cue_count: int
    first_cue: float | None
    last_cue: float | None


def _run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def probe_media(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"arquivo ausente: {path}")
    return _run_json([
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration,start_time:stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of", "json", str(path),
    ])


def _duration(probe: dict[str, Any]) -> float:
    return float(probe["format"]["duration"])


def _normalise_language(value: str) -> str:
    value = value.strip().lower().replace("_", "-")
    for canonical, aliases in LANGUAGE_ALIASES.items():
        if value in aliases:
            return canonical
    return value


def select_text_subtitle(probe: dict[str, Any], language: str) -> dict[str, Any]:
    wanted = _normalise_language(language)
    candidates = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        codec = stream.get("codec_name", "")
        if codec not in TEXT_CODECS:
            continue
        tags = stream.get("tags", {})
        actual = _normalise_language(tags.get("language", ""))
        title = tags.get("title", "")
        title_words = {_normalise_language(word) for word in re.split(r"[^\w-]+", title)}
        if actual == wanted or wanted in title_words:
            candidates.append(stream)
    if not candidates:
        raise ValueError(f"nenhuma legenda textual {language} encontrada")
    if len(candidates) > 1:
        raise ValueError(
            f"mais de uma legenda textual {language}; informe uma origem sem ambiguidade"
        )
    return candidates[0]


def _ass_time(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", value.strip())
    if not match:
        raise ValueError(f"timestamp ASS inválido: {value}")
    hours, minutes, seconds, centiseconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + centiseconds / 100


def _format_ass_time(value: float) -> str:
    if value < -0.0001:
        raise ValueError("timestamp ASS negativo")
    total = int(round(max(0.0, value) * 100))
    hours, rest = divmod(total, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, centiseconds = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def parse_ass(text: str) -> list[Cue]:
    in_events = False
    start_field, end_field = 1, 2
    cues: list[Cue] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
        elif in_events and stripped.lower().startswith("format:"):
            fields = [field.strip().lower() for field in stripped.split(":", 1)[1].split(",")]
            start_field, end_field = fields.index("start"), fields.index("end")
        elif in_events and line.startswith("Dialogue:"):
            fields = line.split(",", max(start_field, end_field) + 1)
            start, end = _ass_time(fields[start_field]), _ass_time(fields[end_field])
            if end <= start:
                raise ValueError(f"cue ASS sem duração: {line[:100]}")
            cues.append(Cue(start, end, line, start_field, end_field))
    if not cues:
        raise ValueError("arquivo ASS não contém falas")
    return cues


def _render_ass_cue(cue: Cue, start: float, end: float) -> str:
    fields = cue.line.split(",", max(cue.start_field, cue.end_field) + 1)
    fields[cue.start_field] = _format_ass_time(start)
    fields[cue.end_field] = _format_ass_time(end)
    return ",".join(fields)


def summarise_ass(path: Path) -> SubtitleSummary:
    cues = parse_ass(path.read_text(encoding="utf-8-sig", errors="strict"))
    return SubtitleSummary(len(cues), min(c.start for c in cues), max(c.end for c in cues))


def _atomic_write(path: Path, content: str, replace: bool = False) -> None:
    if path.exists() and not replace:
        raise FileExistsError(f"saída já existe: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def inspect_pair(source: Path, target: Path, language: str = "por") -> dict[str, Any]:
    source_probe, target_probe = probe_media(source), probe_media(target)
    stream = select_text_subtitle(source_probe, language)
    source_duration, target_duration = _duration(source_probe), _duration(target_probe)
    delta = target_duration - source_duration
    verdict = "likely-compatible" if abs(delta) <= LIKELY_COMPATIBLE_DELTA else "review-required"
    report = {
        "source": str(source),
        "target": str(target),
        "source_duration": source_duration,
        "target_duration": target_duration,
        "duration_delta": delta,
        "subtitle_stream": stream["index"],
        "subtitle_codec": stream["codec_name"],
        "subtitle_language": stream.get("tags", {}).get("language", ""),
        "verdict": verdict,
        "warning": "duração próxima não prova sincronização interna; revisar início, meio e fim",
    }
    if stream["codec_name"] in {"ass", "ssa"}:
        with tempfile.TemporaryDirectory(prefix="subtitle-inspect-") as directory:
            extracted = Path(directory) / "source.ass"
            subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(source),
                "-map", f"0:{stream['index']}", "-c", "copy", str(extracted),
            ], check=True)
            report.update(asdict(summarise_ass(extracted)))
    return report


def extract_sidecar(
    source: Path,
    target: Path,
    language: str = "por",
    output: Path | None = None,
    replace: bool = False,
) -> Path:
    report = inspect_pair(source, target, language)
    suffix = TEXT_CODECS[report["subtitle_codec"]]
    output = output or target.with_suffix(f".pt-BR{suffix}")
    if output.exists() and not replace:
        raise FileExistsError(f"saída já existe: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".partial" + output.suffix)
    temporary.unlink(missing_ok=True)
    try:
        subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(source),
            "-map", f"0:{report['subtitle_stream']}", "-c", "copy", str(temporary),
        ], check=True)
        if suffix == ".ass":
            summarise_ass(temporary)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def shift_ass(source: Path, output: Path, offset: float, replace: bool = False) -> Path:
    text = source.read_text(encoding="utf-8-sig", errors="strict")
    cues = iter(parse_ass(text))
    shifted: list[str] = []
    current = next(cues, None)
    for line in text.splitlines():
        if current is not None and line == current.line:
            start, end = current.start + offset, current.end + offset
            if end > 0:
                shifted.append(_render_ass_cue(current, max(0, start), end))
            current = next(cues, None)
        else:
            shifted.append(line)
    _atomic_write(output, "\n".join(shifted) + "\n", replace)
    return output


def _validate_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    normalised = []
    for raw in segments:
        segment = {key: float(raw[key]) for key in ("source_start", "source_end", "target_start")}
        if segment["source_start"] < 0 or segment["source_end"] <= segment["source_start"] or segment["target_start"] < 0:
            raise ValueError(f"segmento inválido: {raw}")
        normalised.append(segment)
    normalised.sort(key=lambda item: item["source_start"])
    for previous, current in zip(normalised, normalised[1:]):
        if current["source_start"] < previous["source_end"]:
            raise ValueError("segmentos de origem se sobrepõem")
    if not normalised:
        raise ValueError("manifesto sem segmentos")
    return normalised


def retime_ass(manifest_path: Path, replace: bool = False) -> Path:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 1:
        raise ValueError("versão de manifesto não suportada")
    source, output = Path(manifest["source_subtitle"]), Path(manifest["output"])
    target = Path(manifest["target_video"])
    target_duration = _duration(probe_media(target))
    segments = _validate_segments(manifest["segments"])
    text = source.read_text(encoding="utf-8-sig", errors="strict")
    cues = parse_ass(text)
    rendered: list[tuple[float, str]] = []
    for cue in cues:
        for segment in segments:
            clipped_start = max(cue.start, segment["source_start"])
            clipped_end = min(cue.end, segment["source_end"])
            if clipped_end <= clipped_start:
                continue
            start = segment["target_start"] + clipped_start - segment["source_start"]
            end = segment["target_start"] + clipped_end - segment["source_start"]
            if end > target_duration + 0.100:
                raise ValueError("timeline produz fala depois do fim do vídeo")
            rendered.append((start, _render_ass_cue(cue, start, end)))
    if not rendered:
        raise ValueError("nenhuma fala sobreviveu ao mapeamento")
    rendered.sort(key=lambda item: item[0])
    prefix = text.split("Dialogue:", 1)[0]
    _atomic_write(output, prefix + "\n".join(line for _, line in rendered) + "\n", replace)
    return output


def verify_sidecar(subtitle: Path, target: Path) -> dict[str, Any]:
    if subtitle.suffix.lower() != ".ass":
        raise ValueError("verify atualmente exige legenda ASS")
    summary = summarise_ass(subtitle)
    target_duration = _duration(probe_media(target))
    if summary.last_cue is not None and summary.last_cue > target_duration + 0.100:
        raise ValueError(
            f"última fala termina em {summary.last_cue:.3f}s; vídeo em {target_duration:.3f}s"
        )
    return {**asdict(summary), "target_duration": target_duration, "status": "structurally-valid"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="compara edições e localiza a faixa textual")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("target", type=Path)
    inspect.add_argument("--language", default="por")

    extract = commands.add_parser("extract", help="publica um sidecar sem alterar o vídeo")
    extract.add_argument("source", type=Path)
    extract.add_argument("target", type=Path)
    extract.add_argument("--language", default="por")
    extract.add_argument("--output", type=Path)
    extract.add_argument("--replace", action="store_true")

    shift = commands.add_parser("shift", help="aplica um deslocamento constante a um ASS")
    shift.add_argument("source", type=Path)
    shift.add_argument("output", type=Path)
    shift.add_argument("--offset", type=float, required=True)
    shift.add_argument("--replace", action="store_true")

    retime = commands.add_parser("retime", help="aplica cortes e deslocamentos revisados")
    retime.add_argument("manifest", type=Path)
    retime.add_argument("--replace", action="store_true")

    verify = commands.add_parser("verify", help="valida estrutura e limites da timeline")
    verify.add_argument("subtitle", type=Path)
    verify.add_argument("target", type=Path)

    args = parser.parse_args()
    if args.command == "inspect":
        print(json.dumps(inspect_pair(args.source, args.target, args.language), indent=2))
    elif args.command == "extract":
        print(extract_sidecar(args.source, args.target, args.language, args.output, args.replace))
    elif args.command == "shift":
        print(shift_ass(args.source, args.output, args.offset, args.replace))
    elif args.command == "retime":
        print(retime_ass(args.manifest, args.replace))
    else:
        print(json.dumps(verify_sidecar(args.subtitle, args.target), indent=2))


if __name__ == "__main__":
    main()
