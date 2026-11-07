#!/usr/bin/env python3
"""Set up the Cursor Telegram Bridge on this computer. Standard library only.

    python3 bridge_setup.py setup                          guided setup
    python3 bridge_setup.py setup --like-author            the author's setup in one go
    python3 bridge_setup.py setup --like-author --dry-run  show what would happen, change nothing
    python3 bridge_setup.py attach                         same as the cursor-tg command
    python3 bridge_setup.py run                            what the background service runs
    python3 bridge_setup.py doctor                         check the install any time (--offline: no Telegram)

Setup saves the bot token and your chat id, writes bridge.env (the previous one is
kept as a backup), starts Cursor inside the tmux session the bridge drives,
registers a background service (launchd on macOS, systemd --user on Linux),
links the cursor-tg command and sends one test message to your phone. `doctor`
checks an install at any time and changes nothing.
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

APP_NAME = "cursor-telegram-bridge"
APP_DIR = Path(__file__).resolve().parent
SETUP_SCRIPT = APP_DIR / "bridge_setup.py"
BRIDGE_SCRIPT = APP_DIR / "cursor_telegram_bridge.py"
ATTACH_COMMAND = "cursor-tg"
ATTACH_SCRIPT = APP_DIR / ATTACH_COMMAND
DOCTOR_COMMAND = "python3 bridge_setup.py doctor"
SERVICE_NAME = f"{APP_NAME}.service"
LAUNCHD_LABEL = f"com.user.{APP_NAME}"
# The line every service file setup writes carries. A file without it was not
# written by this setup, so setup keeps a copy before it writes over that file.
SERVICE_FILE_MARKER = f"Written by bridge_setup.py for {APP_NAME}"
DEFAULT_SOCKET = "default"
DEFAULT_SESSION = "cursor"
SESSION_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
CHAT_ID_RE = re.compile(r"-?[0-9]+")
EXAMPLE_CHAT_ID = "123456789"  # the placeholder in config.example.env
TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on"})  # the bridge's own reading of a boolean setting
AUTHOR_PRESET_QUESTION = "Use the author's setup (recommended)? [Y/n] "
# "Like the author": the switches where the author's own bridge differs from the
# public defaults in config.example.env. Every other setting already matches.
AUTHOR_PRESET_ENV: tuple[tuple[str, str], ...] = (
    ("CUB_TUI_MIRROR_LOCAL", "1"),
    ("CUB_FLOW_MIRROR", "1"),
    ("CUB_CONTEXT_CLEAR_PCT", "0"),
)
AUTHOR_PRESET_NOTES = {
    "CUB_TUI_MIRROR_LOCAL": "turns you type in the Cursor window also reach your phone",
    "CUB_FLOW_MIRROR": "live progress card with a Stop button, for phone and terminal turns",
    "CUB_CONTEXT_CLEAR_PCT": "never sends /clear on its own",
}
CHAT_ID_WAIT_SECONDS = 180.0
SELF_CHECK_PHONE_TIMEOUT = 60.0
SELF_CHECK_TERMINAL_TIMEOUT = 90.0
SELF_CHECK_PROBE = "Reply with the single word OK. (bridge setup self-check)"
FINAL_SENT_RE = re.compile(r"send final sent=[1-9]")
TERMINAL_MIRROR_MARKERS = ("local mirror tick", "local mirror answers=")
CURSOR_PROCESS_RE = re.compile(r"(?:^|/)cursor-agent(?=\s|/|$)")
NO_TTY = {"?", "??", "-", ""}
# Cursor is showing a question (workspace trust, a tool approval, a letter menu or
# the model picker). The bridge never types into one, and neither does setup.
WAITING_HINT_RE = re.compile(
    r"(?i)use\s+arrow\s+keys|press\s+the\s+key\s+shown|enter\s+to\s+select|workspace\s+trust|"
    r"do\s+you\s+trust|run\s+this\s+command\?|skip\s+&\s+tell\s+the\s+agent"
)
WAITING_OPTION_RE = re.compile("[▶▸]\\s*\\[[A-Za-z]\\]")
PHONE_TEXT = {
    "test": {
        "en": "Cursor Telegram Bridge is set up on this computer. Send /ping, then a normal Cursor prompt.",
        "ko": "이 컴퓨터에 Cursor Telegram Bridge 설정을 마쳤습니다. /ping 을 보낸 뒤 평소처럼 Cursor 에 요청하세요.",
    },
    "check": {
        "en": (
            "Self-check 2/3: send any text to this bot within {seconds} s. It should appear in the "
            "Cursor window and the answer should come back here."
        ),
        "ko": "자가 점검 2/3: {seconds}초 안에 이 봇에 아무 글이나 보내 주세요. Cursor 창에 들어가고 답이 여기로 오면 통과입니다.",
    },
    "waiting": {
        "en": "Cursor is waiting for an answer in its window (for example whether to trust a folder); answer it first.",
        "ko": "Cursor 창이 답을 기다리고 있습니다(예: 폴더 신뢰 여부). 먼저 답해 주세요.",
    },
}

ApiCall = Callable[..., "dict[str, Any] | None"]
RunCommand = Callable[[list], subprocess.CompletedProcess]
Which = Callable[[str], "str | None"]


class SetupError(RuntimeError):
    """A setup step failed. The message is safe to print: it never holds the token."""


@dataclass(frozen=True)
class SetupOptions:
    config_file: Path
    token_file: Path
    state_dir: Path
    link_file: Path
    service_file: Path | None = None
    chat_id: str | None = None
    language: str = "en"
    like_author: bool = False
    dry_run: bool = False
    non_interactive: bool = False


def out(message: str = "") -> None:
    print(message, flush=True)


def ok(message: str) -> None:
    out(f"ok: {message}")


def warn(message: str) -> None:
    out(f"warning: {message}")


def fail(message: str) -> None:
    out(f"error: {message}")


def note(message: str) -> None:
    out(f"  {message}")


def step(number: int, title: str) -> None:
    out("")
    out(f"Step {number}/5 - {title}")


def default_config_dir() -> Path:
    return Path.home() / ".config" / APP_NAME


def default_config_file() -> Path:
    return default_config_dir() / "bridge.env"


def default_token_file() -> Path:
    return default_config_dir() / "token.json"


def default_state_dir() -> Path:
    return Path.home() / f".{APP_NAME}" / "state"


def default_link_file() -> Path:
    return Path.home() / ".local" / "bin" / ATTACH_COMMAND


def default_service_file(os_name: str) -> Path | None:
    if os_name == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if os_name == "Linux":
        return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME
    return None


def expand_path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(str(raw))).expanduser()


def run_command(cmd: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def write_private_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write through a temp file created with `mode`, so the content is never world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    return candidate


def load_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped, surrounding quotes removed."""
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = os.path.expanduser(os.path.expandvars(value))
    return values


def telegram_call(token: str, method: str, timeout: int = 60, **params: Any) -> dict[str, Any] | None:
    if "timeout_param" in params:
        params["timeout"] = params.pop("timeout_param")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(params).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:  # a refused token still comes back as Telegram's JSON answer
        try:
            with exc:
                payload = json.load(exc)
        except Exception:  # noqa: BLE001 - no JSON body becomes "no answer"
            return None
    except Exception:  # noqa: BLE001 - network errors become "no answer"
        return None
    return payload if isinstance(payload, dict) else None


def validate_bot_token(token: str, api_call: ApiCall) -> str:
    payload = api_call(token, "getMe", timeout=30)
    if not payload or not payload.get("ok"):
        raise SetupError("Telegram did not accept that bot token. Copy it again from @BotFather.")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    username = str(result.get("username") or "").strip()
    if not username:
        raise SetupError("Telegram returned no bot username for that token.")
    return username


def read_token_file(path: Path) -> str:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("api_key") or "").strip() if isinstance(payload, dict) else ""


def latest_update_offset(token: str, api_call: ApiCall) -> int:
    payload = api_call(token, "getUpdates", timeout=10, timeout_param=0)
    updates = payload.get("result") if payload and payload.get("ok") else None
    if not isinstance(updates, list):
        return 0
    ids = [item.get("update_id") for item in updates if isinstance(item, dict)]
    valid = [int(item) for item in ids if isinstance(item, int)]
    return max(valid) + 1 if valid else 0


def private_chat_of(update: dict[str, Any]) -> str | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
        return None
    chat = message["chat"]
    if chat.get("type") != "private" or chat.get("id") is None:
        return None
    return str(chat["id"])


def wait_for_chat_id(
    token: str,
    *,
    offset: int,
    api_call: ApiCall,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout_seconds: float = CHAT_ID_WAIT_SECONDS,
) -> str:
    started = clock()
    while clock() - started < timeout_seconds:
        payload = api_call(token, "getUpdates", timeout=20, offset=offset, timeout_param=10)
        updates = payload.get("result") if payload and payload.get("ok") else None
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("update_id"), int):
                offset = int(update["update_id"]) + 1
            chat_id = private_chat_of(update)
            if chat_id:
                return chat_id
        sleep(1)
    raise SetupError(
        f"No /start arrived from a private chat within {int(timeout_seconds)} s. "
        "Open your bot in Telegram, send /start, then run setup again."
    )


def phone_text(key: str, language: str) -> str:
    texts = PHONE_TEXT[key]
    return texts.get(language) or texts["en"]


def send_text(token: str, chat_id: str, text: str, api_call: ApiCall) -> bool:
    payload = api_call(token, "sendMessage", timeout=30, chat_id=chat_id, text=text)
    return bool(payload and payload.get("ok"))


def resolve_cursor_bin(config: dict[str, str], which: Which) -> str | None:
    for candidate in (config.get("CUB_CURSOR_BIN"), "cursor-agent", "agent"):
        if candidate:
            found = which(candidate)
            if found:
                return found
    return None


def service_path(tmux_bin: str | None, cursor_bin: str | None) -> str:
    """PATH for the service: launchd and systemd start with a minimal one, but the bridge calls tmux by name."""
    entries: list[str] = []
    for binary in (cursor_bin, tmux_bin, sys.executable):
        if binary:
            entries.append(str(Path(binary).parent))
    entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    entries.extend(["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    unique: list[str] = []
    for entry in entries:
        if entry and entry not in unique:
            unique.append(entry)
    return os.pathsep.join(unique)


def bridge_env_text(
    *,
    token_file: Path,
    chat_id: str,
    state_dir: Path,
    cursor_bin: str,
    path_value: str,
    language: str,
    like_author: bool,
) -> str:
    lines = [
        "# Cursor Telegram Bridge settings, written by `python3 bridge_setup.py setup`.",
        "# Plain KEY=VALUE lines read by `bridge_setup.py run` (the background service), not a shell script.",
        "# Change a value, then restart the service.",
        f"CUB_TOKEN_FILE={token_file}",
        f"CUB_CHAT_ID={chat_id}",
        f"CUB_STATE_DIR={state_dir}",
        f"CUB_CURSOR_BIN={cursor_bin}",
        f"CUB_TMUX_SOCKET={DEFAULT_SOCKET}",
        f"CUB_TMUX_SESSION={DEFAULT_SESSION}",
        f"CUB_LANGUAGE={language}",
        f"PATH={path_value}",
    ]
    if like_author:
        lines.append("# Author preset (setup --like-author)")
        for key, value in AUTHOR_PRESET_ENV:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def write_bridge_env(config_file: Path, text: str) -> Path | None:
    """Write bridge.env (0600). An existing file is copied to bridge.env.bak-<time> first."""
    backup = None
    if config_file.exists():
        backup = backup_path(config_file)
        shutil.copy2(config_file, backup)
        os.chmod(backup, 0o600)
    write_private_text(config_file, text)
    return backup


def start_cursor_session(*, tmux_bin: str, socket: str, session: str, cursor_bin: str, run: RunCommand,
                         workdir: Path) -> None:
    """Start Cursor in a new detached tmux session and keep its window at index 0 (the bridge pane)."""
    started = run([tmux_bin, "-L", socket, "new-session", "-d", "-s", session, "-n", "Cursor",
                   "-c", str(workdir), shlex.quote(cursor_bin)])
    if started.returncode != 0:
        detail = (started.stderr or started.stdout or f"exit {started.returncode}").strip()
        raise SetupError(f"could not start Cursor in tmux session {socket}/{session}: {detail}")
    windows = run([tmux_bin, "-L", socket, "list-windows", "-t", f"={session}", "-F", "#{window_index}"])
    first = ((windows.stdout or "").split() or ["0"])[0]
    if first != "0":
        run([tmux_bin, "-L", socket, "move-window", "-s", f"={session}:{first}", "-t", f"={session}:0"])


def ensure_cursor_session(*, tmux_bin: str, socket: str, session: str, cursor_bin: str, run: RunCommand,
                          workdir: Path) -> bool:
    """Create the session when it is missing. A running session is kept as it is. True = created."""
    if run([tmux_bin, "-L", socket, "has-session", "-t", f"={session}"]).returncode == 0:
        return False
    start_cursor_session(tmux_bin=tmux_bin, socket=socket, session=session, cursor_bin=cursor_bin, run=run,
                         workdir=workdir)
    return True


def link_attach_command(link_file: Path, target: Path = ATTACH_SCRIPT) -> str:
    try:
        mode = target.stat().st_mode
        if not mode & 0o100:
            target.chmod(mode | 0o111)
    except OSError:
        pass
    if link_file.is_symlink():
        current = Path(os.readlink(link_file))
        if current == target:
            return "kept"
        if current.name != ATTACH_COMMAND:
            warn(f"{link_file} points somewhere else; left it alone. Run {target} directly instead.")
            return "blocked"
        link_file.unlink()
    elif link_file.exists():
        warn(f"{link_file} already exists and is not a link; left it alone. Run {target} directly instead.")
        return "blocked"
    link_file.parent.mkdir(parents=True, exist_ok=True)
    link_file.symlink_to(target)
    return "linked"


def service_command(config_file: Path) -> list[str]:
    return [sys.executable or "python3", str(SETUP_SCRIPT), "run", "--config", str(config_file)]


def launchd_plist_bytes(command: list[str], log_file: Path) -> bytes:
    body = plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
    })
    marker = f"<!-- {SERVICE_FILE_MARKER}; setup replaces this file. -->\n".encode()
    return body.replace(b"<plist ", marker + b"<plist ", 1)


