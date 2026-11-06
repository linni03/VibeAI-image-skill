from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "sub2api-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure as configure_cli  # noqa: E402
import image_client  # noqa: E402
from doctor import run_doctor  # noqa: E402
from edit import edit_image  # noqa: E402
from generate import generate_images  # noqa: E402
from image_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    FALLBACK_MODEL,
    Config,
    image_model_ids,
    migrated_base_url,
    parse_image_model_id,
    read_model_cache,
    resolve_model,
    save_config,
    select_latest_image_model,
    validate_model,
    write_model_cache,
)

_INSTALLER_NAME = "vibeai_image_installer"
if _INSTALLER_NAME in sys.modules:
    installer = sys.modules[_INSTALLER_NAME]
else:
    _spec = importlib.util.spec_from_file_location(_INSTALLER_NAME, REPO_ROOT / "install.py")
    if _spec is None or _spec.loader is None:
        raise RuntimeError("Cannot load install.py")
    installer = importlib.util.module_from_spec(_spec)
    sys.modules[_INSTALLER_NAME] = installer
    _spec.loader.exec_module(installer)


# Image models returned by the production gateway on 2026-11-05, plus noise.
LIVE_LISTING = [
    "gpt-5.5",
    "gpt-image-2",
    "gpt-image-2.5",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-flare-2026-09-08",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.6",
    "gpt-image-2.6-flare",
    "gpt-image-2.6-sunburst",
    "grok-imagine-image",
]
BASE_URL = "https://gateway.example.test/v1"
SECRET = "secret-test-key"


def make_png(width: int = 1024, height: int = 1024) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = (b"\x00" + b"\xff\xff\xff" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, level=9))
        + chunk(b"IEND", b"")
    )


class JSONResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json", "X-Request-ID": "req-models"}
        self.status = 200

    def __enter__(self) -> "JSONResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        data = self._body if size < 0 else self._body[:size]
        self._body = self._body[len(data):]
        return data


class FakeGateway:
    """Route urlopen calls: GET /models returns a listing, POST returns an image."""

    def __init__(self, models: list[str] | None = None, models_error: BaseException | None = None) -> None:
        self.models = LIVE_LISTING if models is None else models
        self.models_error = models_error
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None) -> JSONResponse:
        self.requests.append(request)
        if request.get_method() == "GET":
            if self.models_error is not None:
                raise self.models_error
            return JSONResponse({"object": "list", "data": [{"id": m} for m in self.models]})
        encoded = base64.b64encode(make_png()).decode()
        return JSONResponse({"created": 1, "data": [{"b64_json": encoded}]})

    def paid_requests(self) -> list[object]:
        return [item for item in self.requests if item.get_method() == "POST"]


def unauthorized() -> HTTPError:
    body = json.dumps({"error": {"type": "authentication_error", "message": f"bad key {SECRET}"}})
    return HTTPError(f"{BASE_URL}/models", 401, "Unauthorized", {}, io.BytesIO(body.encode()))


def clean_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("SUB2API_IMAGE_")}


class SelectionTests(unittest.TestCase):
    def test_live_listing_selects_newest_flare(self) -> None:
        self.assertEqual(select_latest_image_model(LIVE_LISTING), "gpt-image-2.6-flare")

    def test_versions_compare_numerically(self) -> None:
        models = ["gpt-image-2.9-flare", "gpt-image-2.10-flare", "gpt-image-3-flare"]
        self.assertEqual(select_latest_image_model(models[:2]), "gpt-image-2.10-flare")
        self.assertEqual(select_latest_image_model(models), "gpt-image-3-flare")
        self.assertEqual(parse_image_model_id("gpt-image-3.0-flare"), ((3,), "flare"))

    def test_sunburst_never_outranks_flare(self) -> None:
        self.assertEqual(
            select_latest_image_model(["gpt-image-2.5-flare", "gpt-image-2.7-sunburst"]),
            "gpt-image-2.5-flare",
        )

    def test_no_flare_model_returns_none(self) -> None:
        self.assertIsNone(select_latest_image_model(["gpt-image-2", "gpt-image-2.6-sunburst"]))
        self.assertIsNone(select_latest_image_model([]))

    def test_dated_snapshots_and_non_image_models_are_excluded(self) -> None:
        filtered = image_model_ids(LIVE_LISTING + [None, 7, "gpt-image-2.6-flare"])
        self.assertNotIn("gpt-image-2.5-flare-2026-09-08", filtered)
        self.assertNotIn("gpt-5.5", filtered)
        self.assertNotIn("grok-imagine-image", filtered)
        self.assertEqual(filtered.count("gpt-image-2.6-flare"), 1)
        self.assertIsNone(parse_image_model_id("gpt-image-2.6-flare\r\nX: y"))

    def test_auto_is_case_insensitive(self) -> None:
        self.assertEqual(validate_model(" AUTO "), "auto")
        self.assertEqual(image_client.DEFAULT_MODEL, "auto")
        self.assertEqual(Config(BASE_URL, SECRET).public_dict()["model_mode"], "auto")
        self.assertEqual(
            Config(BASE_URL, SECRET, model="gpt-image-2").public_dict()["model_mode"],
            "pinned",
        )


