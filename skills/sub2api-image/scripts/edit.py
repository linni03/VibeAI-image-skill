#!/usr/bin/env python3
"""Edit local images with Sub2API and save validated output files."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from generate import (
    BACKGROUNDS,
    MODERATION_LEVELS,
    OUTPUT_FORMATS,
    normalize_options,
    preflight_metadata_path,
    read_prompt_file,
    request_config,
    request_id,
    requested_shape,
    response_match_report,
    select_output_format,
    validate_count,
    validate_prompt,
    write_metadata_report,
)
from image_client import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    ImageClient,
    inspect_image,
    load_config,
    preflight_output_path,
    print_json,
    public_error,
    read_upload,
    resolve_size,
    safe_response_metadata,
    save_response_images,
    validate_model,
)


def _image_paths(value: Sequence[Path | str] | Path | str) -> list[Path | str]:
    if isinstance(value, (Path, str)):
        paths = [value]
    else:
        paths = list(value)
    if not paths:
        raise ConfigError("At least one input image is required")
    if len(paths) > 16:
        raise ConfigError("At most 16 input images are supported")
    return paths


def edit_image(
    config: Config,
    *,
    image_path: Sequence[Path | str] | Path | str,
    prompt: str,
    mask_path: Path | str | None = None,
    tier: str = "1K",
    orientation: str = "square",
    exact_size: str | None = None,
    model: str | None = None,
    count: int = 1,
    quality: str | None = None,
    input_fidelity: str | None = None,
    output_format: str | None = None,
    output_dir: Path | str | None = None,
    output_path: Path | str | None = None,
    overwrite: bool = False,
    background: str | None = None,
    moderation: str | None = None,
    output_compression: int | None = None,
    timeout_seconds: int | None = None,
    dry_run: bool = False,
    metadata: Path | str | None = None,
) -> dict[str, Any]:
    selected_count = validate_count(count)
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
    requested_size, requested_tier = resolve_size(tier, orientation, exact_size)
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

    source_reports: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    for source in _image_paths(image_path):
        source_path, source_data, _ = read_upload(source)
        source_info = inspect_image(source_data)
        files.append(("image", source_path))
        source_reports.append(
            {
                "path": str(source_path.resolve()),
                "width": source_info.width,
                "height": source_info.height,
                "format": source_info.format,
            }
        )

    mask_info: dict[str, Any] | None = None
    if mask_path is not None:
        selected_mask, mask_data, _ = read_upload(mask_path)
        parsed_mask = inspect_image(mask_data)
        first_source = source_reports[0]
        if (parsed_mask.width, parsed_mask.height) != (
            first_source["width"],
            first_source["height"],
        ):
            raise ConfigError("Mask dimensions must exactly match the first input image")
        files.append(("mask", selected_mask))
        mask_info = {
            "path": str(selected_mask.resolve()),
            "width": parsed_mask.width,
            "height": parsed_mask.height,
            "format": parsed_mask.format,
        }

    protected_inputs = {
        Path(item["path"]).expanduser().resolve() for item in source_reports
    }
    if mask_info:
        protected_inputs.add(Path(mask_info["path"]).expanduser().resolve())
    if any(path in protected_inputs for path in planned_outputs):
        raise ConfigError("Output path must not replace an input image or mask")
    if metadata is not None and str(metadata) != "auto":
        metadata_path = Path(metadata).expanduser().resolve()
        if metadata_path in protected_inputs or metadata_path in planned_outputs:
            raise ConfigError("Metadata path must not replace an input or output image")

    fields = {
        "model": selected_model,
        "prompt": selected_prompt,
        "size": requested_size,
        "n": str(selected_count),
        "response_format": "b64_json",
        "output_format": normalized_format,
    }
    if quality:
        fields["quality"] = quality.strip()
    if input_fidelity:
        fields["input_fidelity"] = input_fidelity.strip()
    if normalized_background:
        fields["background"] = normalized_background
    if normalized_moderation:
        fields["moderation"] = normalized_moderation
    if normalized_compression is not None:
        fields["output_compression"] = str(normalized_compression)

    base_report: dict[str, Any] = {
        "ok": True,
        "operation": "edit",
        "model": selected_model,
        "input_images": source_reports,
        "input_image": source_reports[0],
        "requested_size": requested_size,
        "requested_tier": requested_tier,
        "requested_orientation": requested_orientation,
        "requested_count": selected_count,
        "output_format": normalized_format,
        "timeout_seconds": selected_config.timeout_seconds,
    }
    if mask_info:
        base_report["mask"] = mask_info

    safe_request: dict[str, Any] = {
        **fields,
        "images": [item["path"] for item in source_reports],
    }
    if mask_info:
        safe_request["mask"] = mask_info["path"]
    if dry_run:
        base_report.update(
            {
                "dry_run": True,
                "endpoint": f"{selected_config.base_url}/images/edits",
                "request": safe_request,
                "network_request_sent": False,
                "files_written": False,
            }
        )
        return base_report

    started = time.monotonic()
    client = ImageClient(selected_config)
    response, headers = client.edit(fields, files)
    images = save_response_images(
        client,
        response,
        None if output_path is not None else output_dir or config.output_dir,
        operation="edit",
        output_path=output_path,
        overwrite=overwrite,
        protected_paths=protected_inputs,
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
        "images": images,
    }
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
    write_metadata_report(report, safe_request, metadata, overwrite=overwrite)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit images through Sub2API")
    parser.add_argument("--image", type=Path, action="append", required=True)
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    parser.add_argument("--mask", type=Path)
    parser.add_argument("--tier", choices=("1K", "2K", "4K"), default="1K")
    parser.add_argument(
        "--orientation",
        choices=("square", "landscape", "portrait"),
        default="square",
    )
    parser.add_argument("--size", help="auto or exact WIDTHxHEIGHT; overrides tier/orientation")
    parser.add_argument("--model")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality")
    parser.add_argument("--input-fidelity")
    parser.add_argument("--background", choices=BACKGROUNDS)
    parser.add_argument("--moderation", choices=MODERATION_LEVELS)
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS)
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output", type=Path, help="Exact output filename or multi-image basename")
    outputs.add_argument("--output-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--metadata",
        nargs="?",
        const="auto",
        metavar="PATH",
        help="Write safe JSON metadata; omit PATH for an automatic sidecar",
    )
    parser.add_argument("--timeout", type=int, help="Per-request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Validate without network or file writes")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        prompt = args.prompt if args.prompt is not None else read_prompt_file(args.prompt_file)
        report = edit_image(
            config,
            image_path=args.image,
            prompt=prompt,
            mask_path=args.mask,
            tier=args.tier,
            orientation=args.orientation,
            exact_size=args.size,
            model=args.model,
            count=args.n,
            quality=args.quality,
            input_fidelity=args.input_fidelity,
            output_format=args.output_format,
            output_dir=args.output_dir,
            output_path=args.output,
            overwrite=args.overwrite,
            background=args.background,
            moderation=args.moderation,
            output_compression=args.output_compression,
            timeout_seconds=args.timeout,
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
