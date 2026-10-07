# Error Handling

> How errors are handled in this project.

---

## Overview

Sensitive export endpoints return structured JSON errors for validation and all-skipped requests. Successful downloads use `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`; all-skipped sensitive export errors use the same headers because the request concerns credential material.

For partial success, return the file and expose only non-sensitive counts and fixed skip reasons. Never include passwords, TOTP secrets, access tokens, or credential payloads in error metadata or logs.

## Error Types

The WebUI uses Flask responses for API errors. Domain-specific conversion code may raise custom `ValueError`-style exceptions that routes convert to JSON responses.

## Error Handling Patterns

Validate request shape and list limits before reading storage. For batch operations, isolate each selected item, append a fixed reason for skipped items, and continue when safe. Return HTTP 422 when every selected item is skipped.

## API Error Responses

* Invalid JSON object or empty/oversized filename list: HTTP 400 with `{ok: false, error}`.
* No selected record can be exported: HTTP 422 with `{ok: false, error, exported_count, skipped_count, skipped[]}`.
* Partial export: HTTP 200 download with count headers and bounded, non-sensitive skip metadata.

```python
response.headers["Cache-Control"] = "no-store"
response.headers["X-Content-Type-Options"] = "nosniff"
return response, 422
```

## Common Mistakes

* Returning a normal JSON error without sensitive-download headers for an all-skipped credential export.
* Putting account payloads or secret values into skip reasons.
