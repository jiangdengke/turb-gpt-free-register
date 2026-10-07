# Logging Guidelines

> How logging is done in this project.

---

## Overview

The project uses Python logging from backend modules. Logs support task diagnosis but must not become a second channel for sensitive exports.

## Log Levels

Use `info` for state transitions, `warning` for recoverable provider/task failures, and `error` for unexpected failures. Keep messages concise and actionable.

## Structured Logging

Include operation names, counts, filenames, or stable identifiers only when they are needed for diagnosis. Bound untrusted strings before placing them in response metadata or logs.

## What to Log

Log aggregate export counts and fixed skip categories if operational visibility is needed. Log neither exported lines nor account payloads.

## What NOT to Log

Never log passwords, TOTP secrets, access tokens, refresh tokens, OAuth callback values, API keys, cookies, or complete credential JSON. Skip reasons for credential export must contain only filenames and fixed non-sensitive reason labels.
