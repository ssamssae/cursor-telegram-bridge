# Cursor Telegram Bridge 0.4.5

- Scope repeated-final suppression to the same turn, so identical answers in later turns can still be delivered.
- Allow a failed final delivery to be retried; only confirmed sends update the duplicate record.
- Add persistent English/Korean bridge controls and remove retired suggested-reply prompt instructions.
- Synchronize current reply recovery and discard policy-suppressed progress records.

Validation: public export, private-data/token scan, manifest parity, and 24 tests (4 other-engine skips). A source bundle is included; install all shared modules alongside the bridge.

This is a GitHub release. It does not upload to PyPI or restart an installed bridge.
