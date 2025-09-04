#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, time, json, threading
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# ----------------- HAT wiring (BCM) -----------------
PIN_DC   = int(os.environ.get("LCD_PIN_DC",   "25"))
PIN_RST  = int(os.environ.get("LCD_PIN_RST",  "27"))
PIN_BL   = int(os.environ.get("LCD_PIN_BL",   "24"))

# Keys (Waveshare 1.44" LCD HAT)
KEY1     = int(os.environ.get("LCD_KEY1",     "21"))  # Up
KEY2     = int(os.environ.get("LCD_KEY2",     "20"))  # Accept
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
STATUS_URL      = f"{LOCAL}/lcd_status"     # make sure this route exists in app.py
START_URL       = f"{LOCAL}/start"
STOP_URL        = f"{LOCAL}/stop"
TEST_URL        = f"{LOCAL}/test_capture"
SCHED_ARM_URL   = f"{LOCAL}/schedule/arm"
SCHED_FILE      = "/home/pi/timelapse/schedule.json"

# ----------------- Lazy imports -----------------
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi
    from luma.lcd.device import st7735
    from gpiozero import Button, PWMLED
except Exception:
    sys.exit(0)  # headless/no libs → silently exit

# ----------------- LCD init -----------------
try:
    serial = spi(port=SPI_PORT, device=SPI_DEVICE, gpio_DC=PIN_DC, gpio_RST=PIN_RST, bus_speed_hz=16000000)
    # adjust offsets/rotation if you still see edge noise
    device = st7735(serial, width=WIDTH, height=HEIGHT, rotation=0, h_offset=2, v_offset=1, bgr=True)
except Exception:
    sys.exit(0)

# Backlight
try:
    bl = PWMLED(PIN_BL); bl.value = 1.0
except Exception:
    bl = None

# Buttons
def _mk_button(pin):
    try:
        return Button(pin, pull_up=True, bounce_time=0.08)
    except Exception:
        return None

btn_up    = _mk_button(KEY1)     # up
btn_ok    = _mk_button(KEY2)     # accept
btn_down  = _mk_button(KEY3)     # down
js_up     = _mk_button(JS_UP)
js_down   = _mk_button(JS_DOWN)
js_left   = _mk_button(JS_LEFT)
js_right  = _mk_button(JS_RIGHT)
js_push   = _mk_button(JS_PUSH)

# ----------------- Fonts & drawing helpers -----------------
def _font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", sz)
    except Exception:
        return ImageFont.load_default()

F_TITLE  = _font(14)
F_TEXT   = _font(12)
F_SMALL  = _font(10)
F_VALUE = _font(18)   # for the big centered value on wizard pages

WHITE=(255,255,255); GRAY=(140,140,140); CYAN=(120,200,255)
GREEN=(80,220,120); YELL=(255,210,80); BLUE=(90,160,220)

def _clear():
    img = Image.new("RGB", (device.width, device.height), (0,0,0))
    device.display(img)

def _draw_lines(lines, title=None, footer=None, highlight=-1, hints=True):
    img = Image.new("RGB", (device.width, device.height), (0,0,0))
    drw = ImageDraw.Draw(img)

    y = 2
    if title:
        drw.text((2,y), title, font=F_TITLE, fill=WHITE); y += 18

    for i, txt in enumerate(lines):
        fill = CYAN if i == highlight else WHITE
        drw.text((2,y), txt, font=F_TEXT, fill=fill); y += 14

    if footer:
        drw.text((2, HEIGHT-12), footer, font=F_SMALL, fill=GRAY)

    if hints:
        # Right-side softkey hints: ↑ ✓ ↓
        drw.text((WIDTH-14, 8),  "↑", font=F_TEXT,  fill=GRAY)
        drw.text((WIDTH-16, 54), "✓", font=F_TEXT,  fill=GRAY)
        drw.text((WIDTH-14, 98), "↓", font=F_TEXT,  fill=GRAY)

    device.display(img)

