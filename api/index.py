# -*- coding: utf-8 -*-
import io
import glob
import os
import random
import re
import threading
import time
import base64
import json
import secrets
import tempfile
import traceback
from datetime import date, datetime, timezone

# Startup ENV check (names only, no values) for Railway
REDIS_URL = os.getenv("REDIS_URL", "").strip()
print("ENV CHECK: REDIS_URL exists:", bool(REDIS_URL))
print("ENV CHECK: LINE_CHANNEL_ACCESS_TOKEN exists:", bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN")))
print("ENV CHECK: LINE_CHANNEL_SECRET exists:", bool(os.getenv("LINE_CHANNEL_SECRET")))
print("ENV CHECK: OPENAI_API_KEY exists:", bool(os.getenv("OPENAI_API_KEY")))

from flask import Flask, request, abort, Response
import requests
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage,
    QuickReply, QuickReplyButton, MessageAction, PostbackAction, FlexSendMessage, URIAction,
)
from linebot.models.send_messages import AudioSendMessage
from redis import Redis as RedisClient
from pymongo import MongoClient
from openai import OpenAI
import httpx
from httpx_retries import RetryTransport, Retry
import cloudinary
import cloudinary.uploader

try:
    from api.syllabus import (
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        is_course_inquiry_intent,
        build_course_inquiry_flex,
        get_now_taipei,
        OFF_TOPIC_REPLY,
    )
    from api.learning import (
        log_question,
        set_last_question,
        get_last_question,
        set_last_assistant_message,
        get_last_assistant_message,
        append_conv_history,
        get_conv_history,
        set_quiz_pending,
        get_quiz_pending,
        clear_quiz_pending,
        set_user_state,
        get_user_state,
        set_quiz_data,
        get_quiz_data,
        clear_quiz_data,
        STATE_NORMAL,
        STATE_QUIZ_WAITING,
        record_weak_category,
        get_weak_categories,
        clear_weak_category,
        get_last_review_ask,
        set_last_review_ask,
        set_pending_review_category,
        get_pending_review_category,
        clear_pending_review_category,
        generate_dynamic_quiz,
        generate_mcq_quiz,
        reveal_quiz_answer,
        judge_quiz_answer,
        generate_review_note,
        set_mcq_quiz_data,
    )
    from api.research_logging import (
        ensure_user,
        get_interaction_count,
        get_last_interaction_timestamp,
        get_follow_up_count_within_sec,
        classify_qa_intent_and_complexity,
        classify_qa_learning_tags,
        log_interaction,
        update_interaction_quiz_result,
        log_quiz_result,
        log_speaking,
        log_writing,
        count_tcm_terms_in_text,
        run_analytics_middleware,
        generate_review_quiz_from_interactions,
        log_student_feedback,
    )
except ImportError:
    from syllabus import (
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        is_course_inquiry_intent,
        build_course_inquiry_flex,
        get_now_taipei,
        OFF_TOPIC_REPLY,
    )
    from learning import (
        log_question,
        set_last_question,
        get_last_question,
        set_last_assistant_message,
        get_last_assistant_message,
        append_conv_history,
        get_conv_history,
        set_quiz_pending,
        get_quiz_pending,
        clear_quiz_pending,
        set_user_state,
        get_user_state,
        set_quiz_data,
        get_quiz_data,
        clear_quiz_data,
        STATE_NORMAL,
        STATE_QUIZ_WAITING,
        record_weak_category,
        get_weak_categories,
        clear_weak_category,
        get_last_review_ask,
        set_last_review_ask,
        set_pending_review_category,
        get_pending_review_category,
        clear_pending_review_category,
        generate_dynamic_quiz,
        generate_mcq_quiz,
        reveal_quiz_answer,
        judge_quiz_answer,
        generate_review_note,
        set_mcq_quiz_data,
    )
    from research_logging import (
        ensure_user,
        get_interaction_count,
        get_last_interaction_timestamp,
        get_follow_up_count_within_sec,
        classify_qa_intent_and_complexity,
        log_interaction,
        log_quiz_result,
        log_speaking,
        log_writing,
        count_tcm_terms_in_text,
        run_analytics_middleware,
    )
except ImportError:
    try:
        from research_logging import (
            ensure_user,
            get_interaction_count,
            get_last_interaction_timestamp,
            get_follow_up_count_within_sec,
            classify_qa_intent_and_complexity,
            classify_qa_learning_tags,
            log_interaction,
            update_interaction_quiz_result,
            log_quiz_result,
            log_speaking,
            log_writing,
            count_tcm_terms_in_text,
            run_analytics_middleware,
            generate_review_quiz_from_interactions,
            log_student_feedback,
        )
    except ImportError:
        def _noop_user(*a, **k):
            return 0
        def _noop_ts(*a, **k):
            return None
        def _noop_classify(*a, **k):
            return (None, None)
        ensure_user = get_interaction_count = get_follow_up_count_within_sec = _noop_user
        get_last_interaction_timestamp = _noop_ts
        classify_qa_intent_and_complexity = _noop_classify
        log_interaction = lambda *a, **k: None
        update_interaction_quiz_result = log_quiz_result = log_speaking = log_writing = lambda *a, **k: None
        count_tcm_terms_in_text = lambda t: 0
        run_analytics_middleware = lambda *a, **k: None
        generate_review_quiz_from_interactions = lambda *a, **k: None
        log_student_feedback = lambda *a, **k: None
        classify_qa_learning_tags = lambda *a, **k: (None, None)
        append_conv_history = lambda *a, **k: None
        get_conv_history = lambda *a, **k: []

# 1. 初始化
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
line_webhook_handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
# 使用 httpx + RetryTransport 緩解連線瞬斷
_retry = Retry(total=3, backoff_factor=0.5)
_http_client = httpx.Client(
    transport=RetryTransport(retry=_retry),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), http_client=_http_client)
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

# Redis：Railway 使用 REDIS_URL，標準 redis-py 連線（decode_responses=True 回傳 str）
redis = None
if REDIS_URL:
    try:
        redis = RedisClient.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
        )
        redis.ping()
        print(">>> SUCCESS: Connected to Railway Redis via REDIS_URL <<<")
    except Exception as e:
        print(f">>> ERROR: Failed to connect to Redis: {e} <<<")
        redis = None

# MongoDB：Railway 使用 MONGO_URL，標準 pymongo 連線（嚴格避免默認連到 localhost）
MONGO_URL = os.getenv("MONGO_URL", "").strip()
print(f">>> BOOT: Loading MONGO_URL (length: {len(MONGO_URL)})")

mongo_client = None
mongo_db = None
if not MONGO_URL:
    print(">>> CRITICAL ERROR: MONGO_URL is empty! Check Railway Variables. <<<")
else:
    try:
        # 明確指定 URI 與連線 timeout，避免使用預設 localhost:27017
        mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        mongo_db = mongo_client.get_database("line-tcm-bot")
        print(f">>> BOOT SUCCESS: MongoDB is ready! db={getattr(mongo_db, 'name', None)} <<<")
        # 讓 Compass 直接看到 collection（第一次寫入也會自動建立；這裡只是加速可見性）
        try:
            existing = set(mongo_db.list_collection_names())
            if "StudentFeedback" not in existing:
                mongo_db.create_collection("StudentFeedback")
                print(">>> BOOT: created collection StudentFeedback <<<")
        except Exception as e:
            print(f">>> BOOT: create_collection StudentFeedback skipped err={e}")
    except Exception as e:
        print(f">>> BOOT ERROR: MongoDB connection failed: {e}")
        mongo_client = None
        mongo_db = None

# 模式快取：Redis 瞬斷時使用，key=user_id -> (mode, timestamp)
_mode_cache = {}
_MODE_CACHE_TTL = 180
_MODE_CACHE_MAX = 1000
# 序列化 Redis 存取，避免多 thread 同時呼叫 Upstash 造成 "Device or resource busy"
_redis_mode_lock = threading.Lock()

# Cloudinary 設定（TTS 語音檔雲端儲存）
_cloudinary_configured = bool(
    os.getenv("CLOUDINARY_CLOUD_NAME")
    and os.getenv("CLOUDINARY_API_KEY")
    and os.getenv("CLOUDINARY_API_SECRET")
)
if _cloudinary_configured:
    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    )

# 安全聲明：涉及中醫診斷之回覆必須附加（詳細回答 + 參考出處後加此句）
SAFETY_DISCLAIMER = "\n\n以上資料僅供參考，若有身體不適請務必尋求專業醫師診斷與建議。"
SAFETY_DISCLAIMER_EN = "\n\nThe above information is for reference only. Please seek professional medical advice if you have any health concerns."

USER_LANGUAGE_KEY = "user_language:{user_id}"

VOICE_COACH_TTS_VOICE = "shimmer"
TTS_SPEED = 0.8  # shadowing 語音 0.8 倍速，較慢易於跟讀
VOICE_ERROR_MSG = "抱歉，語音生成出了一點問題，請再試一次。"
TIMEOUT_SECONDS = 28  # Assistant + RAG 常需 15–30 秒；保留 buffer 避開 Vercel 預設 30s
TIMEOUT_MESSAGE = "正在努力翻閱典籍/資料中，請稍候再問我一次。"
FORCE_PUSH_MODE = os.getenv("LINE_FORCE_PUSH", "true").strip().lower() in ("1", "true", "yes", "on")
ENABLE_QUIZ_GENERATION = os.getenv("ENABLE_QUIZ_GENERATION", "true").strip().lower() in ("1", "true", "yes", "on")
# 英文版部署時設 FORCE_LANG=en，強制所有回覆使用英文，不依賴動態語言偵測
FORCE_LANG = os.getenv("FORCE_LANG", "").strip().lower()  # "en" | "" (空=動態偵測)

# --- 口說練習：糾錯與分析大腦 ---
def _evaluate_speech(transcript):
    """
    糾錯與分析：檢查語法、拼寫、用詞、語義完整性。
    回傳 (status: "Correct"|"NeedsImprovement", feedback_text: str, corrected_text: str 用於 TTS)。
    """
    if not (transcript or "").strip():
        return "Correct", "", ""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an English language and TCM content coach. Analyze the student's speech transcript and check:\n"
                        "1. Grammar errors, misspellings, unnatural word choice\n"
                        "2. Semantic completeness\n"
                        "3. TCM content accuracy — if the sentence contains TCM concepts (herbs, formulas, organs, pathology, etc.), check if the content is factually correct\n"
                        "Return JSON:\n"
                        '{"status": "Correct" or "NeedsImprovement", "feedback": "brief feedback covering both language and any TCM content errors with short correct explanation", "corrected": "corrected sentence if status is NeedsImprovement, else empty string"}\n'
                        "Status: Correct = language and content both correct; NeedsImprovement = any language OR content error."
                    ),
                },
                {"role": "user", "content": f"Student's speech: {transcript[:500]}"},
            ],
            max_tokens=250,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        # 嘗試從 code block 或純文字中提取 JSON
        candidates = []
        if "```" in raw_text:
            for seg in raw_text.split("```"):
                seg = seg.strip().lstrip("json").strip()
                if seg.startswith("{"):
                    candidates.append(seg)
        candidates.append(raw_text)
        for candidate in candidates:
            try:
                obj = json.loads(candidate)
                status = (obj.get("status") or "Correct").strip()
                if status not in ("Correct", "NeedsImprovement"):
                    status = "Correct" if obj.get("correct", True) else "NeedsImprovement"
                feedback = (obj.get("feedback") or "").strip()[:400]
                corrected = (obj.get("corrected") or "").strip()[:500]
                return status, feedback, corrected
            except Exception:
                pass
    except Exception:
        traceback.print_exc()
    return "Correct", "", ""


def _generate_next_practice_sentence(prev_transcript=""):
    """
    依據使用者上一句的主題，動態生成一句難度略高的中醫英文練習句。
    失敗時回傳 None，由呼叫端決定是否略過。
    """
    try:
        context_hint = f'The student just practiced: "{prev_transcript.strip()[:200]}".\n' if (prev_transcript or "").strip() else ""
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a TCM (Traditional Chinese Medicine) English speaking coach. "
                        "Generate ONE English sentence for the student to practice next. "
                        "Requirements: related to TCM concepts, slightly more challenging than the previous sentence, "
                        "natural spoken English, 10-20 words. "
                        "Return ONLY the sentence itself, no quotation marks, no explanation."
                    ),
                },
                {"role": "user", "content": f"{context_hint}Generate the next practice sentence."},
            ],
            max_tokens=60,
            temperature=0.8,
        )
        sentence = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        return sentence if sentence else None
    except Exception:
        return None


