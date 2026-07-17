#!/usr/bin/env python3
"""Edit a local image with Sub2API and save validated output files."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from generate import OUTPUT_FORMATS, orientation_for, request_id, validate_prompt
from image_client import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    ImageClient,
    inspect_image,
    load_config,
    print_json,
    public_error,
    read_upload,
    resolve_size,
    safe_response_metadata,
    save_response_images,
    validate_model,
)


def edit_image(
    config: Config,
    *,
    image_path: Path | str,
    prompt: str,
    mask_path: Path | str | None = None,
    tier: str = "1K",
    orientation: str = "square",
    exact_size: str | None = None,
    model: str | None = None,
    count: int = 1,
    quality: str | None = None,
    input_fidelity: str | None = None,
    output_format: str = "png",
    output_dir: Path | str | None = None,
) -> dict[str, Any]:
    if not 1 <= count <= 10:
        raise ConfigError("Image count must be between 1 and 10")
    normalized_format = output_format.lower().strip()
    if normalized_format not in OUTPUT_FORMATS:
        raise ConfigError("Output format must be png, jpeg, or webp")
    selected_model = validate_model(model or config.model)
    selected_prompt = validate_prompt(prompt)
    requested_size, requested_tier = resolve_size(tier, orientation, exact_size)
    requested_width, requested_height = map(int, requested_size.split("x"))
    requested_orientation = orientation_for(requested_width, requested_height)

    source_path, source_data, _ = read_upload(image_path)
    source_info = inspect_image(source_data)
    files: list[tuple[str, Path]] = [("image", source_path)]
    mask_info: dict[str, Any] | None = None
    if mask_path is not None:
        selected_mask, mask_data, _ = read_upload(mask_path)
        parsed_mask = inspect_image(mask_data)
        if (parsed_mask.width, parsed_mask.height) != (source_info.width, source_info.height):
            raise ConfigError("Mask dimensions must exactly match the input image")
        files.append(("mask", selected_mask))
        mask_info = {
            "path": str(selected_mask.resolve()),
            "width": parsed_mask.width,
            "height": parsed_mask.height,
            "format": parsed_mask.format,
        }

    fields = {
        "model": selected_model,
        "prompt": selected_prompt,
        "size": requested_size,
        "n": str(count),
        "response_format": "b64_json",
        "output_format": normalized_format,
    }
    if quality:
        fields["quality"] = quality.strip()
    if input_fidelity:
        fields["input_fidelity"] = input_fidelity.strip()

    started = time.monotonic()
    client = ImageClient(config)
    response, headers = client.edit(fields, files)
    images = save_response_images(
        client, response, output_dir or config.output_dir, operation="edit"
    )
    elapsed = round(time.monotonic() - started, 3)
    tier_match = all(item["actual_tier"] == requested_tier for item in images)
    orientation_match = all(
        orientation_for(item["width"], item["height"]) == requested_orientation
        for item in images
    )
    exact_size_match = all(item["actual_size"] == requested_size for item in images)
    ok = tier_match and orientation_match and (not exact_size or exact_size_match)

    report: dict[str, Any] = {
        "ok": ok,
        "operation": "edit",
        "model": selected_model,
        "input_image": {
            "path": str(source_path.resolve()),
            "width": source_info.width,
            "height": source_info.height,
            "format": source_info.format,
        },
        "requested_size": requested_size,
        "requested_tier": requested_tier,
        "requested_orientation": requested_orientation,
        "requested_count": count,
        "output_format": normalized_format,
        "tier_match": tier_match,
        "orientation_match": orientation_match,
        "exact_size_match": exact_size_match,
        "elapsed_seconds": elapsed,
        "images": images,
    }
    if mask_info:
        report["mask"] = mask_info
    identifier = request_id(headers)
    if identifier:
        report["request_id"] = identifier
    metadata = safe_response_metadata(response)
    if metadata:
        report["api_metadata"] = metadata
    if not ok:
        report["error"] = {
            "category": "resolution_mismatch",
            "message": "Returned image dimensions do not match the requested resolution or orientation",
        }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Edit an image through Sub2API")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--mask", type=Path)
    parser.add_argument("--tier", choices=("1K", "2K", "4K"), default="1K")
    parser.add_argument(
        "--orientation",
        choices=("square", "landscape", "portrait"),
        default="square",
    )
    parser.add_argument("--size", help="Exact WIDTHxHEIGHT; overrides tier/orientation")
    parser.add_argument("--model")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality")
    parser.add_argument("--input-fidelity")
    parser.add_argument("--output-format", choices=OUTPUT_FORMATS, default="png")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        report = edit_image(
            config,
            image_path=args.image,
            prompt=args.prompt,
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
        )
        print_json(report)
        return 0 if report["ok"] else 3
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