class ResolutionTests(unittest.TestCase):
    def test_pinned_sources_never_query_models(self) -> None:
        gateway = FakeGateway()
        with patch("image_client.urlopen", side_effect=gateway), patch.dict(
            os.environ, clean_env(), clear=True
        ):
            pinned = Config(BASE_URL, SECRET, model="gpt-image-2")
            self.assertEqual(
                resolve_model(pinned, "gpt-image-2.5-flare").report_fields(),
                {"model": "gpt-image-2.5-flare", "model_source": "cli"},
            )
            self.assertEqual(resolve_model(pinned).source, "config")
            with patch.dict(os.environ, {"SUB2API_IMAGE_MODEL": "gpt-image-2"}):
                self.assertEqual(resolve_model(pinned).source, "env")
        self.assertEqual(gateway.requests, [])

    def test_cli_auto_overrides_pinned_config(self) -> None:
        gateway = FakeGateway()
        with patch("image_client.urlopen", side_effect=gateway):
            resolution = resolve_model(Config(BASE_URL, SECRET, model="gpt-image-2"), "auto")
        self.assertEqual(resolution.model, "gpt-image-2.6-flare")
        self.assertEqual(resolution.source, "auto_discovered")

    def test_auto_discovers_once_and_writes_secret_free_cache(self) -> None:
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-cache.json"
            with patch("image_client.urlopen", side_effect=gateway):
                resolution = resolve_model(Config(BASE_URL, SECRET), cache_path=cache)
            stored = cache.read_text(encoding="utf-8")
            cached = read_model_cache(cache, BASE_URL)

        self.assertEqual(resolution.model, "gpt-image-2.6-flare")
        self.assertEqual(resolution.source, "auto_discovered")
        self.assertEqual(resolution.discovery["status"], "ok")
        self.assertTrue(resolution.discovery["cache_written"])
        self.assertNotIn(SECRET, stored)
        self.assertIn("gpt-image-2.6-flare", cached[0])
        self.assertEqual(len(gateway.requests), 1)
        sent = gateway.requests[0]
        self.assertEqual(sent.full_url, f"{BASE_URL}/models")
        self.assertEqual(sent.get_header("Authorization"), f"Bearer {SECRET}")

    def test_missing_cache_directory_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "absent" / "model-cache.json"
            self.assertFalse(write_model_cache(missing, BASE_URL, ["gpt-image-2.6-flare"]))
            self.assertFalse(missing.exists())


