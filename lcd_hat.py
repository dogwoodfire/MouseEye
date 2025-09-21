#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

import os, sys, time, json, threading, subprocess
from datetime import datetime, timedelta, date
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from collections import deque
import threading
import io
from urllib.parse import quote

DEBUG = os.environ.get("DEBUG") == "1"
def log(*a):
    if DEBUG:
        print(*a, file=sys.stderr, flush=True)

# ---------- GPIO factory (prefer lgpio; fallback to RPi.GPIO) ----------
try:
    from gpiozero.pins.lgpio import LGPIOFactory
    PIN_FACTORY = LGPIOFactory()
except Exception:
    from gpiozero.pins.rpigpio import RPiGPIOFactory
    PIN_FACTORY = RPiGPIOFactory()

# ----------------- HAT wiring (BCM) -----------------
PIN_DC   = int(os.environ.get("LCD_PIN_DC",   "25"))
PIN_RST  = int(os.environ.get("LCD_PIN_RST",  "27"))
PIN_BL   = int(os.environ.get("LCD_PIN_BL",   "24"))  # Backlight (PWM LED)

# Waveshare 1.44" LCD HAT keys + joystick
KEY1     = int(os.environ.get("LCD_KEY1",     "21"))  # AP Activation Key
KEY2     = int(os.environ.get("LCD_KEY2",     "20"))
KEY3     = int(os.environ.get("LCD_KEY3",     "16"))
JS_UP    = int(os.environ.get("LCD_JS_UP",    "6"))
JS_DOWN  = int(os.environ.get("LCD_JS_DOWN",  "19"))
JS_LEFT  = int(os.environ.get("LCD_JS_LEFT",  "5"))
JS_RIGHT = int(os.environ.get("LCD_JS_RIGHT", "26"))
JS_PUSH  = int(os.environ.get("LCD_JS_PUSH",  "13"))

SPI_PORT   = int(os.environ.get("LCD_SPI_PORT", "0"))
SPI_DEVICE = int(os.environ.get("LCD_SPI_DEV",  "0"))

WIDTH, HEIGHT = 128, 128

# ----------------- Backend endpoints -----------------
LOCAL = "http://127.0.0.1:5050"
STATUS_URL      = f"{LOCAL}/lcd_status"
START_URL       = f"{LOCAL}/start"
STOP_URL        = f"{LOCAL}/stop"
SCHED_ARM_URL   = f"{LOCAL}/schedule/arm"
SCHED_DEL_URLS  = [
    f"{LOCAL}/schedule/delete",
    f"{LOCAL}/schedule/remove",
    f"{LOCAL}/schedule/cancel",
]
SCHED_LIST_URL  = f"{LOCAL}/schedule/list"   # preferred if available
SCHED_FILE      = "/home/pi/timelapse/schedule.json"  # legacy fallback
QR_INFO_URL     = f"{LOCAL}/qr_info"
LCD_OFF_FLAG = "/home/pi/timelapse/lcd_off.flag"

# AP endpoints (Flask backend should return {"on","name","device","ip","ips":[]})
AP_STATUS_URL = f"{LOCAL}/ap/status"
AP_TOGGLE_URL = f"{LOCAL}/ap/toggle"
AP_ON_URL     = f"{LOCAL}/ap/on"
AP_OFF_URL    = f"{LOCAL}/ap/off"

# Cache for AP status (to draw overlay without spamming the backend)
_AP_CACHE = {"on": False, "ts": 0.0}
_STATUS_CACHE = {}
_STATUS_LOCK = threading.Lock()

STILLS_LIST_URL = f"{LOCAL}/stills_api"

def _poll_status_worker():
    """
    A daemon thread that polls the backend and updates the shared _STATUS_CACHE.
    """
    global _STATUS_CACHE
    while True:
        try:
            status = _http_json(STATUS_URL, timeout=0.8)
            ap_on = _ap_poll_cache(period=5.0) # Refresh AP status less frequently

            with _STATUS_LOCK:
                if status: # Only update if poll was successful
                    _STATUS_CACHE = status
                _STATUS_CACHE['ap_on'] = ap_on

        except Exception as e:
            log(f"Poll worker error: {e}")
        
        # Poll less frequently to reduce load
        time.sleep(1.5)

def _ap_poll_cache(period=30.0):
    """Refresh AP status cache at most once per `period` seconds.
    If the backend is unreachable, keep the last known state.
    """
    now = time.time()
    if now - _AP_CACHE["ts"] < period:
        return _AP_CACHE["on"]
    j = _http_json(AP_STATUS_URL, timeout=0.2)
    if isinstance(j, dict) and "on" in j:
        _AP_CACHE["on"] = bool(j.get("on"))
        _AP_CACHE["ts"] = now
    # If request failed, do NOT advance timestamp; we'll retry soon.
    return _AP_CACHE["on"]

# ----------------- Preferences (rotation) -----------------
PREFS_FILE = "/home/pi/timelapse/lcd_prefs.json"
def _load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            p = json.load(f)
            return p if isinstance(p, dict) else {}
    except Exception:
        return {}
def _save_prefs(p):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(p, f)
    except Exception:
        pass

# --------- Still Images --------
def _http_post_and_get_image(url, timeout=12.0):
    """Sends a POST request and returns the raw response body (e.g., an image)."""
    try:
        req = Request(url, data=b'', method="POST")
        with urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return r.read()
            return None
    except Exception as e:
        log(f"HTTP Post/Get Image Error: {e}")
        return None
    
# ----------------- Import for QR code ---------------
import qrcode


# ----------------- Imports for LCD & IO -----------------
from PIL import Image, ImageDraw, ImageFont
from gpiozero import Button, PWMLED
from luma.core.interface.serial import spi
from luma.lcd.device import st7735

# ---------- SPI + device helpers (8 MHz for stability) ----------
def _mk_serial():
    return spi(
        port=SPI_PORT, device=SPI_DEVICE,
        gpio_DC=PIN_DC, gpio_RST=PIN_RST,
        bus_speed_hz=8_000_000
    )

def _mk_device(serial_obj):
    # Keep the device at rotation=0; we rotate frames in software.
    return st7735(serial_obj, width=WIDTH, height=HEIGHT,
                  rotation=0, h_offset=1, v_offset=2, bgr=True)

# ----------------- Fonts & colors -----------------
def _load_font(size_px):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size_px)
        except Exception:
            pass
    return ImageFont.load_default()

F_TITLE = _load_font(13)
F_TEXT  = _load_font(11)
F_SMALL = _load_font(9)
F_VALUE = _load_font(17)

WHITE=(255,255,255); GRAY=(140,140,140); CYAN=(120,200,255)
GREEN=(80,220,120);  YELL=(255,210,80);  BLUE=(90,160,220)
RED=(255,80,80);     DIM=(90,90,90)

SPINNER = ["-", "\\", "|", "/"]

# --- Icon Definitions using pytablericons ---
from pytablericons import TablerIcons, OutlineIcon

# Icon settings
ICON_SIZE = (16, 16)
STROKE_WEIGHT = 1
ICON_SIZE_LARGE = (45, 45)
STROKE_WEIGHT = 2

