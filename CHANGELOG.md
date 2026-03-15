# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

- test(recovery): Add end-to-end recovery-path coverage for active compression rotation, stream empty-response compact retry, and non-stream context-overflow compact retry.
- test(resilience): Add end-to-end resilience coverage for outbound-queue overflow behavior, per-user loop serialization, and Feishu streaming failure-path log classification.
- test(concurrency-observability): Add chain-level assertions for cross-user parallel handling and streaming trace metadata consistency (`reply_to_id` on progress/end path).
- fix(qq): Download incoming image attachments and inject image paths so QQ channel can perform image analysis via `image_read`.
- fix(stdio): Prewarm ACP initialization/authentication at gateway start to reduce first-message latency.

## v0.4.3 - 2026-03-14

- feat(ralph): Add /ralph loop with PRD approval, subagent iterations, progress streaming, and stop/status controls.

## v0.4.2 - 2026-03-13

- fix(media): Save inbound images to workspace/images for Feishu/Telegram/Discord and normalize media paths before invoking iflow.

## v0.4.1 - 2026-03-13

- fix(cron): Auto-fill `deliver/channel/to` for chat `/cron add` when omitted so scheduled messages reply in the same chat by default.
- fix(cron): Refresh `cron list` to recompute next-run state and update timeout/missed run status.
- docs: Clarify chat cron defaults in command help.
- feat(chat): Slash commands for status/help/new/compact/cron/model/skills/language with improved parsing & output.

## v0.3.8 - 2026-03-11

- fix(config): Raise default driver timeout to 600s for new installs and migrated configs.

## v0.3.6 - 2026-03-10

- fix(streaming): Use final response content when no stream chunks were emitted to avoid false empty-output fallback.

## v0.3.5 - 2026-03-09

- refactor(cross-platform): Remove shell-script runtime paths in favor of Python-managed startup/test entrypoints, including Docker entrypoint migration and MCP proxy script cleanup.
- fix(windows): Add a unified command resolver for `npm`/`iflow` with explicit Windows shim support (`.cmd`/`.bat`), reducing reliance on implicit shell behavior and aligning command execution paths.
- fix(feishu): Keep streaming enabled but degrade to plain text when interactive card patch/create both fail, while simplifying streaming card content to reduce Feishu-side instability.
- test(feishu): Add streaming delivery tests covering patch success, recreate success, text fallback, and streaming-end cleanup.
- chore(feishu): Add compact streaming observability logs for patch/create/fallback decisions and content length to speed up production debugging.
- fix(session): Ensure `/new` clears stdio runtime session state completely, including mapped session ids, loaded-session cache, and queued rehydrate history, so users actually get a fresh conversation.
- test(session): Add regression tests for stdio session clearing to prevent `/new` from leaving stale runtime context behind.
- tweak(compression): Lower the default proactive session compression trigger from `88888` to `60000` tokens so long-running chats rotate earlier instead of relying almost entirely on overflow/empty-response recovery.
- fix(feishu): Improve channel `post` parsing by recursively extracting nested text/link/image/file references from rich-text messages and downloading embedded post resources when keys are available.
- test(feishu): Add post-parsing regression tests covering nested resource extraction and inbound media collection from Feishu channel posts.
- test(e2e): Add end-to-end loop flow coverage for non-stream, `/new`, streaming progress/end, and empty-stream fallback paths.
- test(observability): Add log-level assertions for key loop signals (`New chat requested`, `Streaming produced empty output`).

## v0.3.4 - 2026-03-06

- fix(cli): Resolve version from installed package metadata first to avoid `v0.0.0` on Windows/installed runs.
- fix(stdio-acp): Keep receive loop alive on oversized chunks (`Separator is not found, and chunk exceed the limit`), avoiding unexpected gateway exit on Windows.
- fix(cli): Add console symbol fallback for non-Unicode terminals (GBK/Windows), preventing startup crashes caused by `UnicodeEncodeError`.
