---
name: sub2api-image
description: Generate and edit raster images through a configured Sub2API OpenAI-compatible Images API, save returned PNG/JPEG/WebP files locally, and verify actual dimensions against requested 1K/2K/4K tiers. Use when the user asks to create, draw, render, or edit a bitmap image with Sub2API, including cover art, posters, product images, aspect-orientation requests, resolution tiers, image-to-image edits, masks, installation smoke tests, or local output files. Do not use for image analysis, API-only questions, pricing discussion without generation, or code-native HTML/CSS/SVG work.
---

# Sub2API Image

Use the bundled standard-library Python clients. Send requests only to the user's configured Sub2API endpoint; never substitute an upstream-provider key or the built-in image tool.

## Workflow

1. Locate this skill directory and use its `scripts/` paths. Do not copy the scripts into the user's project.
2. Read configuration with `python3 scripts/configure.py --show`. If it is absent, configure before generating.
3. Parse the prompt, operation, tier or exact size, orientation, count, model override, output format, input image, and optional mask from the user's request.
4. Default an omitted resolution to `1K` and an omitted orientation to `square`. Tell the user that default in the final result. Ask once when the user only says "HD" or another ambiguous quality term.
5. Run `generate.py` or `edit.py`. Do not hand-build HTTP requests when the bundled client supports the request.
6. Read the emitted JSON report. When a local image-viewing tool is available, inspect every saved result for an error image, blank output, and an obvious prompt/edit mismatch. Keep transport/file validation distinct from visual fidelity.
7. Report every absolute output path, requested model and size, actual dimensions, actual billing tier, and any visual-fidelity caveat.
8. Treat `tier_match: false` as a failed resolution claim even when a file was returned. Never describe a smaller result as 2K or 4K.
9. Return a concise actionable error without exposing credentials. Read [references/sub2api-api.md](references/sub2api-api.md) only for protocol or error troubleshooting. Read [references/model-capabilities.md](references/model-capabilities.md) when choosing a size, handling 4K, or diagnosing a size mismatch.

## Configure

Prefer an interactive terminal so the key is read without echo and never appears in process arguments:

```bash
python3 <skill-dir>/scripts/configure.py
```

The default config is `~/.config/sub2api-image/config.json` with mode `0600`. Never print, repeat, summarize, or place the key in a command. Warn that a key pasted into a chat may remain in conversation/session records; for sensitive keys, ask the user to run the interactive command locally.

Use `--show` to inspect non-secret settings and `--revoke` to remove local configuration. Do not weaken a config permission error; fix the permissions or reconfigure.

## Generate

```bash
python3 <skill-dir>/scripts/generate.py \
  --prompt "A quiet city at dawn" \
  --tier 1K \
  --orientation landscape
```

Use `--size WIDTHxHEIGHT` only when the user explicitly requests exact dimensions or the model/channel requires them. An exact size overrides the tier/orientation preset. Pass `--model`, `--n`, `--quality`, `--output-format`, or `--output-dir` only when needed.

## Edit

```bash
python3 <skill-dir>/scripts/edit.py \
  --image /absolute/path/source.png \
  --prompt "Replace the background with a snowy mountain"
```

Add `--mask /absolute/path/mask.png` when supplied. Use local PNG, JPEG, or WebP inputs. Do not send an untrusted path or silently resize an input.

## Smoke Test

After installation or configuration, run one low-cost 1K request:

```bash
python3 <skill-dir>/scripts/smoke_test.py
```

This verifies authentication, image response decoding, atomic save, format, dimensions, and secret-free output. It cannot verify the administrator-only usage log or charge; state that the operator must check those in Sub2API admin.

## Safety

- Keep one Sub2API user key per user. Never use or request an upstream account key.
- Never put a key in Git, command arguments, URLs, logs, generated filenames, or final replies.
- Do not retry authentication, permission, validation, or resolution-mismatch failures automatically.
- Do not silently downgrade size, model, count, quality, or format.
- Keep returned images; remove only temporary files created by the scripts.
- Current Sub2API exposes synchronous image generation and edit endpoints. Do not invent async task endpoints or poll routes absent from the deployed API.
