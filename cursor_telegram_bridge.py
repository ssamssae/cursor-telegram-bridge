#!/usr/bin/env python3
"""Telegram -> Cursor bridge: one Telegram chat drives one machine's Cursor session.

Messages are pasted into an existing tmux session named `cursor` when that
session is alive. If it is gone, one `cursor-agent -p` process runs instead.
The finished answer is sent back through the Telegram Bot API.
"""
from __future__ import annotations

import fcntl
import datetime
import http.client
import hashlib
import importlib.util
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import bridge_flow_progress as _flow_progress  # noqa: E402
from bridge_public_text import strip_memory_citation  # noqa: E402
import terminal_turn_mirror as _turn_mirror  # noqa: E402


def env(k, default=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else default


def int_env(k, default, minimum=1):
    try:
        v = int(env(k, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default


def bool_env(k, default=False):
    v = env(k)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def float_env(k, default):
    try:
        v = float(env(k, str(default)))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


TOKEN_FILE = env("CUB_TOKEN_FILE")
# No default on purpose: a shipped chat id would hand strangers this machine.
CHAT_ID = env("CUB_CHAT_ID", "") or ""
STATE_DIR = env("CUB_STATE_DIR", os.path.join(HOME, ".cursor-telegram-bridge", "state"))
NAME = env("CUB_NAME", "cursor")
NODE_KEY = env("CUB_NODE_KEY", NAME)
DRY_RUN = bool_env("CUB_DRY_RUN", False)
TMUX_SOCKET = env("CUB_TMUX_SOCKET", "default")
TMUX_SESSION = env("CUB_TMUX_SESSION", "cursor")
TMUX_PANE = env("CUB_TMUX_PANE", f"{TMUX_SESSION}:0.0")
TUI_SUBMIT_KEY = env("CUB_TUI_SUBMIT_KEY", "Enter")
TUI_SUBMIT_DELAY = float_env("CUB_TUI_SUBMIT_DELAY", 0.3)
TUI_SUBMIT_CONFIRM = bool_env("CUB_TUI_SUBMIT_CONFIRM", True)
try:
    TUI_SUBMIT_CONFIRM_WAIT = float(env("CUB_TUI_SUBMIT_CONFIRM_WAIT", "1.5"))
except (TypeError, ValueError):
    TUI_SUBMIT_CONFIRM_WAIT = 1.5
if TUI_SUBMIT_CONFIRM_WAIT < 0:
    TUI_SUBMIT_CONFIRM_WAIT = 1.5
TUI_SUBMIT_RETRY = int_env("CUB_TUI_SUBMIT_RETRY", 1, minimum=0)
TUI_POLL_INTERVAL = float_env("CUB_TUI_POLL_INTERVAL", 0.5)
TUI_PASTE_RESOLVE_SEC = float_env("CUB_TUI_PASTE_RESOLVE_SEC", 3.0)
# paste 토큰이 옛 일기장 마지막 user 와 같아도, job 시작 전 mtime 은 후보에서 뺀다.
TRANSCRIPT_PICK_MTIME_SLACK_SEC = float_env("CUB_TRANSCRIPT_PICK_MTIME_SLACK_SEC", 5.0)
TUI_IDLE_SEC = float_env("CUB_TUI_IDLE_SEC", 8.0)
TUI_WAIT_SEC = int_env("CUB_TUI_WAIT_SEC", 900, minimum=10)
# 턴이 아직 도는 중(도구 호출·일기장 성장)이면 TUI_WAIT_SEC 를 넘겨도 기다린다 — 하드캡.
# 2026-09-04 23:33 live fire: 24분짜리 턴이 15분 캡에 걸려 헤드리스 cursor-agent 가
# 같은 프롬프트를 다시 돌렸다(중복 작업자) + 진짜 답은 폰에 안 갔다.
TUI_WAIT_MAX_SEC = int_env("CUB_TUI_WAIT_MAX_SEC", 7200, minimum=10)
# 연장 중 일기장이 이 시간 동안 한 줄도 안 자라면 멈춘 턴으로 보고 포기한다.
TUI_BUSY_STALL_SEC = int_env("CUB_TUI_BUSY_STALL_SEC", 900, minimum=30)
TUI_BUSY_INJECT = bool_env("CUB_BUSY_INJECT", True)
TUI_FALLBACK_HEADLESS = bool_env("CUB_TUI_FALLBACK_HEADLESS", True)
TUI_MIRROR_LOCAL = bool_env("CUB_TUI_MIRROR_LOCAL", False)
TUI_MIRROR_LOCAL_INTERVAL = float_env("CUB_TUI_MIRROR_LOCAL_INTERVAL", 5.0)
TUI_MIRROR_LOCAL_PROMPT_MAX = int_env("CUB_TUI_MIRROR_LOCAL_PROMPT_MAX", 2500, minimum=20)
# T-260906-011: idle 컨텍스트 가드. 상태줄 % 만 읽고 /context 주입은 하지 않는다.
# PCT=0 이면 끔. 쿨다운 기본 600초. awaiting-human 은 세우지 않는다.
CUB_CONTEXT_CLEAR_PCT = int_env("CUB_CONTEXT_CLEAR_PCT", 50, minimum=0)
CUB_CONTEXT_CLEAR_COOLDOWN_SEC = int_env("CUB_CONTEXT_CLEAR_COOLDOWN_SEC", 600, minimum=0)
_CONTEXT_GUARD_LAST_AT = 0.0
CURSOR_BIN = env("CUB_CURSOR_BIN", "cursor-agent")
TRANSCRIPT_ROOT = env(
    "CUB_TRANSCRIPT_ROOT",
    os.path.join(HOME, ".cursor", "projects"),
)
LOG_KEY = "cub_tui_lane"
TUI_CLEAR_CONFIRM = "세션 클리어됐어. 이어서 말하면 돼."
TUI_RESET_TOKENS = frozenset({"/clear", "/new"})
TUI_NATIVE_SLASH_TOKENS = TUI_RESET_TOKENS | frozenset({"/context"})
TUI_CONTEXT_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%(?:\s*·|\s|$)")
TUI_CONTEXT_STATUS_RE = re.compile(
    r"(?:Composer|Auto|Fast|GPT)[^\n%]*?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
TUI_CONTEXT_TOKENS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[Kk]\s*/\s*(\d+(?:\.\d+)?)\s*[Kk]\b"
)
TUI_CONTEXT_USED_RE = re.compile(
    r"(?:^|\b)(?:used|사용)\b[^\n%]*?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
DOWNLOAD_ATTEMPT_TIMEOUT = int_env("CUB_DOWNLOAD_TIMEOUT", 30, minimum=5)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SUGGESTED_REPLY_SPLIT = bool_env("CUB_SUGGESTED_REPLY_SPLIT", True)
SUGGESTED_CALLBACK_PREFIX = "cub-sr"
SUGGESTED_BUTTON_TEXT = "확인"
DEFAULT_SUGGESTED_REPLY = "이어서 해줘"
SUGGESTED_SENT_BUTTON_TEXT = "✅ 보냄"
SUGGESTED_DONE_CALLBACK = f"{SUGGESTED_CALLBACK_PREFIX}:done"
SUGGESTED_STORE_MAX = 40
SUGGESTED_SURFACE = "direct"
SUGGESTED_SPLIT_LOG_KEY = "cub_suggested_split"
SUGGESTED_REPLY_INSTRUCTION = (
    "답 마지막 줄에는 반드시 <추천답변>...</추천답변> 형식으로, "
    "사용자가 다음에 그대로 보낼 만한 짧은 요청 1개를 작성해. "
    "...은 실제 문구로 바꾸고 마커 안은 한 줄만 써."
)
_SUGGESTED_INSTRUCTION_RE = re.compile(
    r"답\s*마지막\s*줄.{0,120}<추천답변>",
    re.IGNORECASE | re.DOTALL,
)
SUGGESTED_OPEN = "<" + "추천답변" + ">"
SUGGESTED_CLOSE = "</" + "추천답변" + ">"
# sol-260901-0854 — 폰에는 사람용 모델 답 1~2줄만. TUI/세션 원문·마커 금지.
# 0 = 줄 수 제한 없음(Claude·Grok 브릿지와 동일 — 모델 발화 전문을 보낸다. 길이 분할은
# 발신 래퍼가 4096 단위로 한다). 2026-09-04 11:4x 「장난해?」— 진행 보고가 2줄에서
# 잘려 「오늘 진행 상황 정리입니다. **완료**」만 도착했다. 양수면 그 줄 수에서 자른다.
PHONE_FACING_MAX_LINES = int_env("CUB_PHONE_MAX_LINES", 0, minimum=0)
# 2026-09-04 23:16 live fire: Cursor 일기장이 최종답 텍스트 블록 *뒤에* 그 턴의 생각 요약
# (영문 1인칭 산문 4~7단락)을 이어 붙였다. 폰에 "I also want to add ops/…" 버블이 갔다.
# 답 본문이 한글인데 꼬리가 한글 0·1인칭 영문 산문 덩어리면 그 덩어리를 걷는다.
STRIP_TRAILING_REASONING = bool_env("CUB_STRIP_TRAILING_REASONING", True)
_REASONING_OPENER_RE = re.compile(
    r"^(?:I|I'm|I’m|I'll|I’ll|I've|I’ve|I'd|I’d|Now I|So I|My plan|Let me|The user)\b"
)
# 2026-09-05 19:34 live fire: 꼬리가 "The diff check didn't show anything conclusive, …
# so I can go ahead and answer the user." — 첫 단어가 1인칭이 아니라 위 opener 를 비껴갔다.
# 생각 요약은 어디선가 반드시 1인칭(I/me/my)이나 "the user" 를 쓴다. 덩어리 안 어디든 있으면 리즈닝.
_REASONING_FIRST_PERSON_RE = re.compile(
    r"\b(?:I|I'm|I’m|I'll|I’ll|I've|I’ve|I'd|I’d|me|my|the user)\b"
)
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_PHONE_MARKER_RE = re.compile(r"\[REDACTED\]|\[sol-cycle[^\]]*\]", re.IGNORECASE)
_PHONE_DUMP_LINE_RE = re.compile(
    r"(?:^|\b)(?:CONFLICTING|DIRTY)\b|^\s*##\s+main\b|^\s*cub_tui_lane\b|^\s*grb_tui_lane\b",
    re.IGNORECASE,
)
# T-260902-009 — Agent 내부 thinking / skill / tool / status. 한 문구가 아니라 클래스.
_PHONE_AGENT_META_VERBS = (
    "considering|exploring|thinking|analyzing|planning|searching|"
    "investigating|reading|writing|running|using|invoking|checking|"
    "reviewing|looking|figuring|deciding|evaluating|inspecting"
)
_PHONE_AGENT_META_PREFIX_RE = re.compile(
    rf"(?ix)^(?:\*\*(?:{_PHONE_AGENT_META_VERBS})\b[^*]*\*\*\s*)+"
)
_PHONE_AGENT_META_LINE_RE = re.compile(
    rf"(?ix)^(?:"
    rf"\*\*(?:{_PHONE_AGENT_META_VERBS})\b[^*]*\*\*"
    rf"|(?:considering|exploring|thinking|analyzing|planning)\s+\S+"
    rf"|i need to (?:figure|check|investigate|determine|look|see)\b"
    rf"|(?:using|invoking|running|calling)\s+(?:the\s+)?[\w./-]+\s+"
    rf"(?:skill|subagent|tool|function)\b"
    rf"|(?:skill|tool|status)\s*:"
    rf"|askquestion\b"
    rf")"
)
TELEGRAM_ORIGIN_PROMPTS_MAX = 32
# T-260905-006 — 오케 배차 운반체는 사람이 터미널에 친 질문이 아니다.
# 미러가 첫 줄 `[claude-skills HEAD: …]` 를 「터미널에서 물어본 것」으로 폰에 에코하면
# 사용자가 「뭐냐이게」가 된다. 답은 보내고 라벨만 뺀다.
_DISPATCH_CARRIER_RE = re.compile(r"\[directive-carrier nonce:\s*carrier-\d+")
_DISPATCH_HEAD_RE = re.compile(
    r"^\[(?:claude-skills HEAD|CLAUDE-REVIEW-ROUTE|NODE-ACK-)",
    re.MULTILINE,
)
_DISPATCH_ROUTE_RE = re.compile(r"^from=\S+\s*\|?\s*task=", re.MULTILINE)
_DISPATCH_FLEET_RE = re.compile(r"\[APPROVED-FLEET(?:\s+task=|\])")

if not DRY_RUN:
    if not TOKEN_FILE or not os.path.isfile(TOKEN_FILE):
        sys.exit(f"❌ CUB_TOKEN_FILE 부재: {TOKEN_FILE}")
    with open(TOKEN_FILE, encoding="utf-8") as f:
        TOKEN = json.load(f).get("api_key", "").strip()
    if not TOKEN:
        sys.exit(f"❌ 토큰 비어있음: {TOKEN_FILE}")
else:
    TOKEN = ""

if not CHAT_ID:
    sys.exit(
        "CUB_CHAT_ID is not set. Set it to your own Telegram chat id - "
        "the bridge only answers that one chat. See README.md, Step 2."
    )

os.makedirs(STATE_DIR, exist_ok=True)
OFFSET_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.offset")
HEALTH_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.health.json")
INBOX_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.inbox.jsonl")
CURSOR_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.tui-cursor.json")
TUI_INFLIGHT_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.tui-inflight.json")
HEADLESS_TRANSCRIPTS_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.headless-transcripts.json")
SUGGESTED_STORE_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.suggested.json")
MODEL_STORE_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.model.json")
APPROVAL_STORE_FILE = os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.approval.json")
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""

JOBS: queue.Queue = queue.Queue()
_TUI_JOB_ACTIVE = threading.Event()
_TYPING_ACTIVE = threading.Event()
# 잡이 없는데도 커서 턴이 도는 중(로컬 타이핑 · busy 삽입 뒤 앞 턴 final 로 잡이 먼저 닫힘 ·
# 정체 15분 넘겨 wait 만 끝남). local mirror 가 일기장 꼬리를 읽을 때마다 갱신하고
# typing_loop 가 이걸 보고 「입력 중…」을 유지한다 (2026-09-06 00:2x 사용자 「진행하고있는데
# 입력중에 표시가안된다」).
_TUI_TURN_BUSY = threading.Event()
_COMPOSER_LOCK_HELD = threading.local()
_TUI_ADMISSION_LOCK = threading.RLock()
_HEALTH_LOCK = threading.Lock()
_HARVEST_GEN = 0
_LAST_FINAL_BODY = ""
_LAST_FINAL_LOCK = threading.Lock()
_HARVEST_LOCK = threading.Lock()
_TUI_RESET = threading.Event()
_HEALTH = {
    "pid": os.getpid(),
    "name": NAME,
    "started_at": time.time(),
    "last_poll_at": 0.0,
    "last_enqueue_at": 0.0,
    "last_job_started_at": 0.0,
    "last_job_done_at": 0.0,
    "enqueued": 0,
    "done": 0,
    "spool_errors": 0,
    "queue_depth": 0,
    "worker_alive": False,
}
_WORKER_THREAD = None
HEALTH_INTERVAL = int_env("CUB_HEALTH_INTERVAL", 30, minimum=5)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write(path, val):
    Path(path).write_text(str(val), encoding="utf-8")


def health_snapshot():
    """grok-telegram-bridge health_snapshot 동형 — 큐 깊이·워커 생존·기록 시각 포함."""
    with _HEALTH_LOCK:
        snap = dict(_HEALTH)
    snap["queue_depth"] = JOBS.qsize()
    snap["worker_alive"] = bool(_WORKER_THREAD and _WORKER_THREAD.is_alive())
    snap["written_at"] = time.time()
    return snap


def health_write():
    """상태 파일 원자 갱신. 실패해도 브릿지는 죽지 않는다(fail-open).

    tmp 경로는 호출마다 유일해야 한다 — grb T-260822-028 실측: 고정 tmp 를
    ticker·poller·enqueue·worker 네 스레드가 공유하면 os.replace 가 서로를 밟아
    ENOENT 로 죽는다. 유일 tmp + os.replace 면 락 없이 충돌이 없다.
    """
    tmp = f"{HEALTH_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        snap = health_snapshot()
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False)
        os.replace(tmp, HEALTH_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} health write 실패: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass


def health_mark(**fields):
    """카운터(enqueued/done/spool_errors)는 증분, 나머지는 대입 — grb 계약."""
    if fields:
        with _HEALTH_LOCK:
            for key, value in fields.items():
                if key in ("enqueued", "done", "spool_errors"):
                    _HEALTH[key] = _HEALTH.get(key, 0) + value
                else:
                    _HEALTH[key] = value
    health_write()


def health_ticker():
    """주기 하트비트. 적체가 있을 때만 로그로도 말한다(정상 시 침묵)."""
    while True:
        health_write()
        depth = JOBS.qsize()
        if depth > 0:
            snap = health_snapshot()
            idle = time.time() - (snap["last_job_done_at"] or snap["started_at"])
            print(
                f"{time.strftime('%m-%d %H:%M:%S')} "
                f"cursor-bridge[{NAME}] backlog queue_depth={depth} "
                f"enqueued={snap['enqueued']} done={snap['done']} "
                f"since_last_done={idle:.0f}s worker_alive={snap['worker_alive']}",
                flush=True,
            )
        time.sleep(HEALTH_INTERVAL)


def inbox_spool(update_id, source, text):
    """수신분을 offset 전진 전에 디스크에 남긴다. 실패는 health spool_errors 로 드러낸다."""
    rec = {
        "ts": time.time(),
        "update_id": update_id,
        "source": source,
        "text": text,
    }
    try:
        with open(INBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"{LOG_KEY} inbox spool 실패: {exc}", file=sys.stderr)
        health_mark(spool_errors=1)


def tg(method, timeout=60, **params):
    if DRY_RUN or not API:
        return {"ok": True, "result": []}
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API}/{method}" + (f"?{qs}" if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} tg {method} 실패: {type(exc).__name__}", file=sys.stderr)
        return {}


def _tmux(*args, input_text=None):
    cmd = ["tmux", "-L", TMUX_SOCKET, *args]
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def tui_session_alive():
    proc = _tmux("has-session", "-t", TMUX_SESSION)
    return proc.returncode == 0


def tui_dead_message():
    return f"커서 TUI 창({TMUX_SESSION})이 꺼져 있어. 그 창을 다시 열어줘."


def _tui_send_submit_key():
    if getattr(_COMPOSER_LOCK_HELD, "auto_clear", False):
        _context_guard_assert_pending_clear()
    proc = _tmux("send-keys", "-t", TMUX_PANE, TUI_SUBMIT_KEY)
    if getattr(proc, "returncode", 0) not in (0, None):
        print(
            f"{LOG_KEY} 제출키 실패 rc={proc.returncode} key={TUI_SUBMIT_KEY}",
            file=sys.stderr,
        )
    return proc


def _tui_waiting_chrome_row(screen):
    """visible pane 에서 `Waiting  N tokens` 줄 번호. 없으면 -1.

    T-260901-032 Athena 실측: pane h=24 alt=0 고정인데 wait_row 가 4 ↔ -1 로
    잠기면 본문 한 줄이 위아래로 점프한다. 4 vs 5 가 아니라 4 vs 없음.
    """
    for i, ln in enumerate((screen or "").splitlines()):
        if "Waiting" in ln and "token" in ln.lower():
            return i
    return -1


def _tui_repl_shows_turn_started(screen=None):
    """jsonl 이 늦을 때 턴이 시작됐는지 — 답 본문은 안 읽는다 (그록 T-260830-052).

    빈 화면·capture 실패 = 시작 증거 없음. Cursor 마커 = Waiting+token 크롬.
    enter steer / Esc:cancel 은 오버레이라 턴 시작이 아니다.
    """
    if screen is None:
        screen = _tui_capture_pane()
    if not (screen or "").strip():
        return False
    return _tui_waiting_chrome_row(screen) >= 0


def classify_tui_waiting_layout(wait_rows, *, heights=None, alts=None):
    """한 줄 점프 계측 분류. Athena 영상 SoT = waiting-chrome-layout-loop.

    heights 가 변하면 pane-height, alt 0↔1 이면 alternate-screen,
    wait 가 인접 행(4 vs 5)이면 row-off-by-one.
    {-1, N} (N>=0) 이면 크롬이 통째로 꺼졌다 켜진 layout-loop.
    옛 가설은 N=4, Athena 17:30 실측은 N=6.
    """
    waits = {int(w) for w in (wait_rows or [])}
    hs = {str(h) for h in (heights or []) if h is not None and str(h) != ""}
    als = {str(a) for a in (alts or []) if a is not None and str(a) != ""}
    if len(hs) > 1:
        return "pane-height"
    if "0" in als and "1" in als:
        return "alternate-screen"
    if waits in ({3, 4}, {4, 5}, {5, 6}):
        return "row-off-by-one"
    if -1 in waits and len(waits) == 2:
        other = next(iter(waits - {-1}))
        if other >= 0:
            return "waiting-chrome-layout-loop"
    return "stable"


def _tui_more_below_visible(screen):
    """스크롤바 `↓ more below` 가 보이면 True. wait_row 와 잠금걸음 (Athena 17:30)."""
    return "more below" in (screen or "").lower()


_LAYOUT_PREV_WAIT_ROW = -2


def _tui_layout_skip_keys(screen):
    """Waiting 크롬·more-below·직전 크롬 프레임이면 TUI 키 금지.

    Athena 17:30: wait_row {-1,6} 과 more-below 가 같이 켜졌다 꺼진다.
    -1 본문 프레임에서 Space/Enter 를 쏘면 크롬이 다시 켜져 한 줄이 점프한다.
    """
    global _LAYOUT_PREV_WAIT_ROW
    row = _tui_waiting_chrome_row(screen)
    more = _tui_more_below_visible(screen)
    prev = _LAYOUT_PREV_WAIT_ROW
    _LAYOUT_PREV_WAIT_ROW = row
    if row >= 0 or more:
        return True
    if prev >= 0 and row < 0:
        return True
    return False


def _tui_compose_line(screen):
    """입력칸 `→ …` 줄의 본문. 없으면 빈 문자열."""
    for line in reversed((screen or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("→"):
            return stripped[1:].strip()
        if stripped.startswith("->"):
            return stripped[2:].strip()
    return ""


def _tui_compose_is_placeholder(screen):
    """Plan, search / Add a follow-up 자리표시면 True — 여기엔 Enter 금지."""
    line = _tui_compose_line(screen)
    if not line:
        return True
    low = line.lower()
    if low.startswith("add a follow-up"):
        return True
    if low.startswith("plan, search"):
        return True
    return False


def _tui_compose_shows_idle_placeholder(screen):
    """확인된 idle compose. 빈 화면·출처 불명 → 줄은 placeholder 가 아니다.

    `_tui_compose_is_placeholder` 는 제출 Enter 금지용이라 빈 화면도 True 다.
    context-guard 는 그 계약을 쓰면 안 된다 (T-260907-019).
    """
    line = _tui_compose_line(screen)
    if not line:
        return False  # unknown compose
    low = line.lower()
    if low.startswith("add a follow-up"):
        return True
    if low.startswith("plan, search"):
        return True
    return False


def _tui_pane_shows_guard_busy(screen):
    """TUI 가 턴·오버레이·중단키를 보여 주면 True. harvest parser 는 건드리지 않는다."""
    if _tui_repl_shows_turn_started(screen):
        return True
    if _tui_pane_shows_steer_overlay(screen):
        return True
    if _tui_pane_shows_approval_overlay(screen):
        return True
    if _tui_pane_shows_model_picker(screen):
        return True
    tail = "\n".join((screen or "").splitlines()[-16:])
    low = tail.lower()
    if "ctrl+c" in low or "ctrl-c" in low or "⌃c" in tail or "to stop" in low:
        return True
    if re.search(r"(?i)\bgenerating\b", tail) or re.search(r"(?i)\bthinking\b", tail):
        return True
    if re.search(r"(?i)\brunning\b", tail) and "run everything" not in low:
        return True
    # 2026-09-07 live: `Reading  79.29k tokens` / `Grepping  75.94k tokens`
    # 은 Waiting 크롬이 아닌데 placeholder 와 같이 떠 있다.
    if re.search(
        r"(?i)\b(?:reading|grepping|searching|running|waiting)\b[^\n]*\btokens\b",
        screen or "",
    ):
        return True
    return False


def _tui_is_reset_prompt(prompt):
    token = (prompt or "").strip().split(maxsplit=1)
    if not token:
        return False
    return token[0].lower() in TUI_RESET_TOKENS


def _tui_is_native_slash_prompt(prompt):
    """Cursor TUI 슬래시(/clear·/new·/context). 스킬 팔레트와 구분한다."""
    token = (prompt or "").strip().split(maxsplit=1)
    if not token:
        return False
    return token[0].lower().split("@", 1)[0] in TUI_NATIVE_SLASH_TOKENS


def _tui_pane_shows_steer_overlay(screen):
    """Cursor follow-up/steer 오버레이가 떠 있으면 True (post-#2100 실측 마커)."""
    text = screen or ""
    low = text.lower()
    if "enter steer" in low or "esc cancel" in low:
        return True
    # "Add a follow-up" 은 유휴 입력칸. 복수 follow-ups 박스만 overlay.
    if "follow-ups" in low:
        return "○" in text or "enter steer" in low
    return False


def _tui_active_approval_text(screen):
    """과거 대화/일반 상태줄을 걷어낸 현재 Approval 후보 영역."""
    lines = (screen or "").splitlines()
    active_start = 0
    for idx, line in enumerate(lines):
        idle_compose = re.match(
            r"^\s*(?:→|->)\s*(?:add a follow-up|plan,\s*search)\b",
            line,
            re.IGNORECASE,
        )
        ordinary_status = (
            "run everything" in line.lower()
            and "shift+tab" not in line.lower()
        )
        if idle_compose or ordinary_status:
            active_start = idx + 1
    return "\n".join(lines[active_start:]).strip()


def _tui_pane_shows_approval_overlay(screen):
    """Cursor tool-approval 모달. Space/Enter 는 Run(y) 이지 compose 제출이 아니다.

    Athena 2026-09-02 22:17: Approval 1 of 5 + `rtk ls` 위에 cub 이
    Space/paste/Enter 를 넣어 입력을 가로채고 창이 먹통이 됐다.
    과거 대화에 남은 Approval 문구는 현재 compose 아래 상태줄의
    Run Everything 과 결합해 모달로 오인하면 안 된다 (T-260903-006).
    """
    low = _tui_active_approval_text(screen).lower()
    return bool(
        re.search(r"(?m)^\s*approval\s+\d+\s+of\s+\d+\s*$", low)
        and re.search(r"(?m)^\s*run this command\?\s*$", low)
        and re.search(r"(?m)^\s*(?:→\s*)?run \(once\)", low)
        and "to switch approval" in low
    )


def _tui_approval_signature(screen):
    """현재 모달이 callback 생성 뒤 바뀌지 않았는지 확인하는 짧은 서명."""
    active = _tui_active_approval_text(screen)
    if not active or not _tui_pane_shows_approval_overlay(screen):
        return ""
    normalized = "\n".join(line.rstrip() for line in active.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _tui_prompt_visible_in_pane(screen, prompt):
    """입력칸(→ 줄)에 prompt 가 남아 있으면 True.

    제출된 질문은 pane 대화에도 그대로 보인다. 화면 전체에서 찾으면
    제출 성공+jsonl 지연을 stuck 으로 오탐하고 Enter 재발사·에러토스트가 난다
    (TOAST-H-1647 live).
    """
    needle = (prompt or "").strip()
    if not needle or not screen:
        return False
    snippet = needle.splitlines()[0].strip()[:80]
    if not snippet:
        return False
    for line in reversed(screen.splitlines()):
        stripped = line.strip()
        if stripped.startswith("→") or stripped.startswith("->"):
            return snippet in stripped
    return False


def _tui_prompt_queued_in_followups(screen, prompt):
    """붙여넣은 글이 Cursor follow-ups 박스의 ○ 항목으로 잡혀 있으면 True.

    턴이 도는 중 Enter 로 제출한 글은 일기장 user 행이 되지 않고
    ┌─ follow-ups ─┐ 박스 안 `○ <첫 줄>` 로 큐에 선다 (2026-09-04 11:5x 실측).
    이 상태를 실패로 읽으면 잡이 큐로 되돌아가 같은 글을 두 번 붙인다
    (사용자 같은 사진 3번 도착). 박스는 폭에 맞춰 잘리므로 첫 줄 머리만 본다.
    """
    needle = (prompt or "").strip()
    if not needle or not screen:
        return False
    head = needle.splitlines()[0].strip()
    snippet = head[:24].rstrip()
    if len(snippet) < 4:
        return False
    for line in screen.splitlines():
        stripped = line.strip().lstrip("│").strip()
        if stripped.startswith("○") and snippet in stripped:
            return True
    return False


def _tui_submit_took(path, prompt):
    """붙여넣은 글이 일기장 user 행으로 남았으면 제출된 것이다.

    pane 의 Running/Waiting 은 이미 도는 턴에서도 켜져 있어 확인 신호로
    쓰지 않는다 (ENTER-TRACE-1537: follow-up 큐에만 쌓인 채 Running).
    """
    needle = (prompt or "").strip()
    if not needle:
        return False
    live = newest_transcript_path() or path
    if live and _transcript_has_pasted_user(live, needle):
        return True
    if path and live != path and _transcript_has_pasted_user(path, needle):
        return True
    return False


def _tui_wait_submit_took(path, prompt, budget):
    deadline = time.time() + max(0.0, float(budget or 0.0))
    while True:
        if _tui_submit_took(path, prompt):
            return True
        screen = _tui_capture_pane()
        started = _tui_repl_shows_turn_started(screen)
        leftover = _tui_prompt_visible_in_pane(screen, prompt)
        if started and leftover and (
            _tui_pane_shows_steer_overlay(screen) or _tui_is_reset_prompt(prompt)
        ):
            leftover_blocks = True
        else:
            leftover_blocks = False
        if started and not leftover_blocks:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(min(max(TUI_POLL_INTERVAL, 0.01), 0.15))


def _tui_confirm_submit(prompt, path=None):
    """paste 직후 턴이 시작됐는지 보고, 안 됐으면 조건에 따라 복구한다.

    #2100 이 Enter 재발사만 넣었는데, 첫 Enter 가 follow-up/steer 오버레이를
    열면 같은 Enter 재발사는 또 먹힌다 (macOS 노드 live post-#2100). prompt 가
    compose/overlay 에 남아 있고 user 행이 없을 때만 Escape→Enter. 일기장만
    느리면 키를 더 쏘지 않고 기다린다. Waiting 크롬이면 키 금지 (T-260901-032).
    jsonl 침묵이어도 Waiting 이면 제출된 것으로 본다 (그록 T-260830-052).
    compose/overlay 잔류가 없는데 jsonl 이 늦으면 RuntimeError(에러토스트) 를
    올리지 않는다 — wait_for_final/harvest 가 답을 배달한다
    (Hermes/Vulcan 16:30-16:35).
    """
    if not TUI_SUBMIT_CONFIRM:
        return
    watch = path or newest_transcript_path()
    if _tui_wait_submit_took(watch, prompt, TUI_SUBMIT_CONFIRM_WAIT):
        return
    retries = max(0, int(TUI_SUBMIT_RETRY or 0))
    for attempt in range(1, retries + 1):
        if _tui_submit_took(watch, prompt):
            return
        screen = _tui_capture_pane()
        stuck = _tui_prompt_visible_in_pane(screen, prompt)
        if not stuck and _tui_prompt_queued_in_followups(screen, prompt):
            print(
                f"{LOG_KEY} follow-up 큐에 들어갔다 — 제출된 것으로 본다 ({attempt}/{retries})",
                file=sys.stderr,
            )
            return
        overlay = _tui_pane_shows_steer_overlay(screen)
        approval = _tui_pane_shows_approval_overlay(screen)
        skip = _tui_layout_skip_keys(screen)
        placeholder = _tui_compose_is_placeholder(screen)
        native_slash = _tui_is_native_slash_prompt(prompt)
        if approval:
            print(
                f"{LOG_KEY} Approval 오버레이 — 키 생략 ({attempt}/{retries})",
                file=sys.stderr,
            )
        elif native_slash and not placeholder:
            # T-260901-051: 팔레트에 Start a new chat 과 /session-clear 가
            # 같이 있으면 Esc 로 접지 말고 TUI 슬래시를 Enter 로 확정.
            print(
                f"{LOG_KEY} TUI 슬래시 제출키 ({attempt}/{retries})",
                file=sys.stderr,
            )
            _tui_send_submit_key()  # slash-enter
        elif skip and overlay:
            # Waiting+steer 직후 Esc 는 레이아웃 루프를 키운다 (T-260901-032).
            print(
                f"{LOG_KEY} Waiting 크롬 — 키 생략 ({attempt}/{retries})",
                file=sys.stderr,
            )
        elif skip and not (stuck and not placeholder):
            # leftover 가 우리 붙여넣기면 제출이 먼저다 (T-260901-044 / 18:25).
            print(
                f"{LOG_KEY} Waiting 크롬 — 키 생략 ({attempt}/{retries})",
                file=sys.stderr,
            )
        elif stuck and overlay:
            print(
                f"{LOG_KEY} steer 오버레이 닫고 재제출 ({attempt}/{retries})",
                file=sys.stderr,
            )
            _tmux("send-keys", "-t", TMUX_PANE, "Escape")
            time.sleep(TUI_SUBMIT_DELAY)
            _tui_send_submit_key()
        elif stuck:
            # leftover compose. turn-started wait 가 이미 일기장 지연을 걸렀다.
            print(f"{LOG_KEY} 제출키 재발사 ({attempt}/{retries})", file=sys.stderr)
            _tui_send_submit_key()
        else:
            print(
                f"{LOG_KEY} 일기장 대기 (compose/overlay 잔류 없음) ({attempt}/{retries})",
                file=sys.stderr,
            )
        time.sleep(TUI_SUBMIT_DELAY)
        watch = newest_transcript_path() or watch
        if _tui_wait_submit_took(watch, prompt, TUI_SUBMIT_CONFIRM_WAIT):
            return
    watch = newest_transcript_path() or watch
    if _tui_submit_took(watch, prompt):
        return
    screen = _tui_capture_pane()
    stuck = _tui_prompt_visible_in_pane(screen, prompt)
    overlay = _tui_pane_shows_steer_overlay(screen)
    approval = _tui_pane_shows_approval_overlay(screen)
    started = _tui_repl_shows_turn_started(screen)
    if approval:
        raise RuntimeError("TUI 승인 대기 중 — 키 주입 금지")
    if not stuck and _tui_prompt_queued_in_followups(screen, prompt):
        # 큐에 선 글은 지금 턴이 끝나면 TUI 가 스스로 돌린다. 실패가 아니다.
        print(
            f"{LOG_KEY} follow-up 큐에 들어갔다 — 제출된 것으로 본다",
            file=sys.stderr,
        )
        return
    if started and not (stuck and overlay):
        print(
            f"{LOG_KEY} 일기장 침묵이지만 창은 돌아가는 중 — 제출된 것으로 본다",
            file=sys.stderr,
        )
        return
    if not stuck and not overlay:
        # Live Hermes/Vulcan post-#2101: 첫 Enter 로 compose 는 비었고 jsonl 만 늦다.
        # 여기서 RuntimeError 를 내면 폰에 에러토스트가 먼저 가고, harvest 답이 뒤에 온다.
        print(
            f"{LOG_KEY} 일기장 침묵이지만 입력칸은 비었다 — 제출된 것으로 본다",
            file=sys.stderr,
        )
        return
    raise RuntimeError("붙여넣기는 됐는데 제출이 안 됐다 — 입력칸에 글이 남아 있다")


def slash_token(text):
    """텔레그램 한 줄 슬래시 명령 토큰. clb/grok slash_token 과 같은 계약."""
    stripped = (text or "").strip()
    if "\n" in stripped or not stripped.startswith("/"):
        return ""
    return stripped.split(maxsplit=1)[0].split("@", 1)[0].lower()


def is_context_command(text):
    return slash_token(text) == "/context"


def _tui_capture_pane():
    proc = _tmux("capture-pane", "-p", "-t", TMUX_PANE, "-S", "-40")
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _format_context_percent(value):
    text = f"{float(value):.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def _tui_context_line_is_free(line):
    """Free space / 빈칸 / 100-used. intern 63.2% 를 used 로 읽으면 RED."""
    low = (line or "").lower()
    if "free space" in low or "free-space" in low:
        return True
    if "unused" in low or "remaining" in low:
        return True
    if "빈칸" in (line or "") or "여유" in (line or ""):
        return True
    if re.search(r"\bfree\b", low) and "%" in (line or ""):
        return True
    return False


def parse_tui_context_percent(screen):
    """상태줄 used % / intern `73.6K/200K`. Free space 63.2 는 used 가 아니다."""
    if not screen:
        return None
    lines = [ln for ln in reversed(screen.splitlines()) if not _tui_context_line_is_free(ln)]
    for line in lines:
        match = TUI_CONTEXT_USED_RE.search(line)
        if match:
            return match.group(1)
    for line in lines:
        match = TUI_CONTEXT_STATUS_RE.search(line)
        if match:
            return match.group(1)
    for line in lines:
        match = TUI_CONTEXT_TOKENS_RE.search(line)
        if match:
            used = float(match.group(1))
            total = float(match.group(2))
            if total > 0:
                return _format_context_percent(used / total * 100)
    for line in lines:
        match = TUI_CONTEXT_PERCENT_RE.search(line)
        if match:
            return match.group(1)
    return _parse_wrapped_context_percent(screen)


# 좁은 pane(≤60col) 에서 상태줄이 두 줄로 접힌다:
#   "  Cursor Grok   · 87.9· 60 files     Run Everything"
#   "  4.6 High        %     edited"
# 숫자와 % 가 다른 줄에 떨어져 위 정규식이 전부 None → 컨텍스트 가드가 침묵한다 (T-260906-011 후속).
TUI_CONTEXT_WRAPPED_NUM_RE = re.compile(r"·\s*(\d+(?:\.\d+)?)\s*·")
TUI_CONTEXT_WRAPPED_PCT_RE = re.compile(r"(?:^|\s)%(?:\s|$)")


def _parse_wrapped_context_percent(screen):
    rows = (screen or "").splitlines()
    for idx in range(len(rows) - 1, 0, -1):
        line, prev = rows[idx], rows[idx - 1]
        if not TUI_CONTEXT_WRAPPED_PCT_RE.search(line):
            continue
        if _tui_context_line_is_free(prev) or _tui_context_line_is_free(line):
            continue
        match = TUI_CONTEXT_WRAPPED_NUM_RE.search(prev)
        if match:
            return match.group(1)
    return None


def awaiting_human_path():
    return os.path.join(STATE_DIR, f"cursor-bridge-{NAME}.awaiting-human")


def set_awaiting_human():
    path = awaiting_human_path()
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{time.time()}\n")


def clear_awaiting_human():
    try:
        os.remove(awaiting_human_path())
    except FileNotFoundError:
        pass


def is_awaiting_human():
    return os.path.isfile(awaiting_human_path())


def drain_pending_jobs():
    dropped = 0
    while True:
        try:
            JOBS.get_nowait()
        except queue.Empty:
            break
        try:
            JOBS.task_done()
        except ValueError:
            pass
        dropped += 1
    return dropped


def forget_suggested_replies():
    _write_suggested_store({})


def transcript_is_aborted_only(rows):
    """turn_ended aborted 만 남은 옛 일기장. /clear 직후 mtime 최상위면 회수가 0이 된다."""
    if not rows:
        return False
    for row in rows:
        if row.get("role") in {"user", "assistant"}:
            return False
        if row.get("type") == "turn_ended" and row.get("status") != "aborted":
            return False
    return any(row.get("type") == "turn_ended" for row in rows)


def _transcript_has_pasted_user(path, pasted_text):
    token = (pasted_text or "").strip()
    if not token or not path:
        return False
    for row in reversed(read_transcript_rows(path)):
        if row.get("role") != "user":
            continue
        q = user_query_text(row) or "\n".join(_content_texts(row)).strip()
        return token in q or q == token
    return False


def _job_started_at():
    """inflight/job 시각. 없으면 지금 — 옛 mtime 일기장을 토큰 매칭에서 빼기 위해."""
    job = _tui_inflight_load()
    if job:
        for key in ("started_at", "ts", "job_started_at"):
            try:
                val = float(job.get(key) or 0)
            except (TypeError, ValueError):
                val = 0.0
            if val > 0:
                return val
    try:
        started = float(_HEALTH.get("last_job_started_at") or 0)
    except (TypeError, ValueError):
        started = 0.0
    if started > 0:
        return started
    return time.time()


def _transcript_mtime_fresh_for_job(path, started_at=None, slack=None):
    slack = TRANSCRIPT_PICK_MTIME_SLACK_SEC if slack is None else float(slack)
    started = _job_started_at() if started_at is None else float(started_at)
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    return mtime >= (started - slack)


def _stale_token_transcript(path, token, started_at, rows=None):
    """같은 질문이 마지막 user 여도 job 시작 전 mtime 이면 오선택 후보다."""
    if not token or not path:
        return False
    if _transcript_mtime_fresh_for_job(path, started_at):
        return False
    if rows is None:
        return _transcript_has_pasted_user(path, token)
    for row in reversed(rows):
        if row.get("role") != "user":
            continue
        q = user_query_text(row) or "\n".join(_content_texts(row)).strip()
        return token in q or q == token
    return False


def _pick_active_transcript_path(prior_path=None, pasted_text=None):
    """한 번 스캔 — paste 직후 mtime-only newest 가 옛 일기장일 수 있다."""
    paths = list_transcript_paths()
    if not paths:
        return None
    token = (pasted_text or "").strip()
    started = _job_started_at()

    def usable(path, rows=None):
        rows = read_transcript_rows(path) if rows is None else rows
        if transcript_is_aborted_only(rows):
            return False
        return not _stale_token_transcript(path, token, started, rows)

    if token:
        for path in reversed(paths):
            rows = read_transcript_rows(path)
            if not usable(path, rows):
                continue
            if _transcript_has_pasted_user(path, token):
                return path
    # 붙여넣은 글이 아직 어디에도 없으면 방금 붙여넣은 TUI 일기장(prior)을 지킨다.
    # mtime 최신이 다른 세션 일기장이면 그쪽으로 튀어 영영 '침묵'으로 보인다.
    # 토큰이 옛 일기장에만 있으면 그쪽으로 떨어지지 않는다 (T-260905-005).
    if prior_path:
        prior = Path(prior_path)
        if prior in paths and usable(prior):
            return prior
    for path in reversed(paths):
        if usable(path):
            return path
    return paths[-1]


def resolve_active_transcript_path(prior_path=None, pasted_text=None, deadline=None):
    """Paste 직후 live jsonl 이 늦게 생기면 pin≠live race 로 job 이 stuck 된다."""
    token = (pasted_text or "").strip()
    end = time.time() + TUI_PASTE_RESOLVE_SEC if deadline is None else float(deadline)
    while time.time() < end:
        path = _pick_active_transcript_path(prior_path, pasted_text)
        if path and token and _transcript_has_pasted_user(path, token):
            return path
        if path and prior_path and str(path) != str(prior_path):
            if not token or _transcript_has_pasted_user(path, token):
                return path
        time.sleep(TUI_POLL_INTERVAL)
    return _pick_active_transcript_path(prior_path, pasted_text)


def reset_cursor_harvest_state():
    """/clear 뒤 옛 pin(hello 전 세션)이 새 일기장 회수를 막지 않게 커서를 비운다."""
    try:
        os.remove(CURSOR_FILE)
    except FileNotFoundError:
        pass


def _tui_json_load(path):
    try:
        return json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return {}


def _tui_json_save(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _tui_inflight_load():
    return _tui_json_load(TUI_INFLIGHT_FILE)


def _tui_inflight_write(payload):
    rec = dict(payload or {})
    rec["ts"] = time.time()
    _tui_json_save(TUI_INFLIGHT_FILE, rec)


def _tui_inflight_clear():
    try:
        os.unlink(TUI_INFLIGHT_FILE)
    except FileNotFoundError:
        pass


def _inflight_payload(path, baseline, source, text, *, prior_path=None, started_at=None):
    preview = (text or "").strip().splitlines()[0][:80] if text else ""
    payload = {
        "path": str(path or ""),
        "baseline": int(baseline or 0),
        "source": source or "telegram",
        "text": text or "",
        "preview": preview,
        "started_at": float(started_at if started_at is not None else time.time()),
    }
    if prior_path:
        payload["prior_path"] = str(prior_path)
    return payload


def _mesh_delivery_sent(result):
    if not isinstance(result, dict):
        return False
    r = str(result.get("result") or "")
    if r in {"sent", "dry_run", "skipped_duplicate"}:
        return True
    for item in result.get("deliveries") or []:
        if isinstance(item, dict) and str(item.get("result") or "") == "sent":
            return True
    return False


def _answer_already_delivered(path, answer):
    if not path or not answer:
        return False
    rec = load_cursor_state(path)
    # transcript cursor는 "읽은 위치"일 뿐 Telegram 착탄 영수증이 아니다.
    # 재시작 직전 cursor만 전진한 경우 last_final/finals_sent를 신뢰하면 답이 유실된다.
    return answer == str(rec.get("delivered_final") or "")


def _deliver_job_outcome(path, answer, gen, *, inflight_recovery=False):
    """process_job / inflight 회수 공용. True = 배달됐거나 이미 보낸 상태."""
    if not path:
        return False
    if inflight_recovery and _answer_already_delivered(path, answer):
        return True
    sent = harvest_orphaned_finals(path)
    if sent:
        return True
    if gen is not None and gen != _HARVEST_GEN:
        return False
    if not answer:
        return False
    rec = load_cursor_state(path)
    if answer == str(rec.get("delivered_final") or ""):
        return True
    delivered = _mesh_delivery_sent(deliver_cursor_answer(answer))
    if delivered:
        rows = read_transcript_rows(path)
        save_cursor(
            path,
            len(rows),
            last_final=answer,
            finals_sent=len(assistant_final_pairs(rows)),
            delivered_final=answer,
        )
    return delivered


def _cub_wait_inflight_if_any():
    """기동 직후: 죽기 전 붙여넣은 턴의 답이 아직 안 왔으면 기다린다. 붙여넣기는 안 한다."""
    job = _tui_inflight_load()
    if not job:
        return None
    path_s = str(job.get("path") or "")
    if not path_s:
        _tui_inflight_clear()
        return None
    path = Path(path_s)
    if not path.is_file():
        _tui_inflight_clear()
        return None
    if not tui_session_alive():
        return {"job": job, "path": path, "answer": "", "gen": _HARVEST_GEN}
    try:
        baseline = int(job.get("baseline") or 0)
    except (TypeError, ValueError):
        return {"job": job, "path": path, "answer": "", "gen": _HARVEST_GEN}
    prior_s = str(job.get("prior_path") or job.get("path") or "")
    prior_path = Path(prior_s) if prior_s else None
    text = str(job.get("text") or "")
    deadline = time.time() + TUI_WAIT_SEC
    gen = _HARVEST_GEN
    answer, path = wait_for_final(
        path,
        baseline,
        deadline,
        gen=gen,
        pasted_text=text,
        prior_path=prior_path,
    )
    return {"job": job, "path": path, "answer": answer, "gen": gen}


def recover_inflight_on_startup():
    """Grok T-260823-051 cursor port — restart 뒤 in-flight wait 를 이어 받아 배달한다."""
    if not _tui_inflight_load():
        return
    print(f"{LOG_KEY} inflight 회수 시작", flush=True)
    _TUI_JOB_ACTIVE.set()
    delivered = False
    try:
        waited = _cub_wait_inflight_if_any()
        if not waited:
            return
        path = waited.get("path")
        answer = str(waited.get("answer") or "")
        gen = waited.get("gen")
        job = waited.get("job") or {}
        if path:
            try:
                bind_cursor_if_needed(path, int(job.get("baseline") or 0))
            except (TypeError, ValueError):
                bind_cursor_if_needed(path, None)
        delivered = _deliver_job_outcome(path, answer, gen, inflight_recovery=True)
        if delivered:
            print(f"{LOG_KEY} inflight 회수 1건", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} inflight 회수 실패: {exc}", file=sys.stderr)
    finally:
        _TUI_JOB_ACTIVE.clear()
        if delivered:
            _tui_inflight_clear()


def notify_tui_cleared():
    deliver_mesh_event("final", TUI_CLEAR_CONFIRM)


def _tui_context_result_overlay(screen):
    """TUI /context 결과 패널. 슬래시 팔레트와 다르다 — 읽은 뒤 Esc 로 닫는다."""
    text = screen or ""
    if "Esc to close" in text:
        return True
    return "Context •" in text


def handle_context_command(source="telegram"):
    """텔레그램 /context — TUI 에 `/context`+Enter 를 친 뒤 상태줄 % 를 한 줄 회신.

    숫자 % 가 있으면 그 값. 0·빈 세션·No context usage yet 는 0% 다.
    「못 읽음」「안 보임」·mesh error 로 바꾸지 않는다 (AMEND T-260901-053).
    """
    del source  # 회신 경로는 mesh 고정
    if not tui_session_alive():
        deliver_mesh_event("error", tui_dead_message())
        return
    try:
        _tui_paste("/context", interrupt=False)
    except Exception as exc:  # noqa: BLE001
        deliver_mesh_event("error", f"커서 /context 주입 실패: {exc}")
        return
    screen = _tui_capture_pane()
    pct = parse_tui_context_percent(screen)
    if pct is None and not _tui_context_result_overlay(screen):
        # slash Enter 직후 오버레이/상태줄이 한 프레임 늦을 수 있다.
        # #2105 Waiting 가림 재시도와 다른 축 — 한 번만 더 읽는다.
        time.sleep(TUI_SUBMIT_DELAY)
        screen = _tui_capture_pane()
        pct = parse_tui_context_percent(screen)
    if _tui_context_result_overlay(screen):
        _tmux("send-keys", "-t", TMUX_PANE, "Escape")
        time.sleep(TUI_SUBMIT_DELAY)
        screen = _tui_capture_pane()
        after_esc = parse_tui_context_percent(screen)
        if after_esc is not None:
            pct = after_esc
    pct = pct if pct is not None else "0"
    deliver_mesh_event("final", f"Context: {pct}% used")


def composer_lock_path():
    """fleet_dispatch 가 cursor 엔진에 쓰는 기존 composer flock 파일."""
    override = os.environ.get("CUB_COMPOSER_LOCK")
    if override:
        return Path(override).expanduser()
    return Path(STATE_DIR) / "cursor-composer.lock"


@contextmanager
def composer_lock(*, blocking=True):
    """같은 프로세스 재진입은 통과. 다른 writer 와는 flock 으로 직렬화."""
    if getattr(_COMPOSER_LOCK_HELD, "held", False):
        yield
        return
    path = composer_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        _COMPOSER_LOCK_HELD.held = True
        try:
            yield
        finally:
            _COMPOSER_LOCK_HELD.held = False
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _context_guard_inflight_active():
    try:
        job = json.loads(Path(TUI_INFLIGHT_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, UnicodeError):
        return True
    if not isinstance(job, dict):
        return True
    if not job:
        return False
    return bool(job.get("path") or job.get("text") or job.get("preview") or job.get("source"))


def _context_guard_rows_unfinished(rows):
    """명시 turn 종료가 없으면 미완료·불명. harvest 의 tail_is_busy 와 다르다.

    tail_is_busy 는 assistant 텍스트면 끝난 것으로 본다. 가드는 turn_ended 가
    있어야 idle 로 확정한다. 중간 텍스트·tool_use·user 대기는 모두 보류.
    """
    if not rows:
        return True
    for last in reversed(rows):
        if last.get("type") == "turn_ended":
            return last.get("status") != "success"
        if last.get("role") in {"assistant", "user"}:
            return True  # assistant text without turn_ended is unknown
    return True


def _context_guard_mtime_newest_transcript():
    """harvest pin 을 무시하고 mtime 최신 jsonl. newest_transcript_path 는 bound 를 선호한다."""
    paths = list_transcript_paths()
    return paths[-1] if paths else None


def _context_guard_fleet_context_dir():
    override = os.environ.get("CUB_FLEET_CONTEXT_DIR")
    if override:
        return Path(override).expanduser()
    return Path(STATE_DIR) / "dispatch-context"


def _context_guard_fleet_blocks_clear():
    """The fleet carrier persists submitting/submitted before releasing flock.

    That existing record bridges the gap before Cursor renders the new turn.
    Only a successfully ended transcript containing this submission releases it.
    """
    folder = _context_guard_fleet_context_dir()
    try:
        if not folder.exists():
            return False
        records = []
        for path in folder.glob("*.json"):
            rec = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rec, dict):
                return True
            if rec.get("target") == {"node": NODE_KEY, "engine": "cursor"}:
                records.append((path.stat().st_mtime_ns, rec))
        if not records:
            return False
        _, rec = max(records, key=lambda item: item[0])
        task_id = str(rec.get("task_id") or "")
        if rec.get("status") != "submitted" or not re.fullmatch(r"T-\d{6}-\d{2,}", task_id):
            return True
        state = rec.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("dispatched"), list):
            return True
        starts = [entry.get("at") for entry in state["dispatched"] if isinstance(entry, dict)
                  and entry.get("task_id") == task_id and entry.get("worker") == f"{NODE_KEY}:cursor"]
        if len(starts) != 1 or not isinstance(starts[0], str):
            return True
        started = datetime.datetime.fromisoformat(starts[0].replace("Z", "+00:00"))
        if started.tzinfo is None:
            return True
        # The immutable sender timestamp precedes paste. The final atomic
        # submitted rewrite may occur after a fast turn has already ended.
        submitted_ns = int(started.timestamp() * 1_000_000_000)
        marker = f"[APPROVED-FLEET task={task_id} "
        for path in reversed(list_transcript_paths()):
            before = path.stat()
            if before.st_mtime_ns < submitted_ns:
                continue
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            after = path.stat()
            if ((before.st_mtime_ns, before.st_size, before.st_ino)
                    != (after.st_mtime_ns, after.st_size, after.st_ino)):
                return True
            if any(not isinstance(row, dict) for row in rows):
                return True
            if any(row.get("role") == "user" and any(marker in text for text in _content_texts(row)) for row in rows):
                return _context_guard_rows_unfinished(rows)
        return True
    except (OSError, ValueError, UnicodeError):
        return True


def _context_guard_transcript_blocks_clear():
    """활성 일기장이 미종료·불명이거나 pin 과 live 가 갈리면 clear 금지."""
    if _context_guard_inflight_active():
        return True
    if _context_guard_fleet_blocks_clear():
        return True
    newest = _context_guard_mtime_newest_transcript()
    try:
        state = json.loads(Path(CURSOR_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError, UnicodeError):
        return True
    if not isinstance(state, dict):
        return True
    bound_s = str(state.get("path") or "")
    bound = Path(bound_s) if bound_s else None
    seen = set()
    candidates = []
    if newest:
        candidates.append(newest)
    if bound is not None:
        candidates.append(bound)
    if not candidates:
        return True
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path or not Path(path).is_file():
            return True
        # Harvest tolerates partial JSONL; destructive auto-clear must not.
        try:
            before = Path(path).stat()
            rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            after = Path(path).stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                return True
            if any(not isinstance(row, dict) for row in rows):
                return True
        except (OSError, ValueError, UnicodeError):
            return True
        if _context_guard_rows_unfinished(rows):
            return True
    return False


def _context_guard_screen_allows_clear(screen):
    if not _tui_compose_shows_idle_placeholder(screen):
        return False
    if _tui_pane_shows_guard_busy(screen):
        return False
    if _tui_pane_shows_approval_overlay(screen) or _tui_pane_shows_model_picker(screen):
        return False
    return True


def _context_guard_footer_percent(screen):
    lines = (screen or "").splitlines()
    compose = [i for i, line in enumerate(lines) if line.lstrip().startswith("→")]
    return parse_tui_context_percent("\n".join(lines[compose[-1]:])) if compose else None


def _context_guard_ready_to_clear(screen=None):
    """락 안에서 새 capture + busy/queue/inflight/transcript 를 다시 본다. 불명이면 None."""
    if is_awaiting_human() or _TUI_RESET.is_set() or _TUI_JOB_ACTIVE.is_set():
        return None
    if _TUI_TURN_BUSY.is_set():
        return None
    if not JOBS.empty():
        return None
    if _context_guard_inflight_active():
        return None
    screen = _tui_capture_pane() if screen is None else screen
    if _TUI_JOB_ACTIVE.is_set() or _TUI_TURN_BUSY.is_set() or not JOBS.empty():
        return None
    if _context_guard_inflight_active():
        return None
    if not _context_guard_screen_allows_clear(screen):
        return None
    if _context_guard_transcript_blocks_clear():
        return None
    raw = _context_guard_footer_percent(screen)
    if raw is None:
        return None
    try:
        used = float(raw)
    except (TypeError, ValueError):
        return None
    if used < CUB_CONTEXT_CLEAR_PCT:
        return None
    if not tui_session_alive():
        return None
    return screen


def _context_guard_assert_pending_clear():
    """Recheck immediately before Enter, including retries of our own /clear."""
    screen = _tui_capture_pane()
    compose = _tui_compose_line(screen)
    owns_clear = re.fullmatch(r"/clear(?:\s{2,}Start a new chat.*)?", compose)
    if (not owns_clear or _tui_pane_shows_guard_busy(screen)
            or is_awaiting_human() or _TUI_RESET.is_set()
            or _TUI_JOB_ACTIVE.is_set() or _TUI_TURN_BUSY.is_set()
            or not JOBS.empty() or _context_guard_transcript_blocks_clear()):
        raise RuntimeError("context-guard: state changed before submit")


def _context_guard_discard_own_clear():
    """Rollback only our exact unsubmitted /clear, while the composer lock is held."""
    if not getattr(_COMPOSER_LOCK_HELD, "auto_clear", False):
        return False
    screen = _tui_capture_pane()
    compose = _tui_compose_line(screen)
    if (not re.fullmatch(r"/clear(?:\s{2,}Start a new chat.*)?", compose)
            or _TUI_JOB_ACTIVE.is_set() or not JOBS.empty()
            or _context_guard_inflight_active()
            or _tui_pane_shows_approval_overlay(screen)):
        return False
    _tmux("send-keys", "-t", TMUX_PANE, "Escape")
    time.sleep(TUI_SUBMIT_DELAY)
    screen = _tui_capture_pane()
    if _tui_compose_line(screen) != "/clear":
        return False
    _tui_erase_compose(len("/clear"))
    print(f"{LOG_KEY} cancelled unsubmitted automatic /clear", flush=True)
    return True


def handle_tui_reset(source="telegram"):
    """TUI /clear|/new — 브릿지 대기·회수·확인버튼을 먼저 비우고 Cursor 에 /clear 를 넣는다.

    슬래시 팔레트가 뜨면 Esc 로 접지 않고 TUI `/clear`(Start a new chat) 를 Enter 로 확정.
    source=context-guard (T-260906-011) 는 awaiting-human 을 세우지 않는다.
    세우면 이후 오케 directive 가 「/clear 이후 기계 주입 폐기」로 버려진다.
    기존 awaiting-human 파일도 건드리지 않는다 (clear_awaiting_human 호출 금지).
    context-guard 는 queue/inflight/harvest 를 지우기 전에 락 안 재검사로 외부 턴을 보존한다
    (T-260907-019). 사용자 명시 /clear|/new 는 기존처럼 대기잡을 비운다.
    """
    if source == "context-guard":
        if not _TUI_ADMISSION_LOCK.acquire(blocking=False):
            return False
        try:
            with composer_lock(blocking=False):
                return _handle_tui_reset_locked(source)
        except (OSError, ValueError, UnicodeError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"{LOG_KEY} context-guard skipped: {type(exc).__name__}", file=sys.stderr)
            return False
        finally:
            _TUI_ADMISSION_LOCK.release()
    with _TUI_ADMISSION_LOCK, composer_lock():
        return _handle_tui_reset_locked(source)


def _handle_tui_reset_locked(source):
    global _HARVEST_GEN
    auto = source == "context-guard"
    if auto and _context_guard_ready_to_clear() is None:
        print(f"{LOG_KEY} context-guard 재검사 — busy/불명이라 /clear 안 함", file=sys.stderr)
        return False
    if auto:
        # Admission and composer locks keep bridge/fleet writers outside this
        # window. Never set the cancellation flag or discard queued work.
        _COMPOSER_LOCK_HELD.auto_clear = True
        try:
            _tui_paste("/clear", interrupt=False)
        except Exception:
            _context_guard_discard_own_clear()
            raise
        finally:
            _COMPOSER_LOCK_HELD.auto_clear = False
        if not _TUI_JOB_ACTIVE.is_set() and JOBS.empty() and not _context_guard_inflight_active():
            reset_cursor_harvest_state()
            forget_suggested_replies()
        return True
    _TUI_RESET.set()
    _HARVEST_GEN += 1
    dropped = drain_pending_jobs()
    forget_suggested_replies()
    if not auto:
        set_awaiting_human()
    reset_cursor_harvest_state()
    _tui_inflight_clear()
    if dropped:
        print(f"{LOG_KEY} /clear 대기잡 {dropped}건 폐기", file=sys.stderr)
    if tui_session_alive():
        try:
            _tui_paste("/clear", interrupt=False)
        except Exception as exc:  # noqa: BLE001
            deliver_mesh_event("error", f"커서 /clear 주입 실패: {exc}")
            _TUI_RESET.clear()
            return False
    else:
        deliver_mesh_event("error", tui_dead_message())
        _TUI_RESET.clear()
        return False
    if not auto:
        notify_tui_cleared()
    _TUI_RESET.clear()
    return True


def maybe_auto_clear_idle_context(now=None, screen=None):
    """Idle + 상태줄 used% ≥ CUB_CONTEXT_CLEAR_PCT 이면 /clear. /context 주입 금지.

    판정은 idle 에서만: 잡·턴 busy·대기 큐·compose 입력·approval/model picker 없음.
    성공 시에만 쿨다운을 열고 report 1줄을 보낸다. T-260906-011.
    T-260907-019: 자동 clear 는 새 capture·TUI 증거·활성 일기장 종료 증거·composer lock
    재검사를 통과한 뒤에만 수행. 불명이면 세션을 보존한다.
    """
    global _CONTEXT_GUARD_LAST_AT
    if CUB_CONTEXT_CLEAR_PCT <= 0:
        return False
    now = time.time() if now is None else float(now)
    if _CONTEXT_GUARD_LAST_AT and (now - _CONTEXT_GUARD_LAST_AT) < CUB_CONTEXT_CLEAR_COOLDOWN_SEC:
        return False
    if _TUI_JOB_ACTIVE.is_set() or _TUI_TURN_BUSY.is_set():
        return False
    if not JOBS.empty():
        return False
    if screen is None:
        try:
            screen = _tui_capture_pane()
        except (OSError, subprocess.SubprocessError):
            return False
    if not _tui_compose_shows_idle_placeholder(screen):
        return False
    if _tui_pane_shows_approval_overlay(screen) or _tui_pane_shows_model_picker(screen):
        return False
    if _tui_pane_shows_guard_busy(screen):
        return False
    raw = _context_guard_footer_percent(screen)
    if raw is None:
        return False
    try:
        used = float(raw)
    except (TypeError, ValueError):
        return False
    if used < CUB_CONTEXT_CLEAR_PCT:
        return False
    if not tui_session_alive():
        return False
    # A failed automatic attempt also gets a cooldown; never keep re-pasting it.
    _CONTEXT_GUARD_LAST_AT = now
    if not handle_tui_reset(source="context-guard"):
        return False
    deliver_mesh_event(
        "report",
        f"컨텍스트 {raw}% — 자동 /clear 했어. 이 브릿지는 작업을 자동 재개하지 않아. "
        "필요하면 작업 체크포인트에서 이어서 진행해.",
    )
    print(f"{LOG_KEY} context-guard {raw}% → /clear", file=sys.stderr)
    return True


def with_suggested_tail_instruction(text):
    """TUI/헤드리스 프롬프트에는 추천답 꼬리를 넣지 않는다.

    그록 paste 는 원문만 넣는다. cub 이 꼬리를 붙이면 모델이 본문에
    <추천답변>·Considering·AskQuestion 을 적는다 (intern 2026-09-02).
    """
    payload = (text or "").rstrip()
    return payload  # no suggested-reply tail in prompt


def strip_suggested_tail_instruction(text):
    """미러·일기장 조회용. 붙여 넣은 꼬리 지시는 질문 본문이 아니다."""
    raw = (text or "").rstrip()
    marker = SUGGESTED_TAIL_INSTRUCTION
    if raw.endswith(marker):
        return raw[: -len(marker)].rstrip()
    return raw


def _tui_paste(prompt, *, interrupt=False):
    with composer_lock():
        _tui_paste_unlocked(prompt, interrupt=interrupt)


def _tui_paste_unlocked(prompt, *, interrupt=False):
    original = (prompt or "").rstrip("\n")
    payload = with_suggested_tail_instruction(original)
    payload = payload.rstrip("\n")
    if not payload:
        return
    # 그록 현행과 같이 본문은 send-keys 로 치지 않는다. C-c 는 Cursor TUI
    # 턴을 죽이므로 쓰지 않는다. Space 는 스크롤백/follow-up 목록에서
    # 입력칸으로 초점을 옮긴다 — 없으면 Enter 가 steer 로 먹혀 compose 에
    # 글만 남는다 (ENTER-TRACE-1537).
    del interrupt
    path = newest_transcript_path()
    screen = _tui_capture_pane()
    if getattr(_COMPOSER_LOCK_HELD, "auto_clear", False):
        if _context_guard_ready_to_clear(screen=screen) is None:
            raise RuntimeError("context-guard: state changed before paste")
    if _tui_pane_shows_approval_overlay(screen):
        raise RuntimeError("TUI 승인 대기 중 — 키 주입 금지")
    if _tui_is_native_slash_prompt(payload):
        # Hermes live: Space+/clear 는 /fleet-clear·/session-clear 스킬 팔레트.
        print(f"{LOG_KEY} TUI 슬래시 — Space 생략", file=sys.stderr)
    elif not _tui_layout_skip_keys(screen):
        _tmux("send-keys", "-t", TMUX_PANE, "Space")
        time.sleep(TUI_SUBMIT_DELAY)
    else:
        print(f"{LOG_KEY} Waiting 크롬 — Space 생략", file=sys.stderr)
    _tmux("load-buffer", "-", input_text=payload)
    if getattr(_COMPOSER_LOCK_HELD, "auto_clear", False):
        if _context_guard_ready_to_clear() is None:
            raise RuntimeError("context-guard: state changed before paste-buffer")
    _tmux("paste-buffer", "-p", "-t", TMUX_PANE)
    time.sleep(TUI_SUBMIT_DELAY)
    _tui_send_submit_key()
    _tui_confirm_submit(original, path)


def _all_transcript_paths(root=None):
    base = Path(root or TRANSCRIPT_ROOT)
    if not base.is_dir():
        return []
    out = []
    for path in base.glob("*/agent-transcripts/*/*.jsonl"):
        try:
            out.append((path.stat().st_mtime, path))
        except OSError:
            continue
    out.sort(key=lambda item: item[0])
    return [p for _, p in out]


def load_headless_transcripts():
    rec = _tui_json_load(HEADLESS_TRANSCRIPTS_FILE)
    paths = rec.get("paths") if isinstance(rec, dict) else None
    return {str(p) for p in (paths or []) if p}


def remember_headless_transcripts(new_paths):
    """헤드리스 `cursor-agent -p` 가 만든 일기장은 TUI 일기장 후보에서 영구 제외한다.

    mtime 최신순 선택이 헤드리스 일기장을 집으면 TUI 는 영영 '침묵'으로 보이고, 그 일기장의
    옛 답이 폰으로 재발송된다 (9/5 macOS 노드).
    """
    fresh = {str(p) for p in new_paths if p}
    if not fresh:
        return
    known = load_headless_transcripts()
    known |= fresh
    _tui_json_save(HEADLESS_TRANSCRIPTS_FILE, {"paths": sorted(known)[-200:]})


def list_transcript_paths(root=None):
    skip = load_headless_transcripts()
    return [p for p in _all_transcript_paths(root) if str(p) not in skip]


_PANE_OWNED_CACHE = {"at": 0.0, "pid": None, "ids": None}
_CHAT_STORE_RE = re.compile(r"/\.cursor/chats/[^/]+/([^/]+)/store\.db")
_DEFAULT_TRANSCRIPT_ROOT = os.path.join(HOME, ".cursor", "projects")


def _transcript_session_id(path):
    return Path(path).parent.name if path else ""


def _tmux_pane_pid():
    proc = _tmux("display-message", "-p", "-t", TMUX_PANE, "#{pane_pid}")
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw.isdigit():
        return None
    return int(raw)


def _direct_child_pids(pid):
    """tmux pane PID의 직계 자식만. Cursor agent가 store.db를 연다."""
    if not pid:
        return []
    try:
        proc = subprocess.run(
            ["pgrep", "-P", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in (proc.stdout or "").split() if x.strip().isdigit()]


def _lsof_chat_ids_for_pid(pid):
    """한 PID의 open store.db. 실패하면 None(불명)."""
    if not pid:
        return None
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(int(pid)), "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    ids = set()
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("n"):
            continue
        match = _CHAT_STORE_RE.search(line)
        if match:
            ids.add(match.group(1))
    return ids


def _pane_owned_chat_ids():
    """명시 소유. 불명이면 None. 빈 set은 '이 pane이 연 chat store가 없음'.

    pane PID lsof만 보면 Cursor agent 자식이 연 store를 놓쳐 로컬미러가 0이 된다.
    """
    override = os.environ.get("CUB_OWNED_CHAT_IDS")
    if override is not None:
        if not override.strip():
            return None
        if override.strip() in {"-", "."}:
            return set()
        return {part.strip() for part in override.split(",") if part.strip()}
    root = Path(os.environ.get("CUB_TRANSCRIPT_ROOT") or TRANSCRIPT_ROOT)
    try:
        if root.resolve() != Path(_DEFAULT_TRANSCRIPT_ROOT).resolve():
            return None
    except OSError:
        return None
    now = time.time()
    pid = _tmux_pane_pid()
    cache = _PANE_OWNED_CACHE
    if (
        cache["pid"] == pid
        and cache["ids"] is not None
        and (now - float(cache["at"] or 0)) < TUI_MIRROR_LOCAL_INTERVAL
    ):
        return cache["ids"]
    ids = _lsof_chat_ids_for_pid(pid)
    if ids is not None and not ids:
        child_ids = set()
        for cpid in _direct_child_pids(pid):
            more = _lsof_chat_ids_for_pid(cpid)
            if more:
                child_ids |= more
        if child_ids:
            ids = child_ids
    _PANE_OWNED_CACHE.update({"at": now, "pid": pid, "ids": ids})
    return ids


def _transcript_birth_ts(path):
    try:
        st = Path(path).stat()
    except OSError:
        return 0.0
    return float(getattr(st, "st_birthtime", st.st_ctime) or 0.0)


def _process_started_at():
    try:
        return float(_HEALTH.get("started_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _handled_entry(rec, path):
    hp = rec.get("handled_paths") if isinstance(rec, dict) else None
    if not isinstance(hp, dict):
        return None
    entry = hp.get(str(path))
    return entry if isinstance(entry, dict) else None


def _transcript_is_runtime_new_session(path, rec):
    """이 프로세스 시작 이후에 생긴 경로의 최초 처리. 재시작 전 파일·이미 처리한 경로는 제외."""
    if not path or not rec:
        return False
    # A new file can contain imported history; require ownership by the live pane.
    if _transcript_session_id(path) not in (_pane_owned_chat_ids() or set()):
        return False
    if _handled_entry(rec, path):
        return False
    if rec.get("finals_sent") is None:
        return False
    prev = str(rec.get("path") or "")
    if not prev or prev == str(path):
        return False
    started = _process_started_at()
    birth = _transcript_birth_ts(path)
    if started and birth <= started:
        return False
    return True


def _owned_transcript_paths(ids, root=None):
    """owned chat id만 glob. 전역 jsonl을 열어 usable을 만들지 않는다."""
    base = Path(root or TRANSCRIPT_ROOT)
    if not base.is_dir() or not ids:
        return []
    skip = load_headless_transcripts()
    out = []
    for sid in ids:
        if not sid or not re.fullmatch(r"[\w.-]+", sid):
            continue
        for path in base.glob(f"*/agent-transcripts/{sid}/*.jsonl"):
            if str(path) in skip:
                continue
            try:
                out.append((path.stat().st_mtime, path))
            except OSError:
                continue
    out.sort(key=lambda item: item[0])
    return [path for _, path in out]


def newest_transcript_path(root=None):
    """대상 pane이 연 chat store로 후보를 제한한다. 낡은 bound에 묶이면 터미널 답이 유실된다.

    owned is None: 불명 — bound만 읽고 전역 최신 mtime으로 다른 세션을 고르지 않는다.
    owned == set(): 이 pane이 연 chat 없음 — 남의 bound를 반환하지 않는다.
    """
    rec = load_cursor_state()
    bound_s = str(rec.get("path") or "")
    bound = Path(bound_s) if bound_s else None
    skip = load_headless_transcripts()
    owned = _pane_owned_chat_ids()
    if owned is not None:
        if not owned:
            return None
        candidates = _owned_transcript_paths(owned, root)
        usable = [
            path
            for path in candidates
            if not transcript_is_aborted_only(read_transcript_rows(path))
        ]
        if usable:
            return usable[-1]
        return None
    if bound and bound.is_file() and str(bound) not in skip:
        if not transcript_is_aborted_only(read_transcript_rows(bound)):
            return bound
        return None
    paths = list_transcript_paths(root)
    if len(paths) == 1 and str(paths[0]) not in skip:
        if not transcript_is_aborted_only(read_transcript_rows(paths[0])):
            return paths[0]
    return None


def read_transcript_rows(path):
    if not path:
        return []
    rows = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _content_texts(row):
    msg = row.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [content]
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    texts.append(t)
            elif isinstance(block, str) and block:
                texts.append(block)
    return texts


def row_has_tool_use(row):
    msg = row.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)
    return False


def cursor_flow_enabled() -> bool:
    configured = os.environ.get(CUB_FLOW_MIRROR_ENV)
    if configured is not None and configured.strip():
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return os.path.exists(CUB_FLOW_MIRROR_FLAG)


def cursor_flow_detail_enabled() -> bool:
    return _flow_progress.detail_enabled(CUB_FLOW_MIRROR_DETAIL_ENV, CUB_FLOW_MIRROR_DETAIL_FLAG)


def _tool_block_stage_detail(block) -> str:
    inp = (block or {}).get("input") or (block or {}).get("arguments") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            return ""
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "cmd", "shell", "script"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def last_tool_flow_line(rows) -> str:
    for row in reversed(rows or []):
        msg = row.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                detail = _tool_block_stage_detail(block)
                if cursor_flow_detail_enabled():
                    return name
                return _flow_progress.classify_flow_stage(name=name, detail=detail)
    return _flow_progress.FLOW_STAGE_WORK if rows else ""


def _cursor_flow_close(status: str, started: float, last_stage: str) -> None:
    if not cursor_flow_enabled() or not last_stage:
        return
    label = _flow_progress.flow_done_label(status)
    elapsed = _flow_progress.format_flow_elapsed(time.time() - started)
    deliver_mesh_event("report", f"{label} · 소요 {elapsed}")


def assistant_texts(rows):
    out = []
    for row in rows:
        if row.get("role") != "assistant":
            continue
        joined = "\n".join(_content_texts(row)).strip()
        if joined:
            out.append(joined)
    return out


def user_query_text(row):
    """Cursor 가 감싼 <user_query> 본문. 없으면 평문."""
    if (row or {}).get("role") != "user":
        return ""
    raw = "\n".join(_content_texts(row)).strip()
    if not raw:
        return ""
    open_at = raw.find("<user_query>")
    close_at = raw.find("</user_query>")
    if open_at >= 0 and close_at > open_at:
        return strip_suggested_tail_instruction(
            raw[open_at + len("<user_query>"):close_at].strip()
        )
    ts_open = raw.find("<timestamp>")
    ts_close = raw.find("</timestamp>")
    if ts_open >= 0 and ts_close > ts_open:
        raw = (raw[:ts_open] + raw[ts_close + len("</timestamp>"):]).strip()
    return strip_suggested_tail_instruction(raw)


def assistant_final_pairs(rows):
    """[(질문, 최종답), ...] — 사용자 턴마다 마지막 텍스트-only 행 하나.

    Cursor transcript 는 한 턴에 순수 텍스트 assistant 행을 여러 개 남길 수 있다.
    각 행을 final 로 세면 재기동 회수 때 ALIVE·추천 칩이 반복 발송된다.
    tool_use 가 뒤따르면 앞 텍스트는 중간보고이므로 후보에서도 제외한다.
    """
    last_q = ""
    out = []

    def flush(candidate):
        if candidate:
            out.append((last_q, candidate))

    candidate = ""
    for row in rows or []:
        if row.get("role") == "user":
            flush(candidate)
            candidate = ""
            q = user_query_text(row)
            if q:
                last_q = q
            continue
        if row.get("type") == "turn_ended":
            flush(candidate)
            candidate = ""
            continue
        if row.get("role") != "assistant":
            continue
        if row_has_tool_use(row):
            candidate = ""
            continue
        joined = "\n".join(_content_texts(row)).strip()
        if joined:
            candidate = joined
    flush(candidate)
    return out


def assistant_final_texts(rows):
    """사용자 턴별 최종 assistant 텍스트. tool_use 중간보고는 제외한다."""
    return [answer for _question, answer in assistant_final_pairs(rows)]


def harvest_new_assistant(path, baseline):
    rows = read_transcript_rows(path)
    if len(rows) <= baseline:
        return ""
    texts = assistant_final_texts(rows[baseline:])
    return texts[-1] if texts else ""


def tail_is_tool_only(rows):
    if not rows:
        return False
    last = rows[-1]
    return last.get("role") == "assistant" and not _content_texts(last)


def tail_is_busy(rows):
    """turn_ended 등 role 없는 행은 무시한다. 그걸 busy 로 보면 답이 15분 동안 안 나간다."""
    for last in reversed(rows or []):
        if last.get("type") == "turn_ended":
            continue
        role = last.get("role")
        if role not in {"assistant", "user"}:
            continue
        if role != "assistant":
            return True
        return row_has_tool_use(last)
    return False


def turn_has_ended(rows):
    for row in reversed(rows or []):
        if row.get("type") == "turn_ended":
            return True
        if row.get("role") in {"assistant", "user"}:
            return False
    return False


def load_cursor_state(path=None):
    rec = {}
    try:
        rec = json.loads(_read(CURSOR_FILE) or "{}")
    except json.JSONDecodeError:
        rec = {}
    if path is not None and rec.get("path") != str(path):
        return {"path": str(path), "rows": 0, "last_final": ""}
    return rec


def cursor_file_baseline(path):
    rec = load_cursor_state(path)
    try:
        return int(rec.get("rows") or 0)
    except (TypeError, ValueError):
        return 0


def save_cursor(path, rows, last_final=None, finals_sent=None, delivered_final=None, last_observe_at=None):
    rec = load_cursor_state()
    path_changed = rec.get("path") != str(path)
    rec["path"] = str(path)
    rec["rows"] = int(rows)
    if path_changed and delivered_final is None:
        rec["delivered_final"] = ""
    if last_final is not None:
        rec["last_final"] = last_final
    if finals_sent is not None:
        rec["finals_sent"] = int(finals_sent)
    if delivered_final is not None:
        rec["delivered_final"] = delivered_final
    rec["last_observe_at"] = float(time.time() if last_observe_at is None else last_observe_at)
    if finals_sent is not None:
        hp = rec.get("handled_paths") if isinstance(rec.get("handled_paths"), dict) else {}
        hp[str(path)] = {
            "finals_sent": int(finals_sent),
            "last_final": str(rec.get("last_final") or ""),
        }
        rec["handled_paths"] = hp
    _write(CURSOR_FILE, json.dumps(rec, ensure_ascii=False))


def normalize_origin_prompt(text):
    """user_query_text 와 같은 본문 키 — 태그·꼬리 지시를 벗겨 비교한다."""
    raw = strip_suggested_tail_instruction((text or "").strip())
    if not raw:
        return ""
    fake = {"role": "user", "message": {"content": [{"type": "text", "text": raw}]}}
    return (user_query_text(fake) or raw).strip()


def remember_telegram_origin_prompt(text):
    """source=telegram 인입만 기록. 문자열 추측으로 출처를 섞지 않는다."""
    key = normalize_origin_prompt(text)
    if not key:
        return
    rec = load_cursor_state()
    seen = [str(item) for item in (rec.get("telegram_prompts") or []) if str(item).strip()]
    if key in seen:
        return
    seen.append(key)
    rec["telegram_prompts"] = seen[-TELEGRAM_ORIGIN_PROMPTS_MAX:]
    _write(CURSOR_FILE, json.dumps(rec, ensure_ascii=False))


def is_telegram_origin_prompt(question):
    key = normalize_origin_prompt(question)
    if not key:
        return False
    rec = load_cursor_state()
    seen = {str(item) for item in (rec.get("telegram_prompts") or []) if str(item).strip()}
    return key in seen


def is_dispatch_prompt(question):
    """오케/함대 배차 본문인가. 문자열 추측이 아니라 운반체 표식."""
    raw = question or ""
    return bool(
        _DISPATCH_CARRIER_RE.search(raw)
        or _DISPATCH_HEAD_RE.search(raw)
        or _DISPATCH_ROUTE_RE.search(raw)
        or _DISPATCH_FLEET_RE.search(raw)
    )


def bind_cursor_if_needed(path, finals_upto_rows=None):
    if not path:
        return
    rec = load_cursor_state()
    if rec.get("path") == str(path) and rec.get("finals_sent") is not None:
        return
    rows = read_transcript_rows(path)
    sliced = rows[: int(finals_upto_rows)] if finals_upto_rows is not None else rows
    save_cursor(path, len(rows), last_final="", finals_sent=len(assistant_final_texts(sliced)))


def harvest_orphaned_finals(path):
    """그록 harvest_orphaned_tui_finals 동형. 배달은 finals_sent 커서로만 한다.

    경로가 처음이면 현재 개수로만 초기화한다 — 과거 일기장을 폰에 쏟지 않는다.
    새 텔레그램이 앞 잡을 취소해도, 이미 끝난 텍스트-only 답은 여기서 건진다.
    """
    with _HARVEST_LOCK:
        return _harvest_orphaned_finals_locked(path)


def _harvest_orphaned_finals_locked(path):
    if not path:
        return 0
    rows = read_transcript_rows(path)
    texts = assistant_final_texts(rows)
    rec = load_cursor_state()
    path_s = str(path)
    bound_path = str(rec.get("path") or "")
    if bound_path != path_s:
        # 일기장이 바뀌었다(첫 바인딩이든, 다른 일기장에서 옮겨 왔든). 옮겨 온 일기장의
        # 과거 답을 통째로 쏟으면 안 된다 — inflight 잡이 이 일기장을 가리키면 그 baseline
        # 뒤 답만, 아니면 현재 개수로 커서만 맞춘다. (9/5 macOS 노드: 헤드리스 일기장→TUI 일기장
        # 전환에 옛 답 10건이 폰으로 재발송된 사고.)
        job = _tui_inflight_load()
        if job and str(job.get("path") or "") == path_s:
            try:
                inflight_baseline = int(job.get("baseline") or 0)
            except (TypeError, ValueError):
                inflight_baseline = 0
            prior_texts = assistant_final_texts(rows[: max(inflight_baseline, 0)])
            save_cursor(
                path,
                len(rows),
                last_final=str(rec.get("last_final") or ""),
                finals_sent=len(prior_texts),
            )
            rec = load_cursor_state(path)
            try:
                already = int(rec.get("finals_sent") or 0)
            except (TypeError, ValueError):
                already = 0
            if already > len(texts):
                already = len(texts)
            last = str(rec.get("last_final") or "")
        elif bound_path and rec.get("finals_sent") is not None and transcript_is_aborted_only(read_transcript_rows(bound_path)):
            # /clear 가 남긴 aborted-only 핀 → 같은 세션의 새 일기장. 새 일기장은 새 답만 있다.
            already = 0
            last = ""
        elif _transcript_is_runtime_new_session(path, rec):
            # 답 개수는 과거/신규 증거가 아니다. 라벨 있는 전달은 local mirror가 한다.
            save_cursor(path, len(rows), last_final="", finals_sent=0)
            if TUI_MIRROR_LOCAL:
                return 0
            already = 0
            last = ""
        elif _handled_entry(rec, path):
            entry = _handled_entry(rec, path)
            try:
                already = int(entry.get("finals_sent") or 0)
            except (TypeError, ValueError):
                already = 0
            last = str(entry.get("last_final") or "")
        else:
            save_cursor(path, len(rows), last_final=str(rec.get("last_final") or ""), finals_sent=len(texts))
            return 0
    elif rec.get("finals_sent") is None:
        save_cursor(path, len(rows), last_final=str(rec.get("last_final") or ""), finals_sent=len(texts))
        return 0
    else:
        try:
            already = int(rec.get("finals_sent") or 0)
        except (TypeError, ValueError):
            already = 0
        if already > len(texts):
            already = len(texts)
        last = str(rec.get("last_final") or "")
    sent = 0
    for text in texts[already:]:
        if not text or text == last:
            continue
        deliver_cursor_answer(text)
        last = text
        sent += 1
    save_cursor(path, len(rows), last_final=last, finals_sent=len(texts))
    if sent:
        print(f"{LOG_KEY} orphaned final harvest {sent}건", flush=True)
    return sent


def _is_reasoning_prose_para(para):
    """코드 펜스·표·목록이 아닌, 한글 0 인 영문 산문 단락."""
    s = para.strip()
    if not s or "```" in s:
        return False
    if _HANGUL_RE.search(s):
        return False
    first = s.lstrip()
    if first.startswith(("|", "- ", "* ", "#", ">", "[")) or re.match(r"^\d+[.)]\s", first):
        return False
    return sum(ch.isalpha() for ch in s) >= 40


def strip_trailing_reasoning(text):
    """한글 답 뒤에 이어 붙은 영문 생각 요약 덩어리를 걷는다.

    조건 전부: (1) 꼬리 단락들이 한글 0 영문 산문, (2) 그 앞 본문에 한글이 있다,
    (3) 덩어리 첫 단락이 1인칭 리즈닝 어투("I'm …", "I should …", "The user …")로 시작.
    영어 대화(본문에 한글 없음)엔 손대지 않는다 — 공개 배포 사용자 무영향.
    """
    if not STRIP_TRAILING_REASONING:
        return text
    raw = str(text or "")
    if "\n\n" not in raw:
        return text
    paras = raw.split("\n\n")
    cut = len(paras)
    while cut > 0 and _is_reasoning_prose_para(paras[cut - 1]):
        cut -= 1
    if cut == len(paras) or cut == 0:
        return text
    head = "\n\n".join(paras[:cut])
    if not _HANGUL_RE.search(head):
        return text
    tail_block = "\n\n".join(paras[cut:])
    if not (
        _REASONING_OPENER_RE.match(paras[cut].strip())
        or _REASONING_FIRST_PERSON_RE.search(tail_block)
    ):
        return text
    dropped = len(paras) - cut
    print(f"{LOG_KEY} 꼬리 생각요약 {dropped}단락 걷음", flush=True)
    return head.rstrip()


def phone_facing_answer(text, max_lines=None):
    """텔레그램 사람용 답. 세션/TUI 원문·마커·도구 덤프를 걷고 모델 발화만 남긴다.

    재현: Cursor 브릿지만 [REDACTED]·[sol-cycle]·CONFLICTING/DIRTY·표/코드
    펜스를 폰에 흘렸다. 그록 브릿지는 모델 발화만 보낸다. 3노드 cub 공용.
    줄 수는 기본 무제한(max_lines/CUB_PHONE_MAX_LINES 0). 단락 사이 빈 줄 하나는
    유지해 긴 보고의 구조가 폰에서도 읽히게 한다.
    """
    limit = PHONE_FACING_MAX_LINES if max_lines is None else max(0, int(max_lines))
    raw = strip_memory_citation(str(text or ""))
    if not raw.strip():
        return ""
    cleaned = _PHONE_MARKER_RE.sub("", raw)
    kept = []
    in_fence = False
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _PHONE_DUMP_LINE_RE.search(s):
            continue
        s = _PHONE_AGENT_META_PREFIX_RE.sub("", s).strip()
        if not s:
            continue
        if _is_phone_agent_meta_line(s):
            continue
        if SUGGESTED_OPEN in s or SUGGESTED_CLOSE in s:
            continue
        if s.startswith("|") and s.count("|") >= 2:
            continue
        kept.append(s)
        if limit and len([k for k in kept if k]) >= limit:
            break
    return "\n".join(kept).strip()


def _is_phone_agent_meta_line(s):
    return bool(_PHONE_AGENT_META_LINE_RE.search(s or ""))


def _mirror_prompt_body(question):
    # T-260910-012: 사람 터미널은 기존 라벨, 배차는 보낸 지시. 터미널 라벨로 배차를
    # 에코하지 않는다(T-260910-006). 시크릿은 가린다. 도구 원로그는 여기 안 탄다.
    if is_dispatch_prompt(question):
        return _turn_mirror.format_sent_directive(question or "")
    return _turn_mirror.format_terminal_query(question or "")


def mirror_local_tui_turns():
    """tmux 창에 직접 친 턴을 폰으로 올린다. 그록 GRB_TUI_MIRROR_LOCAL 동형.

    켠 순간 과거 일기장은 안 쏟는다. 텔레그램 잡이 도는 동안엔 비킨다.
    생각 중(tool_use)이면 답이 끝날 때까지 기다린다.
    """
    if not TUI_MIRROR_LOCAL:
        return 0
    if _TUI_JOB_ACTIVE.is_set():
        return 0
    path = newest_transcript_path()
    if not path:
        _TUI_TURN_BUSY.clear()
        return 0
    with _HARVEST_LOCK:
        if _TUI_JOB_ACTIVE.is_set():
            return 0
        job = _tui_inflight_load()
        if job and job.get("text"):
            path, _, rebound = _maybe_rebind_active_transcript(
                path, int(job.get("baseline") or 0),
                job.get("prior_path") or job.get("path"), job["text"],
            )
            if rebound:
                print(f"{LOG_KEY} pending reply rebound to {path}", flush=True)
        rows = read_transcript_rows(path)
        if tail_is_busy(rows):
            _TUI_TURN_BUSY.set()
            return 0
        _TUI_TURN_BUSY.clear()
        pairs = assistant_final_pairs(rows)
        rec = load_cursor_state()
        runtime_new = _transcript_is_runtime_new_session(path, rec)
        handled = _handled_entry(rec, path)
        if rec.get("path") == str(path) and rec.get("finals_sent") is not None:
            try:
                already = int(rec.get("finals_sent") or 0)
            except (TypeError, ValueError):
                already = 0
            last = str(rec.get("last_final") or "")
        elif runtime_new:
            already = 0
            last = ""
        elif handled:
            try:
                already = int(handled.get("finals_sent") or 0)
            except (TypeError, ValueError):
                already = 0
            last = str(handled.get("last_final") or "")
        else:
            save_cursor(path, len(rows), last_final=str(rec.get("last_final") or ""), finals_sent=len(pairs))
            return 0
        if already > len(pairs):
            already = len(pairs)
        sent = 0
        for pair_index, (question, answer) in enumerate(pairs[already:], start=already):
            if not answer or answer == last:
                continue
            if is_telegram_origin_prompt(question):
                # 텔레그램 질문의 답은 라벨 없이 간다. 브릿지 잡이 이미 보냈으면
                # (delivered_final 영수증) 중복 생략, 잡이 실패했거나 follow-up 큐로
                # 밀려 TUI 가 나중에 답했으면 여기서 배달한다 (2026-09-04 08:2x 유실).
                if answer == str(rec.get("delivered_final") or ""):
                    last = answer
                    continue
                if not _mesh_delivery_sent(deliver_cursor_answer(answer)):
                    return sent
                rec["delivered_final"] = answer
                save_cursor(path, len(rows), delivered_final=answer,
                            last_final=answer, finals_sent=pair_index + 1)
                last = answer
                sent += 1
                continue
            prompt = _mirror_prompt_body(question)
            if prompt:
                deliver_mesh_event("report", prompt)
            if not _mesh_delivery_sent(deliver_cursor_answer(answer)):
                return sent
            save_cursor(path, len(rows), delivered_final=answer,
                        last_final=answer, finals_sent=pair_index + 1)
            last = answer
            sent += 1
        save_cursor(path, len(rows), last_final=last, finals_sent=len(pairs))
        if sent:
            print(f"{LOG_KEY} local mirror {sent}건", flush=True)
        return sent


def tui_local_mirror_ticker():
    while True:
        time.sleep(TUI_MIRROR_LOCAL_INTERVAL)
        try:
            # 틱마다 pane 캡처 1회 — idle 가드와 공유. /context 주입 금지 (T-260906-011).
            screen = _tui_capture_pane()
            sent = mirror_local_tui_turns()
            if sent:
                print(f"{LOG_KEY} local mirror tick {sent}건", file=sys.stderr)
            maybe_auto_clear_idle_context(screen=screen)
        except Exception as exc:  # noqa: BLE001
            print(f"{LOG_KEY} local mirror 실패: {exc}", file=sys.stderr)


def _maybe_rebind_active_transcript(path, baseline, prior_path, pasted_text):
    """paste resolve 창(기본 3s) 이후 늦게 생긴 live jsonl 로 wait loop 가 회귀."""
    token = (pasted_text or "").strip()
    if not token:
        return path, baseline, False
    active = _pick_active_transcript_path(prior_path, pasted_text)
    if not active or str(active) == str(path):
        return path, baseline, False
    if not _transcript_has_pasted_user(active, token):
        return path, baseline, False
    active_rows = read_transcript_rows(active)
    rows_at_bind = len(active_rows)
    if _transcript_mtime_fresh_for_job(active):
        new_baseline = next((
            index for index in range(len(active_rows) - 1, -1, -1)
            if active_rows[index].get("role") == "user"
            and token in (user_query_text(active_rows[index]) or "\n".join(_content_texts(active_rows[index])).strip())
        ), None)
        if new_baseline is None:  # transcript changed between the matching reads
            return path, baseline, False
        bind_cursor_if_needed(active, new_baseline)
    else:
        bind_cursor_if_needed(active, rows_at_bind)
        new_baseline = rows_at_bind
    job = _tui_inflight_load()
    if job:
        started = job.get("started_at")
        job["path"] = str(active)
        job["baseline"] = new_baseline
        _tui_inflight_write(
            _inflight_payload(
                active,
                new_baseline,
                job.get("source"),
                job.get("text") or token,
                prior_path=prior_path or job.get("prior_path"),
                started_at=started,
            )
        )
    return active, new_baseline, True


def turn_still_running(rows, last_change, now=None, hard_deadline=None):
    """기본 대기(TUI_WAIT_SEC)를 넘겼어도 커서가 아직 도구를 돌리는 중이면 True.

    일기장 꼬리가 busy(도구 호출·사용자 행)이고, 최근 TUI_BUSY_STALL_SEC 안에 한 줄이라도
    자랐으면 살아 있는 턴이다. 하드캡(TUI_WAIT_MAX_SEC)을 넘기면 무조건 False.
    붙여넣기가 제출조차 안 된 경우는 꼬리가 busy 가 아니므로 예전처럼 TUI_WAIT_SEC 에 끝난다.
    """
    now = time.time() if now is None else now
    if hard_deadline is not None and now >= hard_deadline:
        return False
    if not tail_is_busy(rows):
        return False
    return (now - last_change) < TUI_BUSY_STALL_SEC


def wait_for_final(path, baseline, deadline, gen=None, *, pasted_text=None, prior_path=None):
    last_len = baseline
    last_change = time.time()
    last_answer = ""
    last_stage = ""
    started = time.time()
    hard_deadline = deadline - TUI_WAIT_SEC + max(TUI_WAIT_MAX_SEC, TUI_WAIT_SEC)
    extended = False
    while True:
        if _TUI_RESET.is_set():
            _cursor_flow_close("interrupt", started, last_stage)
            return "", path
        if gen is not None and gen != _HARVEST_GEN:
            _cursor_flow_close("cancelled", started, last_stage)
            return "", path
        path, baseline, rebound = _maybe_rebind_active_transcript(
            path, baseline, prior_path, pasted_text
        )
        if rebound:
            last_len = baseline
            last_change = time.time()
            last_answer = ""
            last_stage = ""
        rows = read_transcript_rows(path)
        turn_rows = rows[int(baseline or 0):]
        if len(rows) != last_len:
            last_len = len(rows)
            last_change = time.time()
            last_answer = harvest_new_assistant(path, baseline)
            if cursor_flow_enabled():
                stage = last_tool_flow_line(turn_rows)
                if stage and stage != last_stage:
                    last_stage = stage
                    deliver_mesh_event("report", stage)
        if last_answer and not tail_is_busy(rows):
            if turn_has_ended(rows) or (time.time() - last_change) >= TUI_IDLE_SEC:
                _cursor_flow_close("sent", started, last_stage)
                return last_answer, path
        now = time.time()
        if now >= deadline:
            if not turn_still_running(rows, last_change, now, hard_deadline):
                _cursor_flow_close("timeout", started, last_stage)
                return last_answer, path
            if not extended:
                extended = True
                print(
                    f"{LOG_KEY} 턴 아직 진행 중 — 대기 연장 (하드캡 {TUI_WAIT_MAX_SEC}s)",
                    flush=True,
                )
        time.sleep(TUI_POLL_INTERVAL)


TG_CHUNK = int_env("CUB_TG_CHUNK", 4096, minimum=1)
SEND_SURFACE = "direct"


def _tg_chunks(text, limit):
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind(chr(10), 0, limit)
        if cut <= 0:
            cut = limit
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip(chr(10))
    if remaining:
        yield remaining


def deliver_mesh_event(
    kind, body, *, task_id=None, reply_markup=None, telegram_code_entity=None
):
    """Send one message straight to the Telegram Bot API.

    The maintainer internal build routes delivery through a private message
    bus; this public build has no such bus, so delivery is a direct
    `sendMessage` call. kind/task_id are accepted and ignored here.
    reply_markup is forwarded on the first chunk only, so a confirm button
    can sit on the suggestion bubble.
    """
    payload = str(body or "").strip()
    if not payload:
        return {"deliveries": []}
    if kind == "final":
        global _LAST_FINAL_BODY
        with _LAST_FINAL_LOCK:
            if body and body == _LAST_FINAL_BODY:
                print(f"{LOG_KEY} final skip duplicate len={len(body)}", flush=True)
                return {"result": "skipped_duplicate", "deliveries": []}
            _LAST_FINAL_BODY = body or ""
    if DRY_RUN:
        print(f"{LOG_KEY} dry-run send {kind} len={len(payload)}", flush=True)
        return {"result": "dry_run", "deliveries": []}
    deliveries = []
    first = True
    for chunk in _tg_chunks(payload, TG_CHUNK):
        extra = {}
        if first and reply_markup:
            extra["reply_markup"] = reply_markup
            first = False
        else:
            first = False
        res = tg("sendMessage", chat_id=CHAT_ID, text=chunk, **extra)
        if not res or not res.get("ok"):
            print(f"telegram sendMessage failed: {res}", file=sys.stderr)
            deliveries.append({"surface": SEND_SURFACE, "result": "failed"})
            continue
        deliveries.append(
            {
                "surface": SEND_SURFACE,
                "result": "sent",
                "message_id": (res.get("result") or {}).get("message_id"),
            }
        )
    print(
        f"{LOG_KEY} send {kind} sent={sum(1 for d in deliveries if d.get('result') == 'sent')} "
        f"deliveries={len(deliveries)}",
        flush=True,
    )
    return {"deliveries": deliveries}


def _first_sent_message_id(mesh_result, surface=None):
    if surface is None:
        surface = SUGGESTED_SURFACE
    for delivery in (mesh_result or {}).get("deliveries", []):
        if not isinstance(delivery, dict):
            continue
        if delivery.get("surface") == surface and delivery.get("result") == "sent":
            mid = delivery.get("message_id")
            if mid:
                return mid
    return None


def _read_approval_store():
    try:
        with open(APPROVAL_STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_approval_store(store):
    tmp = APPROVAL_STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, APPROVAL_STORE_FILE)


def clear_approval_store():
    try:
        os.unlink(APPROVAL_STORE_FILE)
    except FileNotFoundError:
        pass


def approval_run_everything_markup(cid):
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {
                        "text": APPROVAL_BUTTON_TEXT,
                        "callback_data": f"{APPROVAL_CALLBACK_PREFIX}:{cid}",
                    }
                ]
            ]
        },
        ensure_ascii=False,
    )


def approval_done_markup():
    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {
                        "text": APPROVAL_DONE_BUTTON_TEXT,
                        "callback_data": APPROVAL_DONE_CALLBACK,
                    }
                ]
            ]
        },
        ensure_ascii=False,
    )


