# -*- coding: utf-8 -*-
"""
時間感知與課綱提示模組（放寬檢索邊界）。

- 不鎖定檢索：與中醫/醫療/穴位/經絡/辯證相關之問題，AI 皆可依知識庫或學術資源回答。
- 進度提示：若問題屬未來課程主題，在回答結束後附加「這是我們第 N 週的重點，你很有先見之明喔！」。
- 精準過濾：僅對「完全與中醫/醫療學術無關」之問題（閒聊、娛樂、私人）回覆「本機器人僅供學業使用」。
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


def get_future_topic_hint(user_text, today=None):
    """
    若使用者問題涉及「未來日期」的講義主題，回傳欲附加在回答後的進度提示（非強制）；
    否則回傳 None。不阻擋 AI 回答。
    """
    if not (user_text or "").strip():
        return None
    today = today or get_today_local()
    entries = _get_all_lecture_entries()
    text_lower = user_text.strip().lower()
    text = user_text.strip()

    for idx, (lecture_date, title, keywords) in enumerate(entries, start=1):
        if lecture_date <= today:
            continue
        for kw in keywords:
            if kw and (kw.lower() in text_lower or kw in text):
                week_num = idx
                return f"這是我們第 {week_num} 週的重點，你很有先見之明喔！"
    return None


def is_off_topic(user_text):
    """
    僅針對「完全與中醫/醫療學術無關」之問題（閒聊、娛樂、私人）回傳 True。
    與中醫、醫療、人體、穴位、經絡、辯證等相關者皆視為學業使用，回傳 False。
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
    中醫問答模式：放寬限制，與中醫相關皆可回答；僅攔截完全無關閒聊。
    """
    parts = [
        "【中醫問答模式】",
        "1. 只要問題與中醫、醫療、人體、穴位、經絡、辯證相關，請依專業知識庫或外部學術資源完整回答，不限制講義進度。",
        "2. 若需引用外部來源，僅限學術資源：WHO TCM database、PubMed、NCCIH 等。",
        "3. 回答末尾請提供參考資料出處。",
        "4. 僅攔截完全無關的閒聊（娛樂、天氣等），回覆「本機器人僅供學業使用」。",
    ]
    return "\n".join(parts)


def get_writing_mode_instructions():
    """
    寫作修訂模式：禁用中醫知識檢索，專注語法、修辭、寫作建議。
    """
    return (
        "【寫作修訂模式】\n"
        "1. 禁用中醫知識檢索與分析。\n"
        "2. 專注於：使用者輸入句子的語法糾錯、修辭優化、寫作建議。\n"
        "3. 請提供具體修改建議與改寫範例。"
    )


def get_course_inquiry_instructions():
    """
    課務查詢模式：強制從 2026schedule.pdf、20260307courseintroduction.pdf 檢索，嚴禁拒絕。
    """
    return (
        "【課務查詢模式】\n"
        "1. 你必須優先且強制檢索以下檔案：2026schedule.pdf、20260307courseintroduction.pdf。\n"
        "2. 嚴禁回覆「無法回答」或「非學業使用」。必須從上述檔案中提取正確答案。\n"
        "3. 若檔案中存在課表、進度、老師、課程介紹等相關資訊，請完整回覆。"
    )


def is_course_inquiry_intent(text):
    """偵測課務相關意圖（這堂課在學什麼、進度、老師、課表等）。"""
    if not (text or "").strip():
        return False
    t = text.strip().lower()
    keywords = [
        "這堂課", "在學什麼", "學什麼", "進度", "老師", "教授", "課表", "schedule",
        "course", "課程介紹", "introduction", "上課", "教室", " syllabus",
    ]
    return any(kw in t for kw in keywords)
