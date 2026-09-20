# Cursor Telegram Bridge

Text your own private Telegram bot, and the message runs as a Cursor turn on
your computer. Cursor's finished answer comes back to your phone.

This bridge is Cursor-specific. It is a sibling of the [Claude Telegram
Bridge](https://github.com/ssamssae/claude-telegram-bridge), the [Codex Telegram
Bridge](https://github.com/ssamssae/codex-telegram-bridge), and the [Grok
Telegram Bridge](https://github.com/ssamssae/grok-telegram-bridge), but it does
not share runtime code with any of them.

## Read This First

**This bridge gives a chat app the ability to run commands on your computer.**

That sentence is the whole security model, so please read it slowly. When you
send a message to your bot, the bridge hands that message to Cursor, and Cursor
is allowed to use its real tools: run shell commands, read files, edit files,
run `git`. It is not a chatbot that only talks back. A line you type on a bus
can delete a file at home.

The only thing standing between the internet and your machine is **one check**:
the bridge compares the Telegram chat id of every incoming message against the
single chat id you configured, and throws away everything that does not match.
That is one line of code, and it is the entire defense. There is no allowlist of
permitted commands, no approval prompt, no sandbox, and no second factor.

So, before you install:

- **Never share your bot's token.** Anyone holding the token can read your bot's
  messages and, if they also learn your chat id, reach your computer.
- **Never add the bot to a group** and never hand it to a friend "to try". It is
  built for exactly one chat: yours.
- **Do not run it on a machine you cannot afford to have damaged.** A first-time
  setup on a spare laptop is a much better idea than your work machine.
- **Assume every message is a command.** "What's in my documents folder?" will
  make Cursor actually go and look.

If that trade is not one you want, stop here. This tool is convenient precisely
because it is unguarded, and pretending otherwise would be dishonest.

## What You Can Do With It

Once it is running, you text your bot from anywhere and Cursor does real work on
the machine at home:

| You send | What happens |
| --- | --- |
| `what changed in my project today?` | Cursor runs `git log` on your machine and summarizes it |
| `fix the typo in README.md line 4` | Cursor edits the actual file |
| `is the server still up?` | Cursor runs the check and reports back |
| a photo (or an image file) | the bridge saves it locally and hands Cursor the path |
| a plain question | Cursor just answers, no tools involved |

The conversation keeps its thread when you use the TUI lane. The bridge watches
Cursor's local transcript, so a follow-up like "now do the same for the other
file" still makes sense.

## What Comes Back To Your Phone

You do not get a wall of tool output. What you do get:

- **The finished answer**, as one or more Telegram messages.
- **A suggestion bubble + confirm button**, when the answer ends with a
  `<추천답변>…</추천답변>` marker. Tapping confirm sends that line as the next
  turn, same as the Grok bridge.
- **A typing indicator** while Cursor is still working.
- **No mid-turn tool dump.** The bridge waits until Cursor has written a
  text-only answer (and, on current Cursor builds, a turn-ended marker).

If a turn overruns the wait, or the bridge restarts while Cursor is still
writing, a late answer is recovered instead of being thrown away.

## Commands From Telegram

These work in the TUI lane (a live `cursor` tmux session):

| You send | What happens |
| --- | --- |
| `/model` | A button menu of Cursor models. Tap one and the bridge switches the Cursor session to it (`CUB_MODEL_MENU` sets the list). |
| `/model opus 5` | Switch straight to the first model whose name contains that text, no menu. |
| `/context` | Cursor's own context usage line comes back to your phone. |
| `/clear` | Starts a fresh Cursor conversation, same as typing it in the terminal. |
| `/status` | One line: is the bridge polling, is the worker alive, how deep is the queue. |

When Cursor stops on a tool-approval prompt, the bridge sends a
**▶ Run Everything** button. Tapping it answers the prompt in the terminal for
you (Cursor's Shift+Tab). If Cursor opens its model picker in the terminal,
the same `/model` menu shows up on your phone so you can finish the choice
from there.

## What You Need

- A computer where the `cursor-agent` CLI is installed and logged in. macOS is
  the path this package is written for. Linux may work if the same CLI and a
  `tmux` session are available.
- Python 3.
- `tmux`, if you want the visible TUI lane (explained below). The headless
  fallback does not need it.
- The Telegram app on your phone.

How you install and log in to `cursor-agent` is Cursor's own documentation, not
this repo's. The bridge only needs the binary on your `PATH`.

## Quick Start

Five steps. Nothing here assumes you have made a bot before.

### Step 1 — Create your own bot

Open Telegram and start a chat with [`@BotFather`](https://t.me/BotFather) — it
is Telegram's official bot for making bots.

Send it `/newbot`. It asks for a display name (anything, e.g. "My Cursor"), then
a username that must end in `bot` (e.g. `my_cursor_1234_bot`). When you are done
it replies with your token. It is one long line: a number, a colon, then a
mixed-case string of letters, digits, hyphens and underscores.

Copy it. **That token is a password.** Do not paste it into a chat, a
screenshot, a public repository, or an issue report.

### Step 2 — Find your chat id

Your chat id is the number that tells the bridge "this chat is mine, ignore
everyone else".

Send any message to your new bot (just say `hi`). Then run this on your
computer, pasting your token in place of `<YOUR_TOKEN>`:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

That command asks Telegram for the messages your bot has received. In the JSON
that comes back, find `"chat":{"id":123456789`. That number is your chat id.

If the reply looks empty (`"result":[]`), send your bot another message and run
the command again.

### Step 3 — Install

```bash
git clone https://github.com/ssamssae/cursor-telegram-bridge.git
cd cursor-telegram-bridge
```

That downloads the bridge into a folder and moves you into it. There is nothing
to compile.

### Step 4 — Save the token

The bridge reads the token from a small JSON file rather than from the command
line. Paste it at the hidden prompt below rather than typing it into a command,
so it never shows up in your shell history:

```bash
mkdir -p ~/.config/cursor-telegram-bridge
read -r -s -p "Paste your bot token, then press Enter: " CUB_BOT_TOKEN; echo
CUB_BOT_TOKEN="$CUB_BOT_TOKEN" python3 - <<'EOF'
import json, os, pathlib
path = pathlib.Path.home() / ".config" / "cursor-telegram-bridge" / "token.json"
path.write_text(json.dumps({"api_key": os.environ["CUB_BOT_TOKEN"].strip()}))
path.chmod(0o600)
print(f"wrote {path}")
EOF
unset CUB_BOT_TOKEN
```

Line by line: make the folder, read the token without echoing it to the screen,
write it into the file, `chmod 600` so only your user account can read it, then
drop it from the shell environment.

### Step 5 — Start it

```bash
export CUB_TOKEN_FILE=~/.config/cursor-telegram-bridge/token.json
export CUB_CHAT_ID=123456789          # the number you found in Step 2
python3 cursor_telegram_bridge.py
```

The first two lines tell the bridge where the token is and which chat is yours.
The third starts it. Leave the terminal window open — closing it stops the
bridge.

If a `tmux` session named `cursor` is already running (a Cursor agent you can
watch), messages are pasted into that session. If that session is missing, the
bridge falls back to one `cursor-agent -p` process per message.

### Step 6 — Say hello

Text your bot: `hello, what machine are you on?`

You should get an answer back within a few seconds. If you do, you are done.
`/start` or `/ping` also replies immediately, without starting a Cursor turn.

## Two Lanes: TUI And Headless

The bridge can drive Cursor in two ways. The difference matters mostly when
something goes wrong.

**TUI (preferred, when a session exists).** A Cursor agent stays open inside
`tmux` session `cursor`, and your messages are pasted into it. You can attach
(`tmux attach -t cursor`) and watch what is happening in real time.

**Headless fallback (default if that session is gone).** Each message runs one
`cursor-agent -p "<your message>"` process, which exits once it has answered.
Simple, and nothing is left running between messages.

To keep the TUI lane, start Cursor in tmux before the bridge:

```bash
tmux new -s cursor
# inside that pane: start your Cursor agent the way you normally do
```

Then start the bridge in another terminal. If you do not want the fallback at
all, set `CUB_TUI_FALLBACK_HEADLESS=0` — the phone will tell you the session is
down instead of silently switching lanes.

Turns you type directly in that tmux pane stay on the machine unless you turn
mirroring on. Set `CUB_TUI_MIRROR_LOCAL=1` and the bridge will also send those
questions and answers to Telegram (a short "you asked this in the terminal"
line, then the finished answer). Off by default so a first install does not
dump old terminal history onto your phone.

## Settings

Every setting is an environment variable. Only the first two are required.

| Variable | Default | What it does |
| --- | --- | --- |
| `CUB_TOKEN_FILE` | — | Path to the JSON file holding your bot token. Required. |
| `CUB_CHAT_ID` | — | The one Telegram chat id allowed to reach this machine. Required. |
| `CUB_CURSOR_BIN` | `cursor-agent` | Path to the CLI, if it is not on your `PATH`. |
| `CUB_TMUX_SESSION` | `cursor` | tmux session the TUI lane pastes into. |
| `CUB_TUI_WAIT_SEC` | `900` | Seconds to wait for one turn before giving up. |
| `CUB_TUI_WAIT_MAX_SEC` | `7200` | Hard cap: keep waiting past `CUB_TUI_WAIT_SEC` while Cursor is still running tools (no headless re-run of the same prompt). |
| `CUB_TUI_BUSY_STALL_SEC` | `900` | While extended, give up if the transcript stops growing for this long. |
| `CUB_STRIP_TRAILING_REASONING` | `1` | Drop first-person English reasoning paragraphs that Cursor sometimes appends after a non-English final answer. Set `0` to keep them. |
| `CUB_CONTEXT_CLEAR_PCT` | `50` | When the session is idle and Cursor's status line shows context usage at or above this percent, the bridge sends `/clear` for you and reports it once. `0` turns the guard off. |
| `CUB_CONTEXT_CLEAR_COOLDOWN_SEC` | `600` | Minimum seconds between two automatic `/clear`s from the context guard. |
| `CUB_TUI_FALLBACK_HEADLESS` | `1` | Set `0` to refuse work when the tmux session is gone. |
| `CUB_TUI_MIRROR_LOCAL` | `0` | Set `1` to also send turns typed in the tmux session to Telegram. |
| `CUB_MODEL_MENU` | built-in list | Comma-separated model names shown as `/model` buttons. |
| `CUB_MODEL_SURFACE` | `1` | Set `0` to turn off the `/model` menu and the picker mirror. |
| `CUB_APPROVAL_SURFACE` | `1` | Set `0` to stop sending the Run Everything button on approval prompts. |
| `CUB_PHONE_MAX_LINES` | `0` | `0` sends the whole answer. A positive number cuts the phone copy at that many lines. |
| `CUB_STATE_DIR` | `~/.cursor-telegram-bridge/state` | Where offsets and harvest cursors are kept. |
| `CUB_DRY_RUN` | `0` | Set `1` to run without calling Cursor at all. |

Cursor subscription vs API billing is **unverified** by this package. The
bridge does not claim that Telegram turns are covered by any particular Cursor
plan. Check Cursor's own billing before you leave this running.

## When It Does Not Work

**Nothing comes back at all.**
Check the terminal where you started the bridge. If it says the token file is
missing, the path in `CUB_TOKEN_FILE` is wrong. If it says the api key is empty,
the JSON file is malformed — it must be exactly `{"api_key": "..."}`.

**The bridge is running but ignores you.**
Almost always the chat id. Redo Step 2 and compare the number with what you set
in `CUB_CHAT_ID`. A message from any other chat is silently discarded by design,
so there is no error to see.

**Typing shows, then nothing.**
Cursor probably finished the turn, but the bridge is still waiting for a
text-only transcript row (or a turn-ended marker). Look at the Cursor pane. If
the answer is already there, restart the bridge — a late answer is harvested on
the next start.

**`cursor-agent: command not found`.**
The bridge could not find the CLI. Point `CUB_CURSOR_BIN` at the full path.

**It answers, but very slowly, or times out at 900 seconds.**
Cursor is probably investigating with tools rather than just answering. Narrow
the job — "read file X and tell me Y" is faster than "figure out what is
wrong". If the answer lands after the wait, the bridge still tries to send it
rather than dropping it.

## What This Is Not

- It is not multi-user. One chat id, one machine.
- It is not a sandbox. Cursor runs with your user account's full permissions.
- It does not ask before acting. There is no approval prompt.
- It does not share code with the Claude, Codex, or Grok bridges, so their
  settings, slash commands, and safety behaviors do not carry over.
- It does not verify Cursor billing. See Settings above.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, matching the sibling bridges.

### Suggested follow-ups: on/off

`CUB_SUGGESTED_TAIL_PROMPT=1` (default) asks for a useful optional next request
on ordinary Telegram prompts. Set it to `0` and restart the bridge to stop adding
that instruction. Slash commands and direct terminal input remain unchanged.
Keep `CUB_SUGGESTED_REPLY_SPLIT=1` to render any model-produced tag as a final
suggestion bubble with **확인** (Confirm). Turning SPLIT off leaves raw markers
in answers; it does not disable generation.

Confirm pastes and submits the suggestion to Cursor; it does not merely copy to
the clipboard. The button changes to **✅ 보냄** and repeated clicks do not resend.
Clear invalidates old buttons. No useful next action means no suggestion is required.
Older builds ignored the generation switch; this build honors both values.
