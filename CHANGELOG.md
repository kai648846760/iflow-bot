# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- refactor(cross-platform): Remove shell-script runtime paths in favor of Python-managed startup/test entrypoints, including Docker entrypoint migration and MCP proxy script cleanup.
- fix(windows): Add a unified command resolver for `npm`/`iflow` with explicit Windows shim support (`.cmd`/`.bat`), reducing reliance on implicit shell behavior and aligning command execution paths.
- fix(feishu): Keep streaming enabled but degrade to plain text when interactive card patch/create both fail, while simplifying streaming card content to reduce Feishu-side instability.
- test(feishu): Add streaming delivery tests covering patch success, recreate success, text fallback, and streaming-end cleanup.
- chore(feishu): Add compact streaming observability logs for patch/create/fallback decisions and content length to speed up production debugging.

## v0.3.4 - 2026-03-06

- fix(cli): Resolve version from installed package metadata first to avoid `v0.0.0` on Windows/installed runs.
- fix(stdio-acp): Keep receive loop alive on oversized chunks (`Separator is not found, and chunk exceed the limit`), avoiding unexpected gateway exit on Windows.
- fix(cli): Add console symbol fallback for non-Unicode terminals (GBK/Windows), preventing startup crashes caused by `UnicodeEncodeError`.

