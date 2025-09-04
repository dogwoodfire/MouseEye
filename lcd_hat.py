#!/usr/bin/env python3
import os, sys, time, json, threading
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


# --- OPTIONAL: change pins through env if your board differs ---
SPI_PORT   = int(os.environ.get("LCD_SPI_PORT", "0"))
SPI_DEVICE = int(os.environ.get("LCD_SPI_DEV",  "0"))
PIN_DC     = int(os.environ.get("LCD_PIN_DC",   "25"))   # D/C
PIN_RST    = int(os.environ.get("LCD_PIN_RST",  "27"))   # Reset
PIN_BL     = int(os.environ.get("LCD_PIN_BL",   "24"))   # Backlight (if available)

# Buttons (change to match your HAT; some have K1/K2/K3 or a 5-way switch)
BTN_START  = int(os.environ.get("LCD_BTN_START", "5"))
BTN_STOP   = int(os.environ.get("LCD_BTN_STOP",  "6"))
BTN_TEST   = int(os.environ.get("LCD_BTN_TEST",  "16"))

WIDTH = 128
HEIGHT = 128

LOCAL = "http://127.0.0.1:5050"
STATUS_URL = f"{LOCAL}/lcd_status"
START_URL  = f"{LOCAL}/start"
STOP_URL   = f"{LOCAL}/stop"
TEST_URL   = f"{LOCAL}/test_capture"  # we won't display the image; just warm/verify

# --- lazy imports so we can exit gracefully if libs are missing ---
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi
    from luma.lcd.device import st7735
    from gpiozero import Button, PWMLED
except Exception:
    # No display libs? Do nothing; headless mode OK.
    sys.exit(0)

# Try to open the device. If this fails, we quit silently (no HAT / SPI disabled).
try:
    serial = spi(port=SPI_PORT, device=SPI_DEVICE, gpio_DC=PIN_DC, gpio_RST=PIN_RST, bus_speed_hz=16000000)
    device = st7735(serial, width=WIDTH, height=HEIGHT, rotation=0, offset_left=2, offset_top=1)  # set rotation if needed
except Exception:
    sys.exit(0)

# Backlight (optional)
bl = None
try:
    bl = PWMLED(PIN_BL)
    bl.value = 1.0
except Exception:
    bl = None  # not fatal

# Buttons
start_btn = stop_btn = test_btn = None
try:
    start_btn = Button(BTN_START, pull_up=True, bounce_time=0.1)
    stop_btn  = Button(BTN_STOP,  pull_up=True, bounce_time=0.1)
    test_btn  = Button(BTN_TEST,  pull_up=True, bounce_time=0.1)
except Exception:
    # If buttons wiring differs, you can just omit them
    pass

# Font
def _font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", sz)
    except Exception:
        return ImageFont.load_default()

FONT_BIG  = _font(14)
FONT_SMALL= _font(11)

def _http_json(url, timeout=1.2):
    try:
        with urlopen(Request(url, headers={"Cache-Control":"no-store"}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None

def _http_post(url, data=None, timeout=1.2):
    try:
        body = b"" if data is None else json.dumps(data).encode("utf-8")
        req = Request(url, data=body, method="POST", headers={"Content-Type":"application/json"})
        with urlopen(req, timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False

def human_time_left(sec):
    if sec is None: return ""
    m, s = divmod(max(0, int(sec)), 60)
    return f"{m}m{s:02d}s"

def draw_screen(state):
    img = Image.new("RGB", (WIDTH, HEIGHT), (0,0,0))
    drw = ImageDraw.Draw(img)

    # Top status line
    status = "Idle"
    if state.get("encoding"):
        status = "Encoding"
    elif state.get("active"):
        status = "Capturing"

    # Colors
    WHITE = (255,255,255)
    GREEN = (80,220,120)
    YELLW = (255,210,80)
    CYAN  = (120,200,255)
    GRAY  = (150,150,150)

    # Title/status
    drw.text((2, 2), f"{status}", font=FONT_BIG, fill=GREEN if status=="Capturing" else (YELLW if status=="Encoding" else WHITE))

    # Second line: session or next schedule
    if state.get("active"):
        sess = state.get("session","")[:10]
        frames = state.get("frames",0)
        left = human_time_left(state.get("remaining_sec"))
        drw.text((2, 22), f"{sess} {frames}f {left}", font=FONT_SMALL, fill=CYAN if left else WHITE)
    else:
        nxt = state.get("next_sched")
        if nxt:
            # show upcoming or active window times
            try:
                st = datetime.fromtimestamp(nxt["start_ts"]).strftime("%H:%M")
                en = datetime.fromtimestamp(nxt["end_ts"]).strftime("%H:%M")
                nam = (nxt.get("name") or "schedule")[:11]
                tag = "now" if nxt.get("active_now") else "next"
                drw.text((2, 22), f"{tag}: {nam}", font=FONT_SMALL, fill=WHITE)
                drw.text((2, 36), f"{st}-{en} / {nxt['interval']}s @{nxt['fps']}fps", font=FONT_SMALL, fill=GRAY)
            except Exception:
                drw.text((2, 22), "next: --", font=FONT_SMALL, fill=WHITE)
        else:
            drw.text((2, 22), "no schedules baby", font=FONT_SMALL, fill=WHITE)

    # Disk meter (bottom)
    disk = state.get("disk", {})
    used_pct = 100 - int(disk.get("pct_free", 0))
    bar_w = int((WIDTH-4) * max(0,min(100,used_pct)) / 100)
    drw.rectangle([2, HEIGHT-14, WIDTH-2, HEIGHT-6], outline=(70,70,70), fill=None)
    drw.rectangle([2, HEIGHT-14, 2+bar_w, HEIGHT-6], fill=(90,160,220))
    drw.text((2, HEIGHT-24), f"Disk {used_pct}% used", font=FONT_SMALL, fill=GRAY)

    device.display(img)

def on_start_pressed():
    # start with the last-used interval from the web UI (form default), or 10s as a fallback
    _http_post(START_URL, data={"interval": 10})

def on_stop_pressed():
    _http_post(STOP_URL)

def on_test_pressed():
    # Warm the camera / quick test, no UI other than brief flash dim
    ok = _http_json(TEST_URL)  # will 500 on error; we ignore the body
    if bl:
        bl.value = 0.2
        time.sleep(0.15)
        bl.value = 1.0

if start_btn: start_btn.when_pressed = on_start_pressed
if stop_btn:  stop_btn.when_pressed  = on_stop_pressed
if test_btn:  test_btn.when_pressed  = on_test_pressed

def main():
    # poll the status and draw ~5–10 fps for smoothness without burning CPU
    while True:
        st = _http_json(STATUS_URL) or {}
        draw_screen(st)
        time.sleep(0.12)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass