from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_reusable_config_preserves_existing_values_without_prompt(self) -> None:
        existing = installer.ConfigState(
            base_url="https://images.example.test/v1",
            api_key="existing-secret",
            model="existing-model",
            output_dir="existing-output",
            timeout_seconds=321,
            provider_profile=installer.DEFAULT_PROVIDER_PROFILE,
        )

        self.assertEqual(
            installer.reusable_config(existing),
            installer.Config(
                base_url=existing.base_url,
                api_key="existing-secret",
                model=existing.model,
                output_dir=existing.output_dir,
                timeout_seconds=existing.timeout_seconds,
                provider_profile=existing.provider_profile,
            ),
        )

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
