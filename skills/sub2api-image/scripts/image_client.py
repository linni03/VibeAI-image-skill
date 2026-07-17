#!/usr/bin/env python3
"""Shared Sub2API Images API client and image validation helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import socket
import stat
import struct
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://vibeai.tech/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_OUTPUT_DIR = "generated_images"
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("SUB2API_IMAGE_CONFIG", "~/.config/sub2api-image/config.json")
).expanduser()
MAX_CONFIG_BYTES = 64 * 1024
MAX_ERROR_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
USER_AGENT = "sub2api-image-skill/1.0"

SIZE_PRESETS = {
    "1K": {
        "square": "1024x1024",
        "landscape": "1024x576",
        "portrait": "576x1024",
    },
    "2K": {
        "square": "2048x2048",
        "landscape": "2048x1152",
        "portrait": "1152x2048",
    },
    "4K": {
        "square": "3840x3840",
        "landscape": "3840x2160",
        "portrait": "2160x3840",
    },
}


class SkillError(Exception):
    """Base exception safe to present to a user."""

    category = "skill_error"

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"category": self.category, "message": str(self)}}


class ConfigError(SkillError):
    category = "configuration"


class ImageValidationError(SkillError):
    category = "image_validation"


class APIError(SkillError):
    category = "api_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        retry_after: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.retry_after = retry_after
        self.request_id = request_id

    def as_dict(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "category": classify_http_error(self.status),
            "message": str(self),
        }
        if self.status is not None:
            details["status"] = self.status
        if self.error_type:
            details["type"] = self.error_type
        if self.retry_after:
            details["retry_after"] = self.retry_after
        if self.request_id:
            details["request_id"] = self.request_id
        return {"ok": False, "error": details}


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    output_dir: str = DEFAULT_OUTPUT_DIR
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def public_dict(self, path: Path | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": True,
            "base_url": self.base_url,
            "api_key": redact_key(self.api_key),
            "model": self.model,
            "output_dir": self.output_dir,
            "timeout_seconds": self.timeout_seconds,
        }
        if path is not None:
            result["config_path"] = str(path.resolve())
        return result


@dataclass(frozen=True)
class ImageInfo:
    format: str
    mime_type: str
    extension: str
    width: int
    height: int

    @property
    def tier(self) -> str:
        return classify_dimensions(self.width, self.height)


def redact_key(value: str) -> str:
    if not value:
        return "<unset>"
    return "<configured>"


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    return redacted[:2000]


def normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ConfigError("Sub2API base URL is empty")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("Sub2API base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ConfigError("Sub2API base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("Sub2API base URL must not contain a query or fragment")

    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif path.lower().endswith("/v1/v1"):
        path = path[:-3]
    elif not path.lower().endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ConfigError("Sub2API API key is empty")
    if len(key) > 4096 or any(character.isspace() for character in key):
        raise ConfigError("Sub2API API key contains invalid whitespace or is too long")
    return key


def validate_model(value: str) -> str:
    model = value.strip()
    if not model or len(model) > 256 or any(ord(character) < 32 for character in model):
        raise ConfigError("Image model is empty or invalid")
    return model


def _positive_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return result


def config_from_mapping(data: Mapping[str, Any]) -> Config:
    if not isinstance(data, Mapping):
        raise ConfigError("Configuration must be a JSON object")
    return Config(
        base_url=normalize_base_url(str(data.get("base_url", DEFAULT_BASE_URL))),
        api_key=validate_api_key(str(data.get("api_key", ""))),
        model=validate_model(str(data.get("model", DEFAULT_MODEL))),
        output_dir=str(data.get("output_dir", DEFAULT_OUTPUT_DIR)).strip()
        or DEFAULT_OUTPUT_DIR,
        timeout_seconds=_positive_int(
            data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            "timeout_seconds",
            1,
            3600,
        ),
    )


def load_config(path: Path | str | None = None) -> Config:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    try:
        info = config_path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration not found at {config_path}; run configure.py first"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"Configuration path is not a regular file: {config_path}")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError(
            f"Configuration permissions are too broad at {config_path}; run chmod 600"
        )
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration at {config_path}: {exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("Configuration file is unexpectedly large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Configuration is not valid UTF-8 JSON: {config_path}") from exc
    return config_from_mapping(data)


def save_config(config: Config, path: Path | str | None = None) -> Path:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    parent = config_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            parent.chmod(0o700)
        except OSError as exc:
            raise ConfigError(f"Cannot secure configuration directory {parent}: {exc}") from exc

    payload = json.dumps(
        {
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "output_dir": config.output_dir,
            "timeout_seconds": config.timeout_seconds,
        },
        indent=2,
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", dir=parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
        config_path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"Cannot write configuration at {config_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return config_path


def classify_dimensions(width: int, height: int) -> str:
    maximum = max(width, height)
    if maximum <= 1024:
        return "1K"
    if maximum <= 2048:
        return "2K"
    return "4K"


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        raise ConfigError("Size must use WIDTHxHEIGHT, for example 1024x1024")
    width, height = int(match.group(1)), int(match.group(2))
    if not (64 <= width <= 16384 and 64 <= height <= 16384):
        raise ConfigError("Image width and height must each be between 64 and 16384")
    return width, height


def resolve_size(
    tier: str = "1K", orientation: str = "square", exact_size: str | None = None
) -> tuple[str, str]:
    if exact_size:
        width, height = parse_size(exact_size)
        normalized = f"{width}x{height}"
        return normalized, classify_dimensions(width, height)
    normalized_tier = tier.upper().strip()
    normalized_orientation = orientation.lower().strip()
    if normalized_tier not in SIZE_PRESETS:
        raise ConfigError("Tier must be one of 1K, 2K, or 4K")
    if normalized_orientation not in SIZE_PRESETS[normalized_tier]:
        raise ConfigError("Orientation must be square, landscape, or portrait")
    return SIZE_PRESETS[normalized_tier][normalized_orientation], normalized_tier


def classify_http_error(status_code: int | None) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code == 401:
        return "authentication"
    if status_code == 403:
        return "permission"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limit"
    if status_code is not None and status_code >= 500:
        return "server_or_upstream"
    if status_code is None:
        return "network_or_timeout"
    return "api_error"


def _read_limited(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise APIError("HTTP response exceeded the safe size limit")
    return data


def _request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("x-request-id", "openai-request-id", "cf-ray"):
        value = headers.get(name)
        if value:
            return value
    return None


def _error_details(raw: bytes) -> tuple[str, str | None]:
    message = raw.decode("utf-8", errors="replace").strip()
    error_type: str | None = None
    try:
        payload = json.loads(message)
        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if isinstance(error, dict):
                extracted = error.get("message") or error.get("detail")
                if extracted:
                    message = str(extracted)
                if error.get("type"):
                    error_type = str(error["type"])
            elif isinstance(error, str):
                message = error
    except json.JSONDecodeError:
        pass
    return message or "Sub2API returned an empty error response", error_type


class ImageClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(
        self,
        method: str,
        path: str,
        body: bytes,
        content_type: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        url = f"{self.config.base_url}{path}"
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": content_type,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = _read_limited(response, MAX_RESPONSE_BYTES)
                headers = {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as exc:
            raw = _read_limited(exc, MAX_ERROR_BYTES)
            headers = {key.lower(): value for key, value in exc.headers.items()}
            message, error_type = _error_details(raw)
            raise APIError(
                redact_text(message, (self.config.api_key,)),
                status=exc.code,
                error_type=error_type,
                retry_after=headers.get("retry-after"),
                request_id=_request_id(headers),
            ) from None
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise APIError(
                redact_text(f"Sub2API request failed: {reason}", (self.config.api_key,))
            ) from None

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(
                "Sub2API returned a successful response that was not valid JSON",
                request_id=_request_id(headers),
            ) from exc
        if not isinstance(payload, dict):
            raise APIError("Sub2API JSON response must be an object")
        return payload, headers

    def generate(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self._request("POST", "/images/generations", body, "application/json")

    def edit(
        self,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, Path]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body, content_type = encode_multipart(fields, files)
        return self._request("POST", "/images/edits", body, content_type)

    def result_bytes(self, item: Mapping[str, Any]) -> bytes:
        encoded = item.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ImageValidationError("Image result contains invalid base64") from exc
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageValidationError("Decoded image exceeds the safe size limit")
            return data

        result_url = item.get("url")
        if not isinstance(result_url, str) or not result_url:
            raise ImageValidationError("Image result has neither b64_json nor url")
        return self._download(result_url)

    def _download(self, value: str) -> bytes:
        if value.startswith("data:"):
            header, separator, payload = value.partition(",")
            if not separator:
                raise ImageValidationError("Image data URL is malformed")
            try:
                if ";base64" in header.lower():
                    data = base64.b64decode(payload, validate=True)
                else:
                    data = unquote_to_bytes(payload)
            except (ValueError, binascii.Error) as exc:
                raise ImageValidationError("Image data URL is invalid") from exc
        else:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ImageValidationError("Image result URL must use HTTP(S) or data:")
            request = Request(value, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    data = _read_limited(response, MAX_IMAGE_BYTES)
            except (HTTPError, URLError, socket.timeout, TimeoutError, OSError) as exc:
                raise APIError(f"Cannot download image result: {getattr(exc, 'reason', exc)}") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageValidationError("Image result exceeds the safe size limit")
        return data


def _file_mime_type(path: Path, data: bytes) -> str:
    info = inspect_image(data)
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/") and guessed != info.mime_type:
        raise ImageValidationError(
            f"File extension and content disagree for {path}: {guessed} vs {info.mime_type}"
        )
    return info.mime_type


def read_upload(path: Path | str) -> tuple[Path, bytes, str]:
    upload_path = Path(path).expanduser()
    try:
        info = upload_path.stat()
    except FileNotFoundError as exc:
        raise ImageValidationError(f"Input image not found: {upload_path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ImageValidationError(f"Input image is not a regular file: {upload_path}")
    if info.st_size <= 0 or info.st_size > MAX_UPLOAD_BYTES:
        raise ImageValidationError(
            f"Input image size must be between 1 byte and {MAX_UPLOAD_BYTES} bytes"
        )
    try:
        data = upload_path.read_bytes()
    except OSError as exc:
        raise ImageValidationError(f"Cannot read input image {upload_path}: {exc}") from exc
    return upload_path, data, _file_mime_type(upload_path, data)


def encode_multipart(
    fields: Mapping[str, str], files: Sequence[tuple[str, Path]]
) -> tuple[bytes, str]:
    boundary = f"sub2api-image-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        if any(character in name for character in '\r\n"'):
            raise ConfigError("Multipart field name is invalid")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, path in files:
        upload_path, data, mime_type = read_upload(path)
        safe_name = upload_path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{safe_name}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def inspect_image(data: bytes) -> ImageInfo:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ImageValidationError("PNG image is truncated or missing IHDR")
        width, height = struct.unpack(">II", data[16:24])
        return _checked_info("png", "image/png", ".png", width, height)
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
        return _checked_info("jpeg", "image/jpeg", ".jpg", width, height)
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
        return _checked_info("webp", "image/webp", ".webp", width, height)
    raise ImageValidationError("Result is not a supported PNG, JPEG, or WebP image")


def _checked_info(
    image_format: str, mime_type: str, extension: str, width: int, height: int
) -> ImageInfo:
    if width <= 0 or height <= 0 or width > 65535 or height > 65535:
        raise ImageValidationError("Image dimensions are invalid")
    return ImageInfo(image_format, mime_type, extension, width, height)


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    index = 2
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index < len(data):
        while index < len(data) and data[index] != 0xFF:
            index += 1
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[index : index + 2])[0]
        if segment_length < 2 or index + segment_length > len(data):
            break
        if marker in start_of_frame:
            if segment_length < 7:
                break
            height, width = struct.unpack(">HH", data[index + 3 : index + 7])
            return width, height
        index += segment_length
    raise ImageValidationError("JPEG image is truncated or has no size marker")


def _webp_dimensions(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    payload = data[20:]
    if chunk == b"VP8X" and len(payload) >= 10:
        width = 1 + int.from_bytes(payload[4:7], "little")
        height = 1 + int.from_bytes(payload[7:10], "little")
        return width, height
    if chunk == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise ImageValidationError("WebP image is truncated or uses an unsupported header")


def _atomic_write(path: Path, data: bytes) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o644)
    except OSError as exc:
        raise ImageValidationError(f"Cannot save image at {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def save_response_images(
    client: ImageClient,
    response: Mapping[str, Any],
    output_dir: Path | str,
    operation: str,
) -> list[dict[str, Any]]:
    items = response.get("data")
    if not isinstance(items, list) or not items:
        raise ImageValidationError("Sub2API response contains no images in data[]")
    directory = Path(output_dir).expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageValidationError(f"Cannot create output directory {directory}: {exc}") from exc
    if not directory.is_dir():
        raise ImageValidationError(f"Output path is not a directory: {directory}")

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    nonce = uuid.uuid4().hex[:8]
    saved: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ImageValidationError(f"Image result {index} is not an object")
        data = client.result_bytes(item)
        info = inspect_image(data)
        path = directory / f"sub2api-{operation}-{stamp}-{nonce}-{index:02d}{info.extension}"
        _atomic_write(path, data)
        saved.append(
            {
                "path": str(path),
                "format": info.format,
                "mime_type": info.mime_type,
                "width": info.width,
                "height": info.height,
                "actual_size": f"{info.width}x{info.height}",
                "actual_tier": info.tier,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return saved


def safe_response_metadata(response: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in ("model", "size", "output_format", "quality", "background", "created"):
        value = response.get(name)
        if isinstance(value, (str, int, float, bool)):
            metadata[name] = value
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        metadata["usage"] = {
            key: value
            for key, value in usage.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    return metadata


def public_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SkillError):
        return exc.as_dict()
    return {
        "ok": False,
        "error": {
            "category": "unexpected_error",
            "message": redact_text(str(exc) or exc.__class__.__name__),
        },
    }


def print_json(payload: Mapping[str, Any], *, stream: Any = None) -> None:
    import sys

    destination = stream if stream is not None else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=destination)
