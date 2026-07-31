---
name: sub2api-image
description: Generate and edit raster images through a configured Sub2API OpenAI-compatible Images API, save PNG/JPEG/WebP files locally, and verify actual size, count, format, and billing tier. Use when the user asks to create, draw, render, or edit a bitmap image with Sub2API, including cover art, posters, product images, exact or automatic sizing, 1K/2K/4K tiers, multiple reference images, masks, transparent backgrounds, installation checks, or named local output files. Do not use for image analysis, API-only questions, pricing discussion without generation, or code-native HTML/CSS/SVG work.
---

# Sub2API Image

Use the bundled standard-library Python clients. Send requests only to the configured Sub2API endpoint; never substitute an upstream-provider key or the built-in image tool.

## Workflow

1. Locate this skill directory and call its scripts in place. Do not copy them into the user's project.
2. Parse the operation, prompt, tier or exact size, orientation, count, model, format, output path, input images, mask, and optional native image parameters.
3. For a clear generation or edit request, invoke `generate.py` or `edit.py` exactly once. The client loads and validates configuration itself. Do not run `configure.py --show`, a Python probe, or `--dry-run` first.
4. Treat a clear request to generate or edit as authorization for one billable API request. Do not ask for a second conversational confirmation. A Codex sandbox approval may still appear when required by the user's security policy.
5. Default an omitted resolution to `1K` square. Ask once only when the user provides an ambiguous term such as "HD" or when a missing choice materially changes the result.
6. Use `--dry-run` only when the user explicitly asks for a no-cost validation, after new configuration during troubleshooting, or when request options cannot be resolved safely. Never include `--dry-run` in the command intended to create the requested image.
7. Read the JSON report. Treat `partial_images` as diagnostics, never final outputs. When local image viewing is available, inspect every final saved image for blank/error output and obvious prompt or edit mismatch.
8. Report every absolute output path, requested model and size, actual dimensions, actual billing tier, and any visual-fidelity caveat.
9. Treat any false `tier_match`, `orientation_match`, `exact_size_match`, `count_match`, or `format_match` as failure. A null size match means `size=auto` made that check inapplicable.
10. Return actionable errors without exposing credentials. Read [references/sub2api-api.md](references/sub2api-api.md) for protocol or error troubleshooting. Read [references/model-capabilities.md](references/model-capabilities.md) before choosing custom sizes or diagnosing size failures.

## Runtime

Read `<skill-dir>/.runtime.json` directly when present and use its absolute `python_executable` when that executable still exists. Do not launch a shell command only to probe Python. Otherwise use `py -3` then `python` on native Windows, or `python3` then `python` on macOS, Linux, and WSL. Require Python 3.10 or newer and treat `<python>` below as the resolved command.

Execute each client invocation as one logical command with separate arguments. Do not combine runtime discovery, configuration inspection, and generation in one PowerShell command. Do not depend on Bash backslash continuations, PowerShell backticks, a `.py` file association, or the current working directory.

## Configure

Use an interactive terminal so the key never appears in process arguments. Key input is intentionally visible, allowing the user to verify that typing or pasting succeeded; ensure nobody else can view the terminal:

```text
<python> <skill-dir>/scripts/configure.py
```

On native Windows, store configuration at `%CODEX_HOME%\sub2api-image\config.json`, falling back to `%USERPROFILE%\.codex\sub2api-image\config.json`. Encrypt the API key with machine-scoped DPAPI so Codex's dedicated Windows sandbox users can decrypt it, and rely on the user-profile/Codex Home ACL to restrict ciphertext access. Never place the config in a shared directory. The client can read old current-user DPAPI and legacy `%USERPROFILE%\.config\sub2api-image\config.json` configurations; running the installer or interactive configure command migrates readable credentials and retains a legacy-path file. If an old credential cannot be decrypted, require a replacement key while preserving validated non-secret settings. On macOS, Linux, and WSL, use `~/.config/sub2api-image/config.json` with mode `0600`.

Never print, repeat, summarize, or place the key in a command. Warn that keys pasted into chat may remain in session records; ask the user to run the interactive command locally.

