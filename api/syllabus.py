# -*- coding: utf-8 -*-
"""
離題判斷模組。

原本這裡還有課務查詢（Flex Message、課綱時間表、AI 週重點）、寫作修訂／口說練習
模式的 System Prompt——這些都是聊天室的舊功能，已經隨「口說練習」「寫作修改」
「課務查詢」三個舊按鈕一起移除（考題/口說/寫作現在都是 LIFF 頁面）。
這支檔案現在只保留 is_off_topic()，因為語音訊息辨識後仍會用它判斷離題。
"""

import os
import json

# 專案根目錄（api 的上一層）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_PATH = os.path.join(_ROOT, "config", "syllabus.json")


def _load_syllabus_config():
    """載入 config/syllabus.json（用於 is_off_topic 的關鍵字清單）。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "tcm_related_keywords": ["中醫", "TCM", "經絡", "氣", "針灸", "穴位", "陰陽", "五行", "課程", "講義"],
        }


def is_off_topic(user_text):
    """
    僅針對「明確與中醫/醫療學術無關」之問題（閒聊、娛樂、天氣、飲食推薦）回傳 True。
    雙重檢查：有 TCM 關鍵字 → 允許；有明確離題關鍵字且無 TCM → 攔截；其餘預設允許。
    """
    if not (user_text or "").strip():
        return False
    cfg = _load_syllabus_config()
    text_lower = user_text.strip().lower()
    text = user_text.strip()

    off_keywords = [k for k in cfg.get("off_topic_keywords", []) if k]
    for kw in off_keywords:
        if kw and (kw.lower() in text_lower or kw in text):
            strong_tcm = ["中醫", "TCM", "經絡", "穴位", "陰陽", "五行", "針灸", "診斷", "臟腑"]
            if any(s in text for s in strong_tcm):
                break
            return True

    tcm_keywords = [k for k in cfg.get("tcm_related_keywords", []) if k]
    if "天氣" in text or "天气" in text:
        tcm_keywords = [k for k in tcm_keywords if k not in ("氣", "qi")]
    for kw in tcm_keywords:
        if kw and (kw.lower() in text_lower or kw in text):
            return False

    return False


OFF_TOPIC_REPLY = (
    "抱歉，我目前專注於協助您的中醫課程學習。"
    "如果您有關於穴位（如：手陽明經、合谷）、經絡、陰陽五行或課程進度的問題，歡迎隨時問我！"
)