def mark_approval_pressed(chat_id, message_id):
    if not chat_id or not message_id:
        return
    try:
        res = tg(
            "editMessageReplyMarkup",
            timeout=10,
            chat_id=chat_id,
            message_id=int(message_id),
            reply_markup=approval_done_markup(),
        )
        if not res or not res.get("ok"):
            print(f"{LOG_KEY} approval button mark 실패: {res}", file=sys.stderr)
    except (TypeError, ValueError, Exception) as exc:  # noqa: BLE001
        print(f"{LOG_KEY} approval button mark 실패: {exc}", file=sys.stderr)


def surface_tui_approval():
    """활성 Approval 모달 하나당 Telegram 버튼을 정확히 한 번 노출한다."""
    if not APPROVAL_SURFACE_ENABLED or not tui_session_alive():
        return 0
    screen = _tui_capture_pane()
    signature = _tui_approval_signature(screen)
    if not signature:
        clear_approval_store()
        return 0
    current = _read_approval_store()
    if current.get("signature") == signature and current.get("cid"):
        return 0
    cid = uuid.uuid4().hex[:12]
    result = deliver_mesh_event(
        "report",
        "Cursor 승인 화면이 떴어. 누르면 Run Everything을 선택할게.",
        reply_markup=approval_run_everything_markup(cid),
    )
    if not _mesh_delivery_sent(result):
        return 0
    _write_approval_store(
        {
            "cid": cid,
            "signature": signature,
            "message_id": _first_sent_message_id(result),
            "ts": time.time(),
        }
    )
    print(f"{LOG_KEY} approval 버튼 노출", flush=True)
    return 1


def approval_surface_ticker():
    while True:
        try:
            surface_tui_approval()
        except Exception as exc:  # noqa: BLE001
            print(f"{LOG_KEY} approval 감시 실패: {exc}", file=sys.stderr)
        try:
            surface_tui_model_picker()
        except Exception as exc:  # noqa: BLE001
            print(f"{LOG_KEY} model 피커 감시 실패: {exc}", file=sys.stderr)
        time.sleep(APPROVAL_POLL_INTERVAL)


# ── /model — Cursor TUI 모델 피커를 폰에서 고른다 ─────────────────────────────
# 실측 2026-09-04 20:1x (Athena cursor 세션):
#   compose 에 "/model" 을 붓고 Enter → 피커("Available models … Type to filter • Enter to select")
#   피커 안에서는 키를 한 글자씩 넣어야 필터가 먹는다(send-keys -l 한 덩이는 통째로 버려짐).
#   "Filter: fable 5.1" → 매치 1건에 → 가 서고 Enter 로 확정, 피커가 닫히며 상태줄 모델명이 바뀐다.
_MODEL_PICKER_HEAD_RE = re.compile(r"(?im)^\s*(?:available models|models matching\b.*)\s")
_MODEL_PICKER_FOOT_RE = re.compile(r"(?i)(?:type|edit prompt) to filter\s*•\s*enter to select")
_MODEL_STATUS_RE = re.compile(r"^\s*(?P<model>[A-Za-z][\w .+-]*?)\s*·")


