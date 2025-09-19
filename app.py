# -*- coding: utf-8 -*-
import os, time, threading, subprocess, shutil, glob, json, mimetypes
from datetime import datetime
from flask import Flask, request, redirect, url_for, send_file, abort, jsonify, render_template_string
import pytz
import subprocess
import io, zipfile
from os.path import basename

# ---------- Hotspot / AP control (NetworkManager) ----------
HOTSPOT_NAME = os.environ.get("HOTSPOT_NAME", "Pi-Hotspot")

def _nmcli(*args, timeout=6):
    """Run nmcli via sudo (allowed in sudoers). Return (ok, stdout)."""
    try:
        proc = subprocess.run(
            ["sudo", "/usr/bin/nmcli", *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False
        )
        ok = (proc.returncode == 0)
        return ok, (proc.stdout.strip() or proc.stderr.strip())
    except Exception as e:
        return False, str(e)
import re

def _ap_ssid(dev):
    # 1) From the connection profile (most reliable)
    ok, out = _nmcli("-g", "802-11-wireless.ssid", "con", "show", HOTSPOT_NAME)
    if ok and out.strip():
        return out.strip()

    # 2) From the live device (fallback)
    if dev:
        try:
            p = subprocess.run(
                ["iw", "dev", dev, "info"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, check=False
            )
            for ln in p.stdout.splitlines():
                ln = ln.strip()
                if ln.lower().startswith("ssid "):
                    return ln.split(None, 1)[1].strip()
        except Exception:
            pass
    return None

def _ap_active_device():
    """Return the device name (e.g. wlan0) for the active AP, or ''."""
    ok, out = _nmcli("con", "show", "--active")
    if not ok or not out:
        return ""
    # nmcli table lines typically have NAME, UUID, TYPE, DEVICE
    # Find the line that contains our HOTSPOT_NAME and extract the last column (device)
    for line in out.splitlines():
        if HOTSPOT_NAME in line:
            parts = line.split()
            if parts:
                return parts[-1]  # DEVICE col
    return ""

def _ipv4_for_device(dev):
    """Return the first IPv4 address for a given device, or ''."""
    if not dev:
        return ""
    try:
        proc = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", dev],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        )
        if proc.returncode == 0:
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout)
            return m.group(1) if m else ""
    except Exception:
        pass
    return ""

def _all_ipv4_local():
    """Return a list of local IPv4 addresses (best-effort)."""
    try:
        proc = subprocess.run(
            ["hostname", "-I"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=1.5
        )
        if proc.returncode == 0:
            return [ip for ip in proc.stdout.split() if "." in ip]
    except Exception:
        pass
    return []

def _ap_is_active():
    # Fast check: is our AP connection currently active?
    ok, out = _nmcli("con", "show", "--active")
    if not ok:
        return False
    # Look for HOTSPOT_NAME in active connections table
    return HOTSPOT_NAME in out
# ---- AP helpers wired to nmcli ----
def ap_is_on():
    try:
        return _ap_is_active()
    except Exception:
        return False

def _set_system_time(time_str):
    """Sets the system time from a string (e.g., ISO 8601 format)."""
    try:
        # The 'date' command is smart enough to parse ISO 8601 format directly
        subprocess.run(["sudo", "date", "-s", time_str], check=True)
    except Exception as e:
        print(f"Error setting system time: {e}", file=sys.stderr)
        # Re-raise the exception so the calling function knows it failed
        raise

# Allow override at runtime; default to Pi path
BASE = os.environ.get("TL_BASE", "/home/pi/timelapse")

def _ensure_dirs(base_root: str):
    """Ensure sessions and stills directories exist."""
    sess_path = os.path.join(base_root, "sessions")
    stills_path = os.path.join(base_root, "stills")
    try:
        os.makedirs(sess_path, exist_ok=True)
        os.makedirs(stills_path, exist_ok=True)
        return base_root, sess_path, stills_path
    except Exception:
        # Fallback to a local, user-writable directory
        fallback_root = os.path.abspath("./timelapse")
        fb_sess_path = os.path.join(fallback_root, "sessions")
        fb_stills_path = os.path.join(fallback_root, "stills")
        os.makedirs(fb_sess_path, exist_ok=True)
        os.makedirs(fb_stills_path, exist_ok=True)
        return fallback_root, fb_sess_path, fb_stills_path

BASE, SESSIONS_DIR, STILLS_DIR = _ensure_dirs(BASE)
STILLS_DIR = os.path.join(BASE, "stills")

CAMERA_STILL = shutil.which("rpicam-still") or "/usr/bin/rpicam-still"
FFMPEG       = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

# capture defaults
CAPTURE_INTERVAL_SEC = 10
CAPTURE_WIDTH   = "1296"
CAPTURE_HEIGHT  = "972"
CAPTURE_QUALITY = "90"

# encoding defaults / choices
FPS_CHOICES = [10, 24, 30]
DEFAULT_FPS = 24

# thumbnails (off by default; we serve full frames on index)
GENERATE_THUMBS = False
THUMB_WIDTH = 320  # only used if GENERATE_THUMBS = True

import atexit
def _cleanup_all():
    try: _stop_live_proc()
    except: pass
    try: stop_timelapse()
    except: pass
    _force_release_camera()
atexit.register(_cleanup_all)

# ---------- Globals ----------

# camera orientation (degrees). Set to 0, 90, 180, or 270
CAM_ROTATE_DEG = int(os.environ.get("CAM_ROTATE_DEG", "180"))

def _rot_flags_for(bin_path: str):
    """
    Return CLI flags to rotate frames for rpicam-* or libcamera-*.
    Uses --rotation <deg>, which is supported by both families.
    """
    try:
        deg = int(CAM_ROTATE_DEG)
    except Exception:
        deg = 0
    if deg not in (0, 90, 180, 270):
        deg = 0
    return ["--rotation", str(deg)] if deg else []

app = Flask(__name__)
app.jinja_env.globals.update(datetime=datetime)

# --- schedule_addon absolute-path import & init ---
# try:
#     import importlib.util
#     _sched_path = "/home/pi/timelapse/schedule_addon.py"
#     _spec = importlib.util.spec_from_file_location("schedule_addon", _sched_path)
#     schedule_addon = importlib.util.module_from_spec(_spec)
#     _spec.loader.exec_module(schedule_addon)
#     # register blueprint(s)
#     try:
#         schedule_addon.init(app)
#     except AttributeError:
#         pass
# except Exception as e:
#     print("[schedule] init failed:", e)

_last_frame_ts = 0
_stop_event = threading.Event()
_capture_thread = None
_current_session = None   # session name (string) while capturing
_jobs = {}                # encode job progress by session

_last_live_spawn = 0
def _can_spawn_live(period=1.0):
    global _last_live_spawn
    now = time.time()
    if now - _last_live_spawn < period:
        return False
    _last_live_spawn = now
    return True

# --- Encode queue/worker (back-pressure + low priority) ---
import queue
_encode_q = queue.Queue()
_action_q = queue.Queue()

def _start_encode_worker_once():
    if getattr(_start_encode_worker_once, "_started", False):
        return
    _start_encode_worker_once._started = True

    def _encode_worker():
        while True:
            task = _encode_q.get()
            if not task:
                _encode_q.task_done()
                continue
            sess, fps = task
            try:
                sess_dir = _session_path(sess)
                out = _video_path(sess_dir)
                frames = sorted(glob.glob(os.path.join(sess_dir, "*.jpg")))
                total_frames = len(frames)
                if total_frames == 0:
                    _jobs[sess] = {"status": "error", "progress": 0}
                    _encode_q.task_done()
                    continue

                _jobs[sess] = {"status": "encoding", "progress": 0}

                # be tolerant if ionice/nice not installed
                prio = []
                if shutil.which("ionice"): prio += ["ionice", "-c3"]
                if shutil.which("nice"):   prio += ["nice", "-n", "19"]

                # ...after computing sess_dir, out, fps...
                cmd = prio + [
                    FFMPEG, "-y",
                    "-framerate", str(fps),                          # input rate
                    "-pattern_type", "glob",                         # read by glob, not %d
                    "-i", os.path.join(sess_dir, "*.jpg"),
                    "-vf", f"scale={CAPTURE_WIDTH}:{CAPTURE_HEIGHT},fps={fps}",  # lock exact fps
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p",
                    "-r", str(fps),                                  # output rate (belt & braces)
                    out
                ]

                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                while True:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if "frame=" in line:
                        try:
                            parts = line.split("frame=")[-1].strip().split()
                            frame_num = int(parts[0])
                            prog = int((frame_num / max(1, total_frames)) * 99)
                            _jobs[sess]["progress"] = max(0, min(99, prog))
                        except Exception:
                            pass

                rc = proc.wait()
                if rc == 0 and os.path.exists(out):
                    _jobs[sess] = {"status": "done", "progress": 100}
                else:
                    _jobs[sess] = {"status": "error", "progress": 0}
            except Exception:
                _jobs[sess] = {"status": "error", "progress": 0}
            finally:
                _encode_q.task_done()
    threading.Thread(target=_encode_worker, daemon=True).start()

_start_encode_worker_once()

# ---- priming the live camera ----
_camera_warmed = False
WARMUP_MS = 1500  # short, just to init pipeline

def _warmup_camera():
    global _camera_warmed
    if _camera_warmed:
        return
    vid_bin = shutil.which("rpicam-vid") or shutil.which("libcamera-vid")
    if not vid_bin:
        return
    try:
        # quick run that discards output; just enough to init pipeline
        cmd = [vid_bin, "--codec", "mjpeg", "-t", str(WARMUP_MS), "-o", "-"]
        if os.path.basename(vid_bin) == "libcamera-vid":
            cmd.insert(1, "-n")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
        _camera_warmed = True
    except Exception:
        # harmless if it fails; we’ll fall back to retry below
        pass

# Globals for timed captures
_capture_stop_timer = None
_capture_end_ts     = None
_capture_start_ts   = None 
_active_schedule_id = None

# ---------- Session status API ----------
# This endpoint returns whether the session is active, the number of frames
# captured so far and the remaining time (if a duration was set).  The
# front-end can poll this to update the UI for the active session.
@app.get("/session_status/<sess>")
def session_status(sess):
    """Return JSON with number of frames and remaining seconds for a session."""
    active = (_current_session == sess)
    frames_count = 0
    try:
        sess_dir = _session_path(sess)
        if os.path.isdir(sess_dir):
            frames_count = len(glob.glob(os.path.join(sess_dir, "*.jpg")))
    except Exception:
        frames_count = 0
    remaining_sec = None
    try:
        if active and _capture_end_ts:
            rem = int(_capture_end_ts - time.time())
            if rem > 0:
                remaining_sec = rem
    except Exception:
        remaining_sec = None
    return jsonify({
        "active": active,
        "frames": frames_count,
        "remaining_sec": remaining_sec
    })

# ---------- Helpers ----------
def _session_path(name): return os.path.join(SESSIONS_DIR, name)
def _session_latest_jpg(sess_dir):
    files = sorted(glob.glob(os.path.join(sess_dir, "*.jpg")))
    return files[-1] if files else None
def _video_path(sess_dir): return os.path.join(sess_dir, "video.mp4")
def _safe_name(s): return "".join(c for c in s if c.isalnum() or c in ("-","_"))
def _timestamped_session():
    return "session-" + datetime.now().strftime("%Y%m%d-%H%M%S")

def _any_encoding_active():
    return any(v.get("status") in ("queued", "encoding") for v in _jobs.values())

def _cancel_schedule_locked(sid: str):
    """Assumes _sched_lock is held."""
    timers = _sched_timers.pop(sid, {})
    for t in timers.values():
        try:
            t and t.cancel()
        except Exception:
            pass
    _schedules.pop(sid, None)
    try:
        _save_sched_state()
    except Exception:
        pass

# ---- Live viewfinder ----
LIVE_PROC = None
LIVE_LOCK = threading.Lock()
from collections import deque
_live_last_stderr = deque(maxlen=120)

def _trace(msg: str):
    try:
        _live_last_stderr.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    except Exception:
        pass

def _force_release_camera():
    """
    Best-effort: kill any leftover processes that hold the camera.
    Safe to call right before spawning live preview.
    """
    names = ["rpicam-vid", "libcamera-vid", "rpicam-still", "libcamera-still"]
    for nm in names:
        try:
            subprocess.run(["pkill", "-TERM", "-x", nm], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    # Give the kernel a moment to release /dev/media*/video* nodes
    time.sleep(0.3)

def _idle_now():
    return (_capture_thread is None or not _capture_thread.is_alive()) and not _any_encoding_active()

def _list_sessions():
    out = []
    for d in sorted(os.listdir(SESSIONS_DIR)):
        sd = os.path.join(SESSIONS_DIR, d)
        if not os.path.isdir(sd): continue
        jpg = _session_latest_jpg(sd)
        vid = _video_path(sd)
        out.append({
            "name": d,
            "dir": sd,
            "has_frame": bool(jpg),
            "latest": os.path.basename(jpg) if jpg else "",
            "has_video": os.path.exists(vid),
            "video": os.path.basename(vid) if os.path.exists(vid) else "",
            "count": len(glob.glob(os.path.join(sd, "*.jpg")))
        })
    out.sort(key=lambda x: x["name"], reverse=True)
    return out

# ===== Simplified Scheduler =====
import uuid

_schedules = {}      # id -> dict (state)
SCHED_FILE = os.path.join(BASE, "schedule.json")
_sched_lock = threading.Lock()

def _save_sched_state():
    try:
        tmp = SCHED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_schedules, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, SCHED_FILE)
    except Exception:
        pass

def _load_sched_state():
    try:
        if os.path.exists(SCHED_FILE):
            with open(SCHED_FILE, "r") as f: data = json.load(f)
            if isinstance(data, dict):
                _schedules.clear()
                _schedules.update(data)
                return True
    except Exception:
        pass
    return False

def _scheduler_thread():
    """A single thread that wakes up periodically to manage all schedules."""
    time.sleep(10)

    while True:
        try:
            with _sched_lock:
                now = time.time()
                
                # --- Stop Logic (now using the tracking variable) ---
                if _current_session and _active_schedule_id:
                    active_schedule = _schedules.get(_active_schedule_id)
                    if active_schedule and active_schedule.get("end_ts", 0) <= now:
                        print(f"[scheduler] Active schedule '{_active_schedule_id}' has ended. Queueing STOP action.")
                        _action_q.put(('stop', {})) # Just send a simple stop command

                # --- Start Logic ---
                elif _idle_now():
                    # Find a schedule that should be active now
                    schedule_to_start = None
                    for sid, sched_data in _schedules.items():
                        if sched_data.get("start_ts", 0) <= now < sched_data.get("end_ts", 0):
                            schedule_to_start = sched_data
                            schedule_to_start['id'] = sid
                            break
                    
                    if schedule_to_start:
                        print(f"[scheduler] Schedule '{schedule_to_start['id']}' is active. Queueing START action.")
                        _action_q.put(('start', {'schedule': schedule_to_start}))
        
        except Exception as e:
            print(f"[scheduler] Error in scheduler thread: {e}")

        time.sleep(5)

# Start the single scheduler thread once
threading.Thread(target=_scheduler_thread, daemon=True).start()

def _action_processor_thread():
    """
    This thread is the ONLY place that starts or stops captures.
    It reads commands from _action_q to ensure all actions are serialized and safe.
    """
    # --- THIS IS THE FIX ---
    # All global variables that are assigned to in this function MUST be declared at the top.
    global _active_schedule_id, _capture_end_ts, _capture_stop_timer
    global _current_session, _capture_thread, _capture_start_ts
    # --- END OF FIX ---

    while True:
        action, payload = _action_q.get()

        if action == 'start':
            if not _idle_now():
                print("[processor] Ignoring START command, not idle.")
                continue

            print("[processor] Processing START command.")
            _stop_live_proc()
            
            if 'schedule' in payload:
                sched = _schedules.get(payload['schedule']['id'], {})
                interval = sched.get('interval', 10)
                sess_name = sched.get('sess', '')
                _active_schedule_id = payload['schedule']['id']
            else:
                interval = payload.get('interval', 10)
                sess_name = payload.get('name', '')
                _active_schedule_id = None
                if 'duration_min' in payload:
                    _capture_end_ts = time.time() + payload['duration_min'] * 60
                    _capture_stop_timer = threading.Timer(payload['duration_min'] * 60, stop_timelapse)
                    _capture_stop_timer.daemon = True
                    _capture_stop_timer.start()

            _current_session = _safe_name(sess_name or _timestamped_session())
            _stop_event.clear()
            _capture_start_ts = time.time()
            sess_dir = _session_path(_current_session)
            os.makedirs(sess_dir, exist_ok=True)
            _capture_thread = threading.Thread(target=_capture_loop, args=(sess_dir, interval), daemon=True)
            _capture_thread.start()
            
        elif action == 'stop':
            session_to_stop = _current_session
            schedule_that_stopped = _schedules.get(_active_schedule_id) if _active_schedule_id else None
            
            stop_timelapse()
            _active_schedule_id = None

            if schedule_that_stopped and schedule_that_stopped.get('auto_encode') and session_to_stop:
                time.sleep(1.5)
                fps = schedule_that_stopped.get('fps', 24)
                print(f"[processor] Auto-encoding session {session_to_stop} at {fps}fps")
                _encode_q.put((session_to_stop, fps))


# Start the action processor thread once
threading.Thread(target=_action_processor_thread, daemon=True).start()


# ---------- Capture thread ----------
def _capture_loop(sess_dir, interval):
    global _stop_event, _last_frame_ts
    
    # Use rpicam-still's built-in timelapse mode. It's far more efficient.
    jpg_pattern = os.path.join(sess_dir, "%06d.jpg")
    total_run_time_ms = 24 * 3600 * 1000 # 24 hours in ms

    cmd = [
        CAMERA_STILL, *(_rot_flags_for(CAMERA_STILL)),
        "-o", jpg_pattern,
        "--width", CAPTURE_WIDTH, "--height", CAPTURE_HEIGHT,
        "--quality", CAPTURE_QUALITY,
        "--nopreview",
        # "--immediate",  # Added this flag back from your original code
        "--timelapse", str(int(interval * 1000)),
        "-t", str(total_run_time_ms)
    ]

    proc = None
    # Save the log inside the specific session's directory
    log_path = os.path.join(sess_dir, "capture.log")

    try:
        # Open a log file to capture errors from the camera process
        with open(log_path, "w") as log_file:
            log_file.write(f"Starting capture at {datetime.now()}\n")
            log_file.write(f"Command: {' '.join(cmd)}\n\n")
            log_file.flush()

            # Start the camera process, redirecting stderr to our log file
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)

            # Wait for the stop event, polling the process to ensure it's still running.
            while not _stop_event.wait(timeout=1.0):
                if proc.poll() is not None:
                    # Process exited unexpectedly. The error should be in the log.
                    log_file.write(f"\nProcess exited unexpectedly with code: {proc.returncode}\n")
                    break
    
    except Exception as e:
        # Log any Python-level errors that occur before or during Popen
        with open(log_path, "a") as log_file:
            log_file.write(f"\nAn error occurred in the capture thread: {e}\n")

    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        
        with open(log_path, "a") as log_file:
            log_file.write(f"Capture loop finished at {datetime.now()}\n")

# ---------- Stop helper (idempotent) ----------
# def stop_timelapse():
#     global _stop_event, _capture_thread, _current_session
#     global _capture_stop_timer, _capture_end_ts
#     try:
#         _stop_event.set()
#     except Exception: pass
#     if _capture_thread and getattr(_capture_thread, "is_alive", lambda: False)():
#         try: _capture_thread.join(timeout=5)
#         except Exception: pass
#     _capture_thread = None
#     _stop_event = threading.Event()
#     _current_session = None
#     if _capture_stop_timer:
#         try:
#             _capture_stop_timer.cancel()
#         except Exception:
#             pass
#     _capture_stop_timer = None
#     _capture_end_ts = None

# ---------- Routes ----------

@app.post("/sync_time")
def sync_time_route():
    """Receives a timestamp from the client and updates the system time."""
    data = request.json
    client_iso_time = data.get('time') if data else None
    
    if not client_iso_time:
        return jsonify({"error": "Missing 'time' in request body"}), 400

    try:
        # First, set the system time using the string from the browser
        _set_system_time(client_iso_time)
        
        # Now, restart the LCD service to show the new time
        subprocess.run(["sudo", "systemctl", "restart", "timelapse-lcd.service"], check=True)
        
    except Exception as e:
        print(f"Error during time sync process: {e}")
        return jsonify({"error": "Failed during sync process", "message": str(e)}), 500

    return ("", 204) # Return an empty success response

def ap_enable():
    """Enables the Wi-Fi radio and brings the hotspot connection up."""
    # Returns a tuple: (bool: success, str: message)
    _nmcli("radio", "wifi", "on")
    ok, out = _nmcli("con", "up", HOTSPOT_NAME)
    return ok, out

def ap_disable():
    """Brings the hotspot connection down."""
    # Returns a tuple: (bool: success, str: message)
    ok, out = _nmcli("con", "down", HOTSPOT_NAME)
    if ok:
        return True, out
    
    # It's okay if the connection was already down. Treat that as success.
    txt = (out or "").lower()
    if ("not active" in txt) or ("unknown connection" in txt):
        return True, out
    
    return False, out

@app.post("/ap/toggle")
def ap_toggle():
    """Toggles the hotspot on or off and returns a detailed JSON response."""
    if ap_is_on():
        ok, message = ap_disable()
    else:
        ok, message = ap_enable()
    
    if ok:
        return jsonify({"on": ap_is_on(), "message": "Success"})
    else:
        # If it fails, return the actual error message from nmcli
        return jsonify({"on": ap_is_on(), "error": "toggle_failed", "message": message}), 500

@app.post("/ap/on")
def ap_on():
    ok = ap_enable()
    return (jsonify({"on": True}) if ok else (jsonify({"on": False, "error":"enable_failed"}), 500))

@app.post("/ap/off")
def ap_off():
    ok = ap_disable()
    return (jsonify({"on": False}) if ok else (jsonify({"on": True, "error":"disable_failed"}), 500))

def ap_toggle():
    # The ap_enable/disable functions now need to return the output message on failure
    if ap_is_on():
        ok, out = ap_disable()
    else:
        ok, out = ap_enable()
    
    if ok:
        return jsonify({"on": ap_is_on()})
    else:
        # Return the actual error message from nmcli
        return jsonify({"on": ap_is_on(), "error": "toggle_failed", "message": out}), 500

@app.get("/ap/status")
def ap_status_json():
    on = ap_is_on()
    dev = _ap_active_device() if on else ""
    ip  = _ipv4_for_device(dev) if dev else ""
    ssid = _ap_ssid(dev) if on else None
    return jsonify({
        "on": on,
        "name": HOTSPOT_NAME,
        "device": dev,
        "ip": ip,                # single best IP for the AP interface
        "ips": _all_ipv4_local(), # all local IPv4s (fallback/diagnostic)
        "ssid": ssid,            # SSID of the AP (if active)
    })

def _ap_status_quick():
    """Return a compact AP status dict for the template."""
    try:
        on = ap_is_on()
    except Exception:
        on = False
    dev = _ap_active_device() if on else ""
    try:
        ssid = _ap_ssid(dev) if on else None
    except Exception:
        ssid = None
    return {"on": on, "ssid": ssid}

@app.route("/", methods=["GET"])
def index():
    # flags for live view
    encoding_active = _any_encoding_active()
    idle_now = ((_capture_thread is None or not _capture_thread.is_alive()) and not encoding_active)

    sessions = _list_sessions()

    # compute remaining seconds for active session if a duration was set
    remaining_sec = None
    try:
        if _current_session and _capture_end_ts:
            rem = int(_capture_end_ts - time.time())
            if rem > 0:
                remaining_sec = rem
    except Exception:
        remaining_sec = None

    # compute minutes and seconds for display
    remaining_min = None
    remaining_sec_only = None
    remaining_sec_padded = None
    if remaining_sec:
        remaining_min = remaining_sec // 60
        remaining_sec_only = remaining_sec % 60
        remaining_sec_padded = f"{remaining_sec_only:02d}"

    # Next schedule card (from multiple schedules)
    try:
        next_sched = _get_next_schedule()
    except Exception:
        next_sched = None

    disk_info = _disk_stats()

    return render_template_string(
        TPL_INDEX,
        sessions=sessions,
        current_session=_current_session,
        fps_choices=FPS_CHOICES,
        default_fps=DEFAULT_FPS,
        interval_default=CAPTURE_INTERVAL_SEC,
        remaining_sec=remaining_sec,
        remaining_min=remaining_min,
        remaining_sec_only=remaining_sec_only,
        remaining_sec_padded=remaining_sec_padded,
        next_sched=next_sched,
        disk=disk_info,
        # NEW flags used by the template for live view
        encoding_active=encoding_active,
        idle_now=idle_now,
        ap_status=_ap_status_quick(),
    )

@app.route("/start", methods=["POST"])
def start():
    # This route now just puts a start command on the queue for the processor thread.
    if not _idle_now():
        return redirect(url_for("index"))

    interval = int(request.form.get("interval", str(CAPTURE_INTERVAL_SEC)))
    name = _safe_name(request.form.get("name") or "")
    
    hr_str = request.form.get("duration_hours", "0") or "0"
    mn_str = request.form.get("duration_minutes", "0") or "0"
    duration_min = int(hr_str) * 60 + int(mn_str)

    payload = {'interval': interval, 'name': name}
    if duration_min > 0:
        payload['duration_min'] = duration_min
    
    _action_q.put(('start', payload))
    
    # Redirect immediately. The UI will update via polling.
    return redirect(url_for("index"))

def _stop_live_proc():
    global LIVE_PROC
    with LIVE_LOCK:
        if LIVE_PROC and LIVE_PROC.poll() is None:
            try:
                LIVE_PROC.terminate()
            except Exception:
                pass
        LIVE_PROC = None


def stop_timelapse():
    global _stop_event, _capture_thread, _current_session
    global _capture_stop_timer, _capture_end_ts, _capture_start_ts

    # Signal the capture thread to stop
    _stop_event.set()

    # Clean up the auto-stop timer if it exists
    if _capture_stop_timer:
        try:
            _capture_stop_timer.cancel()
        except Exception:
            pass
        _capture_stop_timer = None

    # Add a check to ensure the thread exists before trying to join it
    if _capture_thread and _capture_thread.is_alive():
        _capture_thread.join(timeout=5.0)

    # Reset state
    _capture_thread = None
    _current_session = None
    _capture_end_ts = None
    _capture_start_ts = None
    _stop_event = threading.Event()

@app.route("/stop", methods=["GET","POST"], endpoint="stop_route")
def stop_route():
    stop_timelapse()
    return ("", 204)

@app.post("/shutdown")
def shutdown_device():
    """Safely shuts down the Raspberry Pi."""
    try:
        print("[SHUTDOWN] Shutdown command received. Shutting down now.")
        # We use subprocess.run and don't wait for it to complete,
        # as the system will be shutting down.
        subprocess.Popen(["sudo", "/sbin/shutdown", "-h", "now"])
        return jsonify({"message": "Shutdown initiated."}), 202
    except Exception as e:
        print(f"[SHUTDOWN] Error initiating shutdown: {e}")
        return jsonify({"error": "Failed to initiate shutdown", "message": str(e)}), 500

@app.post("/capture_still")
def capture_still():
    """Captures a single high-quality photo and saves it."""
    if not _idle_now():
        return jsonify({"error": "Camera is busy"}), 503

    try:
        # Create a unique filename based on the current timestamp
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"still-{ts}.jpg"
        path = os.path.join(STILLS_DIR, filename)

        # Build and run the capture command
        cmd = [
            CAMERA_STILL, *(_rot_flags_for(CAMERA_STILL)), "-o", path,
            "--width", CAPTURE_WIDTH, "--height", CAPTURE_HEIGHT,
            "--quality", CAPTURE_QUALITY, "--nopreview", "--immediate"
        ]
        subprocess.run(cmd, check=True, timeout=10)

        # Return the captured image directly for the LCD to display
        return send_file(path, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({"error": "Failed to capture still", "message": str(e)}), 500

@app.post("/take_web_still")
def take_web_still():
    """Captures a single photo from the web UI and redirects to a preview page."""
    if not _idle_now():
        return redirect(url_for("index")) # Don't do anything if not idle
    
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"still-{ts}.jpg"
        path = os.path.join(STILLS_DIR, filename)

        cmd = [
            CAMERA_STILL, *(_rot_flags_for(CAMERA_STILL)), "-o", path,
            "--width", CAPTURE_WIDTH, "--height", CAPTURE_HEIGHT,
            "--quality", CAPTURE_QUALITY, "--nopreview", "--immediate"
        ]
        subprocess.run(cmd, check=True, timeout=10)
        
        # Redirect to the new preview page for this image
        return redirect(url_for("still_preview", filename=filename))
    except Exception as e:
        print(f"Error capturing web still: {e}")
        return redirect(url_for("index")) # Redirect home on error

@app.get("/still_preview/<filename>")
def still_preview(filename):
    """Displays a single captured still with options."""
    return render_template_string(TPL_STILL_PREVIEW, filename=filename)

@app.get("/stills")
def stills_gallery():
    """Displays a gallery of all captured stills."""
    try:
        files = sorted(
            [f for f in os.listdir(STILLS_DIR) if f.lower().endswith('.jpg')],
            reverse=True
        )
    except FileNotFoundError:
        files = []
    return render_template_string(TPL_STILLS, stills=files)

@app.get("/stills_api")
def stills_api():
    """Returns a JSON list of still image filenames."""
    try:
        files = sorted(
            [f for f in os.listdir(STILLS_DIR) if f.lower().endswith('.jpg')],
            reverse=True
        )
        return jsonify(files)
    except Exception as e:
        return jsonify({"error": str(e), "files": []}), 500

@app.get("/stills/<filename>")
def serve_still(filename):
    """Serves a single still image file."""
    # Basic security: prevent directory traversal attacks
    if ".." in filename or "/" in filename:
        abort(400)
    return send_file(os.path.join(STILLS_DIR, filename), mimetype='image/jpeg')

@app.post("/stills/delete/<filename>")
def delete_still(filename):
    """Deletes a still image."""
    if ".." in filename or "/" in filename:
        abort(400)
    try:
        os.remove(os.path.join(STILLS_DIR, filename))
    except Exception as e:
        print(f"Error deleting still {filename}: {e}")
    return redirect(url_for("stills_gallery"))

@app.post("/delete_all_stills")
def delete_all_stills():
    """Deletes all still images from the gallery."""
    try:
        # Iterate over all files in the stills directory and remove them
        for filename in os.listdir(STILLS_DIR):
            file_path = os.path.join(STILLS_DIR, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        print("All still images have been deleted.")
    except Exception as e:
        print(f"Error deleting all stills: {e}")
    # Redirect back to the now-empty gallery page
    return redirect(url_for("stills_gallery"))

@app.get("/download_stills_zip")
def download_stills_zip():
    """Creates a ZIP archive of all stills and sends it for download."""
    stills_path = STILLS_DIR # Using the global variable
    try:
        image_files = [f for f in os.listdir(stills_path) if f.lower().endswith('.jpg')]
        if not image_files:
            abort(404, "No still images found to download.")

        # Create an in-memory binary stream
        memory_file = io.BytesIO()

        # Create a ZIP file in the memory stream
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename in image_files:
                file_path = os.path.join(stills_path, filename)
                # Add file to the zip archive. The second argument avoids creating a folder structure inside the zip.
                zf.write(file_path, basename(file_path))
        
        # Move the stream's cursor to the beginning before sending
        memory_file.seek(0)
        
        return send_file(
            memory_file,
            mimetype='application/zip',
            as_attachment=True,
            download_name='timelapse-stills.zip'
        )
    except Exception as e:
        print(f"Error creating ZIP file: {e}")
        abort(500, "Failed to create ZIP file.")

@app.post("/delete_past_schedules")
def delete_past_schedules():
    """Finds and deletes all schedules that have an end time in the past."""
    with _sched_lock:
        now_ts = int(time.time())
        # Create a list of keys (schedule IDs) to delete
        ids_to_delete = [
            sid for sid, data in _schedules.items()
            if int(data.get("end_ts", 0)) < now_ts
        ]
        
        # Delete the identified schedules
        for sid in ids_to_delete:
            _schedules.pop(sid, None)
        
        _save_sched_state()
        
    return redirect(url_for("schedule_page"))

@app.post("/rename/<sess>")
def rename(sess):
    # Disable rename for active session
    if _current_session == sess:
        return redirect(url_for("index"))
    new = _safe_name(request.form.get("new_name","").strip())
    if not new: return redirect(url_for("index"))
    oldp = _session_path(sess)
    newp = _session_path(new)
    try:
        if os.path.isdir(oldp) and not os.path.exists(newp):
            os.rename(oldp, newp)
    except Exception:
        pass
    return redirect(url_for("index"))

@app.post("/delete/<sess>")
def delete(sess):
    # Block deleting the session being encoded
    if _jobs.get(sess, {}).get("status") in ("queued","encoding"):
        return redirect(url_for("index"))
    if _current_session == sess:
        return redirect(url_for("index"))
    p = _session_path(sess)
    try:
        if os.path.isdir(p):
            shutil.rmtree(p)
    except Exception:
        pass
    return redirect(url_for("index"))

@app.get("/session/<sess>/preview")
def preview(sess):
    p = _session_path(sess)
    if not os.path.isdir(p): abort(404)
    jpg = _session_latest_jpg(p)
    if not jpg:
        # tiny 1x1 gif
        data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x01\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        resp = app.response_class(data, mimetype="image/gif")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    if GENERATE_THUMBS:
        tpath = _thumb_for(p, jpg)
        path_to_send = tpath if os.path.exists(tpath) else jpg
    else:
        path_to_send = jpg
    resp = send_file(path_to_send, conditional=False)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.post("/encode/<sess>")
def encode(sess):
    if _any_encoding_active():                    # don't queue more jobs
        _jobs[sess] = {"status":"error","progress":0,"reason":"busy"}
        return redirect(url_for("index"))
    fps = request.form.get("fps", str(DEFAULT_FPS))
    try: fps = int(fps)
    except: fps = DEFAULT_FPS
    if fps not in FPS_CHOICES: fps = DEFAULT_FPS

    sess_dir = _session_path(sess)
    if not os.path.isdir(sess_dir): abort(404)
    if not _enough_space(300):
        _jobs[sess] = {"status":"error","progress":0,"reason":"low_disk"}
        return redirect(url_for("index"))

    # queue the job and return immediately; UI will poll /jobs
    _jobs[sess] = {"status":"queued","progress":0}
    _encode_q.put((sess, fps))
    return redirect(url_for("index"))


@app.post("/schedule/cancel")
def schedule_cancel_compat():
    with _sched_lock:
        _schedules.clear()  # Simply clear the entire dictionary
        _save_sched_state()
    return redirect(url_for("schedule_page"))

@app.get("/jobs")
def jobs():
    # remove finished jobs older than a minute to avoid stale bars
    for k,v in list(_jobs.items()):
        if v.get("status") in ("done","error"):
            # keep for a short grace; here we just leave it and let UI hide it
            pass
    return jsonify(_jobs)

@app.get("/download/<sess>")
def download(sess):
    p = _video_path(_session_path(sess))
    if not os.path.exists(p): abort(404)
    return send_file(p, as_attachment=True, download_name=f"{sess}.mp4")

# In app.py, replace the existing lcd_status function with this one.

@app.get("/lcd_status")
def lcd_status():
    try:
        active = bool(_current_session)
        frames = 0
        start_ts = None
        end_ts = None

        if active:
            # If a capture is active, get the frame count
            sd = _session_path(_current_session)
            if os.path.isdir(sd):
                frames = len(glob.glob(os.path.join(sd, "*.jpg")))

            # The actual start time is always recorded in _capture_start_ts
            start_ts = _capture_start_ts

            # Now determine the end time. Check for an active schedule first.
            nxt = _get_next_schedule()
            if nxt and nxt.get("active_now"):
                # It's a scheduled capture, get the end time from the schedule
                sched_data = _schedules.get(nxt["id"], {})
                end_ts = sched_data.get("end_ts")
            else:
                # It must be a manual capture, get the end time from the timer
                end_ts = _capture_end_ts
        
        # Get next schedule info for the main screen, regardless of active state
        next_sched_info = _get_next_schedule()

        return jsonify({
            "active": active,
            "session": _current_session or "",
            "frames": frames,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "encoding": _any_encoding_active(),
            "disk": _disk_stats(),
            "next_sched": next_sched_info,
            "live_idle": _idle_now(),
        })
    except Exception:
        # never crash the LCD
        return jsonify({
            "active": False, "session": "", "frames": 0,
            "start_ts": None, "end_ts": None, "encoding": False,
            "disk": _disk_stats(), "next_sched": None, "live_idle": True
        })

@app.get("/test_capture")
def test_capture():
    """Capture a single still and return it as JPEG."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    cmd = [
        CAMERA_STILL, *(_rot_flags_for(CAMERA_STILL)), "-o", path,
        "--width", CAPTURE_WIDTH, "--height", CAPTURE_HEIGHT,
        "--quality", CAPTURE_QUALITY,
        "--immediate", "--nopreview"
    ]
    try:
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return send_file(path, mimetype="image/jpeg")
    except Exception:
        abort(500)
    finally:
        try: os.unlink(path)
        except Exception:
            pass

@app.get("/live_status")
def live_status():
    try:
        return jsonify({"idle": _idle_now()})
    except Exception:
        return jsonify({"idle": False})
@app.get("/live_debug")
def live_debug():
    vid_bin = shutil.which("rpicam-vid") or shutil.which("libcamera-vid")
    proc = None
    with LIVE_LOCK:
        proc = LIVE_PROC

    return jsonify({
        "camera_warmed": _camera_warmed,
        "idle_now": _idle_now(),
        "live_proc": {
            "exists": LIVE_PROC is not None,
            "running": (LIVE_PROC and LIVE_PROC.poll() is None) or False,
            "returncode": (None if not LIVE_PROC else LIVE_PROC.returncode),
        },
        "stderr_tail": list(_live_last_stderr)[-40:],  # last ~40 lines
    })        

@app.get("/live.mjpg")
def live_mjpg():
    global LIVE_PROC
    
    _trace("ENTER /live.mjpg")

    if not _idle_now():
        abort(503, "Busy")

    vid_bin = shutil.which("libcamera-vid") or shutil.which("rpicam-vid")
    if not vid_bin:
        abort(500, "No camera video binary found (libcamera-vid/rpicam-vid).")

    with LIVE_LOCK:
        # If the process doesn't exist or has exited, start a new one.
        if LIVE_PROC is None or LIVE_PROC.poll() is not None:
            _force_release_camera() # Clean up any stale processes first

            def build_cmd(w, h):
                base = os.path.basename(vid_bin)
                rot = _rot_flags_for(vid_bin)
                if base.startswith("libcamera-"):
                    return [
                        vid_bin, "-n", "--codec", "mjpeg", "--width", str(w), "--height", str(h),
                        "--framerate", "30", *rot, "-t", "0", "-o", "-",
                    ]
                else:
                    return [
                        vid_bin, "--nopreview", "--codec", "mjpeg", "--width", str(w), "--height", str(h),
                        "--framerate", "30", *rot, "-t", "0", "-o", "-",
                    ]
            
            cmd = build_cmd(CAPTURE_WIDTH, CAPTURE_HEIGHT)
            env = dict(os.environ)
            env.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
            
            _trace("SPAWN single shared camera proc")
            if not _can_spawn_live(): abort(429)

            LIVE_PROC = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, env=env
            )
            _trace(f"SPAWNED pid={LIVE_PROC.pid}")

            # Start a thread to drain stderr to prevent the process from blocking
            def _drain_stderr(p):
                try:
                    for line in iter(p.stderr.readline, b''):
                        if line: _live_last_stderr.append(line.decode('utf-8', 'ignore').strip())
                except Exception: pass
            
            threading.Thread(target=_drain_stderr, args=(LIVE_PROC,), daemon=True).start()

    def gen():
        _trace("GEN start")
        # This generator reads from the single shared LIVE_PROC stdout
        try:
            while True:
                if not _idle_now() or LIVE_PROC.poll() is not None:
                    break
                
                # Find JPEG start and end markers (SOI, EOI)
                # This is more robust for streaming MJPEG
                soi = LIVE_PROC.stdout.read(2)
                if soi != b'\xff\xd8':
                    continue
                
                # Find the EOI marker
                # Read chunks until we find the EOI
                jpeg_data = soi
                while True:
                    chunk = LIVE_PROC.stdout.read(1024)
                    jpeg_data += chunk
                    eoi_pos = jpeg_data.find(b'\xff\xd9')
                    if eoi_pos != -1:
                        frame = jpeg_data[:eoi_pos+2]
                        # Any data after EOI is part of the next frame, but we discard it
                        # and resync to the next SOI for simplicity.
                        break
                
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                       + frame + b"\r\n")
        finally:
            _trace("GEN cleanup - client disconnected")
            # Note: We DO NOT kill the process here.
            # It stays alive for other clients. It will be killed by _stop_live_proc()
            # when a timelapse starts or via the /live_kill endpoint.

    headers = {
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store",
    }
    return app.response_class(gen(), headers=headers)


@app.get("/live_diag")
def live_diag():
    """
    Non-invasive diagnostics for the /live page.
    - If the preview process is running, DO NOT open the camera again.
      Just report that it's already running.
    - Only when no preview process is running do we try a short probe.
    """
    # Prefer rpicam-vid, then libcamera-vid
    vid_bin = shutil.which("rpicam-vid") or shutil.which("libcamera-vid")

    with LIVE_LOCK:
        running = bool(LIVE_PROC and LIVE_PROC.poll() is None)

    # If preview already running, don't grab the camera again.
    if running:
        return jsonify({"probe": {
            "ok": False,
            "bin": vid_bin,
            "reason": "Preview already running — still connecting.",
            "noninvasive": True
        }})

    # No preview running — do a quick, tolerant probe
    if not vid_bin:
        return jsonify({"probe": {"ok": False, "bin": None,
                                  "reason": "No rpicam-vid/libcamera-vid installed"}})

    try:
        # Keep it short and quiet; add -n for libcamera-vid
        cmd = [vid_bin, "--codec", "mjpeg", "-t", "200", "-o", "-"]
        if os.path.basename(vid_bin) == "libcamera-vid":
            cmd.insert(1, "-n")
        p = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, timeout=2.5
        )
        ok = (p.returncode == 0)
        err = (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return jsonify({"probe": {"ok": False, "bin": vid_bin,
                                  "reason": "Camera init exceeded 2.5s (slow startup)."}})
    except Exception as e:
        return jsonify({"probe": {"ok": False, "bin": vid_bin, "reason": str(e)}})

    # Return last line as a compact reason
    reason = ""
    if err:
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        if lines:
            reason = lines[-1]
    return jsonify({"probe": {"ok": ok, "bin": vid_bin, "reason": reason, "noninvasive": False}})

@app.route("/live_kill", methods=["GET","POST"])
def live_kill():
    _stop_live_proc()
    _force_release_camera()   # from the previous step I gave you
    return ("", 204)

@app.get("/live")
def live_page():
    return render_template_string(r"""
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Live View</title>
<style>
  body{margin:0;background:#0b0b0b;color:#e5e7eb;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;gap:10px;align-items:center;padding:10px;border-bottom:1px solid #222}
  main{padding:10px}
  .wrap{position:relative;max-width:1024px;margin:0 auto;background:#000;border-radius:8px;overflow:hidden}
  #live-img{width:100%;height:auto;display:block}
  a.btn{color:#111827;background:#f3f4f6;border:1px solid #374151;border-radius:8px;padding:8px 12px;text-decoration:none}
  .msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.35);font-size:14px}
  .msg.hidden{display:none}
</style>
<header>
  <a class="btn" href="{{ url_for('index') }}">← Back</a>
  <h1 style="margin:0;font-size:16px">Viewfinder 📷</h1>
</header>
<main>
  <div class="wrap">
    <div id="msg" class="msg">Loading camera…</div>
    <!-- Note: no src initially; we set it from JS after the page is shown -->
    <img id="live-img" alt="live view" decoding="async">
  </div>
</main>
<script>
  const img = document.getElementById('live-img');
  const msg = document.getElementById('msg');
  const STREAM_URL = "{{ url_for('live_mjpg') }}";

  // Hide message once a frame has definitely arrived
  function hideWhenReady() {
    if (img.naturalWidth > 0) {
      msg.classList.add('hidden');
      return true;
    }
    return false;
  }

  // 1) Try the simple case: when the first frame decodes, many browsers fire 'load' once.
  img.addEventListener('load', hideWhenReady);

  // 2) Safety net: poll naturalWidth in case the 'load' event doesn't fire for MJPEG.
  let tries = 0;
  (function poll() {
    if (hideWhenReady()) return;
    if (tries++ < 150) setTimeout(poll, 100); // up to ~15s
  })();

  // 3) Error path
  img.addEventListener('error', () => {
    msg.textContent = 'Could not connect to camera stream.';
    msg.classList.remove('hidden');
  });

  // Defer starting the stream until after the page is painted.
  // This avoids any “hang” on the previous page and ensures the overlay is visible first.
  requestAnimationFrame(() => {
    setTimeout(() => {
      img.src = STREAM_URL + "?t=" + Date.now();
    }, 50);
  });
</script>
    """)

# ---------- Template (single file) ----------
TPL_INDEX = r"""
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Pi Timelapse</title>
<style>
  :root {
    --active-bg: #e6f2ff; /* light blue */
    --card-bg: #ffffff;
    --border: #e5e7eb;
    --text: #111827;
    --muted: #6b7280;
    --btn: #f3f4f6;
    --btn-text: #111827;
    --btn-strong-bg: #47b870;
    --btn-strong-text: #ffffff;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background:#f8fafc; color: var(--text);}
  header { position: sticky; top: 0; background:#fff; border-bottom:1px solid var(--border); padding: 10px 12px; display:flex; gap:10px; align-items:center; }
  header h1 { margin: 0; font-size: 18px; }
  main { padding: 12px; max-width: 820px; margin: 0 auto; }

  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; padding: 5px 0px}
  .card {
    background: var(--card-bg);
    border:1px solid var(--border); border-radius:12px;
    padding: 12px; margin-bottom: 12px;
    box-shadow: 0 1px 2px rgba(0,0,0,.04);
  }
  .card.active { background: var(--active-bg); }

  .controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }

  button, .btn {
    border:1px solid var(--border); background: var(--btn); color: var(--btn-text);
    border-radius:10px; padding:10px 12px; font-size:16px; line-height:1; text-decoration:none; display:inline-flex; align-items:center; gap:8px;
  }
  .btn-strong { background: var(--btn-strong-bg); color: var(--btn-strong-text); border-color: transparent;}
  button:disabled { opacity:.5; }

  a.btn.disabled {
  pointer-events: none;
  opacity: 0.5;
  }

  label { font-size:14px; color: var(--muted); margin-right:6px; }
  input[type=number], input[type=text], select { font-size:16px; padding:8px 10px; border:1px solid var(--border); border-radius:8px; }

  .session { display:grid; grid-template-columns: 120px 1fr; gap:10px; align-items:start; }
  .thumb {
    width: 120px; height: 90px; border:1px solid var(--border); border-radius:8px; background:#f1f5f9;
    display:flex; align-items:center; justify-content:center; overflow:hidden;
  }
  .thumb img { width:100%; height:100%; object-fit:cover; display:block; }
  .thumb .placeholder { font-size:13px; color:var(--muted); text-align:center; padding:8px; }
  .meta { display:flex; flex-direction:column; gap:8px; }
  .meta .name { font-weight:600; font-size:16px; }
  .meta .sub { color: var(--muted); font-size:13px; }

  .progress { position:relative; height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; display:none; }
  .progress.show { display:block; }
  .bar { position:absolute; left:0; top:0; bottom:0; width:0%; background:#10b981; }
  
  .is-disabled {
    pointer-events: none;
    opacity: .5;
  }

  .footer {
  position: sticky; bottom: 0; background:#fff;
  border-top:1px solid var(--border); padding:10px 12px;
  }
  .footer .row { align-items:center; justify-content:space-between; }
  .diskbar {
    height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; width:300px;
  }
  .diskbar .fill {
    height:100%; width:0%;
    background:#5ca5d6; /* light blue */
  }
  .footer .label { color: var(--muted); font-size: 13px; }
  .diskbar .fill { height:100%; width:0%; transition:width .25s ease; }
  .diskbar .fill.ok   { background:#5ca5d6; } /* light blue */
  .diskbar .fill.warn { background:#f59e0b; } /* amber */
  .diskbar .fill.crit { background:#ef4444; } /* red */

  @media (max-width: 460px) {
    .session { grid-template-columns: 104px 1fr; }
    .thumb { width: 104px; height: 78px; }
    header h1 { font-size: 16px; }
  }
</style>

<header>
  <h1>📸 Pi Timelapse - Mouse Eye 🐭 </h1>
</header>

<main>
{% set ap = ap_status %}
    <div class="card" id="ap-indicator">
      <div class="row" style="justify-content: space-between;">
        <span>
          {% if ap.on %}
            <span style="color: green;">📶 Hotspot ON (SSID: {{ ap.ssid }})</span>
          {% else %}
            <span style="color: grey;">📡 Hotspot OFF</span>
          {% endif %}
        </span>
        <span>
            <button class="btn" onclick="toggleSettings()">⚙️ Settings</button>
        </span>
      </div>

      <div id="settings-panel" style="display: none; margin-top: 15px; border-top: 1px solid #eee; padding-top: 15px;">
        <div class="row">
          {% if ap.on %}
            <form action="{{ url_for('ap_toggle') }}" method="post" style="display:inline;">
              <button type="submit">Disable Hotspot</button>
            </form>
            <a class="btn" href="#" onclick="syncTime(event)">🕰️ Sync Time</a>
          {% else %}
            <form action="{{ url_for('ap_toggle') }}" method="post" style="display:inline;">
              <button type="submit">Enable Hotspot</button>
            </form>
          {% endif %}
        </div>
        <div class="row" style="margin-top: 10px;">
            <form action="{{ url_for('shutdown_device') }}" method="post" onsubmit="return confirm('Are you sure you want to shut down the Raspberry Pi?');">
                <button type="submit" style="background-color:#d9534f; color:white; border-color:#d43f3a;">
                    🔌 Shutdown Camera
                </button>
            </form>
        </div>
      </div>
    </div>
  <form class="card" action="{{ url_for('start') }}" method="post">
    <div class="row">
      <label>⏱ Interval (s):</label>
      <input name="interval" type="number" min="1" step="1" value="{{ interval_default }}" style="width:90px">
    </div>
    <div class="row">
      <label>⏲ Duration:</label>
      <input name="duration_hours" type="number" min="0" step="1" placeholder="hrs" style="width:60px">
      <input name="duration_minutes" type="number" min="0" step="1" placeholder="mins" style="width:60px">
    </div>
    
    <div class="row">
      <button class="btn-strong" type="submit"
              {% if current_session %}disabled title="Stop current capture first"{% endif %}>
        ▶️ Start
      </button>
      <a class="btn {% if not current_session %}disabled{% endif %}"
        href="#"
        onclick="return stopClick(event)"
        {% if not current_session %}aria-disabled="true"{% endif %}>
        ⏹ Stop
      </a>
    </div>
  </form>
    {% if next_sched %}
  <div class="card">
    <div class="row" style="justify-content:space-between;align-items:center;">
      <div>
        <div style="font-weight:600">⏰ Next schedule</div>
        <div class="sub">
          {{ next_sched.start_human }} → {{ next_sched.end_human }}
          • every {{ next_sched.interval }}s
          • {{ next_sched.fps }} FPS
          • Schedule name: {{next_sched.sess}}
          • Auto-encode: {{ 'on' if next_sched.auto_encode else 'off' }}
          • {{ 'ACTIVE NOW' if next_sched.active_now else 'upcoming' }}
        </div>
      </div>
      <a class="btn" href="{{ url_for('schedule_page') }}">⚙️ Schedule Manager</a>
    </div>
  </div>
  {% else %}
    <div class="card">
    <div style="font-weight:600">Camera 📷</div>
    <div class="row">
        <form action="{{ url_for('take_web_still') }}" method="post" style="display:inline;">
          <button class="btn {% if not idle_now %}disabled{% endif %}" type="submit" {% if not idle_now %}aria-disabled="true"{% endif %}>
              📸 Quick Photo
          </button>
        </form>
    </div>
    <div class="row">
        <a class="btn {% if not idle_now %}disabled{% endif %}" href="{{ url_for('live_page') }}" {% if not idle_now %}aria-disabled="true"{% endif %}>👀 Open viewfinder</a>
        <a class="btn" href="{{ url_for('stills_gallery') }}">🖼️ Stills Gallery</a>
    </div>
    </div>
  <div class="card">
    <div class="row" style="justify-content:space-between;align-items:center;">
      <div>
        <div style="font-weight:600">⏰ No schedule set</div>
        <div class="sub">Set one up to run later.</div>
      </div>
      <a class="btn" href="{{ url_for('schedule_page') }}">➕ New schedule</a>
    </div>
  </div>
  {% endif %}

  {% for s in sessions %}
  <div class="card session {% if current_session == s.name %}active{% endif %}">
    <div class="thumb">
      {% if s.has_frame %}
        <img id="preview-{{ s.name }}" src="{{ url_for('preview', sess=s.name) }}?ts={{ s.latest }}" alt="preview" loading="lazy">
      {% else %}
        <div id="preview-placeholder-{{ s.name }}" class="placeholder">⏳ capturing…</div>
      {% endif %}
    </div>
    <div class="meta">
      <div class="name">
        {% if current_session == s.name %}
          🔴 {{ s.name }} (active)
        {% else %}
          {{ s.name }}
        {% endif %}
      </div>
      <div class="sub">
        <span id="frames-{{ s.name }}">{{ s.count }} frame{{ '' if s.count==1 else 's' }}</span>
        {% if s.has_video %} • 🎞 ready{% endif %}
        {% if current_session == s.name %}
          {# The time-left span is hidden by default unless a remaining time exists #}
          <span id="timeleft-{{ s.name }}"
                {% if remaining_min is none %}style="display:none;"{% endif %}>
            {% if remaining_min is not none %} • ⏳ {{ remaining_min }}m{{ remaining_sec_padded }}s left{% endif %}
          </span>
        {% endif %}
      </div>

      <div class="controls">
        {% if not s.has_video and current_session != s.name %}
          <form action="{{ url_for('encode', sess=s.name) }}" method="post" onsubmit="showProgress('{{ s.name }}')">
            <label>🎞 FPS:</label>
              <select name="fps" {% if encoding_active %}disabled{% endif %}>
                {% for f in fps_choices %}
                  <option value="{{ f }}" {% if f == default_fps %}selected{% endif %}>{{ f }}</option>
                {% endfor %}
              </select>
            <button class="btn" type="submit">🧩 Encode</button>
          </form>
        {% endif %}

        {% if s.has_video %}
          <a class="btn" href="{{ url_for('download', sess=s.name) }}">⬇️ Download</a>
        {% endif %}

        <form action="{{ url_for('rename', sess=s.name) }}" method="post">
          <input name="new_name" type="text" placeholder="rename…" {% if current_session == s.name %}disabled title="Stop capture first"{% endif %}>
          <button class="btn" type="submit" {% if current_session == s.name %}disabled title="Stop capture first"{% endif %}>✏️</button>
        </form>

        <form action="{{ url_for('delete', sess=s.name) }}"
              method="post"
              onsubmit="return submitDelete(this, '{{ s.name }}')">
          <button class="btn" type="submit" {% if current_session == s.name %}disabled title="Stop capture first"{% endif %}>🗑️ Delete</button>
        </form>
      </div>

      <div class="progress" id="prog-{{ s.name }}"><div class="bar" id="bar-{{ s.name }}"></div></div>
    </div>
  </div>
  {% endfor %}

  <div class="footer card">
  <div class="row">
    <div class="label" id="disk-text">
      Storage: {{ disk.free_gb }}GB free of {{ disk.total_gb }}GB ({{ 100 - disk.pct_free }}% used)
    </div>
    <div class="diskbar" aria-label="disk usage">
      <div class="fill" id="disk-fill" style="width: {{ 100 - disk.pct_free }}%;"></div>
    </div>
  </div>
</div>
</main>

<script>
    function toggleSettings() {
        const panel = document.getElementById('settings-panel');
        if (!panel) return; // Safety check
        if (panel.style.display === 'none' || panel.style.display === '') {
        panel.style.display = 'block';
        } else {
        panel.style.display = 'none';
        }
    }
  function stopClick(evt){
    if (evt && evt.preventDefault) evt.preventDefault();
    const btn = evt?.currentTarget;
    if (btn) { btn.classList.add('disabled'); btn.textContent = '⏳ Stopping…'; }

    // Fire stop, then poll until capture is actually inactive, then refresh
    fetch("{{ url_for('stop_route') }}", { method:"POST" }).catch(()=>{});

    const sessionName = {{ current_session | tojson }};
    const statusUrl   = "{{ url_for('session_status', sess='') }}";

    const startT = Date.now();
    const maxWaitMs = 15000;   // up to 15s
    const tickMs    = 400;

    const poll = async () => {
      // if we no longer have a session name, just reload
      if (!sessionName) { location.reload(); return; }
      try {
        const r = await fetch(statusUrl + sessionName, { cache:'no-store' });
        if (r.ok) {
          const j = await r.json();
          if (!j.active) { location.reload(); return; }
        }
      } catch(_) {}
      if (Date.now() - startT > maxWaitMs) { location.reload(); return; }
      setTimeout(poll, tickMs);
    };
    setTimeout(poll, tickMs);
    return false;
  }
async function testCapture(){
    try {
      // Call /test_capture to take a single still
      const resp = await fetch("{{ url_for('test_capture') }}");
      if (!resp.ok) throw new Error('test capture failed');
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);

      // Create an overlay that covers the screen
      const overlay = document.createElement('div');
      overlay.id = 'test-overlay';
      overlay.style.position = 'fixed';
      overlay.style.top = '0';
      overlay.style.left = '0';
      overlay.style.width = '100%';
      overlay.style.height = '100%';
      overlay.style.background = 'rgba(0,0,0,0.7)';
      overlay.style.display = 'flex';
      overlay.style.justifyContent = 'center';
      overlay.style.alignItems = 'center';
      overlay.style.zIndex = '1000';

      // Container for the image and button
      const container = document.createElement('div');
      container.style.background    = '#fff';
      container.style.borderRadius  = '8px';
      container.style.padding       = '10px';
      container.style.maxWidth      = '90%';
      container.style.maxHeight     = '90%';
      container.style.overflow      = 'auto';
      container.style.textAlign     = 'center';

      // Insert the captured image
      const img = document.createElement('img');
      img.src = url;
      img.style.maxWidth = '100%';
      img.style.height   = 'auto';
      img.style.display  = 'block';
      img.style.marginBottom = '10px';

      // Back button to dismiss the overlay
      const btn = document.createElement('button');
      btn.textContent = 'Back';
      btn.style.padding   = '8px 16px';
      btn.style.fontSize  = '16px';
      btn.onclick = () => {
        document.body.removeChild(overlay);
        URL.revokeObjectURL(url);
      };

      container.appendChild(img);
      container.appendChild(btn);
      overlay.appendChild(container);
      document.body.appendChild(overlay);
    } catch(e) {
      console.log(e);
    }
  }

  let pollTimer = null;
  function showProgress(name) {
    const wrap = document.getElementById('prog-' + name);
    const bar  = document.getElementById('bar-' + name);
    if (!wrap || !bar) return;
    wrap.classList.add('show');
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const r = await fetch("{{ url_for('jobs') }}");
        const j = await r.json();
        const job = j[name];
        if (!job) return;
        bar.style.width = (job.progress || 0) + "%";
        if (job.status === 'done' || job.status === 'error') {
          clearInterval(pollTimer);
          setTimeout(() => {
            wrap.classList.remove('show');
            bar.style.width = '0%';
            location.reload();
          }, 1200);
        }
      } catch (e) { console.log(e); }
    }, 600);
  }
  const LIVE_URL = "{{ url_for('live_mjpg') }}";
const LIVE_STATUS_URL = "{{ url_for('live_status') }}";

      function syncTime(evt) {
        if (evt && evt.preventDefault) evt.preventDefault();
        
        // Get the current time from the browser in a standard ISO format
        const clientTimeISO = new Date().toISOString();

        fetch("{{ url_for('sync_time_route') }}", { 
            method: "POST",
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ time: clientTimeISO }) // Send the browser's time
        })
        .then(response => {
            if (!response.ok) {
                // If the server returned an error, show it
                return response.json().then(err => Promise.reject(err));
            }
            return response.text(); // can be empty on success
        })
        .then(() => {
            alert('Time has been synced! The device will now reload.');
            // Use a short delay to allow the LCD service to restart before reloading
            setTimeout(() => {
                location.reload();
            }, 1500);
        })
        .catch((e) => {
            const errorMsg = e.message || 'Unknown error';
            alert('Failed to sync time: ' + errorMsg);
        });
    }

async function updateLiveUIOnce() {
  const msgEl = document.getElementById('live-msg');
  const imgEl = document.getElementById('live-img');
  if (!msgEl || !imgEl) return;

  try {
    const r = await fetch(LIVE_STATUS_URL, { cache: 'no-store' });
    const j = r.ok ? await r.json() : { idle: false };

    if (j.idle) {
      // Bind once
      if (!imgEl._handlersBound) {
        imgEl._handlersBound = true;
        imgEl.addEventListener('load', () => { msgEl.style.display = 'none'; });
        imgEl.addEventListener('error', () => {
          msgEl.style.display = 'flex';
          msgEl.textContent = 'Failed to open camera stream';
        });
      }
      if (!imgEl.src) {
        msgEl.style.display = 'flex';
        msgEl.textContent = 'Connecting to camera…';
        imgEl.src = LIVE_URL;
      }
    } else {
      msgEl.style.display = 'flex';
      msgEl.textContent = 'Camera busy (capturing/encoding)…';
      if (imgEl.src) imgEl.src = ''; // stop requesting stream
    }
  } catch {
    msgEl.style.display = 'flex';
    msgEl.textContent = 'Checking camera…';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  updateLiveUIOnce();
  setInterval(updateLiveUIOnce, 3000);
});
  // This listener should be outside of showProgress(), so it runs
  // immediately when the page loads and resumes polling if needed.
document.addEventListener('DOMContentLoaded', async () => {
    function setDiskBar(pctUsed, totals) {
    const txt  = document.getElementById('disk-text');
    const fill = document.getElementById('disk-fill');
    if (!fill || !txt) return;

    // width
    fill.style.width = pctUsed + '%';

    // color by threshold
    fill.classList.remove('ok','warn','crit');
    if (pctUsed >= 95)      fill.classList.add('crit');
    else if (pctUsed >= 85) fill.classList.add('warn');
    else                    fill.classList.add('ok');

    if (totals) {
      // when we have full stats
      const { free_gb, total_gb } = totals;
      txt.textContent = `Storage: ${free_gb} GB free of ${total_gb} GB (${pctUsed}% used)`;
    } else {
      // text-only fallback
      txt.textContent = `Storage: ${pctUsed}% used`;
    }
  }

  async function pollDisk() {
    try {
      const r = await fetch("{{ url_for('disk') }}");
      if (!r.ok) return;
      const d = await r.json();
      setDiskBar(d.pct_used, d);
    } catch (e) { /* ignore */ }
  }

  // run now + every 15s
  pollDisk();
  setInterval(pollDisk, 15000);

  // --- Refresh meter immediately when a session is deleted ---
  function submitDelete(formEl, sessName) {
    if (!confirm(`Delete ${sessName}?`)) return false;
    // Do the POST via fetch so we can update the meter without waiting
    fetch(formEl.action, { method: 'POST' })
      .then(() => {
        // Remove the card from the DOM
        const card = formEl.closest('.card.session');
        if (card) card.remove();
        // Update the disk meter immediately
        return pollDisk();
      })
      .catch(() => { /* ignore errors; page will eventually refresh */ });
    // prevent the default form navigation
    return false;
  }

  // Poll the active session for frames and remaining time
   const currentSession = {{ current_session | tojson }};
  const statusUrlBase = "{{ url_for('session_status', sess='') }}";
  const previewBase = "/session/"; // Base path for preview images

  let activeCapture = {{ 'true' if current_session else 'false' }};
  let encodingBusy  = false;
  // Assume busy until we know otherwise so buttons are conservative at page load.
  applyControlState();

  //combine flags to drive the UI ---
  function applyControlState(){
    setControlsDisabled(encodingBusy || activeCapture);
  }

  async function pollActive() {
    if (!currentSession) return;
    try {
      const r = await fetch(statusUrlBase + currentSession);
      if (!r.ok) return;
      const d = await r.json();

      // keep the "capture is running" flag updated ---
      activeCapture = !!d.active;
      applyControlState();

      if (d.active) {
        // update frame count
        const fSpan = document.getElementById('frames-' + currentSession);
        if (fSpan) {
          const count = d.frames;
          fSpan.textContent = count + (count === 1 ? ' frame' : ' frames');
        }
        // update time left
        const tSpan = document.getElementById('timeleft-' + currentSession);
        if (tSpan) {
          if (d.remaining_sec !== null && d.remaining_sec > 0) {
            const mins = Math.floor(d.remaining_sec / 60);
            const secs = d.remaining_sec % 60;
            const secStr = String(secs).padStart(2, '0');
            tSpan.textContent = ' • ⏳ ' + mins + 'm' + secStr + 's left';
            tSpan.style.display = '';
          } else {
            tSpan.style.display = 'none';
            tSpan.textContent = '';
          }
        }
        // update preview image if frames > 0
        if (d.frames > 0) {
          const placeholder = document.getElementById('preview-placeholder-' + currentSession);
          if (placeholder) {
            // replace placeholder with an <img>
            const img = document.createElement('img');
            img.id = 'preview-' + currentSession;
            img.src = previewBase + currentSession + '/preview?ts=' + Date.now();
            img.alt = 'preview';
            img.loading = 'lazy';
            img.style.width = '100%';
            img.style.height = '100%';
            img.style.objectFit = 'cover';
            placeholder.parentNode.replaceChild(img, placeholder);
          } else {
            // update existing img source to bust cache
            const pimg = document.getElementById('preview-' + currentSession);
            if (pimg) {
              const base = pimg.src.split('?')[0];
              pimg.src = base + '?ts=' + Date.now();
            }
          }
        }
      }
    } catch (err) {
      console.log(err);
    }
  }
    function setControlsDisabled(disabled) {
    // Keep STOP usable; disable everything else that can mutate state.
    const selectors = [
      'form[action="{{ url_for("start") }}"] button[type="submit"]', // Start
      'button[onclick^="testCapture"]',                               // Viewfinder test
      'form[action*="/encode/"] button[type="submit"]',               // Encode buttons
      'form[action*="/encode/"] select[name="fps"]',                  // FPS dropdown
      'form[action*="/delete/"] button[type="submit"]',               // Delete buttons
      'form[action*="/rename/"] input[name="new_name"]',              // Rename input
      'form[action*="/rename/"] button[type="submit"]'                // Rename button
    ];

    document.querySelectorAll(selectors.join(',')).forEach(el => {
      if (disabled) el.classList.add('is-disabled');
      else el.classList.remove('is-disabled');

      // Inputs/buttons should also be disabled for accessibility
      if ('disabled' in el) el.disabled = !!disabled;
    });
  }

function applyBusyFromJobs(jobs) {
  // --- NEW: set encodingBusy and recompute control state ---
  encodingBusy = Object.values(jobs || {}).some(
    j => j && (j.status === 'queued' || j.status === 'encoding')
  );
  applyControlState();
}

  // Poll /jobs regularly to keep UI state in sync
  async function pollJobsAndUpdateUI() {
    try {
      const r = await fetch("{{ url_for('jobs') }}", { cache: 'no-store' });
      if (!r.ok) return;
      const jobs = await r.json();
      applyBusyFromJobs(jobs);

      // If any jobs are running and the progress bar isn't showing yet,
      // kick showProgress for that session (nice-to-have)
      for (const [sess, job] of Object.entries(jobs)) {
        if (job && (job.status === 'queued' || job.status === 'encoding')) {
          const wrap = document.getElementById('prog-' + sess);
          if (wrap && !wrap.classList.contains('show')) {
            setTimeout(() => showProgress(sess), 10);
          }
        }
      }
    } catch (_) {}
  }

  // Initial UI state + start polling loops
  applyControlState();
  pollJobsAndUpdateUI();
  setInterval(pollJobsAndUpdateUI, 1000);

  pollActive();
  setInterval(pollActive, 2000);
});
</script>
"""

TPL_STILLS = r"""
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Stills Gallery</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background:#f8fafc; color: #111827;}
  header { background:#fff; border-bottom:1px solid #e5e7eb; padding: 10px 12px; display:flex; gap:10px; align-items:center; }
  header h1 { margin: 0; font-size: 18px; }
  main { padding: 12px; max-width: 1200px; margin: 0 auto; }
  .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
  .photo-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }
  .photo-card img { display: block; width: 100%; height: auto; aspect-ratio: 4 / 3; object-fit: cover; }
  .photo-card .info { padding: 8px; }
  .photo-card .info a { color: #111827; text-decoration: none; }
  .btn { border:1px solid #e5e7eb; background: #f3f4f6; color: #111827; border-radius:10px; padding:8px 10px; font-size:14px; text-decoration:none; }
  .btn-del { background-color:#fee2e2; border-color:#fecaca; color:#991b1b; }
  .footer { position: sticky; bottom: 0; background:#fff; border-top:1px solid #e5e7eb; padding:15px; }
  .card { background:#ffffff; border:1px solid #e5e7eb; border-radius:12px; padding: 12px; margin: 12px 0px; box-shadow: 0 1px 2px rgba(0,0,0,.04);}
</style>
    <header>
      <a class="btn" href="{{ url_for('index') }}">← Back to Timelapse</a>
      <h1>📷 Stills Gallery</h1>

    </header>
<main>
  {% if not stills %}
    <p>No stills captured yet. Press KEY3 on the device to take one.</p>
  {% else %}
    <div class="gallery">
    {% for filename in stills %}
      <div class="photo-card">
        <a href="{{ url_for('serve_still', filename=filename) }}" target="_blank">
          <img src="{{ url_for('serve_still', filename=filename) }}" alt="{{ filename }}" loading="lazy">
        </a>
        <div class="info">
          <div style="font-size: 12px; margin-bottom: 8px;">{{ filename }}</div>
          <form action="{{ url_for('delete_still', filename=filename) }}" method="post" onsubmit="return confirm('Delete this image?');">
            <button class="btn btn-del" type="submit">Delete</button>
          </form>
        </div>
      </div>
    {% endfor %}
    </div>
  {% endif %}
{% if stills %}
<div class="footer card">

    <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
      <a class="btn" href="{{ url_for('download_stills_zip') }}">⬇️ Download All (.zip)</a>

      <form action="{{ url_for('delete_all_stills') }}" method="post" onsubmit="return confirm('Are you sure you want to permanently delete ALL still images? This cannot be undone.');">
        <button type="submit" class="btn btn-del">🗑️ Delete All</button>
      </form>
    </div>
</div>
{% endif %}
</main>
"""
TPL_STILL_PREVIEW = r"""
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Still Preview</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background:#f8fafc; color: #111827;}
  header { background:#fff; border-bottom:1px solid #e5e7eb; padding: 10px 12px; display:flex; gap:10px; align-items:center; }
  header h1 { margin: 0; font-size: 18px; }
  main { padding: 12px; max-width: 1200px; margin: 0 auto; text-align: center; }
  img { max-width: 100%; height: auto; border: 1px solid #e5e7eb; border-radius: 8px; margin-bottom: 12px; }
  .btn { border:1px solid #e5e7eb; background: #f3f4f6; color: #111827; border-radius:10px; padding:8px 10px; font-size:14px; text-decoration:none; margin: 0 5px; }
</style>
<header>
  <h1>📷 Photo Preview</h1>
</header>
<main>
  <img src="{{ url_for('serve_still', filename=filename) }}" alt="Captured still image">
  <div>
    <a class="btn" href="{{ url_for('index') }}">← Back to Timelapse</a>
    <a class="btn" href="{{ url_for('stills_gallery') }}">🖼️ View Gallery</a>
    <a class="btn" href="{{ url_for('serve_still', filename=filename) }}" download>⬇️ Download</a>
  </div>
</main>
"""

# ======== Simple Scheduler ========
import threading, time, urllib.request, urllib.parse

_sched_lock = threading.Lock()
_sched_state = {}       # {'start_ts': int, 'end_ts': int, 'interval': int, 'fps': int}
_sched_start_t = None   # handle to the scheduled start timer
_sched_stop_t  = None   # handle to the scheduled stop timer

def _get_next_schedule():
    """Return a dict for the next (or currently active) schedule, or None."""
    now = int(time.time())
    
    # Only consider schedules that haven't ended more than 60 seconds ago.
    # This grace period gives the stop logic a chance to fire.
    upcoming = [(sid, st) for sid, st in _schedules.items()
                if int(st.get("end_ts", 0)) > now - 60]
    
    if not upcoming:
        return None

    # Sort so a currently-active one comes first; otherwise the earliest start
    def _key(item):
        st = item[1]
        start = int(st.get("start_ts", 0))
        # active ones sort as 'now'; future ones by their start time
        return (max(start, now), start)

    sid, st = sorted(upcoming, key=_key)[0]
    try:
        start_h = datetime.fromtimestamp(int(st["start_ts"])).strftime("%a %Y-%m-%d %H:%M")
        end_h   = datetime.fromtimestamp(int(st["end_ts"])).strftime("%a %Y-%m-%d %H:%M")
    except Exception:
        start_h = end_h = "?"

    return {
        "id": sid,
        "start_human": start_h,
        "end_human": end_h,
        "interval": int(st.get("interval", 10)),
        "fps": int(st.get("fps", 24)),
        "sess": (st.get("sess") or None),
        "auto_encode": bool(st.get("auto_encode", False)),
        "active_now": int(st.get("start_ts", 0)) <= now < int(st.get("end_ts", 0)),
    }

def _disk_stats(path=BASE):
    """Return total/used/free and percents for the filesystem containing `path`."""
    st = shutil.disk_usage(path)
    total = st.total
    free  = st.free
    used  = total - free
    to_gb = lambda b: round(b / (1024**3), 1)
    pct_used = int((used / total) * 100) if total else 0
    pct_free = 100 - pct_used
    return {
        "total_gb": to_gb(total),
        "free_gb":  to_gb(free),
        "used_gb":  to_gb(used),
        "pct_used": pct_used,
        "pct_free": pct_free,
    }

def _thumb_for(sess_dir, jpg_path):
    # thumbs in sessions/<name>/thumbs/<filename>.jpg
    tdir = os.path.join(sess_dir, "thumbs")
    os.makedirs(tdir, exist_ok=True)
    return os.path.join(tdir, os.path.basename(jpg_path))

def _make_thumb(src_jpg, dst_jpg, width=320):
    # use ffmpeg to generate a small preview; very light
    cmd = [FFMPEG, "-y", "-i", src_jpg, "-vf", f"scale={width}:-1", "-q:v", "5", dst_jpg]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _free_mb(path):
    st = shutil.disk_usage(path)
    return int(st.free / (1024 * 1024))

def _enough_space(required_mb=500):
    # Require at least this many MB free before we start or encode
    return _free_mb(SESSIONS_DIR) >= required_mb

def _sched_http_post(path, data=None, timeout=5):
    try:
        url = f"http://127.0.0.1:5050{path}"
        if data is None: 
            data = {}
        payload = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        urllib.request.urlopen(req, timeout=timeout).read(1)
        return True
    except Exception:
        return False
    

@app.get("/qr_info")
def qr_info():
    """Provides network info for QR code generation."""
    try:
        is_ap = ap_is_on() #
        if is_ap:
            dev = _ap_active_device() #
            ssid = _ap_ssid(dev) or HOTSPOT_NAME #
            ip = _ipv4_for_device(dev) #
            mode = "AP"
        else:
            ssid = _current_wifi_ssid() #
            ips = _all_ipv4_local() #
            ip = ips[0] if ips else ""
            mode = "Wi-Fi"
        
        return jsonify({
            "mode": mode,
            "ssid": ssid or "Unknown",
            "ip": ip or "Not Connected",
            "url": f"http://{ip}:5050" if ip else ""
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500




# ================== Simple Scheduler (ASCII-safe, single copy) ==================
import threading, time
from datetime import datetime
from flask import render_template_string, request, redirect, url_for

# _sched_lock = threading.Lock()
# _sched_state = {}        # {'start_ts': int, 'end_ts': int, 'interval': int, 'fps': int}
# _sched_start_t = None    # threading.Timer
# _sched_stop_t  = None

SCHED_TPL = '''<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Schedules</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
  h2{margin:0 0 12px}
  form{display:grid;gap:10px;max-width:440px;margin-bottom:18px}
  label{font-weight:600}
  input,select,button{padding:10px;font-size:16px}
  .cards{display:grid;gap:10px;max-width:760px}
  .card{background:#f6f9ff;border:1px solid #d7e4ff;border-radius:8px;padding:12px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .muted{color:#6b7280}
  .danger{background:#f06767;color:#fff;border:0;border-radius:10px;text-decoration:none;}
  .link{padding:8px 12px;background:#eee;border-radius:10px;text-decoration:none;color:#111}
  .full{width:100%}
</style>

<h2>Create a new schedule</h2>
<form method="post" action="{{ url_for('schedule_arm') }}">
  <label>Start (local time)</label>
  <input type="datetime-local" name="start_local" required>
  <label>Duration (hours & minutes)</label>
  <div class="row">
    <input type="number" name="duration_hr"  value=""  min="0" placeholder="hrs" style="width:120px">
    <input type="number" name="duration_min" value="" min="0" placeholder="mins" style="width:120px">
  </div>
  <label>Interval (seconds)</label>
  <input type="number" name="interval" value="{{ interval_default }}" min="1">
  <label>FPS</label>
  <select name="fps">
    {% for f in fps_choices %}
      <option value="{{ f }}" {% if f == default_fps %}selected{% endif %}>{{ f }}</option>
    {% endfor %}
  </select>
  <label style="display:flex;gap:8px;align-items:center;margin-top:6px;">
    <input type="checkbox" name="auto_encode" checked>
    Auto-encode when finished
  </label>
  <label>Session name (optional)</label>
  <input type="text" name="sess_name" placeholder="(auto)">
  <button type="submit">Create Schedule</button>
  <a class="link" href="{{ url_for('index') }}">← Back</a>
</form>

<h2>Upcoming & Active Schedules</h2>
<div class="cards">
  {% if not schedules %}
    <div class="muted">No upcoming schedules.</div>
  {% endif %}
  {% for sid, sc in schedules %}
  <div class="card">
    <div><b>ID:</b> {{ sid }}</div>
    {% if sc.sess %}<div><b>Session:</b> {{ sc.sess }}</div>{% endif %}
    <div><b>Start:</b> {{ sc.start_h }}</div>
    <div><b>End:</b>   {{ sc.end_h }}</div>
    <div><b>Interval:</b> {{ sc.interval }}s &nbsp; <b>FPS:</b> {{ sc.fps }}</div>
    <div><b>Auto-encode:</b> {{ 'on' if sc.auto_encode else 'off' }}</div>
    <form class="row" method="post" action="{{ url_for('schedule_cancel_id', sid=sid) }}" onsubmit="return confirm('Cancel this schedule?')">
      <button class="danger" type="submit">Cancel</button>
    </form>
  </div>
  {% endfor %}
</div>

<h2 style="margin-top:24px;">Past Schedules</h2>
{% if past_schedules %}
  <form action="{{ url_for('delete_past_schedules') }}" method="post" onsubmit="return confirm('Are you sure you want to delete all past schedule records?');" style="margin-bottom: 12px;">
    <button type="submit" class="danger" style="max-width: 1000px;">🗑️ Delete All Past Schedules</button>
  </form>
{% endif %}
<div class="cards">
  {% if not past_schedules %}
    <div class="muted">No past schedules.</div>
  {% endif %}
  {% for sid, sc in past_schedules %}
  <div class="card" style="opacity: 0.7;">
    <div><b>ID:</b> {{ sid }}</div>
    {% if sc.sess %}<div><b>Session:</b> {{ sc.sess }}</div>{% endif %}
    <div><b>Start:</b> {{ sc.start_h }}</div>
    <div><b>End:</b>   {{ sc.end_h }}</div>
    <div><b>Interval:</b> {{ sc.interval }}s &nbsp; <b>FPS:</b> {{ sc.fps }}</div>
    <div><b>Auto-encode:</b> {{ 'on' if sc.auto_encode else 'off' }}</div>
    <form class="row" method="post" action="{{ url_for('schedule_cancel_id', sid=sid) }}" onsubmit="return confirm('Delete this past schedule record?')">
      <button class="danger" type="submit">Delete Record</button>
    </form>
  </div>
  {% endfor %}
</div>
'''

def _sched_fire_start(interval, fps, sess_name=""):
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = globals().get('app')
    if not app:
        return
    with app.app_context():
        with app.test_client() as c:
            c.post('/start', data={'interval': interval, 'fps': fps, 'name': sess_name})

def _sched_fire_stop(sess_name="", fps=24, auto_encode=False):
    # This function runs in a background timer thread.
    # It's more reliable to call our app's functions directly
    # than to simulate HTTP requests.

    # First, get the name of the session that is currently running.
    session_to_stop = _current_session

    # Only proceed if there is an active session.
    if not session_to_stop:
        return

    # Now, stop the timelapse.
    stop_timelapse()

    # Wait a moment for the stop to fully process.
    time.sleep(1.5)

    # If auto-encode is enabled for this schedule, queue the job.
    if auto_encode:
        # Check if the session that was stopped is the one we expected
        # or if a session name was passed from the schedule.
        session_to_encode = session_to_stop or sess_name
        if session_to_encode:
            print(f"[schedule] Auto-encoding session {session_to_encode} at {fps}fps")
            _encode_q.put((session_to_encode, fps))

@app.after_request
def _no_store_for_diag(resp):
    if request.path in ("/live_diag", "/live_status", "/live_debug"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

@app.get("/disk")
def disk():
    return jsonify(_disk_stats())

@app.get("/schedule")
def schedule_page():
    # build view models for upcoming/active and past schedules
    now_ts = int(time.time())
    upcoming_schedules = []
    past_schedules = []
    
    sorted_items = sorted(_schedules.items(), key=lambda kv: kv[1].get("start_ts", 0))
    
    for sid, st in sorted_items:
        try:
            start_h = datetime.fromtimestamp(int(st["start_ts"])).strftime("%a %Y-%m-%d %H:%M")
            end_h   = datetime.fromtimestamp(int(st["end_ts"])).strftime("%a %Y-%m-%d %H:%M")
        except Exception:
            start_h = end_h = "?"
        
        # Create a view model object
        vm = dict(st)
        vm["start_h"] = start_h
        vm["end_h"]   = end_h
        
        # Sort into the correct list based on end time
        if int(st.get("end_ts", 0)) < now_ts:
            past_schedules.append((sid, vm))
        else:
            upcoming_schedules.append((sid, vm))

    return render_template_string(
        SCHED_TPL,
        fps_choices=globals().get("FPS_CHOICES", [10, 24, 30]),
        default_fps=globals().get("DEFAULT_FPS", 24),
        interval_default=globals().get("CAPTURE_INTERVAL_SEC", 10),
        schedules=upcoming_schedules,
        past_schedules=past_schedules # Pass the new list to the template
    )

@app.post("/schedule/arm")
def schedule_arm():
    start_local = request.form.get("start_local", "").strip()
    hr_str  = request.form.get("duration_hr",  "0") or "0"
    min_str = request.form.get("duration_min", "0") or "0" # <-- THE FIX IS HERE

    try: dur_hr = int(hr_str.strip())
    except: dur_hr = 0
    try: dur_min = int(min_str.strip())
    except: dur_min = 0
    duration_min = max(1, dur_hr * 60 + dur_min)

    try: interval = int(request.form.get("interval", "10") or 10)
    except: interval = 10
    try: fps = int(request.form.get("fps", "24") or 24)
    except: fps = 24
    
    auto_encode = bool(request.form.get("auto_encode"))
    sess_name = (request.form.get("sess_name") or "").strip()

    try:
        # Use a timezone-aware conversion to avoid DST bugs
        local_tz = pytz.timezone('Europe/London')
        start_dt = local_tz.localize(datetime.strptime(start_local, "%Y-%m-%dT%H:%M"))
        start_ts = int(start_dt.timestamp())
        end_ts = start_ts + duration_min * 60
    except Exception:
        # Fallback to current time + 1 minute if there is an error
        start_ts = int(time.time()) + 60
        end_ts = start_ts + duration_min * 60

    sid = uuid.uuid4().hex[:8]
    now_ts = int(time.time())
    
    with _sched_lock:
        _schedules[sid] = dict(
            start_ts=start_ts, end_ts=end_ts,
            interval=interval, fps=fps,
            sess=sess_name, auto_encode=auto_encode,
            created_ts=now_ts,
        )
        _save_sched_state()

    return redirect(url_for("schedule_page"))

@app.post("/schedule/cancel/<sid>")
def schedule_cancel_id(sid):
    with _sched_lock:
        _schedules.pop(sid, None)
        _save_sched_state()
    return redirect(url_for("schedule_page"))

@app.get("/schedule/list")
def schedule_list_json():
    """
    Compact JSON for the LCD:
    [
      {"id": "abcd1234", "start_ts": 123, "end_ts": 456, "interval": 10, "fps": 24,
       "sess": "", "auto_encode": true, "created_ts": 123}
    ]
    """
    now = int(time.time())
    items = []
    for sid, st in _schedules.items():
        try:
            d = dict(st)
            d["id"] = sid
            # normalize types
            d["start_ts"] = int(d.get("start_ts", 0))
            d["end_ts"]   = int(d.get("end_ts", 0))
            d["interval"] = int(d.get("interval", 10))
            d["fps"]      = int(d.get("fps", 24))
            d["auto_encode"] = bool(d.get("auto_encode", False))
            d["created_ts"]  = int(d.get("created_ts", d["start_ts"]))
        except Exception:
            continue
        items.append(d)

    items.sort(key=lambda d: d.get("start_ts", 0))
    resp = jsonify(items)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.post("/schedule/delete")
def schedule_delete_json():
    """
    Delete a schedule by id. Accepts form or JSON:
      - form: id=<sid>
      - json: {"id": "<sid>"}
    Returns 204 on success (even if id didn’t exist), 400 if no id supplied.
    """
    sid = request.form.get("id") or (request.json or {}).get("id")
    if not sid:
        return ("missing id", 400)

    with _sched_lock:
        # stop timers and remove
        _cancel_timers_for(sid)
        _schedules.pop(sid, None)
        _save_sched_state()
    return ("", 204)

# ================== /Simple Scheduler ==================
# Load persisted schedule and re-arm timers on process start
try:
    if _load_sched_state():
        _arm_timers_all()
except Exception:
    pass

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)



# # ---------- Main ----------
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=5050) 