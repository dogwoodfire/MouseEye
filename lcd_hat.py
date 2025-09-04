#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, time, json, threading
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# ----------------- HAT wiring (BCM) -----------------
# From your pinout:
#   KEY1 -> BCM21, KEY2 -> BCM20, KEY3 -> BCM16
#   JS_UP -> BCM6, JS_DOWN -> BCM19, JS_LEFT -> BCM5, JS_RIGHT -> BCM26, JS_PRESS -> BCM13
PIN_DC   = int(os.environ.get("LCD_PIN_DC",   "25"))
PIN_RST  = int(os.environ.get("LCD_PIN_RST",  "27"))
PIN_BL   = int(os.environ.get("LCD_PIN_BL",   "24"))

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
STATUS_URL      = f"{LOCAL}/lcd_status"     # assumed to exist in your app
START_URL       = f"{LOCAL}/start"
STOP_URL        = f"{LOCAL}/stop"
TEST_URL        = f"{LOCAL}/test_capture"
SCHED_ARM_URL   = f"{LOCAL}/schedule/arm"
SCHED_FILE      = "/home/pi/timelapse/schedule.json"   # local persistence used by your app

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
        # normalize (sid, dict)
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
        self.menu_items = ["Quick Start", "New Timelapse", "Schedules"]

        # wizard values
        self.wz_interval = 10
        self.wz_hours    = 0
        self.wz_mins     = 0
        self.wz_encode   = True
        self.step        = 1  # joystick left/right toggles 1/10

        self._bind_inputs()
        self.render()

    # input binding → call small handlers
    def _bind_inputs(self):
        if btn_up:   btn_up.when_pressed   = lambda: self.nav(-1)
        if btn_down: btn_down.when_pressed = lambda: self.nav(+1)
        if btn_ok:   btn_ok.when_pressed   = self.ok

        if js_up:    js_up.when_pressed    = lambda: self.adjust(+1)
        if js_down:  js_down.when_pressed  = lambda: self.adjust(-1)
        if js_left:  js_left.when_pressed  = self.step_small
        if js_right: js_right.when_pressed = self.step_big
        if js_push:  js_push.when_pressed  = self.ok

    # ---------- state helpers ----------
    def nav(self, delta):
        if self.state == self.HOME:
            self.menu_idx = (self.menu_idx + delta) % len(self.menu_items)
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
            sel = self.menu_items[self.menu_idx]
            if sel == "Quick Start":
                self.quick_start()
            elif sel == "New Timelapse":
                self.start_wizard()
            elif sel == "Schedules":
                self.open_schedules()
        elif self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
            # advance through wizard
            self.state += 1
            if self.state == self.WZ_CONFIRM:
                self.render()
            else:
                self.render()
        elif self.state == self.WZ_CONFIRM:
            self.start_now_via_schedule()
        elif self.state == self.SCHED_LIST:
            # first item is "New Schedule"
            if self.menu_idx == 0:
                self.start_wizard()
            else:
                # no edit/cancel from HAT (kept simple)
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

    def start_now_via_schedule(self):
        # Use /schedule/arm so auto_encode can run when the duration ends.
        # We set start_local to "now" rounded to minute.
        start_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
        dur_hr, dur_min = self.wz_hours, self.wz_mins
        if dur_hr == 0 and dur_min == 0:
            # if user left 0 duration, treat as 1 minute minimal window
            dur_min = 1

        _draw_center("Arming…")
        ok = _http_post_form(SCHED_ARM_URL, {
            "start_local": start_local,
            "duration_hr":  str(dur_hr),
            "duration_min": str(dur_min),
            "interval":     str(self.wz_interval),
            "fps":          "24",
            "auto_encode":  "on" if self.wz_encode else "",   # checkbox-like
            "sess_name":    "",                                # optional
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
                total = self.wz_hours*60 + self.wz_mins
                summ = f"{self.wz_interval}s • {self.wz_hours}h{self.wz_mins:02d}m • {'AE' if self.wz_encode else 'no AE'}"
                _draw_lines(
                    ["Press ✓ to start now via Schedule"],
                    title="Confirm",
                    footer=summ,
                    hints=True
                )
            elif self.state == self.SCHED_LIST:
                sch = _read_schedules()
                lines = ["➕ New Schedule"]
                now = int(time.time())
                for sid, st in sch:
                    st_ts = int(st.get("start_ts",0)); en_ts = int(st.get("end_ts",0))
                    tag = "now" if (st_ts <= now < en_ts) else "next"
                    name = (st.get("sess") or sid)[:10]
                    lines.append(f"{tag} {name} {st.get('interval',10)}s")
                hi = min(self.menu_idx, len(lines)-1)
                _draw_lines(lines[:6], title="Schedules", footer="↑/↓, ✓ select", highlight=hi, hints=True)
            else:
                _clear()
        except Exception:
            # never crash the loop
            pass

    def _render_home(self):
        # top line = system status (optional)
        st = _http_json(STATUS_URL) or {}
        status = "Idle"
        if st.get("encoding"): status="Encoding"
        elif st.get("active"): status="Capturing"

        lines = self.menu_items[:]
        _draw_lines(lines, title=f"{status}", highlight=self.menu_idx, footer="↑/↓, ✓ select", hints=True)

    def _render_wz(self, title, value):
        lines = [title, f"> {value}", "", "✓ to continue"]
        _draw_lines(lines, title="New Timelapse", highlight=1, footer="JS: +/-  Left/Right: step", hints=True)

# ----------------- main loop -----------------
def main():
    ui = UI()
    # redraw status bar periodically while on HOME
    while True:
        if ui.state == UI.HOME:
            ui.render()
        time.sleep(0.4)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass