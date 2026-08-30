#!/usr/bin/env python3
"""Telegram -> Cursor bridge: one Telegram chat drives one machine's Cursor session.

Messages are pasted into an existing tmux session named `cursor` when that
session is alive. If it is gone, one `cursor-agent -p` process runs instead.
The finished answer is sent back through the Telegram Bot API.
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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
TUI_POLL_INTERVAL = float_env("CUB_TUI_POLL_INTERVAL", 0.5)
TUI_IDLE_SEC = float_env("CUB_TUI_IDLE_SEC", 8.0)
TUI_WAIT_SEC = int_env("CUB_TUI_WAIT_SEC", 900, minimum=10)
TUI_BUSY_INJECT = bool_env("CUB_BUSY_INJECT", True)
TUI_FALLBACK_HEADLESS = bool_env("CUB_TUI_FALLBACK_HEADLESS", True)
CURSOR_BIN = env("CUB_CURSOR_BIN", "cursor-agent")
TRANSCRIPT_ROOT = env(
    "CUB_TRANSCRIPT_ROOT",
    os.path.join(HOME, ".cursor", "projects"),
)
LOG_KEY = "cub_tui_lane"
DOWNLOAD_ATTEMPT_TIMEOUT = int_env("CUB_DOWNLOAD_TIMEOUT", 30, minimum=5)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

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
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""

JOBS: queue.Queue = queue.Queue()
_TUI_JOB_ACTIVE = threading.Event()
_TYPING_ACTIVE = threading.Event()
_HEALTH_LOCK = threading.Lock()
_HARVEST_GEN = 0
_LAST_FINAL_BODY = ""
_LAST_FINAL_LOCK = threading.Lock()
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
    "queue_depth": 0,
    "worker_alive": False,
}


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write(path, val):
    Path(path).write_text(str(val), encoding="utf-8")


def health_mark(**fields):
    with _HEALTH_LOCK:
        _HEALTH.update(fields)
        _HEALTH["queue_depth"] = JOBS.qsize()
        snap = dict(_HEALTH)
    try:
        Path(HEALTH_FILE).write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def inbox_spool(update_id, source, text):
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
    return _tmux("send-keys", "-t", TMUX_PANE, TUI_SUBMIT_KEY)


def _tui_paste(prompt, *, interrupt=False):
    payload = (prompt or "").rstrip("\n")
    if not payload:
        return
    # 그록 현행과 같이 본문은 send-keys 로 치지 않는다. 인터럽트 붙여넣기에
    # C-c 도 보내지 않는다(턴을 죽이면 회수할 답이 사라진다). 유휴 붙여넣기도
    # Cursor TUI 를 죽이면 안 되므로 C-c 없이 버퍼만 넣는다.
    if interrupt:
        _tmux("send-keys", "-t", TMUX_PANE, "Space")
        time.sleep(TUI_SUBMIT_DELAY)
    _tmux("load-buffer", "-", input_text=payload)
    _tmux("paste-buffer", "-p", "-t", TMUX_PANE)
    time.sleep(TUI_SUBMIT_DELAY)
    _tui_send_submit_key()


def list_transcript_paths(root=None):
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


def newest_transcript_path(root=None):
    paths = list_transcript_paths(root)
    return paths[-1] if paths else None


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


def assistant_texts(rows):
    out = []
    for row in rows:
        if row.get("role") != "assistant":
            continue
        joined = "\n".join(_content_texts(row)).strip()
        if joined:
            out.append(joined)
    return out


def assistant_final_texts(rows):
    """텍스트만 있는 assistant 행. 같은 줄에 tool_use 가 있으면 생각 중이라 최종이 아니다."""
    out = []
    for row in rows:
        if row.get("role") != "assistant":
            continue
        if row_has_tool_use(row):
            continue
        joined = "\n".join(_content_texts(row)).strip()
        if joined:
            out.append(joined)
    return out


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


def save_cursor(path, rows, last_final=None, finals_sent=None):
    rec = load_cursor_state()
    rec["path"] = str(path)
    rec["rows"] = int(rows)
    if last_final is not None:
        rec["last_final"] = last_final
    if finals_sent is not None:
        rec["finals_sent"] = int(finals_sent)
    _write(CURSOR_FILE, json.dumps(rec, ensure_ascii=False))


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
    if not path:
        return 0
    rows = read_transcript_rows(path)
    texts = assistant_final_texts(rows)
    rec = load_cursor_state()
    if rec.get("path") != str(path) or rec.get("finals_sent") is None:
        save_cursor(path, len(rows), last_final=str(rec.get("last_final") or ""), finals_sent=len(texts))
        return 0
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
        deliver_mesh_event("final", text)
        last = text
        sent += 1
    save_cursor(path, len(rows), last_final=last, finals_sent=len(texts))
    if sent:
        print(f"{LOG_KEY} orphaned final harvest {sent}건", flush=True)
    return sent


def wait_for_final(path, baseline, deadline, gen=None):
    last_len = baseline
    last_change = time.time()
    last_answer = ""
    while time.time() < deadline:
        if gen is not None and gen != _HARVEST_GEN:
            return ""
        rows = read_transcript_rows(path)
        if len(rows) != last_len:
            last_len = len(rows)
            last_change = time.time()
            last_answer = harvest_new_assistant(path, baseline)
        if last_answer and not tail_is_busy(rows):
            if turn_has_ended(rows) or (time.time() - last_change) >= TUI_IDLE_SEC:
                return last_answer
        time.sleep(TUI_POLL_INTERVAL)
    return last_answer


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


def deliver_mesh_event(kind, body, *, task_id=None):
    """Send one message straight to the Telegram Bot API.

    The maintainer internal build routes delivery through a private message
    bus; this public build has no such bus, so delivery is a direct
    `sendMessage` call. kind/task_id are accepted and ignored here.
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
    for chunk in _tg_chunks(payload, TG_CHUNK):
        res = tg("sendMessage", chat_id=CHAT_ID, text=chunk)
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


