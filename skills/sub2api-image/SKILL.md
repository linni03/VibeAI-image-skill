---
name: sub2api-image
description: Generate and edit raster images through a configured Sub2API OpenAI-compatible Images API, save PNG/JPEG/WebP files locally, and verify actual size, count, format, and billing tier. Use when the user asks to create, draw, render, or edit a bitmap image with Sub2API, including cover art, posters, product images, exact or automatic sizing, 1K/2K/4K tiers, multiple reference images, masks, transparent backgrounds, installation checks, or named local output files. Do not use for image analysis, API-only questions, pricing discussion without generation, or code-native HTML/CSS/SVG work.
---

# Sub2API Image

Use the bundled standard-library Python clients. Send requests only to the configured Sub2API endpoint; never substitute an upstream-provider key or the built-in image tool.

## Workflow

1. Locate this skill directory and call its scripts in place. Do not copy them into the user's project.
2. Resolve the Python runtime as described below, then inspect non-secret settings with `<python> scripts/configure.py --show`. Configure locally when settings are absent.
3. Parse the operation, prompt, tier or exact size, orientation, count, model, format, output path, input images, mask, and optional native image parameters.
4. Default an omitted resolution to `1K` square. Ask once when the user provides only an ambiguous term such as "HD".
5. Use `--dry-run` after configuration or whenever request options are uncertain. Confirm that it reports no network request and no file writes.
6. Run `generate.py` or `edit.py`. Never hand-build an HTTP request when the bundled client supports it.
7. Read the JSON report. When local image viewing is available, inspect every saved image for blank/error output and obvious prompt or edit mismatch.
8. Report every absolute output path, requested model and size, actual dimensions, actual billing tier, and any visual-fidelity caveat.
9. Treat any false `tier_match`, `orientation_match`, `exact_size_match`, `count_match`, or `format_match` as failure. A null size match means `size=auto` made that check inapplicable.
10. Return actionable errors without exposing credentials. Read [references/sub2api-api.md](references/sub2api-api.md) for protocol or error troubleshooting. Read [references/model-capabilities.md](references/model-capabilities.md) before choosing custom sizes or diagnosing size failures.

## Runtime

Read `<skill-dir>/.runtime.json` when present. Use its absolute `python_executable` when that file and executable still exist. Otherwise resolve Python 3.10 or newer with `py -3` then `python` on native Windows, or `python3` then `python` on macOS, Linux, and WSL. Treat `<python>` below as that resolved command.

Execute each client invocation as one logical command with separate arguments. Do not depend on Bash backslash continuations, PowerShell backticks, a `.py` file association, or the current working directory.

## Configure

Prefer an interactive terminal so the key is read without echo and never appears in process arguments:

```text
<python> <skill-dir>/scripts/configure.py
```

Store configuration at `~/.config/sub2api-image/config.json`. Protect it with mode `0600` on macOS, Linux, and WSL. On native Windows, store the API key encrypted with current-user DPAPI instead of claiming POSIX permissions. Never print, repeat, summarize, or place the key in a command. Warn that keys pasted into chat may remain in session records; ask the user to run the interactive command locally.

Use `--show` for non-secret settings and `--revoke` to remove local configuration. Dedicated `SUB2API_IMAGE_*` environment variables may override settings, but never read a generic `OPENAI_API_KEY` or a Codex credential. Do not weaken a config permission error.

## Generate

```text
<python> <skill-dir>/scripts/generate.py --prompt "A quiet city at dawn" --tier 1K --orientation landscape --output ./city.png
```

Use `--prompt-file` for long prompts. Use `--size auto` only when the upstream should choose dimensions; otherwise pass a validated `WIDTHxHEIGHT`. Use `--output-dir` for generated names or `--output` for an exact filename/multi-image basename. The output extension selects PNG, JPEG, or WebP unless `--output-format` is explicitly consistent. Never add `--overwrite` unless replacement is intended.

Pass `--quality`, `--background`, `--moderation`, `--output-compression`, `--timeout`, or `--metadata` only when requested or operationally needed. Transparent backgrounds require PNG or WebP; compression applies only to JPEG or WebP.

## Edit

```text
<python> <skill-dir>/scripts/edit.py --image <absolute-source-path> --prompt "Replace the background with a snowy mountain"
```

Repeat `--image` for multiple reference images. Add one `--mask` when supplied; its dimensions must match the first input image. Use only local PNG, JPEG, or WebP inputs. Do not silently resize inputs.

## Validate Installation

Run a no-cost validation first:

```text
<python> <skill-dir>/scripts/generate.py --prompt "Sub2API image configuration check" --size auto --dry-run
```

Run `<python> <skill-dir>/scripts/smoke_test.py` only when a real paid 1K generation is authorized. It checks authentication, decoding, atomic save, format, dimensions, and secret-free output, but cannot verify administrator-only usage logs or charges.

## Safety

- Keep one Sub2API image-enabled user key per user. Never use an upstream account key.
- Never put a key in Git, command arguments, URLs, logs, metadata, filenames, or replies.
- Never retry authentication, permission, validation, response-mismatch, network-timeout, or HTTP 524 failures automatically.
- Treat 524 and client timeouts as ambiguous paid outcomes. Check the image-only direct Base URL, proxy/origin timeouts, request ID, and usage logs before asking for retry approval.
- Never silently downgrade size, model, count, quality, format, or other native options.
- Keep returned images; remove only temporary files created during the current workflow.
- Use only the deployed synchronous generation and edit endpoints. Do not invent async task routes.
