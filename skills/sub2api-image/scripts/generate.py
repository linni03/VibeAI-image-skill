#!/usr/bin/env python3
"""Generate images with Sub2API and save validated files locally."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from image_client import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_PROGRESS_INTERVAL_SECONDS,
    Config,
    ConfigError,
    default_stream_for_profile,
    GenerationResult,
    ImageClient,
    ImageValidationError,
    max_images_per_request,
    new_client_request_id,
    RequestHeartbeat,
    SkillError,
    StreamInterruptedError,
    load_config,
    pictures_output,
    preflight_output_path,
    print_json,
    public_error,
    resolve_size,
    safe_response_metadata,
    save_response_images,
    validate_model,
    write_json_metadata,
)


OUTPUT_FORMATS = ("png", "jpeg", "webp")
BACKGROUNDS = ("auto", "opaque", "transparent")
MODERATION_LEVELS = ("auto", "low")
MAX_PROMPT_BYTES = 100_000


def validate_prompt(value: str) -> str:
    prompt = value.strip()
    if not prompt:
        raise ConfigError("Image prompt is empty")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ConfigError("Image prompt is too long")
    return prompt


def read_prompt_file(path: Path | str) -> str:
    prompt_path = Path(path).expanduser()
    try:
        info = prompt_path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"Prompt file not found: {prompt_path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"Prompt path is not a regular file: {prompt_path}")
    if info.st_size > MAX_PROMPT_BYTES:
        raise ConfigError("Prompt file is too large")
    try:
        return validate_prompt(prompt_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Prompt file is not valid UTF-8: {prompt_path}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read prompt file {prompt_path}: {exc}") from exc


def orientation_for(width: int, height: int) -> str:
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("x-request-id", "openai-request-id", "cf-ray"):
        if headers.get(name):
            return headers[name]
    return None


def validate_count(value: int, provider_profile: str) -> int:
    maximum = max_images_per_request(provider_profile)
    if not 1 <= value <= maximum:
        if maximum == 1:
            raise ConfigError(
                f"Provider profile {provider_profile} supports exactly one image per paid "
                "request; generate separate outputs as separate authorized requests"
            )
        raise ConfigError(f"Image count must be between 1 and {maximum}")
    return value


def request_config(config: Config, timeout_seconds: int | None) -> Config:
    if timeout_seconds is None:
        return config
    if not 1 <= timeout_seconds <= 3600:
        raise ConfigError("Timeout must be between 1 and 3600 seconds")
    return replace(config, timeout_seconds=timeout_seconds)


def normalize_options(
    output_format: str,
    background: str | None,
    moderation: str | None,
    output_compression: int | None,
) -> tuple[str, str | None, str | None, int | None]:
    normalized_format = output_format.lower().strip()
    if normalized_format not in OUTPUT_FORMATS:
        raise ConfigError("Output format must be png, jpeg, or webp")

    normalized_background = background.lower().strip() if background else None
    if normalized_background not in (*BACKGROUNDS, None):
        raise ConfigError("Background must be auto, opaque, or transparent")
    if normalized_background == "transparent" and normalized_format == "jpeg":
        raise ConfigError("Transparent backgrounds require png or webp output")

    normalized_moderation = moderation.lower().strip() if moderation else None
    if normalized_moderation not in (*MODERATION_LEVELS, None):
        raise ConfigError("Moderation must be auto or low")

    if output_compression is not None:
        if not 0 <= output_compression <= 100:
            raise ConfigError("Output compression must be between 0 and 100")
        if normalized_format == "png":
            raise ConfigError("Output compression is supported only for jpeg or webp")
    return (
        normalized_format,
        normalized_background,
        normalized_moderation,
        output_compression,
    )


def select_output_format(
    output_format: str | None, output_path: Path | str | None
) -> str:
    suffix_format: str | None = None
    if output_path is not None:
        suffix = Path(output_path).suffix.lower()
        suffix_format = {
            ".png": "png",
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".webp": "webp",
        }.get(suffix)
        if suffix and suffix_format is None:
            raise ConfigError(
                "--output must omit its extension or use .png, .jpg, .jpeg, or .webp"
            )
    selected = output_format.lower().strip() if output_format else suffix_format or "png"
    if suffix_format is not None and selected != suffix_format:
        raise ConfigError("--output extension conflicts with --output-format")
    return selected


def requested_shape(size: str) -> tuple[str | None, str | None]:
    if size == "auto":
        return None, None
    width, height = map(int, size.split("x"))
    return f"{width}x{height}", orientation_for(width, height)


def response_match_report(
    images: Sequence[Mapping[str, Any]],
    *,
    requested_size: str,
    requested_tier: str | None,
    requested_orientation: str | None,
    requested_count: int,
    output_format: str,
) -> tuple[bool, dict[str, bool | None], list[str]]:
    auto_size = requested_size == "auto"
    matches: dict[str, bool | None] = {
        "tier_match": None
        if auto_size
        else all(image["actual_tier"] == requested_tier for image in images),
        "orientation_match": None
        if auto_size
        else all(
            orientation_for(int(image["width"]), int(image["height"]))
            == requested_orientation
            for image in images
        ),
        "exact_size_match": None
        if auto_size
        else all(image["actual_size"] == requested_size for image in images),
        "count_match": len(images) == requested_count,
        "format_match": all(image["format"] == output_format for image in images),
    }
    mismatches = [name.removesuffix("_match") for name, value in matches.items() if value is False]
    return not mismatches, matches, mismatches


def preflight_metadata_path(path: Path | str | None, overwrite: bool) -> None:
    if path is None or str(path) == "auto":
        return
    selected = Path(path).expanduser().resolve()
    if selected.exists() and not overwrite:
        raise ImageValidationError(
            f"Metadata file already exists: {selected}; pass --overwrite to replace it"
        )
    if selected.exists() and selected.is_dir():
        raise ImageValidationError(f"Metadata path is a directory: {selected}")


def write_metadata_report(
    report: dict[str, Any],
    request: Mapping[str, Any],
    metadata: Path | str | None,
    *,
    overwrite: bool,
) -> None:
    if metadata is None:
        return
    if str(metadata) == "auto":
        images = report.get("images")
        if not isinstance(images, list) or not images:
            raise ImageValidationError("Cannot choose automatic metadata path without an image")
        destination = Path(f"{images[0]['path']}.json")
    else:
        destination = Path(metadata)
    protected_paths: set[Path] = set()
    for field in ("images", "input_images"):
        entries = report.get(field)
        if isinstance(entries, list):
            protected_paths.update(
                Path(entry["path"]).expanduser().resolve()
                for entry in entries
                if isinstance(entry, Mapping) and isinstance(entry.get("path"), str)
            )
    mask = report.get("mask")
    if isinstance(mask, Mapping) and isinstance(mask.get("path"), str):
        protected_paths.add(Path(mask["path"]).expanduser().resolve())
    if destination.expanduser().resolve() in protected_paths:
        raise ImageValidationError("Metadata path must not replace an input or output image")
    document = dict(report)
    document["request"] = dict(request)
    written = write_json_metadata(destination, document, overwrite=overwrite)
    report["metadata_path"] = str(written)


def partial_output_path(path: Path | str) -> Path:
    selected = Path(path).expanduser()
    if selected.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return selected.with_name(f"{selected.stem}-partial{selected.suffix}")
    return selected.with_name(f"{selected.name}-partial")


def save_interrupted_partials(
    client: ImageClient,
    exc: StreamInterruptedError,
    *,
    output_dir: Path | str | None,
    output_path: Path | str | None,
    configured_output_dir: Path | str,
    overwrite: bool,
    operation: str = "generate",
) -> None:
    items = exc.partial_response.get("data")
    if not isinstance(items, list) or not items:
        return
    try:
        files = save_response_images(
            client,
            exc.partial_response,
            None if output_path is not None else output_dir or configured_output_dir,
            operation=f"{operation}-partial",
            output_path=partial_output_path(output_path) if output_path is not None else None,
            overwrite=overwrite,
        )
    except SkillError as save_error:
        exc.with_partial_save_error(str(save_error))
        return
    for item in files:
        item["diagnostic"] = "partial_image"
        item["final"] = False
    exc.with_partial_files(files)


def generate_images(
    config: Config,
    *,
    prompt: str,
    tier: str = "1K",
    orientation: str = "square",
    exact_size: str | None = None,
    model: str | None = None,
    count: int = 1,
    quality: str | None = None,
    output_format: str | None = None,
    output_dir: Path | str | None = None,
    output_path: Path | str | None = None,
    overwrite: bool = False,
    background: str | None = None,
    moderation: str | None = None,
    output_compression: int | None = None,
    timeout_seconds: int | None = None,
    stream: bool | None = None,
    dry_run: bool = False,
    metadata: Path | str | None = None,
    heartbeat_interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
) -> dict[str, Any]:
    selected_count = validate_count(count, config.provider_profile)
    selected_output_format = select_output_format(output_format, output_path)
    (
        normalized_format,
        normalized_background,
        normalized_moderation,
        normalized_compression,
    ) = normalize_options(
        selected_output_format, background, moderation, output_compression
    )
    selected_model = validate_model(model or config.model)
    selected_prompt = validate_prompt(prompt)
    requested_size, requested_tier = resolve_size(
        tier,
        orientation,
        exact_size,
        provider_profile=config.provider_profile,
    )
    size_source = "exact" if exact_size is not None else "profile_preset"
    selected_stream = (
        default_stream_for_profile(config.provider_profile) if stream is None else stream
    )
    _, requested_orientation = requested_shape(requested_size)
    selected_config = request_config(config, timeout_seconds)

    if output_path is not None and output_dir is not None:
        raise ConfigError("Use either --output or --output-dir, not both")
    planned_outputs = (
        preflight_output_path(output_path, selected_count, normalized_format, overwrite)
        if output_path is not None
        else []
    )
    preflight_metadata_path(metadata, overwrite)
    if metadata is not None and str(metadata) != "auto":
        metadata_path = Path(metadata).expanduser().resolve()
        if metadata_path in planned_outputs:
            raise ConfigError("Metadata path must differ from every output image path")

    payload: dict[str, Any] = {
        "model": selected_model,
        "prompt": selected_prompt,
        "size": requested_size,
        "n": selected_count,
        "response_format": "b64_json",
        "output_format": normalized_format,
    }
    if quality:
        payload["quality"] = quality.strip()
    if normalized_background:
        payload["background"] = normalized_background
    if normalized_moderation:
        payload["moderation"] = normalized_moderation
    if normalized_compression is not None:
        payload["output_compression"] = normalized_compression
    if selected_stream:
        payload["stream"] = True
        payload["partial_images"] = 0

    base_report: dict[str, Any] = {
        "ok": True,
        "operation": "generate",
        "model": selected_model,
        "requested_size": requested_size,
        "requested_tier": requested_tier,
        "requested_orientation": requested_orientation,
        "requested_count": selected_count,
        "output_format": normalized_format,
        "timeout_seconds": selected_config.timeout_seconds,
        "provider_profile": config.provider_profile,
        "max_images_per_request": max_images_per_request(config.provider_profile),
        "size_source": size_source,
        "stream_requested": selected_stream,
    }
    if dry_run:
        base_report.update(
            {
                "dry_run": True,
                "endpoint": f"{selected_config.base_url}/images/generations",
                "request": payload,
                "network_request_sent": False,
                "files_written": False,
            }
        )
        return base_report

    started = time.monotonic()
    client = ImageClient(selected_config)
    client_request_id = new_client_request_id()
    base_report["client_request_id"] = client_request_id
    try:
        with RequestHeartbeat(
            "generate",
            selected_config.timeout_seconds,
            interval_seconds=heartbeat_interval_seconds,
            client_request_id=client_request_id,
        ):
            if selected_stream:
                generation = client.generate_stream(
                    payload,
                    client_request_id=client_request_id,
                )
            else:
                response, headers = client.generate(
                    payload,
                    client_request_id=client_request_id,
                )
                generation = GenerationResult(
                    response=response,
                    headers=headers,
                    response_mode="json",
                )
    except StreamInterruptedError as exc:
        save_interrupted_partials(
            client,
            exc,
            output_dir=output_dir,
            output_path=output_path,
            configured_output_dir=config.output_dir,
            overwrite=overwrite,
        )
        raise
    response = generation.response
    headers = generation.headers
    images = save_response_images(
        client,
        response,
        None if output_path is not None else output_dir or config.output_dir,
        operation="generate",
        output_path=output_path,
        overwrite=overwrite,
    )
    elapsed = round(time.monotonic() - started, 3)
    ok, matches, mismatches = response_match_report(
        images,
        requested_size=requested_size,
        requested_tier=requested_tier,
        requested_orientation=requested_orientation,
        requested_count=selected_count,
        output_format=normalized_format,
    )
    report = {
        **base_report,
        "ok": ok,
        **matches,
        "elapsed_seconds": elapsed,
        "response_mode": generation.response_mode,
        "images": images,
    }
    if generation.response_mode == "sse":
        report["stream"] = {
            "completion_marker_received": bool(
                generation.stream_done or generation.stream_terminal_event
            ),
            "done_marker_received": generation.stream_done,
            "terminal_event_received": generation.stream_terminal_event,
            "event_count": generation.stream_event_count,
        }
    if generation.transport_warning:
        report["transport_warning"] = dict(generation.transport_warning)
    identifier = request_id(headers)
    if identifier:
        report["request_id"] = identifier
    response_metadata = safe_response_metadata(response)
    if response_metadata:
        report["api_metadata"] = response_metadata
    if mismatches:
        report["error"] = {
            "category": "response_mismatch",
            "message": f"Returned response does not match requested: {', '.join(mismatches)}",
            "mismatches": mismatches,
        }
    write_metadata_report(report, payload, metadata, overwrite=overwrite)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate images through Sub2API")
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    parser.add_argument("--tier", choices=("1K", "2K", "4K"), default="1K")
    parser.add_argument(
        "--orientation",
        choices=("square", "landscape", "portrait"),
        default="square",
    )
    parser.add_argument("--size", help="auto or exact WIDTHxHEIGHT; overrides tier/orientation")
    parser.add_argument("--model")
    parser.add_argument(
        "--n",
        type=int,
        default=1,
        help="Images in this paid request; the OAuth profile permits only 1",
    )
    parser.add_argument("--quality")
    parser.add_argument("--background", choices=BACKGROUNDS)
    parser.add_argument("--moderation", choices=MODERATION_LEVELS)
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS)
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output", type=Path, help="Exact output filename or multi-image basename")
    outputs.add_argument("--output-dir", type=Path)
    outputs.add_argument(
        "--pictures",
        nargs="?",
        const="",
        metavar="FILENAME",
        help="Save in the OS Pictures folder, optionally with an exact filename",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--metadata",
        nargs="?",
        const="auto",
        metavar="PATH",
        help="Write safe JSON metadata; omit PATH for an automatic sidecar",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Per-request socket timeout in seconds (skill workflow: 600)",
    )
    streaming = parser.add_mutually_exclusive_group()
    streaming.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        default=None,
        help="Explicitly request SSE keepalives and completion events",
    )
    streaming.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="Explicitly request a single JSON response",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate without network or file writes")
    parser.add_argument(
        "--config",
        type=Path,
        help=f"Configuration path (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        prompt = args.prompt if args.prompt is not None else read_prompt_file(args.prompt_file)
        pictures_dir, pictures_path = (
            pictures_output(args.pictures)
            if args.pictures is not None
            else (None, None)
        )
        report = generate_images(
            config,
            prompt=prompt,
            tier=args.tier,
            orientation=args.orientation,
            exact_size=args.size,
            model=args.model,
            count=args.n,
            quality=args.quality,
            output_format=args.output_format,
            output_dir=pictures_dir or args.output_dir,
            output_path=pictures_path or args.output,
            overwrite=args.overwrite,
            background=args.background,
            moderation=args.moderation,
            output_compression=args.output_compression,
            timeout_seconds=args.timeout,
            stream=args.stream,
            dry_run=args.dry_run,
            metadata=args.metadata,
        )
        print_json(report)
        return 0 if report["ok"] else 3
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
