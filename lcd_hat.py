#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)

import os, sys, time, json, threading, traceback
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode

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
            return p if isinstance(p, dict) else {}
    except Exception:
        return {}
def _save_prefs(p):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(p, f)
    except Exception:
        pass

# ----------------- Imports for LCD & IO -----------------
from PIL import Image, ImageDraw, ImageFont
from gpiozero import Button, PWMLED
from luma.core.interface.serial import spi
from luma.lcd.device import st7735

# ---------- SPI + device helpers (8 MHz for stability) ----------
def _mk_serial():
    return spi(port=SPI_PORT, device=SPI_DEVICE,
               gpio_DC=PIN_DC, gpio_RST=None,
               bus_speed_hz=8_000_000)

def _mk_device(serial_obj):
    # Keep the device at rotation=0; we rotate frames in software.
    return st7735(serial_obj, width=WIDTH, height=HEIGHT,
                  rotation=0, h_offset=0, v_offset=0, bgr=True)

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

SPINNER = ["-", "\\", "|", "/"]

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

# =====================================================================
#                              UI CONTROLLER
# =====================================================================
class UI:
    HOME, WZ_INT, WZ_HR, WZ_MIN, WZ_ENC, WZ_CONFIRM, SCHED_LIST, ENCODING = range(8)

    def __init__(self):
        # prefs
        prefs = _load_prefs()
        self.rot_deg = 90 if int(prefs.get("rot_deg", 0)) == 90 else 0

        # lcd
        self.serial = _mk_serial()
        self.device = _mk_device(self.serial)
        self._draw_lock = threading.Lock()
        self._need_home_clear = True       # per-home blank
        self._need_hard_clear = True       # one-shot blank on next render
        self._busy = False
        self._screen_off = False
        self._spin_idx = 0

        # backlight
        try:
            self.bl = PWMLED(PIN_BL); self.bl.value = 1.0
        except Exception:
            self.bl = None

        # input (constructed before binding)
        self.btn_up   = self._mk_button(KEY1)
        self.btn_ok   = self._mk_button(KEY2)
        self.btn_down = self._mk_button(KEY3)
        self.js_up    = self._mk_button(JS_UP)
        self.js_down  = self._mk_button(JS_DOWN)
        self.js_left  = self._mk_button(JS_LEFT)
        self.js_right = self._mk_button(JS_RIGHT)
        self.js_push  = self._mk_button(JS_PUSH)

        # state
        self.state = self.HOME
        self.menu_idx = 0
        self.menu_items = ["Quick Start", "New Timelapse", "Schedules", "Screen off"]
        self._home_items = self.menu_items[:]
        self.wz_interval = 10
        self.wz_hours    = 0
        self.wz_mins     = 0
        self.wz_encode   = True
        self.confirm_idx = 0
        self._last_status = {}

        # bind inputs and draw
        # bind inputs
        self._bind_inputs()

        # draw something immediately so the panel isn’t left black
        self._draw_center("Booting…")
        time.sleep(0.2)

        # NOW schedule a clean first home draw
        self._need_home_clear = True
        self._need_hard_clear = True

        # render home (this call will clear-then-draw in one go)
        self.render(force=True)

    # ---------- one-shot clears ----------
    def _request_hard_clear(self):
        self._need_hard_clear = True

    def _hard_clear(self):
        with self._draw_lock:
            b = self._blank()
            self._present(b); time.sleep(0.02)
            self._present(b); time.sleep(0.02)

    def _maybe_hard_clear(self):
        if not self._need_hard_clear:
            return
        try:
            # thorough double-black, rotation-aware
            self._present(self._blank()); time.sleep(0.02)
            self._present(self._blank()); time.sleep(0.02)
        except Exception:
            pass
        self._need_hard_clear = False

    def _present(self, img):
        # rotate frame in software
        frame = img.rotate(90, expand=False, resample=Image.NEAREST) if self.rot_deg == 90 else img
        try:
            with self._draw_lock:
                self.device.display(frame)
        except Exception:
            # Recreate SPI+device and try once more
            try:
                self._lcd_reinit()
                time.sleep(0.02)
                with self._draw_lock:
                    self.device.display(frame)
            except Exception:
                # swallow so loop continues; at least we won't crash
                pass

    def _blank(self): return Image.new("RGB", (self.device.width, self.device.height), (0,0,0))
    def _clear(self): self._present(self._blank())

    def _lcd_reinit(self):
        self.serial = _mk_serial()
        self.device = _mk_device(self.serial)
        time.sleep(0.05)
        # rotation-aware blank after reinit
        self._hard_clear()

    # ---------- fonts helpers ----------
    def _text_w(self, font, txt):
        try:   return font.getlength(txt)
        except Exception:
            try:   return font.getsize(txt)[0]
            except Exception: return len(txt) * 6

    # ---------- drawing ----------
    def _draw_lines(self, lines, title=None, footer=None, highlight=-1, hints=True):
        img = self._blank(); drw = ImageDraw.Draw(img); y = 2
        if title:
            drw.text((2,y), title, font=F_TITLE, fill=WHITE); y += 18
        for i, txt in enumerate(lines):
            drw.text((2,y), txt, font=F_TEXT, fill=(CYAN if i==highlight else WHITE)); y += 14
        if footer:
            drw.text((2, HEIGHT-12), footer, font=F_SMALL, fill=GRAY)
        if hints:
            drw.text((WIDTH-30,  8), "UP",   font=F_SMALL, fill=GRAY)
            drw.text((WIDTH-30, 54), "OK",   font=F_SMALL, fill=GRAY)
            drw.text((WIDTH-30, 98), "DOWN", font=F_SMALL, fill=GRAY)
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
        drw.text(((WIDTH - int(tw))//2, 40), val_str, font=F_VALUE, fill=CYAN)
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

    # ---------- status ----------
    def _status(self):
        st = _http_json(STATUS_URL) or {}
        self._last_status = st
        return st

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
        for b in (self.btn_up, self.btn_down, self.btn_ok, self.js_up, self.js_down, self.js_left, self.js_right, self.js_push):
            if b:
                b.when_pressed = None
                b.when_released = None

    def _rebind_joystick(self):
        """
        Rotate controls with the display.
        0°  : Up→Up, Right→Right, Down→Down, Left→Left
        90° : Up→Right, Right→Down, Down→Left, Left→Up   (screen turned CCW)
        """
        if self.rot_deg == 0:
            m = dict(up=self._logical_up, right=self._logical_right,
                     down=self._logical_down, left=self._logical_left)
        else:  # 90°
            m = dict(up=self._logical_right, right=self._logical_down,
                     down=self._logical_left, left=self._logical_up)
        if self.js_up:    self.js_up.when_pressed    = self._wrap_wake(m['up'])
        if self.js_right: self.js_right.when_pressed = self._wrap_wake(m['right'])
        if self.js_down:  self.js_down.when_pressed  = self._wrap_wake(m['down'])
        if self.js_left:  self.js_left.when_pressed  = self._wrap_wake(m['left'])
        if self.js_push:  self.js_push.when_pressed  = self._wrap_wake(self.ok)

    def _bind_inputs(self):
        # side keys (fixed orientation)
        if self.btn_up:   self.btn_up.when_pressed   = self._wrap_wake(lambda: self.nav(-1))
        if self.btn_down: self.btn_down.when_pressed = self._wrap_wake(lambda: self.nav(+1))
        if self.btn_ok:   self.btn_ok.when_pressed   = self._wrap_wake(self.ok)
        # joystick (orientation aware)
        self._rebind_joystick()

    # ---------- logical joystick actions ----------
    def _logical_up(self):
        if self.state in (self.HOME, self.SCHED_LIST, self.WZ_CONFIRM): self.nav(-1)
        else: self.adjust(+1)
    def _logical_down(self):
        if self.state in (self.HOME, self.SCHED_LIST, self.WZ_CONFIRM): self.nav(+1)
        else: self.adjust(-1)
    def _logical_left(self):
        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN): self.adjust(-10)
        elif self.state == self.WZ_CONFIRM: self.confirm_idx = 1 - self.confirm_idx; self.render()
    def _logical_right(self):
        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN): self.adjust(+10)
        elif self.state == self.WZ_CONFIRM: self.confirm_idx = 1 - self.confirm_idx; self.render()

    # ---------- screen power ----------
    def _sleep_screen(self):
        self._screen_off = True
        try:
            if self.bl is not None: self.bl.value = 0.0
        except Exception: pass
        self._clear()

    def _wake_screen(self):
        try:
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
            self.menu_idx = (self.menu_idx + delta) % max(1, len(items))
            self.render()
        elif self.state == self.SCHED_LIST:
            self.menu_idx = max(0, self.menu_idx + delta); self.render()
        elif self.state == self.WZ_CONFIRM:
            self.confirm_idx = 1 - self.confirm_idx; self.render()
        else:
            self.adjust(-1 if delta > 0 else +1)

    def adjust(self, delta):
        if self._busy: return
        if self.state == self.WZ_INT:
            self.wz_interval = max(1, self.wz_interval + delta)
        elif self.state == self.WZ_HR:
            self.wz_hours = max(0, min(999, self.wz_hours + delta))
        elif self.state == self.WZ_MIN:
            self.wz_mins = max(0, min(59, self.wz_mins + delta))
        elif self.state == self.WZ_ENC:
            if abs(delta) >= 1: self.wz_encode = not self.wz_encode
        self.render()

    def ok(self):
        if self._busy or self._screen_off or self.state == self.ENCODING: return

        if self.state == self.HOME:
            items = getattr(self, "_home_items", self.menu_items)
            sel = items[self.menu_idx] if items else None
            if not sel: return
            if sel.startswith("Stop capture"): self.stop_capture()
            elif sel == "Quick Start":         self.quick_start()
            elif sel == "New Timelapse":       self.start_wizard()
            elif sel == "Schedules":           self.open_schedules()
            elif sel == "Screen off":          self._draw_center_sleep_then_off()
            elif sel.startswith("Rotate display"): self.toggle_rotation()
            return

        if self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
            self.state += 1
            if self.state == self.WZ_CONFIRM: self.confirm_idx = 0
            self.render(); return

        if self.state == self.WZ_CONFIRM:
            if self.confirm_idx == 0: self.start_now_via_schedule()
            else:
                self._draw_center("Discarded"); time.sleep(0.5)
                self.state = self.HOME; self.menu_idx = 0; self._request_hard_clear(); self.render()
            return

        if self.state == self.SCHED_LIST:
            if self.menu_idx == 0: self.start_wizard()
            else: self.render()
            return

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

    def start_wizard(self):
        if self._busy: return
        self.wz_interval = 10; self.wz_hours = 0; self.wz_mins = 0
        self.wz_encode = True; self.confirm_idx = 0
        self._request_hard_clear()
        self.state = self.WZ_INT
        self.render()

    def start_now_via_schedule(self):
        if self._busy: return
        self._busy = True
        start_local = datetime.now().strftime("%Y-%m-%dT%H:%M")
        dur_hr, dur_min = self.wz_hours, self.wz_mins
        if dur_hr == 0 and dur_min == 0: dur_min = 1
        self._draw_center("Arming...")
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
            # clear old orientation
            self._hard_clear()

            # flip + persist
            self.rot_deg = 90 if self.rot_deg == 0 else 0
            _save_prefs({"rot_deg": self.rot_deg})

            # re-init panel and BL
            self._lcd_reinit()
            try:
                if self.bl is not None: self.bl.value = 1.0
            except Exception: pass

            # clear in the **new** orientation (important)
            self._present(self._blank()); time.sleep(0.02)
            self._present(self._blank()); time.sleep(0.02)

            # rebind inputs for new mapping
            self._bind_inputs()

            # confirm + go home
            self._request_hard_clear()
            self._draw_center("Rotation set", f"{self.rot_deg} degrees")
            time.sleep(0.6)
            self.state = self.HOME
            self.render(force=True)
        finally:
            self._busy = False

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
        self._draw_lines(lines, title="Confirm",
                         footer="UP/DOWN choose, OK select",
                         highlight=-1, hints=False)

    def _render_home(self):
        self._maybe_hard_clear()
        if self._need_home_clear:
            self._hard_clear()
            self._need_home_clear = False

        st = self._status()
        if st.get("encoding"):
            self.state = self.ENCODING
            self._draw_encoding(self._spin_idx)
            return

        status = "Idle"
        if st.get("encoding"): status = "Encoding"
        elif st.get("active"): status = "Capturing"

        items = []
        if st.get("active"): items.append("Stop capture")
        items += ["Quick Start", "New Timelapse", "Schedules", "Screen off"]
        items.append(f"Rotate display: {'90°' if self.rot_deg == 90 else '0°'}")
        self._home_items = items

        if self.menu_idx >= len(items): self.menu_idx = max(0, len(items)-1)

        self._draw_lines(items, title=status, highlight=self.menu_idx,
                         footer="UP/DOWN move, OK select", hints=True)

    def _render_wz(self):
        self._maybe_hard_clear()
        if self.state == self.WZ_INT:
            self._draw_wizard_page("Interval (s)", f"{self.wz_interval}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_HR:
            self._draw_wizard_page("Duration hours", f"{self.wz_hours}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_MIN:
            self._draw_wizard_page("Duration mins", f"{self.wz_mins:02d}",
                                   tips=["UP/DOWN ±1, LEFT/RIGHT ±10", "OK next"])
        elif self.state == self.WZ_ENC:
            self._draw_wizard_page("Auto-encode", "Yes" if self.wz_encode else "No",
                                   tips=["UP/DOWN toggle", "OK next"])

    import traceback, sys
    def render(self, force=False):
        if self._screen_off or self._busy:
            return
        try:
            if self.state == self.ENCODING:
                self._draw_encoding(self._spin_idx)
                return

            if self.state == self.HOME:
                self._render_home()

            elif self.state in (self.WZ_INT, self.WZ_HR, self.WZ_MIN, self.WZ_ENC):
                self._render_wz()

            elif self.state == self.WZ_CONFIRM:
                self._draw_confirm(
                    self.wz_interval, self.wz_hours, self.wz_mins,
                    self.wz_encode, self.confirm_idx
                )

            elif self.state == self.SCHED_LIST:
                self._maybe_hard_clear()
                sch = _read_schedules()
                lines = ["+ New Schedule"]
                now = int(time.time())
                for sid, st in sch:
                    st_ts = int(st.get("start_ts", 0))
                    en_ts = int(st.get("end_ts", 0))
                    tag = "now" if (st_ts <= now < en_ts) else "next"
                    name = (st.get("sess") or sid)[:10]
                    lines.append(f"{tag} {name} {st.get('interval', 10)}s")
                hi = min(self.menu_idx, len(lines) - 1) if lines else 0
                self._draw_lines(
                    lines[:6], title="Schedules",
                    footer="UP/DOWN, OK select",
                    highlight=hi, hints=True
                )

            else:
                self._clear()

        except Exception:
            # Don't leave a black frame silently—log the reason
            traceback.print_exc(file=sys.stderr)

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
def main():
    ui = UI()
    last_poll = 0
    while True:
        now = time.time()

        if ui._screen_off or ui._busy:
            time.sleep(0.1); continue

        # poll /lcd_status
        if now - last_poll > 0.5:
            st = _http_json(STATUS_URL) or {}
            ui._last_status = st

            if st.get("encoding"):
                ui.state = UI.ENCODING
                ui._spin_idx = (ui._spin_idx + 1) % len(SPINNER)
                ui._draw_encoding(ui._spin_idx)
            else:
                if ui.state == UI.ENCODING:
                    ui.state = UI.HOME
                    ui.menu_idx = 0
                    ui._request_hard_clear()
                if ui.state == UI.HOME:
                    ui._render_home()
            last_poll = now

        time.sleep(0.2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass