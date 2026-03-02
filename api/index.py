# -*- coding: utf-8 -*-
import io
import glob
import os
import re
import threading
import time
import base64
import json
import secrets
import tempfile
import traceback
from flask import Flask, request, abort, Response
import requests
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage,
    QuickReply, QuickReplyButton, MessageAction, FlexSendMessage,
)
from linebot.models.send_messages import AudioSendMessage
from upstash_redis import Redis
from openai import OpenAI
# cloudinary 改為 lazy import（_upload_tts_to_cloudinary 內），避免 TCM 純文字路徑載入

try:
    from api.syllabus import (
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        is_course_inquiry_intent,
        build_course_inquiry_flex,
        OFF_TOPIC_REPLY,
    )
    from api.learning import (
        log_question,
        set_last_question,
        get_last_question,
        set_last_assistant_message,
        get_last_assistant_message,
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
        reveal_quiz_answer,
        judge_quiz_answer,
        generate_review_note,
    )
except ImportError:
    from syllabus import (
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        is_course_inquiry_intent,
        build_course_inquiry_flex,
        OFF_TOPIC_REPLY,
    )
    from learning import (
        log_question,
        set_last_question,
        get_last_question,
        set_last_assistant_message,
        get_last_assistant_message,
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
        reveal_quiz_answer,
        judge_quiz_answer,
        generate_review_note,
    )

# 1. 初始化（保留原有 upstash_redis 連線設定）
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
line_webhook_handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

kv_url = os.getenv("KV_REST_API_URL")
kv_token = os.getenv("KV_REST_API_TOKEN")
redis = Redis(url=kv_url, token=kv_token) if kv_url and kv_token else None

# Cloudinary：僅檢查 env（lazy import 於 _upload_tts_to_cloudinary 內）
_cloudinary_configured = bool(
    os.getenv("CLOUDINARY_CLOUD_NAME")
    and os.getenv("CLOUDINARY_API_KEY")
    and os.getenv("CLOUDINARY_API_SECRET")
)

# 安全聲明：涉及中醫診斷之回覆必須附加
SAFETY_DISCLAIMER = "\n\n⚠️ 僅供教學用途，不具醫療建議。"

VOICE_COACH_TTS_VOICE = "shimmer"
TIMEOUT_SECONDS = 28  # Assistant + RAG 常需 15–30 秒；保留 buffer 避開 Vercel 預設 30s
TIMEOUT_MESSAGE = "正在努力翻閱典籍/資料中，請稍候再問我一次。"

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
                        "你是英文發音與文法助教。分析學生語音辨識文字，執行：\n"
                        "1. 檢查語法錯誤、單字拼寫錯誤、用詞不當\n"
                        "2. 評估語義是否完整\n"
                        "回傳 JSON：\n"
                        '{"status": "Correct" 或 "NeedsImprovement", "feedback": "簡短回饋（需改進處或鼓勵）", "corrected": "修正後的正確文本（若 status 為 Correct 則為空字串）"}\n'
                        "Status: Correct = 完全正確且自然；NeedsImprovement = 有任何細微錯誤。"
                    ),
                },
                {"role": "user", "content": f"學生說出的內容：{transcript[:500]}"},
            ],
            max_tokens=250,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        for block in (raw_text.split("```"), [raw_text]):
            for raw in block:
                raw = raw.strip()
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw.split("```")[0].strip().split("\n")[0])
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

_cloudinary_config_done = False

def _upload_tts_to_cloudinary(audio_bytes, sentence=""):
    """上傳 TTS 語音至 Cloudinary。Lazy import。"""
    global _cloudinary_config_done
    if not _cloudinary_configured or not audio_bytes:
        return (None, 0)
    try:
        import cloudinary
        import cloudinary.uploader
        if not _cloudinary_config_done:
            cloudinary.config(
                cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
                api_key=os.getenv("CLOUDINARY_API_KEY"),
                api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            )
            _cloudinary_config_done = True
        result = cloudinary.uploader.upload(
            io.BytesIO(audio_bytes),
            resource_type="video",  # 音訊用 video 型別，支援轉碼與 CDN 優化
            folder="tts",
            use_filename=True,
            unique_filename=True,
        )
        url = result.get("secure_url")
        if url:
            duration_ms = max(1000, int(len(sentence.split()) / 2.2 * 1000))
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
        )
        audio_bytes = resp.content
        duration_ms = max(1000, int(len(sentence.split()) / 2.2 * 1000))

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
    flex_msg = FlexSendMessage(
        alt_text="📋 課務查詢與本週重點",
        contents=bubble,
        quick_reply=quick_reply_items(),
    )
    if reply_token:
        line_bot_api.reply_message(reply_token, flex_msg)
    else:
        line_bot_api.push_message(user_id, flex_msg)

# --- QuickReply ---
def quick_reply_items():
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="口說練習", text="口說練習")),
            QuickReplyButton(action=MessageAction(label="寫作修改", text="寫作修改")),
            QuickReplyButton(action=MessageAction(label="課務查詢", text="課務查詢")),
            QuickReplyButton(action=MessageAction(label="本週重點", text="本週重點")),
        ]
    )

def text_with_quick_reply(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_items())

def quick_reply_speak_practice():
    """口說練習：要再練習下一句嗎？[練習下一句] [結束練習]。"""
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

# --- 寫作修訂模式：獨立處理，不使用 Assistant API / RAG ---
REVISION_MODE = "writing"
REVISION_MODE_PROMPT = "你已在【✍️ 寫作修訂】模式～請貼上要修改的段落。"
REDIS_KEY_USER_MODE = "user_mode"

# 寫作模式 prompt：回覆自然內容，不要輸出【】標題給使用者
_REVISION_PROMPT = (
    "你是專業溫暖的語言老師。回覆時請自然融入以下內容，不要輸出【】標題："
    "（1）鼓勵／正面肯定；"
    "（2）若有錯誤：需修改的原因＋修正後的版本（用 **粗體** 標示修改處）；若無誤則稱讚原文道地；"
    "（3）鼓勵繼續發問、貼上其他句子練習。"
    "語氣溫暖，段落分明易讀。"
)

def _revision_handler(user_id, text):
    """寫作修訂：gpt-4o-mini + Chat Completion，結果以 push_message 送出。"""
    if not user_id or not str(user_id).strip():
        print(f"[REVISION] ERROR: user_id invalid or empty user_id={repr(user_id)}")
        return
    if not (text or "").strip():
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply_writing("請貼上要修改的段落。"))
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
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _REVISION_PROMPT},
                {"role": "user", "content": f"分析以下句子或段落：\n{text[:1000]}"},
            ],
            max_tokens=600,
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not reply:
            reply = "已收到你的練習！歡迎繼續貼上其他句子～"
        print(f"[REVISION] done user_id={user_id} reply_len={len(reply)}")
        line_bot_api.push_message(user_id, text_with_quick_reply_writing(reply))
    except Exception as e:
        print(f"[REVISION] CRITICAL err={e}")
        traceback.print_exc()
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply_writing("處理時發生錯誤，請再試一次。"))
        except Exception as push_err:
            print(f"[REVISION] push_message (error fallback) failed err={push_err}")

def quick_reply_writing():
    """寫作修訂模式：僅繼續練習按鈕。"""
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="繼續練習", text="繼續練習")),
        ]
    )

def text_with_quick_reply_writing(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_writing())

def _redis_user_mode_key(user_id):
    return f"{REDIS_KEY_USER_MODE}:{user_id}"

# --- 中醫問答：本地 JSON 關鍵字匹配 + Gemini（輕量、扁平）---
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_TCM_JSON_CACHE = None

def _load_tcm_json():
    """載入 data/tcm_*.json，快取。"""
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

