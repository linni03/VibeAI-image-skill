from __future__ import annotations

import base64
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "sub2api-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure as configure_cli  # noqa: E402
import image_client  # noqa: E402
from edit import edit_image  # noqa: E402
from generate import generate_images  # noqa: E402
from image_client import (  # noqa: E402
    APIError,
    Config,
    ConfigError,
    CredentialDecryptionError,
    ImageClient,
    ImageValidationError,
    RequestHeartbeat,
    default_config_path,
    discover_config_path,
    inspect_image,
    load_config,
    normalize_base_url,
    parse_size,
    pictures_directory,
    pictures_output,
    public_error,
    read_config_state,
    resolve_size,
    save_config,
)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = (b"\x00" + b"\xff\xff\xff" * width) * height
    return (
        signature
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(rows, level=9))
        + png_chunk(b"IEND", b"")
    )


def make_jpeg_header(width: int, height: int) -> bytes:
    components = b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    sof = b"\x08" + struct.pack(">HHB", height, width, 3) + components
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof + b"\xff\xd9"


def make_webp_header(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


class MockState:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.image = make_png(1024, 1024)
        self.response_mode = "b64"
        self.response_count = 1
        self.error_status: int | None = None
        self.delay_seconds = 0.0


class MockHandler(BaseHTTPRequestHandler):
    server: "MockHTTPServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.state.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if self.server.state.delay_seconds:
            time.sleep(self.server.state.delay_seconds)
        if self.server.state.error_status is not None:
            payload = json.dumps(
                {
                    "error": {
                        "type": "authentication_error",
                        "message": "invalid secret-test-key",
                    }
                }
            ).encode()
            self.send_response(self.server.state.error_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        encoded = base64.b64encode(self.server.state.image).decode()
        item = (
            {"url": f"data:image/png;base64,{encoded}"}
            if self.server.state.response_mode == "data-url"
            else {"b64_json": encoded}
        )
        payload = json.dumps(
            {
                "created": 1_700_000_000,
                "model": "gpt-image-2",
                "size": "1024x1024",
                "signed_result_url": "https://signed.example/image?token=do-not-store",
                "usage": {
                    "image_count": self.server.state.response_count,
                    "result_url": "https://signed.example/usage?token=do-not-store",
                },
                "data": [dict(item) for _ in range(self.server.state.response_count)],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-ID", "req-test-123")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockHTTPServer(ThreadingHTTPServer):
    state: MockState


class MockServer:
    def __init__(self) -> None:
        self.state = MockState()
        self.server = MockHTTPServer(("127.0.0.1", 0), MockHandler)
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "MockServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"


class ConfigTests(unittest.TestCase):
    def test_new_configuration_defaults_to_three_minutes(self) -> None:
        self.assertEqual(image_client.DEFAULT_TIMEOUT_SECONDS, 180)
        self.assertEqual(image_client.DEFAULT_PROGRESS_INTERVAL_SECONDS, 15)
        self.assertEqual(
            Config("https://images.example.test/v1", "secret").timeout_seconds,
            180,
        )

    def test_platform_default_config_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            codex_home = Path(directory) / "codex-home"
            self.assertEqual(
                default_config_path(
                    platform_name="nt",
                    environ={},
                    home=home,
                ),
                home / ".codex" / "sub2api-image" / "config.json",
            )
            self.assertEqual(
                default_config_path(
                    platform_name="nt",
                    environ={},
                    home=home,
                    codex_home=codex_home,
                ),
                codex_home / "sub2api-image" / "config.json",
            )
            self.assertEqual(
                default_config_path(
                    platform_name="posix",
                    environ={},
                    home=home,
                ),
                home / ".config" / "sub2api-image" / "config.json",
            )
            override = Path(directory) / "override.json"
            self.assertEqual(
                default_config_path(
                    platform_name="nt",
                    environ={"SUB2API_IMAGE_CONFIG": str(override)},
                    home=home,
                ),
                override,
            )

    def test_legacy_config_is_read_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex" / "sub2api-image" / "config.json"
            legacy = Path(directory) / "legacy" / "config.json"
            config = Config("https://example.test/v1", "secret-test-key")
            save_config(config, legacy)

            self.assertEqual(
                discover_config_path(
                    default_path=target,
                    legacy_path=legacy,
                ),
                legacy,
            )
            with (
                patch("image_client.DEFAULT_CONFIG_PATH", target),
                patch("image_client.LEGACY_CONFIG_PATH", legacy),
            ):
                self.assertEqual(load_config(), config)
            self.assertTrue(legacy.is_file())

    def test_interactive_configure_migrates_and_retains_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex" / "sub2api-image" / "config.json"
            legacy = Path(directory) / "legacy" / "config.json"
            config = Config("https://example.test/v1", "secret-test-key")
            save_config(config, legacy)
            args = SimpleNamespace(
                config=None,
                base_url=None,
                model=None,
                output_dir=None,
                timeout=None,
            )
            key_prompts: list[str] = []

            def preserve_visible_key(prompt: str) -> str:
                key_prompts.append(prompt)
                return ""

            with (
                patch("image_client.DEFAULT_CONFIG_PATH", target),
                patch("image_client.LEGACY_CONFIG_PATH", legacy),
                patch.object(configure_cli.sys.stdin, "isatty", return_value=True),
            ):
                report = configure_cli.configure(
                    args,
                    key_input_fn=preserve_visible_key,
                )

            self.assertEqual(report["migrated_from"], str(legacy.resolve()))
            self.assertTrue(report["legacy_config_retained"])
            self.assertIn("input visible", key_prompts[0])
            self.assertEqual(load_config(target), config)
            self.assertTrue(legacy.is_file())

    def test_revoke_removes_current_and_retained_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "codex" / "sub2api-image" / "config.json"
            legacy = Path(directory) / "legacy" / "config.json"
            config = Config("https://example.test/v1", "secret-test-key")
            save_config(config, target)
            save_config(config, legacy)

            with (
                patch("image_client.DEFAULT_CONFIG_PATH", target),
                patch.object(configure_cli, "LEGACY_CONFIG_PATH", legacy),
            ):
                report = configure_cli.remove_config(None)

            self.assertTrue(report["removed"])
            self.assertEqual(len(report["removed_paths"]), 2)
            self.assertFalse(target.exists())
            self.assertFalse(legacy.exists())

    def test_normalize_base_url(self) -> None:
        self.assertEqual(normalize_base_url("https://example.test"), "https://example.test/v1")
        self.assertEqual(normalize_base_url("https://example.test/v1/"), "https://example.test/v1")
        self.assertEqual(normalize_base_url("https://example.test/v1/v1"), "https://example.test/v1")
        self.assertEqual(
            normalize_base_url("https://example.test/openai"),
            "https://example.test/openai/v1",
        )
        with self.assertRaises(ConfigError):
            normalize_base_url("https://user:secret@example.test/v1")

    def test_config_is_atomic_private_and_show_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "private" / "config.json"
            config = Config("https://example.test/v1", "secret-test-key")
            save_config(config, config_path)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            else:
                stored = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertNotIn("api_key", stored)
                self.assertEqual(
                    stored["api_key_protection"], "windows-dpapi-local-machine"
                )
            self.assertEqual(load_config(config_path), config)

            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "configure.py"), "--show", "--config", str(config_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("secret-test-key", result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["api_key"], "<configured>")

    def test_windows_local_machine_payload_round_trip(self) -> None:
        config = Config("https://example.test/v1", "secret-test-key")
        calls: list[tuple[bool, bool]] = []

        def fake_dpapi(
            data: bytes,
            *,
            protect: bool,
            machine_scope: bool = False,
        ) -> bytes:
            calls.append((protect, machine_scope))
            return b"encrypted" if protect else b"secret-test-key"

        with (
            patch("image_client.os.name", "nt"),
            patch("image_client._windows_dpapi", side_effect=fake_dpapi),
        ):
            payload = image_client._config_payload(config)

        self.assertNotIn("api_key", payload)
        self.assertEqual(
            payload["api_key_protection"], "windows-dpapi-local-machine"
        )
        self.assertEqual(calls, [(True, True), (False, False)])

    def test_current_user_dpapi_config_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://images.example.test/v1",
                        "model": "legacy-model",
                        "output_dir": "legacy-output",
                        "timeout_seconds": 321,
                        "api_key_protection": "windows-dpapi-current-user",
                        "api_key_protected": base64.b64encode(b"encrypted").decode(),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            with patch(
                "image_client._unprotect_api_key",
                return_value="legacy-secret",
            ):
                state = read_config_state(config_path)

            self.assertEqual(state.api_key, "legacy-secret")
            self.assertEqual(
                state.credential_protection, "windows-dpapi-current-user"
            )
            self.assertIsNone(state.credential_error)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI migration check")
    def test_real_current_user_dpapi_config_migrates_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            protected = image_client._protect_api_key(
                "legacy-secret",
                image_client.WINDOWS_DPAPI_CURRENT_USER_SCHEME,
            )
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://images.example.test/v1",
                        "api_key_protection": "windows-dpapi-current-user",
                        "api_key_protected": protected,
                    }
                ),
                encoding="utf-8",
            )
            state = read_config_state(config_path)
            self.assertEqual(state.api_key, "legacy-secret")
            migrated = state.with_api_key(state.api_key or "")
            save_config(migrated, config_path)
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored["api_key_protection"], "windows-dpapi-local-machine"
            )
            self.assertEqual(load_config(config_path, apply_env=False), migrated)

    def test_unreadable_dpapi_config_preserves_public_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://images.example.test/v1",
                        "model": "preserved-model",
                        "output_dir": "preserved-output",
                        "timeout_seconds": 321,
                        "api_key_protection": "windows-dpapi-current-user",
                        "api_key_protected": base64.b64encode(b"encrypted").decode(),
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            failure = CredentialDecryptionError(
                "Windows DPAPI could not unprotect the API key (error 13)",
                error_code=13,
            )

            with patch("image_client._unprotect_api_key", side_effect=failure):
                state = read_config_state(config_path)
                with self.assertRaises(CredentialDecryptionError):
                    load_config(config_path, apply_env=False)

            self.assertIsNone(state.api_key)
            self.assertEqual(state.base_url, "https://images.example.test/v1")
            self.assertEqual(state.model, "preserved-model")
            self.assertEqual(state.output_dir, "preserved-output")
            self.assertEqual(state.timeout_seconds, 321)
            self.assertIsNotNone(state.credential_error)
            error = public_error(state.credential_error)["error"]
            self.assertEqual(error["category"], "credential_decryption")
            self.assertEqual(error["dpapi_error_code"], 13)
            self.assertEqual(error["config_path"], str(config_path.resolve()))
            self.assertNotIn("encrypted", json.dumps(error))

    def test_interactive_configure_replaces_unreadable_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            original = {
                "base_url": "https://images.example.test/v1",
                "model": "preserved-model",
                "output_dir": "preserved-output",
                "timeout_seconds": 321,
                "api_key_protection": "windows-dpapi-current-user",
                "api_key_protected": base64.b64encode(b"encrypted").decode(),
            }
            config_path.write_text(json.dumps(original), encoding="utf-8")
            config_path.chmod(0o600)
            args = SimpleNamespace(
                config=config_path,
                base_url=None,
                model=None,
                output_dir=None,
                timeout=None,
            )
            failure = CredentialDecryptionError("unreadable")

            with (
                patch("image_client._unprotect_api_key", side_effect=failure),
                patch.object(configure_cli.sys.stdin, "isatty", return_value=True),
            ):
                with self.assertRaises(ConfigError):
                    configure_cli.configure(args, key_input_fn=lambda _prompt: "")
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)

            original_unprotect = image_client._unprotect_api_key

            def reject_only_old_credential(value: str) -> str:
                if value == original["api_key_protected"]:
                    raise failure
                return original_unprotect(value)

            with (
                patch(
                    "image_client._unprotect_api_key",
                    side_effect=reject_only_old_credential,
                ),
                patch.object(configure_cli.sys.stdin, "isatty", return_value=True),
            ):
                report = configure_cli.configure(
                    args,
                    key_input_fn=lambda _prompt: "replacement-secret",
                )

            configured = load_config(config_path, apply_env=False)
            self.assertEqual(configured.api_key, "replacement-secret")
            self.assertEqual(configured.base_url, "https://images.example.test/v1")
            self.assertEqual(configured.model, "preserved-model")
            self.assertEqual(configured.output_dir, "preserved-output")
            self.assertEqual(configured.timeout_seconds, 321)
            self.assertTrue(report["credential_replaced"])
            self.assertNotIn("replacement-secret", json.dumps(report))

    def test_interactive_configure_rejects_unknown_protection_scheme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            original = {
                "base_url": "https://images.example.test/v1",
                "api_key_protection": "unknown-protection",
                "api_key_protected": "opaque",
            }
            config_path.write_text(json.dumps(original), encoding="utf-8")
            config_path.chmod(0o600)
            args = SimpleNamespace(
                config=config_path,
                base_url=None,
                model=None,
                output_dir=None,
                timeout=None,
            )
            prompts: list[str] = []

            with patch.object(configure_cli.sys.stdin, "isatty", return_value=True):
                with self.assertRaises(ConfigError):
                    configure_cli.configure(
                        args,
                        key_input_fn=lambda prompt: prompts.append(prompt) or "replacement",
                    )

            self.assertEqual(prompts, [])
            self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), original)

    def test_environment_key_bypasses_unreadable_stored_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://images.example.test/v1",
                        "api_key_protection": "windows-dpapi-current-user",
                        "api_key_protected": "not-valid-base64",
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            with patch.dict(
                os.environ,
                {"SUB2API_IMAGE_API_KEY": "environment-secret"},
                clear=True,
            ):
                config = load_config(config_path)
            self.assertEqual(config.api_key, "environment-secret")

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_config_rejects_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_config(Config("https://example.test/v1", "secret-test-key"), config_path)
            config_path.chmod(0o644)
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_dedicated_environment_overrides_without_generic_openai_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with patch.dict(
                os.environ,
                {
                    "SUB2API_IMAGE_API_KEY": "secret-env-key",
                    "SUB2API_IMAGE_BASE_URL": "https://images.example.test",
                    "SUB2API_IMAGE_MODEL": "image-model-env",
                    "SUB2API_IMAGE_TIMEOUT_SECONDS": "42",
                    "SUB2API_IMAGE_PROVIDER_PROFILE": "sub2api-openai-oauth",
                    "OPENAI_API_KEY": "must-not-be-used",
                },
                clear=True,
            ):
                config = load_config(missing)
            self.assertEqual(config.api_key, "secret-env-key")
            self.assertEqual(config.base_url, "https://images.example.test/v1")
            self.assertEqual(config.model, "image-model-env")
            self.assertEqual(config.timeout_seconds, 42)
            self.assertEqual(config.provider_profile, "sub2api-openai-oauth")

            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "generic-only"},
                clear=True,
            ):
                with self.assertRaises(ConfigError):
                    load_config(missing)

    def test_legacy_config_without_profile_migrates_to_oauth_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "base_url": "https://images.example.test/v1",
                        "api_key": "legacy-secret",
                        "model": "gpt-image-2",
                    }
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            config = load_config(config_path, apply_env=False)
        self.assertEqual(config.provider_profile, "sub2api-openai-oauth")
        self.assertFalse(config.public_dict()["default_stream"])


