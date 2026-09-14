# cursor-telegram-bridge 0.4.3

Telegram-facing Cursor bridge updates from mid-September 2026.

## Changes

- Suggested-reply prompt tail is off by default; chips still come from `<추천답변>` in answers when present.
- Busy-session stop keywords (e.g. stop / 멈춰) interrupt the current Cursor turn with Ctrl+C, then paste the new message instead of only queuing.
- Directive/tmux injects send an immediate “received” card on Telegram; local terminal prompts can mirror to Telegram more promptly.
- Markdown tables in answers are converted to bullet lists for Telegram instead of being dropped.

## Validation

- Public export suite: `unittest discover` (10 tests) OK
- Export privacy / chat-id / dangling-helper gates: OK
