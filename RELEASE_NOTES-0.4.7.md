# Cursor Telegram Bridge 0.4.7

- Each turn now edits one progress card, like the Claude bridge, instead of sending every tool step as its own message. The card shows the request, the update time, the last six steps and the current step, and closes as finished, stopped or timed out. Each step reads as one sentence.
- Stop and tool-approval buttons match the Claude bridge. The approval card lists the modal's own options (run once, skip, add to allowlist, run everything) with the command and reason, and the key is sent only after the screen is checked again.
- A message sent during a turn waits in Cursor's follow-ups box. The bridge sends one card for the queued messages with an apply-now button, which presses Enter once per queued message on an empty composer. The card closes itself when the queue goes in on its own.
- Final answers and narration reply to your Telegram message.
- AI handoff cards use the shared layout, and queued input from a Claude session is received. Handoffs reach every native agent endpoint again, fresh Cursor sessions are verified first, and session ownership is checked by numeric ID.
- Long UTF-8 text is pasted in paced frames and submitted once.
- Replies sent to the chat no longer include decorative emoji.
- The public startup controls added in 0.4.6 now come from the export itself. No behaviour change.

Validation: public export with the internal-symbol audit, private-data and token scans with two independent scanners, manifest parity, and 30 tests (4 other-engine skips) on Python 3.14. A source bundle is included; install all shared modules alongside the bridge.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
