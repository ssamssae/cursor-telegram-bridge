# Web guide verification

The guide is static. To check a local copy without running the bridge:

```sh
python3 -m http.server 0 --bind 127.0.0.1 --directory docs
```

Open the printed localhost address. The page header contains the English / 한국어 menu.

1. Select each language. The title, headings, instructions, navigation and footer change immediately.
2. Reload and confirm the selected language stays active. Open `?lang=ko` or `?lang=en` to override a stored choice.
3. Follow the section navigation and check that related guide links carry the selected language.
4. At a 390px viewport, confirm the language menu is visible and the text fits. Code examples may scroll horizontally.
5. With browser storage blocked, confirm immediate switching still works. Persistence is not expected in this mode.

The guide does not log in, run AI actions or change Telegram settings. Website language and `/language` commands are separate.

Initial local verification: 2026-10-06, shared generator and Aside desktop/390×844 previews.
Public hosting is a separate verification step: after publishing, repeat the language checks at the actual HTTPS URL and confirm the JS/CSS assets load.

## 한국어 확인 경로

로컬 설명서를 열어 상단 언어 메뉴 → 본문·목차 즉시 변경 → 새로고침 뒤 선택 유지 → `?lang=ko` 직접 진입을 확인합니다.
390px 화면에서 메뉴·본문이 잘리지 않는지 확인하고, 공개 게시 후에도 실제 HTTPS 주소에서 같은 순서로 확인합니다.
로컬 확인만으로 공개 게시나 설치된 브릿지 동작을 완료로 판단하지 않습니다.

## Release 0.4.5 verification

Public export and regression suite: 24 tests (4 other-engine skips). Run `python3 -m unittest discover -s tests` in a clean checkout. Tests use isolated state and captured senders; they do not send Telegram messages or reset a live session. Source assets are verified against the release tag, and release downloads are checked against `SHA256SUMS`.
Final-delivery regressions cover the same text in different turns and a retry after a failed send.
GitHub Pages rebuilds the existing `main:/docs` guide after merge. This static publication does not run a bridge service.

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

Merging rebuilds the existing static GitHub Pages guide. It does not install
or restart an existing bridge service.