class SizeAndFormatTests(unittest.TestCase):
    def test_pictures_known_folder_and_safe_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pictures = Path(directory) / "Redirected Pictures"
            self.assertEqual(
                pictures_directory(
                    platform_name="nt",
                    windows_resolver=lambda: pictures,
                ),
                pictures.resolve(),
            )
            output_dir, output_path = pictures_output(
                "healing_pixel_landscape_2k.png",
                directory=pictures,
            )
            self.assertIsNone(output_dir)
            self.assertEqual(
                output_path,
                pictures.resolve() / "healing_pixel_landscape_2k.png",
            )
            directory_only, generated_path = pictures_output("", directory=pictures)
            self.assertEqual(directory_only, pictures.resolve())
            self.assertIsNone(generated_path)
            for invalid in ("../escape.png", "folder/image.png", "folder\\image.png"):
                with self.subTest(filename=invalid), self.assertRaises(ConfigError):
                    pictures_output(invalid, directory=pictures)

    def test_oauth_profile_exposes_only_verified_presets(self) -> None:
        self.assertEqual(resolve_size("1K", "square"), ("1024x1024", "1K"))
        self.assertEqual(resolve_size("2K", "landscape"), ("1536x1024", "2K"))
        self.assertEqual(resolve_size("2K", "portrait"), ("1024x1536", "2K"))
        for tier, orientation in (("1K", "landscape"), ("2K", "square"), ("4K", "landscape")):
            with self.subTest(tier=tier, orientation=orientation), self.assertRaises(ConfigError):
                resolve_size(tier, orientation)
        self.assertEqual(resolve_size(exact_size="1536x1024"), ("1536x1024", "2K"))
        self.assertEqual(resolve_size(exact_size="auto"), ("auto", None))

    def test_size_constraints_and_nearest_suggestions(self) -> None:
        self.assertEqual(parse_size("1024x640"), (1024, 640))
        self.assertEqual(parse_size("2880x2880"), (2880, 2880))
        self.assertIsNone(parse_size("auto"))
        for invalid in (
            "1024x576",
            "3840x3840",
            "1000x1000",
            "3840x1024",
            "4096x2048",
        ):
            with self.subTest(size=invalid), self.assertRaises(ConfigError) as caught:
                parse_size(invalid)
            self.assertIn("Nearest legal suggestion", str(caught.exception))

    def test_png_jpeg_and_webp_dimensions(self) -> None:
        png = inspect_image(make_png(17, 23))
        jpeg = inspect_image(make_jpeg_header(31, 19))
        webp = inspect_image(make_webp_header(43, 29))
        self.assertEqual((png.format, png.width, png.height), ("png", 17, 23))
        self.assertEqual((jpeg.format, jpeg.width, jpeg.height), ("jpeg", 31, 19))
        self.assertEqual((webp.format, webp.width, webp.height), ("webp", 43, 29))
        with self.assertRaises(ImageValidationError):
            inspect_image(b'{"error":"not an image"}')


