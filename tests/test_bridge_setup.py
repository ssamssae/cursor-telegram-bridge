# @mutate-why If a preset switch falls back to the public default, "like the author" installs without the progress card and its Stop button.
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | ("CUB_FLOW_MIRROR", "1") | ("CUB_FLOW_MIRROR", "0")
# @mutate-why Attaching with -d would kick every other terminal off the shared Cursor session.
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | "attach-session", "-t" | "attach-session", "-d", "-t"
# @mutate-why Without the session ttys, the bridge's own Cursor window is reported as running outside the session.
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | if name in inside or name in seen: | if name in seen:
# @mutate-why Typing the probe while Cursor shows a question would press Enter on it (workspace trust defaults to Trust).
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | waiting_before_probe = cursor_is_waiting() | waiting_before_probe = False
# @mutate-why doctor reads Linger=no as on, so a Linux service that stops at logout gets no warning
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | if linger == "yes": | if linger == "no":
# @mutate-why doctor stops warning about a Cursor agent on another terminal, which the phone cannot see
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | outside = warn_cursor_outside_session( | outside = 0 and warn_cursor_outside_session(
# @mutate-why doctor exits 0 after a failed check, so a script cannot tell a broken install
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | return 1 if report.failures else 0 | return 0
# @mutate-why setup writes over a service file it did not write, with no copy and no warning
# @mutate packaging/cursor-telegram-bridge/bridge_setup.py | if not path.exists() or service_file_is_ours(path): | if True:
"""One-command setup for the Cursor Telegram Bridge (T-261107-014).

Covers `bridge_setup.py`: the "like the author" preset and its first wizard
question, bridge.env backup, the background service, the tmux session with
Cursor started inside it, the cursor-tg attach command, the warning about Cursor
running outside the bridge session, and the three-step self-check. Nothing here
touches Telegram, tmux, launchd or systemd: every side effect is injected.
"""
import importlib.util
import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SETUP = Path(__file__).resolve().parents[1] / "bridge_setup.py"
TOKEN = "test-token-value"
CHAT = "111222333"
TMUX = "/opt/test/bin/tmux"
CURSOR = "/opt/test/bin/cursor-agent"


def load_setup():
    spec = importlib.util.spec_from_file_location("cursor_bridge_setup_under_test", SETUP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load_setup()


def completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


class FakeRun:
    """Records commands; answers tmux, ps, launchctl and systemctl like a quiet machine."""

    def __init__(self, *, session_exists=False, first_window="0", clients="", pane_ttys="/dev/ttys008\n",
                 ps_text="", socket_path="", fail_new_session=False, screen="", service_state="active",
                 linger="yes", launchctl_rc=0):
        self.calls = []
        self.session_exists = session_exists
        self.first_window = first_window
        self.clients = clients
        self.pane_ttys = pane_ttys
        self.ps_text = ps_text
        self.socket_path = socket_path
        self.fail_new_session = fail_new_session
        self.screen = screen
        self.service_state = service_state  # "" = systemd --user is not available
        self.linger = linger
        self.launchctl_rc = launchctl_rc

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[0] == TMUX:
            if "has-session" in cmd:
                return completed(cmd, 0 if self.session_exists else 1)
            if "new-session" in cmd:
                if self.fail_new_session:
                    return completed(cmd, 1, stderr="no server")
                self.session_exists = True
                return completed(cmd)
            if "list-windows" in cmd:
                return completed(cmd, stdout=self.first_window + "\n")
            if "list-panes" in cmd:
                return completed(cmd, 0 if self.session_exists else 1, stdout=self.pane_ttys if self.session_exists else "")
            if "list-clients" in cmd:
                return completed(cmd, stdout=self.clients)
            if "display-message" in cmd:
                return completed(cmd, stdout=self.socket_path + "\n")
            if "capture-pane" in cmd:
                return completed(cmd, stdout=self.screen)
            return completed(cmd)
        if cmd[0] == "ps":
            return completed(cmd, stdout=self.ps_text)
        if cmd[0] == "systemctl" and "is-active" in cmd:
            if not self.service_state:
                return completed(cmd, 1, stderr="Failed to connect to bus: No medium found")
            return completed(cmd, 0 if self.service_state == "active" else 3, stdout=self.service_state + "\n")
        if cmd[0] == "loginctl":
            return completed(cmd, stdout=f"Linger={self.linger}\n")
        if cmd[:2] == ["launchctl", "print"]:
            return completed(cmd, self.launchctl_rc, stderr="" if self.launchctl_rc == 0 else "Could not find service")
        return completed(cmd)

    def commands(self, word):
        return [cmd for cmd in self.calls if word in cmd]


def fake_which(mapping=None):
    known = {"tmux": TMUX, "cursor-agent": CURSOR}
    if mapping is not None:
        known = mapping

    def which(name):
        if name in known:
            return known[name]
        if name in known.values():
            return name
        return None

    return which


class FakeTelegram:
    def __init__(self, *, start_chat=CHAT, chat_type="private", send_ok=True):
        self.calls = []
        self.start_chat = start_chat
        self.chat_type = chat_type
        self.send_ok = send_ok

    def __call__(self, token, method, timeout=60, **params):
        self.calls.append((method, params))
        if token != TOKEN:
            return {"ok": False}
        if method == "getMe":
            return {"ok": True, "result": {"username": "cursorbot"}}
        if method == "getUpdates":
            if params.get("timeout_param") == 0:
                return {"ok": True, "result": []}
            return {"ok": True, "result": [{
                "update_id": 41,
                "message": {"text": "/start", "chat": {"id": int(self.start_chat), "type": self.chat_type,
                                                       "first_name": "Test"}},
            }]}
        if method == "sendMessage":
            return {"ok": self.send_ok, "result": {"message_id": 7}}
        return {"ok": True, "result": {}}

    def sent_texts(self):
        return [params.get("text", "") for method, params in self.calls if method == "sendMessage"]


class FakeClock:
    def __init__(self, on_sleep=None):
        self.now = 1000.0
        self.on_sleep = on_sleep

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


class ScriptedTail:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)

    def read_new(self):
        return self.chunks.pop(0) if self.chunks else ""


class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def options(self, **overrides):
        values = dict(
            config_file=self.root / "config" / "bridge.env",
            token_file=self.root / "config" / "token.json",
            state_dir=self.root / "state",
            link_file=self.root / "bin" / "cursor-tg",
            service_file=self.root / "service" / "unit",
            chat_id=CHAT,
            non_interactive=True,
        )
        values.update(overrides)
        return setup.SetupOptions(**values)

    def save_token(self, options):
        options.token_file.parent.mkdir(parents=True, exist_ok=True)
        options.token_file.write_text(json.dumps({"api_key": TOKEN}), encoding="utf-8")

    def run_setup(self, options, *, run=None, api=None, which=None, ask=None, secret=None, os_name="Darwin",
                  tail=None, clock=None):
        run = run if run is not None else FakeRun()
        api = api if api is not None else FakeTelegram()
        clock = clock if clock is not None else FakeClock()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.setup_bridge(
                options,
                api_call=api,
                run=run,
                which=which or fake_which(),
                ask=ask or (lambda prompt="": self.fail(f"unexpected question: {prompt}")),
                read_secret=secret or (lambda prompt="": self.fail("unexpected token prompt")),
                sleep=clock.sleep,
                clock=clock.clock,
                os_name=os_name,
                log_tail_factory=lambda *args, **kwargs: tail if tail is not None else ScriptedTail(),
            )
        return rc, output.getvalue(), run, api


class AuthorPresetTests(SetupTestCase):
    PRESET = {"CUB_TUI_MIRROR_LOCAL": "1", "CUB_FLOW_MIRROR": "1", "CUB_CONTEXT_CLEAR_PCT": "0"}

    def test_preset_switches_are_written_only_with_like_author(self):
        plain = self.options(config_file=self.root / "plain" / "bridge.env")
        author = self.options(like_author=True)
        for options in (plain, author):
            self.save_token(options)
        rc, text, _run, _api = self.run_setup(plain)
        self.assertEqual(rc, 0, text)
        rc, text, _run, _api = self.run_setup(author)
        self.assertEqual(rc, 0, text)
        plain_env = setup.load_env_file(plain.config_file)
        author_env = setup.load_env_file(author.config_file)
        self.assertEqual(dict(setup.AUTHOR_PRESET_ENV), self.PRESET)
        for key, value in self.PRESET.items():
            self.assertNotIn(key, plain_env)
            self.assertEqual(author_env.get(key), value, key)
        for env in (plain_env, author_env):
            self.assertEqual(env["CUB_CHAT_ID"], CHAT)
            self.assertEqual(env["CUB_TOKEN_FILE"], str(author.token_file))
            self.assertEqual(env["CUB_TMUX_SESSION"], "cursor")
            self.assertEqual(env["CUB_TMUX_SOCKET"], "default")
            self.assertEqual(env["CUB_CURSOR_BIN"], CURSOR)
            self.assertIn("/opt/test/bin", env["PATH"].split(os.pathsep))
        self.assertEqual(stat.S_IMODE(author.config_file.stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, text)

    def test_existing_bridge_env_is_backed_up_before_the_preset_is_written(self):
        options = self.options(like_author=True)
        self.save_token(options)
        options.config_file.write_text("CUB_CHAT_ID=999\nCUB_MODEL_MENU=mine\n", encoding="utf-8")
        rc, text, _run, _api = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        backups = sorted(options.config_file.parent.glob("bridge.env.bak-*"))
        self.assertEqual(len(backups), 1, text)
        self.assertIn("CUB_MODEL_MENU=mine", backups[0].read_text(encoding="utf-8"))
        self.assertIn(str(backups[0]), text)
        env = setup.load_env_file(options.config_file)
        self.assertEqual(env.get("CUB_FLOW_MIRROR"), "1")

    def test_interactive_wizard_asks_author_preset_first_and_defaults_to_yes(self):
        options = self.options(non_interactive=False, chat_id=None)
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return ""

        rc, text, _run, api = self.run_setup(options, ask=ask, secret=lambda prompt="": TOKEN)
        self.assertEqual(rc, 0, text)
        self.assertEqual(prompts[0], setup.AUTHOR_PRESET_QUESTION)
        self.assertEqual(prompts[0], "Use the author's setup (recommended)? [Y/n] ")
        env = setup.load_env_file(options.config_file)
        for key, value in self.PRESET.items():
            self.assertEqual(env.get(key), value, key)
        self.assertEqual(env["CUB_CHAT_ID"], CHAT)
        self.assertEqual(json.loads(options.token_file.read_text(encoding="utf-8")), {"api_key": TOKEN})
        self.assertEqual(stat.S_IMODE(options.token_file.stat().st_mode), 0o600)
        self.assertNotIn(TOKEN, text)
        self.assertTrue(any(method == "getUpdates" for method, _params in api.calls))

    def test_interactive_wizard_declined_keeps_public_defaults(self):
        options = self.options(non_interactive=False)

        def ask(prompt=""):
            return "n" if prompt == setup.AUTHOR_PRESET_QUESTION else ""

        rc, text, _run, _api = self.run_setup(options, ask=ask, secret=lambda prompt="": TOKEN)
        self.assertEqual(rc, 0, text)
        env = setup.load_env_file(options.config_file)
        for key in self.PRESET:
            self.assertNotIn(key, env)
        self.assertNotIn("self-check", text.lower())

    def test_group_chat_is_not_taken_as_the_owner_chat(self):
        options = self.options(non_interactive=False, chat_id=None)
        clock = FakeClock()
        rc, text, _run, _api = self.run_setup(
            options, ask=lambda prompt="": "n", secret=lambda prompt="": TOKEN,
            api=FakeTelegram(chat_type="group"), clock=clock,
        )
        self.assertEqual(rc, 2, text)
        self.assertIn("/start", text)
        self.assertFalse(options.config_file.exists())

    def test_dry_run_prints_the_plan_and_touches_nothing(self):
        options = self.options(like_author=True, dry_run=True)

        def no_network(*args, **kwargs):
            raise AssertionError("dry run must not call Telegram")

        def no_commands(cmd):
            raise AssertionError(f"dry run must not run commands: {cmd}")

        rc, text, _run, _api = self.run_setup(options, api=no_network, run=no_commands)
        self.assertEqual(rc, 0, text)
        for key, value in self.PRESET.items():
            self.assertIn(f"{key}={value}", text)
        self.assertIn("cursor-tg", text)
        self.assertIn(str(options.config_file), text)
        self.assertIn("self-check", text.lower())
        self.assertFalse(options.config_file.exists())
        self.assertFalse(options.token_file.exists())
        self.assertFalse(options.link_file.exists())
        self.assertFalse(options.service_file.exists())

    def test_missing_token_in_non_interactive_mode_stops_before_writing(self):
        options = self.options(like_author=True)
        rc, text, run, _api = self.run_setup(options)
        self.assertEqual(rc, 2, text)
        self.assertFalse(options.config_file.exists())
        self.assertEqual(run.calls, [])

    def test_author_preset_without_tmux_stops_before_writing(self):
        options = self.options(like_author=True)
        self.save_token(options)
        rc, text, run, _api = self.run_setup(options, which=fake_which({"cursor-agent": CURSOR}))
        self.assertEqual(rc, 2, text)
        self.assertIn("tmux", text)
        self.assertFalse(options.config_file.exists())
        self.assertEqual(run.calls, [])

    def test_plain_setup_without_tmux_keeps_the_headless_lane(self):
        options = self.options()
        self.save_token(options)
        rc, text, run, _api = self.run_setup(options, which=fake_which({"cursor-agent": CURSOR}))
        self.assertEqual(rc, 0, text)
        self.assertIn("headless", text)
        self.assertEqual([cmd for cmd in run.calls if cmd[0] == TMUX], [])
        self.assertEqual(setup.load_env_file(options.config_file)["CUB_CHAT_ID"], CHAT)


class ServiceAndSessionTests(SetupTestCase):
    def test_launchd_service_runs_setup_run_with_bridge_env(self):
        options = self.options()
        self.save_token(options)
        rc, text, run, _api = self.run_setup(options, os_name="Darwin")
        self.assertEqual(rc, 0, text)
        plist = plistlib.loads(options.service_file.read_bytes())
        self.assertEqual(plist["Label"], setup.LAUNCHD_LABEL)
        self.assertEqual(plist["ProgramArguments"][1:], [str(SETUP), "run", "--config", str(options.config_file)])
        self.assertTrue(plist["KeepAlive"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StandardOutPath"], str(options.state_dir / "bridge.log"))
        bootstrap = run.commands("bootstrap")
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(bootstrap[0][-1], str(options.service_file))

    def test_systemd_user_service_runs_setup_run_with_bridge_env(self):
        options = self.options()
        self.save_token(options)
        rc, text, run, _api = self.run_setup(options, os_name="Linux")
        self.assertEqual(rc, 0, text)
        unit = options.service_file.read_text(encoding="utf-8")
        self.assertIn(f"{SETUP} run --config {options.config_file}", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn(["systemctl", "--user", "enable", setup.SERVICE_NAME], run.calls)
        self.assertIn(["systemctl", "--user", "restart", setup.SERVICE_NAME], run.calls)

    def test_systemd_exec_start_quotes_spaces_and_escapes_specifiers(self):
        text = setup.systemd_unit_text(["/usr/bin/python3", "/opt/my dir/bridge_setup.py", "run", "--config",
                                        "/srv/100%/bridge.env"])
        exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertEqual(exec_start,
                         'ExecStart=/usr/bin/python3 "/opt/my dir/bridge_setup.py" run --config /srv/100%%/bridge.env')

    def test_missing_session_is_created_with_cursor_at_window_zero(self):
        options = self.options()
        self.save_token(options)
        run = FakeRun(session_exists=False, first_window="1")
        rc, text, run, _api = self.run_setup(options, run=run)
        self.assertEqual(rc, 0, text)
        created = run.commands("new-session")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][:7], [TMUX, "-L", "default", "new-session", "-d", "-s", "cursor"])
        self.assertEqual(created[0][-1], CURSOR)
        self.assertIn([TMUX, "-L", "default", "move-window", "-s", "=cursor:1", "-t", "=cursor:0"], run.calls)
        session_at = run.calls.index(created[0])
        service_at = run.calls.index(run.commands("bootstrap")[0])
        self.assertLess(session_at, service_at, "Cursor must be running before the bridge service starts")

    def test_existing_session_is_kept_and_never_killed(self):
        options = self.options(like_author=True)
        self.save_token(options)
        rc, text, run, _api = self.run_setup(options, run=FakeRun(session_exists=True))
        self.assertEqual(rc, 0, text)
        self.assertEqual(run.commands("new-session"), [])
        self.assertEqual(run.commands("kill-session"), [])
        self.assertEqual(run.commands("kill-server"), [])

    def test_test_message_goes_to_the_owner_chat_once(self):
        options = self.options()
        self.save_token(options)
        rc, text, _run, api = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        sends = [params for method, params in api.calls if method == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["chat_id"], CHAT)

    def test_cursor_tg_link_points_at_the_repository_script(self):
        options = self.options()
        self.save_token(options)
        rc, text, _run, _api = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        self.assertTrue(options.link_file.is_symlink())
        self.assertEqual(Path(os.readlink(options.link_file)), setup.ATTACH_SCRIPT)
        self.assertEqual(setup.ATTACH_SCRIPT.name, "cursor-tg")

    def test_existing_regular_file_named_cursor_tg_is_left_alone(self):
        options = self.options()
        self.save_token(options)
        options.link_file.parent.mkdir(parents=True)
        options.link_file.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        rc, text, _run, _api = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        self.assertFalse(options.link_file.is_symlink())
        self.assertIn("echo mine", options.link_file.read_text(encoding="utf-8"))
        self.assertIn("left it alone", text)

    def test_cursor_tg_launcher_runs_attach_through_a_symlink(self):
        link = self.root / "cursor-tg"
        link.symlink_to(setup.ATTACH_SCRIPT)
        proc = subprocess.run([sys.executable, str(link), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cursor-tg", proc.stdout)
        self.assertIn("--no-create", proc.stdout)


class AttachTests(SetupTestCase):
    def write_env(self, text):
        config = self.root / "bridge.env"
        config.write_text(text, encoding="utf-8")
        return config

    def attach(self, config, run, *, environ=None, which=None):
        execs = []
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.attach_command(
                config, run=run, which=which or fake_which(),
                exec_fn=lambda binary, argv, env: execs.append((binary, argv, env)),
                environ=environ if environ is not None else {"HOME": str(self.root)},
            )
        return rc, execs, output.getvalue()

    def test_attach_uses_socket_and_session_from_bridge_env_without_detaching_others(self):
        config = self.write_env("CUB_TMUX_SOCKET=sock1\nCUB_TMUX_SESSION=work\n")
        run = FakeRun(session_exists=True, clients="/dev/ttys002: work [80x24 xterm-256color]\n")
        rc, execs, text = self.attach(config, run)
        self.assertEqual(rc, 0, text)
        self.assertIn([TMUX, "-L", "sock1", "has-session", "-t", "=work"], run.calls)
        self.assertEqual([(binary, argv) for binary, argv, _env in execs],
                         [(TMUX, [TMUX, "-L", "sock1", "attach-session", "-t", "=work"])])
        self.assertNotIn("-d", execs[0][1])
        self.assertIn("another terminal", text.lower())
        self.assertEqual(run.commands("new-session"), [])

    def test_attach_creates_a_missing_session_with_cursor_then_attaches(self):
        config = self.write_env(f"CUB_CURSOR_BIN={CURSOR}\n")
        run = FakeRun(session_exists=False)
        rc, execs, text = self.attach(config, run)
        self.assertEqual(rc, 0, text)
        created = run.commands("new-session")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][-1], CURSOR)
        self.assertEqual(execs[0][1], [TMUX, "-L", "default", "attach-session", "-t", "=cursor"])

    def test_attach_inside_the_same_tmux_server_switches_client(self):
        sock = str(self.root / "tmux-sock")
        config = self.write_env("")
        run = FakeRun(session_exists=True, socket_path=sock)
        rc, execs, text = self.attach(config, run, environ={"TMUX": f"{sock},123,0", "TMUX_PANE": "%1"})
        self.assertEqual(rc, 0, text)
        self.assertEqual(execs[0][1], [TMUX, "-L", "default", "switch-client", "-t", "=cursor"])

    def test_attach_from_another_tmux_server_drops_the_nested_marker(self):
        config = self.write_env("")
        run = FakeRun(session_exists=True, socket_path=str(self.root / "bridge-sock"))
        rc, execs, text = self.attach(config, run, environ={"TMUX": f"{self.root / 'other'},1,0", "TMUX_PANE": "%3"})
        self.assertEqual(rc, 0, text)
        binary, argv, env = execs[0]
        self.assertEqual(argv[3], "attach-session")
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)

    def test_attach_without_cursor_agent_fails_closed(self):
        config = self.write_env("")
        run = FakeRun(session_exists=False)
        rc, execs, text = self.attach(config, run, which=fake_which({"tmux": TMUX}))
        self.assertEqual(rc, 2, text)
        self.assertEqual(execs, [])
        self.assertEqual(run.commands("new-session"), [])

    def test_attach_with_no_create_reports_the_missing_session(self):
        config = self.write_env("")
        run = FakeRun(session_exists=False)
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.attach_command(config, run=run, which=fake_which(), create_missing=False,
                                      exec_fn=lambda *a: self.fail("must not attach"), environ={})
        self.assertEqual(rc, 2)
        self.assertEqual(run.commands("new-session"), [])


class OutsideSessionTests(SetupTestCase):
    PS = (
        " 2198 ttys008  /home/dev/.local/bin/cursor-agent --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js\n"
        " 2201 ttys008  /bin/zsh -c ls\n"
        "28961 ??       /home/dev/.local/bin/agent --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js acp\n"
        "30001 ttys011  /home/dev/.local/bin/agent --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js\n"
        "30002 ttys011  /home/dev/.local/share/cursor-agent/versions/1/node worker.js\n"
        "30003 pts/4    /home/dev/.local/share/cursor-agent/versions/1/node --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js\n"
        "30004 ttys012  grep cursor-agent\n"
        "30005 ttys013  vim notes-cursor-agent.md\n"
    )

    def test_outside_processes_skip_bridge_session_ttys_daemons_and_lookalikes(self):
        found = setup.cursor_processes_outside({"/dev/ttys008"}, self.PS)
        self.assertEqual(found, [("30001", "ttys011"), ("30003", "pts/4")])
        self.assertEqual(setup.cursor_processes_outside({"/dev/ttys008", "/dev/ttys011", "/dev/pts/4"}, self.PS), [])

    def test_setup_end_warns_about_cursor_running_outside_the_bridge_session(self):
        options = self.options()
        self.save_token(options)
        run = FakeRun(session_exists=True, pane_ttys="/dev/ttys008\n", ps_text=self.PS)
        rc, text, run, _api = self.run_setup(options, run=run)
        self.assertEqual(rc, 0, text)
        self.assertIn("outside the bridge tmux session", text)
        self.assertIn("pid 30001 on ttys011", text)
        self.assertIn("cursor-tg", text)
        self.assertIn([TMUX, "-L", "default", "list-panes", "-s", "-t", "=cursor", "-F", "#{pane_tty}"], run.calls)
        self.assertIn(["ps", "-A", "-ww", "-o", "pid=,tty=,args="], run.calls)

    def test_no_warning_when_cursor_only_runs_in_the_bridge_session(self):
        options = self.options()
        self.save_token(options)
        run = FakeRun(session_exists=True, ps_text=self.PS.splitlines(keepends=True)[0])
        rc, text, _run, _api = self.run_setup(options, run=run)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("outside the bridge tmux session", text)


class SelfCheckTests(SetupTestCase):
    TRUST_SCREEN = (
        "  |  Workspace Trust Required\n"
        "  |  Do you trust the contents of this directory?\n"
        "  |  ▶ [a] Trust this workspace\n"
        "  |    [q] Quit\n"
        "  |  Use arrow keys to navigate, Enter to select, or press the key shown\n"
    )

    def self_check(self, *, tail, on_sleep=None, sent_ok=True, screen=""):
        api = FakeTelegram()
        clock = FakeClock(on_sleep=on_sleep)
        run = FakeRun(session_exists=True, screen=screen)
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        output = io.StringIO()
        with redirect_stdout(output):
            lines = setup.author_self_check(
                token=TOKEN, chat_id=CHAT, tmux_bin=TMUX,
                config={"CUB_STATE_DIR": str(state), "CUB_TMUX_SOCKET": "default", "CUB_TMUX_SESSION": "cursor"},
                log_tail=tail, log_hint="bridge.log", api_call=api, run=run, sleep=clock.sleep, clock=clock.clock,
                phone_timeout=10, terminal_timeout=10, test_message_sent=sent_ok, language="en",
            )
        return lines, output.getvalue(), api, run

    def test_timeouts_give_three_lines_with_reasons_on_terminal_and_phone(self):
        lines, text, api, run = self.self_check(tail=ScriptedTail())
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("1/3"), lines)
        self.assertIn("no phone message within 10 s", lines[1])
        self.assertIn("no terminal mirror within 10 s", lines[2])
        for line in lines:
            self.assertIn(line, text)
        summary = api.sent_texts()[-1]
        for line in lines:
            self.assertIn(line, summary)
        typed = [cmd for cmd in run.calls if "send-keys" in cmd]
        self.assertEqual(typed[0], [TMUX, "-L", "default", "send-keys", "-t", "=cursor:0.0", "-l", setup.SELF_CHECK_PROBE])
        self.assertEqual(typed[1], [TMUX, "-L", "default", "send-keys", "-t", "=cursor:0.0", "Enter"])

    def test_health_and_log_markers_pass_both_directions(self):
        health = self.root / "state" / "cursor-bridge-cursor.health.json"

        def bridge_works(now):
            health.write_text(json.dumps({"last_job_started_at": now}), encoding="utf-8")

        tail = ScriptedTail(["", "cub_tui_lane send final sent=1 deliveries=1\n", "",
                             "cub_tui_lane local mirror tick 1\n"])
        lines, _text, _api, _run = self.self_check(tail=tail, on_sleep=bridge_works)
        self.assertEqual(lines[1], "2/3 phone -> terminal -> phone: ok")
        self.assertEqual(lines[2], "3/3 terminal -> phone: ok")

    def test_message_that_reached_cursor_without_an_answer_says_so(self):
        health = self.root / "state" / "cursor-bridge-cursor.health.json"

        def started_only(now):
            health.write_text(json.dumps({"last_job_started_at": now}), encoding="utf-8")

        lines, _text, _api, _run = self.self_check(tail=ScriptedTail(), on_sleep=started_only)
        self.assertIn("reached Cursor but no answer came back", lines[1])

    def test_self_check_never_types_into_a_waiting_question(self):
        lines, text, api, run = self.self_check(tail=ScriptedTail(), screen=self.TRUST_SCREEN)
        self.assertEqual([cmd for cmd in run.calls if "send-keys" in cmd], [])
        self.assertIn("skipped", lines[2])
        self.assertIn("waiting for an answer", lines[2])
        self.assertTrue(any("answer it first" in sent for sent in api.sent_texts()), api.sent_texts())
        self.assertIn("waiting for an answer", text)

    def test_waiting_question_detector_matches_trust_and_menus_only(self):
        self.assertTrue(setup.pane_waiting_for_answer(self.TRUST_SCREEN))
        self.assertTrue(setup.pane_waiting_for_answer("  Run this command?\n  → Run (once) (y)\n"))
        self.assertFalse(setup.pane_waiting_for_answer(""))
        self.assertFalse(setup.pane_waiting_for_answer(" → Add a follow-up\n  Auto · 50% context used\n"))

    def test_failed_test_message_is_reported_as_step_one(self):
        lines, _text, _api, _run = self.self_check(tail=ScriptedTail(), sent_ok=False)
        self.assertIn("not delivered", lines[0])

    def test_preset_setup_runs_the_self_check_after_service_and_session(self):
        options = self.options(like_author=True)
        self.save_token(options)
        rc, text, run, api = self.run_setup(options, run=FakeRun(session_exists=True))
        self.assertEqual(rc, 0, text)
        self.assertTrue(any("send-keys" in cmd for cmd in run.calls))
        self.assertTrue(api.sent_texts()[-1].startswith("Setup self-check"))
        self.assertIn("3/3 terminal -> phone", text)

    def test_plain_setup_skips_the_self_check(self):
        options = self.options()
        self.save_token(options)
        rc, text, run, api = self.run_setup(options, run=FakeRun(session_exists=True))
        self.assertEqual(rc, 0, text)
        self.assertFalse(any("send-keys" in cmd for cmd in run.calls))
        self.assertFalse(any(t.startswith("Setup self-check") for t in api.sent_texts()))


class RunAndParserTests(SetupTestCase):
    def test_run_loads_bridge_env_and_execs_the_bridge(self):
        config = self.root / "bridge.env"
        config.write_text("# comment\nCUB_CHAT_ID=111222333\nCUB_TMUX_SESSION=\"cursor\"\nPATH=/opt/test/bin:/usr/bin\n",
                          encoding="utf-8")
        execs = []
        rc = setup.run_bridge(config, exec_fn=lambda binary, argv, env: execs.append((binary, argv, env)))
        self.assertEqual(rc, 0)
        binary, argv, env = execs[0]
        self.assertEqual(argv[-1], str(setup.BRIDGE_SCRIPT))
        self.assertEqual(env["CUB_CHAT_ID"], CHAT)
        self.assertEqual(env["CUB_TMUX_SESSION"], "cursor")
        self.assertEqual(env["PATH"], "/opt/test/bin:/usr/bin")

    def test_run_refuses_a_missing_config(self):
        with redirect_stdout(io.StringIO()):
            rc = setup.run_bridge(self.root / "missing.env", exec_fn=lambda *a: self.fail("must not start"))
        self.assertEqual(rc, 2)

    def test_parser_exposes_like_author_dry_run_attach_and_run(self):
        parser = setup.build_parser()
        args = parser.parse_args(["setup", "--like-author", "--dry-run", "--language", "ko"])
        self.assertTrue(args.like_author)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.language, "ko")
        self.assertEqual(parser.parse_args(["attach", "--no-create"]).command, "attach")
        self.assertEqual(parser.parse_args(["run"]).command, "run")


class DoctorTests(SetupTestCase):
    PS = (
        " 2198 ttys008  /home/dev/.local/bin/cursor-agent --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js\n"
        "28961 ??       /home/dev/.local/bin/agent --use-system-ca /home/dev/.local/share/cursor-agent/versions/1/index.js acp\n"
    )

    def install(self, *, chat_id=CHAT, token=TOKEN, env_mode=0o600, token_mode=0o600, extra=()):
        """A finished install: bridge.env, the token file and the service file, as setup writes them."""
        options = self.options()
        options.config_file.parent.mkdir(parents=True, exist_ok=True)
        options.token_file.write_text(json.dumps({"api_key": token}), encoding="utf-8")
        os.chmod(options.token_file, token_mode)
        lines = [f"CUB_TOKEN_FILE={options.token_file}", f"CUB_STATE_DIR={options.state_dir}",
                 f"CUB_CURSOR_BIN={CURSOR}", "CUB_TMUX_SOCKET=default", "CUB_TMUX_SESSION=cursor", *extra]
        if chat_id is not None:
            lines.append(f"CUB_CHAT_ID={chat_id}")
        options.config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(options.config_file, env_mode)
        options.service_file.parent.mkdir(parents=True, exist_ok=True)
        options.service_file.write_text("unit\n", encoding="utf-8")
        return options

    def doctor(self, options, *, run=None, api=None, which=None, os_name="Linux", offline=False):
        run = run if run is not None else FakeRun(session_exists=True, ps_text=self.PS)
        api = api if api is not None else FakeTelegram()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = setup.doctor(options.config_file, offline=offline, service_file=options.service_file, api_call=api,
                              run=run, which=which or fake_which(), os_name=os_name, user="tester")
        return rc, output.getvalue(), run, api

    def lines(self, text, word):
        """The [ok]/[warn]/[fail] lines that mention `word`, in order."""
        return [line for line in text.splitlines() if line.startswith("[") and word in line.lower()]

    def test_healthy_install_prints_only_ok_lines_and_exits_zero(self):
        rc, text, run, api = self.doctor(self.install())
        self.assertEqual(rc, 0, text)
        self.assertNotIn("[warn]", text)
        self.assertNotIn("[fail]", text)
        for needle in ("bridge.env", "token file", "@cursorbot", CURSOR, "default/cursor", "pid 2198",
                       setup.SERVICE_NAME, "linger"):
            self.assertIn(needle, text)
        self.assertIn("doctor complete: 0 failure(s), 0 warning(s)", text)
        self.assertEqual([method for method, _params in api.calls], ["getMe"], "doctor contacts Telegram once at most")
        self.assertIn(["systemctl", "--user", "is-active", setup.SERVICE_NAME], run.calls)
        self.assertIn(["loginctl", "show-user", "tester", "-p", "Linger"], run.calls)
        self.assertNotIn(TOKEN, text)

    def test_missing_bridge_env_fails_with_exit_code_one_and_checks_nothing_else(self):
        rc, text, run, api = self.doctor(self.options())
        self.assertEqual(rc, 1, text)
        self.assertTrue(text.splitlines()[1].startswith("[fail]"), text)
        self.assertIn("bridge_setup.py setup", text)
        self.assertEqual(run.calls, [])
        self.assertEqual(api.calls, [])

    def test_files_others_can_read_warn_with_chmod_and_the_token_is_never_printed(self):
        rc, text, _run, _api = self.doctor(self.install(env_mode=0o644, token_mode=0o644))
        self.assertEqual(rc, 0, text)
        warnings = [line for line in text.splitlines() if line.startswith("[warn]")]
        self.assertEqual(len(warnings), 2, text)
        for line in warnings:
            self.assertIn("chmod 600", line)
        self.assertNotIn(TOKEN, text)

    def test_refused_token_fails_and_no_answer_from_telegram_only_warns(self):
        rc, text, _run, _api = self.doctor(self.install(token="revoked-token-value"))
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "telegram")[0].startswith("[fail]"), text)
        self.assertNotIn("revoked-token-value", text)

        rc, text, _run, _api = self.doctor(self.install(), api=lambda *args, **kwargs: None)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "telegram")[0].startswith("[warn]"), text)

    def test_offline_never_contacts_telegram(self):
        def no_network(*args, **kwargs):
            raise AssertionError("doctor --offline contacted Telegram")

        rc, text, _run, _api = self.doctor(self.install(), api=no_network, offline=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("--offline", text)

    def test_missing_or_placeholder_chat_id_and_a_missing_token_file_fail(self):
        for chat_id in (None, setup.EXAMPLE_CHAT_ID, "my-chat"):
            rc, text, _run, _api = self.doctor(self.install(chat_id=chat_id))
            self.assertEqual(rc, 1, (chat_id, text))
            self.assertTrue(self.lines(text, "cub_chat_id")[0].startswith("[fail]"), text)

        options = self.install()
        options.token_file.write_text(json.dumps({"api_key": ""}), encoding="utf-8")
        rc, text, _run, _api = self.doctor(options)
        self.assertEqual(rc, 1, text)
        self.assertIn("api_key", text)
        options.token_file.unlink()
        rc, text, _run, api = self.doctor(options)
        self.assertEqual(rc, 1, text)
        self.assertIn("token file missing", text)
        self.assertEqual(api.calls, [], "doctor asked Telegram without a token")

    def test_linger_off_warns_with_the_command_that_turns_it_on_and_never_runs_it(self):
        options = self.install()
        rc, text, run, _api = self.doctor(options, run=FakeRun(session_exists=True, ps_text=self.PS, linger="no"))
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "linger")[0].startswith("[warn]"), text)
        self.assertIn("log out", text)
        self.assertIn("sudo loginctl enable-linger tester", text)
        self.assertFalse(any("enable-linger" in cmd for cmd in run.calls), "doctor turned linger on by itself")

        rc, text, _run, _api = self.doctor(options, run=FakeRun(session_exists=True, ps_text=self.PS, linger="yes"))
        self.assertTrue(self.lines(text, "linger")[0].startswith("[ok]"), text)
        self.assertNotIn("enable-linger", text)

    def test_systemd_service_not_running_not_installed_or_unavailable_warns(self):
        options = self.install()
        inactive = FakeRun(session_exists=True, ps_text=self.PS, service_state="inactive")
        rc, text, _run, _api = self.doctor(options, run=inactive)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "service")[0].startswith("[warn]"), text)
        self.assertIn(f"systemctl --user restart {setup.SERVICE_NAME}", text)

        options.service_file.unlink()
        rc, text, _run, _api = self.doctor(options, run=FakeRun(session_exists=True, ps_text=self.PS,
                                                                service_state="inactive"))
        self.assertIn("not installed", text)

        rc, text, run, _api = self.doctor(options, run=FakeRun(session_exists=True, ps_text=self.PS, service_state=""))
        self.assertIn("systemd --user is not available", text)
        self.assertFalse(any(cmd[0] == "loginctl" for cmd in run.calls), "linger checked without systemd")

    def test_launchd_service_on_macos_and_no_linger_check(self):
        options = self.install()
        rc, text, run, _api = self.doctor(options, os_name="Darwin")
        self.assertEqual(rc, 0, text)
        self.assertIn(f"[ok] service: launchd {setup.LAUNCHD_LABEL} is loaded", text)
        self.assertFalse(any(cmd[0] in {"loginctl", "systemctl"} for cmd in run.calls))

        not_loaded = FakeRun(session_exists=True, ps_text=self.PS, launchctl_rc=113)
        rc, text, _run, _api = self.doctor(options, os_name="Darwin", run=not_loaded)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "service")[0].startswith("[warn]"), text)
        self.assertIn("launchctl bootstrap", text)

        options.service_file.unlink()
        rc, text, _run, _api = self.doctor(options, os_name="Darwin",
                                           run=FakeRun(session_exists=True, ps_text=self.PS, launchctl_rc=113))
        self.assertIn("not installed", text)

    def test_tmux_and_the_session_follow_the_headless_fallback_and_cursor_agent_is_required(self):
        rc, text, _run, _api = self.doctor(self.install(), run=FakeRun(session_exists=False))
        self.assertEqual(rc, 0, text)
        self.assertIn("[warn] tmux session default/cursor is not running", text)
        self.assertIn("cursor-tg", text)

        strict = self.install(extra=("CUB_TUI_FALLBACK_HEADLESS=0",))
        rc, text, _run, _api = self.doctor(strict, run=FakeRun(session_exists=False))
        self.assertEqual(rc, 1, text)
        self.assertIn("[fail] tmux session default/cursor is not running", text)

        no_tmux = fake_which({"cursor-agent": CURSOR})
        rc, text, run, _api = self.doctor(self.install(), which=no_tmux)
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.lines(text, "tmux")[0].startswith("[warn]"), text)
        self.assertEqual([cmd for cmd in run.calls if cmd[0] == TMUX], [])
        rc, text, _run, _api = self.doctor(self.install(extra=("CUB_TUI_FALLBACK_HEADLESS=0",)), which=no_tmux)
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "tmux")[0].startswith("[fail]"), text)

        rc, text, _run, _api = self.doctor(self.install(), which=fake_which({"tmux": TMUX}))
        self.assertEqual(rc, 1, text)
        self.assertTrue(self.lines(text, "cursor-agent")[0].startswith("[fail]"), text)

    def test_cursor_in_the_session_is_ok_and_cursor_on_another_terminal_warns_with_cursor_tg(self):
        run = FakeRun(session_exists=True, pane_ttys="/dev/ttys008\n", ps_text=OutsideSessionTests.PS)
        rc, text, _run, _api = self.doctor(self.install(), run=run)
        self.assertEqual(rc, 0, text)
        self.assertIn("[ok] Cursor is running in the bridge session default/cursor (pid 2198)", text)
        self.assertIn("[warn] Cursor is also running outside the bridge tmux session", text)
        self.assertIn("pid 30001 on ttys011", text)
        self.assertIn("cursor-tg", text)

        lone = FakeRun(session_exists=True, ps_text=" 2201 ttys008  /bin/zsh -c ls\n")
        rc, text, _run, _api = self.doctor(self.install(), run=lone)
        self.assertEqual(rc, 0, text)
        self.assertIn("[warn] no Cursor agent in the bridge session default/cursor", text)
        self.assertNotIn("outside the bridge tmux session", text)

    def test_setup_ends_by_pointing_at_doctor(self):
        options = self.options()
        self.save_token(options)
        rc, text, _run, _api = self.run_setup(options)
        self.assertEqual(rc, 0, text)
        last = [line for line in text.splitlines() if line.strip()][-1]
        self.assertIn("python3 bridge_setup.py doctor", last)

    def test_telegram_call_returns_the_refusal_body_of_an_http_error(self):
        body = io.BytesIO(b'{"ok": false, "error_code": 401, "description": "Unauthorized"}')
        refusal = urllib.error.HTTPError("https://api.telegram.org/bot-x/getMe", 401, "Unauthorized", {}, body)
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=refusal):
            payload = setup.telegram_call(TOKEN, "getMe", timeout=5)
        self.assertEqual(payload, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        with mock.patch.object(setup.urllib.request, "urlopen", side_effect=OSError("no route to host")):
            self.assertIsNone(setup.telegram_call(TOKEN, "getMe", timeout=5))

    def test_parser_and_main_route_doctor_and_offline(self):
        config = self.root / "custom.env"
        args = setup.build_parser().parse_args(["doctor", "--offline", "--config", str(config)])
        self.assertEqual((args.command, args.offline, args.config), ("doctor", True, config))
        self.assertFalse(setup.build_parser().parse_args(["doctor"]).offline)
        with mock.patch.object(setup, "doctor", return_value=1) as fake:
            self.assertEqual(setup.main(["doctor", "--offline", "--config", str(config)]), 1)
        fake.assert_called_once_with(config, offline=True)


class ServiceFileSafetyTests(SetupTestCase):
    FOREIGN_UNIT = "[Service]\nExecStart=/usr/local/bin/someone-elses-bridge\n"

    def prepare(self, options, body):
        self.save_token(options)
        options.service_file.parent.mkdir(parents=True, exist_ok=True)
        options.service_file.write_text(body, encoding="utf-8")

    def backups(self, options):
        return sorted(options.config_file.parent.glob(f"{options.service_file.name}.bak-*"))

    def test_foreign_service_file_is_copied_beside_bridge_env_with_a_warning_then_replaced(self):
        options = self.options()
        self.prepare(options, self.FOREIGN_UNIT)
        rc, text, run, _api = self.run_setup(options, os_name="Linux")
        self.assertEqual(rc, 0, text)
        backups = self.backups(options)
        self.assertEqual(len(backups), 1, text)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.FOREIGN_UNIT)
        self.assertEqual(list(options.service_file.parent.glob("*.bak-*")), [], "a copy beside it could load again")
        self.assertTrue(any(line.startswith("warning:") and str(options.service_file) in line
                            and str(backups[0]) in line for line in text.splitlines()), text)
        self.assertIn(setup.SERVICE_FILE_MARKER, options.service_file.read_text(encoding="utf-8"))
        self.assertIn(["systemctl", "--user", "restart", setup.SERVICE_NAME], run.calls)

    def test_a_service_file_written_by_setup_is_replaced_without_a_copy_or_a_warning(self):
        options = self.options()
        self.save_token(options)
        self.run_setup(options, os_name="Linux")
        rc, text, _run, _api = self.run_setup(options, os_name="Linux")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.backups(options), [])
        self.assertNotIn("not written by bridge_setup.py", text)

    def test_foreign_launchd_plist_is_copied_and_the_new_one_carries_the_marker(self):
        options = self.options()
        self.prepare(options, "<plist>someone else</plist>\n")
        rc, text, _run, _api = self.run_setup(options, os_name="Darwin")
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.backups(options)), 1, text)
        self.assertIn(setup.SERVICE_FILE_MARKER.encode(), options.service_file.read_bytes())
        self.assertEqual(plistlib.loads(options.service_file.read_bytes())["Label"], setup.LAUNCHD_LABEL)

    def test_interactive_setup_asks_first_and_n_keeps_the_foreign_file_and_the_service(self):
        options = self.options(non_interactive=False)
        self.prepare(options, self.FOREIGN_UNIT)
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return "n" if ("Back it up" in prompt or "author" in prompt) else ""

        rc, text, run, _api = self.run_setup(options, ask=ask, os_name="Linux")
        self.assertEqual(rc, 0, text)
        self.assertTrue(any(str(options.service_file) in prompt and prompt.endswith("[Y/n] ") for prompt in prompts),
                        prompts)
        self.assertEqual(options.service_file.read_text(encoding="utf-8"), self.FOREIGN_UNIT)
        self.assertEqual(self.backups(options), [])
        self.assertEqual([cmd for cmd in run.calls if cmd[0] == "systemctl"], [], "setup touched the service after n")
        self.assertIn("kept", text)

        rc, text, _run, _api = self.run_setup(options, ask=lambda prompt="": "n" if "author" in prompt else "",
                                              os_name="Linux")
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.backups(options)), 1, text)
        self.assertIn(setup.SERVICE_FILE_MARKER, options.service_file.read_text(encoding="utf-8"))

    def test_dry_run_shows_the_service_file_and_the_files_setup_writes_over(self):
        def no_network(*args, **kwargs):
            raise AssertionError("dry run must not call Telegram")

        def no_commands(cmd):
            raise AssertionError(f"dry run must not run commands: {cmd}")

        options = self.options(like_author=True, dry_run=True)
        rc, text, _run, _api = self.run_setup(options, api=no_network, run=no_commands, os_name="Linux")
        self.assertEqual(rc, 0, text)
        self.assertIn(str(options.service_file), text)
        self.assertIn("files already there that setup writes over: none", text)

        self.prepare(options, self.FOREIGN_UNIT)
        options.config_file.write_text("CUB_CHAT_ID=999\n", encoding="utf-8")
        before = {item: item.read_bytes() for item in (options.config_file, options.service_file, options.token_file)}
        rc, text, _run, _api = self.run_setup(options, api=no_network, run=no_commands, os_name="Linux")
        self.assertEqual(rc, 0, text)
        listed = text.split("files already there that setup writes over:", 1)[1]
        self.assertIn(str(options.config_file), listed)
        self.assertIn(str(options.service_file), listed)
        self.assertIn("not written by bridge_setup.py", listed)
        self.assertNotIn(str(options.token_file), listed, "a saved token is reused, not written over")
        for item, body in before.items():
            self.assertEqual(item.read_bytes(), body, item)
        self.assertEqual(list(options.config_file.parent.glob("*.bak-*")), [])


if __name__ == "__main__":
    unittest.main()
