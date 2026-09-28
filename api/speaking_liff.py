# -*- coding: utf-8 -*-
"""
NPC 對話式口說教練：協調者（orchestrator）。

只負責串接各個獨立模組，自己不做內容產生、不做分析邏輯——對應規格書第九節
「不要讓一個巨大 Prompt 同時負責所有事情」：

- speaking_content.py   Case Generator／Topic Generator（動態抽病例/題目）
- speaking_agents.py    Patient Agent／Professor Agent（依 Case/Topic 組角色 instructions）
- speaking_evaluator.py Evaluator（英文/TCM 分析、動態 hint、結算摘要）
- speaking_session.py   Session Manager（session_id、對話輪數、已覆蓋主題）

語音對話本身走 OpenAI Realtime API（瀏覽器端用 WebRTC 直接連線到 OpenAI，
不經過我們的伺服器中繼，延遲最低——這個決定經過跟使用者確認，即時對話體驗
比嚴格照規格書的「文字生成→TTS」架構圖更重要）。這裡負責：

1. 依 mode（clinical/academic）動態抽一個 Case/Topic，組出 Realtime session
   的 instructions，並建立一場新的 session 追蹤狀態
2. 伺服器端用主 API Key 換一組短效 ephemeral client secret 給前端用
3. 每輪對話結束後，process_turn() 呼叫 Evaluator 產生規格書要的 structured
   response（reply/translation/hint/language_feedback/terminology/
   session_state/avatar），reply 內容已經由 Realtime API 講出來，這裡不重複回傳

刻意不把逐字稿寫入 MongoDB：對話內容只在這次 session 存在（Redis + TTL），
離開結算頁就清空。
"""
import os
import traceback

try:
    from api.speaking_content import get_random_case, get_random_topic, get_case_by_id, load_cases
    from api.speaking_agents import build_patient_instructions, build_professor_instructions, build_free_practice_instructions
    from api.speaking_evaluator import analyze_turn, summarize_session
    from api.speaking_session import create_session, get_session, record_turn, compute_session_state, delete_session
except ImportError:
    from speaking_content import get_random_case, get_random_topic, get_case_by_id, load_cases
    from speaking_agents import build_patient_instructions, build_professor_instructions, build_free_practice_instructions
    from speaking_evaluator import analyze_turn, summarize_session
    from speaking_session import create_session, get_session, record_turn, compute_session_state, delete_session

MODES = {
    "clinical": {"label_zh": "OSCE 訓練", "character_zh": "患者"},
    "academic": {"label_zh": "學術研究者", "character_zh": "教授",
                 "persona_name": "Dr. Maria Chen", "persona_photo": "academic_professor"},
    "student": {"label_zh": "中醫學生", "character_zh": "夥伴",
                "persona_name": "Alex Wong", "persona_photo": "student_partner"},
}
DEFAULT_MODE = "clinical"
REALTIME_MODEL = "gpt-realtime"

# 語音選擇：依角色性別挑聲音，避免「圖片是男生、開口卻是女生聲音」的違和感。
# academic／student 是固定角色（Dr. Maria Chen 女性／Alex Wong 男性），clinical
# 則依抽到／選到的病例 gender 欄位動態決定；gender 不是 male/female 時退回中性音。
FEMALE_VOICE = "shimmer"
MALE_VOICE = "echo"
NEUTRAL_VOICE = "alloy"


def _voice_for(mode_key, content):
    if mode_key == "academic":
        return FEMALE_VOICE
    if mode_key == "student":
        return MALE_VOICE
    gender = ((content or {}).get("gender") or "").strip().lower()
    if gender == "male":
        return MALE_VOICE
    if gender == "female":
        return FEMALE_VOICE
    return NEUTRAL_VOICE

_PATIENTS_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "assets", "patients")
_PERSONAS_DIR = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")), "assets", "personas")


def _find_photo(directory, base_name, url_prefix):
    """在 directory 底下找 base_name.<ext>；找不到回傳 None，前端就繼續用原本的圖示佔位。"""
    if not base_name:
        return None
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if os.path.isfile(os.path.join(directory, base_name + ext)):
            return f"{url_prefix}/{base_name}{ext}"
    return None


def _photo_url(case_id):
    """依 case_id 找對應的靜態病人照片（assets/patients/<case_id>.<ext>）。"""
    return _find_photo(_PATIENTS_DIR, case_id, "/assets/patients")


def _persona_photo_url(mode_key):
    """academic／student 模式是固定角色（不隨機抽案例），照片固定用 persona_photo 這個檔名
    （assets/personas/<persona_photo>.<ext>），不存在就回傳 None。"""
    persona_photo = MODES.get(mode_key, {}).get("persona_photo")
    return _find_photo(_PERSONAS_DIR, persona_photo, "/assets/personas")


def list_modes():
    """
    給主畫面身分卡片用（OSCE 訓練／學術研究者／中醫學生）。附一個隨機抽到的病例/題目
    當「預覽」（年齡、主訴、預期涵蓋主題），純粹展示用——實際開始對話時
    mint_ephemeral_session 會重新抽一個，不保證跟預覽是同一個。academic／student 是固定
    角色（教授／學生夥伴），不綁病例，photo/display_name 不隨機、每次都一樣。
    """
    out = []
    for key, m in MODES.items():
        preview = None
        display_name = m.get("persona_name") or m["character_zh"]
        photo_url = _persona_photo_url(key)
        if key == "clinical":
            content = get_random_case(None)
            if content:
                display_name = content.get("name") or display_name
                photo_url = _photo_url(content.get("case_id"))
                preview = {
                    "name": content.get("name"),
                    "age": content.get("age"),
                    "gender": content.get("gender"),
                    "occupation": content.get("occupation"),
                    "chief_complaint": content.get("chief_complaint"),
                    "expected_topics": content.get("expected_topics") or [],
                    "photo_url": photo_url,
                }
        elif key == "academic":
            content = get_random_topic(None)
            if content:
                preview = {
                    "title": content.get("title") or content.get("topic_id"),
                    "background": content.get("background") or "",
                    "expected_topics": content.get("discussion_questions") or [],
                    "photo_url": photo_url,
                }
        else:
            preview = {"photo_url": photo_url}
        out.append({
            "key": key, "label_zh": m["label_zh"], "character_zh": m["character_zh"],
            "display_name": display_name, "photo_url": photo_url, "preview": preview,
        })
    return out


def list_cases():
    """給前端『選擇病患情境』畫面用：列出全部臨床病例的基本資訊（不含 hidden_information／
    forbidden_information 等只給 Patient Agent 用的欄位），讓使用者可以指定要練習哪一個病例，
    而不是每次都隨機抽。"""
    out = []
    for c in load_cases():
        out.append({
            "case_id": c.get("case_id"),
            "name": c.get("name"),
            "age": c.get("age"),
            "gender": c.get("gender"),
            "occupation": c.get("occupation"),
            "difficulty": c.get("difficulty"),
            "chief_complaint": c.get("chief_complaint"),
            "expected_topics": c.get("expected_topics") or [],
            "photo_url": _photo_url(c.get("case_id")),
        })
    return out


def _pick_content_and_instructions(mode_key, difficulty=None, case_id=None):
    """依 mode 抽 Case 或 Topic，回傳 (content_id, content_dict, instructions)。內容庫是空的時 content 為 None。
    case_id 有給的話（使用者從清單指定病例）就直接用該病例，不再隨機抽。"""
    if mode_key == "academic":
        topic = get_random_topic(difficulty)
        return (topic.get("topic_id") if topic else None, topic, build_professor_instructions(topic))
    if mode_key == "student":
        return (None, None, build_free_practice_instructions(difficulty))
    case = (get_case_by_id(case_id) if case_id else None) or get_random_case(difficulty)
    return (case.get("case_id") if case else None, case, build_patient_instructions(case))


def _expected_topics(mode_key, content):
    content = content or {}
    return content.get("expected_topics") if mode_key == "clinical" else content.get("discussion_questions")


def _full_context_text(mode_key, content):
    """給前端『提示下拉選單』顯示的完整情境說明（不截斷）：clinical 是完整主訴，
    academic 是題目標題＋背景，student 沒有固定情境所以回傳一句說明文字。"""
    content = content or {}
    if mode_key == "clinical":
        return content.get("chief_complaint") or ""
    if mode_key == "academic":
        title = content.get("title") or ""
        background = content.get("background") or ""
        return f"{title}\n{background}".strip("\n") if (title or background) else ""
    return "沒有固定病例或題目，想聊什麼都可以，用簡單的英文輕鬆練習開口說。"


def mint_ephemeral_session(openai_client, redis_client, mode_key, difficulty=None, case_id=None):
    """
    開始一場新對話：抽/指定 Case 或抽 Topic → 組 instructions → 建 Realtime ephemeral secret
    → 建立 Session Manager 記錄。回傳前端需要的一切（含 session_id）；失敗回傳 None。
    case_id 只對 clinical 模式有意義，使用者從『選擇病患情境』清單指定病例時會帶這個參數。
    """
    mode_key = mode_key if mode_key in MODES else DEFAULT_MODE
    mode = MODES[mode_key]
    content_id, content, instructions = _pick_content_and_instructions(mode_key, difficulty, case_id)

    session_config = {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "audio": {
            "output": {"voice": _voice_for(mode_key, content)},
            "input": {
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "server_vad"},
            },
        },
    }
    try:
        resp = openai_client.realtime.client_secrets.create(session=session_config)
    except Exception:
        traceback.print_exc()
        return None

    session_id = create_session(redis_client, mode_key, content_id, content)
    topics = _expected_topics(mode_key, content)
    is_clinical = mode_key == "clinical"
    photo_url = _photo_url(content_id) if is_clinical else _persona_photo_url(mode_key)
    display_name = ((content or {}).get("name") if is_clinical else None) or mode.get("persona_name") or mode["character_zh"]
    return {
        "session_id": session_id,
        "client_secret": resp.value,
        "expires_at": resp.expires_at,
        "model": REALTIME_MODEL,
        "mode": mode_key,
        "character_zh": mode["character_zh"],
        "label_zh": mode["label_zh"],
        "content_id": content_id,
        # 開場提示：case 沒抽到內容時（內容庫是空的）給通用提示，否則給第一個
        # expected_topic/discussion_question 當開場方向；前端會在連線後直接送一個
        # response.create 請角色主動開口，這個欄位就是那則開場的方向提示
        "opening_hint": (topics or [None])[0] or "Start the conversation naturally in English.",
        # 完整情境說明（不截斷），給前端提示下拉選單用，跟 char-complaint 疊字（會截斷）分開
        "full_context": _full_context_text(mode_key, content),
        # 角色照片與顯示名稱：clinical 是抽到的病例照片/假名，academic／student 是固定角色
        "photo_url": photo_url,
        "display_name": display_name,
        "content_name": (content or {}).get("name") if is_clinical else None,
        "content_age": (content or {}).get("age") if is_clinical else None,
        "content_gender": (content or {}).get("gender") if is_clinical else None,
        "content_occupation": (content or {}).get("occupation") if is_clinical else None,
    }


def process_turn(openai_client, redis_client, session_id, assistant_text, user_text):
    """
    每輪對話結束後呼叫一次：交給 Evaluator 做分析，再結合 Session Manager 更新
    對話狀態，組成規格書格式的 structured response。
    """
    session = get_session(redis_client, session_id)
    mode_key = (session or {}).get("mode") or DEFAULT_MODE
    expected_topics = _expected_topics(mode_key, (session or {}).get("content"))

    analysis = analyze_turn(openai_client, assistant_text, user_text, expected_topics)
    session = record_turn(redis_client, session_id, analysis.get("newly_covered_topics")) or session

    return {
        "translation": analysis["translation"],
        "language_feedback": analysis["language_feedback"],
        "terminology": analysis["terminology"],
        "hint": analysis["hint"],
        "session_state": compute_session_state(session),
        "avatar": {"emotion": None, "gesture": None, "animation": None, "gaze": None},
    }


def build_session_summary(openai_client, transcript):
    """結算頁摘要，委派給 Evaluator。"""
    return summarize_session(openai_client, transcript)


def end_session(redis_client, session_id):
    """對話結束、結算完成後呼叫，清除該 session 的 Redis 記錄。"""
    delete_session(redis_client, session_id)
