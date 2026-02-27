# -*- coding: utf-8 -*-
"""
時間感知與課綱模組（課務查詢統一整合）。

- 時區：Asia/Taipei (UTC+8)，精確至小時分鐘。
- 資料源：config/syllabus.json 為唯一真理來源，含 Meeting Link、Number、Password。
- 當週 vs 下週：若 現在 <= 當週課程 10:00 AM → 顯示當週；否則顯示下週。
- 精準過濾：僅對「完全與中醫/醫療學術無關」之問題回覆「本機器人僅供學業使用」。
"""

import os
import re
import json
from datetime import date, datetime, timezone, timedelta

# Asia/Taipei = UTC+8
TAIPEI_TZ = timezone(timedelta(hours=8))

# 專案根目錄（api 的上一層）
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CONFIG_PATH = os.path.join(_ROOT, "config", "syllabus.json")
_LECTURE_FOLDER_ENV = "LECTURE_FOLDER"

# 當週課程結束判定：該日 10:00 AM
_COURSE_CUTOFF_HOUR = 10
_COURSE_CUTOFF_MINUTE = 0

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
            "meeting_default": {},
            "assessment": {"items": []},
            "important_dates": [],
            "tcm_related_keywords": ["中醫", "TCM", "經絡", "氣", "針灸", "穴位", "陰陽", "五行", "課程", "講義"],
        }


def get_now_taipei():
    """取得 Asia/Taipei (UTC+8) 的當前時間，含小時與分鐘。"""
    return datetime.now(TAIPEI_TZ)


def get_today_local():
    """取得 Asia/Taipei 的當前日期（相容舊 API）。"""
    return get_now_taipei().date()


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


def _get_lectures_with_metadata():
    """
    從 config 載入課綱，回傳 [(date, title, lecturer, meeting_link, meeting_number, password, has_lecture_materials, keywords), ...]。
    每筆皆為 dict 形態的完整 lecture 物件，便於 Flex Message 使用。
    """
    cfg = _load_syllabus_config()
    default_meeting = cfg.get("meeting_default") or {}
    entries = []
    for lec in cfg.get("lectures", []):
        try:
            d = datetime.strptime(lec["date"], "%Y-%m-%d").date()
            title = lec.get("title", "")
            lecturer = lec.get("lecturer", "") or default_meeting.get("lecturer", "課程助教")
            meeting_link = lec.get("meeting_link") or default_meeting.get("meeting_link", "")
            meeting_number = lec.get("meeting_number") or default_meeting.get("meeting_number", "")
            password = lec.get("password") or default_meeting.get("password", "")
            has_materials = bool(lec.get("has_lecture_materials", False))
            keywords = lec.get("keywords", [])
            entries.append({
                "date": d,
                "date_str": lec["date"],
                "title": title,
                "lecturer": lecturer,
                "meeting_link": meeting_link,
                "meeting_number": meeting_number,
                "password": password,
                "has_lecture_materials": has_materials,
                "keywords": keywords,
            })
        except (ValueError, TypeError):
            continue
    entries.sort(key=lambda e: e["date"])
    return entries


def _get_all_lecture_entries():
    """合併 config 與（可選）講義資料夾的課綱，回傳 [(date, title, keywords), ...]。（相容舊 API）"""
    entries = _get_lectures_with_metadata()
    result = [(e["date"], e["title"], e["keywords"]) for e in entries]
    folder = os.getenv(_LECTURE_FOLDER_ENV, "").strip()
    if folder:
        for d, title in _parse_lecture_dates_from_folder(folder):
            if not any(r[0] == d for r in result):
                result.append((d, title, [title]))
    result.sort(key=lambda e: e[0])
    return result


def get_display_week_lectures(now=None):
    """
    當週 vs 下週智慧切換邏輯。
    若 現在 <= 當週課程日期 10:00 AM → 顯示當週；否則顯示下週。
    回傳 (display_lecture, next_lecture, is_showing_current_week)。
    display_lecture / next_lecture 為 dict 或 None。
    """
    now = now or get_now_taipei()
    entries = _get_lectures_with_metadata()
    if not entries:
        return None, None, True

    for i, lec in enumerate(entries):
        cutoff = datetime(
            lec["date"].year,
            lec["date"].month,
            lec["date"].day,
            _COURSE_CUTOFF_HOUR,
            _COURSE_CUTOFF_MINUTE,
            tzinfo=TAIPEI_TZ,
        )
        if now <= cutoff:
            display = lec
            next_lec = entries[i + 1] if i + 1 < len(entries) else None
            return display, next_lec, True

    display = entries[-1]
    return display, None, False


def generate_ai_weekly_highlights(openai_client, lecture_title, max_points=3):
    """
    若該週有講義，調用 OpenAI 根據主題生成 3 個重點。
    回傳 list[str] 或 []。
    """
    if not openai_client or not (lecture_title or "").strip():
        return []
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是中醫課程助教。根據本週課程主題，產出 3 個學習重點。"
                        "每點一行、簡短明確、以數字開頭（1. 2. 3.）。不要其他說明。"
                    ),
                },
                {"role": "user", "content": f"本週主題：{lecture_title.strip()}\n請產出 3 個重點。"},
            ],
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "").strip()
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()][:max_points]
        return lines
    except Exception:
        return []


def build_course_inquiry_flex(openai_client, now=None):
    """
    建立課務查詢 Flex Message 內容。
    含：課程資訊、AI 本週重點（有講義時）、下週預告、固定課務資訊、重要日期、結尾聲明。
    回傳 FlexSendMessage 用的 contents dict（單一 bubble）。
    """
    now = now or get_now_taipei()
    cfg = _load_syllabus_config()
    display_lec, next_lec, is_showing_current = get_display_week_lectures(now)

    body_contents = []

    # ---- 課程資訊：Topic, Lecturer, Meeting Link, Number, Password ----
    if display_lec:
        header_label = "📅 本週課程" if is_showing_current else "📅 下週課程"
        sec_course = {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": [
                {"type": "text", "text": header_label, "weight": "bold", "size": "md"},
                {"type": "text", "text": display_lec["title"], "wrap": True, "size": "sm"},
                {"type": "text", "text": f"講師：{display_lec['lecturer']}", "wrap": True, "size": "xs", "color": "#666666"},
            ],
        }
        if display_lec.get("meeting_link"):
            sec_course["contents"].append({
                "type": "button",
                "style": "link",
                "action": {"type": "uri", "label": "加入會議", "uri": display_lec["meeting_link"]},
            })
        if display_lec.get("meeting_number"):
            sec_course["contents"].append({
                "type": "text",
                "text": f"會議號：{display_lec['meeting_number']}",
                "wrap": True,
                "size": "xs",
                "color": "#666666",
            })
        if display_lec.get("password"):
            sec_course["contents"].append({
                "type": "text",
                "text": f"密碼：{display_lec['password']}",
                "wrap": True,
                "size": "xs",
                "color": "#666666",
            })
        body_contents.append(sec_course)
        body_contents.append({"type": "separator"})

    # ---- AI 本週重點（僅當有講義時）----
    if display_lec and display_lec.get("has_lecture_materials") and openai_client:
        highlights = generate_ai_weekly_highlights(openai_client, display_lec["title"])
        if highlights:
            hi_lines = [{"type": "text", "text": h, "wrap": True, "size": "xs"} for h in highlights]
            body_contents.append({
                "type": "box",
                "layout": "vertical",
                "spacing": "xs",
                "contents": [
                    {"type": "text", "text": "📌 AI 本週重點", "weight": "bold", "size": "md"},
                    {"type": "box", "layout": "vertical", "spacing": "xs", "contents": hi_lines},
                ],
            })
            body_contents.append({"type": "separator"})

    # ---- 下週預告 ----
    if next_lec:
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": [
                {"type": "text", "text": "📆 下週預告", "weight": "bold", "size": "md"},
                {"type": "text", "text": next_lec["title"], "wrap": True, "size": "sm"},
            ],
        })
        body_contents.append({"type": "separator"})

    # ---- 固定課務資訊：評量方式 ----
    assessment_items = cfg.get("assessment", {}).get("items", ["專題報告", "出席狀況", "課堂參與", "心得與反思報告"])
    body_contents.append({
        "type": "box",
        "layout": "vertical",
        "spacing": "xs",
        "contents": [
            {"type": "text", "text": "📋 評量方式", "weight": "bold", "size": "md"},
            {"type": "text", "text": "、".join(assessment_items), "wrap": True, "size": "xs"},
        ],
    })
    body_contents.append({"type": "separator"})

    # ---- 重要日期 ----
    important = cfg.get("important_dates", [
        {"date": "2026-04-18", "label": "期中報告"},
        {"date": "2026-06-13", "label": "期末報告"},
    ])
    if important:
        date_lines = [f"・{item.get('label', '')} {item.get('date', '')}" for item in important if item.get("label")]
        body_contents.append({
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": [
                {"type": "text", "text": "🗓 重要日期", "weight": "bold", "size": "md"},
                {"type": "text", "text": "\n".join(date_lines), "wrap": True, "size": "xs"},
            ],
        })
        body_contents.append({"type": "separator"})

    # ---- 結尾聲明 ----
    body_contents.append({
        "type": "text",
        "text": "如有其他問題，請洽課程助教",
        "wrap": True,
        "size": "xs",
        "color": "#888888",
        "align": "center",
    })

    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": body_contents,
        },
    }
    return bubble


def get_allowed_lecture_dates(today=None):
    """回傳「標題日期 <= 當前日期」的講義日期集合（date 物件）。"""
    today = today or get_today_local()
    entries = _get_all_lecture_entries()
    return set(e[0] for e in entries if e[0] <= today)


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


def get_rag_instructions(today=None):
    """中醫問答模式：放寬限制，與中醫相關皆可回答；僅攔截完全無關閒聊。"""
    parts = [
        "【中醫問答模式】",
        "1. 只要問題與中醫、醫療、人體、穴位、經絡、辯證相關，請依專業知識庫或外部學術資源完整回答，不限制講義進度。",
        "2. 若需引用外部來源，僅限學術資源：WHO TCM database、PubMed、NCCIH 等。",
        "3. 回答末尾請提供參考資料出處。",
        "4. 僅攔截完全無關的閒聊（娛樂、天氣等），友善引導回歸中醫課程議題。",
    ]
    return "\n".join(parts)


def get_writing_mode_instructions():
    """寫作修訂模式：禁用中醫知識檢索，專注語法、修辭、寫作建議。"""
    return (
        "【寫作修訂模式】\n"
        "1. 禁用中醫知識檢索與分析。\n"
        "2. 專注於：使用者輸入句子的語法糾錯、修辭優化、寫作建議。\n"
        "3. 請提供具體修改建議與改寫範例。"
    )


def is_course_inquiry_intent(text):
    """偵測課務相關意圖（這堂課在學什麼、進度、老師、課表、評分、作業等）。"""
    if not (text or "").strip():
        return False
    t = text.strip().lower()
    keywords = [
        "這堂課", "在學什麼", "學什麼", "進度", "老師", "教授", "課表", "schedule",
        "course", "課程介紹", "introduction", "上課", "教室", "syllabus", "課務", "本週重點",
        "評分", "成績", "作業", "繳交", "grading", "assignment",
    ]
    return any(kw in t for kw in keywords)
