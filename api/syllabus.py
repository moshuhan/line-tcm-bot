# -*- coding: utf-8 -*-
"""
時間感知與課綱模組（課務查詢統一整合）。

- 時區：Asia/Taipei (UTC+8)，精確至小時分鐘。
- 資料源：優先 config/syllabus_full.json（含 start_time/end_time/has_handout）；否則 syllabus.json。
- 當週 vs 下週：以當週 end_time 為分界，過後即自動顯示下一週資訊。
- Flex Message：當週課程、AI 重點（has_handout 時）、下週預告、評量、重要日期。
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
_SYLLABUS_FULL_PATH = os.path.join(_ROOT, "config", "syllabus_full.json")
_LECTURE_FOLDER_ENV = "LECTURE_FOLDER"

# 預設當週課程結束時間（syllabus_full 無 end_time 時使用）
_DEFAULT_END_HOUR, _DEFAULT_END_MINUTE = 10, 0

# 講義檔名格式：2026-03-05_Title.pdf
_LECTURE_FILENAME_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)\.(pdf|docx?|pptx?)$", re.I)


def _load_syllabus_config():
    """載入 config/syllabus.json（用於 is_off_topic、keywords 等）。"""
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


def _load_syllabus_full_config():
    """載入 config/syllabus_full.json（課務查詢用，含 start_time/end_time/has_handout）。若不存在則回傳 None。"""
    if not os.path.isfile(_SYLLABUS_FULL_PATH):
        return None
    try:
        with open(_SYLLABUS_FULL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _extract_url_from_markdown_link(text):
    """從 Markdown 連結 [text](url) 中提取純 URL；若非此格式則原樣回傳。"""
    if not text or not isinstance(text, str):
        return text or ""
    m = re.search(r'\((\s*https?://[^\s)]+\s*)\)', text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def _parse_time_str(time_str):
    """解析 'HH:MM' 字串，回傳 (hour, minute)。"""
    if not time_str or not isinstance(time_str, str):
        return _DEFAULT_END_HOUR, _DEFAULT_END_MINUTE
    parts = time_str.strip().split(":")
    try:
        h = int(parts[0]) if len(parts) > 0 else _DEFAULT_END_HOUR
        m = int(parts[1]) if len(parts) > 1 else _DEFAULT_END_MINUTE
        return h, m
    except (ValueError, TypeError):
        return _DEFAULT_END_HOUR, _DEFAULT_END_MINUTE


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
    從 config 載入課綱。優先使用 syllabus_full.json（含 end_time、has_handout）；
    否則使用 syllabus.json。
    每筆為 dict：date, title, lecturer, meeting_link, meeting_number, password,
    has_lecture_materials (或 has_handout), end_hour, end_minute, keywords。
    """
    full_cfg = _load_syllabus_full_config()
    if full_cfg:
        return _get_lectures_from_full(full_cfg)

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
                "end_hour": _DEFAULT_END_HOUR,
                "end_minute": _DEFAULT_END_MINUTE,
                "keywords": keywords,
            })
        except (ValueError, TypeError):
            continue
    entries.sort(key=lambda e: e["date"])
    return entries


def _get_lectures_from_full(full_cfg):
    """從 syllabus_full.json 解析 lectures，支援 end_time、has_handout、topic。"""
    default_meeting = full_cfg.get("meeting_default") or {}
    entries = []
    for lec in full_cfg.get("lectures", []):
        try:
            d = datetime.strptime(lec["date"], "%Y-%m-%d").date()
            end_h, end_m = _parse_time_str(lec.get("end_time", "10:00"))
            topic = (lec.get("topic") or "").strip()
            if topic == "手動輸入":
                topic = "（待填入）"
            lecturer = (lec.get("lecturer") or default_meeting.get("lecturer") or "").strip()
            if lecturer == "手動輸入":
                lecturer = "（待填入）"
            meeting_link = (lec.get("meeting_link") or default_meeting.get("meeting_link") or "").strip()
            if meeting_link == "手動輸入":
                meeting_link = ""
            else:
                meeting_link = _extract_url_from_markdown_link(meeting_link)
            meeting_number = (lec.get("meeting_number") or default_meeting.get("meeting_number") or "").strip()
            if meeting_number == "手動輸入":
                meeting_number = ""
            password = (lec.get("password") or default_meeting.get("password") or "").strip()
            if password == "手動輸入":
                password = ""
            has_handout = bool(lec.get("has_handout", False))
            entries.append({
                "date": d,
                "date_str": lec["date"],
                "title": topic or "（待填入）",
                "lecturer": lecturer or "（待填入）",
                "meeting_link": meeting_link,
                "meeting_number": meeting_number,
                "password": password,
                "has_lecture_materials": has_handout,
                "end_hour": end_h,
                "end_minute": end_m,
                "keywords": [topic] if topic and topic != "（待填入）" else [],
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
    以當週 end_time 為分界：若 現在 <= 當週課程 end_time → 顯示當週；否則自動顯示下一週。
    回傳 (display_lecture, next_lecture, is_showing_current_week)。
    display_lecture / next_lecture 為 dict 或 None。
    """
    now = now or get_now_taipei()
    entries = _get_lectures_with_metadata()
    if not entries:
        return None, None, True

    for i, lec in enumerate(entries):
        end_h = lec.get("end_hour", _DEFAULT_END_HOUR)
        end_m = lec.get("end_minute", _DEFAULT_END_MINUTE)
        cutoff = datetime(
            lec["date"].year,
            lec["date"].month,
            lec["date"].day,
            end_h,
            end_m,
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


def _get_course_inquiry_config():
    """課務查詢用 config：優先 syllabus_full，否則 syllabus.json。"""
    full = _load_syllabus_full_config()
    if full:
        return full
    return _load_syllabus_config()


def build_course_inquiry_flex(openai_client, now=None):
    """
    建立課務查詢 Flex Message 內容。
    含：當週課程資訊、AI 重點（has_handout 時）、下週預告、評量方式、重要日期、結尾聲明。
    回傳 FlexSendMessage 用的 contents dict（單一 bubble）。
    """
    now = now or get_now_taipei()
    cfg = _get_course_inquiry_config()
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

    # ---- AI 本週重點（僅當 has_handout / has_lecture_materials 時）----
    has_handout = display_lec and display_lec.get("has_lecture_materials", False)
    topic_for_ai = (display_lec or {}).get("title", "")
    if has_handout and openai_client and topic_for_ai and topic_for_ai != "（待填入）":
        highlights = generate_ai_weekly_highlights(openai_client, topic_for_ai)
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
