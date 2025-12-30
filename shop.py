#!/usr/bin/env python3
"""
Bench Controller (Flask + 2x MCP23017 + USB switching + MQTT + ADS1115)
+ Interactive PTY terminal via Socket.IO (xterm.js client)

Terminal events:
- Client emits: term_open, term_in, term_resize
- Server emits: term_out

Key reliability goals:
- Service must run even if I2C/MCP/ADS are missing (virtual state fallback)
- Eventlet-safe terminal (green thread reader, monkey_patch early)
- Proper PTY controlling TTY + resize + clean session teardown
"""

# ============================================================
# IMPORTANT: eventlet monkey patch MUST be first
# ============================================================
import eventlet  # type: ignore
eventlet.monkey_patch()

import os
import time
import threading
from typing import Dict, Optional, Tuple, Any

# ---- Terminal (PTY) + Socket.IO ----
import pty
import fcntl
import select
import termios
import struct
import signal
import subprocess
import json
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from flask import Flask, jsonify, request, Response, render_template
from flask_socketio import SocketIO  # type: ignore
from PIL import ImageFont
from luma.core.render import canvas

# --- I2C / MCP libraries ---
try:
    import board  # type: ignore
    import busio  # type: ignore
    from digitalio import Direction  # type: ignore
    from adafruit_mcp230xx.mcp23017 import MCP23017  # type: ignore

    _I2C_LIBS_AVAILABLE = True
except Exception:
    board = None  # type: ignore
    busio = None  # type: ignore
    Direction = None  # type: ignore
    MCP23017 = None  # type: ignore
    _I2C_LIBS_AVAILABLE = False

# --- ADS1115 libraries ---
try:
    import adafruit_ads1x15.ads1115 as ADS  # type: ignore
    from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore

    _ADS_AVAILABLE = True
except Exception:
    ADS = None  # type: ignore
    AnalogIn = None  # type: ignore
    _ADS_AVAILABLE = False

# --- Optional MQTT ---
try:
    import paho.mqtt.client as mqtt  # type: ignore

    _MQTT_AVAILABLE = True
except Exception:
    mqtt = None  # type: ignore
    _MQTT_AVAILABLE = False

# ============================================================
# Configuration
# ============================================================
# ============================================================
# TCP serial configuration
# ============================================================
BENCH_TCP = {
    1: {"host": "127.0.0.1", "port": 3001},
    2: {"host": "127.0.0.1", "port": 3002},
    3: {"host": "127.0.0.1", "port": 3003},
    4: {"host": "127.0.0.1", "port": 3004},
}

# ============================================================
# OLED (I2C 0x3C) scrolling ticker for bench state
# ============================================================


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 26   # THIS is what makes it big

font = ImageFont.truetype(FONT_PATH, FONT_SIZE)

OLED_ENABLED = os.environ.get("OLED_ENABLED", "1") in ("1","true","True","yes","YES")
OLED_I2C_ADDR = int(os.environ.get("OLED_I2C_ADDR", "0x3C"), 16)
OLED_SCROLL_FPS = float(os.environ.get("OLED_SCROLL_FPS", "30"))  # smoothness
OLED_SCROLL_PX_PER_STEP = int(os.environ.get("OLED_SCROLL_STEP", "6"))

_oled_ok = False
_oled_err = None
_oled_device = None
_oled_font = None


try:
    if OLED_ENABLED:
        from luma.core.interface.serial import i2c as luma_i2c  # type: ignore
        from luma.oled.device import ssd1306, sh1106            # type: ignore
        from luma.core.render import canvas                     # type: ignore
        from PIL import ImageFont                               # type: ignore
except Exception as e:
    _oled_ok = False
    _oled_err = f"oled import failed: {e}"

# ============================================================
# Action logging + retention
# ============================================================

LOG_DIR = os.environ.get("BENCH_LOG_DIR", os.path.expanduser("~/bench_logs"))
LOG_RETENTION_DAYS = int(os.environ.get("BENCH_LOG_RETENTION_DAYS", "7"))
term_line_buffers: Dict[str, str] = {}
term_line_buffers_lock = threading.Lock()

# Rolling daily files: bench_actions_YYYY-MM-DD.jsonl
def _log_path_for_today() -> str:
    d = datetime.now(timezone.utc).date().isoformat()
    return os.path.join(LOG_DIR, f"bench_actions_{d}.jsonl")

_log_lock = threading.Lock()

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _ensure_log_dir() -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass

def _safe_client_ip() -> Optional[str]:
    try:
        # honors reverse proxies if you ever add one
        xf = request.headers.get("X-Forwarded-For", "")
        if xf:
            return xf.split(",")[0].strip()
        return request.remote_addr
    except Exception:
        return None

def _safe_route() -> Optional[str]:
    try:
        return request.path
    except Exception:
        return None

def log_event(event: str, **fields: Any) -> None:
    """
    Writes one JSON line per event to today's file.
    Never throws (logging must not break the controller).
    """
    rec = {
        "ts": _utc_now_iso(),
        "event": event,
        # context (best-effort)
        "route": _safe_route(),
        "ip": _safe_client_ip(),
        **fields,
    }
    line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)

    try:
        _ensure_log_dir()
        path = _log_path_for_today()
        with _log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[WARN] log_event failed: {e}")