class FallbackTests(unittest.TestCase):
    def test_failed_discovery_uses_cache_then_builtin(self) -> None:
        failing = FakeGateway(models_error=URLError(ConnectionResetError("reset")))
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-cache.json"
            self.assertTrue(write_model_cache(cache, BASE_URL, ["gpt-image-2.6-flare"]))
            with patch("image_client.urlopen", side_effect=failing):
                cached = resolve_model(Config(BASE_URL, SECRET), cache_path=cache)
                builtin = resolve_model(Config(BASE_URL, SECRET), cache_path=None)

        self.assertEqual((cached.model, cached.source), ("gpt-image-2.6-flare", "auto_cached"))
        self.assertEqual(cached.discovery["status"], "failed")
        self.assertEqual(cached.discovery["error"]["category"], "connection_reset")
        self.assertEqual((builtin.model, builtin.source), (FALLBACK_MODEL, "auto_builtin"))
        self.assertEqual(builtin.discovery["reason"], "no_usable_listing_or_cache")

    def test_rejected_key_falls_back_without_leaking_it(self) -> None:
        with patch("image_client.urlopen", side_effect=FakeGateway(models_error=unauthorized())):
            resolution = resolve_model(Config(BASE_URL, SECRET))
        self.assertEqual(resolution.source, "auto_builtin")
        self.assertEqual(resolution.discovery["error"]["status"], 401)
        self.assertNotIn(SECRET, json.dumps(resolution.report_fields()))

    def test_live_listing_without_flare_does_not_trust_stale_cache(self) -> None:
        no_flare = FakeGateway(models=["gpt-image-2", "gpt-image-2.6-sunburst"])
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-cache.json"
            write_model_cache(cache, BASE_URL, ["gpt-image-2.6-flare"])
            with patch("image_client.urlopen", side_effect=no_flare):
                resolution = resolve_model(Config(BASE_URL, SECRET), cache_path=cache)
        self.assertEqual((resolution.model, resolution.source), (FALLBACK_MODEL, "auto_builtin"))
        self.assertEqual(resolution.discovery["reason"], "no_preferred_family_model_listed")

    def test_cache_for_other_base_url_or_corrupt_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-cache.json"
            write_model_cache(cache, "https://other.example.test/v1", ["gpt-image-2.6-flare"])
            self.assertIsNone(read_model_cache(cache, BASE_URL))
            cache.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_model_cache(cache, BASE_URL))
            offline = resolve_model(Config(BASE_URL, SECRET), allow_network=False, cache_path=cache)
        self.assertEqual((offline.model, offline.source), (FALLBACK_MODEL, "auto_builtin"))
        self.assertEqual(offline.discovery["status"], "skipped_offline")


class RequestIntegrationTests(unittest.TestCase):
    def test_generate_sends_one_paid_request_with_discovered_model(self) -> None:
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            with patch("image_client.urlopen", side_effect=gateway), redirect_stderr(io.StringIO()):
                report = generate_images(
                    Config(BASE_URL, SECRET),
                    prompt="a lighthouse",
                    output_dir=directory,
                    stream=False,
                    model_cache_path=Path(directory) / "model-cache.json",
                )
        self.assertTrue(report["ok"])
        self.assertEqual(report["model"], "gpt-image-2.6-flare")
        self.assertEqual(report["model_source"], "auto_discovered")
        self.assertEqual([item.get_method() for item in gateway.requests], ["GET", "POST"])
        paid = gateway.paid_requests()
        self.assertEqual(len(paid), 1)
        self.assertEqual(json.loads(paid[0].data)["model"], "gpt-image-2.6-flare")

    def test_generate_with_failed_discovery_still_sends_exactly_one_paid_request(self) -> None:
        gateway = FakeGateway(models_error=URLError(TimeoutError("timed out")))
        with tempfile.TemporaryDirectory() as directory:
            with patch("image_client.urlopen", side_effect=gateway), redirect_stderr(io.StringIO()):
                report = generate_images(
                    Config(BASE_URL, SECRET), prompt="a lighthouse", output_dir=directory, stream=False
                )
        self.assertEqual((report["model"], report["model_source"]), (FALLBACK_MODEL, "auto_builtin"))
        self.assertEqual(report["model_discovery"]["error"]["category"], "network_timeout")
        self.assertEqual(len(gateway.paid_requests()), 1)

    def test_dry_run_never_queries_models(self) -> None:
        gateway = FakeGateway()
        with patch("image_client.urlopen", side_effect=gateway):
            report = generate_images(Config(BASE_URL, SECRET), prompt="offline", dry_run=True)
        self.assertEqual(gateway.requests, [])
        self.assertFalse(report["network_request_sent"])
        self.assertEqual(report["request"]["model"], FALLBACK_MODEL)
        self.assertEqual(report["model_discovery"]["status"], "skipped_offline")

    def test_edit_sends_discovered_model_in_multipart_fields(self) -> None:
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            source.write_bytes(make_png())
            with patch("image_client.urlopen", side_effect=gateway), redirect_stderr(io.StringIO()):
                report = edit_image(
                    Config(BASE_URL, SECRET), image_path=source, prompt="snow", output_dir=directory
                )
        self.assertEqual(report["model"], "gpt-image-2.6-flare")
        self.assertEqual(len(gateway.paid_requests()), 1)
        self.assertIn(b'name="model"\r\n\r\ngpt-image-2.6-flare\r\n', gateway.paid_requests()[0].data)