Use `--show` only when the user asks to inspect or diagnose configuration. Use `--revoke` to remove current and retained legacy configuration. Dedicated `SUB2API_IMAGE_*` environment variables may override settings, but never read a generic `OPENAI_API_KEY` or a Codex credential. Do not weaken a config permission error.

## Generate

```text
<python> <skill-dir>/scripts/generate.py --prompt "A quiet city at dawn" --tier 1K --orientation landscape --output ./city.png
```

For a native OS Pictures folder, use one command and let the client resolve the real known-folder path:

```text
<python> <skill-dir>/scripts/generate.py --prompt "A soothing pixel-art landscape" --tier 2K --orientation landscape --pictures healing_pixel_landscape_2k.png
```

Use `--prompt-file` for long prompts. Use `--size auto` only when the upstream should choose dimensions; otherwise pass a validated `WIDTHxHEIGHT`. Use `--output-dir` for generated names or `--output` for an exact filename/multi-image basename. The output extension selects PNG, JPEG, or WebP unless `--output-format` is explicitly consistent. Never add `--overwrite` unless replacement is intended.

Generation streams by default with `stream=true` and `partial_images=1`. A JSON response is accepted from the same request; never resend merely to change response parsing. Use `--no-stream` only when the deployed service is known not to accept streaming fields or the user explicitly requests a non-streaming compatibility check. Do not use it as an automatic retry after TLS, EOF, reset, or timeout failure.

When a validated completed image arrives before a transport EOF, keep the final image and report `transport_warning`. When EOF occurs before completion, report ambiguous billing, save only validated files whose names contain `partial`, and state that they are not final images. Never issue a replacement generation request without new user authorization.

Honor an explicit output path first. When the user says Pictures, My Pictures, 图片目录, or 图片文件夹 without an absolute path, pass `--pictures` with a safe filename. When the user asks for the current project or current directory, pass an explicit relative `--output` path inside the active workspace.

Pass `--quality`, `--background`, `--moderation`, `--output-compression`, `--timeout`, or `--metadata` only when requested or operationally needed. Transparent backgrounds require PNG or WebP; compression applies only to JPEG or WebP.

## Edit

```text
<python> <skill-dir>/scripts/edit.py --image <absolute-source-path> --prompt "Replace the background with a snowy mountain"
```

Repeat `--image` for multiple reference images. Add one `--mask` when supplied; its dimensions must match the first input image. Use only local PNG, JPEG, or WebP inputs. Do not silently resize inputs.

## Validate Installation

Run local, no-network diagnostics for an explicit installation or configuration check:

```text
<python> <skill-dir>/scripts/doctor.py
```

Add `--network` only when TLS, connectivity, or authentication must be tested. It calls `/models`, never an image endpoint, and reports no key:

```text
<python> <skill-dir>/scripts/doctor.py --network
```

Use a dry run when request options themselves need validation:

```text
<python> <skill-dir>/scripts/generate.py --prompt "Sub2API image configuration check" --size auto --dry-run
```

Run `<python> <skill-dir>/scripts/smoke_test.py` only when a real paid 1K generation is authorized. It checks authentication, decoding, atomic save, format, dimensions, and secret-free output, but cannot verify administrator-only usage logs or charges.

## Safety

- Keep one Sub2API image-enabled user key per user. Never use an upstream account key.
- Never put a key in Git, command arguments, URLs, logs, metadata, filenames, or replies.
- On native Windows, treat the config file ACL as part of credential protection; machine-scoped DPAPI is machine-bound rather than user-bound.
- If the sandbox blocks a clear request, request one scoped approval for the actual generation or edit command. Do not use `--show` or `--dry-run` as a permission probe.
- Never retry authentication, permission, validation, response-mismatch, network-timeout, or HTTP 524 failures automatically.
- Treat 524, TLS EOF, incomplete response, reset, disconnect, and client timeout failures as ambiguous paid outcomes. Check the image-only direct Base URL, proxy/origin timeouts, request ID, and usage logs before asking for retry approval.
- Never silently downgrade size, model, count, quality, format, or other native options.
- Keep returned images; remove only temporary files created during the current workflow.
- Use only the deployed synchronous generation and edit endpoints. Do not invent async task routes.
