# Codex 授权页增加登录凭据导出

## Goal

在 Codex 授权页面为选中的授权记录增加“导出登录凭据”操作，导出对应账号的邮箱、ChatGPT 注册密码和 TOTP/2FA 密钥。复用现有账号敏感字段按需读取机制，避免把密码或 2FA 密钥加入普通列表响应。

## What I already know

* 用户希望在截图所示的 Codex 授权页面增加导出操作。
* Codex 页面已有“下载本地”“导出CPA”“导出Sub2”“从CPA下载”等批量操作。
* Codex 记录以凭证文件名为选择标识，并包含邮箱字段；账号表以邮箱保存登录密码和 TOTP secret。
* 账号页已经有 `login_credentials` 敏感字段，当前格式为 `邮箱---密码---2FA密钥`。
* 现有 `/api/accounts/<id>/secret` 与 `/api/accounts/secret-bulk` 采用按需读取方式。
* 当前账号密码来源是 `extra.registration_password`，2FA 来源是 `totp_secret`。
* 现代模板与 Legacy 模板都存在，Codex 页面和账号敏感操作需要保持前端行为一致。

## Requirements

* 在现代 Codex 授权页面增加批量“导出登录凭据”按钮。
* 仅处理当前选中的 Codex 授权记录，并按其邮箱匹配账号记录。
* 导出内容包含：邮箱、账号密码、TOTP/2FA secret。
* 不在 Codex 普通列表 API 中返回密码或 TOTP secret。
* 对未匹配账号、密码为空或 2FA 为空的记录采用明确、可追踪的跳过/占位策略。
* 保持敏感下载响应 `no-store` 与 `nosniff`。
* Legacy 页面同步提供同等能力，或明确复用同一后端接口与格式。

## Decision (ADR-lite)

**Context**: Codex 页面按凭证文件名选择记录，但账号密码和 TOTP 只应在需要时读取。

**Decision**: 沿用账号页现有 TXT 格式，每行输出 `邮箱---密码---2FA密钥`。接口按 Codex 记录邮箱匹配账号；密码缺失输出现有占位值 `未设置`，2FA 缺失保留为空；无法匹配账号的记录跳过，并通过结构化跳过信息和前端提示说明原因。

**Consequences**: 不新增导出格式，兼容现有账号凭据处理；导出仍然是敏感下载，必须使用 `no-store` 和 `nosniff`。批量选择中可能出现部分成功，前端需要显示成功数和跳过数。

## Acceptance Criteria

* [x] Codex 页面可选择记录并点击“导出登录凭据”。
* [x] 下载文件按一行一个账号生成，并包含邮箱、密码、2FA secret。
* [x] 选择中包含无对应账号的 Codex 记录时，接口返回可理解的跳过信息，不泄露额外敏感数据。
* [x] 密码和 2FA 不出现在普通 Codex 列表响应或日志中。
* [x] 现代和 Legacy 前端均通过 JavaScript 语法检查。
* [x] 后端接口有覆盖成功、缺失匹配账号和敏感响应头的测试。

## Definition of Done

* Tests added or updated for API and frontend wiring.
* `py_compile`, embedded JavaScript checks, targeted tests, and `git diff --check` pass.
* No credentials, tokens, passwords, TOTP secrets, or API keys are written to logs, fixtures, commits, or chat output.
* Rollback remains a code-only revert; no production deployment is performed in this task.

## Out of Scope

* 不修改 Codex OAuth 流程。
* 不导出 access token、refresh token、CPA auth-file 或 Sub2API 文件中的 OAuth 凭证。
* 不新增真实账号或真实登录验证。
* 不自动替用户执行生产部署。

## Technical Notes

* Relevant backend: `webui/app.py`, `core/db.py`.
* Export route should only read account secrets after matching the selected Codex record email; ordinary `/api/codex` responses remain compact and secret-free.
* Relevant frontend: `webui/templates/index.html`, `webui/templates/index_legacy.html`.
* Existing account secret implementation: `_account_secret_value(..., "login_credentials")`, `/api/accounts/secret-bulk`.
* Existing Codex batch action endpoint: `/api/codex/...` routes accept selected credential filenames.