# compose 줄: 빈 placeholder 또는 우리가 부은 "/model …" (팔레트가 그 아래 열린다).
_IDLE_COMPOSE_RE = re.compile(
    r"^\s*(?:→|->)\s*(?:add a follow-up|plan,\s*search|/model\b)", re.IGNORECASE
)


def _tui_picker_region(screen):
    """피커는 compose(→ Add a follow-up) 아래에 그려진다. 그 위 스크롤백에 남은
    'Available models' 같은 옛 출력(이 세션이 실험하며 찍은 것 포함)은 보지 않는다."""
    lines = (screen or "").splitlines()
    start = -1
    for idx, line in enumerate(lines):
        if _IDLE_COMPOSE_RE.match(line):
            start = idx
    if start < 0:
        return ""
    return "\n".join(lines[start + 1 :])


def _tui_pane_shows_model_picker(screen):
    text = _tui_picker_region(screen)
    return bool(_MODEL_PICKER_HEAD_RE.search(text) and _MODEL_PICKER_FOOT_RE.search(text))


def _tui_model_picker_rows(screen):
    """피커에 보이는 (선택됨, 모델명) 목록. 열 사이 공백 2칸 이상을 구분자로 본다."""
    rows = []
    lines = _tui_picker_region(screen).splitlines()
    inside = False
    for line in lines:
        if _MODEL_PICKER_HEAD_RE.match(line + "\n"):
            inside = True
            continue
        if not inside:
            continue
        if _MODEL_PICKER_FOOT_RE.search(line) or re.match(r"^\s*\d+-\d+ of \d+", line):
            break
        s = line.strip()
        if not s or s.lower().startswith("filter:") or s.startswith("modify)"):
            continue
        selected = s.startswith("→") or s.startswith("->")
        s = s.lstrip("→->").strip()
        name = re.split(r"\s{2,}", s, maxsplit=1)[0].strip()
        if name and not name.lower().startswith("max mode"):
            rows.append((selected, name))
    return rows


