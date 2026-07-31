# Model And Size Handling

## Known Behavior

Sub2API 0.1.169 routes OpenAI OAuth image accounts through its Responses image-generation bridge. This skill defaults to the `sub2api-openai-oauth` profile and `gpt-image-2`. `/v1/models` can show that a model is routed, but it does not prove every account or bridge supports every image option.

Keep these facts separate:

- Requested size: the value sent by the client.
- Actual dimensions: width and height parsed from returned image bytes.
- Billing tier: Sub2API classification based on output dimensions when available, otherwise request size.

A successful response with matching actual dimensions is the capability check. A valid image can still be blank, fixture-like, or semantically wrong, so inspect visual fidelity separately.

## Accepted Client Sizes

Pass `auto` or a `WIDTHxHEIGHT` satisfying every constraint:

- Width and height are multiples of 16.
- Longest edge is at most 3840.
- Aspect ratio is at most 3:1.
- Total pixels are between 655360 and 8294400 inclusive.

Reject invalid sizes before sending a paid request and report the nearest legal suggestion. Never round or substitute silently.

## Verified OAuth Presets

| Tier | Square | Landscape | Portrait |
| --- | --- | --- | --- |
| `1K` | `1024x1024` | unsupported preset | unsupported preset |
| `2K` | unsupported preset | `1536x1024` | `1024x1536` |
| `4K` | unsupported preset | unsupported preset | unsupported preset |

Unsupported presets fail locally before network activity. They are not silently rounded, downgraded, or replaced. `--size WIDTHxHEIGHT` remains available for an exact size that the operator has separately validated; it must satisfy the client constraints above, and the returned bytes must match exactly.

## Billing Classification

Sub2API classifies explicit or actual dimensions by longest edge:

- `<= 1024`: `1K`
- `<= 2048`: `2K`
- `> 2048`: `4K`

For example, OAuth-native `1536x1024` is `2K`. When returned images span tiers, Sub2API uses the highest output tier and records a size breakdown.

## Matching Rules

For explicit dimensions and presets, require actual size, orientation, and tier to match. Also require returned image count and file format to match every request.

For `size=auto`, set size, orientation, and tier matching fields to null because they are not requested claims. Continue enforcing count and format.
