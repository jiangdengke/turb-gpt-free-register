# 整理并提交项目 Trellis 配置

## Goal

将项目级 `.pi/` 与 `.trellis/` 工作流、规范和脚本纳入 Git 版本控制，同时把依赖安装目录与本地运行时状态加入忽略规则，消除无意提交的噪声。

## Requirements

* 保留并提交项目级 `.pi/` 配置、Trellis agents、skills、prompts 和扩展源码。
* 保留并提交 `.trellis/` 配置、脚本、spec、任务文档和 workspace 记录。
* 忽略 `.pi/npm/node_modules/`、`.trellis/.runtime/`、`.trellis/.developer` 等机器本地生成内容。
* 不修改现有业务代码或生产配置。
* 提交后推送到 `origin/main`。

## Acceptance Criteria

* [ ] `git status` 不再显示本次相关未跟踪文件。
* [ ] `.pi` 和 `.trellis` 项目文件已被 Git 跟踪。
* [ ] 依赖目录与 Trellis 运行时目录被 `.gitignore` 或目录级忽略规则排除。
* [ ] `git diff --check` 通过。
* [ ] 提交已推送，`HEAD` 与 `origin/main` 一致。

## Out of Scope

* 不提交 API Key、Token、密码、Cookie、SQLite 或运行时账号数据。
* 不修改应用功能，不重新部署 Oracle。

## Technical Notes

* Root ignore rules protect project-wide local runtime/dependency paths.
* `.pi/npm/.gitignore` already ignores its `node_modules`; root rules should make this intent explicit.
* `.trellis/.gitignore` already ignores Trellis runtime state; keep those rules intact.