def _tcm_keyword_match_and_gemini(user_id, text):
    """
    關鍵字匹配 tcm_*.json，若匹配則將內容餵給 Gemini 回覆。
    回傳 True 若已回覆，False 則 fallback 至 process_ai_request。
    """
    if not (text or "").strip():
        return False
    txt = text.strip()
    # 從 JSON 萃取關鍵字並匹配
    all_data = _load_tcm_json()
    ctx_parts = []
    for data in all_data:
        for kp in data.get("knowledge_points") or []:
            cat = (kp.get("category") or "").split("(")[0].strip()
            terms = [cat] if len(cat) >= 2 else []
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
            if any(t in txt for t in terms if len(t) >= 2):
                if kp.get("core_logic"):
                    ctx_parts.append(kp["core_logic"])
                if kp.get("mechanism"):
                    ctx_parts.append(kp["mechanism"])
                cr = kp.get("causal_relationships")
                if cr:
                    lines = [f"{r.get('emotion','')}→{r.get('impact','')}：{r.get('symptoms','')}" for r in cr if isinstance(r, dict)]
                    ctx_parts.append("；".join(lines))
                pf = kp.get("pathological_features")
                if pf:
                    lines = [f"{r.get('evil','')}：{r.get('features','')}" for r in pf if isinstance(r, dict)]
                    ctx_parts.append("；".join(lines))
    if not ctx_parts:
        return False
    ctx = "\n".join(ctx_parts)[:1500]
    try:
        from google import genai
        from google.genai import types
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            return False
        gc = genai.Client(api_key=key)
        resp = gc.models.generate_content(
            model="gemini-1.5-flash",
            contents=f"[背景資料]\n{ctx}\n\n[問題]\n{txt}\n\n請根據背景資料在150字內精準回答，跳過開場白。",
            config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=256),
        )
        if resp and getattr(resp, "text", None):
            ai_reply = resp.text.strip()[:500] + SAFETY_DISCLAIMER
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            line_bot_api.push_message(user_id, text_with_quick_reply_quiz(ai_reply + "\n\n是否要進行一題小測驗？"))
            return True
    except Exception:
        pass
    return False

def _safe_get_mode(user_id):
    """安全取得使用者模式，Redis 失敗時回傳 tcm。"""
    try:
        if not redis:
            return "tcm"
        mode_val = redis.get(_redis_user_mode_key(user_id))
        if mode_val is None:
            return "tcm"
        if hasattr(mode_val, "decode"):
            return mode_val.decode("utf-8").strip() or "tcm"
        return str(mode_val).strip() or "tcm"
    except Exception:
        return "tcm"

def handle_writing_correction(user_id, user_text, reply_token):
    """
    寫作修訂獨立處理：切換模式、繼續練習、或執行修訂。
    回傳 True 若已處理，False 則交由其他 handler。
    """
    if user_text in ("寫作修改", "寫作修訂"):
        try:
            if redis:
                redis.set(_redis_user_mode_key(user_id), REVISION_MODE)
        except Exception:
            pass
        msg = REVISION_MODE_PROMPT
        if not redis:
            msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 KV_REST_API 環境變數。"
        line_bot_api.reply_message(reply_token, text_with_quick_reply_writing(msg))
        return True

    current_mode = _safe_get_mode(user_id)
    if current_mode != REVISION_MODE:
        return False

    if user_text == "繼續練習":
        line_bot_api.reply_message(
            reply_token,
            text_with_quick_reply_writing("請貼上要修改的段落。"),
        )
        return True

    line_bot_api.reply_message(
        reply_token,
        TextSendMessage(text="正在分析你的寫作，請稍候... ✨"),
    )
    _revision_handler(user_id, user_text)
    return True

def handle_tcm_qa(event, user_id, user_text):
    """
    中醫問答獨立處理：JSON RAG 關鍵字匹配 + Gemini，無匹配則 fallback Assistant。
    僅於 mode == tcm 時呼叫。
    """
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正在查詢中醫典籍，請稍候..."))
    if _tcm_keyword_match_and_gemini(user_id, user_text):
        return
    process_ai_request(event, user_id, user_text, is_voice=False)

