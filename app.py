# ---------- Hotspot / AP control (NetworkManager) ----------
import os, subprocess, re
from flask import jsonify

HOTSPOT_NAME = os.environ.get("HOTSPOT_NAME", "Pi-Hotspot")

def _nmcli(*args, timeout=6):
    """Run nmcli via sudo (allowed in sudoers). Return (ok, stdout)."""
    try:
        p = subprocess.run(
            ["sudo", "/usr/bin/nmcli", *map(str, args)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, check=False
        )
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        return (p.returncode == 0), out
    except Exception as e:
        return False, str(e)

def _ap_is_active() -> bool:
    ok, out = _nmcli("con", "show", "--active")
    return ok and (HOTSPOT_NAME in out)

def _find_ap_device() -> str | None:
    # Prefer device status table
    ok, out = _nmcli("device", "status")
    if ok:
        # DEVICE  TYPE  STATE      CONNECTION
        # wlan0   wifi  connected  Pi-Hotspot
        for ln in out.splitlines()[1:]:
            cols = ln.split()
            if len(cols) >= 4 and " ".join(cols[3:]) == HOTSPOT_NAME:
                return cols[0]
    # Fallback: active connections table
    ok, out = _nmcli("con", "show", "--active")
    if ok:
        for ln in out.splitlines():
            if HOTSPOT_NAME in ln:
                parts = ln.split()
                return parts[-1] if parts else None
    return None

def _ipv4_for_device(dev: str) -> list[str]:
    """Try nmcli; fallback to iproute2."""
    if not dev:
        return []
    ok, out = _nmcli("-g", "IP4.ADDRESS", "device", "show", dev)
    ips = []
    if ok and out:
        for ln in out.splitlines():
            ip = ln.strip().split("/", 1)[0]
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                ips.append(ip)
    if ips:
        return ips
    # Fallback
    try:
        p = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", dev],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False
        )
        for ln in p.stdout.splitlines():
            # e.g. "3: wlan0    inet 10.42.0.1/24 ..."
            parts = ln.split()
            if len(parts) >= 4 and parts[2] == "inet":
                ip = parts[3].split("/", 1)[0]
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    ips.append(ip)
    except Exception:
        pass
    return ips

def _ap_status_payload() -> dict:
    on  = _ap_is_active()
    dev = _find_ap_device()
    ips = _ipv4_for_device(dev)
    # Choose a single "best" IP (prefer wlan*)
    best = ips[0] if ips else None
    return {
        "on": on,
        "name": HOTSPOT_NAME,
        "mode": "ap" if on else "client",
        "device": dev,
        "ip": best,
        "ips": ips,
    }

# Public helpers (kept minimal & idempotent)
def ap_is_on() -> bool:
    return _ap_is_active()

def ap_enable() -> bool:
    _nmcli("radio", "wifi", "on")
    ok, out = _nmcli("con", "up", HOTSPOT_NAME)
    return ok

def ap_disable() -> bool:
    ok, out = _nmcli("con", "down", HOTSPOT_NAME)
    if ok:
        return True
    txt = (out or "").lower()
    # Treat "not active" as success to make it idempotent
    return ("not active" in txt) or ("unknown connection" in txt)

# ---------- Routes ----------
@app.get("/ap/status")
def ap_status():
    return jsonify(_ap_status_payload())

# Back-compat for older clients that might be calling /ap_status
@app.get("/ap_status")
def ap_status_compat():
    return jsonify(_ap_status_payload())

@app.post("/ap/on")
def ap_on():
    ok = ap_enable()
    return (jsonify({"on": True}) if ok
            else (jsonify({"on": False, "error": "enable_failed"}), 500))

@app.post("/ap/off")
def ap_off():
    ok = ap_disable()
    return (jsonify({"on": False}) if ok
            else (jsonify({"on": ap_is_on(), "error": "disable_failed"}), 500))

@app.post("/ap/toggle")
def ap_toggle():
    target_ok = ap_disable() if ap_is_on() else ap_enable()
    # Report final state (even if the requested action failed)
    return (jsonify({"on": ap_is_on()}) if target_ok
            else (jsonify({"on": ap_is_on(), "error": "toggle_failed"}), 500))

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
    )

