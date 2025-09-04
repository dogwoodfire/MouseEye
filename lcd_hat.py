#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, time, json
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode

# ----------------- HAT wiring (BCM) -----------------
PIN_DC   = int(os.environ.get("LCD_PIN_DC",   "25"))
PIN_RST  = int(os.environ.get("LCD_PIN_RST",  "27"))
PIN_BL   = int(os.environ.get("LCD_PIN_BL",   "24"))

# Waveshare 1.44" LCD HAT buttons / joystick
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
STATUS_URL    = f"{LOCAL}/lcd_status"     # ensure this exists in app.py
START_URL     = f"{LOCAL}/start"
STOP_URL      = f"{LOCAL}/stop"
SCHED_ARM_URL = f"{LOCAL}/schedule/arm"
SCHED_FILE    = "/home/pi/timelapse/schedule.json"

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
    # Adjust offsets/rotation if you still see edge noise
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
F_VALUE  = _font(18)

WHITE=(255,255,255); GRAY=(140,140,140); CYAN=(120,200,255)
GREEN=(80,220,120);  YELL=(255,210,80);   BLUE=(90,160,220)

self.confirm_idx = 0   # 0 = Yes, 1 = No

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
        drw.text((WIDTH-14,  8), "↑", font=F_TEXT,  fill=GRAY)
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