def _log_tail_lines(n: int = 50) -> list[str]:
    """
    Returns last n log lines (pretty-printed).
    Reads across the most recent log file only.
    """
    try:
        path = _log_path_for_today()
        if not os.path.exists(path):
            return ["[no logs for today]"]

        with _log_lock:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()

        tail = lines[-n:]
        out = []
        for ln in tail:
            try:
                j = json.loads(ln)
                ts = j.get("ts", "")
                ev = j.get("event", "")
                bench = j.get("bench", "")
                rail = j.get("rail", "")
                state = j.get("state", "")
                msg = f"{ts}  {ev}"
                if bench:
                    msg += f"  {bench}"
                if rail:
                    msg += f"  {rail}"
                if state != "":
                    msg += f"  {'ON' if state else 'OFF'}"
                out.append(msg)
            except Exception:
                out.append(ln)

        return out or ["[log file empty]"]
    except Exception as e:
        return [f"[log read error: {e}]"]

def _parse_date_from_filename(name: str) -> Optional[datetime]:
    # expects bench_actions_YYYY-MM-DD.jsonl
    try:
        if not name.startswith("bench_actions_") or not name.endswith(".jsonl"):
            return None
        ds = name[len("bench_actions_"):-len(".jsonl")]
        d = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None

def purge_old_logs(retention_days: int = 7) -> int:
    """
    Deletes bench_actions_YYYY-MM-DD.jsonl older than retention_days.
    Returns number of deleted files. Never throws.
    """
    try:
        _ensure_log_dir()
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = 0
        for fn in os.listdir(LOG_DIR):
            d = _parse_date_from_filename(fn)
            if not d:
                continue
            # delete logs strictly older than cutoff date
            if d < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                try:
                    os.remove(os.path.join(LOG_DIR, fn))
                    deleted += 1
                except Exception:
                    pass
        return deleted
    except Exception:
        return 0

def _purge_loop() -> None:
    # run once at boot, then daily
    purge_old_logs(LOG_RETENTION_DAYS)
    while True:
        time.sleep(24 * 3600)
        purge_old_logs(LOG_RETENTION_DAYS)


APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))

# Relay boards are commonly ACTIVE-LOW: pin LOW energizes relay / turns rail ON.
RELAY_ACTIVE_LOW = True

# Boot safety
BOOT_SAFE_KILL_ALL = True          # kill (OFF) all bench rails on startup
DEFAULT_LR_LOCAL = True            # boot LR pins HIGH = Local
DEFAULT_USB_DATA_DISABLED = True   # boot EN3/EN4 HIGH (disabled)
DEFAULT_USB_VBUS_DISABLED = True   # boot VBUS EN LOW (disabled)

# Bench service timing
USB_DATA_TO_VBUS_DELAY_S = 0.10
USB_VBUS_TO_DATA_DELAY_S = 0.10
SERVICE_WAIT_AFTER_KILL_S = 3.0

# MQTT
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "0") in ("1", "true", "True", "yes", "YES")
MQTT_BROKER = os.environ.get("MQTT_BROKER", "192.168.1.8")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "shop-controller")
MQTT_BASE = os.environ.get("MQTT_BASE", "shop_controller")

# MCP addresses (truth for I2C addresses)
MCP_ADDR = {"mcp1": 0x20, "mcp2": 0x21}

# ============================================================
# Channel mapping (TRUTH = THIS DICT)
# name -> (mcp_key, pin_index 0..15)
# ============================================================

CHANNELS: Dict[str, Tuple[str, int]] = {
    # -----------------------------
    # mcp2 (0x21): 5V/12V rails + port3/4 !OE + spares
    # -----------------------------
    # 5V rails (GPA0..3)
    "bench1_5v": ("mcp2", 0),
    "bench2_5v": ("mcp2", 1),
    "bench3_5v": ("mcp2", 2),
    "bench4_5v": ("mcp2", 3),

    # 12V rails (GPA4..7)
    "bench1_12v": ("mcp2", 4),
    "bench2_12v": ("mcp2", 5),
    "bench3_12v": ("mcp2", 6),
    "bench4_12v": ("mcp2", 7),

    # USB Port 3/4 data gate (!OE) (GPB0..1) ACTIVE-LOW
    "port4_en": ("mcp2", 8),   # ON => LOW
    "port3_en": ("mcp2", 9),   # ON => LOW

    # Spares (GPB2..3)
    "spare_1": ("mcp2", 10),
    "spare_2": ("mcp2", 11),

    # VBUS enables (GPB4..7) ACTIVE-HIGH
    "port3_vcc_en": ("mcp2", 12),  # VCCS3
    "port4_vcc_en": ("mcp2", 13),  # VCCS4
    "port2_vcc_en": ("mcp2", 14),  # VCCS2
    "port1_vcc_en": ("mcp2", 15),  # VCCS1

    # -----------------------------
    # mcp1 (0x20): HV rails + LR select + indicators
    # -----------------------------
    # HV rails (GPA0..3)
    "bench1_hv": ("mcp1", 0),
    "bench2_hv": ("mcp1", 1),
    "bench3_hv": ("mcp1", 2),
    "bench4_hv": ("mcp1", 3),

    # GPA4..5
    "air_compressor": ("mcp1", 4),
    "lights": ("mcp1", 5),

    # Local/Remote selects (GPA6..7) HIGH=Local, LOW=Remote
    "lr1": ("mcp1", 6),
    "lr2": ("mcp1", 7),

    # Indicators / extras (GPB0..7 => 8..15)
    "stat_1": ("mcp1", 8),
    "stat_2": ("mcp1", 9),
    "stack_r": ("mcp1", 10),
    "stack_a": ("mcp1", 11),
    "stack_g": ("mcp1", 12),
    "ring_1": ("mcp1", 13),
    "ring_2": ("mcp1", 14),
    "spare_3": ("mcp1", 15),
}

# Bench rails to kill (semantic OFF)
BENCHES: Dict[str, list[str]] = {
    "bench1": ["bench1_hv", "bench1_12v", "bench1_5v"],
    "bench2": ["bench2_hv", "bench2_12v", "bench2_5v"],
    "bench3": ["bench3_hv", "bench3_12v", "bench3_5v"],
    "bench4": ["bench4_hv", "bench4_12v", "bench4_5v"],
}
BENCH_TO_USB_PORT: Dict[str, int] = {"bench1": 1, "bench2": 2, "bench3": 3, "bench4": 4}

RAIL_CHANNELS = set()
for b in (1,2,3,4):
    RAIL_CHANNELS.update({f"bench{b}_5v", f"bench{b}_12v", f"bench{b}_hv"})

# Polarity overrides (mixed output types!)
ACTIVE_HIGH_CHANNELS = {"port1_vcc_en", "port2_vcc_en", "port3_vcc_en", "port4_vcc_en"}
ACTIVE_LOW_CHANNELS = {"port3_en", "port4_en"}  # ON means !OE LOW

# ============================================================
# ADS1115 Pressure Sensor (A0)
# ============================================================

PRESSURE_SENSOR_MAX_PSI = float(os.environ.get("PRESSURE_SENSOR_MAX_PSI", "200.0"))
PRESSURE_WIRING_MODE = os.environ.get("PRESSURE_WIRING_MODE", "divider").strip().lower()  # divider|bypass
PRESSURE_SMOOTH_ALPHA = float(os.environ.get("PRESSURE_SMOOTH_ALPHA", "0.25"))
PRESSURE_POLL_HZ = float(os.environ.get("PRESSURE_POLL_HZ", "10"))
ADS_GAIN = 1

current_psi = 0.0
pressure_lock = threading.Lock()
ads_available = False
_ads_channel = None  # AnalogIn

# ============================================================
# App + Runtime state
# ============================================================

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
# ------------------------------
# Bench Names Persistence
# ------------------------------
BENCH_NAMES_LOCK = threading.Lock()

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BENCH_NAMES_PATH = DATA_DIR / "bench_names.json"

DEFAULT_BENCH_NAMES = {"b1": "", "b2": "", "b3": "", "b4": ""}

def _load_bench_names() -> dict:
    try:
        if not BENCH_NAMES_PATH.exists():
            return dict(DEFAULT_BENCH_NAMES)
        with BENCH_NAMES_PATH.open("r", encoding="utf-8") as f:
            obj = json.load(f) or {}
        # normalize keys
        return {
            "b1": str(obj.get("b1", "") or ""),
            "b2": str(obj.get("b2", "") or ""),
            "b3": str(obj.get("b3", "") or ""),
            "b4": str(obj.get("b4", "") or ""),
        }
    except Exception:
        return dict(DEFAULT_BENCH_NAMES)

def _save_bench_names(names: dict) -> None:
    payload = {
        "b1": str(names.get("b1", "") or ""),
        "b2": str(names.get("b2", "") or ""),
        "b3": str(names.get("b3", "") or ""),
        "b4": str(names.get("b4", "") or ""),
    }
    tmp = BENCH_NAMES_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(BENCH_NAMES_PATH)

# I2C optional
i2c = None
I2C_AVAILABLE = False
I2C_BUS_ERROR: Optional[str] = None

if _I2C_LIBS_AVAILABLE:
    try:
        i2c = busio.I2C(board.SCL, board.SDA)  # type: ignore[arg-type, union-attr]
        I2C_AVAILABLE = True
    except Exception as e:
        i2c = None
        I2C_AVAILABLE = False
        I2C_BUS_ERROR = str(e)
        print(f"[WARN] I2C bus not available; running in NO-HARDWARE mode: {e}")
else:
    I2C_BUS_ERROR = "I2C libraries not installed"
    print("[WARN] I2C libs unavailable. Running in NO-HARDWARE mode.")

mcps: Dict[str, Optional[Any]] = {"mcp1": None, "mcp2": None}
mcp_available: Dict[str, bool] = {"mcp1": False, "mcp2": False}
relay_pins: Dict[str, Any] = {}

# Semantic state:
# - rails / vbus / !OE: True means enabled
# - lr1/lr2: True means REMOTE (LOW), False means LOCAL (HIGH)
state: Dict[str, bool] = {name: False for name in CHANNELS.keys()}
state_lock = threading.Lock()

mqtt_client: Optional[Any] = None
_missing_pin_warned: set[str] = set()

# ============================================================
# Terminal session management (eventlet-safe)
# ============================================================

class TermSession:
    def __init__(self, sid: str, master_fd: int, proc: subprocess.Popen, slave_fd: int):
        self.sid = sid
        self.master_fd = master_fd
        self.slave_fd = slave_fd
        self.proc = proc
        self.alive = True
        self.lock = threading.Lock()

term_sessions: Dict[str, TermSession] = {}
term_sessions_lock = threading.Lock()


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

def _pty_resize(fd: int, cols: int, rows: int) -> None:
    cols = int(cols or 80)
    rows = int(rows or 24)
    winsz = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsz)

def _kill_session(sess: TermSession) -> None:
    # Make teardown idempotent and race-safe
    with sess.lock:
        if not sess.alive:
            return
        sess.alive = False

    # terminate process group first
    try:
        pgid = os.getpgid(sess.proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        pass

    # give it a moment, then SIGKILL if needed
    try:
        eventlet.sleep(0.1)
    except Exception:
        time.sleep(0.1)

    try:
        if sess.proc.poll() is None:
            pgid = os.getpgid(sess.proc.pid)
            os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            sess.proc.kill()
        except Exception:
            pass

    # close fds
    for fd in (sess.master_fd, sess.slave_fd):
        try:
            os.close(fd)
        except Exception:
            pass

def _term_reader_loop(sess: TermSession) -> None:
    try:
        _set_nonblocking(sess.master_fd)
    except Exception:
        pass

    while True:
        with sess.lock:
            if not sess.alive:
                break

        if sess.proc.poll() is not None:
            break

        try:
            r, _, _ = select.select([sess.master_fd], [], [], 0.25)
            if not r:
                continue
            data = os.read(sess.master_fd, 4096)
            if not data:
                break
            socketio.emit("term_out", data.decode("utf-8", errors="ignore"), to=sess.sid)
        except OSError:
            break
        except Exception:
            continue

    _kill_session(sess)
    with term_sessions_lock:
        term_sessions.pop(sess.sid, None)

def _spawn_shell_for_sid(sid: str, cols: int = 80, rows: int = 24) -> TermSession:
    master_fd, slave_fd = pty.openpty()

    # initial resize (best-effort)
    try:
        _pty_resize(master_fd, cols, rows)
    except Exception:
        pass

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"

    # ensure the slave becomes the controlling TTY in the child
    def _preexec() -> None:
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    proc = subprocess.Popen(
        ["/bin/bash", "-l"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=_preexec,
        close_fds=True,
    )

    sess = TermSession(sid=sid, master_fd=master_fd, proc=proc, slave_fd=slave_fd)
    # eventlet green thread (avoids stdlib thread issues under eventlet)
    eventlet.spawn_n(_term_reader_loop, sess)
    return sess

# ============================================================
# Helpers: polarity + hardware access
# ============================================================

def _relay_off_value() -> bool:
    return True if RELAY_ACTIVE_LOW else False

def _relay_on_value() -> bool:
    return False if RELAY_ACTIVE_LOW else True

def _compute_pin_level(channel: str, on: bool) -> bool:
    if channel in ACTIVE_HIGH_CHANNELS:
        return True if on else False
    if channel in ACTIVE_LOW_CHANNELS:
        return False if on else True
    return _relay_on_value() if on else _relay_off_value()

def _safe_warn_missing(channel: str) -> None:
    if channel not in _missing_pin_warned:
        _missing_pin_warned.add(channel)
        print(f"[WARN] Channel '{channel}' has no hardware pin. State will be virtual.")

def _set_pin_level(channel: str, level: bool) -> bool:
    pin = relay_pins.get(channel)
    if pin is None:
        _safe_warn_missing(channel)
        return False
    try:
        pin.value = level  # type: ignore[attr-defined]
        return True
    except Exception as e:
        print(f"[WARN] Failed to write '{channel}' level={level}: {e}")
        return False

def _mqtt_topic_state(ch: str) -> str:
    return f"{MQTT_BASE}/state/{ch}"

def _mqtt_publish_state(ch: str, value: bool) -> None:
    if not (MQTT_ENABLED and _MQTT_AVAILABLE and mqtt_client):
        return
    try:
        mqtt_client.publish(_mqtt_topic_state(ch), payload=("ON" if value else "OFF"), retain=True)
    except Exception:
        pass

def get_channel(channel: str) -> bool:
    """Return the current semantic state for a channel (thread-safe).

    Returns False for unknown channels or on error.
    """
    try:
        with state_lock:
            return bool(state.get(channel, False))
    except Exception:
        return False

def set_channel(channel: str, on: bool) -> None:
    prev = get_channel(channel)

    level = _compute_pin_level(channel, on)
    _set_pin_level(channel, level)

    with state_lock:
        state[channel] = bool(on)

    _mqtt_publish_state(channel, on)

    if prev != bool(on):
        if channel in RAIL_CHANNELS:
            bench = channel.split("_")[0]     # bench1
            rail_type = channel.split("_")[1] # hv / 12v / 5v
            log_event(
                "rail_change",
                channel=channel,
                bench=bench,
                rail=rail_type,
                state=bool(on),
                prev=bool(prev),
            )
        elif channel.startswith("port") and (channel.endswith("_vcc_en") or channel.endswith("_en")):
            # optional: log USB related channel flips too
            log_event(
                "usb_channel_change",
                channel=channel,
                state=bool(on),
                prev=bool(prev),
            )


# ============================================================
# LR control (SPECIAL SEMANTICS: True = REMOTE)
# ============================================================

def set_lr(port: int, remote: bool) -> None:
    if port not in (1, 2):
        raise ValueError("LR port must be 1 or 2")
    ch = "lr1" if port == 1 else "lr2"
    _set_pin_level(ch, False if remote else True)  # LOW=remote, HIGH=local
    with state_lock:
        state[ch] = bool(remote)
    _mqtt_publish_state(ch, remote)

def enable_port_1_policy() -> None:
    # Port 1 enable = BOTH LR1 and LR2 LOW
    set_lr(1, remote=True)
    set_lr(2, remote=True)

def disable_port_1_policy() -> None:
    set_lr(1, remote=False)
    set_lr(2, remote=False)

# ============================================================
# Bench rail helpers
# ============================================================

def bench_kill_power(bench: str) -> None:
    if bench not in BENCHES:
        raise ValueError(f"Unknown bench '{bench}'")
    for ch in BENCHES[bench]:
        set_channel(ch, False)

def bench_enable_power(bench: str) -> None:
    if bench not in BENCHES:
        raise ValueError(f"Unknown bench '{bench}'")
    for ch in BENCHES[bench]:
        set_channel(ch, True)

# ============================================================
# USB helpers
# ============================================================

def usb_set_data(port: int, enable: bool) -> None:
    """
    Policy/truth:
    - Port 1: enable = BOTH LR1+LR2 LOW
    - Port 2: enable = LR2 LOW (only)
    - Port 3: enable = port3_en LOW (active-low !OE)
    - Port 4: enable = port4_en LOW (active-low !OE)
    """
    if port == 1:
        enable_port_1_policy() if enable else disable_port_1_policy()
        return
    if port == 2:
        set_lr(2, remote=enable)
        return
    if port == 3:
        set_channel("port3_en", enable)
        return
    if port == 4:
        set_channel("port4_en", enable)
        return
    raise ValueError("USB port must be 1..4")

def usb_set_vbus(port: int, enable: bool) -> None:
    if port not in (1, 2, 3, 4):
        raise ValueError("USB port must be 1..4")
    ch = f"port{port}_vcc_en"
    if ch not in CHANNELS:
        raise ValueError(f"Missing channel '{ch}' in CHANNELS mapping")
    set_channel(ch, enable)

def usb_port_enable(port: int, data: bool = True, vbus: bool = True) -> None:
    if data and vbus:
        usb_set_data(port, True)
        time.sleep(USB_DATA_TO_VBUS_DELAY_S)
        usb_set_vbus(port, True)
        return
    if (not data) and (not vbus):
        usb_set_vbus(port, False)
        time.sleep(USB_VBUS_TO_DATA_DELAY_S)
        usb_set_data(port, False)
        return
    usb_set_data(port, data)
    usb_set_vbus(port, vbus)

# ============================================================
# Bench service macro
# ============================================================
def bench_service_enable(bench: str) -> None:
    if bench not in BENCHES:
        raise ValueError(f"Unknown bench '{bench}'")

    log_event("service_mode_enter", bench=bench)

    bench_kill_power(bench)
    time.sleep(SERVICE_WAIT_AFTER_KILL_S)

    port = BENCH_TO_USB_PORT.get(bench)
    if not port:
        raise ValueError(f"No USB port mapping for bench '{bench}'")
    usb_port_enable(port, data=True, vbus=True)

def bench_service_disable(bench: str) -> None:
    port = BENCH_TO_USB_PORT.get(bench)
    if not port:
        raise ValueError(f"No USB port mapping for bench '{bench}'")

    usb_port_enable(port, data=False, vbus=False)

    log_event("service_mode_exit", bench=bench)


# ============================================================
# ADS1115 Pressure Sensor (A0)
# ============================================================

def _pressure_divider_ratio() -> float:
    if PRESSURE_WIRING_MODE == "bypass":
        return 1.0
    # 20k top / 10k bottom => 1/3
    return (10_000.0 / (20_000.0 + 10_000.0))

def _adc_init_ads1115() -> None:
    global ads_available, _ads_channel
    if not I2C_AVAILABLE or i2c is None:
        print("[WARN] No I2C bus; ADS1115 pressure reading disabled.")
        ads_available = False
        return
    if not _ADS_AVAILABLE:
        print("[WARN] ADS1115 libraries not installed; pressure reading disabled.")
        ads_available = False
        return
    try:
        ads = ADS.ADS1115(i2c, address=0x48)  # type: ignore[arg-type]
        ads.gain = ADS_GAIN
        _ads_channel = AnalogIn(ads, ADS.P0)  # type: ignore[call-arg]
        ads_available = True
        print("[OK] ADS1115 initialized at 0x48, reading A0 (P0).")
    except Exception as e:
        ads_available = False
        _ads_channel = None
        print(f"[WARN] ADS1115 init failed: {e}")

def _pressure_voltage_to_psi(sensor_voltage: float, sensor_vref: float) -> float:
    if sensor_vref <= 0.01:
        return 0.0
    psi = (sensor_voltage / sensor_vref) * PRESSURE_SENSOR_MAX_PSI
    return max(0.0, min(PRESSURE_SENSOR_MAX_PSI, psi))

def _pressure_read_once() -> float:
    if not (ads_available and _ads_channel):
        return 0.0
    try:
        v_ads = float(_ads_channel.voltage)  # type: ignore[union-attr]
    except Exception:
        return 0.0
    ratio = _pressure_divider_ratio() or 1.0
    v_sensor = v_ads / ratio
    sensor_vref = 3.3 if PRESSURE_WIRING_MODE == "bypass" else 5.0
    return _pressure_voltage_to_psi(v_sensor, sensor_vref)

def _pressure_loop() -> None:
    global current_psi
    period = 1.0 / max(0.5, PRESSURE_POLL_HZ)
    ema: Optional[float] = None
    while True:
        try:
            psi = _pressure_read_once()
            if ema is None:
                ema = psi
            else:
                a = min(1.0, max(0.0, PRESSURE_SMOOTH_ALPHA))
                ema = (a * psi) + ((1.0 - a) * ema)
            with pressure_lock:
                current_psi = float(ema if ema is not None else psi)
        except Exception:
            pass
        time.sleep(period)

# ============================================================
# MQTT (optional)
# ============================================================

def _mqtt_setup() -> None:
    global mqtt_client
    if not (MQTT_ENABLED and _MQTT_AVAILABLE):
        return
    try:
        client = mqtt.Client(client_id=MQTT_CLIENT_ID)
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
        mqtt_client = client
        with state_lock:
            for ch, v in state.items():
                _mqtt_publish_state(ch, v)
        print("[OK] MQTT connected.")
    except Exception as e:
        mqtt_client = None
        print(f"[WARN] MQTT setup failed: {e}")

# ============================================================
# MCP init + boot defaults
# ============================================================

def _init_mcp(mcp_key: str) -> None:
    addr = MCP_ADDR[mcp_key]
    if not I2C_AVAILABLE or i2c is None or MCP23017 is None:
        mcps[mcp_key] = None
        mcp_available[mcp_key] = False
        return
    try:
        mcps[mcp_key] = MCP23017(i2c, address=addr)  # type: ignore[misc]
        mcp_available[mcp_key] = True
    except Exception as e:
        mcps[mcp_key] = None
        mcp_available[mcp_key] = False
        print(f"[WARN] MCP {mcp_key} @ 0x{addr:02X} not available: {e}")

def _init_pins() -> None:
    for mcp_key in ("mcp1", "mcp2"):
        _init_mcp(mcp_key)

    if Direction is None:
        return

    for name, (mcp_key, pin_idx) in CHANNELS.items():
        mcp = mcps.get(mcp_key)
        if not mcp:
            continue
        try:
            pin = mcp.get_pin(pin_idx)
            pin.direction = Direction.OUTPUT
            relay_pins[name] = pin
        except Exception as e:
            print(f"[WARN] Could not init pin '{name}' on {mcp_key}[{pin_idx}]: {e}")

def _apply_boot_defaults() -> None:
    # Default everything OFF (semantic OFF)
    for ch in CHANNELS.keys():
        if ch in ("lr1", "lr2"):
            _set_pin_level(ch, True if DEFAULT_LR_LOCAL else False)  # HIGH=local
            with state_lock:
                state[ch] = (not DEFAULT_LR_LOCAL)  # True=remote
            continue

        _set_pin_level(ch, _compute_pin_level(ch, False))
        with state_lock:
            state[ch] = False

    # Ensure USB data disabled on ports 3/4 (EN lines HIGH)
    if DEFAULT_USB_DATA_DISABLED:
        for ch in ("port3_en", "port4_en"):
            _set_pin_level(ch, True)  # !OE HIGH => disabled
            with state_lock:
                state[ch] = False

    # Ensure VBUS disabled
    if DEFAULT_USB_VBUS_DISABLED:
        for ch in ("port1_vcc_en", "port2_vcc_en", "port3_vcc_en", "port4_vcc_en"):
            _set_pin_level(ch, False)
            with state_lock:
                state[ch] = False

    # Kill all rails at boot
    if BOOT_SAFE_KILL_ALL:
        for b in BENCHES.keys():
            bench_kill_power(b)

def _oled_init() -> None:
    """Best-effort OLED init. Never throws."""
    global _oled_ok, _oled_err, _oled_device, _oled_font
    if not OLED_ENABLED:
        return
    try:
        # bus 1 on modern Pi; address is your 0x3C
        serial = luma_i2c(port=1, address=OLED_I2C_ADDR)

        # Try SSD1306 first, then SH1106 (common 1.3" modules)
        try:
            _oled_device = ssd1306(serial)
        except Exception:
            _oled_device = sh1106(serial)

        # Use a simple bitmap-ish font; fallback to default if not available
        try:
            _oled_font = ImageFont.load_default()
        except Exception:
            _oled_font = None

        _oled_ok = True
        _oled_err = None
        print(f"[OK] OLED init at 0x{OLED_I2C_ADDR:02X}")
    except Exception as e:
        _oled_ok = False
        _oled_err = str(e)
        print(f"[WARN] OLED init failed: {e}")


def _bench_state_summary() -> str:
    """
    Build one-line ticker text from your semantic state dict.
    Keep it short; scrolling will reveal more.
    """
    def b_on(bench: int) -> bool:
        # "benchXMaster" meaning: HV + 12V + 5V all ON
        with state_lock:
            hv  = bool(state.get(f"bench{bench}_hv", False))
            v12 = bool(state.get(f"bench{bench}_12v", False))
            v5  = bool(state.get(f"bench{bench}_5v", False))
        return hv and v12 and v5

    # service mode = any USB VBUS enabled or port3/4 data enabled etc (edit as desired)
    with state_lock:
        p1 = state.get("port1_vcc_en", False)
        p2 = state.get("port2_vcc_en", False)
        p3 = state.get("port3_vcc_en", False)
        p4 = state.get("port4_vcc_en", False)
        d3 = state.get("port3_en", False)   # semantic True = enabled
        d4 = state.get("port4_en", False)

    benches = " ".join([f"B{n}:{'ON' if b_on(n) else 'OFF'}" for n in (1,2,3,4)])
    usb = f"USB VBUS[{int(p1)}{int(p2)}{int(p3)}{int(p4)}] DATA3:{'1' if d3 else '0'} DATA4:{'1' if d4 else '0'}"
    return f"{benches}  |  {usb}"


def _oled_scroll_loop() -> None:
 

    W = int(getattr(_oled_device, "width", 128))
    H = int(getattr(_oled_device, "height", 32))  # keep your assumption

    # ---- BIG FONT SETUP ----
    FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_size = 26 if H <= 32 else 34

    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = _oled_font  # fallback (may be small)

    pad = "   •   "
    x = W

    fps = max(10.0, float(OLED_SCROLL_FPS))
    frame_delay = 1.0 / fps
    step = int(OLED_SCROLL_PX_PER_STEP)

    # Precompute font height once
    # bbox: (left, top, right, bottom)
    font_h = int(font.getbbox("Ag")[3])

    while True:
        try:
            text = _bench_state_summary()
            ticker = text + pad + text + pad

            # Accurate width for BIG fonts (no canvas needed)
            try:
                text_w = int(font.getlength(ticker))
            except Exception:
                # fallback if Pillow is older
                text_w = int(font.getbbox(ticker)[2])

            # Vertical centering for 128x32 looks best
            y = max(0, (H - font_h) // 2)
            with canvas(_oled_device) as draw:
                # Clear ONLY the band where the big text lives
                draw.rectangle((0, y, W, y + font_h + 2), fill=0)

                # Draw the BIG ticker
                draw.text((x, y), ticker, font=font, fill=255)

            x -= step
            if x < -text_w:
                x = W

        except Exception:
            # Don't spin at 100% if something goes wrong
            time.sleep(0.25)

        time.sleep(frame_delay)


# ============================================================
# Flask routes
# ============================================================

@app.get("/api/health")
def api_health() -> Response:
    return jsonify({
        "ok": True,
        "i2c": bool(I2C_AVAILABLE),
        "i2c_error": I2C_BUS_ERROR,
        "mcp1": mcp_available["mcp1"],
        "mcp2": mcp_available["mcp2"],
        "ads": bool(ads_available),
        "mqtt": bool(MQTT_ENABLED and _MQTT_AVAILABLE and mqtt_client is not None),
    })

@app.get("/api/state")
def api_state() -> Response:
    with state_lock:
        return jsonify({"state": dict(state)})

@app.post("/api/set")
def api_set() -> Response:
    data = request.get_json(force=True, silent=True) or {}
    ch = str(data.get("channel", "")).strip()
    on = bool(data.get("state", False))

    if ch not in CHANNELS:
        return jsonify({"ok": False, "error": f"Unknown channel '{ch}'"}), 400

    try:
        # LR are special: "state": true => REMOTE, false => LOCAL
        if ch == "lr1":
            set_lr(1, remote=on)
            return jsonify({"ok": True, "channel": ch, "remote": on, "local": (not on)})
        if ch == "lr2":
            set_lr(2, remote=on)
            return jsonify({"ok": True, "channel": ch, "remote": on, "local": (not on)})

        set_channel(ch, on)
        return jsonify({"ok": True, "channel": ch, "state": on})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/all_off")
def api_all_off() -> Response:
    try:
        for b in BENCHES.keys():
            bench_kill_power(b)
        for p in (1, 2, 3, 4):
            usb_port_enable(p, data=False, vbus=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/usb")
def api_usb() -> Response:
    """{"port": 3, "data": true, "vbus": true}"""
    data = request.get_json(force=True, silent=True) or {}
    port = int(data.get("port", 0))
    data_en = bool(data.get("data", True))
    vbus_en = bool(data.get("vbus", True))

    try:
        usb_port_enable(port, data=data_en, vbus=vbus_en)
        return jsonify({"ok": True, "port": port, "data": data_en, "vbus": vbus_en})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/bench_service")
def api_bench_service() -> Response:
    """{"bench":"bench1","enable":true}"""
    data = request.get_json(force=True, silent=True) or {}
    bench = str(data.get("bench", "")).strip()
    enable = bool(data.get("enable", False))

    if bench not in BENCHES:
        return jsonify({"ok": False, "error": f"Unknown bench '{bench}'"}), 400

    try:
        if enable:
            bench_service_enable(bench)
        else:
            bench_service_disable(bench)
        return jsonify({"ok": True, "bench": bench, "enable": enable})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/air_pressure")
def api_air_pressure() -> Response:
    with pressure_lock:
        psi = float(current_psi)
    return jsonify({
        "ok": bool(ads_available),
        "psi": round(psi, 2),
        "mode": PRESSURE_WIRING_MODE,
        "max_psi": PRESSURE_SENSOR_MAX_PSI,
    })

@app.get("/")
def index() -> Response:
    try:
        return render_template("index.html")
    except Exception:
        return Response("Bench Controller running. UI template missing.", mimetype="text/plain")
@app.get("/api/log_tail")
def api_log_tail() -> Response:
    """
    Returns last N JSONL records across today's log file.
    Query: ?n=200
    """
    n = request.args.get("n", "200")
    try:
        n_int = max(1, min(2000, int(n)))
    except Exception:
        n_int = 200

    path = _log_path_for_today()
    if not os.path.exists(path):
        return jsonify({"ok": True, "path": path, "lines": []})

    # read last N lines efficiently-ish (logs are small; this is fine)
    try:
        with _log_lock:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        tail = lines[-n_int:]
        # parse JSON best-effort
        out = []
        for ln in tail:
            try:
                out.append(json.loads(ln))
            except Exception:
                out.append({"raw": ln})
        return jsonify({"ok": True, "path": path, "lines": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
@app.post("/api/bench/<int:bench_id>/service/start")
def bench_service_start(bench_id: int):
    if bench_id not in BENCH_TCP:
        return jsonify({"ok": False, "error": "Unknown bench"}), 404

    host = BENCH_TCP[bench_id]["host"]
    port = BENCH_TCP[bench_id]["port"]

    try:
        s = socket.create_connection((host, port), timeout=2.0)
        # Optional: send something simple if your Arduino expects it
        # s.sendall(b"PING\n")
        s.close()
        return jsonify({"ok": True, "bench": bench_id, "tcp": f"{host}:{port}"})
    except Exception as e:
        return jsonify({"ok": False, "bench": bench_id, "tcp": f"{host}:{port}", "error": str(e)}), 502
# ============================================================
# Socket.IO events (Terminal)
# ============================================================

@socketio.on("connect")
def _on_connect():
    # Session created on term_open
    pass

@socketio.on("disconnect")
def _on_disconnect():
    sid = request.sid
    with term_sessions_lock:
        sess = term_sessions.get(sid)
    if sess:
        _kill_session(sess)
        with term_sessions_lock:
            term_sessions.pop(sid, None)

@socketio.on("term_open")
def _on_term_open(data):
    sid = request.sid
    cols = int((data or {}).get("cols", 80))
    rows = int((data or {}).get("rows", 24))

    # kill old if any
    with term_sessions_lock:
        old = term_sessions.get(sid)
    if old:
        _kill_session(old)
        with term_sessions_lock:
            term_sessions.pop(sid, None)

    sess = _spawn_shell_for_sid(sid, cols=cols, rows=rows)
    with term_sessions_lock:
        term_sessions[sid] = sess

    socketio.emit("term_out", "\r\n[connected]\r\n", to=sid)

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # CSI sequences like ESC[?2004h, ESC[200~ etc.

def _sanitize_cmd(s: str) -> str:
    # Remove ANSI CSI sequences
    s = _ANSI_RE.sub("", s)
    # Remove any remaining ESC characters
    s = s.replace("\x1b", "")
    # Keep only printable chars + spaces (drop other control chars)
    s = "".join(ch for ch in s if (ch.isprintable() or ch == " "))
    return s.strip()

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

def _sanitize_cmd(s: str) -> str:
    s = _ANSI_RE.sub("", s)
    s = s.replace("\x1b", "")
    s = "".join(ch for ch in s if (ch.isprintable() or ch == " "))
    return s.strip()

@socketio.on("term_in")
def _on_term_in(payload):
    sid = request.sid
    if payload is None:
        return

    # Normalize payload
    if isinstance(payload, dict):
        text = payload.get("data") or payload.get("text") or payload.get("input") or ""
    elif isinstance(payload, (bytes, bytearray)):
        text = bytes(payload).decode("utf-8", errors="ignore")
    else:
        text = str(payload)

    if not text:
        return

    with term_sessions_lock:
        sess = term_sessions.get(sid)
    if not sess:
        return

    def _emit_logs(cmdline: str) -> None:
        parts = cmdline.split()
        n = 50
        if len(parts) > 1:
            try:
                n = max(1, min(500, int(parts[1])))
            except Exception:
                n = 50
        lines_out = _log_tail_lines(n)
        socketio.emit("term_out", "\r\n" + "\r\n".join(lines_out) + "\r\n", to=sid)

    for ch in text:
        # BACKSPACE
        if ch in ("\x7f", "\b"):
            with term_line_buffers_lock:
                buf = term_line_buffers.get(sid, "")
                term_line_buffers[sid] = buf[:-1] if buf else ""
            try:
                os.write(sess.master_fd, ch.encode("utf-8", errors="ignore"))
            except Exception:
                pass
            continue

        # ENTER
        if ch in ("\r", "\n"):
            with term_line_buffers_lock:
                raw_line = term_line_buffers.get(sid, "")
                term_line_buffers[sid] = ""

            cmd = _sanitize_cmd(raw_line)
            low = cmd.lower()

            # Intercept logs/log
            if low.startswith("logs") or low.startswith("log"):
                try:
                    os.write(sess.master_fd, b"\x15\r")  # Ctrl+U then CR
                except Exception:
                    pass
                _emit_logs(cmd)
                continue  # DO NOT forward enter to bash

            # Normal command: forward ENTER
            try:
                os.write(sess.master_fd, ch.encode("utf-8", errors="ignore"))
            except Exception:
                pass
            continue

        # NORMAL CHAR
        with term_line_buffers_lock:
            term_line_buffers[sid] = term_line_buffers.get(sid, "") + ch
        try:
            os.write(sess.master_fd, ch.encode("utf-8", errors="ignore"))
        except Exception:
            pass

@socketio.on("term_resize")
def _on_term_resize(data):
    sid = request.sid
    with term_sessions_lock:
        sess = term_sessions.get(sid)
    if not sess:
        return

    try:
        cols = int((data or {}).get("cols", 80))
        rows = int((data or {}).get("rows", 24))
        _pty_resize(sess.master_fd, cols, rows)
    except Exception as e:
        print(f"[TERM] resize failed: {e}")

# ============================================================
# Main Loop for App
# ============================================================

def main() -> None:
    _init_pins()
    _apply_boot_defaults()
    _mqtt_setup()
    _adc_init_ads1115()

    # pressure loop can be a stdlib thread; it’s fine (no socket i/o)
    threading.Thread(target=_pressure_loop, daemon=True).start()
    threading.Thread(target=_purge_loop, daemon=True).start()
    _oled_init()
    if _oled_ok:
            threading.Thread(target=_oled_scroll_loop, daemon=True).start()

    # IMPORTANT: must be socketio.run to serve /socket.io/*
    socketio.run(app, host=APP_HOST, port=APP_PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