def _tui_current_model(screen=None):
    """상태줄 첫 칸 `Claude Fable 5.1 · 86% · 19 files` 의 모델명. 못 읽으면 ""."""
    text = screen if screen is not None else _tui_capture_pane()
    for line in (text or "").splitlines():
        if "run everything" in line.lower():
            m = _MODEL_STATUS_RE.match(line)
            if m:
                # 좁은 창에서는 `GPT-5.5 272K· 37%` 처럼 컨텍스트 칸이 붙어 온다.
                return re.sub(r"\s+\d+K$", "", m.group("model").strip())
    return ""


def _tui_wait_screen(pred, timeout):
    deadline = time.time() + max(0.2, float(timeout))
    screen = ""
    while time.time() < deadline:
        screen = _tui_capture_pane()
        if pred(screen):
            return True, screen
        time.sleep(0.25)
    return False, screen


def _tui_close_model_picker():
    _tmux("send-keys", "-t", TMUX_PANE, "Escape")
    time.sleep(0.3)


def _tui_erase_compose(chars):
    for _ in range(max(0, int(chars))):
        _tmux("send-keys", "-t", TMUX_PANE, "BSpace")
    time.sleep(MODEL_KEY_DELAY)


def tui_select_model(target):
    """compose 에 "/model <target>" 를 붓고 팔레트 → 줄이 target 이면 Enter. (ok, 메시지).

    피커 안에서 글자를 치는 길은 버렸다: 매치 목록이 줄어들 때마다 재렌더가 키를 삼켜
    0.4초 간격으로도 "fable 5.1" 이 "ble 5.1" 로 들어갔다(2026-09-04 20:3x 실측). 팔레트
    인자형("/model [filter]")은 compose 붓기라 한 덩이로 들어가고 Enter 가 바로 확정한다.
    """
    want = (target or "").strip()
    if not want:
        return False, "모델 이름이 비었어"
    if not tui_session_alive():
        return False, "커서 창이 꺼져 있어"
    screen = _tui_capture_pane()
    if _tui_pane_shows_approval_overlay(screen):
        return False, "승인 화면이 떠 있어 — 먼저 처리해줘"
    if _tui_pane_shows_model_picker(screen):
        # 터미널에서 연 피커가 떠 있으면 닫고 팔레트 인자형으로 간다(피커 타이핑은 키를 삼킨다).
        _tui_close_model_picker()
        screen = _tui_capture_pane()
    if not _tui_compose_is_placeholder(screen):
        return False, "입력칸에 글이 남아 있어 — 지금은 모델을 못 바꿔"
    # 우리가 여는 팔레트는 감시 티커가 다시 폰에 띄우지 않게 먼저 표시한다.
    store = _read_model_store()
    store.update({"picker_open": True, "ts": time.time()})
    _write_model_store(store)
    typed = f"/model {want}"
    _tmux("send-keys", "-t", TMUX_PANE, "-l", typed)

    def palette_ready(s):
        return _tui_pane_shows_model_picker(s) and bool(_tui_model_picker_rows(s))

    ok, screen = _tui_wait_screen(palette_ready, MODEL_PICKER_WAIT)
    rows = _tui_model_picker_rows(screen) if ok else []
    chosen = [n for sel, n in rows if sel]
    if not chosen or want.lower() not in chosen[0].lower():
        _tui_erase_compose(len(typed) + 2)
        if not ok or not rows:
            return False, f"'{want}' 에 맞는 모델이 없어 — 입력칸을 비웠어"
        return False, f"'{want}' 로는 {chosen[0] if chosen else rows[0][1]} 이(가) 잡혀서 넣지 않았어 — 이름을 더 정확히 줘"
    _tmux("send-keys", "-t", TMUX_PANE, "Enter")
    ok, screen = _tui_wait_screen(lambda s: not _tui_pane_shows_model_picker(s), MODEL_PICKER_WAIT)
    if not ok:
        return False, "선택은 넣었는데 창이 안 닫혔어 — 터미널을 봐줘"
    time.sleep(0.5)
    now = _tui_current_model()
    if now and want.lower() in now.lower():
        return True, f"모델 → {now}"
    return True, f"모델 선택 {chosen[0]} 넣었어" + (f" (상태줄: {now})" if now else "")


