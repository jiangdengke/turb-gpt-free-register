# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

Changes spanning API routes and embedded WebUI JavaScript require focused backend tests, template syntax checks, and a cross-layer review.

## Forbidden Patterns

* Do not include passwords, TOTP secrets, access tokens, or OAuth payloads in ordinary list responses, logs, fixtures, or skip metadata.
* Do not add a second implementation of existing account secret formatting when `_account_secret_value(..., "login_credentials")` already provides the contract.
* Do not treat a browser-side download as proof that the backend applied the selected-record and account-match checks.

## Required Patterns

* Validate batch request bodies and cap list sizes.
* Match selected Codex records to accounts by normalized email before reading sensitive fields.
* Use `no-store` and `nosniff` on sensitive download responses, including all-skipped error responses.
* Keep modern and Legacy templates behaviorally equivalent.

## Testing Requirements

Add focused tests for successful export, missing account matches, all-skipped 422 responses, auth protection, sensitive headers, ordinary-list secret exclusion, and both template controls. Run `py_compile`, extract each template's script into a `.js` file for `node --check`, and run `git diff --check`.

## Code Review Checklist

* [ ] Existing storage and secret helpers are reused.
* [ ] Partial and total failure behavior is explicit.
* [ ] No secret values appear in logs or test fixtures.
* [ ] Both templates compile independently.
* [ ] Existing CPA/Sub2/raw Codex exports remain covered.