def _upload_tts_to_cloudinary(audio_bytes, sentence=""):
    """上傳 TTS 語音至 Cloudinary（BytesIO 串流、video 資源型別優化音訊），回傳 (secure_url, duration_ms)。"""
    if not _cloudinary_configured or not audio_bytes:
        return (None, 0)
    try:
        result = cloudinary.uploader.upload(
            io.BytesIO(audio_bytes),
            resource_type="video",  # 音訊用 video 型別，支援轉碼與 CDN 優化
            folder="tts",
            use_filename=True,
            unique_filename=True,
        )
        url = result.get("secure_url")
        if url:
            base_dur = max(1000, int(len(sentence.split()) / 2.2 * 1000))
            duration_ms = int(base_dur / TTS_SPEED)
            return (url, duration_ms)
    except Exception:
        traceback.print_exc()
    return (None, 0)


def _generate_tts_and_store(sentence, voice=None):
    """OpenAI TTS (model: tts-1) 產生語音，直接 BytesIO 串流上傳 Cloudinary，無硬碟寫入。"""
    voice = voice or "shimmer"
    if not (sentence or "").strip():
        return (None, 0)
    token = secrets.token_urlsafe(12)
    vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
    if vercel_url:
        base_url = f"https://{vercel_url}" if not vercel_url.startswith("http") else vercel_url
    else:
        base_url = (request.host_url.rstrip("/") if request else "") or "https://placeholder.vercel.app"
    try:
        resp = client.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=sentence[:4096],
            speed=TTS_SPEED,
        )
        audio_bytes = resp.content
        base_dur = max(1000, int(len(sentence.split()) / 2.2 * 1000))
        duration_ms = int(base_dur / TTS_SPEED)

        # 優先上傳 Cloudinary，取得 HTTPS Secure URL
        if _cloudinary_configured:
            cloud_url, cloud_dur = _upload_tts_to_cloudinary(audio_bytes, sentence)
            if cloud_url:
                return (cloud_url, cloud_dur or duration_ms)

        # 後備：存 Redis，使用 /audio/<token> 路由
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        try:
            if redis:
                redis.set(f"tts_audio:{token}", b64, ex=600)
        except Exception:
            pass
        return (f"{base_url}/audio/{token}", duration_ms)
    except Exception:
        traceback.print_exc()
        return (None, 0)

# --- 課務查詢 Flex Message（與本週重點整合）---
def send_course_inquiry_flex(user_id, reply_token=None):
    """發送課務查詢 Flex Message（含當週/下週切換、AI 重點、評量、重要日期）。reply_token 有值則 reply，否則 push。"""
    bubble = build_course_inquiry_flex(client)
    flex_msg = FlexSendMessage(alt_text="📋 課務查詢與本週重點", contents=bubble, quick_reply=quick_reply_items())
    # 與星等回饋併送時，避免「同一個 reply 內多則訊息都帶 quick_reply」造成 LINE API 失敗
    flex_msg_no_qr = FlexSendMessage(alt_text="📋 課務查詢與本週重點", contents=bubble)
    # 課務助教：回覆後自動詢問滿意度（測試期間不做 24h 節流）
    feedback_msg = _build_course_feedback_message()
    if reply_token:
        # 以同一個 reply_token 一次送出兩則訊息；quick reply 只放在最後一則（feedback）
        try:
            line_bot_api.reply_message(reply_token, [flex_msg_no_qr, feedback_msg])
        except Exception as e:
            print(f">>> DEBUG: send_course_inquiry_flex reply_message failed err={e}")
            # fallback：至少回覆課務查詢，回饋改用 push
            try:
                line_bot_api.reply_message(reply_token, flex_msg)
            except Exception as e2:
                print(f">>> DEBUG: send_course_inquiry_flex fallback reply flex failed err={e2}")
            try:
                line_bot_api.push_message(user_id, feedback_msg)
            except Exception:
                pass
    else:
        try:
            line_bot_api.push_message(user_id, flex_msg)
        except Exception as e:
            print(f">>> DEBUG: send_course_inquiry_flex push flex failed err={e}")
        try:
            line_bot_api.push_message(user_id, feedback_msg)
        except Exception as e:
            print(f">>> DEBUG: send_course_inquiry_flex push feedback failed err={e}")

# --- QuickReply ---
def quick_reply_items():
    if FORCE_LANG == "en":
        return QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label="Speaking Practice", text="Speaking Practice")),
                QuickReplyButton(action=MessageAction(label="Writing Revision", text="Writing Revision")),
            ]
        )
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="口說練習", text="口說練習")),
            QuickReplyButton(action=MessageAction(label="寫作修改", text="寫作修改")),
            QuickReplyButton(action=MessageAction(label="課務查詢", text="課務查詢")),
        ]
    )

def text_with_quick_reply(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_items())


_FEEDBACK_ASK_KEY = "last_feedback_ask:{user_id}"
_FEEDBACK_ASK_COOLDOWN_SEC = 24 * 3600


def quick_reply_feedback_stars():
    return QuickReply(
        items=[
            QuickReplyButton(action=PostbackAction(label="⭐1", data="action=feedback&score=1")),
            QuickReplyButton(action=PostbackAction(label="⭐2", data="action=feedback&score=2")),
            QuickReplyButton(action=PostbackAction(label="⭐3", data="action=feedback&score=3")),
            QuickReplyButton(action=PostbackAction(label="⭐4", data="action=feedback&score=4")),
            QuickReplyButton(action=PostbackAction(label="⭐5", data="action=feedback&score=5")),
        ]
    )


def _should_ask_feedback(user_id):
    # 測試期間：暫時關閉 24 小時節流，方便反覆測試
    return True


def _mark_feedback_asked(user_id):
    # 測試期間：暫時不寫入 last_feedback_ask
    return


def _build_course_feedback_message():
    """
    課務助教回覆後的回饋訊息（測試用：每次都送）。
    星等用 Quick Reply（postback），表單連結直接放在文字內。
    """
    return TextSendMessage(
        text="感謝您的使用！歡迎填寫表單讓我們知道你的意見！https://forms.gle/xUpm5yZSvzEZ6zMh6",
        quick_reply=quick_reply_feedback_stars(),
    )

def quick_reply_speak_practice():
    """口說練習：要再練習下一句嗎？[練習下一句] [結束練習]。"""
    if FORCE_LANG == "en":
        return QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label="Next Sentence", text="Next Sentence")),
                QuickReplyButton(action=MessageAction(label="End Practice", text="End Practice")),
            ]
        )
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="練習下一句", text="練習下一句")),
            QuickReplyButton(action=MessageAction(label="結束練習", text="結束練習")),
        ]
    )

def text_with_quick_reply_speak_practice(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_speak_practice())

def quick_reply_quiz_ask():
    """每個回答後詢問：要來試試一題小測驗嗎？[是, 否]。"""
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="是", text="是")),
            QuickReplyButton(action=MessageAction(label="否", text="否")),
        ]
    )

def text_with_quick_reply_quiz(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_quiz_ask())


def quick_reply_quiz_choices():
    """測驗題 A/B/C 選項：以 Postback 送出 quiz_choice=A/B/C，供後端更新 MongoDB quiz_data。"""
    return QuickReply(
        items=[
            QuickReplyButton(action=PostbackAction(label="(A)", data="quiz_choice=A")),
            QuickReplyButton(action=PostbackAction(label="(B)", data="quiz_choice=B")),
            QuickReplyButton(action=PostbackAction(label="(C)", data="quiz_choice=C")),
        ]
    )


