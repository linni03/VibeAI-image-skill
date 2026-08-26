# Transport And Billing Recovery

Use this reference when a paid request times out, loses TLS, ends early, returns an incomplete body, or is one item in a sequential multi-output job.

## Outcome States

| Observed state | Files | Billing status | Next action |
| --- | --- | --- | --- |
| Local validation failed before `image_request_started` | none | not submitted | Correct the request; this is not a retry |
| HTTP error with a final server response | normally none | use the returned error and operator records | Do not retry automatically |
| Validated `image_generation.completed` or `image_edit.completed` received | final image saved | completed result | Report success; a later transport warning does not invalidate the file |
| TLS, EOF, reset, 524, or timeout before a completed event | no final image; optional partial diagnostics | ambiguous | Stop and report the correlation IDs |
| Returned file fails size, count, or format verification | keep the file for diagnosis | request completed but contract failed | Report mismatch; do not retry automatically |

`image_request_started` proves only that the client began the network call. A local timeout or missing file does not prove that Sub2API or the upstream stopped processing.

## Correlation

The client creates a UUID-based `client_request_id` before every request and sends the same value as both `X-Client-Request-Id` and `X-Request-ID`. Preserve it from heartbeat, success, and error JSON. Sub2API may overwrite its response `X-Client-Request-ID`; when that differs, the client reports it separately as `server_client_request_id`. A server response ID may be absent when the connection fails before response headers; the local client ID must still be reported.

Ask the operator to search ingress request headers for the local `client_request_id` and Sub2API internal logs for the same value as `request_id`, then compare image usage and charged amount. Also provide `server_client_request_id` when present. The client cannot query administrator-only usage records itself.

## User-Facing Failure

State what is known without attributing every EOF to an origin TLS fault:

```text
The connection ended before a validated final image was received. No final file was saved locally. The server may still have completed and billed the request. Client request ID: <id>. I will not retry automatically. Verify this ID in Sub2API usage/logs before authorizing another paid request.
```

Do not reduce this to "reply regenerate." If the user authorizes a replacement, treat it as a new paid request with a new client ID and say so. Never describe it as resuming the old request because this client has no idempotent resume endpoint.

## Multiple Outputs

The OAuth bridge has a verified maximum of one output per API request. Never use `n>1`.

When the user clearly requests N separate files:

1. Resolve N unique output paths before the first request.
2. Submit requests sequentially with `n=1`.
3. After each terminal success, retain and report that file.
4. On any failure or billing ambiguity, stop before submitting the next item.
5. Report `completed_outputs=K`, `requested_outputs=N`, all completed paths, and the failed item's `client_request_id`.

A request for one composition containing multiple subjects or categories remains one paid output. If "two categories" could mean either one composition or two files, ask which result is intended before spending.

## Partial Images

The default stream sends `partial_images=0`; SSE comments and lifecycle events provide transport activity without purchasing previews. If a future client exposes paid previews, they must be explicitly requested. Partial frames are diagnostic artifacts and never satisfy an output count.
