from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vibeai_image_installer", REPO_ROOT / "install.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load install.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

from image_client import CredentialDecryptionError  # noqa: E402


class InstallerTests(unittest.TestCase):
    def test_platform_launchers_keep_one_step_update_entrypoint(self) -> None:
        shell_launcher = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        batch_launcher = (REPO_ROOT / "install.bat").read_text(encoding="utf-8")

        self.assertIn('"$script_dir/install.py" "$@"', shell_launcher)
        self.assertIn('"%~dp0install.py" %*', batch_launcher)

    def test_fresh_install_and_update_replace_the_skill_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            first = installer.install_skill(installer.SKILL_SOURCE, codex_home)
            target = codex_home / "skills" / installer.SKILL_NAME

            self.assertEqual(first.target, target)
            self.assertFalse(first.updated)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertFalse((target / "scripts" / "__pycache__").exists())
            runtime = json.loads((target / ".runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["skill_version"], installer.SKILL_VERSION)

            config_dir = codex_home / "sub2api-image"
            config_dir.mkdir()
            preserved_config = config_dir / "config.json"
            preserved_config.write_text("user-owned-config", encoding="utf-8")

            stale = target / "stale-file.txt"
            stale.write_text("old installation", encoding="utf-8")
            second = installer.install_skill(installer.SKILL_SOURCE, codex_home)

            self.assertTrue(second.updated)
            self.assertIsNone(second.backup_path)
            self.assertFalse(stale.exists())
            self.assertTrue((target / "scripts" / "generate.py").is_file())
            self.assertTrue((target / "scripts" / "image_stream.py").is_file())
            self.assertTrue((target / "scripts" / "doctor.py").is_file())
            self.assertEqual(
                preserved_config.read_text(encoding="utf-8"),
                "user-owned-config",
            )

    def test_prompt_config_uses_defaults_and_saves_private_file(self) -> None:
        key_prompts: list[str] = []

        def enter_visible_key(prompt: str) -> str:
            key_prompts.append(prompt)
            return "sk-image-test"

        config = installer.prompt_config(
            None,
            input_fn=lambda _prompt: "",
            key_input_fn=enter_visible_key,
        )
        self.assertEqual(config.base_url, installer.DEFAULT_BASE_URL)
        self.assertEqual(config.model, installer.DEFAULT_MODEL)
        self.assertEqual(config.provider_profile, installer.DEFAULT_PROVIDER_PROFILE)
        self.assertIn("输入可见", key_prompts[0])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "config.json"
            written = installer.save_config(config, path)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)
            else:
                stored = json.loads(written.read_text(encoding="utf-8"))
                self.assertNotIn("api_key", stored)
                self.assertEqual(
                    stored["api_key_protection"], "windows-dpapi-local-machine"
                )
            self.assertNotIn("sk-image-test", config.public_dict()["api_key"])

    def test_prompt_config_preserves_existing_values_on_enter(self) -> None:
        existing = installer.Config(
            base_url="https://images.example.test/v1",
            api_key="existing-secret",
            model="existing-model",
            output_dir="existing-output",
            timeout_seconds=321,
            provider_profile=installer.DEFAULT_PROVIDER_PROFILE,
        )
        config = installer.prompt_config(
            existing,
            input_fn=lambda _prompt: "",
            key_input_fn=lambda _prompt: "",
        )
        self.assertEqual(config, existing)

    def test_prompted_update_preserves_key_and_migrates_legacy_timeout(self) -> None:
        existing = installer.ConfigState(
            base_url="https://images.example.test/v1",
            api_key="existing-secret",
            model="existing-model",
            output_dir="existing-output",
            timeout_seconds=180,
            provider_profile=installer.DEFAULT_PROVIDER_PROFILE,
        )

        prompts: list[str] = []
        config = installer.prompt_config(
            existing,
            model="updated-model",
            input_fn=lambda prompt: prompts.append(prompt) or "",
            key_input_fn=lambda prompt: prompts.append(prompt) or "",
        )

        self.assertEqual(config.api_key, "existing-secret")
        self.assertEqual(config.base_url, existing.base_url)
        self.assertEqual(config.model, "updated-model")
        self.assertEqual(config.output_dir, existing.output_dir)
        self.assertEqual(config.timeout_seconds, 600)
        self.assertIn(existing.base_url, prompts[0])
        self.assertIn("直接回车保留现有密钥", prompts[1])

        explicitly_configured = installer.ConfigState(
            base_url=existing.base_url,
            api_key=existing.api_key,
            model=existing.model,
            output_dir=existing.output_dir,
            timeout_seconds=180,
            provider_profile=existing.provider_profile,
            defaults_version=installer.SKILL_VERSION,
        )
        self.assertEqual(
            installer.prompt_config(
                explicitly_configured,
                input_fn=lambda _prompt: "",
                key_input_fn=lambda _prompt: "",
            ).timeout_seconds,
            180,
        )

    def test_prompted_update_can_replace_base_url_and_key(self) -> None:
        existing = installer.Config(
            base_url="https://old.example.test/v1",
            api_key="existing-secret",
        )
        config = installer.prompt_config(
            existing,
            input_fn=lambda _prompt: "https://new.example.test/v1",
            key_input_fn=lambda _prompt: "replacement-secret",
        )

        self.assertEqual(config.base_url, "https://new.example.test/v1")
        self.assertEqual(config.api_key, "replacement-secret")

    def test_update_completion_message_and_key_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            config_path = codex_home / "sub2api-image" / "config.json"
            installer.install_skill(installer.SKILL_SOURCE, codex_home)
            installer.save_config(
                installer.Config(
                    "https://images.example.test/v1",
                    "existing-secret",
                    timeout_seconds=180,
                ),
                config_path,
            )
            legacy_payload = json.loads(config_path.read_text(encoding="utf-8"))
            legacy_payload.pop("defaults_version", None)
            legacy_payload.pop("schema_version", None)
            config_path.write_text(
                json.dumps(legacy_payload, indent=2) + "\n", encoding="utf-8"
            )
            if os.name == "posix":
                config_path.chmod(0o600)
            args = SimpleNamespace(
                base_url=None,
                codex_home=codex_home,
                config=config_path,
                reconfigure=False,
                model=None,
                output_dir=None,
                timeout=None,
                provider_profile=None,
            )
            output = io.StringIO()
            prompts: list[str] = []

            def press_enter(prompt: str) -> str:
                prompts.append(prompt)
                return ""

            with (
                patch.object(installer, "parse_args", return_value=args),
                patch.object(installer.sys.stdin, "isatty", return_value=True),
                patch("builtins.input", side_effect=press_enter),
                redirect_stdout(output),
            ):
                result = installer.main()

            updated = installer.load_config(config_path, apply_env=False)

        self.assertEqual(result, 0)
        self.assertEqual(updated.api_key, "existing-secret")
        self.assertEqual(updated.timeout_seconds, 600)
        self.assertIn("[OK] 更新完成", output.getvalue())
        self.assertIn("现有 API Key 已保留", output.getvalue())
        self.assertIn("180 秒迁移为 600 秒", output.getvalue())
        self.assertEqual(len(prompts), 2)
        self.assertIn("https://images.example.test/v1", prompts[0])
        self.assertIn("直接回车保留现有密钥", prompts[1])

    def test_prompt_config_requires_replacement_for_unreadable_key(self) -> None:
        failure = CredentialDecryptionError("unreadable")
        existing = installer.ConfigState(
            base_url="https://images.example.test/v1",
            api_key=None,
            model="existing-model",
            output_dir="existing-output",
            timeout_seconds=321,
            provider_profile=installer.DEFAULT_PROVIDER_PROFILE,
            credential_protection="windows-dpapi-current-user",
            credential_error=failure,
        )

        with self.assertRaises(installer.ConfigError):
            installer.prompt_config(
                existing,
                input_fn=lambda _prompt: "",
                key_input_fn=lambda _prompt: "",
            )

        config = installer.prompt_config(
            existing,
            input_fn=lambda _prompt: "",
            key_input_fn=lambda _prompt: "replacement-secret",
        )
        self.assertEqual(config.base_url, existing.base_url)
        self.assertEqual(config.api_key, "replacement-secret")
        self.assertEqual(config.model, existing.model)
        self.assertEqual(config.output_dir, existing.output_dir)
        self.assertEqual(config.timeout_seconds, existing.timeout_seconds)
        self.assertEqual(config.provider_profile, existing.provider_profile)

    def test_validate_skill_source_requires_implicit_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sub2api-image"
            (source / "agents").mkdir(parents=True)
            (source / "references").mkdir()
            (source / "scripts").mkdir()
            (source / "SKILL.md").write_text(
                "---\nname: sub2api-image\ndescription: Test\n---\n",
                encoding="utf-8",
            )
            (source / "agents" / "openai.yaml").write_text(
                "policy:\n  allow_implicit_invocation: false\n",
                encoding="utf-8",
            )
            for name in (
                "configure.py",
                "generate.py",
                "edit.py",
                "image_client.py",
                "image_stream.py",
                "doctor.py",
                "smoke_test.py",
            ):
                (source / "scripts" / name).write_text("", encoding="utf-8")
            for name in (
                "model-capabilities.md",
                "sub2api-api.md",
                "transport-and-billing.md",
            ):
                (source / "references" / name).write_text("", encoding="utf-8")

            with self.assertRaises(installer.InstallError):
                installer.validate_skill_source(source)


if __name__ == "__main__":
    unittest.main()
