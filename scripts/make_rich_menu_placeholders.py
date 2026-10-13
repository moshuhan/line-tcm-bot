# -*- coding: utf-8 -*-
"""
產生 Rich Menu 佔位圖（2500x1686，文字標示各按鈕區塊），等正式設計圖好了直接覆蓋同名檔案：
    assets/rich_menu/life.png      生活版（預設）
    assets/rich_menu/student.png   學生版
然後重跑 `node scripts/setup_rich_menu_tabs.js` 就會用新圖重建選單。

版面（與 setup_rich_menu_tabs.js 的 bounds 對應）：
    上方分頁列  y 0–200：左「學生」右「生活」（目前所在分頁反白）
    下方 2×2 按鈕 y 200–1686，每格 1250 × 743
用法：python scripts/make_rich_menu_placeholders.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H, TAB_H = 2500, 1686, 200
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "rich_menu")

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msjhbd.ttc", r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\NotoSansTC-VF.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/PingFang.ttc",
]

INK, PAPER, GOLD, JADE, CIN = (42, 33, 24), (247, 240, 225), (217, 169, 44), (31, 111, 84), (178, 58, 46)

MENUS = {
    "life": {
        "active_tab": "life",
        "cells": [("節氣闖關", "答題・收集食材", JADE), ("食材圖鑑", "藥材與茶飲收藏", GOLD),
                  ("今日節氣", "當季是哪一節氣", CIN), ("我的進度", "等級・連續天數", INK)],
    },
    "student": {
        "active_tab": "student",
        "cells": [("國考刷題", "題庫・錯題本", JADE), ("英文口說", "病患・教授情境", GOLD),
                  ("英文寫作", "即時標註・批改", CIN), ("藥材圖鑑", "方劑合成收集", INK)],
    },
}


def font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def center(draw, box, text, fnt, fill):
    l, t, r, b = draw.textbbox((0, 0), text, font=fnt)
    x = box[0] + (box[2] - box[0] - (r - l)) / 2 - l
    y = box[1] + (box[3] - box[1] - (b - t)) / 2 - t
    draw.text((x, y), text, font=fnt, fill=fill)


def build(name, spec):
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    # 分頁列
    for i, (key, label) in enumerate((("student", "學　生"), ("life", "生　活"))):
        box = (i * (W // 2), 0, (i + 1) * (W // 2), TAB_H)
        active = key == spec["active_tab"]
        d.rectangle(box, fill=INK if active else (231, 220, 190))
        center(d, box, label, font(86), (241, 223, 166) if active else (110, 96, 72))
    # 2x2 按鈕
    cw, ch = W // 2, (H - TAB_H) // 2
    for i, (title, sub, color) in enumerate(spec["cells"]):
        x, y = (i % 2) * cw, TAB_H + (i // 2) * ch
        pad = 18
        box = (x + pad, y + pad, x + cw - pad, y + ch - pad)
        d.rounded_rectangle(box, radius=48, fill=color)
        center(d, (box[0], box[1] + 120, box[2], box[1] + 400), title, font(170), (255, 250, 235))
        center(d, (box[0], box[1] + 420, box[2], box[1] + 560), sub, font(76), (255, 244, 214))
        center(d, (box[0], box[3] - 120, box[2], box[3] - 30), "（佔位圖：待正式設計）", font(46), (255, 235, 190))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{name}.png")
    img.save(path, optimize=True)
    print(path, os.path.getsize(path) // 1024, "KB")


if __name__ == "__main__":
    for k, v in MENUS.items():
        build(k, v)