@app.route("/start", methods=["POST"])
def start():
    global _current_session, _capture_thread, _capture_stop_timer, _capture_end_ts

    # Block starting while an encode is active
    if _any_encoding_active():
        return redirect(url_for("index"))

    if _capture_thread and _capture_thread.is_alive():
        return redirect(url_for("index"))

    # --- read form values ---
    name = _safe_name(request.form.get("name") or _timestamped_session())
    interval_raw = request.form.get("interval", str(CAPTURE_INTERVAL_SEC))
    try:
        interval = max(1, int(interval_raw))
    except Exception:
        interval = CAPTURE_INTERVAL_SEC

    # refuse to start if low on disk
    if not _enough_space(50):
        abort(507, "Low Storage")  # Insufficient Storage

    # optional duration for automatic stop
    hr_str = request.form.get("duration_hours", "0") or "0"
    mn_str = request.form.get("duration_minutes", "0") or "0"
    try: hr_val = int(hr_str.strip())
    except Exception: hr_val = 0
    try: mn_val = int(mn_str.strip())
    except Exception: mn_val = 0
    duration_min = hr_val * 60 + mn_val
    if duration_min <= 0:
        duration_min = None

    # --- IMPORTANT: ensure live view isn’t holding the camera ---
    _stop_live_proc()

    # Set up the session
    sess_dir = _session_path(name)
    os.makedirs(sess_dir, exist_ok=True)
    _current_session = name
    _stop_event.clear()

    t = threading.Thread(target=_capture_loop, args=(sess_dir, interval), daemon=True)
    _capture_thread = t
    t.start()

    # Auto-stop timer (if duration provided)
    if _capture_stop_timer:
        try: _capture_stop_timer.cancel()
        except Exception: pass
        _capture_stop_timer = None
        _capture_end_ts = None

    if duration_min:
        _capture_end_ts = time.time() + duration_min * 60
        _capture_stop_timer = threading.Timer(duration_min * 60, stop_timelapse)
        _capture_stop_timer.daemon = True
        _capture_stop_timer.start()

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

def _finalize_stop_background():
    global _capture_thread, _stop_event, _current_session, _capture_stop_timer, _capture_end_ts
    try:
        t = _capture_thread
        if t and getattr(t, "is_alive", lambda: False)():
            t.join(timeout=10)
    except Exception:
        pass
    finally:
        _capture_thread = None
        _stop_event = threading.Event()
        _current_session = None
        if _capture_stop_timer:
            try: _capture_stop_timer.cancel()
            except Exception: pass
            _capture_stop_timer = None
        _capture_end_ts = None

def stop_timelapse():
    try: _stop_event.set()
    except Exception: pass
    threading.Thread(target=_finalize_stop_background, daemon=True).start()

@app.route("/stop", methods=["GET","POST"], endpoint="stop_route")
def stop_route():
    stop_timelapse()
    return ("", 204)

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

@app.post("/schedule/cancel/<sid>")
def schedule_cancel_one(sid):
    with _sched_lock:
        if sid in _schedules:
            _cancel_schedule_locked(sid)
    return redirect(url_for("schedule_page"))

@app.post("/schedule/cancel")
def schedule_cancel_compat():
    with _sched_lock:
        ids = list(_schedules.keys())
        for sid in ids:
            _cancel_schedule_locked(sid)
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

