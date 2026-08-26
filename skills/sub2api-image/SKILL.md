---
name: sub2api-image
description: Generate or edit raster images through a configured Sub2API Images API, using a conservative OpenAI OAuth profile, safe local file validation, traceable paid requests, and explicit multi-output handling. Use for Sub2API-backed PNG/JPEG/WebP creation or editing, including reference images, masks, exact output paths, installation checks, and transport-failure diagnosis. Do not use for image analysis, API-only questions, or code-native HTML/CSS/SVG work.
---

# Sub2API Image

Use the bundled standard-library Python clients against the configured Sub2API endpoint. Never substitute an upstream key, another image provider, or the built-in image tool.

## Workflow

1. Locate this skill directory and run its scripts in place. Read `.runtime.json` directly and use its `python_executable` when it still exists; otherwise use Python 3.10 or newer.
2. Resolve the operation, prompt, size, orientation, output format and path, model options, source images, mask, and requested output count before making a paid request.
3. Distinguish concepts from files. "One image containing two categories" is one output; "two categories, one image each" is two outputs. Ask once when this distinction is materially ambiguous.
4. The `sub2api-openai-oauth` profile supports exactly one image per paid request. Never pass `--n` above 1. A clear request for N separate outputs authorizes exactly N sequential paid requests, each with `--n 1` and a unique output path. Stop before the next request after any failure, ambiguous billing outcome, or user interruption. Never retry a failed request automatically.
5. Invoke each paid generation or edit with `--timeout 600`. Do not run a probe or dry run first unless the user explicitly requests no-cost validation or troubleshooting requires it.
6. Preserve the complete structured command result and original `session_id`. Resume that same session at intervals of up to 15 seconds. Pending output, an absent file, or an outer `cell_id` completing without final client JSON is not a terminal result.
7. Treat only an explicit client exit with final JSON as terminal. A deadline signal is not proof the underlying request stopped. Never start a replacement invocation while the original command may still be running.
8. Inspect every saved final image when local image viewing is available. Report absolute paths, requested and actual dimensions, tier, format, server request ID when present, and the always-present `client_request_id`.
9. Treat false size, tier, orientation, count, or format matches as failure even if files were saved.

Read [references/transport-and-billing.md](references/transport-and-billing.md) for TLS, EOF, timeout, sequential multi-output, and billing-ambiguity handling. Read [references/sub2api-api.md](references/sub2api-api.md) for the deployed wire contract. Read [references/model-capabilities.md](references/model-capabilities.md) before selecting custom sizes.

## Configure

Run the interactive configuration script only when configuration is missing or the user asks to change it:

```text
<python> <skill-dir>/scripts/configure.py
```

Never print or place the key in commands, URLs, logs, metadata, filenames, or replies. Dedicated `SUB2API_IMAGE_*` variables may override configuration; never use a generic `OPENAI_API_KEY` or Codex credential.

On Windows, configuration is under `%CODEX_HOME%\sub2api-image\config.json`, falling back to `%USERPROFILE%\.codex\sub2api-image\config.json`, with DPAPI protection. On macOS, Linux, and WSL it is under `~/.config/sub2api-image/config.json` with mode `0600`.

## Generate

```text
<python> <skill-dir>/scripts/generate.py --prompt "A quiet city at dawn" --tier 1K --orientation square --output ./city.png --n 1 --timeout 600
```

The OAuth profile defaults to SSE with `partial_images=0`. This supplies transport keepalives and completed events without purchasing preview images. A JSON response to the same request is accepted without resending. Use `--no-stream` only when explicitly required for diagnosis or compatibility, never as a retry strategy.

Verified presets are `1024x1024` (1K square), `1536x1024` (2K landscape), and `1024x1536` (2K portrait). Unsupported presets fail locally. Exact `--size WIDTHxHEIGHT` bypasses only the preset allow-list and must still pass legal-size and returned-byte validation.

Use `--prompt-file` for long prompts, `--pictures` for the native Pictures folder, and `--output` for an exact path. Never add `--overwrite` unless replacement is intended.

## Edit

```text
<python> <skill-dir>/scripts/edit.py --image <absolute-source-path> --prompt "Replace the background with a snowy mountain" --n 1 --timeout 600
```

Editing uses the same default SSE, request tracing, terminal-event, timeout, and retry rules. Repeat `--image` for references and add one `--mask` when supplied. The mask dimensions must match the first input. Use local PNG, JPEG, or WebP files and never silently resize them.

## Validate Installation

Run local diagnostics without network access or image billing:

```text
<python> <skill-dir>/scripts/doctor.py
```

Add `--network` only to check DNS, TLS, and the image ingress `/health` route. It does not call an image endpoint, incur image billing, or verify API-key authorization:

```text
<python> <skill-dir>/scripts/doctor.py --network
```

Use `generate.py --dry-run` only for explicit no-cost option validation. Run `smoke_test.py` only after the user authorizes a real paid 1K generation.

## Safety

- Never retry authentication, permission, validation, response mismatch, TLS, EOF, reset, timeout, or HTTP 524 failures automatically.
- Treat TLS/EOF/reset/timeout before a validated completed event as an ambiguous paid outcome. Report the `client_request_id` and do not tell the user a new request is free.
- Keep a validated final image received before a later transport failure and report its transport warning. Partial images are diagnostics, never final output.
- Do not continue a sequential multi-output job after an ambiguous request; report completed K of N outputs and the failed request's correlation details.
- Never silently change model, size, tier, output count, quality, format, or other native options.
- Use only the bundled synchronous generation and edit clients. Do not invent or probe unimplemented async routes.
- Keep returned images and remove only temporary files created during the current workflow.