def build_quiz_flex_message(question):
    """建立測驗題目 Flex Message（學生的回答將視為新問題）。"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "📝 一題小測驗", "weight": "bold", "size": "lg"},
                {"type": "text", "text": question, "wrap": True, "size": "sm"},
            ],
        },
    }
    alt = f"小測驗：{(question or '')[:80]}"
    if len(question or "") > 80:
        alt += "..."
    return FlexSendMessage(alt_text=alt, contents=bubble)

# --- 時間解鎖小測驗：沿用 syllabus 時間邏輯 ---
# 使用 __file__ 取得安全路徑，避免 Vercel 上 cwd 或 _DATA_DIR 未定義導致 500
_QUIZ_ALL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tcm_quiz_all.json")
_QUIZ_ALL_PATH = os.path.normpath(os.path.abspath(_QUIZ_ALL_PATH))
# 模組解鎖日（台灣日期，與 syllabus 對齊）：p0 隨時；p1 2026-03-14；p2 2026-03-21；p3 2026-04-11；p4 2026-05-09
_QUIZ_UNLOCK_DATES = {
    "p0": date(2000, 1, 1),
    "p1": date(2026, 3, 14),
    "p2": date(2026, 3, 21),
    "p3": date(2026, 4, 11),
    "p4": date(2026, 5, 9),
}
_TCM_QUIZ_ALL_CACHE = None

def _load_timed_quiz_pool():
    """載入 data/tcm_quiz_all.json，依 get_now_taipei() 篩選已解鎖題目，回傳 list[dict]。"""
    global _TCM_QUIZ_ALL_CACHE
    try:
        now = get_now_taipei()
        today = now.date() if hasattr(now, "date") else date(now.year, now.month, now.day)
    except Exception as e:
        traceback.print_exc()
        today = date.today()
    if _TCM_QUIZ_ALL_CACHE is None:
        try:
            with open(_QUIZ_ALL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _TCM_QUIZ_ALL_CACHE = (data.get("questions") or [])
        except Exception as e:
            traceback.print_exc()
            print(f"[QUIZ] Failed to load {_QUIZ_ALL_PATH!r}: {e}")
            _TCM_QUIZ_ALL_CACHE = []
    unlocked = []
    for q in _TCM_QUIZ_ALL_CACHE:
        if not isinstance(q, dict):
            continue
        mod = (q.get("module") or "p0").strip().lower()
        unlock_date = _QUIZ_UNLOCK_DATES.get(mod)
        if unlock_date is not None and today >= unlock_date:
            unlocked.append(q)
    return unlocked

def build_timed_quiz_flex_message(question_obj):
    """
    將單題題目封裝為 LINE Flex Message。
    含：題目、選項、正確答案（背景色區隔）、解析（背景色區隔）。
    question_obj: dict 含 id, question, options, answer, analysis
    """
    q = question_obj.get("question") or ""
    options = question_obj.get("options") or []
    ans = (question_obj.get("answer") or "").strip().upper()
    analysis = (question_obj.get("analysis") or "").strip()
    body_contents = [
        {"type": "text", "text": "📝 時間解鎖小測驗", "weight": "bold", "size": "lg"},
        {"type": "text", "text": q, "wrap": True, "size": "md"},
    ]
    for opt in options[:10]:
        if isinstance(opt, str):
            body_contents.append({"type": "text", "text": opt, "wrap": True, "size": "sm"})
    body_contents.append({"type": "separator", "margin": "md"})
    body_contents.append({
        "type": "box",
        "layout": "vertical",
        "contents": [{"type": "text", "text": f"✅ 正確答案：{ans}", "weight": "bold", "size": "sm"}],
        "backgroundColor": "#E8F5E9",
        "paddingAll": "md",
        "cornerRadius": "sm",
    })
    body_contents.append({
        "type": "box",
        "layout": "vertical",
        "contents": [{"type": "text", "text": f"📖 解析：{analysis}", "wrap": True, "size": "sm"}],
        "backgroundColor": "#E3F2FD",
        "paddingAll": "md",
        "cornerRadius": "sm",
        "margin": "md",
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
    alt = f"時間解鎖小測驗：{(q or '')[:60]}..."
    return FlexSendMessage(alt_text=alt, contents=bubble)

def time_locked_quiz_handler(user_id, reply_token=None):
    """
    時間解鎖小測驗：依 get_now_taipei() 篩選已解鎖題目，隨機抽一題，以 Flex 回覆。
    reply_token 有值則 reply_message，否則 push_message。
    讀取失敗或異常時回傳友善訊息，避免 500。
    """
    try:
        pool = _load_timed_quiz_pool()
        if not pool:
            msg = TextSendMessage(text="目前沒有可用的題目，請稍後再試。")
            if reply_token:
                line_bot_api.reply_message(reply_token, msg)
            else:
                line_bot_api.push_message(user_id, msg)
            return
        chosen = random.choice(pool)
        flex_msg = build_timed_quiz_flex_message(chosen)
        if reply_token:
            line_bot_api.reply_message(reply_token, flex_msg)
        else:
            line_bot_api.push_message(user_id, flex_msg)
    except Exception as e:
        traceback.print_exc()
        print(f"[QUIZ] time_locked_quiz_handler error: {e}")
        try:
            fallback = TextSendMessage(text="小測驗暫時無法使用，請稍後再試。")
            if reply_token:
                line_bot_api.reply_message(reply_token, fallback)
            else:
                line_bot_api.push_message(user_id, fallback)
        except Exception:
            pass

def quick_reply_review_ask():
    """主動複習：需要幫你整理複習筆記嗎？[要, 不要]。"""
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="要", text="要複習筆記")),
            QuickReplyButton(action=MessageAction(label="不要", text="不要複習筆記")),
        ]
    )

def text_with_quick_reply_review_ask(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_review_ask())

# --- 寫作修訂模式：獨立處理，不經過 Assistant API / RAG ---
REVISION_MODE = "writing"
REVISION_MODE_PROMPT = "你已在【✍️ 寫作修訂】模式～請貼上要修改的段落。"
REDIS_KEY_USER_MODE = "user_mode"  # 與 Postback/切換按鈕寫入的 Key 完全一致：user_mode:{user_id}

# 寫作模式 prompt：回饋需含下列內容，但不要輸出標題給使用者
_REVISION_PROMPT = (
    "你是專業溫暖的語言老師，同時具備中醫學術背景。回覆時請自然融入以下內容，不要輸出【】標題：\n"
    "（1）鼓勵／正面肯定\n"
    "（2）【語言層面】若有語法、用詞、拼寫錯誤：說明原因＋修正後的版本（用 **粗體** 標示修改處）；若無語言錯誤則稱讚原文道地\n"
    "（3）【內容層面】若句子涉及中醫知識且有觀念錯誤（如藥方主治、病機、臟腑功能等），請明確指出錯誤並附上簡短的正確說明；若中醫內容正確則無需提及\n"
    "（4）鼓勵繼續發問、貼上其他句子練習\n"
    "語氣溫暖，段落分明易讀。"
)

_REVISION_PROMPT_EN = (
    "You are a warm and professional language teacher with a background in Traditional Chinese Medicine (TCM). "
    "Naturally incorporate the following into your response — do not output section headers:\n"
    "(1) Encouragement and positive affirmation\n"
    "(2) [Language] If there are grammar, vocabulary, or spelling errors: explain the issue and provide a corrected version "
    "(use **bold** to highlight changes); if there are no errors, praise the writing as natural and well-expressed\n"
    "(3) [TCM Content] If the text involves TCM concepts and contains factual errors (e.g., formula indications, pathomechanisms, organ functions), "
    "clearly point out the error with a brief correct explanation; if the TCM content is accurate, no need to mention it\n"
    "(4) Encourage the user to continue practicing and send more sentences\n"
    "Keep a warm tone with clear, readable paragraphs. Respond entirely in English."
)

def _revision_handler(user_id, text):
    """
    寫作修訂：gpt-4o-mini + Chat Completion，非串流以加速。結果以 push_message 送出。
    """
    if not user_id or not str(user_id).strip():
        print(f"[REVISION] ERROR: user_id invalid or empty user_id={repr(user_id)}")
        return
    if not (text or "").strip():
        try:
            prompt_msg = "Please paste the paragraph you'd like to revise." if FORCE_LANG == "en" else "請貼上要修改的段落。"
            line_bot_api.push_message(user_id, text_with_quick_reply_writing(prompt_msg))
        except Exception as e:
            print(f"[REVISION] push_message failed (empty text branch) err={e}")
            traceback.print_exc()
        return
    try:
        print(f"[REVISION] start user_id={user_id} text_len={len(text)}")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("[REVISION] ERROR: OPENAI_API_KEY not set")
            line_bot_api.push_message(user_id, text_with_quick_reply_writing("系統設定錯誤，請稍後再試。"))
            return
        if FORCE_LANG == "en":
            revision_system = _REVISION_PROMPT_EN
            revision_user = f"Analyze the following sentence or paragraph:\n{text[:1000]}"
        else:
            revision_system = _REVISION_PROMPT
            revision_user = f"分析以下句子或段落：\n{text[:1000]}"
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": revision_system},
                {"role": "user", "content": revision_user},
            ],
            max_tokens=600,
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            reply = "已收到你的練習！歡迎繼續貼上其他句子～"
        print(f"[REVISION] done user_id={user_id} reply_len={len(reply)}")
        # 研究用：Writing 原始/修訂與 improvement index
        try:
            if mongo_db is not None:
                ensure_user(mongo_db, user_id)
                log_writing(mongo_db, user_id, text, reply)
                run_analytics_middleware(mongo_db, user_id)
        except Exception as e:
            print(f">>> RESEARCH log_writing error: {e}")
        line_bot_api.push_message(user_id, text_with_quick_reply_writing(reply))
    except Exception as e:
        print(f"[REVISION] CRITICAL err={e}")
        traceback.print_exc()
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply_writing("An error occurred, please try again." if FORCE_LANG == "en" else "處理時發生錯誤，請再試一次。"))
        except Exception as push_err:
            print(f"[REVISION] push_message (error fallback) failed err={push_err}")

def quick_reply_writing():
    """寫作修訂模式：回到中醫問答按鈕。"""
    if FORCE_LANG == "en":
        return QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label="Back to TCM Q&A", text="TCM Q&A")),
            ]
        )
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="回到中醫問答", text="回到中醫問答")),
        ]
    )

def text_with_quick_reply_writing(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_writing())

def _redis_user_mode_key(user_id):
    """統一的 Redis Key，與 Postback/切換按鈕寫入處完全一致。"""
    return f"{REDIS_KEY_USER_MODE}:{user_id}"

# --- 中醫問答：tcm_master_knowledge.json + OpenAI gpt-4o-mini（純 OpenAI）---
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_TCM_JSON_CACHE = None
_TCM_FULL_CONTEXT_CACHE = None

def _load_tcm_json():
    """載入 data/tcm_master_knowledge.json，快取。"""
    global _TCM_JSON_CACHE
    if _TCM_JSON_CACHE is not None:
        return _TCM_JSON_CACHE
    paths = glob.glob(os.path.join(_DATA_DIR, "tcm_*.json"))
    out = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                if d and isinstance(d, dict):
                    out.append(d)
        except Exception:
            pass
    _TCM_JSON_CACHE = out
    return _TCM_JSON_CACHE

def _build_full_tcm_context():
    """將 tcm_master_knowledge.json 全部知識點序列化為文字 context（快取）。"""
    global _TCM_FULL_CONTEXT_CACHE
    if _TCM_FULL_CONTEXT_CACHE is not None:
        return _TCM_FULL_CONTEXT_CACHE
    parts = []
    for data in _load_tcm_json():
        for kp in data.get("knowledge_points") or []:
            block = []
            if kp.get("category"):
                block.append(f"【{kp['category']}】")
            if kp.get("core_logic"):
                block.append(kp["core_logic"])
            if kp.get("mechanism"):
                block.append(kp["mechanism"])
            for cr in (kp.get("causal_relationships") or []):
                if isinstance(cr, dict):
                    block.append(f"{cr.get('emotion','')}→{cr.get('impact','')}：{cr.get('symptoms','')}")
            for pf in (kp.get("pathological_features") or []):
                if isinstance(pf, dict):
                    block.append(f"{pf.get('evil','')}：{pf.get('features','')}")
            for row in (kp.get("five_elements_table") or []):
                if isinstance(row, dict):
                    block.append(json.dumps(row, ensure_ascii=False))
            for qa in (kp.get("student_qa") or []):
                if isinstance(qa, str):
                    block.append(qa)
            if kp.get("interactions"):
                for k, v in (kp["interactions"] or {}).items():
                    block.append(f"{k}: {v}")
            for ii in (kp.get("inspection_items") or []):
                if isinstance(ii, dict):
                    block.append(ii.get("item", "") + ": " + (ii.get("logic") or ", ".join(ii.get("types", []))))
            if kp.get("mapping"):
                for k, v in (kp["mapping"] or {}).items():
                    block.append(f"{k}: {v}")
            for feat in (kp.get("features") or []):
                if isinstance(feat, dict):
                    block.append(feat.get("type", "") + ": " + (feat.get("logic") or ""))
                    for d in (feat.get("details") or []):
                        if isinstance(d, dict):
                            block.append(json.dumps(d, ensure_ascii=False))
            for item in (kp.get("items") or []):
                if isinstance(item, dict):
                    block.append(f"{item.get('name','')}: {item.get('logic','')}")
            for d in (kp.get("details") or []):
                if isinstance(d, dict):
                    label = d.get("type") or d.get("item", "")
                    block.append(f"{label}: {d.get('logic','')}")
            for t in (kp.get("types") or []):
                if isinstance(t, dict):
                    block.append(f"{t.get('name','')}: {t.get('logic','')}")
            if kp.get("functions"):
                block.append(kp["functions"])
            for m in (kp.get("methods") or []):
                if isinstance(m, dict):
                    block.append(f"{m.get('name','')}: {m.get('details','')}")
            for cc in (kp.get("common_conditions") or []):
                if isinstance(cc, str):
                    block.append(cc)
            for tq in (kp.get("ten_questions_logic") or []):
                if isinstance(tq, dict):
                    block.append(f"{tq.get('item','')}: {tq.get('logic','')}")
            if kp.get("pulse_mapping"):
                for k, v in (kp["pulse_mapping"] or {}).items():
                    block.append(f"{k}: {v}")
            for cp in (kp.get("common_pulses") or []):
                if isinstance(cp, dict):
                    block.append(f"{cp.get('pulse','')}: {cp.get('logic','')}")
            if block:
                parts.append("\n".join(block))
    result = "\n\n".join(parts) if parts else ""
    _TCM_FULL_CONTEXT_CACHE = result
    return result

_TCM_SYSTEM_PROMPT = """
你是一位嚴謹且親切的中醫學術助教。在回答任何問題時，請遵循以下原則：
1. 優先從「課程教材」、「中醫經典文獻（如：黃帝內經、傷寒雜病論、神農本草經）」以及「PubMed 上的現代醫學論文」中提取資訊。
2. 嚴禁自行推斷或編造未經證實的療效。若資料庫中無相關記載，請誠實告知。
3. 回答必須結構清晰，並在文末明確列出【資料來源】（包含書名、章節或論文標題）。
4. 始終保持專業、客觀的語氣，並在結尾附上醫療警語。
5. 避免產生幻覺，不確定的資訊不要提供。
6. 若使用者提出與中醫無直接關聯的一般性問題（如飲食、生活習慣），請嘗試從中醫養生或食療的角度給予建議，並結合課程內容引導學習。

輸出格式要求：
- 先給出「回答」內容（條列或分段皆可，務必清楚）。
- 文末一定要有一段「資料來源：」列出本次回答實際使用的來源（至少 1 條；若無可用來源，請寫明「資料來源：無（資料庫未收錄/不足以支持）」）。
""".strip()

_TCM_SYSTEM_PROMPT_EN = """
You are a knowledgeable and friendly academic assistant specializing in Traditional Chinese Medicine (TCM). When answering any question, follow these principles:
1. Prioritize information from course materials, TCM classical texts (e.g., Huangdi Neijing, Shang Han Lun, Shen Nong Ben Cao Jing), and modern medical papers on PubMed.
2. Never fabricate or infer unverified therapeutic effects. If the information is not in the knowledge base, say so honestly.
3. Answers must be clearly structured, with sources listed at the end (book title, chapter, or paper title).
4. Maintain a professional and friendly tone at all times, with a medical disclaimer at the end.
5. Avoid hallucinations — do not provide information you are uncertain about.
6. If the user asks a general question not directly related to TCM (e.g., food, lifestyle), try to offer advice from a TCM dietary or wellness perspective, and gently connect it back to course content.

Output format:
- Start with the answer (bullet points or paragraphs, must be clear).
- End with a "Sources:" section listing actual sources used (at least 1; if none available, write "Sources: none (not in knowledge base)").
- Respond entirely in English.
""".strip()


def _ensure_sources_section(text: str, english: bool = False) -> str:
    """確保回覆末尾包含來源區段（保底防漏）。"""
    t = (text or "").strip()
    if not t:
        return t
    if english:
        if "Source:" in t or "Sources:" in t:
            return t
        return t + "\n\nSources: none (not in knowledge base)"
    else:
        if "資料來源：" in t or "Source:" in t or "Sources:" in t:
            return t
        return t + "\n\n資料來源：無（資料庫未收錄/不足以支持）"


def _is_english_input(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    has_latin = bool(re.search(r"[A-Za-z]", t))
    has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf\uF900-\uFAFF]", t))
    return has_latin and not has_cjk


def _set_user_language(user_id, lang):
    if not redis or not user_id or not lang:
        return
    try:
        redis.set(USER_LANGUAGE_KEY.format(user_id=user_id), lang, ex=7 * 24 * 3600)
    except Exception:
        pass


def _get_user_language(user_id):
    if not redis or not user_id:
        return "zh"
    try:
        val = redis.get(USER_LANGUAGE_KEY.format(user_id=user_id))
        if val is None:
            return "zh"
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        return str(val or "zh").strip().lower() or "zh"
    except Exception:
        return "zh"


def _maybe_send_review_prompt(user_id, reply_token=None):
    weak = get_weak_categories(redis, user_id, min_count=2)
    if not weak:
        return False
    if (time.time() - get_last_review_ask(redis, user_id)) <= 7 * 24 * 3600:
        return False
    category = next(iter(weak.keys()), None)
    if not category:
        return False
    set_last_review_ask(redis, user_id)
    set_pending_review_category(redis, user_id, category)
    user_lang = "en" if FORCE_LANG == "en" else _get_user_language(user_id)
    if user_lang == "en":
        review_msg = text_with_quick_reply_review_ask(f"I noticed you are less confident with '{category}'. Would you like a review note?")
    else:
        review_msg = text_with_quick_reply_review_ask(f"發現你對「{category}」這部分較不熟，需要幫你整理複習筆記嗎？")
    try:
        if FORCE_PUSH_MODE or not reply_token:
            line_bot_api.push_message(user_id, review_msg)
        else:
            line_bot_api.reply_message(reply_token, review_msg)
    except Exception as e:
        print(f">>> DEBUG: review prompt failed err={e}")
    return True

# 模組載入時預熱 TCM 快取，減少首次問答延遲
try:
    _load_tcm_json()
    _build_full_tcm_context()
except Exception:
    pass


def _start_loading_indicator(user_id, loading_seconds=20):
    """
    呼叫 LINE Chat Loading API，在聊天室顯示打字動畫（三個點）。
    不消耗 push 額度，動畫最長持續 loading_seconds 秒後自動消失。
    失敗不影響主流程。
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    if not token or not user_id:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            json={"chatId": user_id, "loadingSeconds": loading_seconds},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=3,
        )
    except Exception as e:
        print(f"[LOADING] failed err={e}")

def _log_interaction_to_mongodb_async(user_id, text, ai_reply, is_eng):
    """
    背景非同步記錄：chat_history + interaction + research logging。
    避免阻塞主流程。
    """
    if mongo_db is None:
        print(">>> LOGGING ERROR: db instance is None, skipping async logging")
        return
    try:
        # 取得 LINE 使用者名稱
        user_name = None
        try:
            prof = line_bot_api.get_profile(user_id)
            user_name = getattr(prof, "display_name", None)
        except Exception:
            user_name = None

        # 寫入 chat_history
        mongo_db.chat_history.insert_one(
            {
                "user_id": (user_name or "").strip()[:200] or user_id,
                "userId": user_id,
                "userName": (user_name or "").strip()[:200] or None,
                "question": text,
                "answer": ai_reply,
                "timestamp": datetime.now(timezone.utc),
                "source": "unified_loop",
            }
        )
        print(f">>> MONGODB: Successfully logged message from {user_id}")

        # 寫入研究資料
        try:
            ensure_user(mongo_db, user_id)
            count_before = get_interaction_count(mongo_db, user_id)
            last_ts = get_last_interaction_timestamp(mongo_db, user_id)
            now_utc = datetime.now(timezone.utc)
            session_duration_sec = (now_utc - last_ts).total_seconds() if last_ts else 0
            follow_up = get_follow_up_count_within_sec(mongo_db, user_id, within_sec=1800)
            intent_tag, complexity_score = classify_qa_intent_and_complexity(client, text)
            interaction_id = log_interaction(
                mongo_db,
                user_id,
                "QA",
                text,
                ai_reply,
                intent_tag=intent_tag,
                complexity_score=complexity_score,
                session_duration_sec=session_duration_sec,
                follow_up_count=follow_up,
                feedback_requested=((count_before + 1) % 20 == 0),
            )
            # Redis：寫入 quiz_interaction_id，供測驗作答時更新
            if interaction_id and redis:
                try:
                    redis.set(f"quiz_interaction_id:{user_id}", str(interaction_id), ex=3600)
                except Exception:
                    pass
            run_analytics_middleware(mongo_db, user_id)
        except Exception as e:
            print(f">>> RESEARCH LOGGING ERROR: {e}")
    except Exception as e:
        print(f">>> MONGODB ERROR: Failed to log message: {e}")


def _tcm_openai_reply(user_id, text, reply_token=None):
    """
    以 tcm_master_knowledge.json 為 context，用 OpenAI gpt-4o-mini 生成回覆。
    先關鍵字匹配，有匹配用精簡 context；無匹配用完整 JSON。不經過 Assistant API。
    回傳 True 若已回覆，False 若失敗。
    優先使用 reply_token 以避免 push 額度限制。
    """
    if not (text or "").strip():
        return False
    import time

    txt = text.strip()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return False
    start_ts = time.time()
    all_data = _load_tcm_json()
    ctx_parts = []
    for data in all_data:
        for kp in data.get("knowledge_points") or []:
            cat = (kp.get("category") or "").split("(")[0].strip()
            terms = [cat] if len(cat) >= 2 else []
            if "五行" in cat:
                terms.append("五行")
            for cr in (kp.get("causal_relationships") or []):
                if isinstance(cr, dict):
                    for k in ("emotion", "target_organ"):
                        v = cr.get(k, "")
                        if isinstance(v, str) and len(v) >= 1:
                            terms.extend(v.replace("/", " ").split())
            for pf in (kp.get("pathological_features") or []):
                if isinstance(pf, dict):
                    v = pf.get("evil", "")
                    if isinstance(v, str):
                        terms.append(v.split("(")[0].strip())
            for row in (kp.get("five_elements_table") or []):
                if isinstance(row, dict):
                    for k in ("organ", "element"):
                        v = row.get(k, "")
                        if isinstance(v, str) and len(v) >= 2:
                            terms.append(v)
            for qa in (kp.get("student_qa") or []):
                if isinstance(qa, str) and "：" in qa:
                    q = qa.split("：", 1)[0].strip().replace("？", "").replace("?", "")
                    if 2 <= len(q) <= 25:
                        terms.append(q)
            for ii in (kp.get("inspection_items") or []):
                if isinstance(ii, dict):
                    v = ii.get("item", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            if "望診" in cat or "舌" in cat:
                terms.extend(["望診", "舌診", "舌"])
            for k in (kp.get("mapping") or {}):
                if isinstance(k, str) and len(k) >= 2:
                    terms.append(k.split("(")[0].strip())
            for feat in (kp.get("features") or []):
                if isinstance(feat, dict):
                    v = feat.get("type", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            for item in (kp.get("items") or []):
                if isinstance(item, dict):
                    v = item.get("name", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            for d in (kp.get("details") or []):
                if isinstance(d, dict):
                    v = (d.get("type") or d.get("item", "")).strip()
                    if len(v) >= 2:
                        terms.append(v.split("(")[0].strip())
            for t in (kp.get("types") or []):
                if isinstance(t, dict):
                    v = t.get("name", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            for m in (kp.get("methods") or []):
                if isinstance(m, dict):
                    v = m.get("name", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            if "經絡" in cat or "穴位" in cat or "針灸" in cat or "刺灸" in cat:
                terms.extend(["經絡", "穴位", "針灸", "刺灸", "阿是穴", "得氣", "灸法", "放血"])
            for tq in (kp.get("ten_questions_logic") or []):
                if isinstance(tq, dict):
                    v = tq.get("item", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            if "聞診" in cat or "問診" in cat or "十問" in cat or "切診" in cat or "脈" in cat:
                terms.extend(["聞診", "問診", "十問歌", "切診", "脈診", "脈"])
            for k in (kp.get("pulse_mapping") or {}):
                if isinstance(k, str) and len(k) >= 2:
                    terms.append(k)
            for cp in (kp.get("common_pulses") or []):
                if isinstance(cp, dict):
                    v = cp.get("pulse", "").split("(")[0].strip()
                    if len(v) >= 2:
                        terms.append(v)
            if any(t in txt for t in terms if t and len(t) >= 2):
                if kp.get("core_logic"):
                    ctx_parts.append(kp["core_logic"])
                if kp.get("mechanism"):
                    ctx_parts.append(kp["mechanism"])
                cr = kp.get("causal_relationships")
                if cr:
                    lines = [f"{r.get('emotion','')}→{r.get('impact','')}：{r.get('symptoms','')}" for r in cr if isinstance(r, dict)]
                    ctx_parts.append("；".join(lines))
                for row in (kp.get("five_elements_table") or []):
                    if isinstance(row, dict):
                        ctx_parts.append(json.dumps(row, ensure_ascii=False))
                if kp.get("interactions"):
                    for k, v in (kp["interactions"] or {}).items():
                        ctx_parts.append(f"{k}: {v}")
                pf = kp.get("pathological_features")
                if pf:
                    lines = [f"{r.get('evil','')}：{r.get('features','')}" for r in pf if isinstance(r, dict)]
                    ctx_parts.append("；".join(lines))
                for qa in (kp.get("student_qa") or []):
                    if isinstance(qa, str):
                        ctx_parts.append(qa)
                for ii in (kp.get("inspection_items") or []):
                    if isinstance(ii, dict):
                        ctx_parts.append(ii.get("item", "") + ": " + (ii.get("logic") or ", ".join(ii.get("types", []))))
                if kp.get("mapping"):
                    for k, v in (kp["mapping"] or {}).items():
                        ctx_parts.append(f"{k}: {v}")
                for feat in (kp.get("features") or []):
                    if isinstance(feat, dict):
                        ctx_parts.append(feat.get("type", "") + ": " + (feat.get("logic") or ""))
                        for d in (feat.get("details") or []):
                            if isinstance(d, dict):
                                ctx_parts.append(json.dumps(d, ensure_ascii=False))
                for item in (kp.get("items") or []):
                    if isinstance(item, dict):
                        ctx_parts.append(f"{item.get('name','')}: {item.get('logic','')}")
                for d in (kp.get("details") or []):
                    if isinstance(d, dict):
                        label = d.get("type") or d.get("item", "")
                        ctx_parts.append(f"{label}: {d.get('logic','')}")
                for t in (kp.get("types") or []):
                    if isinstance(t, dict):
                        ctx_parts.append(f"{t.get('name','')}: {t.get('logic','')}")
                if kp.get("functions"):
                    ctx_parts.append(kp["functions"])
                for m in (kp.get("methods") or []):
                    if isinstance(m, dict):
                        ctx_parts.append(f"{m.get('name','')}: {m.get('details','')}")
                for cc in (kp.get("common_conditions") or []):
                    if isinstance(cc, str):
                        ctx_parts.append(cc)
                for tq in (kp.get("ten_questions_logic") or []):
                    if isinstance(tq, dict):
                        ctx_parts.append(f"{tq.get('item','')}: {tq.get('logic','')}")
                if kp.get("pulse_mapping"):
                    for k, v in (kp["pulse_mapping"] or {}).items():
                        ctx_parts.append(f"{k}: {v}")
                for cp in (kp.get("common_pulses") or []):
                    if isinstance(cp, dict):
                        ctx_parts.append(f"{cp.get('pulse','')}: {cp.get('logic','')}")
    ctx = "\n".join(ctx_parts)[:2000] if ctx_parts else _build_full_tcm_context()[:4000]
    if not ctx or not ctx.strip():
        return False
    try:
        is_eng = True if FORCE_LANG == "en" else _is_english_input(txt)
        if is_eng:
            system_prompt = _TCM_SYSTEM_PROMPT_EN
            disclaimer = SAFETY_DISCLAIMER_EN
            user_question = f"[Context]\n{ctx}\n\n[Question]\n{txt}\n\nPlease answer based on the context, with a clear structure and source references."
        else:
            system_prompt = _TCM_SYSTEM_PROMPT
            disclaimer = SAFETY_DISCLAIMER
            user_question = f"[背景資料]\n{ctx}\n\n[問題]\n{txt}\n\n請根據背景資料精準回答（可適度詳盡），跳過冗長開場白，回答末尾請簡要註明參考資料或出處。"

        # 帶入最近 3 輪對話歷史，讓 GPT 能理解上下文追問
        history = get_conv_history(redis, user_id)
        messages = [{"role": "system", "content": system_prompt}]
        for turn in history:
            messages.append({"role": "user", "content": turn.get("u", "")})
            messages.append({"role": "assistant", "content": turn.get("a", "")})

        # 有對話歷史（追問）時，要求先給重點再分項說明
        if history:
            if is_eng:
                user_question += "\n\nFormat: start with a **Key Point** summary (1-2 sentences), then provide detailed breakdown in numbered points."
            else:
                user_question += "\n\n格式要求：先用 1-2 句話給出【重點摘要】，再分項條列詳細說明。"

        messages.append({"role": "user", "content": user_question})

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=800,
            temperature=0.2,
        )
        base_reply = (resp.choices[0].message.content or "").strip()[:800]
        base_reply = _ensure_sources_section(base_reply, english=is_eng)
        ai_reply = base_reply + disclaimer

        # MongoDB 寫入改為背景非同步（不阻塞答復流程）
        threading.Thread(
            target=_log_interaction_to_mongodb_async,
            args=(user_id, text, ai_reply, is_eng),
            daemon=True,
        ).start()

        # QA → Quiz：改為非同步背景創題，不阻塞答復流程。
        # 先立即回覆答案（reply），測驗稍後非同步 push。
        if ENABLE_QUIZ_GENERATION:
            threading.Thread(target=_process_quiz_sync, args=(user_id, base_reply, "en" if is_eng else "zh"), daemon=True).start()

        # 回覆：只回覆答案（無測驗訊息），根據 FORCE_PUSH_MODE 決定是否 push。
        ai_msg = text_with_quick_reply(ai_reply)
        try:
            if FORCE_PUSH_MODE:
                line_bot_api.push_message(user_id, ai_msg)
            elif reply_token:
                line_bot_api.reply_message(reply_token, ai_msg)
            else:
                line_bot_api.push_message(user_id, ai_msg)
        except Exception as e:
            print(f">>> DEBUG: tcm reply/push failed err={e}")

        try:
            # 設定使用者語言偏好（reply 之後，不在 critical path）
            _set_user_language(user_id, "en" if is_eng else "zh")
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            append_conv_history(redis, user_id, txt, base_reply)
        except Exception:
            pass

        return True
    except Exception:
        traceback.print_exc()
        return False

def _get_cached_mode(user_id):
    """Redis 失敗時從本地快取讀取最近一次成功的模式。"""
    now = time.time()
    if user_id in _mode_cache:
        mode, ts = _mode_cache[user_id]
        if now - ts < _MODE_CACHE_TTL:
            return mode
        try:
            del _mode_cache[user_id]
        except KeyError:
            pass
    return None

def _set_cached_mode(user_id, mode):
    """寫入模式快取，供 Redis 瞬斷時 fallback。"""
    now = time.time()
    while len(_mode_cache) >= _MODE_CACHE_MAX:
        try:
            oldest = min(_mode_cache.items(), key=lambda x: x[1][1])
            del _mode_cache[oldest[0]]
        except (ValueError, KeyError):
            break
    _mode_cache[user_id] = (mode, now)

def _safe_get_mode(user_id):
    """
    安全取得使用者模式。Key 與 Postback 寫入處一致。
    回傳 Redis 內的值（含 'tcm'/'speaking'/'writing'/'quiz'），僅在 key 真正缺失或為空時才 fallback 至 tcm。
    快取優先；Redis 存取以 lock 序列化。
    """
    try:
        cached = _get_cached_mode(user_id)
        if cached:
            print(f"DEBUG: Fetching mode for {user_id}. Result: {cached}")
            return cached
        if not redis:
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=redis_none")
            print(f"DEBUG: Fetching mode for {user_id}. Result: tcm")
            return "tcm"
        key = _redis_user_mode_key(user_id)
        mode_val = None
        for attempt in range(3):
            try:
                with _redis_mode_lock:
                    mode_val = redis.get(key)
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                # Redis 重試後仍失敗：嘗試快取
                cached = _get_cached_mode(user_id)
                if cached:
                    err_detail = f"errno={getattr(e, 'errno', 'N/A')} type={type(e).__name__}"
                    print(f"[MODE] _safe_get_mode user_id={user_id} redis_fail using_cache={cached} {err_detail}")
                    print(f"DEBUG: Fetching mode for {user_id}. Result: {cached}")
                    return cached
                err_detail = f"errno={getattr(e, 'errno', 'N/A')} type={type(e).__name__}"
                print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=exception_after_retry {err_detail} err={e}")
                traceback.print_exc()
                print(f"DEBUG: Fetching mode for {user_id}. Result: tcm")
                return "tcm"
        if mode_val is None:
            cached = _get_cached_mode(user_id)
            if cached:
                print(f"[MODE] _safe_get_mode user_id={user_id} key_missing using_cache={cached}")
                print(f"DEBUG: Fetching mode for {user_id}. Result: {cached}")
                return cached
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=key_missing_or_null")
            print(f"DEBUG: Fetching mode for {user_id}. Result: tcm")
            return "tcm"
        if isinstance(mode_val, bytes):
            mode_str = mode_val.decode("utf-8", errors="replace").strip()
        else:
            mode_str = str(mode_val).strip()
        if not mode_str:
            cached = _get_cached_mode(user_id)
            if cached:
                print(f"DEBUG: Fetching mode for {user_id}. Result: {cached}")
                return cached
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=empty_value raw={repr(mode_val)}")
            print(f"DEBUG: Fetching mode for {user_id}. Result: tcm")
            return "tcm"
        result = mode_str.lower()
        _set_cached_mode(user_id, result)
        print(f"DEBUG: Fetching mode for {user_id}. Result: {result}")
        return result
    except Exception as e:
        cached = _get_cached_mode(user_id)
        if cached:
            print(f"[MODE] _safe_get_mode user_id={user_id} outer_exception using_cache={cached} err={e}")
            print(f"DEBUG: Fetching mode for {user_id}. Result: {cached}")
            return cached
        print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=exception err={e}")
        print(f"DEBUG: Fetching mode for {user_id}. Result: tcm")
        return "tcm"

# --- AI 核心函數（模式路由器）---
# _process_assistant_sync / _revision_handler 均在背景 thread 執行，可安全存取模組全域
#（line_bot_api, redis, client）及 os.environ，無須額外傳遞。
def _process_assistant_sync(user_id, text):
    """Assistant API 邏輯：Thread/Run/RAG，完成後 push_message。供 process-text-async 背景呼叫。"""
    try:
        mode = _safe_get_mode(user_id)
        if mode == REVISION_MODE:
            _revision_handler(user_id, text)
            return
        tag = "🩺 中醫問答"
        if mode == "speaking":
            tag = "🗣️ 口說練習"
        elif mode == "writing":
            tag = "✍️ 寫作修訂"

        thread_id = None
        try:
            if redis:
                t_id = redis.get(f"user_thread:{user_id}")
                if t_id is not None:
                    thread_id = t_id.decode("utf-8") if hasattr(t_id, "decode") else str(t_id)
                    if thread_id == "None" or not thread_id.strip():
                        thread_id = None
        except Exception:
            pass

        if not thread_id:
            new_thread = client.beta.threads.create()
            thread_id = new_thread.id
            try:
                if redis:
                    redis.set(f"user_thread:{user_id}", thread_id)
            except Exception:
                pass

        if mode == "writing":
            mode_instructions = get_writing_mode_instructions()
        else:
            mode_instructions = get_rag_instructions()

        user_content = f"{mode_instructions}\n\n【{tag}】\n使用者的話：{text}"
        if mode == "tcm":
            user_content += "\n(提醒：回答末尾請提供參考資料出處)"

        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_content,
        )
        run = client.beta.threads.runs.create_and_poll(
            thread_id=thread_id,
            assistant_id=assistant_id,
            timeout=TIMEOUT_SECONDS,
        )

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            if mode == "tcm":
                ai_reply = ai_reply.rstrip() + SAFETY_DISCLAIMER
            # 注意：push_message 可能因 LINE 月額度限制而失敗（429）
            try:
                line_bot_api.push_message(user_id, text_with_quick_reply(ai_reply))
            except Exception as e:
                print(f">>> DEBUG: push_message failed (likely quota). err={e}")
            try:
                log_question(redis, user_id, text)
                set_last_question(redis, user_id, text)
                set_last_assistant_message(redis, user_id, ai_reply)
            except Exception:
                pass
        else:
            try:
                line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
            except Exception as e:
                print(f">>> DEBUG: push_message TIMEOUT failed err={e}")
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception as e:
            print(f">>> DEBUG: push_message TIMEOUT failed err={e}")


def _run_ai_work(user_id, text, is_voice=False):
    """依 mode 分派：REVISION_MODE → _revision_handler；其餘 → _process_assistant_sync。"""
    try:
        mode = _safe_get_mode(user_id)
        print(f"[MODE] _run_ai_work user_id={user_id} mode={mode} routing={'revision' if mode == REVISION_MODE else 'assistant'}")
        if mode == REVISION_MODE:
            _revision_handler(user_id, text)
            return
        _process_assistant_sync(user_id, text)
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception:
            pass


def process_ai_request(event, user_id, text, is_voice=False):
    """
    State-Based Router：依 user_state (mode) 切換，直接執行 AI 邏輯。
    寫作模式 → _revision_handler；其餘 → _process_assistant_sync（內含 create_and_poll）。
    """
    try:
        _run_ai_work(user_id, text, is_voice=is_voice)
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception:
            pass


def _run_text_background(user_id, text, task, base_url, cron_secret):
    """Background Task：觸發 process-text-async 或本地執行，不阻塞 webhook。"""
    print(f"[TEXT_BG] start user_id={user_id} task={task} has_base={bool(base_url)} has_secret={bool(cron_secret)}")
    if base_url and cron_secret:
        try:
            r = requests.post(
                f"{base_url}/api/process-text-async",
                json={"user_id": user_id, "text": text, "task": task},
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=30,
            )
            print(f"[TEXT_BG] POST result status={r.status_code}")
        except Exception as e:
            print(f"[TEXT_BG] POST failed, fallback local err={e}")
            traceback.print_exc()
            try:
                if task == "revision":
                    _revision_handler(user_id, text)
                else:
                    _process_assistant_sync(user_id, text)
            except Exception as inner:
                print(f"[TEXT_BG] fallback handler failed err={inner}")
                traceback.print_exc()
    else:
        try:
            if task == "revision":
                _revision_handler(user_id, text)
            else:
                _process_assistant_sync(user_id, text)
        except Exception as e:
            print(f"[TEXT_BG] direct handler failed err={e}")
            traceback.print_exc()

# --- 每週報告 Cron（需 CRON_SECRET 驗證）---
try:
    from api.weekly_report import run_weekly_report
except ImportError:
    from weekly_report import run_weekly_report

@app.route("/api/cron/weekly", methods=['GET', 'POST'])
def cron_weekly_report():
    """每週固定時間由 Vercel Cron 或外部排程呼叫，產出 PDF 並寄送至 REPORT_EMAIL。"""
    secret = request.headers.get("Authorization") or request.args.get("secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret != expected and secret != "Bearer " + expected:
        return "Unauthorized", 401
    try:
        ok, msg = run_weekly_report(redis, client)
        return (msg, 200) if ok else (msg, 500)
    except Exception as e:
        traceback.print_exc()
        return str(e)[:200], 500

# --- 路由設定 ---
@app.route("/", methods=['GET'])
def home():
    return 'Line Bot Server is running!', 200

@app.route("/favicon.ico", methods=['GET'])
@app.route("/favicon.png", methods=['GET'])
def favicon():
    """避免瀏覽器/爬蟲請求 favicon 產生 404 日誌。"""
    return "", 204

def _run_voice_background(user_id, message_id, base_url, cron_secret):
    """Background Task：語音轉錄、GPT 分析、TTS、Cloudinary 上傳。不阻塞 webhook 回傳。"""
    if base_url and cron_secret:
        try:
            requests.post(
                f"{base_url}/api/process-voice-async",
                json={"user_id": user_id, "message_id": message_id},
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=30,
            )
        except Exception:
            try:
                _process_voice_sync(user_id, message_id)
            except Exception:
                traceback.print_exc()
    else:
        try:
            _process_voice_sync(user_id, message_id)
        except Exception:
            traceback.print_exc()


def _process_voice_sync(user_id, message_id):
    """
    語音處理：Whisper 辨識 -> GPT 評估 -> TTS -> Cloudinary。
    一律用 push_message 回傳，錯誤時主動 push 友善提示。
    """
    if not user_id or not str(user_id).strip():
        print(f"[VOICE] ERROR: user_id invalid user_id={repr(user_id)}")
        return
    try:
        print(f"[VOICE] start user_id={user_id} message_id={message_id}")
        message_content = line_bot_api.get_message_content(message_id)
        tmp_dir = tempfile.gettempdir()
        temp_path = os.path.join(tmp_dir, f"{message_id}.m4a")
        try:
            with open(temp_path, "wb") as f:
                for chunk in message_content.iter_content():
                    f.write(chunk)
        except Exception:
            temp_path = os.path.join(os.path.dirname(__file__) or ".", f"{message_id}.m4a")
            with open(temp_path, "wb") as f:
                for chunk in message_content.iter_content():
                    f.write(chunk)

        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        if os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        transcript_text = (transcript.text or "").strip()
        if FORCE_LANG == "en":
            transcription_msg = f"🎤 Recognized: \"{transcript_text}\""
        else:
            transcription_msg = f"🎤 辨識內容：「{transcript_text}」"
        line_bot_api.push_message(user_id, TextSendMessage(text=transcription_msg))

        mode = _safe_get_mode(user_id)

        # 口說模式：記錄 transcript 長度與 TCM 術語次數
        if mode == "speaking" and mongo_db is not None:
            try:
                ensure_user(mongo_db, user_id)
                log_speaking(
                    mongo_db,
                    user_id,
                    len(transcript_text),
                    count_tcm_terms_in_text(transcript_text),
                    transcript_text,
                )
                run_analytics_middleware(mongo_db, user_id)
            except Exception as e:
                print(f">>> RESEARCH log_speaking error: {e}")

        if mode == REVISION_MODE:
            _revision_handler(user_id, transcript_text)
            print(f"[VOICE] done revision path")
            return
        if mode == "speaking":
            status, feedback, corrected_text = _evaluate_speech(transcript_text)
            is_en_speaking = FORCE_LANG == "en"
            if status == "Correct":
                next_sentence = _generate_next_practice_sentence(transcript_text)
                if is_en_speaking:
                    praise = "Great pronunciation! Well done! 🎉\n\n🔊 Listen to the model pronunciation:"
                    if next_sentence:
                        next_msg = f"💡 Try this next:\n\"{next_sentence}\"\n\nSend a voice message to practice, or record your own sentence!"
                    else:
                        next_msg = "Ready for the next sentence?"
                else:
                    praise = "發音非常標準！太棒了！🎉\n\n🔊 聆聽示範語音："
                    if next_sentence:
                        next_msg = f"💡 建議下一句：\n「{next_sentence}」\n\n直接傳語音跟著唸，或錄你自己想練習的句子都可以！"
                    else:
                        next_msg = "要再練習下一句嗎？"
                line_bot_api.push_message(user_id, TextSendMessage(text=praise))
                tts_err_msg = "Sorry, audio generation failed. Please try again." if is_en_speaking else VOICE_ERROR_MSG
                try:
                    audio_url, duration_ms = _generate_tts_and_store(transcript_text, voice=VOICE_COACH_TTS_VOICE)
                    if audio_url and duration_ms:
                        line_bot_api.push_message(user_id, AudioSendMessage(original_content_url=audio_url, duration=duration_ms))
                    else:
                        line_bot_api.push_message(user_id, TextSendMessage(text=tts_err_msg))
                except Exception as tts_err:
                    print(f"[VOICE] TTS err (Correct path): {tts_err}")
                    line_bot_api.push_message(user_id, TextSendMessage(text=tts_err_msg))
                line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(next_msg))
                print(f"[VOICE] done speaking Correct")
                return
            feedback_header = "📊 Speaking Practice Feedback" if is_en_speaking else "📊 口說練習回饋"
            line_bot_api.push_message(
                user_id,
                text_with_quick_reply(f"{feedback_header}\n\n{feedback}"),
            )
            text_for_tts = corrected_text.strip() if corrected_text else transcript_text
            if is_en_speaking:
                tts_label = f"🔊 Listen and repeat: \"{text_for_tts}\""
                tts_sent_msg = "Demo audio sent! Ready for the next sentence?"
                tts_err_msg = "Sorry, audio generation failed. Please try again."
            else:
                tts_label = f"🔊 請跟著唸：「{text_for_tts}」"
                tts_sent_msg = "示範語音已送上，要再練習下一句嗎？"
                tts_err_msg = VOICE_ERROR_MSG
            line_bot_api.push_message(user_id, TextSendMessage(text=tts_label))
            try:
                audio_url, duration_ms = _generate_tts_and_store(text_for_tts, voice=VOICE_COACH_TTS_VOICE)
                if audio_url and duration_ms:
                    line_bot_api.push_message(
                        user_id,
                        AudioSendMessage(original_content_url=audio_url, duration=duration_ms),
                    )
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply_speak_practice(tts_sent_msg),
                    )
                else:
                    line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(tts_err_msg))
            except Exception as tts_err:
                print(f"[VOICE] TTS/Cloudinary err={tts_err}")
                traceback.print_exc()
                line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(tts_err_msg))
            print(f"[VOICE] done speaking NeedsImprovement")
            return
        if is_course_inquiry_intent(transcript_text):
            line_bot_api.push_message(user_id, TextSendMessage(text="正在查詢課務資料..."))
            send_course_inquiry_flex(user_id)
        elif is_off_topic(transcript_text):
            line_bot_api.push_message(user_id, text_with_quick_reply(OFF_TOPIC_REPLY))
        else:
            process_ai_request(None, user_id, transcript_text, is_voice=True)
        print(f"[VOICE] done other mode")
    except Exception as e:
        print(f"[VOICE] CRITICAL err={e}")
        traceback.print_exc()
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))
        except Exception:
            pass


def _process_quiz_sync(user_id, context, language="zh"):
    """
    依據中醫回答內容 context 產生三選一小測驗並 push 給使用者。
    先以同步 redis.set 寫入 state 與 mode（TTL 1 小時），驗證後再送題目。
    language 由呼叫端傳入（"en"/"zh"），避免 race condition。
    """
    if not (context or "").strip():
        return
    try:
        quiz = generate_mcq_quiz(client, context, language=language)
    except Exception:
        traceback.print_exc()
        quiz = None
    if not (quiz and quiz.get("question") and quiz.get("options") and quiz.get("answer")):
        return
    try:
        # 同步寫入：state 與 mode 直接 redis.set，不經 background，TTL 至少 1 小時
        if redis:
            state_key = f"user_state:{user_id}"
            mode_key = _redis_user_mode_key(user_id)
            redis.set(state_key, STATE_QUIZ_WAITING, ex=3600)
            redis.set(mode_key, "quiz", ex=3600)
            verify_state = redis.get(state_key)
            verify_mode = redis.get(mode_key)
            if isinstance(verify_state, bytes):
                verify_state = verify_state.decode("utf-8", errors="replace").strip()
            else:
                verify_state = str(verify_state or "").strip()
            if isinstance(verify_mode, bytes):
                verify_mode = verify_mode.decode("utf-8", errors="replace").strip()
            else:
                verify_mode = str(verify_mode or "").strip()
            print(f"DEBUG: Write Verification - state key expected '{STATE_QUIZ_WAITING}', got '{verify_state}'")
            print(f"DEBUG: Write Verification - mode key expected 'quiz', got '{verify_mode}'")
            quiz_id = secrets.token_hex(8)
            set_mcq_quiz_data(
                redis,
                user_id,
                quiz.get("question", ""),
                quiz.get("options", []),
                quiz.get("answer", ""),
                quiz.get("explanation", ""),
                category="其他",
                quiz_id=quiz_id,
            )
            set_quiz_pending(redis, user_id, quiz.get("question", ""))
            print(f"DEBUG: Successfully updated {user_id} to quiz mode")
        if language == "en":
            quiz_text = (
                "——\n📝 Quiz\n"
                + quiz["question"]
                + "\n"
                + "\n".join(quiz["options"])
                + "\n\n(Select A/B/C to answer, or send a new question to continue learning.)"
            )
        else:
            quiz_text = (
                "——\n📝 小測驗\n"
                + quiz["question"]
                + "\n"
                + "\n".join(quiz["options"])
                + "\n\n(點選 A/B/C 作答，或直接輸入新問題繼續學習喔！)"
            )
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=quiz_text, quick_reply=quick_reply_quiz_choices()),
        )
        if redis:
            try:
                redis.set(f"quiz_sent_at:{user_id}", str(time.time()), ex=3600)
            except Exception:
                pass
    except Exception:
        traceback.print_exc()


@app.route("/api/process-voice-async", methods=["POST"])
def process_voice_async():
    """Background Task：接收語音 message_id，執行 Whisper -> 評估 -> TTS -> Cloudinary -> push。"""
    secret = request.headers.get("Authorization") or request.headers.get("X-Internal-Secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret not in (expected, "Bearer " + expected):
        return "Unauthorized", 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = (data.get("user_id") or "").strip()
        message_id = (data.get("message_id") or "").strip()
        if not user_id or not message_id:
            return "Missing user_id or message_id", 400
        _process_voice_sync(user_id, message_id)
        return "OK", 200
    except Exception as e:
        traceback.print_exc()
        try:
            line_bot_api.push_message(
                (request.get_json(force=True, silent=True) or {}).get("user_id", ""),
                text_with_quick_reply("❌ 語音辨識或處理失敗，請再試一次。"),
            )
        except Exception:
            pass
        return str(e)[:200], 500


def _run_process_text_task(user_id, text, task):
    """Background worker for process-text-async：完成後 push_message。"""
    try:
        if task == "revision":
            _revision_handler(user_id, text)
        else:
            _process_assistant_sync(user_id, text)
        print(f"[process-text-async] done task={task}")
    except Exception as e:
        print(f"[process-text-async] CRITICAL err={e}")
        traceback.print_exc()
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception as push_err:
            print(f"[process-text-async] push error fallback failed err={push_err}")


@app.route("/api/process-text-async", methods=["POST"])
def process_text_async():
    """Background Task：接收文字 AI 任務，立即回傳 200，寫作修訂/Assistant RAG 在背景執行並 push_message。"""
    secret = request.headers.get("Authorization") or request.headers.get("X-Internal-Secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret not in (expected, "Bearer " + expected):
        print(f"[process-text-async] 401 Unauthorized")
        return "Unauthorized", 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = (data.get("user_id") or "").strip()
        text = (data.get("text") or "").strip()
        task = (data.get("task") or "assistant").strip().lower()
        print(f"[process-text-async] received user_id={user_id!r} task={task} text_len={len(text)}")
        if not user_id:
            return "Missing user_id", 400
        threading.Thread(
            target=_run_process_text_task,
            args=(user_id, text, task),
            daemon=True,
        ).start()
        return "OK", 200
    except Exception as e:
        print(f"[process-text-async] CRITICAL err={e}")
        traceback.print_exc()
        try:
            uid = (request.get_json(force=True, silent=True) or {}).get("user_id", "")
            if uid:
                line_bot_api.push_message(uid, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception as push_err:
            print(f"[process-text-async] push error fallback failed err={push_err}")
        return str(e)[:200], 500


@app.route("/api/process-quiz-async", methods=["POST"])
def process_quiz_async():
    """
    Background Task：依據已送出的中醫回答內容產生三選一小測驗並推送。
    由 _tcm_openai_reply 觸發，不阻塞原本的 webhook。
    """
    secret = request.headers.get("Authorization") or request.headers.get("X-Internal-Secret") or ""
    expected = os.getenv("CRON_SECRET", "")
    if expected and secret not in (expected, "Bearer " + expected):
        return "Unauthorized", 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = (data.get("user_id") or "").strip()
        context = (data.get("context") or "").strip()
        if not user_id or not context:
            return "Missing user_id or context", 400
        _process_quiz_sync(user_id, context)
        return "OK", 200
    except Exception as e:
        traceback.print_exc()
        return str(e)[:200], 500


@app.route("/audio/<token>", methods=['GET'])
def serve_audio(token):
    """提供 TTS 音檔給 LINE 播放（Redis 暫存，TTL 約 10 分鐘）。"""
    try:
        if not redis:
            return "Not Found", 404
        b64 = redis.get(f"tts_audio:{token}")
        if not b64:
            return "Not Found", 404
        s = b64.decode("ascii") if hasattr(b64, "decode") else b64
        data = base64.b64decode(s)
        return Response(data, mimetype="audio/mpeg", direct_passthrough=True)
    except Exception:
        return "Not Found", 404

@app.route("/callback", methods=['POST'])
def callback():
    """LINE Webhook 唯一入口（Railway 等長連線環境：直接執行 handle，gunicorn timeout 120s）。"""
    signature = request.headers.get('X-Line-Signature') or ''
    body = request.get_data(as_text=True) or ''
    try:
        line_webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        traceback.print_exc()
    return Response('OK', status=200)

def _handle_quiz_answer(user_id, choice, reply_token=None):
    """
    處理測驗作答（文字 A/B/C 或 Postback quiz_choice=A/B/C）。
    更新 MongoDB 對應 interaction 的 quiz_data，並回覆結果。reply_token 有值則 reply_message，否則 push_message。
    """
    qd = get_quiz_data(redis, user_id) or {}
    if qd.get("type") != "mcq":
        if reply_token:
            line_bot_api.reply_message(reply_token, text_with_quick_reply("此題已失效，請輸入新問題繼續學習～"))
        return
    correct = str(qd.get("answer") or "").strip().upper()
    explanation = (qd.get("explanation") or "").strip()
    user_lang = "en" if FORCE_LANG == "en" else _get_user_language(user_id)
    if user_lang == "en":
        guidance = "Hope this helps you understand TCM better! Feel free to ask another question anytime, and I will continue to answer and generate quizzes for you. ✨"
        if choice == correct:
            reply = "Great job! 🎉\n\nYou got it right and your concept is solid.\n\n" + guidance
        else:
            reply = "Oops, not quite.\n\n"
            reply += f"Correct answer: {correct}\n\n"
            if explanation:
                reply += f"Explanation:\n{explanation}\n\n"
            reply += guidance
    else:
        guidance = "希望這能幫助你更了解中醫！隨時可以再輸入新問題，我會繼續為你解答並出題喔！✨"
        if choice == correct:
            reply = "恭喜你答對了！👏\n\n你選對了，觀念掌握得不錯。\n\n" + guidance
        else:
            reply = "哎呀，答錯囉！\n\n"
            reply += f"【正確答案】{correct}\n\n"
            if explanation:
                reply += f"【中醫概念說明】\n{explanation}\n\n"
            reply += guidance
        try:
            record_weak_category(redis, user_id, (qd.get("category") or "其他"))
        except Exception:
            pass
    sent_at = redis.get(f"quiz_sent_at:{user_id}") if redis else None
    response_time_sec = None
    if sent_at is not None:
        try:
            response_time_sec = round(time.time() - float(sent_at), 2)
        except (TypeError, ValueError):
            pass
    if mongo_db is not None:
        try:
            interaction_id_raw = redis.get(f"quiz_interaction_id:{user_id}") if redis else None
            if interaction_id_raw is not None:
                try:
                    oid_str = interaction_id_raw.decode("utf-8", errors="replace").strip() if isinstance(interaction_id_raw, bytes) else str(interaction_id_raw).strip()
                    if oid_str:
                        update_interaction_quiz_result(
                            mongo_db,
                            oid_str,
                            choice,
                            choice == correct,
                            True,
                            response_time_sec=response_time_sec,
                        )
                except Exception as eu:
                    print(f">>> RESEARCH update_interaction_quiz_result error: {eu}")
            log_quiz_result(
                mongo_db,
                user_id,
                (qd.get("quiz_type") or "Immediate"),
                qd.get("quiz_id") or qd.get("question") or "",
                choice,
                choice == correct,
                response_time_sec=response_time_sec,
            )
        except Exception as e:
            print(f">>> RESEARCH QuizResult logging error: {e}")
    try:
        if redis:
            redis.delete(f"quiz_sent_at:{user_id}")
            redis.delete(f"quiz_interaction_id:{user_id}")
        set_user_state(redis, user_id, STATE_NORMAL)
        if redis:
            redis.set(_redis_user_mode_key(user_id), "tcm", ex=86400)
        clear_quiz_data(redis, user_id)
        clear_quiz_pending(redis, user_id)
    except Exception:
        pass
    msg = text_with_quick_reply(reply)
    if reply_token:
        line_bot_api.reply_message(reply_token, msg)
    else:
        line_bot_api.push_message(user_id, msg)


# --- 事件處理 ---
@line_webhook_handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    user_id = event.source.user_id
    try:
        # 課務助教回饋：action=feedback&score=1-5
        if data.startswith("action=feedback"):
            # 解析星等（允許 action=feedback&score=5 或 action=feedback&score=5&...
            score = None
            for part in data.split("&"):
                if part.startswith("score="):
                    score = part.split("=", 1)[1].strip()
                    break

            # 取使用者名稱（失敗不影響主流程）
            user_name = None
            try:
                prof = line_bot_api.get_profile(user_id)
                user_name = getattr(prof, "display_name", None)
            except Exception:
                user_name = None

            # MongoDB：寫入 StudentFeedback（失敗不影響機器人正常運作）
            try:
                if mongo_db is not None:
                    print(f">>> DEBUG: feedback write start db={getattr(mongo_db, 'name', None)} user_id={user_id} score={score} user_name={user_name}")
                    log_student_feedback(mongo_db, user_id=user_id, user_name=user_name, score=score)
                else:
                    print(">>> DEBUG: feedback received but mongo_db is None")
            except Exception as e:
                print(f">>> DEBUG: log_student_feedback unexpected error: {e}")

            # 回覆：優先 reply token，失敗再 fallback push（避免誤顯示『回饋紀錄失敗』）
            msg = text_with_quick_reply("感謝你的回饋！已收到～")
            try:
                line_bot_api.reply_message(event.reply_token, msg)
            except Exception as e:
                print(f">>> DEBUG: feedback reply_message failed, fallback push. err={e}")
                try:
                    line_bot_api.push_message(user_id, msg)
                except Exception:
                    pass
            return

        # 測驗選項：使用者點擊 A/B/C 按鈕（Postback）
        if data.startswith("quiz_choice="):
            choice = data.split("=", 1)[1].strip().upper()
            if choice in ("A", "B", "C"):
                quiz_state = get_user_state(redis, user_id)
                if quiz_state == STATE_QUIZ_WAITING:
                    _handle_quiz_answer(user_id, choice, reply_token=event.reply_token)
                    return
        if data == "action=course" or data == "action=weekly":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return
        if data == "action=timed_quiz":
            time_locked_quiz_handler(user_id, reply_token=event.reply_token)
            return
        # mode=tcm / mode=speaking / mode=writing（Rich Menu 切換）
        mode = data.split("=")[1].strip() if "=" in data else "tcm"
        mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
        _set_cached_mode(user_id, mode)
        redis_ok = False
        try:
            if redis:
                redis.set(_redis_user_mode_key(user_id), mode)
                redis_ok = True
                # 寫入後立即讀回驗證（供除錯）
                verify = redis.get(_redis_user_mode_key(user_id))
                v = verify.decode("utf-8").strip() if isinstance(verify, bytes) else str(verify or "").strip()
                verified = (v == mode)
                print(f"[MODE] Postback user_id={user_id} set_mode={mode} redis_ok={redis_ok} verified={verified}")
        except Exception as e:
            print(f"[MODE] Postback user_id={user_id} set_mode={mode} redis_set_failed err={e}")
        # 與 CLI/文字指令一致的切換訊息（寫作修訂需含操作指引）
        if mode == REVISION_MODE:
            msg = REVISION_MODE_PROMPT
            if not redis:
                msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 REDIS_URL 環境變數。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply_writing(msg))
        elif mode == "speaking":
            msg = "已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(msg))
        else:
            msg = f"已切換至【{mode_map.get(mode, mode)}】模式"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(msg))
    except Exception as e:
        traceback.print_exc()
        try:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("選單處理發生錯誤，請再試一次。"))
        except Exception:
            pass

@line_webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = (event.message.text or "").strip()
    current_mode = _safe_get_mode(user_id)
    print(f"DEBUG: Received text '{user_text}' from {user_id}. Current Mode from Redis: {current_mode}")
    try:
        def _parse_mcq_choice(text):
            t = (text or "").strip()
            if not t:
                return None
            # 移除常見包裹符號與標點
            norm = re.sub(r'^[\s【\[\(（『「〈《<"\'`，。、．\.\!！\?？；;：:、]+', "", t)
            norm = re.sub(r'[\s】\]\)）』」〉》>"\'`，。、．\.\!！\?？；;：:、]+$', "", norm)
            # 全形轉半形
            norm = norm.replace("Ａ", "A").replace("Ｂ", "B").replace("Ｃ", "C")
            up = norm.upper()
            if up in ("A", "B", "C"):
                return up
            if re.match(r"^選\s*[ABC]", up):
                return re.findall(r"[ABC]", up)[0]
            if re.match(r"^\([ABC]\)", up) or re.match(r"^（[ABC]）", up):
                return re.findall(r"[ABC]", up)[0]
            return None

        suppress_yes_no_command = False

        # --- Rich Menu 按鈕：立即回覆，避免延遲 ---
        if user_text in ("中醫問答", "回到中醫問答", "TCM Q&A"):
            _set_cached_mode(user_id, "tcm")
            if redis:
                for _attempt in range(3):
                    try:
                        with _redis_mode_lock:
                            redis.set(_redis_user_mode_key(user_id), "tcm", ex=86400)
                        break
                    except Exception as _e:
                        print(f"[MODE] TCM Q&A redis set failed attempt={_attempt} err={_e}")
                        if _attempt < 2:
                            time.sleep(0.2)
            if FORCE_LANG == "en" or user_text == "TCM Q&A":
                confirm_msg = "Switched to [🩺 TCM Q&A] mode. What would you like to ask?"
            else:
                confirm_msg = "已切換至【🩺 中醫問答】模式，有什麼想問的嗎？"
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply(confirm_msg),
            )
            return
        if user_text in ("口說練習", "Speaking Practice"):
            _set_cached_mode(user_id, "speaking")
            if redis:
                for _attempt in range(3):
                    try:
                        with _redis_mode_lock:
                            redis.set(_redis_user_mode_key(user_id), "speaking", ex=86400)
                        break
                    except Exception as _e:
                        print(f"[MODE] Speaking Practice redis set failed attempt={_attempt} err={_e}")
                        if _attempt < 2:
                            time.sleep(0.2)
            if FORCE_LANG == "en" or user_text == "Speaking Practice":
                confirm_msg = "Switched to [🗣️ Speaking Practice] mode. Send a voice message or type a sentence."
            else:
                confirm_msg = "已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(confirm_msg))
            return
        if user_text in ("寫作修改", "寫作修訂", "Writing Revision"):
            _set_cached_mode(user_id, REVISION_MODE)
            if redis:
                for _attempt in range(3):
                    try:
                        with _redis_mode_lock:
                            redis.set(_redis_user_mode_key(user_id), REVISION_MODE, ex=86400)
                        break
                    except Exception as _e:
                        print(f"[MODE] Writing Revision redis set failed attempt={_attempt} err={_e}")
                        if _attempt < 2:
                            time.sleep(0.2)
            if FORCE_LANG == "en" or user_text == "Writing Revision":
                msg = "You are now in [✍️ Writing Revision] mode. Please paste the paragraph you'd like to revise."
                if not redis:
                    msg += "\n\n⚠️ Mode could not be saved (Redis not configured). Please check the REDIS_URL environment variable."
            else:
                msg = REVISION_MODE_PROMPT
                if not redis:
                    msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 REDIS_URL 環境變數。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply_writing(msg))
            return
        if user_text == "課務查詢":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return
        if user_text == "時間解鎖小測驗":
            time_locked_quiz_handler(user_id, reply_token=event.reply_token)
            return
        if (user_text or "").strip() == "測驗模式":
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply(
                    "您現在就在「中醫問答 ＋ 小測驗」循環中～\n\n"
                    "輸入任何中醫相關問題，我會先回答，再自動出一題小測驗。答完後可繼續問新問題，形成 QA → Quiz → QA → Quiz 的學習循環喔！✨"
                ),
            )
            return

        # --- 寫作修訂模式隔離：優先判斷，跳過中醫邏輯 ---
        # 直接讀 Redis（繞過本地快取），避免 Vercel 多實例快取不同步導致誤判
        current_mode = None
        if redis:
            try:
                _v = redis.get(_redis_user_mode_key(user_id))
                if _v:
                    current_mode = (_v.decode("utf-8") if isinstance(_v, bytes) else str(_v)).strip()
            except Exception:
                pass
        if not current_mode:
            current_mode = _safe_get_mode(user_id)
        print(f"[MODE] handle_message user_id={user_id} current_mode={current_mode} text_preview={user_text[:50]!r}")
        if current_mode == REVISION_MODE:
            print(f"[MODE] handle_message -> REVISION_MODE branch, skipping TCM Assistant")
            if user_text in ("寫作修改", "寫作修訂", "Writing Revision"):
                if FORCE_LANG == "en" or user_text == "Writing Revision":
                    re_enter_msg = "You are now in [✍️ Writing Revision] mode. Please paste the paragraph you'd like to revise."
                else:
                    re_enter_msg = REVISION_MODE_PROMPT
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_writing(re_enter_msg),
                )
                return
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Analyzing your writing, please wait... ✨" if FORCE_LANG == "en" else "正在分析你的寫作，請稍候... ✨"),
            )
            print(f"[REVISION] running sync (worker) user_id={user_id}")
            _revision_handler(user_id, user_text)
            return

        # 課務查詢／本週重點：統一以 Flex Message 回傳
        if is_course_inquiry_intent(user_text):
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        # 小測驗等待作答：A/B/C/D 時再讀一次 state，避免漏掉剛寫入的 quiz 狀態
        quiz_state = get_user_state(redis, user_id)
        if (user_text or "").strip().upper() in ("A", "B", "C", "D"):
            quiz_state = get_user_state(redis, user_id)
        if quiz_state == STATE_QUIZ_WAITING:
            print("DEBUG: Inside Quiz logic block - comparing answer...")
            mode = _safe_get_mode(user_id)
            qd = get_quiz_data(redis, user_id) or {}
            if mode in ("tcm", "quiz") and (qd.get("type") == "mcq"):
                choice = _parse_mcq_choice(user_text)
                if choice:
                    _handle_quiz_answer(user_id, choice, reply_token=event.reply_token)
                    return

                # 非選項：視為跳過，清狀態後把這則當新提問（且不要把「是/否」當作舊題庫指令）
                suppress_yes_no_command = True
                try:
                    if mongo_db is not None and redis:
                        interaction_id_raw = redis.get(f"quiz_interaction_id:{user_id}")
                        if interaction_id_raw is not None:
                            oid_str = interaction_id_raw.decode("utf-8", errors="replace").strip() if isinstance(interaction_id_raw, bytes) else str(interaction_id_raw).strip()
                            if oid_str:
                                try:
                                    update_interaction_quiz_result(
                                        mongo_db,
                                        oid_str,
                                        None,
                                        False,
                                        False,
                                    )
                                except Exception:
                                    pass
                            redis.delete(f"quiz_interaction_id:{user_id}")
                    set_user_state(redis, user_id, STATE_NORMAL)
                    if redis:
                        redis.set(_redis_user_mode_key(user_id), "tcm", ex=86400)
                    clear_quiz_data(redis, user_id)
                    clear_quiz_pending(redis, user_id)
                except Exception:
                    pass
            else:
                # 非 tcm/quiz 或非 MCQ：維持舊相容邏輯（視為新提問）
                try:
                    set_user_state(redis, user_id, STATE_NORMAL)
                    if redis:
                        redis.set(_redis_user_mode_key(user_id), "tcm", ex=86400)
                    clear_quiz_data(redis, user_id)
                    clear_quiz_pending(redis, user_id)
                except Exception:
                    pass

        # 主動複習測驗：依最近 10 筆互動產生個人化複習題
        if user_text in ("複習測驗", "我要複習測驗") and mongo_db is not None:
            try:
                review_quiz = generate_review_quiz_from_interactions(mongo_db, user_id, client, last_n=10)
                if review_quiz and review_quiz.get("question") and review_quiz.get("options") and review_quiz.get("answer"):
                    if redis:
                        redis.set(f"user_state:{user_id}", STATE_QUIZ_WAITING, ex=3600)
                        redis.set(_redis_user_mode_key(user_id), "quiz", ex=3600)
                        quiz_id_r = secrets.token_hex(8)
                        set_mcq_quiz_data(
                            redis,
                            user_id,
                            review_quiz.get("question", ""),
                            review_quiz.get("options", []),
                            review_quiz.get("answer", ""),
                            review_quiz.get("explanation", ""),
                            category="複習",
                            quiz_id=quiz_id_r,
                            quiz_type="Review",
                        )
                        set_quiz_pending(redis, user_id, review_quiz.get("question", ""))
                    quiz_text = (
                        "——\n📝 複習測驗（依你最近的問答出題）\n"
                        + review_quiz["question"]
                        + "\n"
                        + "\n".join(review_quiz["options"])
                        + "\n\n(點選 A/B/C 作答，或輸入新問題繼續學習～)"
                    )
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=quiz_text, quick_reply=quick_reply_quiz_choices()),
                    )
                    if redis:
                        redis.set(f"quiz_sent_at:{user_id}", str(time.time()), ex=3600)
                    return
                line_bot_api.reply_message(event.reply_token, text_with_quick_reply("尚無足夠的問答記錄可出複習題，先多問幾題中醫問題吧～"))
            except Exception as e:
                traceback.print_exc()
                line_bot_api.reply_message(event.reply_token, text_with_quick_reply("複習測驗暫時無法使用，請稍後再試。"))
            return

        # 主動複習：使用者選擇「要複習筆記」
        if user_text == "要複習筆記":
            cat = get_pending_review_category(redis, user_id)
            clear_pending_review_category(redis, user_id)
            if cat:
                note = generate_review_note(client, cat)
                clear_weak_category(redis, user_id, cat)
                review_msg = text_with_quick_reply(f"📝 【{cat}】複習筆記\n\n{note}")
            else:
                review_msg = text_with_quick_reply("好的，有需要再跟我說～")
            if FORCE_PUSH_MODE:
                line_bot_api.push_message(user_id, review_msg)
            else:
                line_bot_api.reply_message(event.reply_token, review_msg)
            return
        if user_text == "不要複習筆記":
            clear_pending_review_category(redis, user_id)
            review_msg = text_with_quick_reply("好的，有需要再跟我說～")
            if FORCE_PUSH_MODE:
                line_bot_api.push_message(user_id, review_msg)
            else:
                line_bot_api.reply_message(event.reply_token, review_msg)
            return

        # 主動複習：偵測到弱項且超過冷卻期（在中醫問答回覆後才詢問）
        # 這裡不直接回覆，避免擋掉原本問題回答流程。
        pass

        # 小測驗（舊題庫）：點擊「否」→ 友善回覆，保持一般問答模式
        if (not suppress_yes_no_command) and user_text == "否":
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("沒問題！如果有其他想了解的，歡迎隨時提問。"))
            return
        # 小測驗（舊題庫）：點擊「是」→ 時間解鎖題庫
        if (not suppress_yes_no_command) and user_text == "是":
            time_locked_quiz_handler(user_id, reply_token=event.reply_token)
            return

        if user_text == "本週重點":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        if user_text in ("練習下一句", "Next Sentence"):
            mode = _safe_get_mode(user_id)
            if mode == "speaking":
                next_sentence = _generate_next_practice_sentence()
                if FORCE_LANG == "en" or user_text == "Next Sentence":
                    if next_sentence:
                        msg = f"💡 Try this sentence:\n\"{next_sentence}\"\n\nSend a voice message to practice, or record your own sentence!"
                    else:
                        msg = "Send a voice message to start practicing — I'll analyze your pronunciation and grammar."
                else:
                    if next_sentence:
                        msg = f"💡 建議練習這句：\n「{next_sentence}」\n\n傳語音跟著唸，或錄你自己想練習的句子都可以！"
                    else:
                        msg = "請傳送語音訊息開始練習～我會幫你分析發音與文法。"
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_speak_practice(msg),
                )
                return
        if user_text in ("結束練習", "End Practice"):
            _set_cached_mode(user_id, "tcm")
            if redis:
                for _attempt in range(3):
                    try:
                        with _redis_mode_lock:
                            redis.set(_redis_user_mode_key(user_id), "tcm", ex=86400)
                        break
                    except Exception as _e:
                        print(f"[MODE] End Practice redis set failed attempt={_attempt} err={_e}")
                        if _attempt < 2:
                            time.sleep(0.2)
            else:
                print(f"[MODE] End Practice: redis unavailable, mode only in local cache")
            if FORCE_LANG == "en" or user_text == "End Practice":
                end_msg = "Speaking practice ended. Switched back to TCM Q&A mode."
            else:
                end_msg = "已結束口說練習，已切換回中醫問答模式。"
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply(end_msg),
            )
            return

        # --- 社交短句：道謝、打招呼等，直接友善回覆，不走 GPT ---
        _social_en = {"thank you", "thanks", "thank u", "thx", "ty", "great", "ok", "okay",
                      "got it", "i see", "cool", "nice", "awesome", "perfect", "good",
                      "alright", "sure", "noted", "understood", "bye", "goodbye", "hello",
                      "hi", "hey", "you're welcome", "welcome", "no problem", "np"}
        _social_zh = {"謝謝", "感謝", "感謝你", "感謝您", "謝", "好的", "了解", "知道了",
                      "收到", "明白", "沒問題", "好", "讚", "再見", "拜拜", "你好", "哈囉"}
        _txt_lower = user_text.strip().lower()
        if _txt_lower in _social_en or user_text.strip() in _social_zh:
            if FORCE_LANG == "en":
                ack_msg = "You're welcome! 😊 Feel free to ask any TCM questions anytime."
            else:
                ack_msg = "不客氣！😊 隨時歡迎繼續發問中醫相關問題。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(ack_msg))
            return

        # --- 課程聯絡資訊：請至課程群組發問 ---
        _contact_en = {"contact", "email", "ta ", "teaching assistant", "instructor",
                       "professor", "teacher", "how to reach", "office hour", "line group",
                       "course group", "chat group", "get in touch"}
        _contact_zh = {"聯絡", "聯繫", "助教", "老師", "教授", "信箱", "email",
                       "課程群組", "群組", "line群", "怎麼問", "怎麼聯絡"}
        if (any(k in _txt_lower for k in _contact_en) or
                any(k in user_text for k in _contact_zh)):
            if FORCE_LANG == "en":
                contact_msg = "For questions about the course or contact information, please ask in the course LINE group. 📢"
            else:
                contact_msg = "關於課程或聯絡事項，請至課程 LINE 群組發問喔！📢"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(contact_msg))
            return

        # 最終路由：直接讀 Redis，跳過本地快取，避免多 worker 快取不同步導致誤判
        mode = None
        if redis:
            try:
                with _redis_mode_lock:
                    _rv = redis.get(_redis_user_mode_key(user_id))
                if _rv:
                    mode = (_rv.decode("utf-8") if isinstance(_rv, bytes) else str(_rv)).strip()
            except Exception as _e:
                print(f"[MODE] final routing redis read failed: {_e}")
        if not mode:
            mode = _safe_get_mode(user_id)
        print(f"[MODE] handle_message -> AI (current_mode={mode!r})")

        # 統一 TCM 問答：tcm / quiz 一律走同一邏輯（避免 push：直接用 reply_token 回覆最終結果）
        if mode in ("tcm", "quiz"):
            _start_loading_indicator(user_id)
            if not _tcm_openai_reply(user_id, user_text, reply_token=event.reply_token):
                try:
                    line_bot_api.reply_message(event.reply_token, text_with_quick_reply("An error occurred, please try again." if FORCE_LANG == "en" else "處理時發生錯誤，請稍後再試。"))
                except Exception:
                    pass
            # 七天後（冷卻）弱項檢查：先回答原問題，之後再詢問是否要複習筆記
            _maybe_send_review_prompt(user_id, reply_token=None)
            return

        # 口說 / 寫作：依模式顯示載入訊息並走 Assistant API
        if FORCE_LANG == "en":
            mode_name = {"speaking": "🗣️ Speaking Practice", "writing": "✍️ Writing Revision"}.get(mode, mode)
            analyzing_msg = f"Analyzing in [{mode_name}] mode, please wait... ✨"
        else:
            mode_name = {"speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, mode)
            analyzing_msg = f"正在以【{mode_name}】模式分析中..."
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=analyzing_msg))
        _run_ai_work(user_id, user_text)
    except Exception as e:
        traceback.print_exc()
        err_msg = str(e).strip()[:100]
        try:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(f"處理訊息時發生錯誤，請再試一次。（{err_msg}）"))
        except Exception:
            try:
                line_bot_api.push_message(user_id, text_with_quick_reply(f"處理訊息時發生錯誤，請再試一次。（{err_msg}）"))
            except Exception:
                pass

@line_webhook_handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    """口說教練：立即回覆釋放 token，背景/同步處理語音。"""
    user_id = event.source.user_id
    message_id = event.message.id

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="Converting voice, please wait... 🎙️" if FORCE_LANG == "en" else "正在轉換語音，請稍候... 🎙️"),
    )

    print(f"[VOICE] running sync (worker) user_id={user_id}")
    _process_voice_sync(user_id, message_id)


if __name__ == "__main__":
    # 本地快速測試：python -m api.index 或 python api/index.py（從專案根目錄）
    # 再開一個終端執行 ngrok http 5000，並將 LINE Webhook 改為 https://YOUR-NGROK-URL/callback
    app.run(host="0.0.0.0", port=5000, debug=True)