def _read_model_store():
    try:
        with open(MODEL_STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_model_store(store):
    tmp = f"{MODEL_STORE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, MODEL_STORE_FILE)


def model_menu_markup(current="", page=0):
    """모델 버튼을 2열·페이지로 나눈다. 한 줄 스택은 텔레그램이 아래를 자른다."""
    names = list(MODEL_MENU)
    size = max(1, int(MODEL_PAGE_SIZE))
    cols = max(1, int(MODEL_MENU_COLUMNS))
    pages = max(1, (len(names) + size - 1) // size) if names else 1
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    page = page % pages
    chunk = names[page * size : page * size + size]
    rows = []
    row = []
    for name in chunk:
        mark = "✅ " if current and name.lower() == current.lower() else ""
        row.append({"text": f"{mark}{name}", "callback_data": f"{MODEL_CALLBACK_PREFIX}:{name}"[:64]})
        if len(row) >= cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        rows.append(
            [
                {"text": "◀", "callback_data": f"{MODEL_PAGE_CALLBACK}:{(page - 1) % pages}"},
                {"text": f"{page + 1}/{pages}", "callback_data": f"{MODEL_PAGE_CALLBACK}:{page}"},
                {"text": "▶", "callback_data": f"{MODEL_PAGE_CALLBACK}:{(page + 1) % pages}"},
            ]
        )
    rows.append([{"text": "✖ 닫기", "callback_data": MODEL_CLOSE_CALLBACK}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def is_model_command(text):
    return slash_token(text) == "/model"


def handle_model_command(text):
    """/model → 메뉴, /model <이름> → 바로 적용. clb /model 동형."""
    arg = (text or "").strip()[len("/model") :].strip()
    if arg:
        ok, msg = tui_select_model(arg)
        deliver_mesh_event("report" if ok else "error", msg)
        return
    current = _tui_current_model() if tui_session_alive() else ""
    label = current or "모름"
    result = deliver_mesh_event(
        "report",
        f"현재 커서 모델: {label}\n바꿀 모델을 골라줘 — 누르면 커서 창에서 바로 적용할게.",
        reply_markup=model_menu_markup(current),
    )
    _write_model_store({"menu_message_id": _first_sent_message_id(result), "ts": time.time()})


def surface_tui_model_picker():
    """터미널에서 직접 연 모델 피커를 폰에 한 번 노출한다 (Approval 버튼 동형)."""
    if not MODEL_SURFACE_ENABLED or not tui_session_alive():
        return 0
    screen = _tui_capture_pane()
    store = _read_model_store()
    if not _tui_pane_shows_model_picker(screen):
        if store.get("picker_open"):
            store["picker_open"] = False
            _write_model_store(store)
        return 0
    if store.get("picker_open"):
        return 0
    current = _tui_current_model(screen)
    result = deliver_mesh_event(
        "report",
        "커서 모델 선택창이 떴어. 여기서 골라도 돼.",
        reply_markup=model_menu_markup(current),
    )
    if not _mesh_delivery_sent(result):
        return 0
    store.update({"picker_open": True, "menu_message_id": _first_sent_message_id(result), "ts": time.time()})
    _write_model_store(store)
    print(f"{LOG_KEY} model 피커 노출", flush=True)
    return 1


def handle_model_callback(data, answer, message=None):
    if data == MODEL_CLOSE_CALLBACK:
        if tui_session_alive() and _tui_pane_shows_model_picker(_tui_capture_pane()):
            _tui_close_model_picker()
            answer("선택창 닫았어")
        else:
            answer()
        return
    if data.startswith(f"{MODEL_PAGE_CALLBACK}:"):
        raw = data[len(MODEL_PAGE_CALLBACK) + 1 :]
        try:
            page = int(raw)
        except ValueError:
            answer()
            return
        current = _tui_current_model() if tui_session_alive() else ""
        mid = message.get("message_id") if isinstance(message, dict) else None
        if mid:
            try:
                tg(
                    "editMessageReplyMarkup",
                    timeout=10,
                    chat_id=CHAT_ID,
                    message_id=int(mid),
                    reply_markup=model_menu_markup(current, page=page),
                )
            except (TypeError, ValueError, Exception) as exc:  # noqa: BLE001
                print(f"{LOG_KEY} model page edit 실패: {exc}", file=sys.stderr)
        answer()
        return
    name = data[len(MODEL_CALLBACK_PREFIX) + 1 :].strip()
    allowed = {m.lower() for m in MODEL_MENU}
    if name.lower() not in allowed:
        answer("메뉴에 없는 모델이야")
        return
    ok, msg = tui_select_model(name)
    answer(msg[:180])
    deliver_mesh_event("report" if ok else "error", msg)
    print(f"{LOG_KEY} model 선택 {name}: {msg}", flush=True)


def _read_suggested_store():
    try:
        with open(SUGGESTED_STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_suggested_store(store):
    tmp = SUGGESTED_STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, SUGGESTED_STORE_FILE)


def register_suggested_reply(text):
    """문구를 짧은 id 로 저장한다. Telegram callback_data 는 64바이트 한도.

    빈 문구도 cid 를 준다 — 확인 칩은 유지하되 「이어서 해줘」 기본 주입은 하지 않는다.
    """
    phrase = (text or "").strip()
    store = _read_suggested_store()
    cid = uuid.uuid4().hex[:12]
    store[cid] = {"text": phrase, "ts": time.time()}
    if len(store) > SUGGESTED_STORE_MAX:
        ordered = sorted(store.items(), key=lambda item: float((item[1] or {}).get("ts") or 0))
        for old_id, _ in ordered[: len(store) - SUGGESTED_STORE_MAX]:
            store.pop(old_id, None)
    _write_suggested_store(store)
    return cid


def remember_suggested_message_id(cid, message_id):
    key = str(cid or "")
    if not key or not message_id:
        return
    store = _read_suggested_store()
    rec = store.get(key)
    if not isinstance(rec, dict):
        return
    try:
        rec["message_id"] = int(message_id)
    except (TypeError, ValueError):
        return
    store[key] = rec
    _write_suggested_store(store)


def take_suggested_reply(cid):
    key = str(cid or "")
    if not key:
        return "", None
    store = _read_suggested_store()
    rec = store.pop(key, None)
    if rec is not None:
        _write_suggested_store(store)
    if isinstance(rec, dict):
        mid = rec.get("message_id")
        return str(rec.get("text") or "").strip(), mid
    return str(rec or "").strip(), None


def suggested_confirm_markup(cid, phrase=""):
    """그록과 같이 확인 버튼 1개. phrase 는 콜백 저장용이며 버튼 라벨이 아니다."""
    if not cid:
        return ""
    del phrase  # 표시에 쓰지 않는다. 주입 문구는 register_suggested_reply 가 맡는다.
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": SUGGESTED_BUTTON_TEXT, "callback_data": f"{SUGGESTED_CALLBACK_PREFIX}:{cid}"}]
            ]
        },
        ensure_ascii=False,
    )


def suggested_sent_markup():
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": SUGGESTED_SENT_BUTTON_TEXT, "callback_data": SUGGESTED_DONE_CALLBACK}]
            ]
        },
        ensure_ascii=False,
    )