def _draw_center(msg, sub=None):
    img = Image.new("RGB", (device.width, device.height), (0,0,0))
    drw = ImageDraw.Draw(img)
    w,h = device.width, device.height
    tw  = F_TITLE.getlength(msg)
    drw.text(((w-tw)//2, 42), msg, font=F_TITLE, fill=WHITE)
    if sub:
        sw = F_SMALL.getlength(sub)
        drw.text(((w-sw)//2, 64), sub, font=F_SMALL, fill=GRAY)
    device.display(img)

def _draw_wizard_page(title, value, tips=None, footer=None, step=None):
    """Compact wizard layout for the 128x128 screen."""
    img = Image.new("RGB", (device.width, device.height), (0,0,0))
    drw = ImageDraw.Draw(img)

    # Title
    drw.text((2, 2), title, font=F_TITLE, fill=WHITE)

    # Optional step indicator (top-right), e.g. ×10
    if step is not None:
        step_str = f"×{step}"
        sw = F_SMALL.getlength(step_str)
        drw.text((WIDTH - 2 - sw, 2), step_str, font=F_SMALL, fill=GRAY)

    # Big centered value
    val_str = str(value)
    tw = F_VALUE.getlength(val_str)
    drw.text(((WIDTH - tw)//2, 40), val_str, font=F_VALUE, fill=CYAN)

    # Tips block near bottom
    y = 80
    for line in (tips or []):
        drw.text((2, y), line, font=F_SMALL, fill=GRAY)
        y += 12

    # Optional footer at very bottom
    if footer:
        drw.text((2, HEIGHT - 12), footer, font=F_SMALL, fill=GRAY)

    device.display(img)

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
    HOME, WZ_INT, WZ_HR, WZ_MIN, WZ_ENC, WZ_CONFIRM, SCHED_LIST = range(7)

    def __init__(self):
        self.state = self.HOME
        self.menu_idx = 0
        # default list; actual rendered list is dynamic (_home_items)
        self.menu_items = ["Quick Start", "New Timelapse", "Schedules"]
        self._home_items = self.menu_items[:]

        # wizard values
        self.wz_interval = 10
        self.wz_hours    = 0
        self.wz_mins     = 0
        self.wz_encode   = True
        self.step        = 1  # joystick left/right toggles 1/10

        self._bind_inputs()
        self.render()

    # --- status helper (NOW INSIDE THE CLASS) ---
    def _status(self):
        return _http_json(STATUS_URL) or {}

    # input binding → call small handlers
    def _bind_inputs(self):
    # MENUS / WIZARD via side keys
        if btn_up:   btn_up.when_pressed   = lambda: self.nav(+1)   # was -1
        if btn_down: btn_down.when_pressed = lambda: self.nav(-1)   # was +1
        if btn_ok:   btn_ok.when_pressed   = self.ok

        # Joystick direct value changes (already correct)
        if js_up:    js_up.when_pressed    = lambda: self.adjust(+1)
        if js_down:  js_down.when_pressed  = lambda: self.adjust(-1)
        if js_left:  js_left.when_pressed  = self.step_small
        if js_right: js_right.when_pressed = self.step_big
        if js_push:  js_push.when_pressed  = self.ok

    # ---------- state helpers ----------
    def nav(self, delta):
        if self.state == self.HOME:
            # USE THE LIST THAT'S ACTUALLY RENDERED
            items = getattr(self, "_home_items", self.menu_items)
            if items:
                self.menu_idx = (self.menu_idx + delta) % len(items)
            else:
                self.menu_idx = 0
            self.render()
        elif self.state == self.SCHED_LIST:
            # move cursor within sched list
            self.menu_idx = max(0, self.menu_idx + delta)
            self.render()
        else:
            # in wizard, up/down change value (handled via joystick too)
            self.adjust(delta)

    def adjust(self, delta):
        if self.state == self.WZ_INT:
            self.wz_interval = max(1, self.wz_interval + delta * self.step)
        elif self.state == self.WZ_HR:
            self.wz_hours = max(0, min(999, self.wz_hours + delta * self.step))
        elif self.state == self.WZ_MIN:
            self.wz_mins = max(0, min(59,  self.wz_mins  + delta * self.step))
        elif self.state == self.WZ_ENC:
            self.wz_encode = not self.wz_encode
        self.render()

    def step_small(self): self.step = 1;  self.render()
    def step_big(self):   self.step = 10; self.render()

    def ok(self):
        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            sel = items[self.menu_idx] if items else None
            if not sel:
                return
            if sel.startswith("⏹ Stop Capture"):
                self.stop_capture()
            elif sel == "Quick Start":
                self.quick_start()
            elif sel == "New Timelapse":
                self.start_wizard()
            elif sel == "Schedules":
                self.open_schedules()
            return
        elif self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
            self.state += 1
            self.render()
        elif self.state == self.WZ_CONFIRM:
            self.start_now_via_schedule()
        elif self.state == self.SCHED_LIST:
            if self.menu_idx == 0:
                self.start_wizard()
            else:
                self.render()

    # ---------- actions ----------
    def quick_start(self):
        _draw_center("Starting…")
        ok = _http_post_form(START_URL, {"interval": 10})
        _draw_center("Started" if ok else "Failed", "Quick Start")
        time.sleep(0.6)
        self.render()

    def start_wizard(self):
        self.wz_interval = 10
        self.wz_hours    = 0
        self.wz_mins     = 0
        self.wz_encode   = True
        self.step        = 1
        self.state = self.WZ_INT
        self.render()

    def stop_capture(self):
        _draw_center("Stopping…")
        ok = _http_post_form(STOP_URL, {})
        _draw_center("Stopped" if ok else "Failed")
        time.sleep(0.6)
        self.state = self.HOME
        self.menu_idx = 0
        self.render()

    def start_now_via_schedule(self):
        start_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
        dur_hr, dur_min = self.wz_hours, self.wz_mins
        if dur_hr == 0 and dur_min == 0:
            dur_min = 1  # minimum window to trigger

        _draw_center("Arming…")
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
        self.state = self.HOME
        self.menu_idx = 0
        self.render()

    def open_schedules(self):
        self.state = self.SCHED_LIST
        self.menu_idx = 0
        self.render()

    # ---------- render ----------

    def _draw_confirm(interval_s, h, m, auto_encode):
        lines = [
            f"Interval: {interval_s}s",
            f"Duration: {h}h{m:02d}m",
            f"Auto-encode: {'Yes' if auto_encode else 'No'}",
        ]
        _draw_lines(
            lines,
            title="Confirm",
            footer="Press ✓ to start",
            highlight=-1,
            hints=False  # no right-side glyphs here
        )
        
    def render(self):
        try:
            if self.state == self.HOME:
                self._render_home()
            elif self.state == self.WZ_INT:
                self._render_wz("Interval (s)", f"{self.wz_interval}  (step {self.step})")
            elif self.state == self.WZ_HR:
                self._render_wz("Duration hours", f"{self.wz_hours}  (step {self.step})")
            elif self.state == self.WZ_MIN:
                self._render_wz("Duration mins", f"{self.wz_mins}  (0–59)")
            elif self.state == self.WZ_ENC:
                self._render_wz("Auto-encode", "Yes" if self.wz_encode else "No")
            elif self.state == self.WZ_CONFIRM:
                _draw_confirm(self.wz_interval, self.wz_hours, self.wz_mins, self.wz_encode)
            elif self.state == self.SCHED_LIST:
                sch = _read_schedules()
                lines = ["➕ New Schedule"]
                now = int(time.time())
                for sid, st in sch:
                    st_ts = int(st.get("start_ts",0)); en_ts = int(st.get("end_ts",0))
                    tag = "now" if (st_ts <= now < en_ts) else "next"
                    name = (st.get("sess") or sid)[:10]
                    lines.append(f"{tag} {name} {st.get('interval',10)}s")
                hi = min(self.menu_idx, len(lines)-1) if lines else 0
                _draw_lines(lines[:6], title="Schedules", footer="↑/↓, ✓ select", highlight=hi, hints=True)
            else:
                _clear()
        except Exception:
            pass  # never crash the loop

    def _render_home(self):
        st = self._status()
        status = "Idle"
        if st.get("encoding"): status = "Encoding"
        elif st.get("active"): status = "Capturing"

        # Build the visible menu dynamically
        items = []
        if st.get("active"):
            items.append("⏹ Stop Capture")
        items += ["Quick Start", "New Timelapse", "Schedules"]

        # keep a copy so ok() acts on exactly what’s shown
        self._home_items = items

        # clamp selection in case the item count changed since last render
        if self.menu_idx >= len(items):
            self.menu_idx = max(0, len(items) - 1)

        _draw_lines(items, title=f"{status}", highlight=self.menu_idx,
                    footer="↑/↓ to move, ✓ to select", hints=True)

    def _render_wz(self, title, value):
        # default tips; tweak per step below
        tips = ["↑/↓ change, ✓ next", "← step=1, → step=10"]
        step = self.step

        if self.state == self.WZ_INT:
            _draw_wizard_page("Interval (s)", f"{self.wz_interval}", tips=tips, step=step)
        elif self.state == self.WZ_HR:
            _draw_wizard_page("Duration hours", f"{self.wz_hours}", tips=tips, step=step)
        elif self.state == self.WZ_MIN:
            # minutes limited 0–59 — keep the value 2-digit for clarity
            _draw_wizard_page("Duration mins", f"{self.wz_mins:02d}", tips=tips, step=step)
        elif self.state == self.WZ_ENC:
            # toggle, so no step indicator and simpler tips
            _draw_wizard_page("Auto-encode", "Yes" if self.wz_encode else "No",
                            tips=["↑/↓ toggle, ✓ next"], step=None)

# ----------------- main loop -----------------
def main():
    ui = UI()
    while True:
        if ui.state == UI.HOME:
            ui.render()
        time.sleep(0.4)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass