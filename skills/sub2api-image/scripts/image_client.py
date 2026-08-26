#!/usr/bin/env python3
"""Shared Sub2API Images API client and image validation helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import mimetypes
import os
import re
import socket
import ssl
import stat
import struct
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from image_stream import ImageStreamState, SSEEventError, SSEImageParser, SSEParseError


DEFAULT_BASE_URL = "https://images.vibeai.tech/v1"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_OUTPUT_DIR = "generated_images"
DEFAULT_TIMEOUT_SECONDS = 600
LEGACY_DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_PROGRESS_INTERVAL_SECONDS = 15
DEFAULT_PROVIDER_PROFILE = "sub2api-openai-oauth"
SKILL_VERSION = "1.7.0"
CONFIG_SCHEMA_VERSION = 2
DEFAULT_TIMEOUT_MIGRATION_VERSION = (1, 7, 0)
LEGACY_CONFIG_PATH = Path("~/.config/sub2api-image/config.json").expanduser()
MAX_CONFIG_BYTES = 64 * 1024
MAX_ERROR_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
USER_AGENT = f"sub2api-image-skill/{SKILL_VERSION}"
WINDOWS_DPAPI_CURRENT_USER_SCHEME = "windows-dpapi-current-user"
WINDOWS_DPAPI_LOCAL_MACHINE_SCHEME = "windows-dpapi-local-machine"
WINDOWS_DPAPI_SCHEME = WINDOWS_DPAPI_LOCAL_MACHINE_SCHEME
WINDOWS_DPAPI_SCHEMES = {
    WINDOWS_DPAPI_CURRENT_USER_SCHEME,
    WINDOWS_DPAPI_LOCAL_MACHINE_SCHEME,
}
WINDOWS_PICTURES_FOLDER_ID = "33e28130-4e1e-4676-835a-98395c3bc3bb"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4

SIZE_ALIGNMENT = 16
MIN_IMAGE_PIXELS = 655_360
MAX_IMAGE_PIXELS = 8_294_400
MAX_IMAGE_EDGE = 3_840
MAX_ASPECT_RATIO = 3

CONFIG_ENV_VARS = {
    "base_url": "SUB2API_IMAGE_BASE_URL",
    "api_key": "SUB2API_IMAGE_API_KEY",
    "model": "SUB2API_IMAGE_MODEL",
    "output_dir": "SUB2API_IMAGE_OUTPUT_DIR",
    "timeout_seconds": "SUB2API_IMAGE_TIMEOUT_SECONDS",
    "provider_profile": "SUB2API_IMAGE_PROVIDER_PROFILE",
}


def _installed_codex_home() -> Path | None:
    skill_dir = Path(__file__).resolve().parent.parent
    if skill_dir.parent.name.lower() != "skills":
        return None
    if not (skill_dir / ".runtime.json").is_file():
        return None
    return skill_dir.parent.parent


def default_config_path(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    codex_home: Path | str | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    override = environment.get("SUB2API_IMAGE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()

    selected_home = Path(home).expanduser() if home is not None else Path.home()
    if (platform_name or os.name) == "nt":
        configured_codex_home = environment.get("CODEX_HOME", "").strip()
        if codex_home is not None:
            selected_codex_home = Path(codex_home).expanduser()
        elif configured_codex_home:
            selected_codex_home = Path(configured_codex_home).expanduser()
        else:
            selected_codex_home = _installed_codex_home() or selected_home / ".codex"
        return selected_codex_home / "sub2api-image" / "config.json"
    return selected_home / ".config" / "sub2api-image" / "config.json"


DEFAULT_CONFIG_PATH = default_config_path()

PROVIDER_PROFILES = {
    DEFAULT_PROVIDER_PROFILE: {
        "default_stream": True,
        "max_images_per_request": 1,
        "presets": {
            "1K": {"square": "1024x1024"},
            "2K": {
                "landscape": "1536x1024",
                "portrait": "1024x1536",
            },
        },
    },
}


class SkillError(Exception):
    """Base exception safe to present to a user."""

    category = "skill_error"

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"category": self.category, "message": str(self)}}


class ConfigError(SkillError):
    category = "configuration"


class CredentialDecryptionError(ConfigError):
    category = "credential_decryption"

    def __init__(
        self,
        reason: str,
        *,
        error_code: int | None = None,
        config_path: Path | None = None,
        protection: str | None = None,
    ) -> None:
        self.reason = reason
        self.error_code = error_code
        self.config_path = config_path
        self.protection = protection
        message = reason
        if config_path is not None:
            message = f"{message}: {config_path.resolve()}"
        super().__init__(message)

    def with_context(self, path: Path, protection: str) -> "CredentialDecryptionError":
        return CredentialDecryptionError(
            self.reason,
            error_code=self.error_code,
            config_path=path,
            protection=protection,
        )

    def as_dict(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "category": self.category,
            "message": str(self),
            "action": (
                "Update or reconfigure the skill from the Windows user account. "
                "A replacement API key is required only if the old credential cannot be migrated."
            ),
        }
        if self.config_path is not None:
            details["config_path"] = str(self.config_path.resolve())
        if self.protection is not None:
            details["credential_protection"] = self.protection
        if self.error_code is not None:
            details["dpapi_error_code"] = self.error_code
        return {"ok": False, "error": details}


class ImageValidationError(SkillError):
    category = "image_validation"


class RequestHeartbeat:
    """Emit secret-free progress while a synchronous image request is pending."""

    def __init__(
        self,
        operation: str,
        timeout_seconds: int,
        *,
        interval_seconds: float = DEFAULT_PROGRESS_INTERVAL_SECONDS,
        client_request_id: str | None = None,
        stream: Any = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Heartbeat interval must be positive")
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.interval_seconds = interval_seconds
        self.client_request_id = client_request_id
        self.stream = stream if stream is not None else sys.stderr
        self._stop = threading.Event()
        self._started = 0.0
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RequestHeartbeat":
        self._started = time.monotonic()
        self._emit("image_request_started", 0)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            elapsed = max(1, int(time.monotonic() - self._started))
            deadline_reached = elapsed >= self.timeout_seconds
            self._emit(
                "image_request_deadline_reached"
                if deadline_reached
                else "image_request_pending",
                elapsed,
            )
            if deadline_reached:
                return

    def _emit(self, status: str, elapsed_seconds: int) -> None:
        payload = {
            "status": status,
            "operation": self.operation,
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": max(0, self.timeout_seconds - elapsed_seconds),
            "timeout_seconds": self.timeout_seconds,
        }
        if self.client_request_id:
            payload["client_request_id"] = self.client_request_id
        print(json.dumps(payload, sort_keys=True), file=self.stream, flush=True)


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
        client_request_id: str | None = None,
        server_client_request_id: str | None = None,
        category_override: str | None = None,
        billing_ambiguous: bool = False,
        transport_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.retry_after = retry_after
        self.request_id = request_id
        self.client_request_id = client_request_id
        self.server_client_request_id = server_client_request_id
        self.category_override = category_override
        self.billing_ambiguous = billing_ambiguous
        self.transport_kind = transport_kind

    def as_dict(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "category": self.category_override or classify_http_error(self.status),
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
        if self.client_request_id:
            details["client_request_id"] = self.client_request_id
        if self.server_client_request_id:
            details["server_client_request_id"] = self.server_client_request_id
        if self.transport_kind:
            details["transport"] = self.transport_kind
        if self.status == 401:
            details["retry_safe"] = False
            details["action"] = (
                "Reconfigure the skill with a valid Sub2API user API key for this "
                "image endpoint. Do not retry with the rejected key."
            )
        elif self.status == 403:
            details["retry_safe"] = False
            details["action"] = (
                "Use a Sub2API user API key whose group is enabled for image generation. "
                "Do not retry until the permission is corrected."
            )
        elif self.status == 524:
            details["retry_safe"] = False
            details["action"] = (
                "The paid request outcome is ambiguous. Check the image-only direct base URL, "
                "Cloudflare/origin timeouts, and usage logs before approving a retry."
            )
        elif self.billing_ambiguous:
            details["billing_status"] = "ambiguous"
            details["retry_safe"] = False
            details["action"] = (
                "The request may have reached the image service. Check Sub2API connectivity, "
                "the client request ID and usage logs before approving another paid request."
            )
        return {"ok": False, "error": details}


class StreamInterruptedError(APIError):
    """A generation stream ended without a trustworthy completion boundary."""

    def __init__(
        self,
        message: str,
        *,
        headers: Mapping[str, str],
        partial_response: Mapping[str, Any] | None = None,
        category: str = "stream_interrupted",
        error_type: str | None = None,
        transport_kind: str | None = None,
        client_request_id: str | None = None,
    ) -> None:
        self.headers = dict(headers)
        self.partial_response = dict(partial_response or {"data": []})
        self.partial_files: list[dict[str, Any]] = []
        self.partial_save_error: str | None = None
        super().__init__(
            message,
            error_type=error_type,
            request_id=_request_id(self.headers),
            client_request_id=client_request_id,
            server_client_request_id=response_client_request_id(
                self.headers, client_request_id
            ),
            category_override=category,
            billing_ambiguous=True,
            transport_kind=transport_kind,
        )

    def with_partial_files(
        self, files: Sequence[Mapping[str, Any]]
    ) -> "StreamInterruptedError":
        self.partial_files = [dict(item) for item in files]
        return self

    def with_partial_save_error(self, message: str) -> "StreamInterruptedError":
        self.partial_save_error = message
        return self

    def as_dict(self) -> dict[str, Any]:
        result = super().as_dict()
        details = result["error"]
        details["stream_incomplete"] = True
        details["final_images_saved"] = 0
        details["partial_image_count"] = len(
            self.partial_response.get("data", [])
        )
        if self.partial_files:
            details["partial_images"] = [dict(item) for item in self.partial_files]
            details["partial_images_are_final"] = False
        if self.partial_save_error:
            details["partial_save_error"] = self.partial_save_error
        return result


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    model: str = DEFAULT_MODEL
    output_dir: str = DEFAULT_OUTPUT_DIR
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    provider_profile: str = DEFAULT_PROVIDER_PROFILE

    def public_dict(self, path: Path | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": True,
            "base_url": self.base_url,
            "api_key": redact_key(self.api_key),
            "model": self.model,
            "output_dir": self.output_dir,
            "timeout_seconds": self.timeout_seconds,
            "provider_profile": self.provider_profile,
            "default_stream": default_stream_for_profile(self.provider_profile),
            "max_images_per_request": max_images_per_request(self.provider_profile),
        }
        if path is not None:
            result["config_path"] = str(path.resolve())
        return result


@dataclass(frozen=True)
class ConfigState:
    base_url: str
    api_key: str | None
    model: str
    output_dir: str
    timeout_seconds: int
    provider_profile: str = DEFAULT_PROVIDER_PROFILE
    credential_protection: str | None = None
    credential_error: CredentialDecryptionError | None = None
    defaults_version: str | None = None

    def with_api_key(self, api_key: str) -> Config:
        return config_from_mapping(
            {
                "base_url": self.base_url,
                "api_key": api_key,
                "model": self.model,
                "output_dir": self.output_dir,
                "timeout_seconds": self.timeout_seconds,
                "provider_profile": self.provider_profile,
            }
        )


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


@dataclass(frozen=True)
class GenerationResult:
    response: dict[str, Any]
    headers: dict[str, str]
    response_mode: str
    stream_done: bool | None = None
    stream_terminal_event: bool | None = None
    stream_event_count: int = 0
    transport_warning: dict[str, Any] | None = None


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


def validate_provider_profile(value: str) -> str:
    profile = value.strip().lower()
    if profile not in PROVIDER_PROFILES:
        supported = ", ".join(sorted(PROVIDER_PROFILES))
        raise ConfigError(f"Provider profile must be one of: {supported}")
    return profile


def default_stream_for_profile(provider_profile: str) -> bool:
    profile = validate_provider_profile(provider_profile)
    return bool(PROVIDER_PROFILES[profile]["default_stream"])


def max_images_per_request(provider_profile: str) -> int:
    profile = validate_provider_profile(provider_profile)
    return int(PROVIDER_PROFILES[profile]["max_images_per_request"])


def new_client_request_id() -> str:
    return f"img-{uuid.uuid4().hex}"


def migrated_timeout_seconds(
    existing_timeout_seconds: int | None,
    requested_timeout_seconds: int | None = None,
    *,
    defaults_version: str | None = None,
) -> tuple[int, bool]:
    """Select an install-time timeout and identify the legacy 180s migration."""
    if requested_timeout_seconds is not None:
        return requested_timeout_seconds, False
    version_numbers = (
        tuple(int(part) for part in defaults_version.split("."))
        if defaults_version and re.fullmatch(r"\d+\.\d+\.\d+", defaults_version)
        else None
    )
    if (
        existing_timeout_seconds == LEGACY_DEFAULT_TIMEOUT_SECONDS
        and (
            version_numbers is None
            or version_numbers < DEFAULT_TIMEOUT_MIGRATION_VERSION
        )
    ):
        return DEFAULT_TIMEOUT_SECONDS, True
    if existing_timeout_seconds is not None:
        return existing_timeout_seconds, False
    return DEFAULT_TIMEOUT_SECONDS, False


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
        provider_profile=validate_provider_profile(
            str(data.get("provider_profile", DEFAULT_PROVIDER_PROFILE))
        ),
    )


def _environment_overrides() -> dict[str, str]:
    return {
        field: os.environ[variable]
        for field, variable in CONFIG_ENV_VARS.items()
        if variable in os.environ
    }


def config_protection() -> str:
    if os.name == "nt":
        return WINDOWS_DPAPI_SCHEME
    return "posix-mode-0600"


def _windows_dpapi(
    data: bytes,
    *,
    protect: bool,
    machine_scope: bool = False,
) -> bytes:
    if os.name != "nt":
        raise ConfigError("Windows DPAPI credentials can only be used on Windows")

    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        input_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        input_blob = DataBlob(
            len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        output_blob = DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        if protect:
            operation = crypt32.CryptProtectData
            operation.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            flags = CRYPTPROTECT_UI_FORBIDDEN
            if machine_scope:
                flags |= CRYPTPROTECT_LOCAL_MACHINE
            arguments = (
                ctypes.byref(input_blob),
                "VibeAI Sub2API Image API Key",
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        else:
            operation = crypt32.CryptUnprotectData
            operation.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            arguments = (
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )

        operation.restype = wintypes.BOOL
        if not operation(*arguments):
            error_code = ctypes.get_last_error()
            action = "protect" if protect else "unprotect"
            message = f"Windows DPAPI could not {action} the API key (error {error_code})"
            if protect:
                raise ConfigError(message)
            raise CredentialDecryptionError(message, error_code=error_code)

        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        try:
            return ctypes.string_at(output_blob.data, output_blob.size)
        finally:
            if output_blob.data:
                kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))
    except ConfigError:
        raise
    except Exception as exc:
        message = f"Windows DPAPI is unavailable ({type(exc).__name__})"
        if protect:
            raise ConfigError(message) from exc
        raise CredentialDecryptionError(message) from exc


def _protect_api_key(value: str, protection: str = WINDOWS_DPAPI_SCHEME) -> str:
    if protection not in WINDOWS_DPAPI_SCHEMES:
        raise ConfigError("Configuration uses an unsupported API key protection scheme")
    protected = _windows_dpapi(
        value.encode("utf-8"),
        protect=True,
        machine_scope=protection == WINDOWS_DPAPI_LOCAL_MACHINE_SCHEME,
    )
    return base64.b64encode(protected).decode("ascii")


def _unprotect_api_key(value: str) -> str:
    try:
        protected = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CredentialDecryptionError(
            "Windows DPAPI API key payload is invalid"
        ) from exc
    if not protected:
        raise CredentialDecryptionError("Windows DPAPI API key payload is empty")
    raw = _windows_dpapi(protected, protect=False)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialDecryptionError(
            "Windows DPAPI API key is not valid UTF-8"
        ) from exc


def _config_payload(config: Config) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "defaults_version": SKILL_VERSION,
        "base_url": config.base_url,
        "model": config.model,
        "output_dir": config.output_dir,
        "timeout_seconds": config.timeout_seconds,
        "provider_profile": config.provider_profile,
    }
    if os.name == "nt":
        payload["api_key_protection"] = WINDOWS_DPAPI_SCHEME
        protected = _protect_api_key(config.api_key)
        if _unprotect_api_key(protected) != config.api_key:
            raise ConfigError("Windows DPAPI API key verification failed")
        payload["api_key_protected"] = protected
    else:
        payload["api_key"] = config.api_key
    return payload


def selected_config_path(
    path: Path | str | None = None, *, default_path: Path | str | None = None
) -> Path:
    selected = path if path is not None else default_path or DEFAULT_CONFIG_PATH
    return Path(selected).expanduser()


def discover_config_path(
    path: Path | str | None = None,
    *,
    default_path: Path | str | None = None,
    legacy_path: Path | str | None = None,
) -> Path:
    selected = selected_config_path(path, default_path=default_path)
    legacy = Path(legacy_path or LEGACY_CONFIG_PATH).expanduser()
    if path is None and not selected.exists() and legacy != selected and legacy.exists():
        return legacy
    return selected


def _read_config_document(config_path: Path) -> dict[str, Any]:
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
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Configuration is not valid UTF-8 JSON: {config_path}") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigError("Configuration must be a JSON object")
    return dict(parsed)


def read_config_state(
    path: Path | str,
    *,
    decrypt_credential: bool = True,
) -> ConfigState:
    config_path = Path(path).expanduser()
    data = _read_config_document(config_path)
    public_config = config_from_mapping({**data, "api_key": "validation-placeholder"})
    protection: str | None = None
    credential_error: CredentialDecryptionError | None = None
    api_key: str | None = None

    if "api_key_protected" in data:
        protection_value = data.get("api_key_protection")
        if protection_value not in WINDOWS_DPAPI_SCHEMES:
            raise ConfigError("Configuration uses an unsupported API key protection scheme")
        protection = str(protection_value)
        protected_value = data.get("api_key_protected")
        if not isinstance(protected_value, str):
            raise ConfigError("Windows DPAPI API key payload must be a string")
        if decrypt_credential:
            try:
                api_key = validate_api_key(_unprotect_api_key(protected_value))
            except CredentialDecryptionError as exc:
                credential_error = exc.with_context(config_path, protection)
    elif decrypt_credential:
        api_key = validate_api_key(str(data.get("api_key", "")))

    return ConfigState(
        base_url=public_config.base_url,
        api_key=api_key,
        model=public_config.model,
        output_dir=public_config.output_dir,
        timeout_seconds=public_config.timeout_seconds,
        provider_profile=public_config.provider_profile,
        credential_protection=protection,
        credential_error=credential_error,
        defaults_version=(
            str(data["defaults_version"])
            if isinstance(data.get("defaults_version"), str)
            else None
        ),
    )


def load_config(
    path: Path | str | None = None, *, apply_env: bool = True
) -> Config:
    config_path = discover_config_path(path)
    overrides = _environment_overrides() if apply_env else {}
    if config_path.exists():
        state = read_config_state(
            config_path,
            decrypt_credential="api_key" not in overrides,
        )
        if state.credential_error is not None:
            raise state.credential_error
        data: dict[str, Any] = {
            "base_url": state.base_url,
            "api_key": state.api_key or "",
            "model": state.model,
            "output_dir": state.output_dir,
            "timeout_seconds": state.timeout_seconds,
            "provider_profile": state.provider_profile,
        }
    else:
        if "api_key" not in overrides:
            raise ConfigError(
                f"Configuration not found at {config_path}; run configure.py first"
            )
        data = {}
    data.update(overrides)
    return config_from_mapping(data)


def save_config(config: Config, path: Path | str | None = None) -> Path:
    config_path = selected_config_path(path)
    parent = config_path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            parent.chmod(0o700)
        except OSError as exc:
            raise ConfigError(f"Cannot secure configuration directory {parent}: {exc}") from exc

    payload = json.dumps(
        _config_payload(config),
        indent=2,
        ensure_ascii=True,
    ).encode("utf-8") + b"\n"

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary_name)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
        if os.name == "posix":
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


def _windows_pictures_directory() -> Path:
    if os.name != "nt":
        raise ConfigError("The Windows Pictures known folder is available only on Windows")

    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("data1", wintypes.DWORD),
                ("data2", wintypes.WORD),
                ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8),
            ]

        folder_id = GUID.from_buffer_copy(
            uuid.UUID(WINDOWS_PICTURES_FOLDER_ID).bytes_le
        )
        selected = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        operation = shell32.SHGetKnownFolderPath
        operation.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        operation.restype = ctypes.c_long
        result = operation(ctypes.byref(folder_id), 0, None, ctypes.byref(selected))
        if result != 0:
            raise ConfigError(
                f"Windows could not resolve the Pictures known folder (HRESULT 0x{result & 0xFFFFFFFF:08X})"
            )
        try:
            if not selected.value:
                raise ConfigError("Windows returned an empty Pictures known folder path")
            return Path(selected.value).resolve()
        finally:
            if selected:
                ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ole32.CoTaskMemFree.restype = None
                ole32.CoTaskMemFree(ctypes.cast(selected, ctypes.c_void_p))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"Windows Pictures known folder is unavailable ({type(exc).__name__})"
        ) from exc


def pictures_directory(
    *,
    platform_name: str | None = None,
    home: Path | str | None = None,
    windows_resolver: Callable[[], Path] | None = None,
) -> Path:
    if (platform_name or os.name) == "nt":
        resolver = windows_resolver or _windows_pictures_directory
        return Path(resolver()).expanduser().resolve()
    selected_home = Path(home).expanduser() if home is not None else Path.home()
    return (selected_home / "Pictures").resolve()


def pictures_output(
    filename: str | None,
    *,
    directory: Path | str | None = None,
) -> tuple[Path | None, Path | None]:
    selected_directory = (
        Path(directory).expanduser().resolve()
        if directory is not None
        else pictures_directory()
    )
    if filename is None or filename == "":
        return selected_directory, None
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ConfigError("--pictures accepts a filename, not a path")
    selected_name = Path(filename)
    if selected_name.is_absolute() or selected_name.name != filename:
        raise ConfigError("--pictures accepts a filename, not a path")
    return None, selected_directory / selected_name


def classify_dimensions(width: int, height: int) -> str:
    maximum = max(width, height)
    if maximum <= 1024:
        return "1K"
    if maximum <= 2048:
        return "2K"
    return "4K"


def _size_violations(width: int, height: int) -> list[str]:
    violations: list[str] = []
    if width % SIZE_ALIGNMENT or height % SIZE_ALIGNMENT:
        violations.append(f"both dimensions must be multiples of {SIZE_ALIGNMENT}")
    if max(width, height) > MAX_IMAGE_EDGE:
        violations.append(f"the longest edge must not exceed {MAX_IMAGE_EDGE}")
    pixels = width * height
    if pixels < MIN_IMAGE_PIXELS or pixels > MAX_IMAGE_PIXELS:
        violations.append(
            f"total pixels must be between {MIN_IMAGE_PIXELS} and {MAX_IMAGE_PIXELS}"
        )
    if min(width, height) <= 0 or max(width, height) > min(width, height) * MAX_ASPECT_RATIO:
        violations.append(f"the aspect ratio must not exceed {MAX_ASPECT_RATIO}:1")
    return violations


def nearest_valid_size(width: int, height: int) -> tuple[int, int]:
    target_width = max(width, 1)
    target_height = max(height, 1)
    best: tuple[float, int, int, int] | None = None
    for candidate_width in range(SIZE_ALIGNMENT, MAX_IMAGE_EDGE + 1, SIZE_ALIGNMENT):
        for candidate_height in range(SIZE_ALIGNMENT, MAX_IMAGE_EDGE + 1, SIZE_ALIGNMENT):
            if _size_violations(candidate_width, candidate_height):
                continue
            distance = (
                ((candidate_width - target_width) / target_width) ** 2
                + ((candidate_height - target_height) / target_height) ** 2
            )
            area_delta = abs(candidate_width * candidate_height - width * height)
            score = (distance, area_delta, candidate_width, candidate_height)
            if best is None or score < best:
                best = score
    if best is None:  # Constants above always admit at least one size.
        raise ConfigError("No legal image size can be suggested")
    return best[2], best[3]


def parse_size(value: str) -> tuple[int, int] | None:
    if value.strip().lower() == "auto":
        return None
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        raise ConfigError("Size must be auto or WIDTHxHEIGHT, for example 1024x1024")
    width, height = int(match.group(1)), int(match.group(2))
    violations = _size_violations(width, height)
    if violations:
        suggested_width, suggested_height = nearest_valid_size(width, height)
        raise ConfigError(
            f"Invalid image size {width}x{height}: {'; '.join(violations)}. "
            f"Nearest legal suggestion: {suggested_width}x{suggested_height}"
        )
    return width, height


def resolve_size(
    tier: str = "1K",
    orientation: str = "square",
    exact_size: str | None = None,
    *,
    provider_profile: str = DEFAULT_PROVIDER_PROFILE,
) -> tuple[str, str | None]:
    if exact_size:
        parsed = parse_size(exact_size)
        if parsed is None:
            return "auto", None
        width, height = parsed
        normalized = f"{width}x{height}"
        return normalized, classify_dimensions(width, height)
    normalized_tier = tier.upper().strip()
    normalized_orientation = orientation.lower().strip()
    if normalized_tier not in {"1K", "2K", "4K"}:
        raise ConfigError("Tier must be one of 1K, 2K, or 4K")
    if normalized_orientation not in {"square", "landscape", "portrait"}:
        raise ConfigError("Orientation must be square, landscape, or portrait")
    profile = validate_provider_profile(provider_profile)
    presets = PROVIDER_PROFILES[profile]["presets"]
    size = presets.get(normalized_tier, {}).get(normalized_orientation)
    if size is None:
        raise ConfigError(
            f"The {profile} profile has no verified {normalized_tier} "
            f"{normalized_orientation} preset. Use a verified preset or pass an exact "
            "--size supported by your OAuth image backend."
        )
    return str(size), normalized_tier


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
    if status_code == 524:
        return "edge_timeout"
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


def _transport_details(exc: BaseException) -> tuple[str, BaseException]:
    reason: BaseException = exc
    while isinstance(reason, URLError) and isinstance(reason.reason, BaseException):
        reason = reason.reason

    if isinstance(reason, ssl.SSLError):
        message = str(reason).upper()
        category = (
            "tls_unexpected_eof"
            if isinstance(reason, ssl.SSLEOFError) or "UNEXPECTED_EOF" in message
            else "tls_error"
        )
    elif isinstance(reason, http.client.IncompleteRead):
        category = "incomplete_response"
    elif isinstance(reason, http.client.RemoteDisconnected):
        category = "remote_disconnected"
    elif isinstance(reason, (ConnectionResetError, BrokenPipeError)):
        category = "connection_reset"
    elif isinstance(reason, ConnectionAbortedError):
        category = "connection_aborted"
    elif isinstance(reason, (socket.timeout, TimeoutError)):
        category = "network_timeout"
    elif isinstance(reason, EOFError):
        category = "incomplete_response"
    else:
        category = "network_error"
    return category, reason


def _transport_api_error(
    exc: BaseException,
    *,
    api_key: str,
    headers: Mapping[str, str] | None = None,
    client_request_id: str | None = None,
    billing_ambiguous: bool = True,
) -> APIError:
    category, reason = _transport_details(exc)
    return APIError(
        redact_text(f"Sub2API request failed: {reason}", (api_key,)),
        request_id=_request_id(headers or {}),
        client_request_id=client_request_id,
        server_client_request_id=response_client_request_id(
            headers or {}, client_request_id
        ),
        category_override=category,
        billing_ambiguous=billing_ambiguous,
        transport_kind=category,
    )


def _request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("x-request-id", "openai-request-id", "cf-ray"):
        value = headers.get(name)
        if value:
            return value
    return None


def _client_request_id(headers: Mapping[str, str]) -> str | None:
    value = headers.get("x-client-request-id")
    return value if value else None


def response_client_request_id(
    headers: Mapping[str, str], client_request_id: str | None
) -> str | None:
    response_id = _client_request_id(headers)
    if response_id and response_id != client_request_id:
        return response_id
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


def _parse_json_response(
    raw: bytes,
    headers: Mapping[str, str],
    *,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise APIError(
            "Sub2API returned a successful response that was not valid JSON",
            request_id=_request_id(headers),
            client_request_id=client_request_id or _client_request_id(headers),
            server_client_request_id=response_client_request_id(
                headers, client_request_id
            ),
        ) from exc
    if not isinstance(payload, dict):
        raise APIError(
            "Sub2API JSON response must be an object",
            request_id=_request_id(headers),
            client_request_id=client_request_id or _client_request_id(headers),
            server_client_request_id=response_client_request_id(
                headers, client_request_id
            ),
        )
    if payload.get("error") is not None:
        message, error_type = _error_details(raw)
        raise APIError(
            message,
            error_type=error_type,
            request_id=_request_id(headers),
            client_request_id=client_request_id or _client_request_id(headers),
            server_client_request_id=response_client_request_id(
                headers, client_request_id
            ),
        )
    return payload


class ImageClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(
        self,
        method: str,
        path: str,
        body: bytes,
        content_type: str,
        *,
        client_request_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        selected_client_request_id = client_request_id or new_client_request_id()
        url = f"{self.config.base_url}{path}"
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Cache-Control": "no-store",
                "Content-Type": content_type,
                "Pragma": "no-cache",
                "User-Agent": USER_AGENT,
                "X-Client-Request-Id": selected_client_request_id,
                "X-Request-ID": selected_client_request_id,
            },
        )
        headers: dict[str, str] = {}
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                headers.setdefault("x-client-request-id", selected_client_request_id)
                raw = _read_limited(response, MAX_RESPONSE_BYTES)
        except HTTPError as exc:
            raw = _read_limited(exc, MAX_ERROR_BYTES)
            headers = {key.lower(): value for key, value in exc.headers.items()}
            headers.setdefault("x-client-request-id", selected_client_request_id)
            message, error_type = _error_details(raw)
            raise APIError(
                redact_text(message, (self.config.api_key,)),
                status=exc.code,
                error_type=error_type,
                retry_after=headers.get("retry-after"),
                request_id=_request_id(headers),
                client_request_id=selected_client_request_id,
                server_client_request_id=response_client_request_id(
                    headers, selected_client_request_id
                ),
            ) from None
        except (
            http.client.IncompleteRead,
            URLError,
            socket.timeout,
            TimeoutError,
            OSError,
        ) as exc:
            raise _transport_api_error(
                exc,
                api_key=self.config.api_key,
                headers=headers,
                client_request_id=selected_client_request_id,
            ) from None

        return (
            _parse_json_response(
                raw, headers, client_request_id=selected_client_request_id
            ),
            headers,
        )

    def generate(
        self,
        payload: Mapping[str, Any],
        *,
        client_request_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self._request(
            "POST",
            "/images/generations",
            body,
            "application/json",
            client_request_id=client_request_id,
        )

    def generate_stream(
        self,
        payload: Mapping[str, Any],
        *,
        client_request_id: str | None = None,
    ) -> GenerationResult:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        return self._stream_request(
            "/images/generations",
            body,
            "application/json",
            expected_count=int(payload.get("n", 1)),
            client_request_id=client_request_id,
        )

    def _stream_request(
        self,
        path: str,
        body: bytes,
        content_type: str,
        *,
        expected_count: int,
        client_request_id: str | None = None,
    ) -> GenerationResult:
        selected_client_request_id = client_request_id or new_client_request_id()
        request = Request(
            f"{self.config.base_url}{path}",
            data=body,
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self.config.api_key}",
                "Cache-Control": "no-store",
                "Content-Type": content_type,
                "Pragma": "no-cache",
                "User-Agent": USER_AGENT,
                "X-Client-Request-Id": selected_client_request_id,
                "X-Request-ID": selected_client_request_id,
            },
        )
        headers: dict[str, str] = {}
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                headers.setdefault("x-client-request-id", selected_client_request_id)
                content_type = headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    raw = _read_limited(response, MAX_RESPONSE_BYTES)
                    return GenerationResult(
                        response=_parse_json_response(
                            raw,
                            headers,
                            client_request_id=selected_client_request_id,
                        ),
                        headers=headers,
                        response_mode="json",
                    )
                return self._read_generation_stream(
                    response,
                    headers,
                    expected_count=expected_count,
                    client_request_id=selected_client_request_id,
                )
        except HTTPError as exc:
            raw = _read_limited(exc, MAX_ERROR_BYTES)
            headers = {key.lower(): value for key, value in exc.headers.items()}
            headers.setdefault("x-client-request-id", selected_client_request_id)
            message, error_type = _error_details(raw)
            raise APIError(
                redact_text(message, (self.config.api_key,)),
                status=exc.code,
                error_type=error_type,
                retry_after=headers.get("retry-after"),
                request_id=_request_id(headers),
                client_request_id=selected_client_request_id,
                server_client_request_id=response_client_request_id(
                    headers, selected_client_request_id
                ),
            ) from None
        except StreamInterruptedError:
            raise
        except (
            http.client.IncompleteRead,
            URLError,
            socket.timeout,
            TimeoutError,
            OSError,
        ) as exc:
            raise _transport_api_error(
                exc,
                api_key=self.config.api_key,
                headers=headers,
                client_request_id=selected_client_request_id,
            ) from None

    def _read_generation_stream(
        self,
        response: Any,
        headers: Mapping[str, str],
        *,
        expected_count: int,
        client_request_id: str,
    ) -> GenerationResult:
        parser = SSEImageParser()
        failure: BaseException | None = None
        try:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                parser.feed(chunk)
            state = parser.finish()
        except http.client.IncompleteRead as exc:
            failure = exc
            try:
                if exc.partial:
                    parser.feed(bytes(exc.partial))
                state = parser.finish()
            except SSEParseError as parse_error:
                failure = parse_error
                state = parser.state
        except SSEParseError as exc:
            failure = exc
            state = parser.state
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            failure = exc
            try:
                state = parser.finish()
            except SSEParseError as parse_error:
                failure = parse_error
                state = parser.state

        validated_partial, partial_error = self._validated_stream_items(
            state.partial_data
        )
        validated_completed, completed_error = self._validated_stream_items(state.data)
        state.partial_data = validated_partial
        state.data = validated_completed
        validation_error = completed_error or partial_error
        if validation_error is not None:
            failure = validation_error

        terminal_event_received = (
            state.completed_count >= expected_count and len(state.data) >= expected_count
        )
        if failure is None and not state.done and not terminal_event_received:
            failure = EOFError("SSE stream ended before a completed image event")
        if failure is None and not state.data:
            failure = SSEParseError("SSE stream completed without a final image")

        transport_failure = isinstance(
            failure,
            (
                EOFError,
                http.client.IncompleteRead,
                URLError,
                socket.timeout,
                TimeoutError,
                OSError,
            ),
        )
        if failure is not None and terminal_event_received and transport_failure:
            category, reason = _transport_details(failure)
            return GenerationResult(
                response=state.response(),
                headers=dict(headers),
                response_mode="sse",
                stream_done=state.done,
                stream_terminal_event=terminal_event_received,
                stream_event_count=state.event_count,
                transport_warning={
                    "category": category,
                    "message": redact_text(
                        f"The stream ended after a completed image was received: {reason}",
                        (self.config.api_key,),
                    ),
                    "retry_safe": False,
                    "final_image_received": True,
                },
            )
        if failure is not None:
            raise self._stream_interruption(
                failure,
                headers,
                state,
                client_request_id=client_request_id,
            ) from None

        return GenerationResult(
            response=state.response(),
            headers=dict(headers),
            response_mode="sse",
            stream_done=state.done,
            stream_terminal_event=terminal_event_received,
            stream_event_count=state.event_count,
        )

    def _validated_stream_items(
        self, items: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], Exception | None]:
        valid: list[dict[str, Any]] = []
        for item in items:
            try:
                inspect_image(self.result_bytes(item))
            except (APIError, ImageValidationError) as exc:
                return valid, exc
            valid.append(dict(item))
        return valid, None

    def _stream_interruption(
        self,
        failure: BaseException,
        headers: Mapping[str, str],
        state: ImageStreamState,
        *,
        client_request_id: str,
    ) -> StreamInterruptedError:
        error_type: str | None = None
        transport_kind: str | None = None
        if isinstance(failure, SSEParseError):
            category = failure.category
            error_type = failure.error_type
            reason: BaseException = failure
        elif isinstance(failure, ImageValidationError):
            category = "stream_image_validation"
            reason = failure
        elif isinstance(failure, EOFError):
            category = "incomplete_response"
            transport_kind = category
            reason = failure
        else:
            category, reason = _transport_details(failure)
            transport_kind = category
        return StreamInterruptedError(
            redact_text(
                f"Sub2API image stream was interrupted: {reason}",
                (self.config.api_key,),
            ),
            headers=headers,
            partial_response=state.partial_response(),
            category=category,
            error_type=error_type,
            transport_kind=transport_kind,
            client_request_id=client_request_id,
        )

    def edit(
        self,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, Path]],
        *,
        client_request_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body, content_type = encode_multipart(fields, files)
        return self._request(
            "POST",
            "/images/edits",
            body,
            content_type,
            client_request_id=client_request_id,
        )

    def edit_stream(
        self,
        fields: Mapping[str, str],
        files: Sequence[tuple[str, Path]],
        *,
        expected_count: int = 1,
        client_request_id: str | None = None,
    ) -> GenerationResult:
        body, content_type = encode_multipart(fields, files)
        return self._stream_request(
            "/images/edits",
            body,
            content_type,
            expected_count=expected_count,
            client_request_id=client_request_id,
        )

    def probe_health(self) -> dict[str, Any]:
        """Check the image ingress TLS and health route without creating an image."""
        parsed = urlsplit(self.config.base_url)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
        client_request_id = new_client_request_id()
        request = Request(
            endpoint,
            method="GET",
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "User-Agent": USER_AGENT,
                "X-Client-Request-Id": client_request_id,
                "X-Request-ID": client_request_id,
            },
        )
        headers: dict[str, str] = {}
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                headers.setdefault("x-client-request-id", client_request_id)
                raw = _read_limited(response, MAX_ERROR_BYTES)
                status = getattr(response, "status", response.getcode())
        except HTTPError as exc:
            raw = _read_limited(exc, MAX_ERROR_BYTES)
            headers = {key.lower(): value for key, value in exc.headers.items()}
            headers.setdefault("x-client-request-id", client_request_id)
            message, error_type = _error_details(raw)
            raise APIError(
                redact_text(message, (self.config.api_key,)),
                status=exc.code,
                error_type=error_type,
                retry_after=headers.get("retry-after"),
                request_id=_request_id(headers),
                client_request_id=client_request_id,
                server_client_request_id=response_client_request_id(
                    headers, client_request_id
                ),
            ) from None
        except (
            http.client.IncompleteRead,
            URLError,
            socket.timeout,
            TimeoutError,
            OSError,
        ) as exc:
            raise _transport_api_error(
                exc,
                api_key=self.config.api_key,
                headers=headers,
                client_request_id=client_request_id,
                billing_ambiguous=False,
            ) from None
        result: dict[str, Any] = {
            "ok": 200 <= int(status) < 300,
            "endpoint": endpoint,
            "status": int(status),
            "content_type": headers.get("content-type"),
            "response_bytes": len(raw),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "image_request_sent": False,
            "billing_expected": False,
            "authentication_checked": False,
            "client_request_id": client_request_id,
        }
        identifier = _request_id(headers)
        if identifier:
            result["request_id"] = identifier
        server_client_id = response_client_request_id(headers, client_request_id)
        if server_client_id:
            result["server_client_request_id"] = server_client_id
        return result

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
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name == "posix":
            path.chmod(0o644)
    except OSError as exc:
        raise ImageValidationError(f"Cannot save file at {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _extension_for_format(image_format: str) -> str:
    if image_format == "png":
        return ".png"
    if image_format == "jpeg":
        return ".jpg"
    if image_format == "webp":
        return ".webp"
    raise ConfigError(f"Unsupported image format for output path: {image_format}")


def planned_output_paths(
    output_path: Path | str, formats: Sequence[str]
) -> list[Path]:
    if not formats:
        raise ConfigError("At least one output format is required")
    selected = Path(output_path).expanduser().resolve()
    if selected.exists() and selected.is_dir():
        raise ConfigError(f"--output must be a file path, not a directory: {selected}")
    supported_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    if selected.suffix and selected.suffix.lower() not in supported_suffixes:
        raise ConfigError("--output must omit its extension or use .png, .jpg, .jpeg, or .webp")

    stem_path = selected.with_suffix("") if selected.suffix else selected
    multiple = len(formats) > 1
    paths: list[Path] = []
    for index, image_format in enumerate(formats, start=1):
        extension = _extension_for_format(image_format)
        if (
            not multiple
            and selected.suffix.lower() in supported_suffixes
            and (
                selected.suffix.lower() == extension
                or image_format == "jpeg" and selected.suffix.lower() == ".jpeg"
            )
        ):
            paths.append(selected)
            continue
        suffix = f"-{index:02d}" if multiple else ""
        paths.append(stem_path.with_name(f"{stem_path.name}{suffix}{extension}"))
    return paths


def _check_output_paths(paths: Sequence[Path], overwrite: bool) -> None:
    for path in paths:
        if path.exists():
            if path.is_dir():
                raise ImageValidationError(f"Output path is a directory: {path}")
            if not overwrite:
                raise ImageValidationError(
                    f"Output file already exists: {path}; pass --overwrite to replace it"
                )
        if path.parent.exists() and not path.parent.is_dir():
            raise ImageValidationError(f"Output parent is not a directory: {path.parent}")


def preflight_output_path(
    output_path: Path | str,
    count: int,
    output_format: str,
    overwrite: bool = False,
) -> list[Path]:
    paths = planned_output_paths(output_path, [output_format] * count)
    _check_output_paths(paths, overwrite)
    return paths


def save_response_images(
    client: ImageClient,
    response: Mapping[str, Any],
    output_dir: Path | str | None,
    operation: str,
    *,
    output_path: Path | str | None = None,
    overwrite: bool = False,
    protected_paths: Sequence[Path | str] = (),
) -> list[dict[str, Any]]:
    items = response.get("data")
    if not isinstance(items, list) or not items:
        raise ImageValidationError("Sub2API response contains no images in data[]")
    if output_path is not None and output_dir is not None:
        raise ConfigError("Use either output_path or output_dir, not both")

    decoded: list[tuple[bytes, ImageInfo]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ImageValidationError(f"Image result {index} is not an object")
        data = client.result_bytes(item)
        info = inspect_image(data)
        decoded.append((data, info))

    if output_path is not None:
        paths = planned_output_paths(output_path, [info.format for _, info in decoded])
        _check_output_paths(paths, overwrite)
        directories = {path.parent for path in paths}
    else:
        if output_dir is None:
            raise ConfigError("An output directory or output file is required")
        directory = Path(output_dir).expanduser().resolve()
        if directory.exists() and not directory.is_dir():
            raise ImageValidationError(f"Output path is not a directory: {directory}")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        nonce = uuid.uuid4().hex[:8]
        paths = [
            directory / f"sub2api-{operation}-{stamp}-{nonce}-{index:02d}{info.extension}"
            for index, (_, info) in enumerate(decoded, start=1)
        ]
        directories = {directory}

    protected = {Path(path).expanduser().resolve() for path in protected_paths}
    if any(path in protected for path in paths):
        raise ImageValidationError("Output path must not replace a protected input file")

    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ImageValidationError(f"Cannot create output directory {directory}: {exc}") from exc

    saved: list[dict[str, Any]] = []
    for path, (data, info) in zip(paths, decoded):
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


def write_json_metadata(
    path: Path | str, payload: Mapping[str, Any], *, overwrite: bool = False
) -> Path:
    metadata_path = Path(path).expanduser().resolve()
    _check_output_paths([metadata_path], overwrite)
    try:
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageValidationError(
            f"Cannot create metadata directory {metadata_path.parent}: {exc}"
        ) from exc
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(metadata_path, data)
    return metadata_path


def safe_response_metadata(response: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in ("model", "size", "output_format", "quality", "background", "created"):
        value = response.get(name)
        if _is_safe_metadata_scalar(name, value):
            metadata[name] = value
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        metadata["usage"] = {
            key: value
            for key, value in usage.items()
            if isinstance(key, str) and _is_safe_metadata_scalar(key, value)
        }
    return metadata


def _is_safe_metadata_scalar(name: str, value: Any) -> bool:
    normalized_name = name.lower()
    if normalized_name in {"url", "b64_json"} or normalized_name.endswith("_url"):
        return False
    if not isinstance(value, (str, int, float, bool)):
        return False
    if isinstance(value, str):
        if len(value) > 4096 or value.lower().startswith("data:"):
            return False
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"}:
            return False
    return True


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