def mark_suggested_pressed(chat_id, message_id):
    if not chat_id or not message_id:
        return
    try:
        res = tg(
            "editMessageReplyMarkup",
            timeout=10,
            chat_id=chat_id,
            message_id=int(message_id),
            reply_markup=suggested_sent_markup(),
        )
        if not res or not res.get("ok"):
            print(f"{LOG_KEY} suggested button mark 실패: {res}", file=sys.stderr)
    except (TypeError, ValueError, Exception) as exc:  # noqa: BLE001
        print(f"{LOG_KEY} suggested button mark 실패: {exc}", file=sys.stderr)


def handle_telegram_callback(callback):
    """추천답변 칩 또는 현재 Approval의 Run Everything 버튼."""
    cb = callback if isinstance(callback, dict) else {}
    qid = str(cb.get("id") or "")
    message = cb.get("message") if isinstance(cb.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}

    def answer(text=""):
        if not qid:
            return
        params = {"callback_query_id": qid}
        if text:
            params["text"] = text
        tg("answerCallbackQuery", timeout=10, **params)

    if str(chat.get("id")) != str(CHAT_ID):
        answer("이 채팅의 버튼이 아니야")
        return
    data = str(cb.get("data") or "")
    if data.startswith(f"{MODEL_CALLBACK_PREFIX}:"):
        handle_model_callback(data, answer, message)
        return
    approval_prefix = f"{APPROVAL_CALLBACK_PREFIX}:"
    if data.startswith(approval_prefix):
        if data == APPROVAL_DONE_CALLBACK:
            answer()
            return
        current = _read_approval_store()
        if not current or data[len(approval_prefix) :] != str(current.get("cid") or ""):
            answer("만료된 승인 버튼이야")
            return
        if current.get("handled"):
            answer("이미 처리한 승인 버튼이야")
            return
        if not tui_session_alive():
            clear_approval_store()
            answer("커서 창이 꺼져 있어")
            return
        screen = _tui_capture_pane()
        signature = _tui_approval_signature(screen)
        if not signature or signature != str(current.get("signature") or ""):
            clear_approval_store()
            answer("승인 화면이 이미 닫혔거나 바뀌었어")
            return
        proc = _tmux("send-keys", "-t", TMUX_PANE, "BTab")
        if getattr(proc, "returncode", 0) not in (0, None):
            answer("Shift+Tab 주입에 실패했어")
            return
        answer("Run Everything 선택했어")
        mark_approval_pressed(
            CHAT_ID,
            message.get("message_id") or current.get("message_id"),
        )
        current["handled"] = True
        current["handled_at"] = time.time()
        _write_approval_store(current)
        print(f"{LOG_KEY} approval Run Everything 선택", flush=True)
        return
    prefix = f"{SUGGESTED_CALLBACK_PREFIX}:"
    if not data.startswith(prefix):
        answer("알 수 없는 버튼이야")
        return
    if data == SUGGESTED_DONE_CALLBACK:
        answer()
        return
    phrase, stored_mid = take_suggested_reply(data[len(prefix) :])
    mid = message.get("message_id") or stored_mid
    if not phrase:
        answer()
        mark_suggested_pressed(CHAT_ID, mid)
        return
    answer()
    mark_suggested_pressed(CHAT_ID, mid)
    inbox_spool(cb.get("id"), "telegram", phrase)
    print(f">>> [{NAME}] 텔레그램 cursor 추천답변칩: {phrase[:80]}", flush=True)
    handle_message_text(phrase, source="telegram")


def split_suggested_reply(text):
    """Split a trailing suggestion marker into (body, suggestion).

    Only a marker at the very end of the answer is a suggestion.
    """
    raw = text or ""
    stripped = raw.rstrip()
    if not stripped.endswith(SUGGESTED_CLOSE):
        return raw, ""
    open_at = stripped.rfind(SUGGESTED_OPEN)
    close_at = stripped.rfind(SUGGESTED_CLOSE)
    if open_at < 0 or close_at < 0 or open_at > close_at:
        return raw, ""
    if stripped[close_at + len(SUGGESTED_CLOSE):].strip():
        return raw, ""
    reply = stripped[open_at + len(SUGGESTED_OPEN):close_at].strip()
    body = stripped[:open_at].rstrip()
    if not reply or not body:
        return raw, ""
    return body, reply


COPY_SPLIT_LOG_KEY = "cub_copy_split"


def split_copy_content(text):
    """Public build keeps commands in the prose.

    The maintainer build splits copy-paste bubbles with the shared parser
    from the sister bridge. That file is not in this package, so this public
    copy does not split commands.
    """
    return text or "", []


def _copy_bubble_is_dump(code):
    """세션/TUI 내부 덤프(cub_tui_lane·CONFLICTING·[REDACTED]…)만으로 된 펜스는 버블로 안 보낸다."""
    lines = [ln.strip() for ln in str(code or "").splitlines() if ln.strip()]
    if not lines:
        return True
    return all(
        _PHONE_DUMP_LINE_RE.search(ln) or _PHONE_MARKER_RE.search(ln) for ln in lines
    )


def deliver_cursor_answer(text, task_id=None):
    """본문 1통 + (있으면) 명령 복붙 버블 N통 + (있으면) 추천답변 버블 1통.

    발신 순서 = grb mirror_answer 와 같다(R-C8 4항): 남은 산문 → 복붙 버블 →
    추천답변 버블. 명령 버블만 telegram_code_entity=True. <추천답변> 마커가
    없는 답에는 추천 문구를 합성하지 않는다(Claude·Grok 동일).
    """
    text = strip_trailing_reasoning(text)
    body, suggested = (
        split_suggested_reply(text) if SUGGESTED_REPLY_SPLIT else (text, "")
    )
    if not suggested:
        body = text or body
    body, copy_bubbles = split_copy_content(body)
    copy_bubbles = [c for c in copy_bubbles if not _copy_bubble_is_dump(c)]
    body = phone_facing_answer(body)
    if not body and not copy_bubbles:
        return {}
    bubble = {}
    if body:
        bubble = deliver_mesh_event("final", body, task_id=task_id)
    for code in copy_bubbles:
        sent = deliver_mesh_event(
            "copy_content", code, task_id=task_id, telegram_code_entity=True
        )
        bubble = bubble or sent
    if not suggested:
        return bubble
    phrase = suggested.strip()
    cid = register_suggested_reply(phrase)
    bubble = deliver_mesh_event(
        "copy_content",
        phrase,
        task_id=task_id,
        reply_markup=suggested_confirm_markup(cid, phrase),
    )
    remember_suggested_message_id(cid, _first_sent_message_id(bubble))
    return bubble