def systemd_quote(value: str) -> str:
    """One ExecStart word: `%` is a unit specifier and `$` a variable, so both are doubled."""
    value = value.replace("%", "%%").replace("$", "$$")
    if value and re.fullmatch(r"[A-Za-z0-9_@%+=:,./$-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_unit_text(command: list[str]) -> str:
    return "\n".join([
        f"# {SERVICE_FILE_MARKER}; setup replaces this file.",
        "[Unit]",
        "Description=Cursor Telegram Bridge",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(systemd_quote(part) for part in command),
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def service_file_is_ours(path: Path) -> bool:
    """True when setup wrote this service file: it carries SERVICE_FILE_MARKER."""
    try:
        return SERVICE_FILE_MARKER.encode() in path.read_bytes()
    except OSError:
        return False


def back_up_foreign_service_file(path: Path, *, ask: Callable[[str], str] | None, backup_dir: Path | None) -> bool:
    """Before setup writes `path`: copy a file that setup did not write, and say so.

    Interactive setup asks first. Returns False when you answer n; setup then leaves
    the file and the service alone. The copy goes next to bridge.env, not beside the
    service file, so launchd or systemd never loads it again.
    """
    if not path.exists() or service_file_is_ours(path):
        return True
    question = f"{path} exists and was not written by bridge_setup.py. Back it up and replace it? [Y/n] "
    if ask is not None and ask(question).strip().lower() in {"n", "no"}:
        warn(f"kept {path}; the background service was not installed or restarted")
        return False
    backup = backup_path((backup_dir or default_config_dir()) / path.name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    warn(f"{path} was not written by bridge_setup.py; saved a copy as {backup} before replacing it")
    return True


def install_service(
    *,
    config_file: Path,
    state_dir: Path,
    service_file: Path | None,
    os_name: str,
    run: RunCommand,
    sleep: Callable[[float], None],
    ask: Callable[[str], str] | None = None,
    backup_dir: Path | None = None,
) -> tuple[str, bool]:
    """Register and (re)start the background service. Returns (kind, started)."""
    command = service_command(config_file)
    service_file = service_file or default_service_file(os_name)
    if os_name == "Darwin" and service_file is not None:
        if not back_up_foreign_service_file(service_file, ask=ask, backup_dir=backup_dir):
            return "launchd", False
        service_file.parent.mkdir(parents=True, exist_ok=True)
        service_file.write_bytes(launchd_plist_bytes(command, state_dir / "bridge.log"))
        domain = f"gui/{getattr(os, 'getuid', lambda: 0)()}"
        run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        for attempt in range(3):
            loaded = run(["launchctl", "bootstrap", domain, str(service_file)])
            if loaded.returncode == 0:
                ok(f"launchd service started: {LAUNCHD_LABEL} ({service_file})")
                return "launchd", True
            if attempt < 2:
                sleep(1)
        warn(f"launchd did not start {LAUNCHD_LABEL}: {(loaded.stderr or loaded.stdout or '').strip()}")
        return "launchd", False
    if os_name == "Linux" and service_file is not None:
        if not back_up_foreign_service_file(service_file, ask=ask, backup_dir=backup_dir):
            return "systemd", False
        service_file.parent.mkdir(parents=True, exist_ok=True)
        service_file.write_text(systemd_unit_text(command), encoding="utf-8")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", SERVICE_NAME])
        started = run(["systemctl", "--user", "restart", SERVICE_NAME])
        if started.returncode == 0:
            ok(f"systemd user service started: {SERVICE_NAME} ({service_file})")
            return "systemd", True
        warn(f"systemd did not start {SERVICE_NAME}: {(started.stderr or started.stdout or '').strip()}")
        return "systemd", False
    warn(f"no background service for {os_name}; start the bridge yourself with: {shlex.join(command)}")
    return "manual", False


def log_hint_for(os_name: str, state_dir: Path) -> str:
    if os_name == "Linux":
        return f"journalctl --user -u {SERVICE_NAME}"
    return str(state_dir / "bridge.log")


class BridgeLogTail:
    """New bridge log text since construction: the launchd log file on macOS, the journal on Linux."""

    def __init__(self, os_name: str, log_file: Path, *, since: float, run: RunCommand = run_command) -> None:
        self.os_name = os_name
        self.run = run
        self.since = since
        self.path = None if os_name == "Linux" else log_file
        self.offset = self.path.stat().st_size if self.path is not None and self.path.exists() else 0
        self.seen = 0

    def read_new(self) -> str:
        if self.path is not None:
            if not self.path.exists():
                return ""
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read()
            self.offset += len(data)
            return data.decode("utf-8", "replace")
        proc = self.run(["journalctl", "--user", "-u", SERVICE_NAME, "--since", f"@{int(self.since)}",
                         "-o", "cat", "--no-pager"])
        text = proc.stdout or ""
        fresh = text[self.seen:]
        self.seen = len(text)
        return fresh


def pane_waiting_for_answer(screen: str) -> bool:
    """True while Cursor shows a question in its window; typed keys would answer it."""
    text = screen or ""
    return bool(WAITING_HINT_RE.search(text) or WAITING_OPTION_RE.search(text))


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def author_self_check(
    *,
    token: str,
    chat_id: str,
    tmux_bin: str,
    config: dict[str, str],
    log_tail: Any,
    log_hint: str,
    api_call: ApiCall,
    run: RunCommand,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    phone_timeout: float = SELF_CHECK_PHONE_TIMEOUT,
    terminal_timeout: float = SELF_CHECK_TERMINAL_TIMEOUT,
    test_message_sent: bool = True,
    language: str = "en",
) -> list[str]:
    """Three live checks after an author-preset install. The result lines go to the terminal and the phone."""
    out("")
    out("Self-check: bot -> phone, phone -> terminal -> phone, terminal -> phone")
    if test_message_sent:
        lines = ["1/3 bot -> phone: ok (test message delivered)"]
    else:
        lines = ["1/3 bot -> phone: the test message was not delivered; check CUB_CHAT_ID in bridge.env"]
    state_dir = Path(config.get("CUB_STATE_DIR") or default_state_dir())
    health_file = state_dir / f"cursor-bridge-{config.get('CUB_NAME') or 'cursor'}.health.json"
    socket = config.get("CUB_TMUX_SOCKET") or DEFAULT_SOCKET
    session = config.get("CUB_TMUX_SESSION") or DEFAULT_SESSION
    target = f"={session}:0.0"

    def cursor_is_waiting() -> bool:
        screen = run([tmux_bin, "-L", socket, "capture-pane", "-p", "-t", target]).stdout or ""
        return pane_waiting_for_answer(screen)

    started = clock()
    request = phone_text("check", language).format(seconds=int(phone_timeout))
    if cursor_is_waiting():
        request += "\n" + phone_text("waiting", language)
        note(f"Cursor is waiting for an answer in its window; answer it with {ATTACH_COMMAND} or with the card on your phone first.")
    send_text(token, chat_id, request, api_call)
    note(f"waiting up to {int(phone_timeout)} s for a message from your phone ...")
    arrived = answered = False
    while True:
        if FINAL_SENT_RE.search(log_tail.read_new()):
            answered = True
        try:
            job_started = float(read_json(health_file).get("last_job_started_at") or 0)
        except (TypeError, ValueError):
            job_started = 0.0
        arrived = arrived or job_started >= started
        if answered or clock() - started >= phone_timeout:
            break
        sleep(2)
    if answered:
        lines.append("2/3 phone -> terminal -> phone: ok")
    elif arrived:
        lines.append(
            f"2/3 phone -> terminal -> phone: the message reached Cursor but no answer came back in "
            f"{int(phone_timeout)} s; look at the Cursor window with {ATTACH_COMMAND}"
        )
    else:
        lines.append(
            f"2/3 phone -> terminal -> phone: no phone message within {int(phone_timeout)} s; send any text "
            f"to the bot later, and if nothing comes back read the bridge log: {log_hint}"
        )

    waiting_before_probe = cursor_is_waiting()
    if waiting_before_probe:
        mirrored = None
    else:
        run([tmux_bin, "-L", socket, "send-keys", "-t", target, "-l", SELF_CHECK_PROBE])
        sleep(0.5)
        run([tmux_bin, "-L", socket, "send-keys", "-t", target, "Enter"])
        note(f"typed a probe into the Cursor window; waiting up to {int(terminal_timeout)} s for its phone copy ...")
        started = clock()
        mirrored = False
        while True:
            chunk = log_tail.read_new()
            if any(marker in chunk for marker in TERMINAL_MIRROR_MARKERS):
                mirrored = True
                break
            if clock() - started >= terminal_timeout:
                break
            sleep(2)
    if mirrored is None:
        lines.append(
            "3/3 terminal -> phone: skipped; Cursor is waiting for an answer in its window, so nothing was typed. "
            f"Answer it with {ATTACH_COMMAND}, then type any prompt there to see it on your phone"
        )
    elif mirrored:
        lines.append("3/3 terminal -> phone: ok")
    else:
        lines.append(
            f"3/3 terminal -> phone: no terminal mirror within {int(terminal_timeout)} s; check that bridge.env "
            f"has CUB_TUI_MIRROR_LOCAL=1 and that the service restarted, then read the bridge log: {log_hint}"
        )
    for line in lines:
        out(line)
    send_text(token, chat_id, "Setup self-check\n" + "\n".join(lines), api_call)
    return lines


def tty_name(value: str) -> str:
    value = value.strip()
    return value[len("/dev/"):] if value.startswith("/dev/") else value


def cursor_processes_outside(session_ttys: set[str] | list[str], ps_text: str) -> list[tuple[str, str]]:
    """(pid, tty) of Cursor agents on a terminal that is not part of the bridge session; one per terminal.

    `ps_text` is `ps -Ao pid=,tty=,args=` output. Processes without a terminal
    (services, the bridge's own one-shot runs) are ignored.
    """
    inside = {tty_name(tty) for tty in session_ttys if tty and tty.strip()}
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for raw in ps_text.splitlines():
        parts = raw.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, args = parts
        if tty in NO_TTY or not CURSOR_PROCESS_RE.search(args):
            continue
        name = tty_name(tty)
        if name in inside or name in seen:
            continue
        seen.add(name)
        found.append((pid, tty))
    return found


def cursor_processes_inside(session_ttys: set[str] | list[str], ps_text: str) -> list[tuple[str, str]]:
    """(pid, tty) of Cursor agents on a terminal of the bridge session; ps_text as above."""
    inside = {tty_name(tty) for tty in session_ttys if tty and tty.strip()}
    found: list[tuple[str, str]] = []
    for raw in ps_text.splitlines():
        parts = raw.split(None, 2)
        if len(parts) < 3 or parts[1] in NO_TTY or tty_name(parts[1]) not in inside:
            continue
        if CURSOR_PROCESS_RE.search(parts[2]):
            found.append((parts[0], parts[1]))
    return found


def warn_cursor_outside_session(*, tmux_bin: str, socket: str, session: str, run: RunCommand,
                                report: Callable[[str], None] = warn) -> int:
    """Warn (setup's last step, and doctor) about Cursor running on a terminal the phone cannot see."""
    panes = run([tmux_bin, "-L", socket, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_tty}"])
    ttys = set((panes.stdout or "").split()) if panes.returncode == 0 else set()
    processes = run(["ps", "-A", "-ww", "-o", "pid=,tty=,args="])
    outside = cursor_processes_outside(ttys, processes.stdout or "")
    if not outside:
        return 0
    listed = ", ".join(f"pid {pid} on {tty}" for pid, tty in outside)
    report(
        f"Cursor is also running outside the bridge tmux session ({listed}). Your phone cannot see that "
        f"window; use {ATTACH_COMMAND} so the phone and the terminal share one Cursor session."
    )
    return len(outside)


def setup_plan_lines(options: SetupOptions, *, os_name: str, tmux_bin: str | None, cursor_bin: str | None) -> list[str]:
    service_file = options.service_file or default_service_file(os_name)
    lines = [
        "Dry run: nothing is written, no service is touched, and Telegram is not contacted.",
        f"config: {options.config_file} (an existing file is backed up first)",
        f"token file: {options.token_file}",
        f"state dir: {options.state_dir}",
    ]
    if options.like_author:
        lines.append("author preset switches (bridge.env):")
        lines.extend(f"  {key}={value}  # {AUTHOR_PRESET_NOTES[key]}" for key, value in AUTHOR_PRESET_ENV)
    else:
        lines.append("switches: public defaults (config.example.env)")
    lines.append(f"cursor-agent: {cursor_bin or 'not found on PATH (setup stops here)'}")
    if tmux_bin:
        lines.append(f"tmux: {tmux_bin}")
    elif options.like_author:
        lines.append("tmux: not found on PATH (the author's setup stops here)")
    else:
        lines.append("tmux: not found on PATH (each message runs one headless cursor-agent)")
    lines.append(f"tmux session: {DEFAULT_SOCKET}/{DEFAULT_SESSION} with Cursor started inside; a running session is kept")
    if service_file is not None:
        kind = "launchd" if os_name == "Darwin" else "systemd --user"
        lines.append(f"service ({kind}): {service_file} -> {shlex.join(service_command(options.config_file))}")
    else:
        lines.append(f"service: none on {os_name}; run {shlex.join(service_command(options.config_file))}")
    lines.append(f"attach any time with: {ATTACH_COMMAND} (link: {options.link_file} -> {ATTACH_SCRIPT})")
    lines.append("test message: one message to your chat")
    if options.like_author:
        lines.append(
            f"self-check after install: bot -> phone, phone -> terminal -> phone ({int(SELF_CHECK_PHONE_TIMEOUT)} s), "
            f"terminal -> phone ({int(SELF_CHECK_TERMINAL_TIMEOUT)} s)"
        )
    replaced = files_setup_writes_over(options, os_name=os_name)
    if replaced:
        lines.append("files already there that setup writes over:")
        lines.extend(f"  {item}" for item in replaced)
    else:
        lines.append("files already there that setup writes over: none")
    return lines


def files_setup_writes_over(options: SetupOptions, *, os_name: str) -> list[str]:
    """Files already on disk that setup would write over, each with what happens to it. Reads only.

    A saved token file is not one of them: setup reuses it unless you paste a new token.
    """
    found: list[str] = []
    if options.config_file.exists():
        found.append(f"{options.config_file} (a copy is kept first)")
    service_file = options.service_file or default_service_file(os_name)
    if service_file is not None and service_file.exists():
        if service_file_is_ours(service_file):
            found.append(str(service_file))
        else:
            found.append(
                f"{service_file} (not written by bridge_setup.py: a copy goes to {options.config_file.parent} first)"
            )
    return found


def obtain_token(options: SetupOptions, *, ask: Callable[[str], str], read_secret: Callable[[str], str]) -> tuple[str, bool]:
    """(token, is_new). A saved token file is reused; otherwise the token is read without echo."""
    saved = read_token_file(options.token_file)
    if saved:
        if options.non_interactive:
            return saved, False
        answer = ask(f"Use the saved bot token in {options.token_file}? [Y/n] ").strip().lower()
        if answer not in {"n", "no"}:
            return saved, False
    if options.non_interactive:
        raise SetupError(
            f"no bot token in {options.token_file}. Save it there first (README, Step 4) "
            "or run setup without --non-interactive."
        )
    token = read_secret("Paste your BotFather token (hidden), then press Enter: ").strip()
    if not token:
        raise SetupError("no token entered")
    return token, True


def setup_bridge(
    options: SetupOptions,
    *,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    which: Which = shutil.which,
    ask: Callable[[str], str] = input,
    read_secret: Callable[[str], str] = getpass.getpass,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    os_name: str | None = None,
    log_tail_factory: Callable[..., Any] = BridgeLogTail,
) -> int:
    os_name = os_name or platform.system()
    existing = load_env_file(options.config_file)
    tmux_bin = which("tmux")
    cursor_bin = resolve_cursor_bin(existing, which)
    if options.dry_run:
        out("Cursor Telegram Bridge setup (dry run)")
        for line in setup_plan_lines(options, os_name=os_name, tmux_bin=tmux_bin, cursor_bin=cursor_bin):
            out(line)
        return 0
    try:
        return _setup(options, existing=existing, tmux_bin=tmux_bin, cursor_bin=cursor_bin, api_call=api_call,
                      run=run, ask=ask, read_secret=read_secret, sleep=sleep, clock=clock, os_name=os_name,
                      log_tail_factory=log_tail_factory)
    except SetupError as exc:
        fail(str(exc))
        return 2


def _setup(options: SetupOptions, *, existing: dict[str, str], tmux_bin: str | None, cursor_bin: str | None,
           api_call: ApiCall, run: RunCommand, ask: Callable[[str], str], read_secret: Callable[[str], str],
           sleep: Callable[[float], None], clock: Callable[[], float], os_name: str,
           log_tail_factory: Callable[..., Any]) -> int:
    out("Cursor Telegram Bridge setup")
    if not options.like_author and not options.non_interactive:
        note(
            "The author's setup also mirrors turns typed in the Cursor window to your phone, shows the "
            "progress card with its Stop button, never sends /clear on its own, and checks both directions "
            "once at the end. Answer n to keep the public defaults."
        )
        if ask(AUTHOR_PRESET_QUESTION).strip().lower() not in {"n", "no"}:
            options = dataclasses.replace(options, like_author=True)
    if options.language not in {"en", "ko"}:
        raise SetupError("language must be en or ko")
    if not cursor_bin:
        raise SetupError("cursor-agent not found on PATH. Install Cursor's CLI, log in once, then run setup again.")
    if not tmux_bin and options.like_author:
        raise SetupError("the author's setup needs tmux. Install it (macOS: brew install tmux), then run setup again.")

    step(1, "Bot token")
    token, token_is_new = obtain_token(options, ask=ask, read_secret=read_secret)
    username = validate_bot_token(token, api_call)
    ok(f"Telegram accepted the token for @{username}")

    step(2, "Your chat id")
    chat_id = (options.chat_id or existing.get("CUB_CHAT_ID") or "").strip()
    if chat_id:
        ok(f"using chat id {chat_id}")
    elif options.non_interactive:
        raise SetupError("no chat id: pass --chat-id, or run setup interactively and send /start to your bot")
    else:
        offset = latest_update_offset(token, api_call)
        out(f"Open Telegram, find @{username}, and send /start in a private chat. Waiting ...")
        chat_id = wait_for_chat_id(token, offset=offset, api_call=api_call, sleep=sleep, clock=clock)
        ok(f"found your chat id: {chat_id}")
    if not CHAT_ID_RE.fullmatch(chat_id):
        raise SetupError(f"chat id must be a number, got {chat_id!r}")

    step(3, "Private settings")
    options.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if token_is_new:
        write_private_text(options.token_file, json.dumps({"api_key": token}) + "\n")
        ok(f"saved the token (readable by you only): {options.token_file}")
    options.state_dir.mkdir(parents=True, exist_ok=True)
    backup = write_bridge_env(
        options.config_file,
        bridge_env_text(
            token_file=options.token_file, chat_id=chat_id, state_dir=options.state_dir, cursor_bin=cursor_bin,
            path_value=service_path(tmux_bin, cursor_bin), language=options.language,
            like_author=options.like_author,
        ),
    )
    if backup:
        ok(f"previous settings kept in {backup}")
    ok(f"wrote {options.config_file}")
    if options.like_author:
        for key, value in AUTHOR_PRESET_ENV:
            note(f"{key}={value}: {AUTHOR_PRESET_NOTES[key]}")

    step(4, "Cursor inside tmux, and the cursor-tg command")
    if tmux_bin:
        created = ensure_cursor_session(tmux_bin=tmux_bin, socket=DEFAULT_SOCKET, session=DEFAULT_SESSION,
                                        cursor_bin=cursor_bin, run=run, workdir=Path.home())
        ok(("started Cursor in" if created else "kept the running") + f" tmux session {DEFAULT_SOCKET}/{DEFAULT_SESSION}")
    else:
        warn("tmux not found: each message runs one headless cursor-agent. Install tmux for the visible Cursor window.")
    linked = link_attach_command(options.link_file)
    if linked in {"linked", "kept"}:
        ok(f"{ATTACH_COMMAND} -> {ATTACH_SCRIPT}")
        if str(options.link_file.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            note(f"{options.link_file.parent} is not on your PATH; add it, or run {ATTACH_SCRIPT}")

    step(5, "Background service")
    kind, started = install_service(config_file=options.config_file, state_dir=options.state_dir,
                                    service_file=options.service_file, os_name=os_name, run=run, sleep=sleep,
                                    ask=None if options.non_interactive else ask,
                                    backup_dir=options.config_file.parent)

    sent = send_text(token, chat_id, phone_text("test", options.language), api_call)
    if sent:
        ok("sent a test message to your phone")
    else:
        warn("the test message did not go through; check the chat id")
    out("")
    out(f"Setup complete. Watch or type in the same Cursor session any time: {ATTACH_COMMAND}")
    out(f"Then send /ping to @{username}.")
    if options.like_author:
        if started:
            author_self_check(
                token=token, chat_id=chat_id, tmux_bin=tmux_bin, config=load_env_file(options.config_file),
                log_tail=log_tail_factory(os_name, options.state_dir / "bridge.log", since=clock(), run=run),
                log_hint=log_hint_for(os_name, options.state_dir), api_call=api_call, run=run, sleep=sleep,
                clock=clock, test_message_sent=sent, language=options.language,
            )
        else:
            note(f"Self-check skipped: the {kind} service is not running.")
    if tmux_bin:
        warn_cursor_outside_session(tmux_bin=tmux_bin, socket=DEFAULT_SOCKET, session=DEFAULT_SESSION, run=run)
    out(f"Check the bridge again any time: {DOCTOR_COMMAND}")
    return 0


def attach_command(
    config_file: Path,
    *,
    run: RunCommand = run_command,
    which: Which = shutil.which,
    exec_fn: Callable[[str, list[str], dict[str, str]], Any] = os.execve,
    environ: dict[str, str] | None = None,
    create_missing: bool = True,
) -> int:
    """Attach this terminal to the Cursor session the bridge drives (`cursor-tg`)."""
    env = dict(os.environ if environ is None else environ)
    config = load_env_file(config_file)
    tmux_bin = which("tmux")
    if not tmux_bin:
        fail(f"tmux not found on PATH; install tmux, then run {ATTACH_COMMAND} again")
        return 2
    socket = config.get("CUB_TMUX_SOCKET") or DEFAULT_SOCKET
    session = config.get("CUB_TMUX_SESSION") or DEFAULT_SESSION
    if not SESSION_NAME_RE.fullmatch(session):
        fail(f"CUB_TMUX_SESSION must be letters, digits, _ or -: {session!r}")
        return 2
    target = f"={session}"
    if run([tmux_bin, "-L", socket, "has-session", "-t", target]).returncode != 0:
        if not create_missing:
            fail(f"tmux session missing: {socket}/{session}")
            return 2
        cursor_bin = resolve_cursor_bin(config, which)
        if not cursor_bin:
            fail(f"cursor-agent not found on PATH; install Cursor's CLI, then run {ATTACH_COMMAND} again")
            return 2
        try:
            start_cursor_session(tmux_bin=tmux_bin, socket=socket, session=session, cursor_bin=cursor_bin, run=run,
                                 workdir=Path.home())
        except SetupError as exc:
            fail(str(exc))
            return 2
        ok(f"started Cursor in tmux session {socket}/{session}")
    clients = run([tmux_bin, "-L", socket, "list-clients", "-t", target])
    if (clients.stdout or "").strip():
        note(
            "Another terminal is already attached; this one joins it and nobody is detached. "
            "The window keeps the smaller size until the other terminal leaves (Ctrl+B, then D)."
        )
    inside = env.get("TMUX", "").split(",")[0]
    if inside:
        server = (run([tmux_bin, "-L", socket, "display-message", "-p", "-t", target, "#{socket_path}"]).stdout
                  or "").strip()
        if server and os.path.realpath(inside) == os.path.realpath(server):
            exec_fn(tmux_bin, [tmux_bin, "-L", socket, "switch-client", "-t", target], env)
            return 0
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
    exec_fn(tmux_bin, [tmux_bin, "-L", socket, "attach-session", "-t", target], env)
    return 0


def run_bridge(config_file: Path, *, exec_fn: Callable[[str, list[str], dict[str, str]], Any] = os.execve) -> int:
    """Start the bridge with bridge.env applied on top of the current environment (the service entry point)."""
    config = load_env_file(config_file)
    if not config:
        fail(f"config file missing or empty: {config_file}; run `python3 {SETUP_SCRIPT} setup` first")
        return 2
    env = dict(os.environ)
    env.update(config)
    python = sys.executable or "python3"
    exec_fn(python, [python, str(BRIDGE_SCRIPT)], env)
    return 0


class DoctorReport:
    """One [ok], [warn] or [fail] line per check. Any failure makes doctor exit with 1."""

    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        out(f"[ok] {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        out(f"[warn] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        out(f"[fail] {message}")


def current_user() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except (OSError, KeyError):  # no USER and no password entry
        return ""


def doctor_private_file(report: DoctorReport, label: str, path: Path) -> None:
    """[ok] when only your user can read the file; [warn] with the chmod that fixes it otherwise."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        report.warn(f"{label}: could not read the permissions of {path} ({exc.strerror or exc})")
        return
    if mode & 0o077:
        report.warn(f"{label} is open to other users (mode {mode:o}); fix it with: chmod 600 {shlex.quote(str(path))}")
    else:
        report.ok(f"{label}: {path} (only you can read it)")


def doctor_telegram(report: DoctorReport, token: str, *, offline: bool, api_call: ApiCall) -> None:
    """One getMe call. A refused token fails; no answer at all (offline, DNS, timeout) only warns."""
    if offline:
        note("Telegram not contacted (--offline).")
        return
    payload = api_call(token, "getMe", timeout=15)
    if not payload:
        report.warn("Telegram did not answer; check the network, or skip this check with --offline")
        return
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if payload.get("ok"):
        report.ok(f"Telegram accepts the token: @{result.get('username') or '?'}")
        return
    reason = str(payload.get("description") or "refused").replace(token, "<token>")[:80]
    report.fail(f"Telegram refused the token ({reason}); copy it again from @BotFather and run setup")


def doctor_tmux(report: DoctorReport, config: dict[str, str], *, run: RunCommand, which: Which) -> None:
    """tmux, the bridge session, Cursor inside it, and Cursor on any other terminal."""
    socket = config.get("CUB_TMUX_SOCKET") or DEFAULT_SOCKET
    session = config.get("CUB_TMUX_SESSION") or DEFAULT_SESSION
    headless = (config.get("CUB_TUI_FALLBACK_HEADLESS") or "1").strip().lower() in TRUE_WORDS
    tmux_bin = which("tmux")
    if not tmux_bin:
        message = "tmux not found (macOS: brew install tmux, Debian or Ubuntu: sudo apt install tmux)"
        if headless:
            report.warn(f"{message}; each message runs one headless cursor-agent, with no Cursor window to share")
        else:
            report.fail(f"{message}; with CUB_TUI_FALLBACK_HEADLESS=0 messages from your phone get an error")
        return
    report.ok(f"tmux: {tmux_bin}")
    if run([tmux_bin, "-L", socket, "has-session", "-t", f"={session}"]).returncode != 0:
        message = f"tmux session {socket}/{session} is not running; start it with {ATTACH_COMMAND}"
        if headless:
            report.warn(f"{message}. Until then each message runs one headless cursor-agent")
        else:
            report.fail(f"{message}. Until then messages from your phone get an error")
        return
    report.ok(f"tmux session {socket}/{session} is running")
    panes = run([tmux_bin, "-L", socket, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_tty}"])
    ttys = set((panes.stdout or "").split()) if panes.returncode == 0 else set()
    if not ttys:
        report.warn(f"could not read the panes of {socket}/{session}; look at it with {ATTACH_COMMAND}")
        return
    inside = cursor_processes_inside(ttys, run(["ps", "-A", "-ww", "-o", "pid=,tty=,args="]).stdout or "")
    if inside:
        report.ok(f"Cursor is running in the bridge session {socket}/{session} (pid {inside[0][0]})")
    else:
        report.warn(f"no Cursor agent in the bridge session {socket}/{session}; look at it with {ATTACH_COMMAND}")
    outside = warn_cursor_outside_session(tmux_bin=tmux_bin, socket=socket, session=session, run=run,
                                          report=report.warn)
    if not outside:
        report.ok("no Cursor agent runs on another terminal")


def doctor_linger(report: DoctorReport, *, run: RunCommand, user: str) -> None:
    """Linux: without linger, systemd stops your user services when you log out. Doctor never turns it on."""
    shown = run(["loginctl", "show-user", user, "-p", "Linger"])
    linger = ""
    for line in (shown.stdout or "").splitlines():
        if line.strip().startswith("Linger="):
            linger = line.strip()[len("Linger="):].strip().lower()
    if linger == "yes":
        report.ok("linger: on, so the service keeps running after you log out")
    elif linger == "no":
        report.warn(
            "linger: off, so the service stops when you log out. "
            f"To keep it running, run: sudo loginctl enable-linger {shlex.quote(user)}"
        )
    else:
        detail = (shown.stderr or shown.stdout or f"exit {shown.returncode}").strip().splitlines()
        report.warn(
            f"linger: unknown ({detail[-1] if detail else 'no answer'}); "
            "without linger the service stops when you log out"
        )


def doctor_service(report: DoctorReport, *, os_name: str, service_file: Path | None, config_file: Path,
                   run: RunCommand, user: str) -> None:
    """The background service: launchd on macOS; systemd --user and linger on Linux."""
    manual = shlex.join(service_command(config_file))
    service_file = service_file or default_service_file(os_name)
    if os_name == "Darwin":
        domain = f"gui/{getattr(os, 'getuid', lambda: 0)()}"
        if run(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"]).returncode == 0:
            report.ok(f"service: launchd {LAUNCHD_LABEL} is loaded")
        elif service_file is not None and service_file.is_file():
            report.warn(f"service: launchd {LAUNCHD_LABEL} is not loaded; start it with: "
                        f"launchctl bootstrap {domain} {shlex.quote(str(service_file))}")
        else:
            report.warn(f"service: not installed ({service_file} is missing); run setup, or start the bridge with: {manual}")
        return
    if os_name != "Linux":
        report.warn(f"service: not automated on {os_name}; start the bridge with: {manual}")
        return
    state = (run(["systemctl", "--user", "is-active", SERVICE_NAME]).stdout or "").strip()
    if not state:
        report.warn(f"service: systemd --user is not available here; start the bridge with: {manual}")
        return
    if state == "active":
        report.ok(f"service: systemd --user {SERVICE_NAME} is active")
    elif service_file is not None and service_file.is_file():
        report.warn(
            f"service: {SERVICE_NAME} is {state}; start it with: systemctl --user restart {SERVICE_NAME} "
            f"(log: journalctl --user -u {SERVICE_NAME})"
        )
    else:
        report.warn(f"service: not installed ({service_file} is missing); run setup, or start the bridge with: {manual}")
    doctor_linger(report, run=run, user=user)


def doctor_summary(report: DoctorReport) -> int:
    out(f"doctor complete: {report.failures} failure(s), {report.warnings} warning(s)")
    return 1 if report.failures else 0


def doctor(
    config_file: Path,
    *,
    offline: bool = False,
    service_file: Path | None = None,
    api_call: ApiCall = telegram_call,
    run: RunCommand = run_command,
    which: Which = shutil.which,
    os_name: str | None = None,
    user: str | None = None,
) -> int:
    """Check an install and change nothing. Exit code 1 when any check fails. Never prints the token."""
    os_name = os_name or platform.system()
    report = DoctorReport()
    out("Cursor Telegram Bridge doctor")
    if not config_file.is_file():
        report.fail(f"bridge.env missing: {config_file}; run `python3 bridge_setup.py setup` first")
        return doctor_summary(report)
    doctor_private_file(report, "bridge.env", config_file)
    config = load_env_file(config_file)

    chat_id = (config.get("CUB_CHAT_ID") or "").strip()
    if not chat_id:
        report.fail("bridge.env has no CUB_CHAT_ID; run setup again")
    elif chat_id == EXAMPLE_CHAT_ID or not CHAT_ID_RE.fullmatch(chat_id):
        report.fail(f"CUB_CHAT_ID={chat_id} is not your chat id; run setup again to detect it")
    else:
        report.ok(f"chat id: {chat_id}")

    token = ""
    token_setting = (config.get("CUB_TOKEN_FILE") or "").strip()
    if not token_setting:
        report.fail("bridge.env has no CUB_TOKEN_FILE; run setup again")
    else:
        token_file = expand_path(token_setting)
        if not token_file.is_file():
            report.fail(f"token file missing: {token_file}; run setup again")
        else:
            token = read_token_file(token_file)
            if token:
                doctor_private_file(report, "token file", token_file)
            else:
                report.fail(f"token file has no api_key: {token_file}; run setup again")
    if token:
        doctor_telegram(report, token, offline=offline, api_call=api_call)

    cursor_bin = resolve_cursor_bin(config, which)
    if cursor_bin:
        report.ok(f"cursor-agent: {cursor_bin}")
    else:
        report.fail(f"cursor-agent not found ({config.get('CUB_CURSOR_BIN') or 'cursor-agent'}); install Cursor's "
                    "CLI, or set CUB_CURSOR_BIN to its full path")
    doctor_tmux(report, config, run=run, which=which)
    doctor_service(report, os_name=os_name, service_file=service_file, config_file=config_file, run=run,
                   user=current_user() if user is None else user)
    return doctor_summary(report)


def build_parser(prog: str = "bridge_setup.py") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Set up and run the Cursor Telegram Bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="guided setup (first question offers the author's setup)")
    setup_parser.add_argument("--like-author", action="store_true",
                              help="the author's setup: terminal mirror, progress card with Stop, no automatic "
                                   "/clear, plus a self-check at the end")
    setup_parser.add_argument("--dry-run", action="store_true", help="print what setup would do and change nothing")
    setup_parser.add_argument("--language", choices=("en", "ko"), default="en", help="bridge language (default en)")
    setup_parser.add_argument("--config", type=expand_path, default=default_config_file())
    setup_parser.add_argument("--token-file", type=expand_path, default=default_token_file())
    setup_parser.add_argument("--state-dir", type=expand_path, default=default_state_dir())
    setup_parser.add_argument("--chat-id", default=None, help="your Telegram chat id (skips the /start wait)")
    setup_parser.add_argument("--non-interactive", action="store_true",
                              help="ask nothing: needs a saved token file and a chat id")
    attach_parser = commands.add_parser("attach", help=f"attach to the bridge's Cursor session (same as {ATTACH_COMMAND})")
    attach_parser.add_argument("--config", type=expand_path, default=default_config_file())
    attach_parser.add_argument("--no-create", action="store_true", help="fail instead of starting a missing session")
    run_parser = commands.add_parser("run", help="start the bridge with bridge.env (used by the service)")
    run_parser.add_argument("--config", type=expand_path, default=default_config_file())
    doctor_parser = commands.add_parser(
        "doctor", help="check bridge.env, the token, tmux, the service and linger, and Cursor on other terminals"
    )
    doctor_parser.add_argument("--config", type=expand_path, default=default_config_file())
    doctor_parser.add_argument("--offline", action="store_true", help="do not contact Telegram")
    return parser


def attach_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=ATTACH_COMMAND,
                                     description="Attach this terminal to the Cursor session the Telegram bridge drives")
    parser.add_argument("--config", type=expand_path, default=default_config_file())
    parser.add_argument("--no-create", action="store_true", help="fail instead of starting a missing session")
    args = parser.parse_args(argv)
    return attach_command(args.config, create_missing=not args.no_create)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(values or ["setup"])
    if args.command == "attach":
        return attach_command(args.config, create_missing=not args.no_create)
    if args.command == "run":
        return run_bridge(args.config)
    if args.command == "doctor":
        return doctor(args.config, offline=args.offline)
    return setup_bridge(SetupOptions(
        config_file=args.config,
        token_file=args.token_file,
        state_dir=args.state_dir,
        link_file=default_link_file(),
        chat_id=args.chat_id,
        language=args.language,
        like_author=args.like_author,
        dry_run=args.dry_run,
        non_interactive=args.non_interactive,
    ))


if __name__ == "__main__":
    sys.exit(main())