def run_headless(prompt):
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
    print(f"{LOG_KEY} busy 중 텔레그램 삽입", file=sys.stderr)
    return {"kind": "injected_harvest", "baseline": baseline, "path": str(path or "")}


def process_job(source, text, meta=None):
    health_mark(last_job_started_at=time.time(), worker_alive=True)
    _TUI_JOB_ACTIVE.set()
    try:
        injected = isinstance(meta, dict) and meta.get("kind") == "injected_harvest"
        path = Path(meta["path"]) if injected and meta.get("path") else newest_transcript_path()
        baseline = int((meta or {}).get("baseline") or 0) if injected else (
            len(read_transcript_rows(path)) if path else 0
        )
        if path:
            bind_cursor_if_needed(path, baseline if injected else None)
        if not injected:
            if tui_session_alive():
                _tui_paste(text, interrupt=False)
                path = newest_transcript_path() or path
            elif TUI_FALLBACK_HEADLESS:
                answer = run_headless(text)
                deliver_mesh_event("final", answer)
                return
            else:
                deliver_mesh_event("error", tui_dead_message())
                return
        deadline = time.time() + TUI_WAIT_SEC
        gen = _HARVEST_GEN
        answer = wait_for_final(path, baseline, deadline, gen=gen) if path else ""
        sent = harvest_orphaned_finals(path) if path else 0
        if sent:
            return
        if gen != _HARVEST_GEN:
            return
        if answer:
            return
        if TUI_FALLBACK_HEADLESS and not injected:
            answer = run_headless(text)
            if answer:
                deliver_mesh_event("final", answer)
                return
        deliver_mesh_event("error", "커서가 답을 안 남겼어. TUI 창을 한 번 봐줘.")
    finally:
        _TUI_JOB_ACTIVE.clear()
        if JOBS.empty():
            typing_off()
        health_mark(last_job_done_at=time.time(), done=_HEALTH.get("done", 0) + 1)


def start_ready_message():
    return (
        "Cursor Telegram Bridge is on. Messages you send here go into the "
        "Cursor session on this machine."
    )


def handle_message_text(text, source="telegram", meta=None):
    global _HARVEST_GEN
    token = (text or "").strip()
    if token.lower() in ("/start", "/ping"):
        deliver_mesh_event("final", start_ready_message())
        return
    typing_on()
    _HARVEST_GEN += 1
    busy = maybe_busy_inject_telegram(text, source)
    if busy:
        JOBS.put((source, text, busy))
        health_mark(last_enqueue_at=time.time(), enqueued=_HEALTH.get("enqueued", 0) + 1)
        return
    JOBS.put((source, text, meta))
    health_mark(last_enqueue_at=time.time(), enqueued=_HEALTH.get("enqueued", 0) + 1)


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


def typing_loop():
    while True:
        if (_TYPING_ACTIVE.is_set() or _TUI_JOB_ACTIVE.is_set()) and not DRY_RUN:
            send_typing_now()
        time.sleep(4)


def main():
    print(f"{LOG_KEY} start name={NAME} node={NODE_KEY} session={TMUX_SESSION} dry={int(DRY_RUN)}", flush=True)
    threading.Thread(target=worker_loop, name="cub-worker", daemon=True).start()
    health_mark(worker_alive=True)
    threading.Thread(target=typing_loop, name="cub-typing", daemon=True).start()
    if DRY_RUN:
        return
    drain_pending_updates()
    try:
        harvest_orphaned_finals(newest_transcript_path())
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_KEY} 기동 회수 실패: {exc}", file=sys.stderr)
    telegram_poller()


if __name__ == "__main__":
    main()