def run_headless(prompt):
    prompt = with_suggested_tail_instruction(prompt)
    cmd = [
        CURSOR_BIN,
        "-p",
        prompt,
        "--output-format",
        "text",
        "--force",
        "--trust",
        "--workspace",
        HOME,
    ]
    if DRY_RUN:
        return f"[dry-run] cursor-agent {prompt[:40]}"
    before = {str(p) for p in _all_transcript_paths()}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TUI_WAIT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"헤드리스 커서 실행 실패: {exc}"
    finally:
        try:
            remember_headless_transcripts({str(p) for p in _all_transcript_paths()} - before)
        except OSError:
            pass
    out = (proc.stdout or "").strip()
    return out or (proc.stderr or "").strip() or "헤드리스 커서가 빈 답을 냈어."


def maybe_busy_inject_telegram(text, source):
    if not TUI_BUSY_INJECT:
        return False
    if source != "telegram":
        return False
    if not _TUI_JOB_ACTIVE.is_set():
        return False
    path = newest_transcript_path()
    baseline = len(read_transcript_rows(path)) if path else 0
    try:
        _tui_paste(text, interrupt=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} busy 삽입 실패 — 큐로 넘김: {exc}", file=sys.stderr)
        return False
    if path:
        _tui_inflight_write(_inflight_payload(path, baseline, source, text))
    print(f"{LOG_KEY} busy 중 텔레그램 삽입", file=sys.stderr)
    # grb 동형 ack. Cursor 는 턴을 끊지 않고 follow-up 큐에 세우므로 문구만 다르다.
    try:
        deliver_mesh_event("ack", "지금 하던 일 뒤에 줄 세웠어. 이 턴 끝나면 바로 이어서 본다.")
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} busy 삽입 ack 실패: {exc}", file=sys.stderr)
    return {"kind": "injected_harvest", "baseline": baseline, "path": str(path or "")}


def process_job(source, text, meta=None):
    with _TUI_ADMISSION_LOCK:
        if _TUI_RESET.is_set():
            return
        if is_awaiting_human() and source != "telegram":
            print(f"{LOG_KEY} /clear 이후 기계 잡 폐기 source={source}", file=sys.stderr)
            return
        health_mark(last_job_started_at=time.time())
        if source == "telegram":
            remember_telegram_origin_prompt(text)
        _TUI_JOB_ACTIVE.set()
    try:
        injected = isinstance(meta, dict) and meta.get("kind") == "injected_harvest"
        path = Path(meta["path"]) if injected and meta.get("path") else newest_transcript_path()
        baseline = int((meta or {}).get("baseline") or 0) if injected else (
            len(read_transcript_rows(path)) if path else 0
        )
        if path:
            bind_cursor_if_needed(path, baseline if injected else None)
        prior_path = Path(meta["path"]) if injected and meta.get("path") else None
        if not injected:
            if tui_session_alive():
                prior_path = path
                if path:
                    _tui_inflight_write(_inflight_payload(path, baseline, source, text, prior_path=prior_path))
                _tui_paste(text, interrupt=False)
                path = resolve_active_transcript_path(prior_path, text) or newest_transcript_path() or path
                # /clear 뒤 새 일기장으로 갈리면 옛 baseline 을 가져가면
                # wait_for_final 이 이미 적힌 답을 못 본다 (live fire 2026-09-01).
                if path and prior_path and str(path) != str(prior_path):
                    if _transcript_mtime_fresh_for_job(path):
                        baseline = 0
                    else:
                        baseline = len(read_transcript_rows(path))
                if path:
                    bind_cursor_if_needed(path, baseline)
                    _tui_inflight_write(
                        _inflight_payload(path, baseline, source, text, prior_path=prior_path)
                    )
            elif TUI_FALLBACK_HEADLESS:
                answer = run_headless(text)
                deliver_cursor_answer(answer)
                _tui_inflight_clear()
                return
            else:
                deliver_mesh_event("error", tui_dead_message())
                _tui_inflight_clear()
                return
        deadline = time.time() + TUI_WAIT_SEC
        gen = _HARVEST_GEN
        answer, path = (
            wait_for_final(
                path,
                baseline,
                deadline,
                gen=gen,
                pasted_text=text,
                prior_path=prior_path,
            )
            if path
            else ("", path)
        )
        delivered = _deliver_job_outcome(path, answer, gen)
        if delivered:
            _tui_inflight_clear()
            return
        if gen != _HARVEST_GEN:
            return
        if path and tui_session_alive() and tail_is_busy(read_transcript_rows(path)):
            # 커서가 아직 그 턴을 돌리는 중이다(하드캡·정체로 대기만 끝남). 헤드리스로
            # 같은 프롬프트를 다시 돌리면 작업자가 둘이 된다 — 대신 알리고, 답은
            # inflight 회수·local mirror 가 턴이 끝난 뒤 배달한다.
            deliver_mesh_event(
                "report",
                f"커서가 {_elapsed_words(time.time() - (deadline - TUI_WAIT_SEC))}째 아직 작업 중이야. "
                "끝나면 답 이어서 보낼게 (같은 일 두 번 안 시킨다).",
            )
            return
        if TUI_FALLBACK_HEADLESS and not injected:
            answer = run_headless(text)
            if answer:
                if _mesh_delivery_sent(deliver_cursor_answer(answer)):
                    _tui_inflight_clear()
                return
        deliver_mesh_event("error", "커서가 답을 안 남겼어. TUI 창을 한 번 봐줘.")
    finally:
        _TUI_JOB_ACTIVE.clear()
        if JOBS.empty():
            typing_off()
        health_mark(last_job_done_at=time.time(), done=1)


NODE_LABELS = {}


def node_label():
    return NODE_LABELS.get(NODE_KEY, NAME)


def start_ready_message():
    return (
        "Cursor Telegram Bridge is on. Messages you send here go into the "
        "Cursor session on this machine."
    )


def _elapsed_words(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분"
    return f"{seconds // 3600}시간 {(seconds % 3600) // 60}분"


def status_message():
    """/status 한 줄 — clb·crb 와 같은 자리. health 스냅샷을 사람 말로 요약한다."""
    snap = health_snapshot()
    now = time.time()
    tui = "살아 있음" if tui_session_alive() else "없음"
    busy = "일하는 중" if _TUI_JOB_ACTIVE.is_set() else "대기"
    last_done = snap.get("last_job_done_at") or 0
    last = f"{_elapsed_words(now - last_done)} 전" if last_done else "없음"
    return (
        f"커서 {node_label()} 브릿지 {busy} · 가동 {_elapsed_words(now - snap['started_at'])} · "
        f"큐 {snap['queue_depth']} · 처리 {snap['done']}/{snap['enqueued']} · "
        f"마지막 답 {last} · TUI 세션 {tui}"
    )


def is_status_command(text):
    return slash_token(text) == "/status"


def handle_message_text(text, source="telegram", meta=None):
    with _TUI_ADMISSION_LOCK:
        return _handle_message_text_locked(text, source, meta)


def _handle_message_text_locked(text, source="telegram", meta=None):
    global _HARVEST_GEN
    token = (text or "").strip()
    if token.lower() in ("/start", "/ping"):
        # grb 와 같이 ack — 발신 래퍼가 DM 표면에 노드 이모지 영수증으로 그린다. 모델 답(final)이 아니다.
        deliver_mesh_event("ack", start_ready_message())
        return
    if is_status_command(text):
        # 상태 한 줄은 본문이 보여야 하므로 report (grb 진행/박힘 경보와 같은 kind).
        deliver_mesh_event("report", status_message())
        return
    if is_model_command(text):
        handle_model_command(text)
        return
    if slash_token(text) in TUI_RESET_TOKENS:
        handle_tui_reset(source)
        return
    if is_context_command(text):
        handle_context_command(source)
        return
    if is_awaiting_human():
        if source == "telegram" and not slash_token(text):
            clear_awaiting_human()
        else:
            print(f"{LOG_KEY} /clear 이후 기계 주입 폐기 source={source}", file=sys.stderr)
            return
    typing_on()
    _HARVEST_GEN += 1
    if source == "telegram":
        remember_telegram_origin_prompt(text)
    busy = maybe_busy_inject_telegram(text, source)
    if busy:
        JOBS.put((source, text, busy))
        health_mark(last_enqueue_at=time.time(), enqueued=1)
        return
    JOBS.put((source, text, meta))
    health_mark(last_enqueue_at=time.time(), enqueued=1)


def worker_loop():
    while True:
        source, text, meta = JOBS.get()
        try:
            process_job(source, text, meta)
        except Exception as exc:  # noqa: BLE001
            print(f"{LOG_KEY} job 실패: {exc}", file=sys.stderr)
            try:
                deliver_mesh_event("error", f"커서 브릿지 잡 실패: {exc}")
            except Exception:
                pass
        finally:
            JOBS.task_done()


def safe_filename_part(value):
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value or ""))
    return cleaned.strip("-")[:80] or "file"


def suffix_from_metadata(file_name="", mime_type="", default=".bin"):
    suffix = os.path.splitext(file_name or "")[1].lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed.lower() if guessed else default


def format_metadata(metadata):
    parts = []
    for key, value in (metadata or {}).items():
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={value}")
    return "; ".join(parts)


def media_output_dir():
    return env("CUB_MEDIA_DIR") or os.path.join(STATE_DIR, "cursor-telegram-bridge-media", NAME)


def image_prompt_text(caption_text, image_path, metadata):
    lines = [
        "[Telegram image received]",
        f"local_path: {image_path}",
    ]
    if caption_text:
        lines.append(f"caption: {caption_text}")
    metadata_line = format_metadata(metadata)
    if metadata_line:
        lines.append(f"metadata: {metadata_line}")
    lines.extend(
        [
            "",
            "Open the local image path, inspect it, and answer the Telegram user in Korean. "
            "Keep the answer concise and useful.",
        ]
    )
    return "\n".join(lines)


def download_file(file_id, output_dir, name_hint, default_suffix=".bin", allowed_extensions=None):
    """Telegram getFile → 로컬 저장. 그록/클로드 브릿지와 같은 청크·재시도 계약."""
    if DRY_RUN or not TOKEN:
        raise RuntimeError("image download blocked in dry-run")
    payload = tg("getFile", file_id=file_id, timeout=15)
    if not payload or not payload.get("ok") or not isinstance(payload.get("result"), dict):
        raise RuntimeError("Telegram getFile failed")
    file_path = str(payload["result"].get("file_path") or "")
    if not file_path:
        raise RuntimeError("Telegram getFile returned empty file_path")

    suffix = os.path.splitext(file_path)[1].lower() or default_suffix
    if allowed_extensions is not None and suffix not in allowed_extensions:
        suffix = default_suffix
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{safe_filename_part(name_hint)}{suffix}")

    quoted_path = urllib.parse.quote(file_path, safe="/")
    request = urllib.request.Request(f"https://api.telegram.org/file/bot{TOKEN}/{quoted_path}")
    last_err = None
    for attempt in range(3):
        try:
            buf = bytearray()
            with urllib.request.urlopen(request, timeout=DOWNLOAD_ATTEMPT_TIMEOUT) as response:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    buf.extend(chunk)
            with open(output_path, "wb") as fh:
                fh.write(bytes(buf))
            return output_path
        except (OSError, http.client.HTTPException) as err:
            last_err = err
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"file download failed after 3 attempts: {last_err}")


def _pick_largest_photo(photos):
    candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(item.get("file_size") or 0),
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )


def prompt_from_telegram_message(message, update_id):
    """글이면 본문, 사진·이미지 파일이면 로컬 경로 프롬프트. 그록 image 경로 동형."""
    raw_text = message.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    caption = message.get("caption")
    caption_text = caption.strip() if isinstance(caption, str) else ""
    dest = media_output_dir()

    try:
        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            photo = _pick_largest_photo(photos)
            if photo:
                name_hint = f"telegram-{update_id}-{photo.get('file_unique_id') or photo.get('file_id')}"
                image_path = download_file(
                    str(photo["file_id"]),
                    dest,
                    name_hint,
                    default_suffix=".jpg",
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                        "file_size": photo.get("file_size"),
                    },
                )

        document = message.get("document") if isinstance(message.get("document"), dict) else None
        if document and str(document.get("mime_type") or "").startswith("image/"):
            file_id = str(document.get("file_id") or "")
            if file_id:
                name_hint = f"telegram-{update_id}-{document.get('file_unique_id') or file_id}"
                default_suffix = suffix_from_metadata(
                    str(document.get("file_name") or ""),
                    str(document.get("mime_type") or ""),
                    ".jpg",
                )
                image_path = download_file(
                    file_id,
                    dest,
                    name_hint,
                    default_suffix=default_suffix,
                    allowed_extensions=IMAGE_EXTENSIONS,
                )
                return image_prompt_text(
                    caption_text,
                    image_path,
                    {
                        "mime_type": document.get("mime_type"),
                        "file_name": document.get("file_name"),
                        "file_size": document.get("file_size"),
                    },
                )
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} image download 실패: {type(exc).__name__}", file=sys.stderr)
        if caption_text:
            return caption_text
        return ""

    return caption_text


def telegram_prompt_from_update(upd):
    # edited_message 는 같은 글을 두 번 붙이는 길이 된다. 원문 message 만 받는다.
    msg = upd.get("message")
    if not msg:
        return ""
    if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
        return ""
    return prompt_from_telegram_message(msg, upd.get("update_id"))


def drain_pending_updates():
    """첫 기동에서 쌓인 getUpdates 를 처리하지 않고 버린다.

    봇 생성·BotFather 확인 중에 들어온 글을 브릿지가 켜지는 순간 TUI 에
    다시 붙이면 안 된다. offset 파일이 이미 있으면 그대로 이어간다.
    """
    if DRY_RUN:
        return
    existing = _read(OFFSET_FILE)
    if existing.isdigit():
        return
    res = tg("getUpdates", timeout=5)
    last = 0
    for upd in res.get("result") or []:
        try:
            last = max(last, int(upd.get("update_id") or 0))
        except (TypeError, ValueError):
            continue
    if last:
        _write(OFFSET_FILE, last + 1)


def telegram_poller():
    offset = _read(OFFSET_FILE)
    offset = int(offset) if offset.isdigit() else 0
    while True:
        res = tg("getUpdates", offset=offset, timeout=30)
        health_mark(last_poll_at=time.time())
        if not res or not res.get("ok"):
            time.sleep(3)
            continue
        for upd in res.get("result", []):
            try:
                callback = upd.get("callback_query")
                if callback:
                    handle_telegram_callback(callback)
                    continue
                text = telegram_prompt_from_update(upd)
                if not text:
                    continue
                inbox_spool(upd.get("update_id"), "telegram", text)
                preview = text.splitlines()[0][:80]
                print(f">>> [{NAME}] 텔레그램 cursor: {preview}", flush=True)
                handle_message_text(text, source="telegram")
            except Exception as exc:  # noqa: BLE001
                print(f"telegram update 처리 실패: {exc}", file=sys.stderr)
            finally:
                offset = upd["update_id"] + 1
                _write(OFFSET_FILE, offset)


def send_typing_now():
    if DRY_RUN or not API:
        return {"ok": True, "result": "dry_run"}
    res = tg("sendChatAction", timeout=5, chat_id=CHAT_ID, action="typing")
    if not res.get("ok"):
        desc = str(res.get("description") or res.get("error") or "")[:80]
        print(f"{LOG_KEY} typing 실패 ok={res.get('ok')} {desc}", file=sys.stderr)
    return res


def typing_on():
    _TYPING_ACTIVE.set()
    send_typing_now()


def typing_off():
    _TYPING_ACTIVE.clear()


def typing_should_send():
    """잡이 살아 있거나, 잡은 닫혔어도 커서 턴이 아직 도는 중이면 「입력 중…」 유지."""
    return _TYPING_ACTIVE.is_set() or _TUI_JOB_ACTIVE.is_set() or _TUI_TURN_BUSY.is_set()


def typing_loop():
    while True:
        if typing_should_send() and not DRY_RUN:
            send_typing_now()
        time.sleep(4)


def main():
    print(
        f"{LOG_KEY} start name={NAME} node={NODE_KEY} session={TMUX_SESSION} "
        f"dry={int(DRY_RUN)} mirror={int(TUI_MIRROR_LOCAL)} "
        f"approval={int(APPROVAL_SURFACE_ENABLED)}",
        flush=True,
    )
    global _WORKER_THREAD
    _WORKER_THREAD = threading.Thread(target=worker_loop, name="cub-worker", daemon=True)
    _WORKER_THREAD.start()
    health_mark(worker_alive=True)
    threading.Thread(target=health_ticker, name="cub-health", daemon=True).start()
    threading.Thread(target=typing_loop, name="cub-typing", daemon=True).start()
    if TUI_MIRROR_LOCAL:
        threading.Thread(target=tui_local_mirror_ticker, name="cub-tui-mirror", daemon=True).start()
    if APPROVAL_SURFACE_ENABLED:
        threading.Thread(
            target=approval_surface_ticker,
            name="cub-approval-surface",
            daemon=True,
        ).start()
    if DRY_RUN:
        return
    drain_pending_updates()
    # inflight 회수는 턴이 끝날 때까지 기다린다(연장 포함 최대 TUI_WAIT_MAX_SEC). 메인 스레드에서
    # 돌리면 그동안 getUpdates 가 멈춰 폰에서 /status 조차 안 먹는다 → 스레드로 뺀다.
    # 회수 중엔 _TUI_JOB_ACTIVE 가 켜져 있어 새 텔레그램 글은 평소처럼 busy-inject(follow-up) 로 간다.
    threading.Thread(
        target=_recover_inflight_guarded, name="cub-inflight-recover", daemon=True
    ).start()
    try:
        harvest_orphaned_finals(newest_transcript_path())
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} 기동 회수 실패: {exc}", file=sys.stderr)
    telegram_poller()


def _recover_inflight_guarded():
    try:
        recover_inflight_on_startup()
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} inflight 회수 스레드 실패: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
