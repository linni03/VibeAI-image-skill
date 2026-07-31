# Sub2API Images API

## Endpoints

Use a Base URL ending in `/v1`. The client appends `/v1` to a bare origin and avoids duplicate `/v1/v1` segments.

| Operation | Method and path | Body |
| --- | --- | --- |
| Generate | `POST /v1/images/generations` | JSON |
| Edit | `POST /v1/images/edits` | `multipart/form-data` |

Authenticate with a Sub2API image-enabled user key. The client does not implement async generation, edit, or task polling routes.

## Requests

Generation sends `model`, `prompt`, `size`, `n`, `response_format=b64_json`, and `output_format`. By default it also sends `stream=true` and `partial_images=1`; `--no-stream` omits both fields. Editing sends equivalent scalar multipart fields and repeats the `image` part for each local reference image. It sends at most one `mask` part.

Optional supported fields include `quality`, `background`, `moderation`, `output_compression`, and edit `input_fidelity`. Transparent backgrounds require PNG or WebP. Output compression is an integer from 0 through 100 and applies only to JPEG or WebP.

Use `Cache-Control: no-store` and `Pragma: no-cache`. `b64_json` avoids an object-storage dependency; never write base64 response bodies to logs or metadata.

## Responses

For JSON, require a non-empty `data[]`. Each item must contain valid `b64_json`, a data URL, or an HTTP(S) URL that decodes to PNG, JPEG, or WebP. Inspect file magic and actual dimensions before atomic save.

For `text/event-stream`, parse incrementally across arbitrary byte boundaries. Support CRLF, comments/keepalives, multiline `data:`, `image_generation.partial_image`, `image_generation.completed`, explicit error events, and `[DONE]`. Reject malformed JSON and oversized events. If the response is JSON despite a streaming request, parse that same response without sending a second request.

An EOF after a validated completed event leaves the final image usable but adds a transport warning. An EOF before completion is a failed, billing-ambiguous outcome: save validated partial images under clearly partial names, never report them as final, and never retry automatically.

Reports preserve only safe scalar response metadata and usage scalars. Metadata sidecars may contain request options and local paths, but must exclude API keys, image base64, response URLs, and signed URL query parameters.

Treat size, tier, orientation, image count, or output format mismatch as a failed result even when files were saved.

## Errors

| Status | Category | Action |
| --- | --- | --- |
| `400` | `invalid_request` | Fix the named field; do not retry unchanged |
| `401` | `authentication` | Reconfigure with a valid Sub2API user key |
| `403` | `permission` | Use an image-enabled group/key |
| `404` | `not_found` | Check normalized Base URL, route, model, and deployment version |
| `429` | `rate_limit` | Respect `Retry-After`; retry only with user approval |
| `524` | `edge_timeout` | Check direct image ingress, proxy/origin timeouts, request ID, and usage before retry approval |
| other `5xx` | `server_or_upstream` | Report the safe server message and request ID |

`credential_decryption` is a local pre-request failure, not an API status. Report its configuration path, DPAPI error code when available, and stored protection scheme. Update or reconfigure the skill; preserve validated non-secret settings, and require a replacement key only when the old credential cannot be migrated.

Timeouts, TLS EOF, incomplete HTTP bodies, remote disconnects and connection resets are ambiguous paid outcomes: the upstream might finish after the client disconnects. Never retry automatically.

## Billing Verification

The client reports requested and actual tiers but cannot query administrator-only usage logs with a normal user key. Ask the Sub2API operator to verify `image_count`, `image_size`, `image_size_source`, `image_size_breakdown`, and charged amount separately.