# --- AI 核心函數（模式路由器）---
def process_ai_request(event, user_id, text, is_voice=False):
    """State-Based Router：寫作模式走 _revision_handler，其餘走 Assistant API。"""
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
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)

        start_time = time.time()
        while run.status in ['queued', 'in_progress']:
            if time.time() - start_time > TIMEOUT_SECONDS:
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            if mode == "tcm":
                ai_reply = ai_reply.rstrip() + SAFETY_DISCLAIMER
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            if mode == "tcm":
                line_bot_api.push_message(user_id, text_with_quick_reply_quiz(ai_reply + "\n\n是否要進行一題小測驗？"))
            else:
                line_bot_api.push_message(user_id, text_with_quick_reply(ai_reply))
        else:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))

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
    """本機/缺少 VERCEL_URL 時同步執行語音處理（避免非同步觸發失敗時無回應）。"""
    try:
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
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🎤 辨識內容：「{transcript_text}」"))

        mode = _safe_get_mode(user_id)

        if mode == REVISION_MODE:
            _revision_handler(user_id, transcript_text)
            print(f"[VOICE] done revision path")
            return
        if mode == "speaking":
            status, feedback, corrected_text = _evaluate_speech(transcript_text)
            if status == "Correct":
                line_bot_api.push_message(
                    user_id,
                    text_with_quick_reply_speak_practice("發音非常標準！太棒了！\n\n要再練習下一句嗎？"),
                )
            else:
                text_for_tts = corrected_text.strip() if corrected_text else transcript_text
                # 先推文字，降低體感等待；TTS + Cloudinary 在後
                line_bot_api.push_message(
                    user_id,
                    text_with_quick_reply(f"📊 口說練習回饋\n\n{feedback}\n\n🔊 請跟著唸：「{text_for_tts}」"),
                )
                audio_url, duration_ms = _generate_tts_and_store(text_for_tts, voice=VOICE_COACH_TTS_VOICE)
                if audio_url and duration_ms:
                    line_bot_api.push_message(
                        user_id,
                        AudioSendMessage(original_content_url=audio_url, duration=duration_ms),
                    )
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply_speak_practice("示範語音已送上，要再練習下一句嗎？"),
                    )
                else:
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply_speak_practice(
                            f"修正文本：{text_for_tts}\n\n要再練習下一句嗎？"
                        ),
                    )
        else:
            if is_course_inquiry_intent(transcript_text):
                line_bot_api.push_message(user_id, TextSendMessage(text="正在查詢課務資料..."))
                send_course_inquiry_flex(user_id)
            elif is_off_topic(transcript_text):
                line_bot_api.push_message(user_id, text_with_quick_reply(OFF_TOPIC_REPLY))
            else:
                process_ai_request(None, user_id, transcript_text, is_voice=True)
    except Exception:
        traceback.print_exc()
        line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))


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
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        line_webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        traceback.print_exc()
        # 仍回傳 200，避免 LINE 重試造成重複觸發
    return 'OK', 200

# --- 事件處理 ---
@line_webhook_handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    user_id = event.source.user_id
    try:
        if data == "action=course" or data == "action=weekly":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return
        # mode=tcm / mode=speaking / mode=writing
        mode = data.split("=")[1].strip() if "=" in data else "tcm"
        try:
            if redis:
                redis.set(_redis_user_mode_key(user_id), mode)
        except Exception:
            pass
        mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
        if mode == REVISION_MODE:
            msg = REVISION_MODE_PROMPT
            if not redis:
                msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 KV_REST_API 環境變數。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply_writing(msg))
        else:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(f"已切換至【{mode_map.get(mode, mode)}】模式"))
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
    try:
        # 課務查詢／本週重點：統一以 Flex Message 回傳
        if is_course_inquiry_intent(user_text):
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        # 寫作修訂：獨立 handler（切換模式／繼續練習／修訂）
        if handle_writing_correction(user_id, user_text, event.reply_token):
            return

        # 小測驗後（舊狀態相容）：學生的回答視為新問題，交由 AI 處理
        if get_user_state(redis, user_id) == STATE_QUIZ_WAITING:
            set_user_state(redis, user_id, STATE_NORMAL)
            clear_quiz_data(redis, user_id)
            clear_quiz_pending(redis, user_id)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正在分析中..."))
            process_ai_request(event, user_id, user_text, is_voice=False)
            return

        # 主動複習：使用者選擇「要複習筆記」
        if user_text == "要複習筆記":
            cat = get_pending_review_category(redis, user_id)
            clear_pending_review_category(redis, user_id)
            if cat:
                note = generate_review_note(client, cat)
                clear_weak_category(redis, user_id, cat)
                line_bot_api.reply_message(event.reply_token, text_with_quick_reply(f"📝 【{cat}】複習筆記\n\n{note}"))
            else:
                line_bot_api.reply_message(event.reply_token, text_with_quick_reply("好的，有需要再跟我說～"))
            return
        if user_text == "不要複習筆記":
            clear_pending_review_category(redis, user_id)
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("好的，有需要再跟我說～"))
            return

        # 主動複習：偵測到弱項且超過冷卻期 → 詢問是否整理複習筆記
        weak = get_weak_categories(redis, user_id, min_count=2)
        if weak and (time.time() - get_last_review_ask(redis, user_id)) > 7 * 24 * 3600:
            category = next(iter(weak.keys()), None)
            if category:
                set_last_review_ask(redis, user_id)
                set_pending_review_category(redis, user_id, category)
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_review_ask(f"發現你對「{category}」這部分較不熟，需要幫你整理複習筆記嗎？"),
                )
                return

        # 小測驗：點擊「否」→ 友善回覆，保持一般問答模式
        if user_text == "否":
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("沒問題！如果有其他想了解的，歡迎隨時提問。"))
            return
        # 小測驗：點擊「是」→ 針對剛才討論的主題出題（學生的回答將視為新問題）
        if user_text == "是":
            discussed_topic = get_last_question(redis, user_id)
            last_ctx = get_last_assistant_message(redis, user_id)
            question, _, _ = generate_dynamic_quiz(client, discussed_topic=discussed_topic, last_context=last_ctx)
            flex_msg = build_quiz_flex_message(question)
            line_bot_api.reply_message(event.reply_token, flex_msg)
            return

        if user_text == "本週重點":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        if user_text == "口說練習":
            try:
                if redis:
                    redis.set(_redis_user_mode_key(user_id), "speaking")
            except Exception:
                pass
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"))
            return
        if user_text == "練習下一句":
            mode = _safe_get_mode(user_id)
            if mode == "speaking":
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_speak_practice("請傳送語音訊息開始練習～我會幫你分析發音與文法。\n\n要再練習下一句嗎？"),
                )
                return
        if user_text == "結束練習":
            try:
                if redis:
                    redis.set(_redis_user_mode_key(user_id), "tcm")
            except Exception:
                pass
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply("已結束口說練習，已切換回中醫問答模式。"),
            )
            return

        # 精準過濾：僅完全與中醫/醫療學術無關（閒聊、娛樂、私人）→ 僅供學業使用
        if is_off_topic(user_text):
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(OFF_TOPIC_REPLY))
            return

        # 主路由：明確 if/elif 解耦
        mode = _safe_get_mode(user_id)
        if mode == "tcm":
            handle_tcm_qa(event, user_id, user_text)
            return
        elif mode == "speaking":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正在以【🗣️ 口說練習】模式分析中..."),
            )
            process_ai_request(event, user_id, user_text, is_voice=False)
            return
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正在分析中..."),
            )
            process_ai_request(event, user_id, user_text, is_voice=False)
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
    """口說教練：Webhook 須在 2 秒內回傳 200；語音轉錄／GPT／TTS／Cloudinary 全在 Background 執行。"""
    user_id = event.source.user_id
    message_id = event.message.id

    # 1. 立即回覆使用者（唯一必要的阻塞呼叫，通常 <1.5s）
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🎙️ 正在轉換語音..."))

    # 2. 觸發 Background Task（語音轉錄、GPT、TTS、Cloudinary 在 /api/process-voice-async 獨立執行）
    vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
    base_url = f"https://{vercel_url}" if vercel_url and not vercel_url.startswith("http") else (vercel_url or "")
    cron_secret = os.getenv("CRON_SECRET", "")

    if base_url and cron_secret:
        # Vercel：同步 fire POST，timeout=0.8s，請求送出即觸發新 invocation，不阻塞
        try:
            requests.post(
                f"{base_url}/api/process-voice-async",
                json={"user_id": user_id, "message_id": message_id},
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=0.8,
            )
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
            pass  # 預期：async 已觸發，本函數不等待其完成
        except Exception:
            threading.Thread(
                target=_run_voice_background,
                args=(user_id, message_id, base_url, cron_secret),
                daemon=True,
            ).start()  # 連線失敗時由 thread 執行 fallback
    else:
        # 本機：thread 中執行，避免阻塞
        threading.Thread(
            target=_run_voice_background,
            args=(user_id, message_id, base_url, cron_secret),
            daemon=True,
        ).start()
