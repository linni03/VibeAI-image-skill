#!/usr/bin/env python3
"""Bounded incremental SSE parser for Sub2API image generation events."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


MAX_SSE_EVENT_BYTES = 144 * 1024 * 1024
MAX_SSE_STREAM_BYTES = 512 * 1024 * 1024


class SSEParseError(Exception):
    """An SSE response violated the expected image event protocol."""

    category = "stream_protocol"

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


class SSEEventError(SSEParseError):
    """The server emitted an explicit error event."""

    category = "stream_error"


@dataclass
class ImageStreamState:
    """Collected image events without transport- or credential-sensitive data."""

    data: list[dict[str, Any]] = field(default_factory=list)
    partial_data: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    done: bool = False
    event_count: int = 0
    completed_count: int = 0
    total_bytes: int = 0

    def response(self) -> dict[str, Any]:
        return {**self.metadata, "data": [dict(item) for item in self.data]}

    def partial_response(self) -> dict[str, Any]:
        return {"data": [dict(item) for item in self.partial_data]}


class SSEImageParser:
    """Parse arbitrary byte chunks according to the Server-Sent Events grammar."""

    def __init__(
        self,
        *,
        max_event_bytes: int = MAX_SSE_EVENT_BYTES,
        max_stream_bytes: int = MAX_SSE_STREAM_BYTES,
    ) -> None:
        if max_event_bytes <= 0 or max_stream_bytes <= 0:
            raise ValueError("SSE size limits must be positive")
        if max_event_bytes > max_stream_bytes:
            raise ValueError("SSE event limit must not exceed stream limit")
        self.state = ImageStreamState()
        self._max_event_bytes = max_event_bytes
        self._max_stream_bytes = max_stream_bytes
        self._line_buffer = bytearray()
        self._event_name = ""
        self._data_lines: list[str] = []
        self._event_bytes = 0

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("SSE chunks must be bytes")
        if not chunk:
            return
        self.state.total_bytes += len(chunk)
        if self.state.total_bytes > self._max_stream_bytes:
            raise SSEParseError("SSE response exceeded the safe aggregate size limit")
        search_from = len(self._line_buffer)
        self._line_buffer.extend(chunk)

        while True:
            newline = self._line_buffer.find(b"\n", search_from)
            if newline < 0:
                break
            raw_line = bytes(self._line_buffer[:newline])
            del self._line_buffer[: newline + 1]
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            self._process_line(raw_line)
            search_from = 0
        if len(self._line_buffer) + self._event_bytes > self._max_event_bytes:
            raise SSEParseError("SSE event exceeded the safe size limit")

    def finish(self) -> ImageStreamState:
        if self._line_buffer:
            raw_line = bytes(self._line_buffer)
            self._line_buffer.clear()
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            self._process_line(raw_line)
        self._dispatch_event()
        return self.state

    def _process_line(self, raw_line: bytes) -> None:
        if not raw_line:
            self._dispatch_event()
            return

        self._event_bytes += len(raw_line) + 1
        if self._event_bytes > self._max_event_bytes:
            raise SSEParseError("SSE event exceeded the safe size limit")
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SSEParseError("SSE response was not valid UTF-8") from exc
        if line.startswith(":"):
            return

        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            self._event_name = value
        elif field_name == "data":
            self._data_lines.append(value)

    def _dispatch_event(self) -> None:
        if not self._data_lines:
            self._reset_event()
            return
        raw_data = "\n".join(self._data_lines)
        event_name = self._event_name
        self._reset_event()
        if raw_data.strip() == "[DONE]":
            self.state.done = True
            return
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise SSEParseError("SSE data event contained malformed JSON") from exc
        if not isinstance(payload, Mapping):
            raise SSEParseError("SSE data event must contain a JSON object")

        self.state.event_count += 1
        self._extract_event(dict(payload), event_name)

    def _reset_event(self) -> None:
        self._event_name = ""
        self._data_lines = []
        self._event_bytes = 0

    def _extract_event(self, payload: dict[str, Any], event_name: str) -> None:
        event_type = str(payload.get("type") or event_name)
        if event_type == "error" or payload.get("error") is not None:
            message, error_type = _safe_event_error(payload)
            raise SSEEventError(message, error_type=error_type)

        if event_type in {
            "image_generation.partial_image",
            "image_edit.partial_image",
            "response.image_generation_call.partial_image",
        }:
            encoded = payload.get("b64_json")
            if encoded is None:
                encoded = payload.get("partial_image_b64")
            if not isinstance(encoded, str) or not encoded:
                raise SSEParseError("Partial image event did not contain base64 image data")
            item: dict[str, Any] = {"b64_json": encoded}
            index = payload.get("partial_image_index")
            if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
                item["partial_image_index"] = index
            self.state.partial_data.append(item)
            return

        if event_type in {"image_generation.completed", "image_edit.completed"}:
            encoded = payload.get("b64_json")
            if not isinstance(encoded, str) or not encoded:
                raise SSEParseError("Completed image event did not contain base64 image data")
            self.state.data.append({"b64_json": encoded})
            self.state.completed_count += 1
            self._copy_metadata(payload)
            return

        data = payload.get("data")
        if isinstance(data, list) and data:
            if not all(isinstance(item, Mapping) for item in data):
                raise SSEParseError("SSE image data must contain only JSON objects")
            self.state.data.extend(dict(item) for item in data)
            self.state.completed_count += len(data)
            self._copy_metadata(payload)

    def _copy_metadata(self, payload: Mapping[str, Any]) -> None:
        for name in ("model", "size", "output_format", "quality", "background", "created"):
            value = payload.get(name)
            if isinstance(value, (str, int, float, bool)):
                self.state.metadata[name] = value
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.state.metadata["usage"] = dict(usage)


def _safe_event_error(payload: Mapping[str, Any]) -> tuple[str, str | None]:
    error = payload.get("error", payload)
    message = "Sub2API returned an SSE error event"
    error_type: str | None = None
    if isinstance(error, Mapping):
        candidate = error.get("message") or error.get("detail")
        if isinstance(candidate, str) and candidate.strip():
            message = candidate.strip()
        candidate_type = error.get("type") or error.get("code")
        if isinstance(candidate_type, (str, int)):
            error_type = str(candidate_type)
    elif isinstance(error, str) and error.strip():
        message = error.strip()
    return message, error_type


def parse_sse_chunks(
    chunks: Iterable[bytes],
    *,
    max_event_bytes: int = MAX_SSE_EVENT_BYTES,
    max_stream_bytes: int = MAX_SSE_STREAM_BYTES,
) -> ImageStreamState:
    parser = SSEImageParser(
        max_event_bytes=max_event_bytes,
        max_stream_bytes=max_stream_bytes,
    )
    for chunk in chunks:
        parser.feed(chunk)
    return parser.finish()
