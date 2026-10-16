# Public startup verification

The public package supports `CUB_TRANSPORT=tui`. The visible tmux session is used
when available; the existing headless fallback remains configurable. IDE/ACP
adapters are not bundled. Those transport settings now stop before workers start.

The v0.4.5 export omitted surface, model, reset and flow settings. It also tried
to load the unbundled IDE adapter during TUI startup. Use the startup-fix commit
or a later release containing it. Do not install the unpatched v0.4.5 archive.

## Reproducible checks

Run `python3 -m unittest discover -s tests -p 'test_*.py'` with an isolated HOME.
`test_public_startup.py` supplies temporary state and dummy chat settings. It
stubs threads and external boundaries; no model, tmux or Telegram is invoked.

- Default dry startup reaches its return without a missing-setting exception.
- Normal TUI startup reaches the poller without importing the IDE adapter.
- IDE, ACP and unknown transports stop before constructing workers.
- Referenced module globals are defined; controls retain their documented defaults.
- Progress flags resolve under the explicit public state directory.

These are isolated regressions. A live acceptance check still needs a private
bot message, the intended Cursor session, its answer and the returned Telegram
message. A passing startup test does not establish a live round trip.

Merging this repository does not install or restart an existing bridge. No
GitHub deployment workflow is configured in this repository.
