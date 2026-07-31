#!/usr/bin/env python3
"""Install and configure the Sub2API image skill for the current user."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parent
SKILL_NAME = "sub2api-image"
SKILL_SOURCE = REPO_ROOT / "skills" / SKILL_NAME
SOURCE_SCRIPTS = SKILL_SOURCE / "scripts"

if str(SOURCE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SOURCE_SCRIPTS))

from image_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    LEGACY_CONFIG_PATH,
    Config,
    ConfigState,
    ConfigError,
    config_protection,
    config_from_mapping,
    default_config_path,
    discover_config_path,
    load_config,
    read_config_state,
    save_config,
)


class InstallError(RuntimeError):
    """An installation failure safe to show to the user."""


@dataclass(frozen=True)
class InstallResult:
    target: Path
    updated: bool
    backup_path: Path | None = None


def detect_platform() -> str:
    if os.name == "nt":
        return "Windows native"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        try:
            release = Path("/proc/sys/kernel/osrelease").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            release = ""
        if "microsoft" in release.lower():
            return "Windows WSL (Linux)"
        return "Linux"
    return sys.platform


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="安装并配置 VibeAI Sub2API 图像 Skill。"
    )
    parser.add_argument(
        "--base-url",
        help=f"跳过 Base URL 提问（默认：{DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex 主目录（默认：CODEX_HOME 或 ~/.codex）",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"配置文件路径（默认：{DEFAULT_CONFIG_PATH}）",
    )
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


def resolve_codex_home(value: Path | str | None = None) -> Path:
    if value is not None:
        selected = Path(value)
    else:
        configured = os.environ.get("CODEX_HOME", "").strip()
        selected = Path(configured) if configured else Path.home() / ".codex"
    return selected.expanduser().resolve()


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if (
            name == "__pycache__"
            or lowered in {".ds_store", ".runtime.json", "config.json"}
            or lowered.endswith((".pyc", ".pyo", ".key", ".pem", ".secret"))
            or lowered == ".env"
            or lowered.startswith(".env.")
        ):
            ignored.add(name)
    return ignored


def _write_runtime_metadata(skill_dir: Path, platform_name: str) -> None:
    executable = Path(sys.executable).expanduser().resolve()
    if not executable.is_file():
        raise InstallError(f"Python executable is unavailable: {executable}")
    payload = {
        "schema_version": 1,
        "platform": platform_name,
        "python_executable": str(executable),
    }
    runtime_path = skill_dir / ".runtime.json"
    runtime_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        runtime_path.chmod(0o644)


def validate_skill_source(source: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise InstallError(f"Skill source is missing or unsafe: {source}")

    required = (
        "SKILL.md",
        "agents/openai.yaml",
        "scripts/configure.py",
        "scripts/generate.py",
        "scripts/edit.py",
        "scripts/image_client.py",
    )
    missing = [name for name in required if not (source / name).is_file()]
    if missing:
        raise InstallError(f"Skill source is incomplete; missing: {', '.join(missing)}")

    for root, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            candidate = Path(root) / name
            if candidate.is_symlink():
                raise InstallError(f"Skill source must not contain symbolic links: {candidate}")

    try:
        skill_text = (source / "SKILL.md").read_text(encoding="utf-8")
        agent_text = (source / "agents" / "openai.yaml").read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"Cannot read the skill metadata: {exc}") from exc

    if not skill_text.startswith("---\n") or not re.search(
        rf"(?m)^name:\s*{re.escape(SKILL_NAME)}\s*$", skill_text
    ):
        raise InstallError("SKILL.md does not declare the expected skill name")
    if not re.search(r"(?m)^\s*allow_implicit_invocation:\s*true\s*$", agent_text):
        raise InstallError("agents/openai.yaml does not enable implicit invocation")


def install_skill(
    source: Path,
    codex_home: Path | str,
    *,
    retain_backup: bool = False,
    platform_name: str | None = None,
) -> InstallResult:
    source = source.expanduser().resolve()
    validate_skill_source(source)

    skills_dir = Path(codex_home).expanduser().resolve() / "skills"
    target = skills_dir / SKILL_NAME
    if source == target.resolve():
        return InstallResult(target=target, updated=False)

    try:
        skills_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"Cannot create the Codex skills directory {skills_dir}: {exc}") from exc
    if not skills_dir.is_dir():
        raise InstallError(f"Codex skills path is not a directory: {skills_dir}")

    stage_root = Path(
        tempfile.mkdtemp(prefix=f".{SKILL_NAME}-install-", dir=skills_dir)
    )
    staged_skill = stage_root / SKILL_NAME
    backup: Path | None = None
    updated = _path_exists(target)
    try:
        shutil.copytree(source, staged_skill, copy_function=shutil.copy2, ignore=_copy_ignore)
        _write_runtime_metadata(staged_skill, platform_name or detect_platform())
        validate_skill_source(staged_skill)

        if updated:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup = skills_dir / f".{SKILL_NAME}-backup-{stamp}-{uuid.uuid4().hex[:8]}"
            os.replace(target, backup)

        try:
            os.replace(staged_skill, target)
        except OSError:
            if backup is not None and _path_exists(backup) and not _path_exists(target):
                os.replace(backup, target)
                backup = None
            raise

        if backup is not None and not retain_backup:
            try:
                _remove_path(backup)
                backup = None
            except OSError:
                pass
    except (OSError, shutil.Error) as exc:
        raise InstallError(f"Cannot install the skill at {target}: {exc}") from exc
    finally:
        if _path_exists(stage_root):
            try:
                _remove_path(stage_root)
            except OSError:
                pass

    return InstallResult(target=target, updated=updated, backup_path=backup)


def rollback_install(result: InstallResult) -> None:
    try:
        if _path_exists(result.target):
            _remove_path(result.target)
        if result.backup_path is not None and _path_exists(result.backup_path):
            os.replace(result.backup_path, result.target)
    except OSError as exc:
        raise InstallError(
            f"Installation failed and the previous skill could not be restored: {exc}"
        ) from exc


def finalize_install(result: InstallResult) -> InstallResult:
    backup = result.backup_path
    if backup is not None:
        try:
            _remove_path(backup)
            backup = None
        except OSError:
            pass
    return InstallResult(
        target=result.target,
        updated=result.updated,
        backup_path=backup,
    )


def read_existing_config(path: Path) -> ConfigState | None:
    expanded = path.expanduser()
    if not _path_exists(expanded):
        return None
    return read_config_state(expanded)


def prompt_config(
    existing: Config | ConfigState | None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    output_dir: str | None = None,
    timeout_seconds: int | None = None,
    input_fn: Callable[[str], str] = input,
    key_input_fn: Callable[[str], str] = input,
) -> Config:
    default_base_url = existing.base_url if existing else DEFAULT_BASE_URL
    if base_url is None:
        entered_base_url = input_fn(f"Sub2API Base URL [{default_base_url}]: ").strip()
        selected_base_url = entered_base_url or default_base_url
    else:
        selected_base_url = base_url

    key_label = "Sub2API 生图 API Key（输入可见）"
    if existing is not None and existing.api_key is not None:
        key_label += " [直接回车保留现有密钥]"
    elif isinstance(existing, ConfigState) and existing.credential_error is not None:
        key_label += " [旧密钥无法解密，必须输入替换密钥]"
    entered_key = key_input_fn(f"{key_label}: ")
    if entered_key:
        selected_key = entered_key
    elif existing is not None and existing.api_key is not None:
        selected_key = existing.api_key
    elif isinstance(existing, ConfigState) and existing.credential_error is not None:
        raise ConfigError("旧 Windows 密钥无法解密，请输入新的 Sub2API API Key")
    else:
        selected_key = ""

    return config_from_mapping(
        {
            "base_url": selected_base_url,
            "api_key": selected_key,
            "model": model or (existing.model if existing else DEFAULT_MODEL),
            "output_dir": output_dir
            or (existing.output_dir if existing else DEFAULT_OUTPUT_DIR),
            "timeout_seconds": timeout_seconds
            if timeout_seconds is not None
            else (existing.timeout_seconds if existing else DEFAULT_TIMEOUT_SECONDS),
        }
    )


def main() -> int:
    args = parse_args()
    try:
        validate_skill_source(SKILL_SOURCE)
        if not sys.stdin.isatty():
            raise InstallError(
                "Run this installer in an interactive terminal to enter the API key"
            )

        platform_name = detect_platform()
        codex_home = resolve_codex_home(args.codex_home)
        config_path = (
            args.config.expanduser()
            if args.config is not None
            else default_config_path(codex_home=codex_home)
        )
        existing_config_path = discover_config_path(
            args.config,
            default_path=config_path,
            legacy_path=LEGACY_CONFIG_PATH,
        )

        print("VibeAI Sub2API 图像 Skill 安装器")
        print(f"检测到系统：{platform_name}")
        print(f"Python：{Path(sys.executable).resolve()}")
        print(f"Codex Home：{codex_home}")
        print(f"配置文件：{config_path.resolve()}")
        print("直接按回车即可采用方括号中的默认值。\n")

        existing = read_existing_config(existing_config_path)
        if existing is not None and existing.credential_error is not None:
            print(
                "[WARN] 现有 Windows 密钥无法在当前安全上下文中解密；"
                "将保留非敏感配置并要求输入替换密钥"
            )
        config = prompt_config(
            existing,
            base_url=args.base_url,
            model=args.model,
            output_dir=args.output_dir,
            timeout_seconds=args.timeout,
        )
        result = install_skill(
            SKILL_SOURCE,
            codex_home,
            retain_backup=True,
            platform_name=platform_name,
        )
        try:
            written_config = save_config(config, config_path)
            if load_config(written_config, apply_env=False) != config:
                raise ConfigError("Saved configuration verification failed")
        except BaseException as exc:
            try:
                rollback_install(result)
            except InstallError as rollback_error:
                raise rollback_error from exc
            if isinstance(exc, (ConfigError, InstallError, OSError, KeyboardInterrupt)):
                raise
            raise InstallError(
                f"Unexpected configuration failure: {type(exc).__name__}"
            ) from exc
        result = finalize_install(result)

        action = "已更新" if result.updated else "已安装"
        print(f"\n[OK] Skill {action}：{result.target}")
        print(f"[OK] 配置已保存：{written_config.resolve()}")
        if existing_config_path != config_path and existing_config_path.exists():
            print(f"[OK] 已从旧配置迁移：{existing_config_path.resolve()}")
            print("[INFO] 旧配置文件已保留，可在确认新版本正常后手动删除")
        if os.name == "nt":
            print("[OK] API Key 保护：Windows DPAPI LocalMachine + 用户目录 ACL")
        else:
            print("[OK] API Key 保护：配置文件权限 0600")
        print(f"[OK] Base URL: {config.base_url}")
        print(f"[OK] 模型：{config.model}")
        print(f"[OK] 保护方式：{config_protection()}")
        if (
            existing is not None
            and existing.credential_protection is not None
            and existing.credential_protection != config_protection()
        ):
            print(
                f"[OK] 密钥保护已迁移：{existing.credential_protection}"
                f" -> {config_protection()}"
            )
        if existing is not None and existing.credential_error is not None:
            print("[OK] 无法解密的旧密钥已由新输入密钥替换")
        print("[OK] API Key 已受保护保存（输入时在终端中可见）")
        if result.backup_path is not None:
            print(f"[WARN] 旧安装备份未能清理，保留于：{result.backup_path}")

        print("\n请重启 Codex 或新建 Codex 会话，让 Codex 重新加载 Skill。")
        print("之后可直接说：生成一张 1K 横向图片并保存到当前目录。")
        print(f"也可以用 ${SKILL_NAME} 显式调用。")
        return 0
    except KeyboardInterrupt:
        print("\n安装已取消。", file=sys.stderr)
        return 130
    except (ConfigError, InstallError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
