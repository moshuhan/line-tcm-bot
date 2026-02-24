# -*- coding: utf-8 -*-
"""
時間感知檢索與課綱鎖定模組。

- 依系統當前日期（本地時間）限制可檢索的講義範圍（標題日期 <= 當日）。
- 偵測未來課程關鍵字時回覆引導訊息。
- 學術過濾：非課綱且與中醫無關則回報僅供學業使用。
- 回答策略：穴位課程日前不預設學生具備穴位知識。
"""

import os
import re
import json
from datetime import date, datetime

# 專案根目錄（api 的上一層）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_PATH = os.path.join(_ROOT, "config", "syllabus.json")
_LECTURE_FOLDER_ENV = "LECTURE_FOLDER"

# 講義檔名格式：2026-03-05_Title.pdf
_LECTURE_FILENAME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)\.(pdf|docx?|pptx?)$", re.I)


def _load_syllabus_config():
    """載入 config/syllabus.json。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "acupoint_lecture_date": "2026-04-01",
            "lectures": [],
            "tcm_related_keywords": ["中醫", "TCM", "經絡", "氣", "針灸", "穴位", "陰陽", "五行", "課程", "講義"],
        }


def get_today_local():
    """取得系統當前日期（本地時間）。"""
    return date.today()


def _parse_lecture_dates_from_folder(folder_path):
    """掃描講義資料夾，從檔名解析 (date, title) 列表。格式：YYYY-MM-DD_Title.pdf。"""
    result = []
    if not folder_path or not os.path.isdir(folder_path):
        return result
    try:
        for name in os.listdir(folder_path):
            m = _LECTURE_FILENAME_PATTERN.match(name.strip())
            if m:
                d = m.group(1)
                title = m.group(2).strip()
                try:
                    result.append((datetime.strptime(d, "%Y-%m-%d").date(), title))
                except ValueError:
                    pass
    except Exception:
        pass
    return result


def _get_all_lecture_entries():
    """合併 config 與（可選）講義資料夾的課綱，回傳 [(date, title, keywords), ...]。"""
    cfg = _load_syllabus_config()
    entries = []
    for lec in cfg.get("lectures", []):
        try:
            d = datetime.strptime(lec["date"], "%Y-%m-%d").date()
            title = lec.get("title", "")
            keywords = lec.get("keywords", [])
            entries.append((d, title, keywords))
        except (ValueError, TypeError):
            continue
    folder = os.getenv(_LECTURE_FOLDER_ENV, "").strip()
    if folder:
        for d, title in _parse_lecture_dates_from_folder(folder):
            # 若 config 已有同一天，不重複；否則用檔名當關鍵字
            if not any(e[0] == d for e in entries):
                entries.append((d, title, [title]))
    entries.sort(key=lambda e: e[0])
    return entries


def get_allowed_lecture_dates(today=None):
    """回傳「標題日期 <= 當前日期」的講義日期集合（date 物件）。"""
    today = today or get_today_local()
    entries = _get_all_lecture_entries()
    return set(e[0] for e in entries if e[0] <= today)


def get_future_topic_reply(user_text, today=None):
    """
    若使用者問題涉及「未來日期」的講義主題，回傳應顯示的訊息；
    否則回傳 None（表示可繼續走 AI）。
    """
    if not (user_text or "").strip():
        return None
    today = today or get_today_local()
    entries = _get_all_lecture_entries()
    text_lower = user_text.strip().lower()
    text = user_text.strip()

    for lecture_date, title, keywords in entries:
        if lecture_date <= today:
            continue
        # 未來課程：檢查關鍵字是否出現在問題中
        for kw in keywords:
            if kw and (kw.lower() in text_lower or kw in text):
                date_str = lecture_date.strftime("%Y-%m-%d")
                return (
                    f"這部分屬於 {date_str} 的課程（{title}），我們到時候會詳細講解喔！"
                    "建議先複習本週的內容。"
                )
    return None


def is_off_topic(user_text):
    """
    若學生問題不在本學期課綱內且與中醫無關，回傳 True（應回覆「本機器人僅供學業使用」）。
    """
    if not (user_text or "").strip():
        return False
    cfg = _load_syllabus_config()
    tcm_keywords = [k for k in cfg.get("tcm_related_keywords", []) if k]
    if not tcm_keywords:
        return False
    text_lower = user_text.strip().lower()
    text = user_text.strip()
    for kw in tcm_keywords:
        if kw and (kw.lower() in text_lower or kw in text):
            return False
    # 完全沒有 TCM/課程相關關鍵字 → 視為非學業用途
    return True


def get_rag_instructions(today=None):
    """
    回傳要注入給 AI 的檢索與回答策略說明（日期鎖定、學術來源、教學進度遞增）。
    """
    today = today or get_today_local()
    cfg = _load_syllabus_config()
    allowed = get_allowed_lecture_dates(today)
    acupoint_date_str = cfg.get("acupoint_lecture_date", "2026-04-01")
    try:
        acupoint_date = datetime.strptime(acupoint_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        acupoint_date = None

    parts = [
        "【檢索與回答規則】",
        "1. 教材檢索範圍：僅使用「講義/教材標題日期 ≤ 今日」的內容；今日為 " + today.isoformat() + "。",
        "2. 若需引用外部來源，僅限學術資源：WHO TCM database、PubMed、NCCIH 等（Academic sources only）。",
        "3. 回答末尾請提供參考資料出處。",
    ]
    if acupoint_date and today < acupoint_date:
        parts.append(
            "4. 教學進度遞增原則：目前尚未教授「穴位」課程（穴位課程日期為 " + acupoint_date_str + "），"
            "回答時請預設學生不具備穴位知識；若問題涉及穴位，可簡要說明將在後續課程講解，並引導複習已教內容。"
        )
    return "\n".join(parts)