class BaseUrlMigrationTests(unittest.TestCase):
    LEGACY = "https://images.vibeai.tech/v1"

    def test_only_the_retired_default_is_migrated(self) -> None:
        self.assertEqual(DEFAULT_BASE_URL, "https://vibeai.tech/v1")
        self.assertEqual(migrated_base_url(self.LEGACY), (DEFAULT_BASE_URL, True))
        self.assertEqual(migrated_base_url("https://images.vibeai.tech"), (DEFAULT_BASE_URL, True))
        self.assertEqual(migrated_base_url("https://my.example.test/v1"), ("https://my.example.test/v1", False))

    def test_installer_enter_moves_legacy_default_and_sets_auto(self) -> None:
        existing = installer.Config(self.LEGACY, "existing-secret", model="gpt-image-2.5-flare")
        prompts: list[str] = []
        config = installer.prompt_config(
            existing,
            input_fn=lambda prompt: prompts.append(prompt) or "",
            key_input_fn=lambda _prompt: "",
        )
        self.assertEqual(config.base_url, DEFAULT_BASE_URL)
        self.assertEqual(config.model, "auto")
        self.assertIn(f"[{DEFAULT_BASE_URL}]", prompts[0])
        typed = installer.prompt_config(
            existing, input_fn=lambda _prompt: self.LEGACY, key_input_fn=lambda _prompt: ""
        )
        self.assertEqual(typed.base_url, self.LEGACY)

    def test_installer_main_reports_url_and_model_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            config_path = codex_home / "sub2api-image" / "config.json"
            installer.install_skill(installer.SKILL_SOURCE, codex_home)
            save_config(Config(self.LEGACY, "existing-secret", model="gpt-image-2.5-flare"), config_path)
            args = SimpleNamespace(
                base_url=None, codex_home=codex_home, config=config_path, reconfigure=False,
                model=None, output_dir=None, timeout=None, provider_profile=None,
            )
            output = io.StringIO()
            with (
                patch.object(installer, "parse_args", return_value=args),
                patch.object(installer.sys.stdin, "isatty", return_value=True),
                patch("builtins.input", return_value=""),
                redirect_stdout(output),
            ):
                self.assertEqual(installer.main(), 0)
            updated = installer.load_config(config_path, apply_env=False)
        self.assertEqual((updated.base_url, updated.model), (DEFAULT_BASE_URL, "auto"))
        self.assertIn(f"Base URL 已更新：{self.LEGACY} -> {DEFAULT_BASE_URL}", output.getvalue())
        self.assertIn("模型配置已更新：gpt-image-2.5-flare -> auto", output.getvalue())


    def test_configure_migrates_legacy_default_and_keeps_pinned_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_config(Config(self.LEGACY, "existing-secret", model="gpt-image-2"), config_path)
            args = SimpleNamespace(config=config_path, base_url=None, model=None, output_dir=None, timeout=None)
            with patch.object(configure_cli.sys.stdin, "isatty", return_value=True), patch.dict(
                os.environ, clean_env(), clear=True
            ):
                report = configure_cli.configure(args, key_input_fn=lambda _prompt: "")
        self.assertEqual(report["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(report["base_url_migrated_from"], self.LEGACY)
        self.assertEqual((report["model"], report["model_mode"]), ("gpt-image-2", "pinned"))


class DoctorModelsTests(unittest.TestCase):
    def test_doctor_models_lists_and_previews_auto_selection_without_billing(self) -> None:
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_config(Config(BASE_URL, SECRET, output_dir=directory), config_path)
            with patch("image_client.urlopen", side_effect=gateway), patch.dict(
                os.environ, clean_env(), clear=True
            ):
                report = run_doctor(config_path=config_path, models=True)
            cache_created = (Path(directory) / "model-cache.json").exists()

        models = report["models"]
        self.assertTrue(report["ok"])
        self.assertEqual(models["auto_selection"], "gpt-image-2.6-flare")
        self.assertEqual(models["auto_selection_source"], "auto_discovered")
        self.assertIn("gpt-image-2.6-sunburst", models["image_models"])
        self.assertTrue(models["authentication_checked"])
        self.assertFalse(models["image_request_sent"])
        self.assertEqual(report["configuration"]["model_mode"], "auto")
        self.assertEqual(gateway.paid_requests(), [])
        self.assertFalse(cache_created)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_doctor_models_reports_rejected_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            save_config(Config(BASE_URL, SECRET, output_dir=directory), config_path)
            with patch("image_client.urlopen", side_effect=FakeGateway(models_error=unauthorized())), patch.dict(
                os.environ, clean_env(), clear=True
            ):
                report = run_doctor(config_path=config_path, models=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["models"]["error"]["category"], "authentication")
        self.assertNotIn(SECRET, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