# Generate PIL Image objects directly with the color white
screen_off_icon = TablerIcons.load(OutlineIcon.MOON, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_SCREEN_OFF = screen_off_icon.resize(ICON_SIZE)

rotate_icon = TablerIcons.load(OutlineIcon.REFRESH, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_ROTATE = rotate_icon.resize(ICON_SIZE)

shutdown_icon = TablerIcons.load(OutlineIcon.POWER, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_SHUTDOWN = shutdown_icon.resize(ICON_SIZE)

icon_portrait = TablerIcons.load(OutlineIcon.RECTANGLE_VERTICAL, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_PORTRAIT = icon_portrait.resize(ICON_SIZE_LARGE)

icon_landscape = TablerIcons.load(OutlineIcon.RECTANGLE, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_LANDSCAPE = icon_landscape.resize(ICON_SIZE_LARGE)

icon_play = TablerIcons.load(OutlineIcon.PLAYER_PLAY, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_PLAY = icon_play.resize(ICON_SIZE)

icon_camera = TablerIcons.load(OutlineIcon.CAMERA_PLUS, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_CAMERA = icon_camera.resize(ICON_SIZE)

icon_calendar = TablerIcons.load(OutlineIcon.CALENDAR_EVENT, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_CALENDAR = icon_calendar.resize(ICON_SIZE)

icon_photo = TablerIcons.load(OutlineIcon.PHOTO, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_PHOTO = icon_photo.resize(ICON_SIZE)

icon_settings = TablerIcons.load(OutlineIcon.SETTINGS, stroke_width=STROKE_WEIGHT, color="white")
IMG_ICON_SETTINGS = icon_settings.resize(ICON_SIZE)


# ----------------- HTTP helpers -----------------
def _ap_status():
    j = _http_json(AP_STATUS_URL)
    return bool(j and j.get("on"))

def _http_json(url, timeout=0.4):
    try:
        with urlopen(Request(url, headers={"Cache-Control":"no-store"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def _http_post_form(url, data: dict, timeout=3.5):
    try:
        body = urlencode(data).encode("utf-8")
        req = Request(url, data=body, method="POST",
                      headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urlopen(req, timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False

# ----------------- Local network helpers -----------------
def _local_ipv4s():
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, stderr=subprocess.DEVNULL).strip()
        return [ip for ip in out.split() if "." in ip]
    except Exception:
        return []

def _current_wifi_ssid():
    # Try a few nmcli invocations; return first non-empty SSID.
    cmds = [
        ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],   # lines like "yes:MySSID"
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,ACTIVE", "con", "show", "--active"],  # "HomeNet:wifi:wlan0:yes"
    ]
    for cmd in cmds:
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=0.8)
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                if cmd[2] == "dev":
                    if line.startswith("yes:"):
                        ssid = line.split(":", 1)[1]
                        if ssid:
                            return ssid
                else:
                    parts = line.split(":")
                    if len(parts) >= 4 and parts[1] in ("wifi", "802-11-wireless") and parts[-1] in ("yes", "activated", "activated (externally)"):
                        return parts[0]
        except Exception:
            continue
    return ""

# ----------------- Schedules (read: prefer backend JSON) -----------------
def _read_schedules():
    # 1) Try backend list
    arr = _http_json(SCHED_LIST_URL)
    if isinstance(arr, list):
        out = []
        for d in arr:
            if isinstance(d, dict) and d.get("id"):
                sid = str(d["id"])
                d2 = dict(d); d2.pop("id", None)
                out.append((sid, d2))
        out.sort(key=lambda kv: int(kv[1].get("start_ts", 0)))
        return out
    # 2) Fallback to file (legacy)
    try:
        with open(SCHED_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            items = [(k, v) for k, v in data.items() if isinstance(v, dict)]
        elif isinstance(data, list):
            items = []
            for d in data:
                if isinstance(d, dict):
                    sid = str(d.get("id", "")) or f"{int(d.get('start_ts',0))}-{int(d.get('end_ts',0))}"
                    items.append((sid, d))
        else:
            items = []
        items.sort(key=lambda kv: int(kv[1].get("start_ts", 0)))
        return items
    except Exception:
        return []

def _post_schedule_arm(start_dt, duration_hr, duration_min, interval_s, auto_encode=True, fps=24, sess_name=""):
    start_local = start_dt.strftime("%Y-%m-%dT%H:%M")
    payload = {
        "start_local": start_local,
        "duration_hr":  str(int(duration_hr)),
        "duration_min": str(int(duration_min)),
        "interval":     str(int(interval_s)),
        "fps":          str(int(fps)),
        "auto_encode":  "on" if auto_encode else "",
        "sess_name":    sess_name or "",
    }
    return _http_post_form(SCHED_ARM_URL, payload)

def _delete_schedule_backend(sched_id: str):
    # Try common endpoints; if all fail, backend will still be source of truth.
    for url in SCHED_DEL_URLS:
        if _http_post_form(url, {"id": sched_id}):
            return True
    return False

# =====================================================================
#                              UI CONTROLLER
# =====================================================================
class UI:
    # States
    HOME, TL_INT, TL_HR, TL_MIN, TL_ENC, TL_CONFIRM, \
    SCH_INT, SCH_DATE, SCH_SH, SCH_SM, SCH_EH, SCH_EM, SCH_ENC, SCH_CONFIRM, \
    SCHED_LIST, SCHED_DEL_CONFIRM, ENCODING, MODAL, QR_CODE, CAPTURING, SHUTDOWN_CONFIRM, SETTINGS_MENU, STILLS_VIEWER = range(23)

    def __init__(self):
        # prefs
        prefs = _load_prefs()
        self.rot_deg = 90 if int(prefs.get("rot_deg", 180)) == 90 else 180

        # lcd
        self.serial = _mk_serial()
        self.device = _mk_device(self.serial)
        self._draw_lock = threading.RLock()
        self._need_home_clear = True
        self._need_hard_clear = True
        self._busy = False
        self._screen_off = False
        self._spin_idx = 0

        # backlight
        try:
            self.bl = PWMLED(PIN_BL); self.bl.value = 1.0
        except Exception:
            self.bl = None

        # inputs
        self.btn_key1 = self._mk_button(KEY1)
        self.btn_key2 = self._mk_button(KEY2)
        self.btn_key3 = self._mk_button(KEY3)
        self.js_up    = self._mk_button(JS_UP)
        self.js_down  = self._mk_button(JS_DOWN)
        self.js_left  = self._mk_button(JS_LEFT)
        self.js_right = self._mk_button(JS_RIGHT)
        self.js_push  = self._mk_button(JS_PUSH)
        self._bind_inputs()

        # defaults & state
        now = datetime.now()
        self.wz_interval = 10
        self.tl_hours = 0
        self.tl_mins  = 0
        self.sch_date = now.date()
        self.sch_start_h  = now.hour
        self.sch_start_m  = (now.minute + 1) % 60
        self.sch_end_h    = (now.hour + 1) % 24
        self.sch_end_m    = self.sch_start_m
        self.wz_encode   = True
        self.confirm_idx = 0
        self._last_status = {}
        self._sch_rows = []

        self.state = self.HOME
        self.menu_idx = 0
        self.menu_items = ["Quick Start", "New Timelapse", "Schedules", "View Stills", "Settings"]
        self.settings_menu_items = ["Screen off", "Rotate display", "Shutdown Camera", "‹ Back"]
        self._home_items = self.menu_items[:]
        
        self.stills_list = []
        self.stills_idx = 0

        try:
            # Load and display a custom splash screen image
            splash_path = "/home/pi/timelapse/splash.png"
            splash_img = Image.open(splash_path).convert("RGB")
            # Ensure it's the correct size for the display
            if splash_img.size != (WIDTH, HEIGHT): #
                splash_img = splash_img.resize((WIDTH, HEIGHT)) #
            self._present(splash_img)
        except Exception:
            # Fallback to text if the image fails to load
            self._draw_center("Booting…")
        
        # Keep splash visible for a few seconds before loading the full UI
        time.sleep(3)
        # --- END of new splash screen code ---

    # ---------- panel power helpers ----------
    def _panel_off(self):
        try:
            self.device.command(0x28)
            self.device.command(0x10)
        except Exception:
            pass

    def _panel_on(self):
        try:
            self.device.command(0x11)
            time.sleep(0.12)
            self.device.command(0x29)
        except Exception:
            pass

    # ---------- clears / present ----------
    def _request_hard_clear(self):
        self._need_hard_clear = True

    def _hard_clear(self):
        img = Image.new("RGB", (self.device.width, self.device.height), (0, 0, 0))
        frame = img.rotate(-90, expand=False, resample=Image.NEAREST) if self.rot_deg == 90 else img
        with self._draw_lock:
            self.device.display(frame); time.sleep(0.02)
            self.device.display(frame); time.sleep(0.02)

    def _maybe_hard_clear(self):
        if not self._need_hard_clear:
            return
        try:
            self._hard_clear()
        except Exception:
            pass
        self._need_hard_clear = False

    def _present(self, img):
        # Always draw hotspot overlay before showing the image
        try:
            self._overlay_ap(img)
        except Exception:
            pass
        # Handle 90-degree (landscape) and 180-degree (flipped portrait) rotation
        if self.rot_deg == 90:
            frame = img.rotate(-90, expand=False, resample=Image.NEAREST)
        elif self.rot_deg == 180:
            frame = img.rotate(-180, expand=False, resample=Image.NEAREST)
        else: # Fallback for any other case
            frame = img
            
        with self._draw_lock:
            self.device.display(frame)

    def _blank(self): return Image.new("RGB", (self.device.width, self.device.height), (0,0,0))
    def _clear(self): self._present(self._blank())

    def _lcd_reinit(self):
        log("LCD: reinit")
        self.serial = _mk_serial()
        self.device = _mk_device(self.serial)
        time.sleep(0.05)
        self._hard_clear()

    # ---------- fonts helpers ----------
    def _text_w(self, font, txt):
        try:   return font.getlength(txt)
        except Exception:
            try:   return font.getsize(txt)[0]
            except Exception: return len(txt) * 6

    # ---------- drawing ----------
    def _draw_lines(self, lines, title=None, footer=None,
                    highlight_idxes=None, dividers=False, divider_after=None):
        if highlight_idxes is None: highlight_idxes = set()
        else: highlight_idxes = set(highlight_idxes)
        if divider_after is None: divider_after = set()
        else: divider_after = set(divider_after)

        img = self._blank(); drw = ImageDraw.Draw(img); y = 2
        if title:
            drw.text((2,y), title, font=F_TITLE, fill=WHITE); y += 18
        for i, txt in enumerate(lines):
            fill = BLUE if i in highlight_idxes else WHITE
            drw.text((2,y), txt, font=F_TEXT, fill=fill); y += 14
            if dividers:
                if divider_after:
                    if i in divider_after:
                        drw.line((2, y-2, WIDTH-2, y-2), fill=DIM)
                else:
                    if i < len(lines) - 1:
                        drw.line((2, y-2, WIDTH-2, y-2), fill=DIM)
        if footer:
                    # Calculate the width of the footer text
                    footer_w = self._text_w(F_SMALL, footer)
                    # Center the text by calculating the starting x-coordinate
                    x_pos = (WIDTH - int(footer_w)) // 2
                    # Draw the footer with the new centered position and white color
                    drw.text((x_pos, HEIGHT-12), footer, font=F_SMALL, fill=WHITE)
        self._present(img)

    def _draw_center(self, msg, sub=None):
        img = self._blank(); drw = ImageDraw.Draw(img); w = self.device.width
        tw = self._text_w(F_TITLE, msg)
        drw.text(((w-int(tw))//2, 36), msg, font=F_TITLE, fill=WHITE)
        if sub:
            y = 60
            for line in sub.split("\n"):
                sw = self._text_w(F_SMALL, line)
                drw.text(((w-int(sw))//2, y), line, font=F_SMALL, fill=GRAY); y += 14
        self._present(img)

    def _draw_wizard_page(self, title, value, tips=None):
        img = self._blank(); drw = ImageDraw.Draw(img)
        drw.text((2, 2), title, font=F_TITLE, fill=WHITE)
        val_str = str(value); tw = self._text_w(F_VALUE, val_str)
        drw.text(((WIDTH - int(tw))//2, 40), val_str, font=F_VALUE, fill=BLUE)
        y = 80
        for line in (tips or []):
            drw.text((2, y), line, font=F_SMALL, fill=GRAY); y += 12
        self._present(img)

    def _draw_encoding(self, spin_idx=0):
        img = self._blank(); drw = ImageDraw.Draw(img)
        msg = "Encoding..."; sp = SPINNER[spin_idx % len(SPINNER)]
        tw = self._text_w(F_TITLE, msg); sw = self._text_w(F_TITLE, sp)
        drw.text(((WIDTH-int(tw))//2, 40), msg, font=F_TITLE, fill=YELL)
        drw.text(((WIDTH-int(sw))//2, 62), sp,  font=F_TITLE, fill=YELL)
        self._present(img)

    def _overlay_ap(self, base_img):
        """Draw a tiny Wi-Fi badge in the top-right if AP is ON.
        Mutates `base_img` in place.
        """
        try:
            ap_on = _ap_poll_cache()
        except Exception:
            ap_on = False
        if not ap_on:
            return
        try:
            drw = ImageDraw.Draw(base_img)
            # top-right with small padding
            pad = 3
            x1, y1 = WIDTH - 1 - pad, 1 + pad

            # Clearer, thicker arcs sharing a common center so they don't merge
            cx = x1 - 8   # center x a touch further left
            cy = y1 + 9   # center y

            for r in (7, 5, 3):
                drw.arc((cx - r, cy - r, cx + r, cy + r), 215, 325, fill=WHITE, width=2)

            # base dot
            drw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=WHITE)
        except Exception:
            pass

    # ---------- status ----------
    def _status(self):
        # This now reads from the non-blocking cache
        with _STATUS_LOCK:
            st = _STATUS_CACHE.copy()
        self._last_status = st
        return st
    # ---------- Still images ----------
    def take_still_photo(self):
        """Handler for KEY3 to capture and display a single photo."""
        if self._busy or self.state != self.HOME:
            return

        self._busy = True
        try:
            self._draw_center("Capturing...", sub="Please wait")

            # Call the new endpoint in the Flask app
            image_data = _http_post_and_get_image(f"{LOCAL}/capture_still")

            if image_data:
                # Success! Display the captured image.
                self._draw_center("Success!", sub="Showing image...")
                time.sleep(0.5)

                # Load the image data into PIL
                img = Image.open(io.BytesIO(image_data))

                # --- Start of new resizing logic ---

                # Create a proportionally scaled thumbnail. This modifies the image in-place.
                img.thumbnail((WIDTH, HEIGHT), Image.LANCZOS)

                # Create a new black background image of the correct screen size.
                background = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))

                # Calculate the position to paste the thumbnail in the center.
                paste_x = (WIDTH - img.width) // 2
                paste_y = (HEIGHT - img.height) // 2

                # Paste the thumbnail onto the black background.
                background.paste(img, (paste_x, paste_y))

                # Display the final composite image.
                self._present(background)

                # --- End of new resizing logic ---

                time.sleep(5) # Display for 5 seconds
            else:
                # Failure
                self._draw_center("Capture Failed")
                time.sleep(2)

        finally:
            self._busy = False
            # Return to the home screen and force a redraw
            self.state = self.HOME
            self.render(force=True)
    # ------- Capturing Screen ------
    def _draw_capturing_screen(self):
        """Draws the active timelapse status screen."""
        st = self._status()
        if not st: return

        img = self._blank()
        drw = ImageDraw.Draw(img)

        # 1. Get data from status
        frames = st.get("frames", 0)
        start_ts = st.get("start_ts")
        end_ts = st.get("end_ts")

        # 2. Calculate progress and remaining time
        time_left_str = ""
        progress_pct = 0
        if start_ts and end_ts:
            now = time.time()
            total_duration = end_ts - start_ts
            elapsed = now - start_ts
            progress_pct = min(1.0, elapsed / total_duration) if total_duration > 0 else 0
            
            remaining_sec = int(end_ts - now)
            if remaining_sec > 0:
                mins, secs = divmod(remaining_sec, 60)
                time_left_str = f"{mins}m {secs:02d}s left"

        # 3. Draw UI elements
        drw.text((2, 2), "Capturing...", font=F_TITLE, fill=WHITE)
        drw.text((2, 20), f"Frames: {frames}", font=F_TEXT, fill=WHITE)
        if time_left_str:
            drw.text((2, 34), time_left_str, font=F_TEXT, fill=WHITE)
        
        # Draw Progress Bar
        if progress_pct > 0:
            bar_y = 52
            bar_width = int((WIDTH - 4) * progress_pct)
            drw.rectangle((2, bar_y, WIDTH - 2, bar_y + 8), outline=GRAY, fill=None)
            drw.rectangle((2, bar_y, 2 + bar_width, bar_y + 8), outline=None, fill=GREEN)

        # 4. Draw Menu Options
        options = ["Screen off", "Stop Timelapse"]
        y = 80
        for i, opt in enumerate(options):
            fill = BLUE if i == self.menu_idx else WHITE
            prefix = "> " if i == self.menu_idx else "  "
            drw.text((10, y), prefix + opt, font=F_TEXT, fill=fill)
            y += 16
            
        self._present(img)
    # ---------- wake wrapper ----------
    def _wrap_wake(self, fn):
        def inner():
            if self._screen_off:
                self._wake_screen(); return
            fn()
        return inner

    # ---------- buttons ----------
    def _mk_button(self, pin):
        try:
            return Button(pin, pull_up=True, bounce_time=0.08, pin_factory=PIN_FACTORY)
        except Exception:
            return None

    def _unbind_inputs(self):
        for b in (self.btn_key1, self.btn_key2, self.btn_key3,
                  self.js_up, self.js_down, self.js_left, self.js_right, self.js_push):
            if b:
                b.when_pressed = None
                b.when_released = None

    def _rebind_joystick(self):
        if self.rot_deg == 180:
            # Flipped Portrait: Up is Down, Down is Up, etc.
            m = dict(up=self._logical_down, right=self._logical_left,
                     down=self._logical_up, left=self._logical_right)
        else: # Landscape (90 degrees)
            m = dict(up=self._logical_left, right=self._logical_up,
                     down=self._logical_right, left=self._logical_down)

        if self.js_up:    self.js_up.when_pressed    = self._wrap_wake(m['up'])
        if self.js_right: self.js_right.when_pressed = self._wrap_wake(m['right'])
        if self.js_down:  self.js_down.when_pressed  = self._wrap_wake(m['down'])
        if self.js_left:  self.js_left.when_pressed  = self._wrap_wake(m['left'])
        if self.js_push:  self.js_push.when_pressed  = self._wrap_wake(self.ok)

    def _bind_inputs(self):
        # KEY1 toggles hotspot
        if self.btn_key1:
            self.btn_key1.when_pressed = self._wrap_wake(self.toggle_hotspot)
        # KEY2 shows AP connect info on demand
        if self.btn_key2:
            self.btn_key2.when_pressed = self._wrap_wake(self.show_ap_info)
        # KEY3 takes still photo
        if self.btn_key3:
                self.btn_key3.when_pressed = self._wrap_wake(self.take_still_photo) # Add this line
        # keep joystick driving menus (rotation-aware)
        self._rebind_joystick()
    def show_ap_info(self):
        """Show connect info on demand (KEY2).
        If AP is ON, show hotspot SSID + its IP.
        If AP is OFF, show current Wi‑Fi SSID (if any) and local IP so you can reach Flask.
        """
        if self._busy:
            return
        self._busy = True
        try:
            st = _http_json(AP_STATUS_URL) or {}
            ap_on = bool(st.get("on"))
            if ap_on:
                ssid = st.get("ssid") or st.get("name") or "Hotspot"
                ip   = st.get("ip") or ""
                ips  = st.get("ips") or []
                # Poll IP briefly if not yet assigned
                if not ip:
                    deadline = time.time() + 4.0
                    while time.time() < deadline and not ip:
                        time.sleep(0.3)
                        st2 = _http_json(AP_STATUS_URL) or {}
                        ip = st2.get("ip") or ""
                        if not ips:
                            ips = st2.get("ips") or []
                self._show_connect_url_modal(ssid, ip, ips)
                return
            # AP is OFF → show current Wi‑Fi network and LAN IP
            ssid = _current_wifi_ssid() or "Wi‑Fi"
            ips  = st.get("ips") or _local_ipv4s()
            ip   = ips[0] if ips else ""
            self._show_connect_url_modal(ssid, ip, ips)
        finally:
            self._busy = False
            if self.state != self.MODAL:
                self.render(force=True)

    # ---------- MODAL helpers (show URL until any key pressed) ----------
    def _bind_modal_inputs(self, handler):
        """Bind all keys/joystick to a single handler during modal screen."""
        for b in (self.btn_key1, self.btn_key2, self.btn_key3,
                  self.js_up, self.js_down, self.js_left, self.js_right, self.js_push):
            if b:
                b.when_pressed = handler

    def _modal_ack(self):
        # Dismiss modal and restore normal inputs
        self.state = self.HOME
        self._request_hard_clear()
        self._bind_inputs()
        self.render(force=True)

    def _show_connect_url_modal(self, ssid, ip, ips):
        """Show SSID, IP, and a scannable QR code until a key is pressed."""
        # First, determine the IP address to use.
        connect_ip = ip
        if not connect_ip:
            # try a fallback hint if ips list exists
            hint = (ips[0] if isinstance(ips, list) and ips else "")
            if hint:
                connect_ip = hint

        # If we have an IP, generate a QR code. Otherwise, show an error.
        if connect_ip:
            url = f"http://{connect_ip}:5050"

            # 1. Create a QRCode object with a smaller border (default is 4)
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=2, # <-- Reduced border
            )
            qr.add_data(url)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            
            # Resize it to fit nicely on the 128x128 screen
            qr_img = qr_img.resize((96, 96), Image.NEAREST)

            # Create a new blank image to draw on
            img = self._blank()
            img.paste(qr_img, (16, 0)) # Paste QR code at the very top

            # Draw the human-readable text underneath
            drw = ImageDraw.Draw(img)
            ssid_text = f"SSID: {ssid or 'Hotspot'}"
            # 2. Add the port number to the displayed IP address
            ip_text = f"IP: {connect_ip}:5050"
            prompt_text = "Press any key..."

            w_ssid = self._text_w(F_SMALL, ssid_text)
            w_ip = self._text_w(F_SMALL, ip_text)
            w_prompt = self._text_w(F_SMALL, prompt_text)

            # 3. Adjust Y-coordinates to move text up slightly
            drw.text(((WIDTH - w_ssid) // 2, 98), ssid_text, font=F_SMALL, fill=WHITE)
            drw.text(((WIDTH - w_ip) // 2, 108), ip_text, font=F_SMALL, fill=WHITE)
            drw.text(((WIDTH - w_prompt) // 2, 118), prompt_text, font=F_SMALL, fill=GRAY)
            
            self._present(img)

        else:
            # If no IP is available, just show the text-based error as before
            lines = [f"SSID: {ssid or 'Not Connected'}", "IP: (unavailable)", "", "Press any key…"]
            self._draw_center("No Connection", "\n".join(lines))

        # Set the modal state and bind inputs to dismiss the screen
        self.state = self.MODAL
        self._bind_modal_inputs(self._modal_ack)

    # ---------- logical joystick actions ----------
    def _logical_up(self):
        if self.state in (self.HOME, self.SETTINGS_MENU, self.SCHED_LIST, self.TL_CONFIRM, self.SCH_CONFIRM, self.SCHED_DEL_CONFIRM, self.SHUTDOWN_CONFIRM):
            self.nav(-1)
        elif self.state == self.CAPTURING:
            self.menu_idx = (self.menu_idx - 1 + 2) % 2
            self.render()
        elif self.state == self.SCH_DATE:
            self.sch_date = self.sch_date + timedelta(days=1); self.render()
        elif self.state == self.STILLS_VIEWER:
            # This exits the viewer and returns to the home menu
            self.state = self.HOME
            self.render()
        else:
            self.adjust(+1)
            
    def _logical_down(self):
        if self.state in (self.HOME, self.SETTINGS_MENU, self.SCHED_LIST, self.TL_CONFIRM, self.SCH_CONFIRM, self.SCHED_DEL_CONFIRM, self.SHUTDOWN_CONFIRM):
            self.nav(+1)
        elif self.state == self.CAPTURING:
            self.menu_idx = (self.menu_idx + 1) % 2
            self.render()
        elif self.state == self.SCH_DATE:
            self.sch_date = self.sch_date - timedelta(days=1); self.render()
        elif self.state == self.STILLS_VIEWER:
            # This also exits the viewer
            self.state = self.HOME
            self.render()
        else:
            self.adjust(-1)

    def _logical_left(self):
        if self.state in (self.TL_INT, self.TL_HR, self.TL_MIN, self.SCH_INT, self.SCH_SH, self.SCH_SM, self.SCH_EH, self.SCH_EM):
            self.adjust(-10)
        elif self.state == self.SCH_DATE:
            self.sch_date = self.sch_date - timedelta(days=10); self.render()
        elif self.state in (self.TL_CONFIRM, self.SCH_CONFIRM, self.SCHED_DEL_CONFIRM, self.SHUTDOWN_CONFIRM):
            self.confirm_idx = 1 - self.confirm_idx; self.render()
        elif self.state == self.STILLS_VIEWER:
            if self.stills_list:
                self.stills_idx = (self.stills_idx - 1) % len(self.stills_list)
                self.render()
            
    def _logical_right(self):
        if self.state in (self.TL_INT, self.TL_HR, self.TL_MIN, self.SCH_INT, self.SCH_SH, self.SCH_SM, self.SCH_EH, self.SCH_EM):
            self.adjust(+10)
        elif self.state == self.SCH_DATE:
            self.sch_date = self.sch_date + timedelta(days=10); self.render()
        elif self.state in (self.TL_CONFIRM, self.SCH_CONFIRM, self.SCHED_DEL_CONFIRM, self.SHUTDOWN_CONFIRM):
            self.confirm_idx = 1 - self.confirm_idx; self.render()
        elif self.state == self.STILLS_VIEWER:
            if self.stills_list:
                self.stills_idx = (self.stills_idx + 1) % len(self.stills_list)
                self.render()

    # ---------- screen power ----------
    def _sleep_screen(self):
        self._screen_off = True
        try:
            # Create the flag file to signal that the screen should be off
            if not os.path.exists(LCD_OFF_FLAG):
                open(LCD_OFF_FLAG, 'a').close()
            if self.bl is not None: self.bl.value = 0.0
        except Exception: pass
        self._clear()

    def _wake_screen(self):
        try:
            if os.path.exists(LCD_OFF_FLAG):
                os.remove(LCD_OFF_FLAG)
            if self.bl is not None: self.bl.value = 1.0
        except Exception: pass
        self._screen_off = False
        self.state = self.HOME
        self.menu_idx = 0
        self._need_home_clear = True
        self.render(force=True)

    # ---------- state helpers ----------
    def nav(self, delta):
        if self._busy: return
        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            self.menu_idx = (self.menu_idx + delta) % len(items)
            self.render()
        elif self.state == self.SETTINGS_MENU:
            # This block handles navigation for the new menu
            self.menu_idx = (self.menu_idx + delta) % len(self.settings_menu_items)
            self.render()
        elif self.state == self.SCHED_LIST:
            n_items = 2 + len(self._sch_rows)
            self.menu_idx = (self.menu_idx + delta) % max(1, n_items)
            self.render()
        elif self.state in (self.TL_CONFIRM, self.SCH_CONFIRM, self.SCHED_DEL_CONFIRM, self.SHUTDOWN_CONFIRM):
            self.confirm_idx = 1 - self.confirm_idx; self.render()
        else:
            self.adjust(-1 if delta > 0 else +1)

    def adjust(self, delta):
        if self._busy: return
        # Timelapse wizard
        if self.state == self.TL_INT:
            step = 10 if abs(delta) >= 10 else 1
            self.wz_interval = max(1, self.wz_interval + (step if delta>0 else -step))
        elif self.state == self.TL_HR:
            step = 10 if abs(delta) >= 10 else 1
            self.tl_hours = max(0, min(999, self.tl_hours + (step if delta>0 else -step)))
        elif self.state == self.TL_MIN:
            step = 10 if abs(delta) >= 10 else 1
            self.tl_mins = max(0, min(59, self.tl_mins + (step if delta>0 else -step)))
        # Schedule wizard
        elif self.state == self.SCH_INT:
            step = 10 if abs(delta) >= 10 else 1
            self.wz_interval = max(1, self.wz_interval + (step if delta>0 else -step))
        elif self.state == self.SCH_DATE:
            self.sch_date = self.sch_date + (timedelta(days=1) if delta>0 else timedelta(days=-1))
        elif self.state == self.SCH_SH:
            step = 10 if abs(delta) >= 10 else 1
            self.sch_start_h = (self.sch_start_h + (step if delta>0 else -step)) % 24
        elif self.state == self.SCH_SM:
            step = 10 if abs(delta) >= 10 else 1
            self.sch_start_m = (self.sch_start_m + (step if delta>0 else -step)) % 60
        elif self.state == self.SCH_EH:
            step = 10 if abs(delta) >= 10 else 1
            self.sch_end_h = (self.sch_end_h + (step if delta>0 else -step)) % 24
        elif self.state == self.SCH_EM:
            step = 10 if abs(delta) >= 10 else 1
            self.sch_end_m = (self.sch_end_m + (step if delta>0 else -step)) % 60
        elif self.state in (self.TL_ENC, self.SCH_ENC):
            if abs(delta) >= 1: self.wz_encode = not self.wz_encode
        self.render()

    def ok(self):
        if self._busy or self._screen_off or self.state == self.ENCODING or self.state == self.MODAL:
            return
        
        if self.state == self.CAPTURING:
            if self.menu_idx == 0: # Screen off
                self._draw_center_sleep_then_off()
            elif self.menu_idx == 1: # Stop Timelapse
                self.stop_capture()
            return

        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            sel = items[self.menu_idx]
            if sel == "Quick Start":
                self.quick_start()
            elif sel == "New Timelapse":
                self.start_tl_wizard()
            elif sel == "Schedules":
                self.open_schedules()
            elif sel == "View Stills":
                self._refresh_stills_list()
                self.state = self.STILLS_VIEWER
                self.render()
            elif sel == "Settings":
                self.state = self.SETTINGS_MENU
                self.menu_idx = 0
                self.render()
            return
        
        # elif self.state == self.STILLS_VIEWER:
        #     self.state = self.HOME
        #     self.render()
        #     return
        
        elif self.state == self.SETTINGS_MENU:
            sel = self.settings_menu_items[self.menu_idx]
            if sel == "Screen off":
                self._draw_center_sleep_then_off()
            elif sel == "Rotate display":
                self.toggle_rotation()
            elif sel == "Shutdown Camera":
                self.state = self.SHUTDOWN_CONFIRM
                self.confirm_idx = 1  # Default to "No"
                self.render()
            elif sel == "‹ Back":
                self.state = self.HOME
                self.menu_idx = 0
                self.render()
            return
        
        elif self.state == self.STILLS_VIEWER:
                self._fetch_and_draw_still()
                return
        
        # advance through TL or SCH wizard
        if self.state in (self.TL_INT, self.TL_HR, self.TL_MIN, self.TL_ENC):
            self.state += 1
            if self.state == self.TL_CONFIRM: self.confirm_idx = 0
            self.render(); return

        if self.state in (self.SCH_INT, self.SCH_DATE, self.SCH_SH, self.SCH_SM, self.SCH_EH, self.SCH_EM, self.SCH_ENC):
            self.state += 1
            if self.state == self.SCH_CONFIRM: self.confirm_idx = 0
            self.render(); return

        if self.state == self.TL_CONFIRM:
            if self.confirm_idx == 0: self.start_timelapse_now()
            else: self._abort_to_home()
            return

        if self.state == self.SCH_CONFIRM:
            if self.confirm_idx == 0: self.arm_schedule_with_times()
            else: self._abort_to_home()
            return

        if self.state == self.SCHED_LIST:
            if self.menu_idx == 0:
                self.state = self.HOME; self.menu_idx = 0; self.render()
            elif self.menu_idx == 1:
                self.start_schedule_wizard()
            else:
                idx = self.menu_idx - 2
                if 0 <= idx < len(self._sch_rows):
                    self._selected_sched = self._sch_rows[idx][0]  # schedule id
                    self.state = self.SCHED_DEL_CONFIRM
                    self.confirm_idx = 1  # default to "No"
                    self.render()
            return

        if self.state == self.SCHED_DEL_CONFIRM:
            if self.confirm_idx == 0 and getattr(self, "_selected_sched", None):
                sid = str(self._selected_sched)
                self._busy = True
                self._draw_center("Deleting…")

                ok_flag = _delete_schedule_backend(sid)
                time.sleep(0.1)

                success = False
                deadline = time.time() + 1.2
                while time.time() < deadline:
                    rows = _read_schedules()
                    if not any(str(rid) == sid for rid, _ in rows):
                        success = True
                        break
                    time.sleep(0.1)

                self._busy = False
                self._draw_center("Deleted" if (success or ok_flag) else "Failed")
                time.sleep(0.4)

                self._selected_sched = None
                self._request_hard_clear()
                self.state = self.SCHED_LIST
                self.menu_idx = min(max(self.menu_idx, 0), 1 + len(self._sch_rows))
                self.render(force=True)
                return

            self._selected_sched = None
            self._reload_schedules_view()
            return
            
        if self.state == self.SHUTDOWN_CONFIRM:
            if self.confirm_idx == 0: # User selected "Yes"
                self._draw_center("Shutting down...", "")
                time.sleep(1) # Give user time to read
                _http_post_form(f"{LOCAL}/shutdown", {})
                time.sleep(30) 
            else: # User selected "No"
                self._abort_to_home()
            return

    def _abort_to_home(self):
        self._draw_center("Discarded"); time.sleep(0.5)
        self.state = self.HOME; self.menu_idx = 0; self._request_hard_clear(); self.render()
    # Add these new methods to the UI class

    def _fetch_and_draw_still(self):
        """Fetches and draws a still image using the known-working logic."""
        if not self.stills_list:
            self._refresh_stills_list()
            if not self.stills_list:
                self._draw_center("No Stills Found", sub="Press joystick to exit.")
                return


        filename = self.stills_list[self.stills_idx]
        image_data = None
        
        try:
            safe_filename = quote(filename)
            url = f"{LOCAL}/stills/{safe_filename}"
            with urlopen(url, timeout=5.0) as r:
                if r.status == 200:
                    image_data = r.read()
        except Exception as e:
            log(f"Failed to fetch still '{filename}': {e}")

        if image_data:
            try:
                # --- Start of known-working logic from take_still_photo ---
                img = Image.open(io.BytesIO(image_data))

                # Create a proportionally scaled thumbnail using the correct constant.
                img.thumbnail((WIDTH, HEIGHT), Image.LANCZOS)

                background = Image.new('RGB', (WIDTH, HEIGHT), (0, 0, 0))

                paste_x = (WIDTH - img.width) // 2
                paste_y = (HEIGHT - img.height) // 2

                background.paste(img, (paste_x, paste_y))
                # --- End of known-working logic ---

                drw = ImageDraw.Draw(background)
                page_text = f"{self.stills_idx + 1} of {len(self.stills_list)}"
                footer_w = self._text_w(F_SMALL, page_text)
                drw.text(((WIDTH - int(footer_w)) // 2, HEIGHT - 12), page_text, font=F_SMALL, fill=WHITE)
                
                self._present(background)

            except Exception as e:
                log(f"Error rendering image '{filename}': {e}")
                self._draw_center("Render Failed", sub=filename)
        else:
            self._draw_center("Load Failed", sub=filename)

    def _refresh_stills_list(self):
        """Fetch the list of stills from the backend and reset index."""
        try:
            files = _http_json(STILLS_LIST_URL, timeout=1.5)
            if isinstance(files, list):
                self.stills_list = files
                self.stills_idx = 0 if files else 0
            else:
                self.stills_list = []
        except Exception:
            self.stills_list = []


    def _render_stills_viewer(self):
        """Renders the current still image."""
        self._fetch_and_draw_still()
        
    # ---------- actions ----------
    def quick_start(self):
        if self._busy: return
        self._busy = True
        self._draw_center("Starting...")
        ok = _http_post_form(START_URL, {"interval": 10})
        self._draw_center("Started" if ok else "Failed", "Quick Start")
        time.sleep(0.6)
        self._busy = False
        self._request_hard_clear()
        self.render(force=True)

    def stop_capture(self):
        if self._busy: return
        self._busy = True
        self._draw_center("Stopping...")
        ok = _http_post_form(STOP_URL, {})
        self._draw_center("Stopped" if ok else "Failed")
        time.sleep(0.6)
        self._busy = False
        self.state = self.HOME; self.menu_idx = 0
        self._request_hard_clear()
        self.render(force=True)

    # --- New Timelapse (start now; duration hr/min) ---
    def start_tl_wizard(self):
        if self._busy: return
        self.wz_interval = 10
        self.tl_hours = 0
        self.tl_mins  = 0
        self.wz_encode = True
        self.confirm_idx = 0
        self._request_hard_clear()
        self.state = self.TL_INT
        self.render()

    def start_timelapse_now(self):
        if self._busy: return
        self._busy = True
        try:
            dur_hr = max(0, int(self.tl_hours))
            dur_min = max(0, int(self.tl_mins))
            if dur_hr == 0 and dur_min == 0: dur_min = 1
            start_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
            self._draw_center("Starting…")
            ok = _http_post_form(SCHED_ARM_URL, {
                "start_local": start_local,
                "duration_hr":  str(dur_hr),
                "duration_min": str(dur_min),
                "interval":     str(self.wz_interval),
                "fps":          "24",
                "auto_encode":  "on" if self.wz_encode else "",
                "sess_name":    "",
            })
            self._draw_center("Scheduled" if ok else "Failed", "Starts now")
            time.sleep(0.8)
        finally:
            self._busy = False
            self.state = self.HOME; self.menu_idx = 0
            self._request_hard_clear()
            self.render(force=True)

    # --- New Schedule (date + start/end clock times) ---
    def start_schedule_wizard(self):
        if self._busy: return
        now = datetime.now()
        self.wz_interval = 10
        self.sch_date = now.date()
        self.sch_start_h  = now.hour
        self.sch_start_m  = (now.minute + 1) % 60
        self.sch_end_h    = (now.hour + 1) % 24
        self.sch_end_m    = self.sch_start_m
        self.wz_encode = True
        self.confirm_idx = 0
        self._request_hard_clear()
        self.state = self.SCH_INT
        self.render()

    def arm_schedule_with_times(self):
        if self._busy: return
        self._busy = True
        try:
            start = datetime(
                self.sch_date.year, self.sch_date.month, self.sch_date.day,
                self.sch_start_h, self.sch_start_m, 0
            )
            end   = datetime(
                self.sch_date.year, self.sch_date.month, self.sch_date.day,
                self.sch_end_h, self.sch_end_m, 0
            )
            if end <= start:
                end += timedelta(days=1)
            dur = end - start
            mins = max(1, int(dur.total_seconds() // 60))
            dur_hr, dur_min = divmod(mins, 60)

            self._draw_center("Arming…")
            ok = _post_schedule_arm(
                start_dt=start,
                duration_hr=dur_hr,
                duration_min=dur_min,
                interval_s=int(self.wz_interval),
                auto_encode=bool(self.wz_encode),
                fps=24,
                sess_name=""
            )
            self._draw_center("Scheduled" if ok else "Failed",
                              f"{start.strftime('%y-%m-%d %H:%M')}")
            time.sleep(0.8)
        finally:
            self._busy = False
            self.state = self.HOME; self.menu_idx = 0
            self._request_hard_clear()
            self.render(force=True)

    def open_schedules(self):
        if self._busy: return
        self._request_hard_clear()
        self.state = self.SCHED_LIST
        self.menu_idx = 0
        self.render()

    def toggle_rotation(self):
        if self._busy: return
        self._busy = True
        self._unbind_inputs()
        try:
            if self.bl is not None: self.bl.value = 0.0
            self._panel_off()

            self._hard_clear()
            self.rot_deg = 90 if self.rot_deg == 180 else 180
            _save_prefs({"rot_deg": self.rot_deg})

            self._lcd_reinit()
            self._hard_clear()
            self._panel_on()
            if self.bl is not None: self.bl.value = 1.0

            img = self._blank()
            drw = ImageDraw.Draw(img)
            
            if self.rot_deg == 1800:
                # Text is now split with \n for two lines
                title_text = "Rotation set to:\nPortrait"
                icon_to_draw = IMG_ICON_PORTRAIT
            else:
                title_text = "Rotation set to:\nLandscape"
                icon_to_draw = IMG_ICON_LANDSCAPE

            # --- Centering logic for multi-line text and larger icon ---
            y_pos = 15 # Starting y-position for the first line of text
            for line in title_text.split('\n'):
                line_w = self._text_w(F_TITLE, line)
                drw.text(((WIDTH - int(line_w)) // 2, y_pos), line, font=F_TITLE, fill=WHITE)
                y_pos += 16 # Move down for the next line

            # Paste the pre-colored icon using its own alpha channel as the mask
            icon_pos = ((WIDTH - icon_to_draw.width) // 2, y_pos + 10)
            img.paste(icon_to_draw, icon_pos, mask=icon_to_draw)
            
            self._present(img)
            
            self._bind_inputs()
            self._request_hard_clear()
            time.sleep(2) # Keep on screen a little longer
            self.state = self.HOME
            self.render(force=True)
        finally:
            self._busy = False

    def toggle_hotspot(self):
        if self._busy:
            return
        self._busy = True
        self._draw_center("Toggling AP…")

        def worker():
            try:
                # Give nmcli time and be tolerant of timeouts
                ok = _http_post_form(AP_TOGGLE_URL, {}, timeout=4.0)
                if not ok:
                    # Even if POST timed out, it may still succeed shortly.
                    deadline = time.time() + 3.0
                    success = False
                    while time.time() < deadline:
                        st = _http_json(AP_STATUS_URL) or {}
                        if isinstance(st.get("on"), bool):
                            success = True
                            break
                        time.sleep(0.2)
                    if not success:
                        self._draw_center("AP toggle failed")
                        time.sleep(0.6)
                        return

                # Confirm final state (with a few retries while NM settles)
                st = {}
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    st = _http_json(AP_STATUS_URL) or {}
                    if "on" in st:
                        break
                    time.sleep(0.2)

                if st.get("on"):
                    ssid = st.get("ssid") or st.get("name") or "Hotspot"
                    # Poll IP briefly if not yet assigned
                    ip = st.get("ip") or ""
                    ips = st.get("ips") or []
                    if not ip:
                        ip_deadline = time.time() + 6.0
                        while time.time() < ip_deadline and not ip:
                            time.sleep(0.4)
                            st2 = _http_json(AP_STATUS_URL) or {}
                            ip = st2.get("ip") or ""
                            ips = st2.get("ips") or ips

                    # Show sticky modal with URL until key press
                    self._show_connect_url_modal(ssid, ip, ips)
                else:
                    self._draw_center("Hotspot OFF")
                    time.sleep(0.8)
            finally:
                # Allow input again (modal will rebind on show)
                self._busy = False
                if self.state != self.MODAL:
                    self.render(True)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- render ----------
    def _draw_confirm_tl(self, interval_s, h, m, auto_encode, hi):
        lines = [
            f"Interval:  {interval_s}s",
            f"Duration:  {h}h{m:02d}m",
            f"Auto-enc.: {'Yes' if auto_encode else 'No'}",
            "",
        ]
        yes = "[Yes]" if hi == 0 else " Yes "
        no  = "[No] " if hi == 1 else " No  "
        lines.append(f"{yes}    {no}")
        self._draw_lines(lines, title="Confirm",
                         footer="UP/DOWN choose, OK select",
                         highlight_idxes=set(), dividers=False)

    def _draw_confirm_sch(self, interval_s, sch_date, sh, sm, eh, em, auto_encode, hi):
        lines = [
            f"Interval:  {interval_s}s",
            f"Date:      {sch_date.strftime('%y-%m-%d')}",
            f"Start:     {sh:02d}:{sm:02d}",
            f"End:       {eh:02d}:{em:02d}",
            f"Auto-enc.: {'Yes' if auto_encode else 'No'}",
            "",
        ]
        yes = "[Yes]" if hi == 0 else " Yes "
        no  = "[No] " if hi == 1 else " No  "
        lines.append(f"{yes}    {no}")
        self._draw_lines(lines, title="Confirm",
                         footer="UP/DOWN choose, OK select",
                         highlight_idxes=set(), dividers=False)

    def _format_sched_lines(self, st: dict):
        interval = int(st.get("interval", 10))
        st_ts = int(st.get("start_ts", 0)); en_ts = int(st.get("end_ts", 0))
        if st_ts and en_ts:
            sd = datetime.fromtimestamp(st_ts); ed = datetime.fromtimestamp(en_ts)
            line1 = f"{interval}s • {sd.strftime('%y-%m-%d')}"
            enc = "on" if bool(st.get("auto_encode", False)) else "off"
            line2 = f"{sd.strftime('%H:%M')}–{ed.strftime('%H:%M')} • fps{int(st.get('fps',24))} • enc:{enc}"
        else:
            line1 = f"{interval}s • (no date)"
            line2 = "(unscheduled)"
        return line1, line2

    def _render_home(self):
        self._maybe_hard_clear()
        if self._need_home_clear:
            self._hard_clear()
            self._need_home_clear = False

        st = self._status() or {}
        if st.get("encoding"):
            self.state = self.ENCODING
            self._draw_encoding(self._spin_idx)
            return

        status = "Idle"
        items = []

        if st.get("active"):
            status = "Capturing"
            items = ["Stop capture", "Screen off"] 
        else:
            items = self.menu_items

        self._home_items = items
        if self.menu_idx >= len(items):
            self.menu_idx = max(0, len(items) - 1)

        # --- Start of New Icon Rendering Logic ---
        img = self._blank()
        drw = ImageDraw.Draw(img)
        y = 2

        drw.text((2, y), status, font=F_TITLE, fill=WHITE); y += 18
        
        icons = {
            "Quick Start": IMG_ICON_PLAY,
            "New Timelapse": IMG_ICON_CAMERA,
            "Schedules": IMG_ICON_CALENDAR,
            "View Stills": IMG_ICON_PHOTO,
            "Settings": IMG_ICON_SETTINGS
        }

        for i, txt in enumerate(items):
            fill = BLUE if i == self.menu_idx else WHITE
            icon_image = icons.get(txt)
            
            if icon_image:
                icon_pos = (5, y)
                text_pos = (28, y)
                img.paste(icon_image, icon_pos, mask=icon_image)
                drw.text(text_pos, txt, font=F_TEXT, fill=fill)
            else:
                drw.text((10, y), txt, font=F_TEXT, fill=fill)
            
            y += 20
        
        now_str = datetime.now().strftime("%H:%M:%S")
        footer_text = f"{now_str}"
        footer_w = self._text_w(F_SMALL, footer_text)
        drw.text(((WIDTH - int(footer_w)) // 2, HEIGHT - 12), footer_text, font=F_SMALL, fill=WHITE)
        
        self._present(img)
        # --- End of New Icon Rendering Logic ---

    def _render_wz(self):
        self._maybe_hard_clear()
        if self.state == self.TL_INT:
            self._draw_wizard_page("Interval (s)", f"{self.wz_interval}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.TL_HR:
            self._draw_wizard_page("Duration hours", f"{self.tl_hours}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.TL_MIN:
            self._draw_wizard_page("Duration mins", f"{self.tl_mins:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.TL_ENC:
            self._draw_wizard_page("Auto-encode", "Yes" if self.wz_encode else "No",
                                   tips=["UP/DOWN toggle", "OK next"])
        elif self.state == self.SCH_INT:
            self._draw_wizard_page("Interval (s)", f"{self.wz_interval}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.SCH_DATE:
            self._draw_wizard_page("Date", self.sch_date.strftime("%y-%m-%d"),
                                   tips=["UP/DOWN ±1 day, LEFT/RIGHT ±10 days", "OK next"])
        elif self.state == self.SCH_SH:
            self._draw_wizard_page("Start hour", f"{self.sch_start_h:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.SCH_SM:
            self._draw_wizard_page("Start minute", f"{self.sch_start_m:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.SCH_EH:
            self._draw_wizard_page("End hour", f"{self.sch_end_h:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.SCH_EM:
            self._draw_wizard_page("End minute", f"{self.sch_end_m:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.SCH_ENC:
            self._draw_wizard_page("Auto-encode", "Yes" if self.wz_encode else "No",
                                   tips=["UP/DOWN toggle", "OK next"])

    def render(self, force=False):
        if self._screen_off or self._busy or self.state == self.MODAL:
            return
        try:
            if self.state == self.CAPTURING:
                self._draw_capturing_screen()
                return
            if self.state == self.ENCODING:
                self._draw_encoding(self._spin_idx); return

            if self.state == self.HOME:
                self._render_home(); return
            
            if self.state == self.SETTINGS_MENU:
                img = self._blank()
                drw = ImageDraw.Draw(img)
                y = 2

                drw.text((2, y), "Settings", font=F_TITLE, fill=WHITE); y += 18
                footer_text = "OK select, UP/DOWN nav"
                footer_w = self._text_w(F_SMALL, footer_text)
                drw.text(((WIDTH - int(footer_w)) // 2, HEIGHT - 12), footer_text, font=F_SMALL, fill=WHITE)

                icons = {
                    "Screen off": IMG_ICON_SCREEN_OFF,
                    "Rotate display": IMG_ICON_ROTATE,
                    "Shutdown Camera": IMG_ICON_SHUTDOWN
                }

                for i, txt in enumerate(self.settings_menu_items):
                    fill = BLUE if i == self.menu_idx else WHITE
                    icon_image = icons.get(txt)

                    if icon_image:
                        # --- SIMPLIFIED METHOD ---
                        icon_pos = (5, y)
                        text_pos = (28, y)
                        # Paste the pre-colored icon using its own alpha channel as the mask
                        img.paste(icon_image, icon_pos, mask=icon_image)
                        drw.text(text_pos, txt, font=F_TEXT, fill=fill)
                    else:
                        drw.text((10, y), txt, font=F_TEXT, fill=fill)

                    y += 20

                self._present(img)
                return
            
            if self.state == self.STILLS_VIEWER:
                self._fetch_and_draw_still()
                return

            if self.state in (
                self.TL_INT, self.TL_HR, self.TL_MIN, self.TL_ENC,
                self.SCH_INT, self.SCH_DATE, self.SCH_SH, self.SCH_SM, self.SCH_EH, self.SCH_EM, self.SCH_ENC,
            ):
                self._render_wz(); return

            if self.state == self.TL_CONFIRM:
                self._draw_confirm_tl(self.wz_interval, self.tl_hours, self.tl_mins,
                                      self.wz_encode, self.confirm_idx); return

            if self.state == self.SCH_CONFIRM:
                self._draw_confirm_sch(self.wz_interval, self.sch_date,
                                       self.sch_start_h, self.sch_start_m,
                                       self.sch_end_h, self.sch_end_m,
                                       self.wz_encode, self.confirm_idx); return

            if self.state == self.SCHED_LIST:
                self._maybe_hard_clear()
                rows = _read_schedules()
                self._sch_rows = rows[:]  # keep same order

                lines = ["‹ Back", "+ New Schedule"]
                highlight = set()
                now_ts = int(time.time())

                max_sched_to_show = 3
                divider_after = set()

                for idx, (_, st) in enumerate(rows[:max_sched_to_show]):
                    l1, l2 = self._format_sched_lines(st)
                    st_ts = int(st.get("start_ts",0)); en_ts = int(st.get("end_ts",0))
                    tag = "now" if (st_ts <= now_ts < en_ts) else "next"
                    l2 = f"{l2}  [{tag}]"

                    lines.append(l1)
                    lines.append(l2)
                    divider_after.add(len(lines)-1)

                if self.menu_idx >= 2:
                    sched_i = self.menu_idx - 2
                    top = 2 + sched_i*2
                    if top < len(lines):
                        highlight.update({top, top+1})
                hi_ok = {0} if self.menu_idx == 0 else ({1} if self.menu_idx == 1 else highlight)

                self._draw_lines(
                    lines, title="Schedules",
                    footer="OK select",
                    highlight_idxes=hi_ok,
                    dividers=True,
                    divider_after=divider_after
                )
                return

            if self.state == self.SCHED_DEL_CONFIRM:
                yes = "[Yes]" if self.confirm_idx == 0 else " Yes "
                no  = "[No] " if self.confirm_idx == 1 else " No  "
                lines = ["Delete this schedule?", "", f"{yes}    {no}"]
                self._draw_lines(lines, title="Confirm delete",
                                 footer="UP/DOWN choose, OK select",
                                 highlight_idxes=set(), dividers=False)
                return
            
            if self.state == self.SHUTDOWN_CONFIRM:
                yes = "[Yes]" if self.confirm_idx == 0 else " Yes "
                no  = "[No] " if self.confirm_idx == 1 else " No  "
                lines = ["Shut down the Pi?", "", f"{yes}    {no}"]
                self._draw_lines(lines, title="Confirm shutdown",
                                footer="OK select, L/R choose",
                                highlight_idxes=set(), dividers=False)
                return

            self._clear()
        except Exception as e:
            log("render error:", repr(e))

    # show "sleeping" for a beat, then turn panel off
    def _draw_center_sleep_then_off(self):
        prev_state = self.state
        self.state = None
        self._clear()
        self._draw_center("Screen off", "Press any key\n to wake up")
        time.sleep(2.5)
        self._sleep_screen()
        self.state = prev_state

# ----------------- main loop -----------------
# ----------------- main loop -----------------
def main():
    ui = UI()
    poll_thread = threading.Thread(target=_poll_status_worker, daemon=True)
    poll_thread.start()
    time.sleep(1.0)

    while True:
        # --- THIS IS THE NEW LOGIC ---
        # Check for the flag file to control screen power
        if os.path.exists(LCD_OFF_FLAG):
            if not ui._screen_off:
                ui._sleep_screen()
            time.sleep(2) # Sleep longer when screen is off
            continue # Skip the rest of the loop
        elif ui._screen_off:
            # If flag is gone but screen is off, wake it
            ui._wake_screen()
        # --- END OF NEW LOGIC ---

        if ui._busy or ui.state == ui.MODAL:
            time.sleep(0.1)
            continue

        st = ui._status()
        is_active = st.get("active", False)
        is_encoding = st.get("encoding", False)

        if is_encoding:
            if ui.state != UI.ENCODING:
                ui.state = UI.ENCODING
        elif is_active:
            if ui.state != UI.CAPTURING:
                ui.state = UI.CAPTURING
                ui.menu_idx = 0
        elif ui.state in (UI.CAPTURING, UI.ENCODING):
             ui.state = UI.HOME
             ui.menu_idx = 0

        ui.render()

        if ui.state == UI.ENCODING:
            ui._spin_idx = (ui._spin_idx + 1) % len(SPINNER)

        time.sleep(1.0)

if __name__ == "__main__":
    try:
        if DEBUG:
            print("DEBUG on", file=sys.stderr, flush=True)
        # Ensure stdout/stderr are not buffered under systemd
        try:
            import sys as _sys
            _sys.stdout.reconfigure(line_buffering=True)
            _sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass
        main()
    except KeyboardInterrupt:
        pass