# Directory Structure

> How backend code is organized in this project.

---

## Overview

This repository keeps Flask route definitions in `webui/app.py`, SQLite and storage access in `core/db.py`, reusable domain transforms in `core/`, and focused tests in `tests/`. HTML templates with embedded JavaScript live in `webui/templates/`.

## Directory Layout

```
webui/app.py                 # Flask API routes and app wiring
webui/templates/*.html       # modern and Legacy WebUI templates
core/db.py                   # SQLite-backed storage helpers
core/*.py                    # domain services and transforms
tests/test_*.py              # unittest coverage
```

## Module Organization

Keep route-specific request validation and response construction in `webui/app.py`. Reuse storage helpers from `core/db.py`; do not read SQLite tables directly from templates. Sensitive values should be loaded through existing secret helpers only after the route has matched the requested record.

## Naming Conventions

Use snake_case for Python functions and JSON fields. Keep paired modern/Legacy template behavior and endpoint names aligned.

## Examples

* `webui/app.py:api_codex_export_local_format` demonstrates a selected-filename batch download route.
* `webui/app.py:_account_secret_value` demonstrates on-demand sensitive field access.
