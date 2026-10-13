# -*- coding: utf-8 -*-
"""
把 AI 生成的 Rich Menu 背景圖（無文字）合成最終選單圖：縮放到 2500x1686、疊上中文標題、輸出 JPEG（LINE 限制 1MB 以內）。

輸入（放在 assets/rich_menu/，檔名開頭符合即可，例如 life_raw.png 或 life_raw.png.png）：
    life_raw*   生活版背景
    student_raw* 學生版背景
輸出：
    assets/rich_menu/life.jpg、student.jpg   （setup_rich_menu_tabs.js 會優先使用 .jpg）

版面（與 setup_rich_menu_tabs.js 的觸控範圍一致，兩邊改的時候要一起改）：
    分頁列 y 0–170（左半＝學生、右半＝生活），2×2 按鈕：第一列 y 170–887、第二列 y 887–1686，左右各 1250 寬
用法：python scripts/compose_rich_menu.py
"""
import glob
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 2500, 1686
TAB_H, ROW_SPLIT, COL_SPLIT = 170, 887, 1250
DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "rich_menu")

SERIF_CANDIDATES = [r"C:\Windows\Fonts\NotoSerifTC-VF.ttf", r"C:\Windows\Fonts\msjhbd.ttc",
                    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"]
SANS_CANDIDATES = [r"C:\Windows\Fonts\NotoSansTC-VF.ttf", r"C:\Windows\Fonts\msjh.ttc",
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]

INK, MUTE, GOLD, CREAM = (42, 33, 24), (120, 104, 78), (217, 169, 44), (247, 240, 225)
JADE, CIN = (31, 111, 84), (178, 58, 46)

MENUS = {
    "life": {
        "active": "life",
        "cells": [("節氣闖關", "答題・收集食材", JADE), ("食材圖鑑", "藥材與茶飲收藏", (150, 110, 10)),
                  ("今日節氣", "當季是哪一節氣", CIN), ("我的進度", "等級・連續天數", INK)],
    },
    "student": {
        "active": "student",
        "cells": [("國考刷題", "題庫・錯題本", JADE), ("英文口說", "病患・教授情境", (150, 110, 10)),
                  ("英文寫作", "即時標註・批改", CIN), ("藥材圖鑑", "方劑合成收集", INK)],
    },
}


def load_font(cands, size, weight="Bold"):
    for p in cands:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size)
                try:
                    f.set_variation_by_name(weight)
                except Exception:
                    pass
                return f
            except Exception:
                continue
    return ImageFont.load_default()


def find_raw(name):
    hits = sorted(glob.glob(os.path.join(DIR, f"{name}_raw*")))
    return hits[0] if hits else None


def draw_text(d, xy, text, font, fill, anchor="ls", stroke=0, stroke_fill=None):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_fill)


def compose(name, spec):
    src = find_raw(name)
    if not src:
        print(f"找不到 {name}_raw*，略過")
        return
    im = Image.open(src).convert("RGB").resize((W, H), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    # 分頁列文字（左＝學生、右＝生活）
    tab_font = load_font(SERIF_CANDIDATES, 84)
    for key, label, cx in (("student", "學　生", COL_SPLIT // 2), ("life", "生　活", COL_SPLIT + COL_SPLIT // 2)):
        active = key == spec["active"]
        draw_text(d, (cx, 80), label, tab_font, CREAM if active else MUTE, anchor="mm")
    # 四個按鈕：標題＋副標，靠左、垂直置中偏下，避開左上角裝飾與右下角吉祥物
    rows = [(TAB_H, ROW_SPLIT), (ROW_SPLIT, H)]
    title_font = load_font(SERIF_CANDIDATES, 176)
    sub_font = load_font(SANS_CANDIDATES, 70, "Medium")
    for i, (title, sub, color) in enumerate(spec["cells"]):
        col, row = i % 2, i // 2
        x0 = col * COL_SPLIT
        y0, y1 = rows[row]
        # 視覺卡片的內縮邊界（圖上卡片外框約離邊界 1–2%）
        left = x0 + int(COL_SPLIT * 0.115)
        cy = y0 + int((y1 - y0) * (0.70 if row == 0 else 0.60))   # 偏下：避開左上角裝飾，標題落在左下空白區；第二列卡片較矮，要再往上一點
        draw_text(d, (left, cy), title, title_font, color, anchor="ls", stroke=4, stroke_fill=(253, 248, 235))
        draw_text(d, (left + 6, cy + 112), sub, sub_font, (90, 76, 54), anchor="ls", stroke=3, stroke_fill=(253, 248, 235))
    out = os.path.join(DIR, f"{name}.jpg")
    for q in (92, 88, 84, 80, 74, 68):
        im.save(out, "JPEG", quality=q, optimize=True, progressive=True)
        if os.path.getsize(out) <= 950 * 1024:
            break
    print(out, im.size, os.path.getsize(out) // 1024, "KB (quality", q, ")")


if __name__ == "__main__":
    for k, v in MENUS.items():
        compose(k, v)
