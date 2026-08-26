from __future__ import annotations

import base64
import json
import ssl
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "sub2api-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from edit import edit_image  # noqa: E402
from generate import generate_images  # noqa: E402
from image_client import APIError, Config, StreamInterruptedError  # noqa: E402
from image_stream import SSEEventError, SSEImageParser, SSEParseError  # noqa: E402


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_png(width: int = 1024, height: int = 1024) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = (b"\x00" + b"\xff\xff\xff" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(rows, level=9))
        + _png_chunk(b"IEND", b"")
    )


def sse_event(event_type: str, **fields: object) -> bytes:
    payload = json.dumps({"type": event_type, **fields}, separators=(",", ":"))
    return f"event: {event_type}\r\ndata: {payload}\r\n\r\n".encode()


class FakeResponse:
    def __init__(self, content_type: str, actions: list[bytes | BaseException]) -> None:
        self.headers = {
            "Content-Type": content_type,
            "X-Request-ID": "req-stream-test",
            "X-Client-Request-ID": "server-stream-test",
        }
        self.status = 200
        self._actions = list(actions)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _size: int = -1) -> bytes:
        if not self._actions:
            return b""
        action = self._actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class SSEParserTests(unittest.TestCase):
    def test_chunk_boundaries_crlf_keepalive_multiline_and_done(self) -> None:
        encoded = base64.b64encode(b"partial").decode()
        text = (
            b": keepalive\r\n\r\n"
            b"event: image_generation.partial_image\r\n"
            b"data: {\"type\":\"image_generation.partial_image\",\r\n"
            + f'data: "b64_json":"{encoded}","partial_image_index":0}}\r\n\r\n'.encode()
            + sse_event("image_generation.completed", b64_json=encoded)
            + b"data: [DONE]\r\n\r\n"
        )
        parser = SSEImageParser()
        for offset in range(0, len(text), 7):
            parser.feed(text[offset : offset + 7])
        state = parser.finish()

        self.assertTrue(state.done)
        self.assertEqual(state.event_count, 2)
        self.assertEqual(state.partial_data[0]["partial_image_index"], 0)
        self.assertEqual(state.data, [{"b64_json": encoded}])

    def test_edit_events_are_supported_without_done_marker(self) -> None:
        encoded = base64.b64encode(b"edit").decode()
        state = SSEImageParser()
        state.feed(sse_event("image_edit.completed", b64_json=encoded))
        parsed = state.finish()

        self.assertFalse(parsed.done)
        self.assertEqual(parsed.completed_count, 1)
        self.assertEqual(parsed.data, [{"b64_json": encoded}])

    def test_malformed_error_and_size_limits_are_not_ignored(self) -> None:
        parser = SSEImageParser()
        with self.assertRaises(SSEParseError):
            parser.feed(b"data: {not-json}\n\n")

        parser = SSEImageParser()
        with self.assertRaises(SSEEventError):
            parser.feed(b'data: {"type":"error","error":{"message":"failed"}}\n\n')

        parser = SSEImageParser(max_event_bytes=16, max_stream_bytes=32)
        with self.assertRaises(SSEParseError):
            parser.feed(b"data: 12345678901234567890")

        parser = SSEImageParser(max_event_bytes=16, max_stream_bytes=20)
        parser.feed(b": a\n\n")
        with self.assertRaises(SSEParseError):
            parser.feed(b": 1234567890123456\n\n")


class StreamingGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config("https://images.example.test/v1", "secret-test-key")
        self.encoded = base64.b64encode(make_png()).decode()

    def test_sse_generation_saves_final_image_with_one_request(self) -> None:
        response = FakeResponse(
            "text/event-stream; charset=utf-8",
            [
                sse_event("image_generation.partial_image", b64_json=self.encoded),
                sse_event("image_generation.completed", b64_json=self.encoded),
                b"data: [DONE]\n\n",
            ],
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            report = generate_images(
                self.config,
                prompt="streamed image",
                output_path=Path(directory) / "result.png",
                stream=True,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["response_mode"], "sse")
        self.assertTrue(report["stream"]["completion_marker_received"])
        self.assertEqual(request.call_count, 1)
        sent = json.loads(request.call_args.args[0].data)
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["partial_images"], 0)
        self.assertTrue(report["client_request_id"].startswith("img-"))
        self.assertEqual(
            request.call_args.args[0].get_header("X-client-request-id"),
            report["client_request_id"],
        )
        self.assertEqual(
            request.call_args.args[0].get_header("X-request-id"),
            report["client_request_id"],
        )
        self.assertEqual(report["server_client_request_id"], "server-stream-test")
        self.assertIn(
            "text/event-stream", request.call_args.args[0].get_header("Accept")
        )

    def test_json_fallback_uses_the_same_request(self) -> None:
        payload = json.dumps({"data": [{"b64_json": self.encoded}]}).encode()
        response = FakeResponse("application/json", [payload])
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            report = generate_images(
                self.config,
                prompt="fallback image",
                output_dir=directory,
                stream=True,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["response_mode"], "json")
        self.assertEqual(request.call_count, 1)

    def test_success_status_json_error_is_not_reported_as_missing_image(self) -> None:
        payload = json.dumps(
            {"error": {"type": "upstream_error", "message": "upstream failed"}}
        ).encode()
        response = FakeResponse("application/json", [payload])
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            with self.assertRaises(APIError) as caught:
                generate_images(
                    self.config,
                    prompt="error fallback",
                    output_dir=directory,
                    stream=True,
                )

        error = caught.exception.as_dict()["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertEqual(error["message"], "upstream failed")
        self.assertTrue(error["client_request_id"].startswith("img-"))
        self.assertEqual(request.call_count, 1)

    def test_partial_then_ssl_eof_saves_diagnostic_not_final(self) -> None:
        response = FakeResponse(
            "text/event-stream",
            [
                sse_event("image_generation.partial_image", b64_json=self.encoded),
                ssl.SSLError(ssl.SSL_ERROR_EOF, "UNEXPECTED_EOF_WHILE_READING"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            output = Path(directory) / "result.png"
            with self.assertRaises(StreamInterruptedError) as caught:
                generate_images(
                    self.config, prompt="interrupted", output_path=output, stream=True
                )

            error = caught.exception.as_dict()["error"]
            self.assertEqual(error["category"], "tls_unexpected_eof")
            self.assertEqual(error["billing_status"], "ambiguous")
            self.assertFalse(error["retry_safe"])
            self.assertEqual(error["final_images_saved"], 0)
            self.assertFalse(error["partial_images_are_final"])
            partial = Path(error["partial_images"][0]["path"])
            self.assertIn("partial", partial.name)
            self.assertTrue(partial.is_file())
            self.assertFalse(output.exists())
            self.assertEqual(request.call_count, 1)

    def test_completed_then_ssl_eof_keeps_final_with_warning(self) -> None:
        response = FakeResponse(
            "text/event-stream",
            [
                sse_event("image_generation.completed", b64_json=self.encoded),
                ssl.SSLError(ssl.SSL_ERROR_EOF, "UNEXPECTED_EOF_WHILE_READING"),
            ],
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            output = Path(directory) / "result.png"
            report = generate_images(
                self.config, prompt="completed", output_path=output, stream=True
            )

            self.assertTrue(output.is_file())

        self.assertTrue(report["ok"])
        self.assertEqual(report["transport_warning"]["category"], "tls_unexpected_eof")
        self.assertTrue(report["transport_warning"]["final_image_received"])
        self.assertEqual(request.call_count, 1)

    def test_completed_then_clean_eof_is_success_without_done_marker(self) -> None:
        response = FakeResponse(
            "text/event-stream",
            [sse_event("image_generation.completed", b64_json=self.encoded)],
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ):
            report = generate_images(
                self.config,
                prompt="completed cleanly",
                output_path=Path(directory) / "result.png",
                stream=True,
            )

        self.assertTrue(report["ok"])
        self.assertTrue(report["stream"]["terminal_event_received"])
        self.assertFalse(report["stream"]["done_marker_received"])
        self.assertNotIn("transport_warning", report)

    def test_edit_stream_accepts_edit_completed_without_done_marker(self) -> None:
        response = FakeResponse(
            "text/event-stream",
            [sse_event("image_edit.completed", b64_json=self.encoded)],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            source.write_bytes(make_png())
            with patch("image_client.urlopen", return_value=response) as request:
                report = edit_image(
                    self.config,
                    image_path=source,
                    prompt="edit streamed image",
                    output_path=root / "edited.png",
                    stream=True,
                )

        self.assertTrue(report["ok"])
        self.assertEqual(report["response_mode"], "sse")
        self.assertTrue(report["stream"]["terminal_event_received"])
        self.assertFalse(report["stream"]["done_marker_received"])
        sent = request.call_args.args[0]
        self.assertIn("text/event-stream", sent.get_header("Accept"))
        self.assertIn(b'name="stream"', sent.data)
        self.assertIn(b"true", sent.data)
        self.assertIn(b'name="partial_images"', sent.data)

    def test_partial_eof_error_event_and_invalid_base64_fail_cleanly(self) -> None:
        cases = (
            (
                [sse_event("image_generation.partial_image", b64_json=self.encoded)],
                "incomplete_response",
                True,
            ),
            (
                [
                    sse_event("image_generation.partial_image", b64_json=self.encoded),
                    b'data: {"type":"error","error":{"message":"upstream stopped"}}\n\n',
                ],
                "stream_error",
                True,
            ),
            (
                [
                    sse_event("image_generation.partial_image", b64_json="not-base64"),
                    b"data: [DONE]\n\n",
                ],
                "stream_image_validation",
                False,
            ),
        )
        for actions, category, has_partial in cases:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                response = FakeResponse("text/event-stream", list(actions))
                with patch("image_client.urlopen", return_value=response):
                    with self.assertRaises(StreamInterruptedError) as caught:
                        generate_images(
                            self.config,
                            prompt="failure",
                            output_dir=directory,
                            stream=True,
                        )
                error = caught.exception.as_dict()["error"]
                self.assertEqual(error["category"], category)
                self.assertEqual(bool(error.get("partial_images")), has_partial)

    def test_no_stream_omits_stream_fields(self) -> None:
        payload = json.dumps({"data": [{"b64_json": self.encoded}]}).encode()
        response = FakeResponse("application/json", [payload])
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            report = generate_images(
                self.config,
                prompt="plain json",
                output_dir=directory,
                stream=False,
            )
        sent = json.loads(request.call_args.args[0].data)
        self.assertNotIn("stream", sent)
        self.assertNotIn("partial_images", sent)
        self.assertFalse(report["stream_requested"])

    def test_oauth_profile_defaults_to_zero_preview_stream(self) -> None:
        payload = json.dumps({"data": [{"b64_json": self.encoded}]}).encode()
        response = FakeResponse("application/json", [payload])
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen", return_value=response
        ) as request:
            report = generate_images(
                self.config, prompt="OAuth default", output_dir=directory
            )
        sent = json.loads(request.call_args.args[0].data)
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["partial_images"], 0)
        self.assertTrue(report["stream_requested"])
        self.assertEqual(report["provider_profile"], "sub2api-openai-oauth")


if __name__ == "__main__":
    unittest.main()
