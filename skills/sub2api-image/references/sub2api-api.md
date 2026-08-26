# Sub2API Images API

## Supported Endpoints

Use a Base URL ending in `/v1`. The client normalizes a bare origin and avoids duplicate `/v1/v1` segments.

| Operation | Method and path | Body |
| --- | --- | --- |
| Generate | `POST /v1/images/generations` | JSON |
| Edit | `POST /v1/images/edits` | `multipart/form-data` |
| Ingress health | `GET /health` on the same origin | none |

Authenticate paid calls with a Sub2API image-enabled user key. The bundled client intentionally does not use async, task, batch, or billing-query routes because those contracts are not verified across supported deployments.

## OAuth Request Contract

Generation sends `model`, `prompt`, `size`, `n=1`, `response_format=b64_json`, and `output_format`. Editing sends equivalent scalar multipart fields, repeated `image` parts, and at most one `mask`.

The `sub2api-openai-oauth` profile rejects `n>1` locally. Multiple requested files are separate, sequential, authorized paid requests; they are not one Images API request.

By default, generation and editing send `stream=true` and `partial_images=0`. This uses the bridge's SSE keepalive and completion path without buying preview frames. `--no-stream` omits both fields and requests one JSON response. A JSON response to a streaming request is accepted without a second request.

Each request sends:

- `X-Client-Request-Id`: generated before the request and preserved in every report.
- `Cache-Control: no-store` and `Pragma: no-cache`.
- `Accept: text/event-stream` in streaming mode or `application/json` otherwise.

Optional fields include `quality`, `background`, `moderation`, `output_compression`, and edit `input_fidelity`. Transparent output requires PNG or WebP. Compression from 0 through 100 applies only to JPEG or WebP.

## Response Contract

For JSON, require a non-empty `data[]`. Each item must contain valid `b64_json`, a data URL, or an HTTP(S) URL that decodes to PNG, JPEG, or WebP. Inspect magic bytes and dimensions before atomic save.

For SSE, parse arbitrary byte boundaries, CRLF, comments, multiline data, explicit errors, and these events:

- `image_generation.partial_image`
- `image_edit.partial_image`
- `response.image_generation_call.partial_image`
- `image_generation.completed`
- `image_edit.completed`
- optional `[DONE]`

A validated completed event is the Sub2API terminal boundary. `[DONE]` is accepted but not required. A clean EOF after the expected completed event is success. A transport exception after that event preserves the final image and adds a warning. An EOF before completion is billing-ambiguous and may save only clearly named partial diagnostics.

Returned scalar metadata and usage values may be reported. Never store API keys, base64 bodies, response URLs, or signed URL query parameters in logs or sidecars.

Treat size, tier, orientation, count, or format mismatch as failure even when a file was returned.

## Error Handling

| Status | Category | Action |
| --- | --- | --- |
| `400` | `invalid_request` | Fix the named field; do not retry unchanged |
| `401` | `authentication` | Reconfigure with a valid Sub2API user key |
| `403` | `permission` | Use an image-enabled group and user key |
| `404` | `not_found` | Check the normalized Base URL and deployed route |
| `429` | `rate_limit` | Report `Retry-After`; do not retry automatically |
| `524` | `edge_timeout` | Treat billing as ambiguous and report correlation IDs |
| other `5xx` | `server_or_upstream` | Report the safe message and IDs; do not retry automatically |

Transport exceptions before a completed event include `billing_status=ambiguous`, `retry_safe=false`, and `client_request_id`. See [transport-and-billing.md](transport-and-billing.md) for the recovery workflow.

## Wait Lifecycle

Paid commands use a 600-second caller and socket timeout. The client emits `image_request_started` immediately, then `image_request_pending` every 15 seconds. These secret-free events include the `client_request_id`; stdout remains reserved for final JSON.

Preserve the original command `session_id` and continue only that session. `image_request_deadline_reached` is a caller deadline, not proof that the network request exited. Stop the session, perform one final result check, and report an ambiguous outcome without launching a replacement request.

## Health Check

`doctor.py --network` calls the same origin's `/health` route. It checks DNS, TLS, HTTP reachability, and response timing without calling an image endpoint. It deliberately reports `authentication_checked=false`; health success does not prove that a user key or model is authorized.
