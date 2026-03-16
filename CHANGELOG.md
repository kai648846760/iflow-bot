# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

## v0.4.5 - 2026-03-16

- fix(ralph): Seed a minimal FastAPI route test during Ralph verification prep so fresh API scaffolds no longer fall into recovery only because `pytest` collected zero tests.
- fix(ralph): Strip internal completion markers from user-facing Ralph completion summaries while keeping `[RALPH_DONE]` only in persisted progress state.
- test(ralph): Add regression coverage for FastAPI scaffold verification seeding and user-visible summary sanitization in `tests/test_ralph_runtime_guards.py`.
- docs(ralph): Refresh README and README_CN with the exact supported Ralph commands, approval flow, status fields, auto-resume behavior, and non-blocking main-chat guarantees verified in Feishu E2E.

## v0.4.4 - 2026-03-16

- feat(ralph): Add `/ralph resume`, richer `/ralph status` progress details, natural-language progress query routing, and automatic resume after gateway restart for unfinished runs.
- fix(ralph): Run Ralph subagents on a dedicated stdio adapter so the main chat session stays responsive while long tasks execute.
- fix(ralph): Add execution watchdogs and recovery retries for active prompts that never go idle, including targeted dependency hints such as missing `httpx` in verification flows.
- fix(ralph): Synthesize missing acceptance criteria and tighter artifact path detection so researcher/writer/engineer stories produce usable deliverables more reliably.
- feat(chat): Add a waiting hint for slow first streaming chunks and keep progress/status text aligned with the configured chat language.
- docs: Expand chat command and Ralph workflow documentation in both README files, including exact supported slash commands.

## v0.4.3 - 2026-03-15

- test(recovery): Add end-to-end recovery-path coverage for active compression rotation, stream empty-response compact retry, and non-stream context-overflow compact retry.
- test(resilience): Add end-to-end resilience coverage for outbound-queue overflow behavior, per-user loop serialization, and Feishu streaming failure-path log classification.
- test(concurrency-observability): Add chain-level assertions for cross-user parallel handling and streaming trace metadata consistency (`reply_to_id` on progress/end path).
- fix(qq): Download incoming image attachments and inject image paths so QQ channel can perform image analysis via `image_read`.
- fix(stdio): Prewarm ACP initialization/authentication at gateway start to reduce first-message latency.

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