@app.get("/lcd_status")
def lcd_status():
    try:
        active = bool(_current_session)
        frames = 0
        if _current_session:
            sd = _session_path(_current_session)
            if os.path.isdir(sd):
                frames = len(glob.glob(os.path.join(sd, "*.jpg")))
        remaining = None
        if active and _capture_end_ts:
            rem = int(_capture_end_ts - time.time())
            if rem > 0: remaining = rem

        nxt = _get_next_schedule()
        next_sched = None
        if nxt:
            # keep it small & machine-friendly
            # (use timestamps so the HAT can render human text itself)
            # you already compute active_now; keep it.
            # include id so a future LCD menu could cancel by id if wanted.
            next_sched = {
                "id": nxt["id"],
                "start_ts": int(_schedules[nxt["id"]]["start_ts"]) if nxt["id"] in _schedules else None,
                "end_ts":   int(_schedules[nxt["id"]]["end_ts"])   if nxt["id"] in _schedules else None,
                "interval": nxt["interval"],
                "fps": nxt["fps"],
                "name": nxt.get("sess"),
                "active_now": bool(nxt["active_now"]),
                "auto_encode": bool(nxt["auto_encode"]),
            }

        return jsonify({
            "active": active,
            "session": _current_session or "",
            "frames": frames,
            "remaining_sec": remaining,     # null if not set
            "encoding": _any_encoding_active(),
            "disk": _disk_stats(),
            "next_sched": next_sched,
            "live_idle": _idle_now(),
        })
    except Exception:
        # never crash the LCD
        return jsonify({
            "active": False, "session": "", "frames": 0,
            "remaining_sec": None, "encoding": False,
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
    def _trace(msg):
        try:
            _live_last_stderr.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        except Exception:
            pass

    _trace("ENTER /live.mjpg")

    # Only stream when idle, otherwise 503
    if not _idle_now():
        abort(503, "Busy")

    # Per-request process (no globals)
    vid_bin = shutil.which("libcamera-vid") or shutil.which("rpicam-vid")
    if not vid_bin:
        abort(500, "No camera video binary found (libcamera-vid/rpicam-vid).")

    # Optional: free stragglers before starting (safe + fast)
    _force_release_camera()

    def build_cmd(w, h):
        base = os.path.basename(vid_bin)
        rot = _rot_flags_for(vid_bin)
        if base.startswith("libcamera-"):
            return [
                vid_bin, "-n",
                "--codec", "mjpeg",
                "--width", str(w), "--height", str(h),
                "--framerate", "30",
                *rot,
                "-t", "0",
                "-o", "-",
            ]
        else:
            return [
                vid_bin, "--nopreview",
                "--codec", "mjpeg",
                "--width", str(w), "--height", str(h),
                "--framerate", "30",
                *rot,
                "-t", "0",
                "-o", "-",
            ]

    # Start the local process
    cmd = build_cmd(CAPTURE_WIDTH, CAPTURE_HEIGHT)
    env = dict(os.environ)
    env.setdefault("LIBCAMERA_LOG_LEVELS", "*:ERROR")
    env.setdefault("RPI_LOG_LEVEL", "error")

    _trace("SPAWN camera proc")
    if not _can_spawn_live(): abort(429)  # Too Many Requests
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0, env=env, start_new_session=True,
    )
    _trace(f"SPAWNED pid={proc.pid}")

    # Drain stderr to avoid blocking; keep last lines for /live_debug
    def _drain_stderr(p):
        try:
            if p.stderr:
                for raw in iter(lambda: p.stderr.readline(), b""):
                    try:
                        line = raw.decode("utf-8", "ignore").strip()
                    except Exception:
                        line = ""
                    if line:
                        _live_last_stderr.append(line)
        except Exception:
            pass
        finally:
            try:
                p.stderr and p.stderr.close()
            except Exception:
                pass

    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    boundary = b"--frame"

    def cleanup():
        try:
            _trace("CLEANUP proc")
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

    def gen():
        _trace("GEN start")
        buf = b""
        # 🚫 remove the text preamble, start with JPEG frames only
        try:
            while True:
                if not _idle_now():
                    break
                if proc.poll() is not None:
                    break

                chunk = proc.stdout.read(4096)
                if not chunk:
                    break

                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1: break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1: break
                    frame = buf[soi:eoi+2]
                    buf = buf[eoi+2:]
                    yield (b"--frame\r\n"
                          b"Content-Type: image/jpeg\r\n"
                          b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                          + frame + b"\r\n")
        finally:
            cleanup()

    headers = {
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store",
    }
    return app.response_class(gen(), headers=headers)
import re


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
  <h1>📸 Pi Timelapse - Mouse Eye 🐭</h1>
</header>

<main>
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
          <a class="btn {% if not idle_now %}disabled{% endif %}" href="{{ url_for('live_page') }}"{% if not idle_now %}aria-disabled="true"{% endif %}>👀 Open viewfinder</a>
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



# ======== Simple Scheduler ========
import threading, time, urllib.request, urllib.parse

_sched_lock = threading.Lock()
_sched_state = {}       # {'start_ts': int, 'end_ts': int, 'interval': int, 'fps': int}
_sched_start_t = None   # handle to the scheduled start timer
_sched_stop_t  = None   # handle to the scheduled stop timer

def _get_next_schedule():
    """Return a dict for the next (or currently active) schedule, or None."""
    now = int(time.time())
    # only schedules that haven't ended yet
    upcoming = [(sid, st) for sid, st in _schedules.items()
                if int(st.get("end_ts", 0)) > now]
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

def _sched_fire_stop():
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = globals().get('app')
    if not app: return
    with app.app_context():
        with app.test_client() as c:
            c.post('/stop')





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
  .danger{background:#ef4444;color:#fff;border:0}
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

<h2>Existing schedules</h2>
<div class="cards">
  {% if not schedules %}
    <div class="muted">No schedules yet.</div>
  {% endif %}
  {% for sid, sc in schedules %}
  <div class="card">
    <div><b>ID:</b> {{ sid }}</div>
    {% if sc.sess %}<div><b>Session:</b> {{ sc.sess }}</div>{% endif %}
    <div><b>Start:</b> {{ sc.start_h }}</div>
    <div><b>End:</b>   {{ sc.end_h }}</div>
    <div><b>Interval:</b> {{ sc.interval }}s &nbsp; <b>FPS:</b> {{ sc.fps }}</div>
    <div><b>Auto-encode:</b> {{ 'on' if sc.auto_encode else 'off' }}</div>
    <form class="row" method="post" action="{{ url_for('schedule_cancel_one', sid=sid) }}" onsubmit="return confirm('Cancel this schedule?')">
      <button class="danger" type="submit">Cancel</button>
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
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = globals().get('app')
    if not app:
        return
    with app.app_context():
        with app.test_client() as c:
            c.post('/stop')
            if auto_encode and sess_name:
                try:
                    c.post(f'/encode/{sess_name}', data={'fps': fps})
                except Exception:
                    pass

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
    # build view model
    items = []
    for sid, st in sorted(_schedules.items(), key=lambda kv: kv[1].get("start_ts", 0)):
        try:
            start_h = datetime.fromtimestamp(int(st["start_ts"])).strftime("%a %Y-%m-%d %H:%M")
            end_h   = datetime.fromtimestamp(int(st["end_ts"])).strftime("%a %Y-%m-%d %H:%M")
        except Exception:
            start_h = end_h = "?"
        vm = dict(st)
        vm["start_h"] = start_h
        vm["end_h"]   = end_h
        items.append((sid, type("S", (), vm)))
    return render_template_string(
        SCHED_TPL,
        fps_choices=globals().get("FPS_CHOICES", [10, 24, 30]),
        default_fps=globals().get("DEFAULT_FPS", 24),
        interval_default=globals().get("CAPTURE_INTERVAL_SEC", 10),
        schedules=items
    )

@app.post("/schedule/arm")
def schedule_arm():
    start_local = request.form.get("start_local", "").strip()
    hr_str  = request.form.get("duration_hr",  "0") or "0"
    min_str = request.form.get("duration_min", "60") or "60"
    try: dur_hr  = int(hr_str.strip())
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
        start_ts = int(datetime.strptime(start_local, "%Y-%m-%dT%H:%M").timestamp())
    except Exception:
        start_ts = int(time.time()) + 60
    end_ts = start_ts + duration_min * 60

    sid = uuid.uuid4().hex[:8]  # short id
    now_ts = int(time.time())
    with _sched_lock:
        _schedules[sid] = dict(
            start_ts=start_ts, end_ts=end_ts,
            interval=interval, fps=fps,
            sess=sess_name, auto_encode=auto_encode,
            created_ts=now_ts,          # <-- add this line
        )
        _save_sched_state()
        _arm_timers_for(sid, _schedules[sid])
    return redirect(url_for("schedule_page"))

@app.post("/schedule/cancel/<sid>")
def schedule_cancel_id(sid):
    with _sched_lock:
        _cancel_timers_for(sid)
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