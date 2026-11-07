# Cursor Telegram Bridge 0.4.8

- While a tool-approval card or a choice card waits for your answer, the bridge pins that card in your private chat without a notification. Later messages can keep arriving; the pinned bar at the top of the chat keeps the open card within reach. Answering the card, from Telegram or in the terminal, unpins that one card, and so does the card closing or its expiry after one hour. The bridge pins only cards it sent to this chat, never clears pins you made yourself, makes at most one pin or unpin call per poll tick, and retries a failed call up to three times, one minute apart. Pinning is presentation only; it never answers a card or grants an approval.
- The pin ledger sits next to the other state files as `cursor-bridge-<name>.question-pins.json`. Ownership is written before each call, so a lost reply still ends with that card alone being unpinned, and a bridge restart keeps a confirmed pin without pinning or sending the card again.
- The shared helper module `terminal_turn_mirror.py` matches the sibling bridges again: the handoff card writer accepts an optional log hook that a sibling bridge uses to record whether its card went out. The Cursor bridge does not pass one, so nothing changes for Cursor.
- The regression test `tests/test_cursor_question_pins.py` ships with the public suite.

Validation: public export with the internal-symbol audit, private-data and token scans with two independent scanners (no new matches against 0.4.7), manifest parity, and 44 tests (4 other-engine skips, 1 skip for the ACP lane that the public build does not bundle) on Python 3.14. A source bundle is included; install all shared modules alongside the bridge.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
