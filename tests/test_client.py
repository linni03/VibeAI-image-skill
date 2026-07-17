from __future__ import annotations

import base64
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "sub2api-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from edit import edit_image  # noqa: E402
from generate import generate_images  # noqa: E402
from image_client import (  # noqa: E402
    APIError,
    Config,
    ConfigError,
    ImageClient,
    ImageValidationError,
    inspect_image,
    load_config,
    normalize_base_url,
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
        self.error_status: int | None = None


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
                "data": [item],
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
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
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

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_config_rejects_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_config(Config("https://example.test/v1", "secret-test-key"), config_path)
            config_path.chmod(0o644)
            with self.assertRaises(ConfigError):
                load_config(config_path)


class SizeAndFormatTests(unittest.TestCase):
    def test_size_presets_match_billing_tiers(self) -> None:
        self.assertEqual(resolve_size("1K", "landscape"), ("1024x576", "1K"))
        self.assertEqual(resolve_size("2K", "portrait"), ("1152x2048", "2K"))
        self.assertEqual(resolve_size("4K", "landscape"), ("3840x2160", "4K"))
        self.assertEqual(resolve_size(exact_size="1536x1024"), ("1536x1024", "2K"))

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
            self.assertEqual(captured["path"], "/v1/images/generations")
            headers = captured["headers"]
            self.assertEqual(headers["authorization"], "Bearer secret-test-key")
            request_payload = json.loads(captured["body"])
            self.assertEqual(request_payload["size"], "1024x1024")
            self.assertEqual(request_payload["response_format"], "b64_json")
            self.assertNotIn("secret-test-key", json.dumps(report))

    def test_data_url_and_resolution_mismatch(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            server.state.response_mode = "data-url"
            report = generate_images(
                self.config(server),
                prompt="draw",
                exact_size="2048x2048",
                output_dir=directory,
            )
            self.assertFalse(report["ok"])
            self.assertFalse(report["tier_match"])
            self.assertFalse(report["exact_size_match"])
            self.assertEqual(report["error"]["category"], "resolution_mismatch")
            self.assertTrue(Path(report["images"][0]["path"]).exists())

    def test_error_is_classified_and_secret_is_redacted(self) -> None:
        with MockServer() as server:
            server.state.error_status = 401
            with self.assertRaises(APIError) as caught:
                ImageClient(self.config(server)).generate({"prompt": "draw"})
            error = caught.exception.as_dict()["error"]
            self.assertEqual(error["category"], "authentication")
            self.assertNotIn("secret-test-key", error["message"])

    def test_edit_multipart_and_mask_validation(self) -> None:
        with MockServer() as server, tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            mask = Path(directory) / "mask.png"
            source.write_bytes(make_png(1024, 1024))
            mask.write_bytes(make_png(1024, 1024))
            report = edit_image(
                self.config(server),
                image_path=source,
                mask_path=mask,
                prompt="replace background",
                output_dir=Path(directory) / "out",
            )
            self.assertTrue(report["ok"])
            captured = server.state.requests[0]
            self.assertEqual(captured["path"], "/v1/images/edits")
            body = captured["body"]
            self.assertIn(b'name="image"; filename="source.png"', body)
            self.assertIn(b'name="mask"; filename="mask.png"', body)
            self.assertIn(b'name="prompt"', body)

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
