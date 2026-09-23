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
import traceback

try:
    from api.speaking_content import get_random_case, get_random_topic
    from api.speaking_agents import build_patient_instructions, build_professor_instructions
    from api.speaking_evaluator import analyze_turn, summarize_session
    from api.speaking_session import create_session, get_session, record_turn, compute_session_state, delete_session
except ImportError:
    from speaking_content import get_random_case, get_random_topic
    from speaking_agents import build_patient_instructions, build_professor_instructions
    from speaking_evaluator import analyze_turn, summarize_session
    from speaking_session import create_session, get_session, record_turn, compute_session_state, delete_session

MODES = {
    "clinical": {"label_zh": "臨床衛教", "character_zh": "患者", "voice": "shimmer"},
    "academic": {"label_zh": "學術討論", "character_zh": "教授", "voice": "cedar"},
}
DEFAULT_MODE = "clinical"
REALTIME_MODEL = "gpt-realtime"


def list_modes():
    """
    給主畫面情境卡片用。附一個隨機抽到的病例/題目當「預覽」（年齡、主訴、預期涵蓋主題），
    純粹展示用——實際開始對話時 mint_ephemeral_session 會重新抽一個，不保證跟預覽是同一個。
    """
    out = []
    for key, m in MODES.items():
        content = get_random_case(None) if key == "clinical" else get_random_topic(None)
        preview = None
        if content:
            if key == "clinical":
                preview = {
                    "age": content.get("age"),
                    "gender": content.get("gender"),
                    "chief_complaint": content.get("chief_complaint"),
                    "expected_topics": content.get("expected_topics") or [],
                }
            else:
                preview = {
                    "title": content.get("title") or content.get("topic_id"),
                    "expected_topics": content.get("discussion_questions") or [],
                }
        out.append({"key": key, "label_zh": m["label_zh"], "character_zh": m["character_zh"], "preview": preview})
    return out


def _pick_content_and_instructions(mode_key, difficulty=None):
    """依 mode 抽 Case 或 Topic，回傳 (content_id, content_dict, instructions)。內容庫是空的時 content 為 None。"""
    if mode_key == "academic":
        topic = get_random_topic(difficulty)
        return (topic.get("topic_id") if topic else None, topic, build_professor_instructions(topic))
    case = get_random_case(difficulty)
    return (case.get("case_id") if case else None, case, build_patient_instructions(case))


def _expected_topics(mode_key, content):
    content = content or {}
    return content.get("expected_topics") if mode_key == "clinical" else content.get("discussion_questions")


def mint_ephemeral_session(openai_client, redis_client, mode_key, difficulty=None):
    """
    開始一場新對話：抽 Case/Topic → 組 instructions → 建 Realtime ephemeral secret
    → 建立 Session Manager 記錄。回傳前端需要的一切（含 session_id）；失敗回傳 None。
    """
    mode_key = mode_key if mode_key in MODES else DEFAULT_MODE
    mode = MODES[mode_key]
    content_id, content, instructions = _pick_content_and_instructions(mode_key, difficulty)

    session_config = {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": instructions,
        "audio": {
            "output": {"voice": mode["voice"]},
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
        # expected_topic/discussion_question 當開場方向
        "opening_hint": (topics or [None])[0] or "Start the conversation naturally in English.",
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
