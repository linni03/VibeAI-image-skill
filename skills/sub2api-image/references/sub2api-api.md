# Sub2API Images API

## Endpoints

Use a base URL ending in `/v1`. The client normalizes a bare origin by appending `/v1` and avoids duplicate `/v1/v1` segments.

| Operation | Method and path | Body |
| --- | --- | --- |
| Generate | `POST /v1/images/generations` | JSON |
| Edit | `POST /v1/images/edits` | `multipart/form-data` |

Authenticate with `Authorization: Bearer <Sub2API user key>`. Never use an upstream account credential.

The current Sub2API gateway does not expose `/images/generations/async`, `/images/edits/async`, or `/images/tasks/{id}`. A `404` for those paths is not evidence that object storage is misconfigured; those routes are not part of this client contract.

## Generate Request

The client sends these OpenAI-compatible fields:

```json
{
  "model": "gpt-image-2",
  "prompt": "...",
  "size": "1024x1024",
  "n": 1,
  "response_format": "b64_json",
  "output_format": "png"
}
```

Optional fields are omitted unless requested. `b64_json` avoids an object-storage dependency and is decoded in memory without writing base64 to a log or intermediate file.

## Edit Request

Send the same scalar fields as multipart fields. Send the input as `image` and an optional mask as `mask`. The client accepts PNG, JPEG, and WebP and checks file magic before upload.

## Response

Successful responses contain `data[]` entries with either `b64_json` or `url`. The client supports base64, data URLs, and HTTP(S) URLs, then writes each image atomically. It recognizes PNG, JPEG, and WebP by file content rather than trusting a requested extension.

Sub2API may also return top-level `model`, `size`, `output_format`, and `usage`. The report preserves safe metadata but never includes response image payloads or authorization data.

## Errors

| Status | Meaning | Action |
| --- | --- | --- |
| `400` | Invalid prompt, model option, size, or multipart input | Fix the named field; do not retry unchanged |
| `401` | Invalid or revoked Sub2API user key | Reconfigure with a valid user key |
| `403` | The key's group cannot generate images | Enable image generation on the group or use the correct key |
| `404` | Wrong base URL/path or unavailable model route | Check the normalized base URL and deployed Sub2API version |
| `429` | User/group/account rate or concurrency limit | Respect `Retry-After`, then retry only with user approval |
| `5xx` | Sub2API or upstream account failure | Report the request ID and safe server message |

Timeouts are ambiguous: the upstream might have completed after the client disconnected. Do not automatically repeat a paid request. Ask before retrying.

## Billing Verification

The client reports requested and actual image tiers. A normal user API key cannot query administrator usage logs. The Sub2API operator must separately verify `image_count`, `image_size`, `image_size_source`, `image_size_breakdown`, and the charged amount in the admin usage log.
