#!/usr/bin/env python3
import os, time
from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import spi
from luma.lcd.device import st7735

WIDTH, HEIGHT = 128, 128

def frame(tag):
    img = Image.new("RGB", (WIDTH, HEIGHT), "black")
    d = ImageDraw.Draw(img)
    # 2-px magenta border (reveals any wrap/garbage)
    d.rectangle([0,0,WIDTH-1,HEIGHT-1], outline=(255,0,255), width=2)
    d.text((4, 4), tag, fill=(255,255,255))
    return img

def make(serial, **kw):
    dev = st7735(serial, width=WIDTH, height=HEIGHT, **kw)
    dev.clear()
    return dev

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=27, bus_speed_hz=8000000)

combos = [
    dict(rotation=0,   h_offset=0, v_offset=0,  bgr=True,  invert=False),
    dict(rotation=0,   h_offset=2, v_offset=3,  bgr=True,  invert=False),
    dict(rotation=0,   h_offset=2, v_offset=3,  bgr=True,  invert=True),
    dict(rotation=0,   h_offset=2, v_offset=1,  bgr=True,  invert=False),
    dict(rotation=90,  h_offset=2, v_offset=3,  bgr=True,  invert=False),
    dict(rotation=180, h_offset=2, v_offset=3,  bgr=True,  invert=False),
    dict(rotation=270, h_offset=2, v_offset=3,  bgr=True,  invert=False),
    dict(rotation=0,   h_offset=2, v_offset=3,  bgr=False, invert=False),
]

i = 0
dev = None
try:
    while True:
        cfg = combos[i % len(combos)]
        if dev: dev.clear()
        dev = make(serial, **cfg)
        tag = ", ".join(f"{k}={v}" for k,v in cfg.items())
        dev.display(frame(tag))
        print("Showing:", tag)
        time.sleep(2.0)
        i += 1
except KeyboardInterrupt:
    if dev: dev.clear()