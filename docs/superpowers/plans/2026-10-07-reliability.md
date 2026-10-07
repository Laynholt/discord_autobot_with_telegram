# Bot reliability implementation plan

> Execution: superpowers:executing-plans, sequentially in this session. One commit per numbered fix.

**Goal:** Resolve all 16 findings in the 2026-10-07 audit, then stop both clients cleanly on terminal termination.
**Architecture:** Keep the existing two-client design. Add durable delivery status, atomic JSON storage and explicit task ownership; keep all network boundaries mockable.
**Tech stack:** Python 3.13, asyncio, aiogram, discord-py-self, standard-library unittest.
**Spec:** C:/Users/Mike/.codex/artifacts/telegram_bot_for_discord/2026-10-07/audit.md and the user's request.

## Constraints and review focus

No push, server restart, real messages, new production dependencies or unrelated refactoring. Each task gets a failing reproduction, its minimal fix, a passing offline check and a commit. Check persistence failures, partial deliveries, cancellation during waits, stale FSM callbacks and restart recovery. Signals apply to this process; stdin EOF applies only to an interactive foreground terminal.

## Tasks

For each row: write the named regression in tests/test_reliability.py (or tests/test_lifecycle.py), run it red, implement, run the relevant suite green, inspect git diff --check and commit. tests/test_scheduler.py must continue to pass.

| # | Files / interface | Required assertion / implementation |
|---|---|---|
| 1 | telegram_bot/bot.py; DelayedMessage.status, retry_delayed_message_callback | Failed send keeps JSON and attachments, status failed, manual retry button; failed predecessors do not block later jobs. |
| 2 | utils.py atomic_write_json; Telegram save/load and mutation callers | Failed replace preserves original; corrupt load raises without overwriting; failed mutations roll back and do not confirm success. |
| 3 | Telegram download_file | Same original filename gives different paths and preserves both contents. |
| 4 | Telegram save_attachments_callback | Explicit Save survives reload; write failure keeps draft and reports error. |
| 5 | main.py main; Telegram run wrapper | Either main task ending cancels and awaits its sibling; failure exits nonzero. |
| 6 | Telegram load/_restore; status missed | Overdue job survives with files and is not sent automatically; explicit retry/reschedule works. |
| 7 | Discord shared senders; durable next-part progress | Retry second part never repeats confirmed first text/file part; preserve progress across job retries. |
| 8 | Discord file sender | Missing file returns failure before any text/file is sent. |
| 9 | Discord channel resolution | Cache miss fetches channel; restored jobs wait for readiness. |
| 10 | Telegram navigation/FSM attachment drafts | Back clears state; cancel discards draft files and never alters committed attachments. |
| 11 | Telegram parse/finalize/edit date | Reject explicit past dates and dates elapsed during upload; time-only rolls to tomorrow. |
| 12 | Telegram shared safe text output and paged lists | 4096-char text, markup and long filenames cannot break menus; list bounded and paginated. |
| 13 | Discord random time | 11:59:58 and 11:59:59 stay within the window without throwing. |
| 14 | Discord workday send | Wake at 12:01, weekend, disabled or already sent does not deliver. |
| 15 | Discord settings persistence using atomic_write_json | Restart restores text, enabled state, pause, manual time and successful date. |
| 16 | Discord auto send/retries; owner notification | Failed attempt never records success; retries bounded within window; exhausted attempts notify once. |
| 17 | main.py signals/stdin; both controllers close | Ctrl+C/SIGINT, Ctrl+Z/SIGTSTP, SIGTERM and interactive Ctrl+D/EOF stop owned tasks and clients; noninteractive EOF does not stop service. |

## Verification

Prerequisite discovered while importing the locked runtime: discord-py-self 2.0.1 fails on Python 3.13 (CachedSlotProperty signature). Task 1 also updates this dependency to 2.1.0 and its required curl-cffi/cffi versions; other locked versions stay unchanged. Tests use Python 3.13.3 with the resulting lock.

Use the isolated Python 3.13 environment outside the repository with locked dependencies. Run `python -m unittest discover -s tests`, `python tests/test_scheduler.py`, compileall and git diff --check. Validate POSIX signals/EOF in Linux if available; record any platform limitation. Final independent review is read-only and focuses on the whole resulting change.