def _draw_wizard_page(title, value, tips=None, step=None):
    img = Image.new("RGB", (device.width, device.height), (0,0,0))
    drw = ImageDraw.Draw(img)
    drw.text((2, 2), title, font=F_TITLE, fill=WHITE)
    if step is not None:
        s = f"×{step}"; sw = F_SMALL.getlength(s)
        drw.text((WIDTH-2-sw, 2), s, font=F_SMALL, fill=GRAY)
    val = str(value); tw = F_VALUE.getlength(val)
    drw.text(((WIDTH - tw)//2, 40), val, font=F_VALUE, fill=CYAN)
    y = 80
    for line in (tips or []):
        drw.text((2, y), line, font=F_SMALL, fill=GRAY); y += 12
    device.display(img)

def _draw_confirm(self, interval_s, h, m, auto_encode, hi):
    lines = [
        f"Interval:  {interval_s}s",
        f"Duration:  {h}h{m:02d}m",
        f"Auto-enc.: {'Yes' if auto_encode else 'No'}",
        "",
    ]
    # two options on one line; bracket the highlighted choice
    yes = "[Yes]" if hi == 0 else " Yes "
    no  = "[No] " if hi == 1 else " No  "
    lines.append(f"{yes}    {no}")

    # draw: no right-side hints here; footer shows how to act
    _draw_lines(
        lines,
        title="Confirm",
        footer="↑/↓ choose, ✓ select",
        highlight=-1,
        hints=False
    )

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
    # Main menu + two wizards (Immediate Timelapse and New Schedule)
    HOME, TL_INT, TL_HR, TL_MIN, TL_ENC, TL_CONFIRM, \
    SCH_OFF_H, SCH_OFF_M, SCH_INT, SCH_DUR_H, SCH_DUR_M, SCH_ENC, SCH_CONFIRM, \
    SCHED_LIST = range(14)
    ENCODING = 99

    def __init__(self):
        self.state = self.HOME
        self.menu_idx = 0
        self._home_items = ["Quick Start", "New Timelapse", "Schedules"]

        # Immediate timelapse wizard values
        self.tl_interval = 10
        self.tl_hours    = 0
        self.tl_mins     = 0
        self.tl_encode   = True
        self.step        = 1  # 1 or 10

        # New schedule wizard values
        self.sch_off_h   = 0  # start in +H
        self.sch_off_m   = 1  # start in +M (min 1)
        self.sch_interval= 10
        self.sch_hours   = 0
        self.sch_mins    = 0
        self.sch_encode  = True

        self._armed_at = time.time() + 0.50  # ignore spurious edges for first 500 ms
        self._bind_inputs()
        self.render()
        self._spin = 0  # spinner frame for encoding overlay

    # --- status helper ---
    def _status(self):
        return _http_json(STATUS_URL) or {}
    
    def _poll_status_for_encoding(self):
        st = self._status()
        enc = bool(st.get("encoding"))
        if enc and self.state != self.ENCODING:
            self.state = self.ENCODING
            self._spin = 0
        elif not enc and self.state == self.ENCODING:
            self.state = self.HOME
            self.menu_idx = 0
            self.render()
        return enc

    # ---------- input binding ----------
    def _bind_inputs(self):
        def locked(fn):
            def inner():
                if self.state == self.ENCODING:
                    return  # block input while encoding
                fn()
            return inner

        def press_up():
            if self.state in (self.HOME, self.SCHED_LIST):
                self.nav(-1)
            else:
                self.adjust(+1)

        def press_down():
            if self.state in (self.HOME, self.SCHED_LIST):
                self.nav(+1)
            else:
                self.adjust(-1)

        if btn_up:   btn_up.when_pressed   = locked(press_up)
        if btn_down: btn_down.when_pressed = locked(press_down)
        if btn_ok:   btn_ok.when_pressed   = locked(self.ok)

        if js_up:    js_up.when_pressed    = locked(press_up)
        if js_down:  js_down.when_pressed  = locked(press_down)
        if js_left:  js_left.when_pressed  = locked(self.step_small)
        if js_right: js_right.when_pressed = locked(self.step_big)
        if js_push:  js_push.when_pressed  = locked(self.ok)

    # ---------- helpers ----------
    def step_small(self): self.step = 1;  self.render()
    def step_big(self):   self.step = 10; self.render()

    def nav(self, delta):
        if self.state == self.HOME:
            items = self._home_menu_items()
            if items:
                self.menu_idx = (self.menu_idx + delta) % len(items)
            else:
                self.menu_idx = 0
            self.render()
        elif self.state == self.SCHED_LIST:
            lines = self._schedule_list_lines()
            if lines:
                self.menu_idx = (self.menu_idx + delta) % len(lines)
            else:
                self.menu_idx = 0
            self.render()

    def adjust(self, delta):
        if self.state == self.WZ_INT:
            self.wz_interval = max(1, self.wz_interval + delta * self.step)
        elif self.state == self.WZ_HR:
            self.wz_hours = max(0, min(999, self.wz_hours + delta * self.step))
        elif self.state == self.WZ_MIN:
            self.wz_mins = max(0, min(59,  self.wz_mins  + delta * self.step))
        elif self.state == self.WZ_ENC:
            self.wz_encode = not self.wz_encode
        elif self.state == self.WZ_CONFIRM:
            self._draw_confirm(self.wz_interval, self.wz_hours, self.wz_mins, self.wz_encode, self.confirm_idx)
        self.render()

    # ---------- OK flow ----------
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
            # advance through wizard steps
            self.state += 1
            if self.state == self.WZ_CONFIRM:
                self.confirm_idx = 0  # default to "Yes"
            self.render()

        elif self.state == self.WZ_CONFIRM:
            if self.confirm_idx == 0:
                # YES → start via schedule
                self.start_now_via_schedule()
            else:
                # NO → discard and go home
                _draw_center("Discarded")
                time.sleep(0.5)
                self.state = self.HOME
                self.menu_idx = 0
                self.render()

        elif self.state == self.SCHED_LIST:
            if self.menu_idx == 0:
                self.start_wizard()
            else:
                self.render()
            return

    # ---------- Actions ----------
    def _home_menu_items(self):
        st = self._status()
        items = []
        if st.get("active"):
            items.append("⏹ Stop Capture")
        items += ["Quick Start", "New Timelapse", "Schedules"]
        return items

    def _home_ok(self):
        items = self._home_menu_items()
        sel = items[self.menu_idx] if items else None
        if not sel:
            return
        if sel.startswith("⏹"):
            _draw_center("Stopping…")
            ok = _http_post_form(STOP_URL, {})
            _draw_center("Stopped" if ok else "Failed")
            time.sleep(0.6)
            self.menu_idx = 0
            self.render()
        elif sel == "Quick Start":
            _draw_center("Starting…")
            ok = _http_post_form(START_URL, {"interval": 10})
            _draw_center("Started" if ok else "Failed", "Quick Start")
            time.sleep(0.6)
            self.render()
        elif sel == "New Timelapse":
            self._start_timelapse_wizard()
        elif sel == "Schedules":
            self.state = self.SCHED_LIST
            self.menu_idx = 0
            self.render()

    def _render_encoding(self):
        """Full-screen 'Encoding…' with a tiny spinner."""
        from math import sin, cos, pi
        img = Image.new("RGB", (device.width, device.height), (0,0,0))
        drw = ImageDraw.Draw(img)

        title = "Encoding…"
        tw = F_TITLE.getlength(title)
        drw.text(((WIDTH - tw)//2, 18), title, font=F_TITLE, fill=(255,255,255))

        cx, cy, r = WIDTH//2, 76, 20
        spokes = 12
        active = self._spin % spokes
        for k in range(spokes):
            a = (2*pi/spokes)*k
            x0 = cx + int((r-8) * cos(a))
            y0 = cy + int((r-8) * sin(a))
            x1 = cx + int(r * cos(a))
            y1 = cy + int(r * sin(a))
            col = (120,200,255) if k == active else (60,60,60)
            drw.line((x0,y0,x1,y1), fill=col, width=2)

        drw.text((2, HEIGHT-12), "Please wait…", font=F_SMALL, fill=(140,140,140))
        device.display(img)
        self._spin = (self._spin + 1) % 120

    def _start_timelapse_wizard(self):
        self.tl_interval = 10
        self.tl_hours    = 0
        self.tl_mins     = 0
        self.tl_encode   = True   # NOTE: with /start, auto-encode must be handled later manually
        self.step        = 1
        self.state       = self.TL_INT
        self.render()

    def _start_schedule_wizard(self):
        self.sch_off_h    = 0
        self.sch_off_m    = 1
        self.sch_interval = 10
        self.sch_hours    = 0
        self.sch_mins     = 0
        self.sch_encode   = True
        self.step         = 1
        self.state        = self.SCH_OFF_H
        self.render()

    def _start_timelapse_now(self):
        # Start immediately via /start (so it WON'T persist and auto-start after reboot)
        _draw_center("Starting…")
        data = {
            "interval":         str(self.tl_interval),
            "duration_hours":   str(self.tl_hours),
            "duration_minutes": str(self.tl_mins),
        }
        ok = _http_post_form(START_URL, data)
        _draw_center("Started" if ok else "Failed")
        time.sleep(0.6)
        self.state = self.HOME
        self.menu_idx = 0
        self.render()

    def _arm_schedule(self):
        # Compute start_local = now + offset (rounded to minute)
        start_dt = datetime.now() + timedelta(hours=self.sch_off_h, minutes=self.sch_off_m)
        start_local = start_dt.strftime("%Y-%m-%dT%H:%M")
        _draw_center("Scheduling…")
        ok = _http_post_form(SCHED_ARM_URL, {
            "start_local": start_local,
            "duration_hr":  str(self.sch_hours),
            "duration_min": str(self.sch_mins),
            "interval":     str(self.sch_interval),
            "fps":          "24",
            "auto_encode":  "on" if self.sch_encode else "",
            "sess_name":    "",
        })
        _draw_center("Scheduled" if ok else "Failed")
        time.sleep(0.8)
        self.state = self.HOME
        self.menu_idx = 0
        self.render()

    # ---------- Rendering ----------
    def _schedule_list_lines(self):
        sch = _read_schedules()
        lines = ["➕ New Schedule"]
        now = int(time.time())
        for sid, st in sch:
            st_ts = int(st.get("start_ts",0)); en_ts = int(st.get("end_ts",0))
            tag = "now" if (st_ts <= now and now < en_ts) else "next"
            name = (st.get("sess") or sid)[:10]
            lines.append(f"{tag} {name} {st.get('interval',10)}s")
        return lines

    def render(self):
        try:
            if self.state == self.ENCODING:
                self._render_encoding()
                return
            if self.state == self.HOME:
                items = self._home_menu_items()
                if items:
                    self.menu_idx %= len(items)
                _draw_lines(items, title=("Capturing" if self._status().get("active") else
                                          "Encoding" if self._status().get("encoding") else "Idle"),
                            highlight=self.menu_idx, footer="↑/↓ move, ✓ select", hints=True)

            # Immediate TL wizard pages
            elif self.state == self.TL_INT:
                _draw_wizard_page("Interval (s)", f"{self.tl_interval}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.TL_HR:
                _draw_wizard_page("Duration hours", f"{self.tl_hours}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.TL_MIN:
                _draw_wizard_page("Duration mins", f"{self.tl_mins:02d}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.TL_ENC:
                # kept for parity, but /start can't auto-encode; you can still show it
                _draw_wizard_page("Auto-encode", "Yes" if self.tl_encode else "No",
                                  ["(Info) Encode after finish"], None)
            elif self.state == self.TL_CONFIRM:
                _draw_confirm("Confirm timelapse", [
                    f"Interval: {self.tl_interval}s",
                    f"Duration: {self.tl_hours}h{self.tl_mins:02d}m",
                    f"Auto-encode: {'Yes' if self.tl_encode else 'No'}",
                ])

            # New Schedule wizard pages
            elif self.state == self.SCH_OFF_H:
                _draw_wizard_page("Start in (hours)", f"{self.sch_off_h}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.SCH_OFF_M:
                _draw_wizard_page("Start in (mins)", f"{self.sch_off_m:02d}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.SCH_INT:
                _draw_wizard_page("Interval (s)", f"{self.sch_interval}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.SCH_DUR_H:
                _draw_wizard_page("Duration hours", f"{self.sch_hours}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.SCH_DUR_M:
                _draw_wizard_page("Duration mins", f"{self.sch_mins:02d}",
                                  ["↑/↓ change, ✓ next", "← step=1, → step=10"], self.step)
            elif self.state == self.SCH_ENC:
                _draw_wizard_page("Auto-encode", "Yes" if self.sch_encode else "No",
                                  ["↑/↓ toggle, ✓ next"], None)
            elif self.state == self.SCH_CONFIRM:
                start_in = f"{self.sch_off_h}h{self.sch_off_m:02d}m"
                _draw_confirm("Confirm schedule", [
                    f"Start in: {start_in}",
                    f"Interval: {self.sch_interval}s",
                    f"Duration: {self.sch_hours}h{self.sch_mins:02d}m",
                    f"Auto-encode: {'Yes' if self.sch_encode else 'No'}",
                ])

            elif self.state == self.SCHED_LIST:
                lines = self._schedule_list_lines()
                if lines:
                    self.menu_idx %= len(lines)
                _draw_lines(lines[:6], title="Schedules", footer="↑/↓, ✓ select",
                            highlight=(self.menu_idx if lines else -1), hints=True)
            else:
                _clear()
        except Exception:
            pass  # never crash the loop

# ----------------- main loop -----------------
def main():
    ui = UI()
    while True:
        if ui._poll_status_for_encoding():
            ui._render_encoding()
            time.sleep(0.12)   # smooth spinner refresh
            continue

        if ui.state == UI.HOME:
            ui.render()
        time.sleep(0.4)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass