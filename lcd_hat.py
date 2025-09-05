#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, time, json, traceback, threading
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# -------- Pin factory (prefer lgpio, fall back to RPi.GPIO) --------
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
KEY1     = int(os.environ.get("LCD_KEY1",     "21"))  # Up
KEY2     = int(os.environ.get("LCD_KEY2",     "20"))  # OK
KEY3     = int(os.environ.get("LCD_KEY3",     "16"))  # Down
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
TEST_URL        = f"{LOCAL}/test_capture"
SCHED_ARM_URL   = f"{LOCAL}/schedule/arm"
SCHED_FILE      = "/home/pi/timelapse/schedule.json"

# ----------------- Preferences (rotation) -----------------
PREFS_FILE = "/home/pi/timelapse/lcd_prefs.json"
def _load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            p = json.load(f)
            if isinstance(p, dict):
                return p
    except Exception:
        pass
    return {"rot_deg": 0}
def _save_prefs(p):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(p, f)
    except Exception:
        pass
try:
    ROT_DEG = 90 if int(_load_prefs().get("rot_deg", 0)) == 90 else 0
except Exception:
    ROT_DEG = 0

# ----------------- Imports & LCD init -----------------
from PIL import Image, ImageDraw, ImageFont
from gpiozero import Button, PWMLED
from luma.core.interface.serial import spi
from luma.lcd.device import st7735

# NOTE: calmer SPI (8 MHz) to reduce white-outs
def _mk_serial():
    return spi(port=SPI_PORT, device=SPI_DEVICE,
               gpio_DC=PIN_DC, gpio_RST=PIN_RST,
               bus_speed_hz=8_000_000)

def _mk_device(serial_obj):
    # raw device always rotation=0; we rotate frames in software
    return st7735(serial_obj, width=WIDTH, height=HEIGHT,
                  rotation=0, h_offset=0, v_offset=0, bgr=True)

try:
    serial = _mk_serial()
    device = _mk_device(serial)
except Exception:
    print("LCD device init failed:", file=sys.stderr); traceback.print_exc(); sys.exit(1)

# Backlight control (optional)
try:
    bl = PWMLED(PIN_BL)
    bl.value = 1.0
except Exception:
    bl = None

# Buttons
def _mk_button(pin):
    try:
        return Button(pin, pull_up=True, bounce_time=0.08, pin_factory=PIN_FACTORY)
    except Exception:
        return None

btn_up    = _mk_button(KEY1)     # side keys
btn_ok    = _mk_button(KEY2)
btn_down  = _mk_button(KEY3)
js_up     = _mk_button(JS_UP)    # joystick
js_down   = _mk_button(JS_DOWN)
js_left   = _mk_button(JS_LEFT)
js_right  = _mk_button(JS_RIGHT)
js_push   = _mk_button(JS_PUSH)

# ----------------- Fonts & colors -----------------
def _load_font(size_px):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size_px)
    except Exception:
        return ImageFont.load_default()

F_TITLE = _load_font(14)
F_TEXT  = _load_font(12)
F_SMALL = _load_font(10)
F_VALUE = _load_font(18)

WHITE=(255,255,255); GRAY=(140,140,140); CYAN=(120,200,255)
GREEN=(80,220,120);  YELL=(255,210,80);  BLUE=(90,160,220)
RED=(255,80,80)

_rot_pending_clear = False

# ----------------- Drawing core (lock + auto-reinit) -----------------
_draw_lock = threading.Lock()

def _lcd_reinit():
    """Recreate serial + device (togs reset line)"""
    global serial, device
    try:
        s = _mk_serial()
        d = _mk_device(s)
        serial = s
        device = d
        return True
    except Exception:
        return False

def _present(img):
    """Rotate frame if needed, serialize display, and auto-reinit on failure."""
    global _rot_pending_clear
    frame = img.rotate(90, expand=False, resample=Image.NEAREST) if ROT_DEG == 90 else img

    try:
        with _draw_lock:
            # ► If rotation just changed, hard clear twice to purge ghosting
            if _rot_pending_clear:
                device.display(Image.new("RGB", (device.width, device.height), (0,0,0)))
                time.sleep(0.03)
                device.display(Image.new("RGB", (device.width, device.height), (0,0,0)))
                time.sleep(0.03)
                _rot_pending_clear = False

            device.display(frame)
        return True
    except Exception:
        if _lcd_reinit():
            try:
                with _draw_lock:
                    device.display(frame)
                return True
            except Exception:
                return False
        return False

