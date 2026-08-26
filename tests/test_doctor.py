from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "sub2api-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from doctor import run_doctor  # noqa: E402
from image_client import Config, save_config  # noqa: E402


class FakeHealthResponse:
    status = 200
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": "req-models-test",
    }

    def __enter__(self) -> "FakeModelsResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _size: int = -1) -> bytes:
        return b'{"status":"ok"}'


class DoctorTests(unittest.TestCase):
    def test_local_doctor_is_secret_free_and_does_not_use_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            output_dir = root / "output"
            output_dir.mkdir()
            save_config(
                Config(
                    "https://images.example.test/v1",
                    "secret-test-key",
                    output_dir=str(output_dir),
                ),
                config_path,
            )
            with patch("image_client.urlopen") as request:
                report = run_doctor(config_path=config_path)

        self.assertTrue(report["ok"])
        self.assertTrue(report["runtime"]["ok"])
        self.assertTrue(report["configuration"]["credential_readable"])
        self.assertEqual(
            report["configuration"]["provider_profile"],
            "sub2api-openai-oauth",
        )
        self.assertTrue(report["configuration"]["default_stream"])
        self.assertEqual(report["configuration"]["max_images_per_request"], 1)
        self.assertTrue(report["output"]["parent_writable"])
        self.assertFalse(report["output"]["write_test_performed"])
        self.assertNotIn("network", report)
        self.assertEqual(request.call_count, 0)
        self.assertNotIn("secret-test-key", json.dumps(report))

    def test_network_doctor_uses_health_get_without_image_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            save_config(
                Config("https://images.example.test/v1", "secret-test-key"),
                config_path,
            )
            with patch(
                "image_client.urlopen", return_value=FakeHealthResponse()
            ) as request:
                report = run_doctor(
                    config_path=config_path,
                    output_dir=root,
                    network=True,
                )

        self.assertTrue(report["ok"])
        self.assertFalse(report["network"]["image_request_sent"])
        self.assertFalse(report["network"]["billing_expected"])
        self.assertFalse(report["network"]["authentication_checked"])
        self.assertTrue(report["network"]["client_request_id"].startswith("img-"))
        self.assertEqual(report["network"]["request_id"], "req-models-test")
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_method(), "GET")
        self.assertEqual(sent.full_url, "https://images.example.test/health")
        self.assertIsNone(sent.get_header("Authorization"))
        self.assertEqual(
            sent.get_header("X-client-request-id"),
            report["network"]["client_request_id"],
        )
        self.assertEqual(request.call_count, 1)

    def test_missing_config_prevents_network_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "image_client.urlopen"
        ) as request:
            report = run_doctor(
                config_path=Path(directory) / "missing.json",
                output_dir=directory,
                network=True,
            )

        self.assertFalse(report["ok"])
        self.assertFalse(report["configuration"]["ok"])
        self.assertFalse(report["network"]["ok"])
        self.assertEqual(request.call_count, 0)


if __name__ == "__main__":
    unittest.main()