class ClientIntegrationTests(unittest.TestCase):
    def config(self, server: MockServer) -> Config:
        return Config(server.base_url, "secret-test-key", timeout_seconds=10)

    def test_generation_json_auth_and_atomic_save(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            report = generate_images(
                self.config(server),
                prompt="draw a blue square",
                output_dir=directory,
            )
            self.assertTrue(report["ok"])
            self.assertTrue(report["tier_match"])
            self.assertEqual(report["request_id"], "req-test-123")
            image_path = Path(report["images"][0]["path"])
            self.assertTrue(image_path.is_file())
            self.assertEqual(inspect_image(image_path.read_bytes()).width, 1024)

            captured = server.state.requests[0]
            self.assertEqual(len(server.state.requests), 1)
            self.assertEqual(captured["path"], "/v1/images/generations")
            headers = captured["headers"]
            self.assertEqual(headers["authorization"], "Bearer secret-test-key")
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["pragma"], "no-cache")
            request_payload = json.loads(captured["body"])
            self.assertEqual(request_payload["size"], "1024x1024")
            self.assertEqual(request_payload["response_format"], "b64_json")
            self.assertNotIn("secret-test-key", json.dumps(report))

    def test_delayed_generation_emits_heartbeats_and_sends_one_request(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            server.state.delay_seconds = 0.045
            stderr = io.StringIO()
            stdout = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                report = generate_images(
                    self.config(server),
                    prompt="slow image",
                    output_dir=directory,
                    timeout_seconds=180,
                    heartbeat_interval_seconds=0.01,
                )

            heartbeats = [json.loads(line) for line in stderr.getvalue().splitlines()]
            self.assertTrue(report["ok"])
            self.assertEqual(len(server.state.requests), 1)
            self.assertGreaterEqual(len(heartbeats), 2)
            self.assertEqual(heartbeats[0]["status"], "image_request_started")
            self.assertEqual(heartbeats[0]["elapsed_seconds"], 0)
            self.assertEqual(heartbeats[0]["remaining_seconds"], 180)
            self.assertTrue(
                all(
                    item["status"] == "image_request_pending"
                    for item in heartbeats[1:]
                )
            )
            self.assertNotIn(
                "image_request_deadline_reached",
                [item["status"] for item in heartbeats],
            )
            self.assertTrue(all(item["operation"] == "generate" for item in heartbeats))
            self.assertTrue(all(item["timeout_seconds"] == 180 for item in heartbeats))
            self.assertNotIn("slow image", stderr.getvalue())
            self.assertNotIn("secret-test-key", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")

    def test_timeout_is_ambiguous_failure_without_retry(self) -> None:
        config = Config(
            "https://images.example.test/v1",
            "secret-test-key",
            timeout_seconds=180,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", side_effect=socket.timeout("timed out")
        ) as request:
            with self.assertRaises(APIError) as caught:
                generate_images(
                    config,
                    prompt="timeout image",
                    output_dir=directory,
                    timeout_seconds=180,
                )

        error = caught.exception.as_dict()["error"]
        self.assertEqual(request.call_count, 1)
        self.assertEqual(error["category"], "network_timeout")
        self.assertEqual(error["billing_status"], "ambiguous")
        self.assertFalse(error["retry_safe"])

    def test_heartbeat_emits_started_immediately(self) -> None:
        output = io.StringIO()
        with patch("image_client.time.monotonic", return_value=100.0):
            with RequestHeartbeat(
                "generate", 180, interval_seconds=60, stream=output
            ):
                pass

        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["status"], "image_request_started")
        self.assertEqual(payloads[0]["elapsed_seconds"], 0)
        self.assertEqual(payloads[0]["remaining_seconds"], 180)
        self.assertEqual(payloads[0]["timeout_seconds"], 180)

    def test_heartbeat_reports_scaled_fifteen_second_checks(self) -> None:
        class ScriptedStop:
            def wait(self, _interval: float) -> bool:
                return False

        output = io.StringIO()
        heartbeat = RequestHeartbeat("generate", 180, stream=output)
        heartbeat._stop = ScriptedStop()  # type: ignore[assignment]
        heartbeat._started = 100.0
        with patch(
            "image_client.time.monotonic",
            side_effect=tuple(float(value) for value in range(115, 281, 15)),
        ):
            heartbeat._run()

        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [item["elapsed_seconds"] for item in payloads],
            list(range(15, 181, 15)),
        )
        self.assertEqual(
            [item["status"] for item in payloads],
            ["image_request_pending"] * 11
            + ["image_request_deadline_reached"],
        )
        self.assertEqual(
            [item["remaining_seconds"] for item in payloads],
            list(range(165, -1, -15)),
        )
        self.assertTrue(all(item["timeout_seconds"] == 180 for item in payloads))

    def test_skill_requires_same_session_waiting_and_explicit_timeout(self) -> None:
        skill = (REPO_ROOT / "skills" / "sub2api-image" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("exactly once with `--timeout 180`", skill)
        self.assertIn("retain the original command `session_id`", skill)
        self.assertIn("outer `cell_id`", skill)
        self.assertIn("Never reduce a command result to `output` alone", skill)
        self.assertIn("intervals of up to 15 seconds", skill)
        self.assertIn("Only an explicit client exit", skill)
        self.assertIn("empty output", skill)
        self.assertIn("Never start another client invocation", skill)

    def test_unverified_oauth_preset_fails_before_network(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                generate_images(
                    self.config(server),
                    prompt="unsupported preset",
                    tier="2K",
                    orientation="square",
                    output_dir=directory,
                )
            self.assertIn("no verified 2K square preset", str(caught.exception))
            self.assertEqual(server.state.requests, [])

    def test_data_url_and_4k_resolution_mismatch(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            server.state.response_mode = "data-url"
            report = generate_images(
                self.config(server),
                prompt="draw",
                exact_size="3840x2160",
                output_dir=directory,
            )
            self.assertFalse(report["ok"])
            self.assertEqual(report["requested_size"], "3840x2160")
            self.assertEqual(report["requested_tier"], "4K")
            self.assertFalse(report["tier_match"])
            self.assertFalse(report["orientation_match"])
            self.assertFalse(report["exact_size_match"])
            self.assertEqual(report["error"]["category"], "response_mismatch")
            self.assertEqual(report["images"][0]["actual_size"], "1024x1024")
            self.assertTrue(Path(report["images"][0]["path"]).exists())

    def test_auto_size_skips_dimension_claims(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            report = generate_images(
                self.config(server),
                prompt="draw",
                exact_size="auto",
                output_dir=directory,
            )
            self.assertTrue(report["ok"])
            self.assertIsNone(report["requested_tier"])
            self.assertIsNone(report["tier_match"])
            self.assertIsNone(report["orientation_match"])
            self.assertIsNone(report["exact_size_match"])

    def test_error_is_classified_and_secret_is_redacted(self) -> None:
        with MockServer() as server:
            server.state.error_status = 401
            with self.assertRaises(APIError) as caught:
                ImageClient(self.config(server)).generate({"prompt": "draw"})
            error = caught.exception.as_dict()["error"]
            self.assertEqual(error["category"], "authentication")
            self.assertNotIn("secret-test-key", error["message"])

            server.state.error_status = 524
            with self.assertRaises(APIError) as timeout:
                ImageClient(self.config(server)).generate({"prompt": "draw"})
            timeout_error = timeout.exception.as_dict()["error"]
            self.assertEqual(timeout_error["category"], "edge_timeout")
            self.assertFalse(timeout_error["retry_safe"])
            self.assertIn("direct base URL", timeout_error["action"])

    def test_edit_multipart_and_mask_validation(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            second_source = Path(directory) / "second.png"
            mask = Path(directory) / "mask.png"
            source.write_bytes(make_png(1024, 1024))
            second_source.write_bytes(make_png(1024, 1024))
            mask.write_bytes(make_png(1024, 1024))
            report = edit_image(
                self.config(server),
                image_path=[source, second_source],
                mask_path=mask,
                prompt="replace background",
                output_dir=Path(directory) / "out",
            )
            self.assertTrue(report["ok"])
            captured = server.state.requests[0]
            self.assertEqual(captured["path"], "/v1/images/edits")
            body = captured["body"]
            self.assertIn(b'name="image"; filename="source.png"', body)
            self.assertIn(b'name="image"; filename="second.png"', body)
            self.assertEqual(body.count(b'name="image"; filename='), 2)
            self.assertIn(b'name="mask"; filename="mask.png"', body)
            self.assertIn(b'name="prompt"', body)
            self.assertEqual(len(report["input_images"]), 2)

            mismatch = Path(directory) / "bad-mask.png"
            mismatch.write_bytes(make_png(512, 512))
            with self.assertRaises(ConfigError):
                edit_image(
                    self.config(server),
                    image_path=source,
                    mask_path=mismatch,
                    prompt="replace background",
                    output_dir=Path(directory) / "unused",
                )

            request_count = len(server.state.requests)
            with self.assertRaises(ConfigError):
                edit_image(
                    self.config(server),
                    image_path=source,
                    prompt="in-place replacement",
                    output_path=source,
                    overwrite=True,
                    dry_run=True,
                )
            with self.assertRaises(ConfigError):
                edit_image(
                    self.config(server),
                    image_path=source,
                    prompt="metadata collision",
                    metadata=source,
                    overwrite=True,
                    dry_run=True,
                )
            self.assertEqual(len(server.state.requests), request_count)

    def test_exact_output_multi_numbering_and_overwrite_protection(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            server.state.response_count = 2
            output = Path(directory) / "result.png"
            report = generate_images(
                self.config(server),
                prompt="two tiles",
                count=2,
                output_path=output,
            )
            self.assertTrue(report["ok"])
            paths = [Path(image["path"]) for image in report["images"]]
            self.assertEqual([path.name for path in paths], ["result-01.png", "result-02.png"])
            self.assertTrue(all(path.exists() for path in paths))

            request_count = len(server.state.requests)
            with self.assertRaises(ImageValidationError):
                generate_images(
                    self.config(server),
                    prompt="two tiles",
                    count=2,
                    output_path=output,
                )
            self.assertEqual(len(server.state.requests), request_count)

            replaced = generate_images(
                self.config(server),
                prompt="two replacement tiles",
                count=2,
                output_path=output,
                overwrite=True,
            )
            self.assertTrue(replaced["ok"])

            collision = Path(directory) / "collision.png"
            with self.assertRaises(ConfigError):
                generate_images(
                    self.config(server),
                    prompt="collision",
                    output_path=collision,
                    metadata=collision,
                    overwrite=True,
                    dry_run=True,
                )

    def test_count_and_format_mismatches_are_failures(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            report = generate_images(
                self.config(server),
                prompt="draw",
                count=2,
                output_format="webp",
                output_dir=directory,
            )
            self.assertFalse(report["ok"])
            self.assertFalse(report["count_match"])
            self.assertFalse(report["format_match"])
            self.assertEqual(report["error"]["category"], "response_mismatch")
            self.assertEqual(report["error"]["mismatches"], ["count", "format"])

    def test_options_metadata_and_signed_url_redaction(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "result.json"
            report = generate_images(
                self.config(server),
                prompt="transparent icon",
                background="transparent",
                moderation="low",
                timeout_seconds=33,
                output_dir=Path(directory) / "images",
                metadata=metadata,
            )
            self.assertTrue(report["ok"])
            self.assertEqual(report["timeout_seconds"], 33)
            self.assertEqual(report["metadata_path"], str(metadata.resolve()))

            request_payload = json.loads(server.state.requests[0]["body"])
            self.assertEqual(request_payload["background"], "transparent")
            self.assertEqual(request_payload["moderation"], "low")

            document = json.loads(metadata.read_text(encoding="utf-8"))
            serialized = json.dumps(document)
            self.assertEqual(document["request"]["prompt"], "transparent icon")
            encoded_image = base64.b64encode(server.state.image).decode()
            self.assertNotIn(encoded_image, serialized)
            self.assertNotIn("signed_result_url", serialized)
            self.assertNotIn("result_url", serialized)
            self.assertNotIn("do-not-store", serialized)
            self.assertNotIn("secret-test-key", serialized)
            self.assertEqual(document["api_metadata"]["usage"]["image_count"], 1)

    def test_prompt_file_dry_run_has_no_network_or_output(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            prompt_path = Path(directory) / "prompt.txt"
            output_path = Path(directory) / "never-created.png"
            save_config(self.config(server), config_path)
            prompt_path.write_text("A clean product photo", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "generate.py"),
                    "--prompt-file",
                    str(prompt_path),
                    "--size",
                    "auto",
                    "--output",
                    str(output_path),
                    "--dry-run",
                    "--config",
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["network_request_sent"])
            self.assertFalse(payload["files_written"])
            self.assertEqual(payload["request"]["prompt"], "A clean product photo")
            self.assertEqual(server.state.requests, [])
            self.assertFalse(output_path.exists())

    def test_pictures_cli_resolves_without_extra_probe_or_file_write(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            filename = f"sub2api-pictures-test-{Path(directory).name}.png"
            save_config(self.config(server), config_path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "generate.py"),
                    "--prompt",
                    "A soothing pixel-art landscape",
                    "--tier",
                    "2K",
                    "--orientation",
                    "landscape",
                    "--pictures",
                    filename,
                    "--dry-run",
                    "--config",
                    str(config_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["requested_size"], "1536x1024")
            self.assertEqual(payload["provider_profile"], "sub2api-openai-oauth")
            self.assertFalse(payload["stream_requested"])
            self.assertFalse(payload["network_request_sent"])
            self.assertFalse(payload["files_written"])
            self.assertEqual(server.state.requests, [])

    def test_output_compression_validation_and_dry_run(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            report = generate_images(
                self.config(server),
                prompt="compressed",
                output_path=Path(directory) / "result.webp",
                output_compression=75,
                dry_run=True,
            )
            self.assertEqual(report["output_format"], "webp")
            self.assertEqual(report["request"]["output_compression"], 75)
            self.assertEqual(server.state.requests, [])
            with self.assertRaises(ConfigError):
                generate_images(
                    self.config(server),
                    prompt="invalid",
                    output_format="png",
                    output_compression=75,
                    dry_run=True,
                )
            with self.assertRaises(ConfigError):
                generate_images(
                    self.config(server),
                    prompt="conflicting format",
                    output_path=Path(directory) / "result.webp",
                    output_format="png",
                    dry_run=True,
                )

    def test_smoke_test_cli(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            output_dir = Path(directory) / "images"
            save_config(self.config(server), config_path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "smoke_test.py"),
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["smoke_test"])
            self.assertIn("Not available", payload["billing_verification"])
            self.assertNotIn("secret-test-key", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
