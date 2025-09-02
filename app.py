# -*- coding: utf-8 -*-
import os, time, threading, subprocess, shutil, glob, json, mimetypes
from datetime import datetime
from flask import Flask, request, redirect, url_for, send_file, abort, jsonify, render_template_string

# ---------- Paths & config ----------
BASE         = "/home/pi/timelapse"
SESSIONS_DIR = os.path.join(BASE, "sessions")
IMAGES_DIR   = os.path.join(BASE, "images")      # legacy; not used for new sessions
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR,   exist_ok=True)

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

# ---------- Globals ----------
app = Flask(__name__)

# --- schedule_addon absolute-path import & init ---
try:
    import importlib.util
    _sched_path = "/home/pi/timelapse/schedule_addon.py"
    _spec = importlib.util.spec_from_file_location("schedule_addon", _sched_path)
    schedule_addon = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(schedule_addon)
    # register blueprint(s)
    try:
        schedule_addon.init(app)
    except AttributeError:
        pass
except Exception as e:
    print("[schedule] init failed:", e)


_stop_event = threading.Event()
_capture_thread = None
_current_session = None   # session name (string) while capturing
_jobs = {}                # encode job progress by session

# ---------- Helpers ----------
def _session_path(name): return os.path.join(SESSIONS_DIR, name)
def _session_latest_jpg(sess_dir):
    files = sorted(glob.glob(os.path.join(sess_dir, "*.jpg")))
    return files[-1] if files else None
def _video_path(sess_dir): return os.path.join(sess_dir, "video.mp4")
def _safe_name(s): return "".join(c for c in s if c.isalnum() or c in ("-","_"))
def _timestamped_session():
    return "session-" + datetime.now().strftime("%Y%m%d-%H%M%S")

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

# ---------- Capture thread ----------
def _capture_loop(sess_dir, interval):
    global _stop_event
    i = 0
    while not _stop_event.is_set():
        i += 1
        jpg = os.path.join(sess_dir, f"{i:06d}.jpg")
        cmd = [
            CAMERA_STILL, "-o", jpg,
            "--width", CAPTURE_WIDTH, "--height", CAPTURE_HEIGHT,
            "--quality", CAPTURE_QUALITY, "--immediate", "--nopreview"
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            # If capture fails, sleep a little and keep trying
            time.sleep(min(2, interval))
        # wait for next frame, but allow early stop
        for _ in range(int(interval*10)):
            if _stop_event.is_set(): break
            time.sleep(0.1)

# ---------- Stop helper (idempotent) ----------
def stop_timelapse():
    global _stop_event, _capture_thread, _current_session
    try: _stop_event.set()
    except Exception: pass
    if _capture_thread and getattr(_capture_thread, "is_alive", lambda: False)():
        try: _capture_thread.join(timeout=5)
        except Exception: pass
    _capture_thread = None
    _stop_event = threading.Event()
    _current_session = None

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def index():
    sessions = _list_sessions()
    return render_template_string(TPL_INDEX,
        sessions=sessions,
        current_session=_current_session,
        fps_choices=FPS_CHOICES,
        default_fps=DEFAULT_FPS,
        interval_default=CAPTURE_INTERVAL_SEC
    )

@app.route("/start", methods=["POST"])
def start():
    global _current_session, _capture_thread
    # if already running, do nothing
    if _capture_thread and _capture_thread.is_alive():
        return redirect(url_for("index"))

    name = _safe_name(request.form.get("name") or _timestamped_session())
    interval = request.form.get("interval", str(CAPTURE_INTERVAL_SEC))
    try: interval = max(1, int(interval))
    except: interval = CAPTURE_INTERVAL_SEC

    sess_dir = _session_path(name)
    os.makedirs(sess_dir, exist_ok=True)
    _current_session = name
    _stop_event.clear()

    t = threading.Thread(target=_capture_loop, args=(sess_dir, interval), daemon=True)
    _capture_thread = t
    t.start()
    return redirect(url_for("index"))

@app.route("/stop", methods=["GET","POST"], endpoint="stop_route")
def stop_route():
    stop_timelapse()
    return redirect(url_for("index"))

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
    # Disable delete for active session
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
        # No frame yet; serve a 1x1 transparent gif to avoid broken image icon
        data = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x01\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        return app.response_class(data, mimetype="image/gif")
    return send_file(jpg)

@app.post("/encode/<sess>")
def encode(sess):
    fps = request.form.get("fps", str(DEFAULT_FPS))
    try: fps = int(fps)
    except: fps = DEFAULT_FPS
    if fps not in FPS_CHOICES: fps = DEFAULT_FPS

    sess_dir = _session_path(sess)
    if not os.path.isdir(sess_dir): abort(404)
    frames = sorted(glob.glob(os.path.join(sess_dir, "*.jpg")))
    if not frames: return redirect(url_for("index"))

    out = _video_path(sess_dir)
    job_key = sess
    _jobs[job_key] = {"status":"encoding","progress":0}

    def _worker():
        try:
            # build ffmpeg command
            # input: %06d.jpg
            cmd = [
                FFMPEG, "-y",
                "-framerate", str(fps),
                "-i", os.path.join(sess_dir, "%06d.jpg"),
                "-vf", f"scale={CAPTURE_WIDTH}:{CAPTURE_HEIGHT}",
                "-pix_fmt", "yuv420p",
                out
            ]
            # track progress approximately by polling file size / time
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            # crude progress tick
            ticks = 0
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                ticks += 1
                # fake a 0-95% progress while running
                _jobs[job_key]["progress"] = min(95, ticks % 96)
            rc = proc.wait()
            if rc == 0 and os.path.exists(out):
                _jobs[job_key] = {"status":"done","progress":100}
            else:
                _jobs[job_key] = {"status":"error","progress":0}
        except Exception:
            _jobs[job_key] = {"status":"error","progress":0}

    threading.Thread(target=_worker, daemon=True).start()
    return redirect(url_for("index"))

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
    --btn-strong-bg: #2563eb;
    --btn-strong-text: #ffffff;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background:#f8fafc; color: var(--text);}
  header { position: sticky; top: 0; background:#fff; border-bottom:1px solid var(--border); padding: 10px 12px; display:flex; gap:10px; align-items:center; }
  header h1 { margin: 0; font-size: 18px; }
  main { padding: 12px; max-width: 820px; margin: 0 auto; }

  .row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
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
  .btn-strong { background: var(--btn-strong-bg); color: var(--btn-strong-text); border-color: transparent; }
  button:disabled { opacity:.5; }

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

  @media (max-width: 460px) {
    .session { grid-template-columns: 104px 1fr; }
    .thumb { width: 104px; height: 78px; }
    header h1 { font-size: 16px; }
  }
</style>

<header>
  <h1>📸 Pi Timelapse</h1>
</header>

<main>
  <form class="card" action="{{ url_for('start') }}" method="post">
    <div class="row">
      <button class="btn-strong" type="submit">▶️ Start</button>
      <a class="btn" href="{{ url_for('stop_route') }}" onclick="event.preventDefault(); postStop();">⏹ Stop</a>
      <label>⏱ Interval (s):</label>
      <input name="interval" type="number" min="1" step="1" value="{{ interval_default }}" style="width:90px">
      <label>📝 Session name:</label>
      <input name="name" type="text" placeholder="(auto)" style="min-width:160px">
      <button class="btn" type="button" onclick="testCapture()">🧪 Test</button>
    </div>
  </form>

  {% for s in sessions %}
  <div class="card session {% if current_session == s.name %}active{% endif %}">
    <div class="thumb">
      {% if s.has_frame %}
        <img src="{{ url_for('preview', sess=s.name) }}?ts={{ s.latest }}" alt="preview" loading="lazy">
      {% else %}
        <div class="placeholder">⏳ capturing…</div>
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
      <div class="sub">{{ s.count }} frame{{ '' if s.count==1 else 's' }}{% if s.has_video %} • 🎞 ready{% endif %}</div>

      <div class="controls">
        {% if not s.has_video and current_session != s.name %}
          <form action="{{ url_for('encode', sess=s.name) }}" method="post" onsubmit="showProgress('{{ s.name }}')">
            <label>🎞 FPS:</label>
            <select name="fps">
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
          <button class="btn" type="submit" {% if current_session == s.name %}disabled title="Stop capture first"{% endif %}>✏️ Rename</button>
        </form>

        <form action="{{ url_for('delete', sess=s.name) }}" method="post" onsubmit="return confirm('Delete {{ s.name }}?')" >
          <button class="btn" type="submit" {% if current_session == s.name %}disabled title="Stop capture first"{% endif %}>🗑️ Delete</button>
        </form>
      </div>

      <div class="progress" id="prog-{{ s.name }}"><div class="bar" id="bar-{{ s.name }}"></div></div>
    </div>
  </div>
  {% endfor %}
</main>

<script>
  async function postStop(){
    try{
      await fetch("{{ url_for('stop_route') }}", {method:"POST"});
      location.reload();
    }catch(e){ console.log(e); }
  }
  async function testCapture(){
    try{
      // quick single still just to test camera; non-blocking
      await fetch("{{ url_for('preview', sess=(current_session or (sessions[0].name if sessions else '')) ) }}");
    }catch(e){ console.log(e); }
  }

  let pollTimer=null;
  function showProgress(name){
    const wrap = document.getElementById('prog-'+name);
    const bar  = document.getElementById('bar-'+name);
    if(!wrap||!bar) return;
    wrap.classList.add('show');
    if(pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async ()=>{
      try{
        const r = await fetch("{{ url_for('jobs') }}");
        const j = await r.json();
        const job = j[name];
        if(!job){ return; }
        bar.style.width = (job.progress||0) + "%";
        if(job.status==='done' || job.status==='error'){
          clearInterval(pollTimer);
          setTimeout(()=>{ wrap.classList.remove('show'); bar.style.width='0%'; location.reload(); }, 400);
        }
      }catch(e){ console.log(e); }
    }, 600);
  }
</script>
"""

# ---------- Main ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)


# ======== Simple Scheduler (adds /schedule page) ========
import threading, time, urllib.request, urllib.parse

_sched_lock = threading.Lock()
_sched_state = {}  # {'start_ts':int,'end_ts':int,'interval':int,'fps':int}
_sched_start_timer = None
_sched_stop_timer  = None

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


def _sched_fire_start(interval, fps):
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = globals().get('app')
    if not app: return
    with app.app_context():
        with app.test_client() as c:
            c.post('/start', data={'interval': interval, 'fps': fps})

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

_sched_lock = threading.Lock()
_sched_state = {}        # {'start_ts': int, 'end_ts': int, 'interval': int, 'fps': int}
_sched_start_t = None    # threading.Timer
_sched_stop_t  = None

SCHED_TPL = '''<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Schedule Timelapse</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:16px}
  form{display:grid;gap:10px;max-width:420px}
  label{font-weight:600}
  input,select,button{padding:10px;font-size:16px}
  .card{background:#f6f9ff;border:1px solid #d7e4ff;border-radius:8px;padding:12px;margin-top:12px}
</style>
<h2>Schedule a timelapse</h2>
<form method="post" action="{{ url_for('schedule_arm') }}">
  <label>Start (local time)</label>
  <input type="datetime-local" name="start_local" required>
  <label>Duration (minutes)</label>
  <input type="number" name="duration_min" value="60" min="1">
  <label>Interval (seconds)</label>
  <input type="number" name="interval" value="{{ interval_default }}" min="1">
  <label>FPS</label>
  <select name="fps">
    {% for f in fps_choices %}
      <option value="{{ f }}" {% if f == default_fps %}selected{% endif %}>{{ f }}</option>
    {% endfor %}
  </select>
  <button type="submit">Arm Schedule</button>
</form>

{% if sched %}
<div class="card">
  <div><b>Current schedule</b></div>
  <div>Start: {{ sched.start_ts }} (unix)</div>
  <div>End: {{ sched.end_ts }} (unix)</div>
  <div>Interval: {{ sched.interval }}s &nbsp; FPS: {{ sched.fps }}</div>
  <form method="post" action="{{ url_for('schedule_cancel') }}" style="margin-top:8px">
    <button type="submit">Cancel Schedule</button>
  </form>
</div>
{% endif %}
'''

def _sched_fire_start(interval, fps):
    # call your existing /start route via test_client
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = globals().get('app')
    if not app: return
    with app.app_context():
        with app.test_client() as c:
            c.post('/start', data={'interval': interval, 'fps': fps})

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

@app.get("/schedule")
def schedule_page():
    cur = type("S", (), _sched_state) if _sched_state else None
    return render_template_string(
        SCHED_TPL,
        fps_choices=globals().get("FPS_CHOICES", [10,24,30]),
        default_fps=globals().get("DEFAULT_FPS", 24),
        interval_default=globals().get("CAPTURE_INTERVAL_SEC", 10),
        sched=cur
    )

@app.post("/schedule/arm")
def schedule_arm():
    start_local = request.form.get("start_local","").strip()
    duration_min = int(request.form.get("duration_min","60") or 60)
    interval = int(request.form.get("interval","10") or 10)
    fps = int(request.form.get("fps","24") or 24)
    try:
        start_ts = int(datetime.strptime(start_local, "%Y-%m-%dT%H:%M").timestamp())
    except Exception:
        start_ts = int(time.time()) + 60
    end_ts = start_ts + duration_min*60

    now = int(time.time())
    delay_start = max(0, start_ts - now)
    delay_stop  = max(0, end_ts - now)

    global _sched_start_t, _sched_stop_t
    with _sched_lock:
        if _sched_start_t:
            try: _sched_start_t.cancel()
            except: pass
            _sched_start_t = None
        if _sched_stop_t:
            try: _sched_stop_t.cancel()
            except: pass
            _sched_stop_t = None
        _sched_state.clear()
        _sched_state.update(dict(start_ts=start_ts,end_ts=end_ts,interval=interval,fps=fps))
        _sched_start_t = threading.Timer(delay_start,_sched_fire_start,args=(interval,fps))
        _sched_stop_t  = threading.Timer(delay_stop,_sched_fire_stop)
        _sched_start_t.daemon=True; _sched_stop_t.daemon=True
        _sched_start_t.start(); _sched_stop_t.start()
    return redirect(url_for("schedule_page"))

@app.post("/schedule/cancel")
def schedule_cancel():
    global _sched_start_t, _sched_stop_t
    with _sched_lock:
        if _sched_start_t:
            try: _sched_start_t.cancel()
            except: pass
            _sched_start_t=None
        if _sched_stop_t:
            try: _sched_stop_t.cancel()
            except: pass
            _sched_stop_t=None
        _sched_state.clear()
    return redirect(url_for("schedule_page"))
# ================== /Simple Scheduler ==================


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port)



# --- init scheduler blueprint (safe to run multiple times) ---
try:
    schedule_addon.init(app)
except Exception as e:
    print('[schedule] init failed:', e)