def _blank():   return Image.new("RGB", (device.width, device.height), (0,0,0))
def _clear():   _present(_blank())

def _text_w(font, txt):
    try:   return font.getlength(txt)
    except Exception:
        try:   return font.getsize(txt)[0]
        except Exception: return len(txt) * 6

def _draw_lines(lines, title=None, footer=None, highlight=-1, hints=True):
    img = _blank(); drw = ImageDraw.Draw(img); y = 2
    if title:
        drw.text((2,y), title, font=F_TITLE, fill=WHITE); y += 18
    for i, txt in enumerate(lines):
        drw.text((2,y), txt, font=F_TEXT, fill=(CYAN if i==highlight else WHITE)); y += 14
    if footer:
        drw.text((2, HEIGHT-12), footer, font=F_SMALL, fill=GRAY)
    if hints:
        drw.text((WIDTH-26,  8), "UP",   font=F_SMALL, fill=GRAY)
        drw.text((WIDTH-26, 54), "OK",   font=F_SMALL, fill=GRAY)
        drw.text((WIDTH-26, 98), "DOWN", font=F_SMALL, fill=GRAY)
    _present(img)

def _draw_center(msg, sub=None):
    img = _blank(); drw = ImageDraw.Draw(img); w = device.width
    tw = _text_w(F_TITLE, msg); drw.text(((w-int(tw))//2, 36), msg, font=F_TITLE, fill=WHITE)
    if sub:
        y = 60
        for line in sub.split("\n"):
            sw = _text_w(F_SMALL, line)
            drw.text(((w-int(sw))//2, y), line, font=F_SMALL, fill=GRAY); y += 14
    _present(img)

def _draw_wizard_page(title, value, tips=None, footer=None):
    img = _blank(); drw = ImageDraw.Draw(img)
    drw.text((2, 2), title, font=F_TITLE, fill=WHITE)
    val_str = str(value); tw = _text_w(F_VALUE, val_str)
    drw.text(((WIDTH - int(tw))//2, 40), val_str, font=F_VALUE, fill=CYAN)
    y = 80
    for line in (tips or []):
        drw.text((2, y), line, font=F_SMALL, fill=GRAY); y += 12
    if footer:
        drw.text((2, HEIGHT-12), footer, font=F_SMALL, fill=GRAY)
    _present(img)

SPINNER = ["-", "\\", "|", "/"]
def _draw_encoding(spin_idx=0):
    img = _blank(); drw = ImageDraw.Draw(img)
    msg = "Encoding..."; sp = SPINNER[spin_idx % len(SPINNER)]
    tw = _text_w(F_TITLE, msg); sw = _text_w(F_TITLE, sp)
    drw.text(((WIDTH-int(tw))//2, 40), msg, font=F_TITLE, fill=YELL)
    drw.text(((WIDTH-int(sw))//2, 62), sp,  font=F_TITLE, fill=YELL)
    _present(img)

# ----------------- HTTP helpers -----------------
def _http_json(url, timeout=1.2):
    try:
        with urlopen(Request(url, headers={"Cache-Control":"no-store"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None

def _http_post_form(url, data: dict, timeout=2.5):
    try:
        body = urlencode(data).encode("utf-8")
        req = Request(url, data=body, method="POST",
                      headers={"Content-Type":"application/x-www-form-urlencoded"})
        with urlopen(req, timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False

# ----------------- Schedules (local read) -----------------
def _read_schedules():
    try:
        with open(SCHED_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return sorted(data.items(), key=lambda kv: kv[1].get("start_ts", 0))
    except Exception:
        pass
    return []

# ----------------- Controller -----------------
class UI:
    HOME, WZ_INT, WZ_HR, WZ_MIN, WZ_ENC, WZ_CONFIRM, SCHED_LIST, ENCODING = range(8)

    def __init__(self):
        self.state = self.HOME
        self.menu_idx = 0
        self.menu_items = ["Quick Start", "New Timelapse", "Schedules", "Screen off"]  # base
        self._home_items = self.menu_items[:]

        # wizard values
        self.wz_interval = 10
        self.wz_hours    = 0
        self.wz_mins     = 0
        self.wz_encode   = True
        self.confirm_idx = 0  # 0=Yes, 1=No

        # status cache / spinner
        self._last_status = {}
        self._spin_idx = 0

        # screen power & busy guard
        self.screen_off = False
        self.busy = False

        self._bind_inputs()
        self.render(force=True)

    # --- status helper ---
    def _status(self):
        st = _http_json(STATUS_URL) or {}
        self._last_status = st
        return st

    # ---------- wake wrapper ----------
    def _wrap_wake(self, fn):
        def inner():
            if self.screen_off:
                self._wake_screen(); return
            fn()
        return inner

    # ---------- logical joystick actions (screen-relative) ----------
    def _logical_up(self):
        if self.state in (self.HOME, self.SCHED_LIST, self.WZ_CONFIRM):
            self.nav(-1)
        else:
            self.adjust(+1)

    def _logical_down(self):
        if self.state in (self.HOME, self.SCHED_LIST, self.WZ_CONFIRM):
            self.nav(+1)
        else:
            self.adjust(-1)

    def _logical_left(self):
        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN):
            self.adjust(-10)
        elif self.state == self.WZ_CONFIRM:
            self.confirm_idx = 1 - self.confirm_idx; self.render()

    def _logical_right(self):
        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN):
            self.adjust(+10)
        elif self.state == self.WZ_CONFIRM:
            self.confirm_idx = 1 - self.confirm_idx; self.render()

    # Map physical joystick pins to logical directions depending on ROT_DEG
    def _rebind_joystick(self):
        """
        Map physical joystick to on-screen directions.
        - 0° : identity (up->up, down->down, left->left, right->right)
        - 90°: flip axes (up<->down, left<->right)
        """
        if ROT_DEG == 0:
            m = {
                'up':    self._logical_up,
                'down':  self._logical_down,
                'left':  self._logical_left,
                'right': self._logical_right,
            }
        else:  # 90° CCW: flip each axis
            m = {
                'up':    self._logical_down,
                'down':  self._logical_up,
                'left':  self._logical_right,
                'right': self._logical_left,
            }

        if js_up:    js_up.when_pressed    = self._wrap_wake(m['up'])
        if js_down:  js_down.when_pressed  = self._wrap_wake(m['down'])
        if js_left:  js_left.when_pressed  = self._wrap_wake(m['left'])
        if js_right: js_right.when_pressed = self._wrap_wake(m['right'])
        if js_push:  js_push.when_pressed  = self._wrap_wake(self.ok)
    # ---------- input bindings ----------
    def _bind_inputs(self):
        # side keys (not rotated)
        if btn_up:   btn_up.when_pressed   = self._wrap_wake(lambda: self.nav(-1))
        if btn_down: btn_down.when_pressed = self._wrap_wake(lambda: self.nav(+1))
        if btn_ok:   btn_ok.when_pressed   = self._wrap_wake(self.ok)
        # joystick (orientation-aware)
        self._rebind_joystick()

    # ---------- screen power ----------
    def _sleep_screen(self):
        self.screen_off = True
        try:
            if bl is not None: bl.value = 0.0
        except Exception:
            pass
        _clear()

    def _wake_screen(self):
        try:
            if bl is not None: bl.value = 1.0
        except Exception:
            pass
        self.screen_off = False
        self.state = self.HOME
        self.menu_idx = 0
        self.render(force=True)

    # ---------- state helpers ----------
    def nav(self, delta):
        if self.busy: return
        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            self.menu_idx = (self.menu_idx + delta) % max(1, len(items))
            self.render()
        elif self.state == self.SCHED_LIST:
            self.menu_idx = max(0, self.menu_idx + delta)
            self.render()
        elif self.state == self.WZ_CONFIRM:
            self.confirm_idx = 1 - self.confirm_idx
            self.render()
        else:
            self.adjust(-1 if delta > 0 else +1)

    def adjust(self, delta):
        if self.busy: return
        if self.state == self.WZ_INT:
            self.wz_interval = max(1, self.wz_interval + delta)
        elif self.state == self.WZ_HR:
            self.wz_hours = max(0, min(999, self.wz_hours + delta))
        elif self.state == self.WZ_MIN:
            self.wz_mins = max(0, min(59, self.wz_mins + delta))
        elif self.state == self.WZ_ENC:
            if abs(delta) >= 1:
                self.wz_encode = not self.wz_encode
        self.render()

    def ok(self):
        if self.busy or self.screen_off: return
        if self.state == self.ENCODING:  return

        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            sel = items[self.menu_idx] if items else None
            if not sel: return
            if sel.startswith("Stop capture"):
                self.stop_capture()
            elif sel == "Quick Start":
                self.quick_start()
            elif sel == "New Timelapse":
                self.start_wizard()
            elif sel == "Schedules":
                self.open_schedules()
            elif sel == "Screen off":
                self._draw_center_sleep_then_off()
            elif sel.startswith("Rotate display"):
                self.toggle_rotation()
            return

        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
            self.state += 1
            if self.state == self.WZ_CONFIRM:
                self.confirm_idx = 0
            self.render()
            return

        if self.state == self.WZ_CONFIRM:
            if self.confirm_idx == 0:
                self.start_now_via_schedule()
            else:
                _draw_center("Discarded"); time.sleep(0.5)
                self.state = self.HOME; self.menu_idx = 0; self.render()
            return

        if self.state == self.SCHED_LIST:
            if self.menu_idx == 0:
                self.start_wizard()
            else:
                self.render()
            return

    # ---------- actions (busy-guarded) ----------
    def quick_start(self):
        if self.busy: return
        self.busy = True
        _draw_center("Starting...")
        ok = _http_post_form(START_URL, {"interval": 10})
        _draw_center("Started" if ok else "Failed", "Quick Start")
        time.sleep(0.6)
        self.busy = False
        self.render(force=True)

    def stop_capture(self):
        if self.busy: return
        self.busy = True
        _draw_center("Stopping...")
        ok = _http_post_form(STOP_URL, {})
        _draw_center("Stopped" if ok else "Failed")
        time.sleep(0.6)
        self.busy = False
        self.state = self.HOME; self.menu_idx = 0
        self.render(force=True)

    def start_wizard(self):
        if self.busy: return
        self.wz_interval = 10; self.wz_hours = 0; self.wz_mins = 0
        self.wz_encode = True; self.confirm_idx = 0
        self.state = self.WZ_INT
        self.render()

    def start_now_via_schedule(self):
        if self.busy: return
        self.busy = True
        start_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
        dur_hr, dur_min = self.wz_hours, self.wz_mins
        if dur_hr == 0 and dur_min == 0: dur_min = 1
        _draw_center("Arming...")
        ok = _http_post_form(SCHED_ARM_URL, {
            "start_local": start_local,
            "duration_hr":  str(dur_hr),
            "duration_min": str(dur_min),
            "interval":     str(self.wz_interval),
            "fps":          "24",
            "auto_encode":  "on" if self.wz_encode else "",
            "sess_name":    "",
        })
        _draw_center("Scheduled" if ok else "Failed", "Starts now")
        time.sleep(0.8)
        self.busy = False
        self.state = self.HOME; self.menu_idx = 0
        self.render(force=True)

    def open_schedules(self):
        if self.busy: return
        self.state = self.SCHED_LIST; self.menu_idx = 0
        self.render()

    def toggle_rotation(self):
        global ROT_DEG, _rot_pending_clear
        ROT_DEG = 90 if ROT_DEG == 0 else 0
        _save_prefs({"rot_deg": ROT_DEG})

        # Rebind joystick for the new orientation and request the hard clear
        self._rebind_joystick()
        _rot_pending_clear = True

        _draw_center("Rotation set", f"{ROT_DEG} degrees")
        _clear()
        time.sleep(0.6)
        self.state = self.HOME
        self.render(force=True)

    # ---------- render ----------
    def _draw_confirm(self, interval_s, h, m, auto_encode, hi):
        lines = [
            f"Interval:  {interval_s}s",
            f"Duration:  {h}h{m:02d}m",
            f"Auto-enc.: {'Yes' if auto_encode else 'No'}",
            "",
        ]
        yes = "[Yes]" if hi == 0 else " Yes "
        no  = "[No] " if hi == 1 else " No  "
        lines.append(f"{yes}    {no}")
        _draw_lines(lines, title="Confirm", footer="UP/DOWN choose, OK select",
                    highlight=-1, hints=False)

    def _render_home(self):
        st = self._status()
        if st.get("encoding"):
            self.state = self.ENCODING; _draw_encoding(self._spin_idx); return
        status = "Idle"
        if st.get("encoding"): status = "Encoding"
        elif st.get("active"): status = "Capturing"
        items = []
        if st.get("active"): items.append("Stop capture")
        items += ["Quick Start", "New Timelapse", "Schedules", "Screen off"]
        items.append(f"Rotate display: {'90°' if ROT_DEG == 90 else '0°'}")
        self._home_items = items
        if self.menu_idx >= len(items): self.menu_idx = max(0, len(items)-1)
        _draw_lines(items, title=status, highlight=self.menu_idx,
                    footer="UP/DOWN move, OK select", hints=True)

    def _render_wz(self):
        if self.state == self.WZ_INT:
            _draw_wizard_page("Interval (s)", f"{self.wz_interval}",
                              tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_HR:
            _draw_wizard_page("Duration hours", f"{self.wz_hours}",
                              tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_MIN:
            _draw_wizard_page("Duration mins", f"{self.wz_mins:02d}",
                              tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_ENC:
            _draw_wizard_page("Auto-encode", "Yes" if self.wz_encode else "No",
                              tips=["UP/DOWN toggle", "OK next"])

    def render(self, force=False):
        if self.screen_off or self.busy: return
        try:
            if self.state == self.ENCODING:
                _draw_encoding(self._spin_idx); return
            if self.state == self.HOME:
                self._render_home()
            elif self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
                self._render_wz()
            elif self.state == self.WZ_CONFIRM:
                self._draw_confirm(self.wz_interval, self.wz_hours, self.wz_mins,
                                   self.wz_encode, self.confirm_idx)
            elif self.state == self.SCHED_LIST:
                sch = _read_schedules()
                lines = ["+ New Schedule"]
                now = int(time.time())
                for sid, st in sch:
                    st_ts = int(st.get("start_ts",0)); en_ts = int(st.get("end_ts",0))
                    tag = "now" if (st_ts <= now < en_ts) else "next"
                    name = (st.get("sess") or sid)[:10]
                    lines.append(f"{tag} {name} {st.get('interval',10)}s")
                hi = min(self.menu_idx, len(lines)-1) if lines else 0
                _draw_lines(lines[:6], title="Schedules",
                            footer="UP/DOWN, OK select",
                            highlight=hi, hints=True)
            else:
                _clear()
        except Exception:
            pass

    # show "sleeping" for a beat, then turn panel off
    def _draw_center_sleep_then_off(self):
        prev_state = self.state
        self.state = None
        _clear()
        _draw_center("Screen off", "Press any key\n to wake up")
        time.sleep(2.5)
        self._sleep_screen()
        self.state = prev_state

# ----------------- main loop -----------------
def main():
    ui = UI()
    last_poll = 0
    while True:
        now = time.time()

        if ui.screen_off or ui.busy:
            time.sleep(0.1); continue

        # Poll /lcd_status periodically for encoding state
        if now - last_poll > 0.5:
            st = _http_json(STATUS_URL) or {}
            ui._last_status = st

            if st.get("encoding"):
                ui.state = UI.ENCODING
                ui._spin_idx = (ui._spin_idx + 1) % len(SPINNER)
                _draw_encoding(ui._spin_idx)
            else:
                if ui.state == UI.ENCODING:
                    ui.state = UI.HOME; ui.menu_idx = 0
                if ui.state == UI.HOME:
                    ui._render_home()
            last_poll = now

        if ui.state == UI.HOME:
            ui.render()

        time.sleep(0.2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass