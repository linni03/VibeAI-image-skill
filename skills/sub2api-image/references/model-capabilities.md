# Model And Size Handling

## What Is Known

The current Sub2API checkout defaults the Images API to `gpt-image-2`, recognizes `gpt-image-*` image models, and passes explicit sizes through to the selected channel. `/v1/models` can establish that a model name is routed, but it does not publish per-channel image-size capabilities.

Therefore, distinguish three facts:

- **Requested size**: what the client sent.
- **Actual dimensions**: parsed from the returned image bytes.
- **Billing tier**: derived by Sub2API from output dimensions when available, otherwise the request size.

Do not claim channel support from the model name alone. A successful live response with matching actual dimensions is the capability check.

## Client Presets

The scripts use presets whose maximum edge matches Sub2API's tier classifier:

| Tier | Square | Landscape | Portrait |
| --- | --- | --- | --- |
| `1K` | `1024x1024` | `1024x576` | `576x1024` |
| `2K` | `2048x2048` | `2048x1152` | `1152x2048` |
| `4K` | `3840x3840` | `3840x2160` | `2160x3840` |

These are request presets, not universal upstream guarantees. If a selected channel rejects a preset, report its error and let the operator configure a supported exact size. Never silently substitute another resolution.

## Billing Classification

Sub2API classifies an explicit or actual `WIDTHxHEIGHT` by its longest edge:

- `<= 1024`: `1K`
- `<= 2048`: `2K`
- `> 2048`: `4K`

This means `1536x1024` is billed as `2K`, not `1K`. The original design's proposed `1K landscape: 1536x1024` mapping was inconsistent with the project billing code and is intentionally not used.

If returned images span multiple tiers, Sub2API bills using the highest output tier and records a size breakdown. The client reports each image's actual tier and sets `tier_match` false when any image misses the requested tier.

## Choosing A Size

- Default no-size requests to `1K` square.
- Honor explicit `1K`, `2K`, or `4K` without downgrade.
- Ask when the user says only "HD", "high resolution", or another ambiguous term.
- Treat orientation as a size selector, not merely prompt text.
- Use `--size` for an exact supported channel size.
- For 4K, describe the result as 4K only after actual dimensions classify as 4K.
- If a request succeeds with different dimensions in the same tier, report both values without calling it an exact-size match.
