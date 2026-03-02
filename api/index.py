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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import httpx
from httpx_retries import RetryTransport, Retry
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
# 使用 httpx + RetryTransport 緩解 Vercel 上 Errno 16 "Device or resource busy" 等瞬斷
_retry = Retry(total=3, backoff_factor=0.5)
_http_client = httpx.Client(
    transport=RetryTransport(retry=_retry),
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), http_client=_http_client)
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

# Gemini：google-genai SDK 全域單例重用（lazy init 避免 webhook cold start 載入）
GEMINI_FLASH = "gemini-1.5-flash"
GEMINI_PRO = "gemini-1.5-pro"
_gemini_client = None

def _get_gemini_client():
    """全域實例重用：首次 TCM 問答時初始化，之後秒速調用。不依賴 cloudinary 等大型庫。"""
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    try:
        from google import genai as _genai_module
        _key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if _key:
            _gemini_client = _genai_module.Client(api_key=_key)
    except ImportError:
        pass
    except Exception:
        pass
    return _gemini_client

# TCM 本地知識庫：Local First，遍歷 data/tcm_*.json
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_TCM_JSON_GLOB = os.path.join(_DATA_DIR, "tcm_*.json")
_TCM_LOCAL_CACHE = None
_TCM_CACHE_LOCK = threading.Lock()
_EMOTION_KEYWORDS = ("怒", "喜", "思", "悲", "憂", "恐", "驚", "生氣", "憤怒", "快樂", "悲傷", "擔心", "憂鬱", "害怕", "恐懼", "驚嚇", "驚恐", "情緒")
_CLIMATE_KEYWORDS = ("風", "寒", "暑", "濕", "燥", "火", "濕氣", "燥熱", "風邪", "寒邪", "暑邪", "濕邪", "燥邪", "火邪", "六邪")


def _load_all_tcm_json():
    """
    非同步平行載入 data/tcm_*.json（使用 ThreadPoolExecutor 避免阻塞）。
    快取於模組層級，首次呼叫後 <1s 完成。
    """
    global _TCM_LOCAL_CACHE
    with _TCM_CACHE_LOCK:
        if _TCM_LOCAL_CACHE is not None:
            return _TCM_LOCAL_CACHE

    def _read_one(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return path, json.load(f)
        except Exception:
            return path, {}

    paths = glob.glob(_TCM_JSON_GLOB)
    result = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(paths)))) as ex:
        futures = [ex.submit(_read_one, p) for p in paths]
        for fut in as_completed(futures):
            try:
                p, data = fut.result()
                if data and isinstance(data, dict):
                    result.append(data)
            except Exception:
                pass

    with _TCM_CACHE_LOCK:
        _TCM_LOCAL_CACHE = result
    return _TCM_LOCAL_CACHE


def _extract_kp_keywords(kp):
    """從 knowledge_point 萃取可匹配的關鍵字。"""
    terms = set()
    cat = (kp.get("category") or "").strip()
    if cat:
        terms.add(cat.split("(")[0].strip())
        for part in re.split(r"[(\s,，、；;]+", cat):
            if len(part) >= 2:
                terms.add(part)
    for key in ("keywords",):
        arr = kp.get(key)
        if isinstance(arr, list):
            for x in arr:
                if isinstance(x, str) and len(x) >= 2:
                    terms.add(x.strip())
    for cr in (kp.get("causal_relationships") or []):
        if isinstance(cr, dict):
            for k in ("emotion", "target_organ"):
                v = cr.get(k)
                if isinstance(v, str):
                    terms.update(re.split(r"[/\s,]+", v))
    for pf in (kp.get("pathological_features") or []):
        if isinstance(pf, dict):
            v = pf.get("evil")
            if isinstance(v, str):
                terms.update(re.split(r"[\s(]+", v))
    for row in (kp.get("five_elements_table") or []):
        if isinstance(row, dict):
            for k in ("element", "organ", "emotion"):
                v = row.get(k)
                if isinstance(v, str):
                    terms.add(v.split("(")[0].strip())
    core = (kp.get("core_logic") or "")[:80]
    for m in re.findall(r"[\u4e00-\u9fff]{2,4}", core):
        terms.add(m)
    return terms


def _build_kp_context(kp):
    """從 knowledge_point 建構可注入的 context（core_logic 或 causal_relationships）。"""
    parts = []
    if kp.get("core_logic"):
        parts.append(kp["core_logic"])
    if kp.get("mechanism"):
        parts.append(kp["mechanism"])
    cr = kp.get("causal_relationships")
    if cr:
        lines = [f"{r.get('emotion','')}→{r.get('impact','')}：{r.get('symptoms','')}（{r.get('target_organ','')}）" for r in cr if isinstance(r, dict)]
        parts.append("[七情致病] " + "；".join(lines))
    pf = kp.get("pathological_features")
    if pf:
        lines = [f"{r.get('evil','')}：{r.get('features','')}，症狀如{r.get('symptoms','')}" for r in pf if isinstance(r, dict)]
        parts.append("[六邪致病] " + "；".join(lines))
    return "\n".join(parts) if parts else ""


def _local_first_tcm_context(user_text):
    """
    Local First：遍歷 tcm_*.json，關鍵字比對 category/keywords。
    回傳 (context_str, matched: bool)。若 matched 則 context 可注入。
    """
    if not (user_text or "").strip():
        return "", False
    text = user_text.strip()
    all_data = _load_all_tcm_json()
    matched_kps = []
    for data in all_data:
        kps = data.get("knowledge_points") or []
        for kp in kps:
            terms = _extract_kp_keywords(kp)
            if any(t in text for t in terms if len(t) >= 2):
                matched_kps.append(kp)
    if not matched_kps:
        return "", False
    contexts = [_build_kp_context(kp) for kp in matched_kps if _build_kp_context(kp)]
    context = "\n\n".join(contexts)[:2000] if contexts else ""
    return context, bool(context)


def _get_tcm_injected_knowledge(user_text):
    """
    若使用者提到情緒或氣候，回傳 causal_relationships 或 pathological_features 的結構化文字。
    Local First 有匹配時優先使用；此函數保留作為 emotion/climate 專用補充。
    """
    if not (user_text or "").strip():
        return ""
    text = user_text.strip()
    all_data = _load_all_tcm_json()
    kps = []
    for data in all_data:
        kps.extend(data.get("knowledge_points") or [])
    out = []
    need_emotion = any(kw in text for kw in _EMOTION_KEYWORDS)
    need_climate = any(kw in text for kw in _CLIMATE_KEYWORDS)
    for kp in kps:
        if need_emotion:
            cr = kp.get("causal_relationships")
            if cr:
                lines = [f"{r.get('emotion','')}→{r.get('impact','')}：{r.get('symptoms','')}（{r.get('target_organ','')}）" for r in cr if isinstance(r, dict)]
                out.append("[七情致病] " + "；".join(lines))
                need_emotion = False
        if need_climate:
            pf = kp.get("pathological_features")
            if pf:
                lines = [f"{r.get('evil','')}：{r.get('features','')}，症狀如{r.get('symptoms','')}" for r in pf if isinstance(r, dict)]
                out.append("[六邪致病] " + "；".join(lines))
                need_climate = False
        if not need_emotion and not need_climate:
            break
    return "\n".join(out) if out else ""

# Redis：全域單例，Upstash REST API 無需連線池，模組載入時建立一次
kv_url = os.getenv("KV_REST_API_URL")
kv_token = os.getenv("KV_REST_API_TOKEN")
redis = None
if kv_url and kv_token:
    try:
        redis = Redis(
            url=kv_url,
            token=kv_token,
            rest_retries=5,
            rest_retry_interval=2,
        )
    except TypeError:
        try:
            redis = Redis(url=kv_url, token=kv_token)
        except Exception as e:
            print(f"[REDIS] init failed err={e}")
            redis = None
    except Exception as e:
        print(f"[REDIS] init failed err={e}")
        redis = None

# 模式快取：Redis 瞬斷時使用，key=user_id -> (mode, timestamp)
_mode_cache = {}
_MODE_CACHE_TTL = 180
_MODE_CACHE_MAX = 1000

# Cloudinary：僅檢查 env（lazy import 於 _upload_tts_to_cloudinary 內，TCM 純文字路徑不載入）
_cloudinary_configured = bool(
    os.getenv("CLOUDINARY_CLOUD_NAME")
    and os.getenv("CLOUDINARY_API_KEY")
    and os.getenv("CLOUDINARY_API_SECRET")
)

# 安全聲明：涉及中醫診斷之回覆必須附加
SAFETY_DISCLAIMER = "\n\n⚠️ 僅供教學用途，不具醫療建議。"

VOICE_COACH_TTS_VOICE = "shimmer"
TTS_SPEED = 0.8  # shadowing 語音 0.8 倍速，較慢易於跟讀
VOICE_ERROR_MSG = "抱歉，語音生成出了一點問題，請再試一次。"
TIMEOUT_SECONDS = 28  # Assistant + RAG 常需 15–30 秒；保留 buffer 避開 Vercel 預設 30s
TIMEOUT_MESSAGE = "正在努力翻閱典籍/資料中，請稍候再問我一次。"

# --- Gemini 模型路由 Helper（google-genai SDK）---
def _gemini_generate(model, user_content, system_instruction=None, max_tokens=1024, temperature=None, top_p=None):
    """Gemini 生成，回傳 (content: str, ok: bool)。使用 google-genai Client。"""
    gc = _get_gemini_client()
    if not gc:
        return "", False
    try:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            system_instruction=(system_instruction or ""),
        )
        if temperature is not None:
            cfg.temperature = temperature
        if top_p is not None:
            cfg.top_p = top_p
        resp = gc.models.generate_content(
            model=model,
            contents=user_content[:8000],
            config=cfg,
        )
        if resp and hasattr(resp, "text") and resp.text:
            return (resp.text.strip(), True)
    except Exception:
        pass
    return "", False


def _gemini_tcm_stream_send(user_id, text, reply_token=None):
    """
    中醫問答：Local First 優先本地檢索 tcm_*.json，關鍵字比對後注入 context。
    若匹配：直接用背景資料 + 150 字快速回答。若無匹配：才用 AI 內建或外部搜尋。
    temperature=0.2 加快推理。使用 google-genai SDK。
    純文字回應：僅 line_bot_api.push_message，不觸發檔案儲存或上傳。
    """
    gc = _get_gemini_client()
    if not gc:
        return False
    try:
        # Local First：先遍歷 tcm_*.json 做關鍵字比對
        local_ctx, matched = _local_first_tcm_context(text)

        if matched and local_ctx:
            # 有匹配：注入 context，150 字內快速精準回答，跳過開場白
            user_content = f"[背景資料]\n{local_ctx}\n\n[問題]\n{text.strip()}\n\n[指令] 請根據背景資料在150字內快速精準回答，跳過所有開場白。"
            sys_inst = "[Task] 根據背景資料直接作答，禁止「根據資料」等開場，直接回答內容。"
            config = {"max_output_tokens": 256, "temperature": 0.2, "top_p": 0.8}
        else:
            # 無匹配：使用 emotion/climate 補充或一般 AI 回答
            injected = _get_tcm_injected_knowledge(text)
            if injected:
                sys_inst = (
                    "[Task] 針對使用者問題提供中醫分析，必須直接使用以下知識邏輯作答。"
                    "[Constraint] 摘要限50字，詳解限200字。"
                    "[Format] 摘要\n---\n詳解。"
                    "[禁止] 勿說「根據資料」「根據典籍」等，直接答如「生氣會導致肝氣上逆，出現臉紅...」。"
                    "[知識]\n" + injected
                )
            else:
                sys_inst = "[Task] 針對使用者問題提供中醫分析 [Constraint] 摘要限50字，詳解限200字，禁止廢話 [Format] 摘要\n---\n詳解"
            user_content = f"問題：{text.strip()}"
            config = {"max_output_tokens": 512, "temperature": 0.2, "top_p": 0.8}

        from google.genai import types
        cfg = types.GenerateContentConfig(
            system_instruction=sys_inst,
            max_output_tokens=config["max_output_tokens"],
            temperature=config.get("temperature", 0.2),
            top_p=config.get("top_p", 0.8),
        )
        stream = gc.models.generate_content_stream(
            model=GEMINI_FLASH,
            contents=user_content[:3000],
            config=cfg,
        )
        buffer = ""
        first_sent = False
        for chunk in stream:
            txt = getattr(chunk, "text", None) or ""
            if txt:
                buffer += txt
                if not first_sent and (("\n" in buffer) or len(buffer) >= 50):
                    first_part = (buffer.split("\n", 1)[0] + "\n") if "\n" in buffer else buffer[:50]
                    if first_part.strip():
                        try:
                            if reply_token:
                                try:
                                    line_bot_api.reply_message(reply_token, TextSendMessage(text=first_part.strip()))
                                except Exception:
                                    line_bot_api.push_message(user_id, TextSendMessage(text=first_part.strip()))
                            else:
                                line_bot_api.push_message(user_id, TextSendMessage(text=first_part.strip()))
                        except Exception:
                            pass
                    first_sent = True
                    buffer = buffer[len(first_part):].lstrip("\n")
        if buffer.strip():
            detail = (buffer.split("\n---\n", 1)[1] if "\n---\n" in buffer else buffer).strip().lstrip("-").strip()[:500]
            if detail:
                ai_reply = detail.rstrip() + SAFETY_DISCLAIMER
                log_question(redis, user_id, text)
                set_last_question(redis, user_id, text)
                set_last_assistant_message(redis, user_id, ai_reply)
                line_bot_api.push_message(user_id, text_with_quick_reply_quiz(ai_reply + "\n\n是否要進行一題小測驗？"))
                return True
        if first_sent:
            return True
    except Exception:
        pass
    return False


# --- 口說練習：糾錯與分析（Gemini 1.5 Pro 保證語感品質）---
def _evaluate_speech(transcript):
    """
    糾錯與分析：檢查語法、拼寫、用詞、語義完整性。
    回傳 (status: "Correct"|"NeedsImprovement", feedback_text: str, corrected_text: str 用於 TTS)。
    使用 Gemini 1.5 Pro 以保證語感品質。
    """
    if not (transcript or "").strip():
        return "Correct", "", ""
    sys_inst = (
        "你是英文發音與文法助教。分析學生語音辨識文字，執行：\n"
        "1. 檢查語法錯誤、單字拼寫錯誤、用詞不當\n"
        "2. 評估語義是否完整\n"
        "回傳 JSON（僅一行）：\n"
        '{"status":"Correct"或"NeedsImprovement","feedback":"簡短回饋","corrected":"修正後文本(Correct時空字串)"}\n'
        "Correct=完全正確且自然；NeedsImprovement=有細微錯誤。"
    )
    content, ok = _gemini_generate(GEMINI_PRO, f"學生說出的內容：{transcript[:500]}", sys_inst, max_tokens=250)
    if ok and content:
        for block in (content.split("```"), [content]):
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
    # Fallback to OpenAI when Gemini not configured
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": sys_inst},
                {"role": "user", "content": f"學生說出的內容：{transcript[:500]}"},
            ],
            max_tokens=250,
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        for block in (raw_text.split("```"), [raw_text]):
            for r in block:
                raw = r.strip() if isinstance(r, str) else ""
                if raw.startswith("{"):
                    try:
                        obj = json.loads(raw.split("\n")[0])
                        return (
                            (obj.get("status") or "Correct").strip()[:20] or "Correct",
                            (obj.get("feedback") or "").strip()[:400],
                            (obj.get("corrected") or "").strip()[:500],
                        )
                    except Exception:
                        pass
    except Exception:
        traceback.print_exc()
    return "Correct", "", ""

_cloudinary_config_done = False

def _upload_tts_to_cloudinary(audio_bytes, sentence=""):
    """上傳 TTS 語音至 Cloudinary（BytesIO 串流、video 資源型別優化音訊），回傳 (secure_url, duration_ms)。Lazy import。"""
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

# --- 寫作修訂模式：獨立處理，不經過 Assistant API / RAG ---
REVISION_MODE = "writing"
REVISION_MODE_PROMPT = "你已在【✍️ 寫作修訂】模式～請貼上要修改的段落。"
REDIS_KEY_USER_MODE = "user_mode"  # 與 Postback/切換按鈕寫入的 Key 完全一致：user_mode:{user_id}

# 寫作模式 prompt：回饋需含下列內容，但不要輸出標題給使用者
_REVISION_PROMPT = (
    "你是專業溫暖的語言老師。回覆時請自然融入以下內容，不要輸出【】標題："
    "（1）鼓勵／正面肯定"
    "（2）若有錯誤：需修改的原因＋修正後的版本（用 **粗體** 標示修改處）；若無誤則稱讚原文道地"
    "（3）鼓勵繼續發問、貼上其他句子練習"
    "語氣溫暖，段落分明易讀。"
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
    """寫作修訂模式：僅繼續練習按鈕（已取消離開模式）。"""
    return QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label="繼續練習", text="繼續練習")),
        ]
    )

def text_with_quick_reply_writing(content):
    return TextSendMessage(text=content, quick_reply=quick_reply_writing())

def _redis_user_mode_key(user_id):
    """統一的 Redis Key，與 Postback/切換按鈕寫入處完全一致。"""
    return f"{REDIS_KEY_USER_MODE}:{user_id}"

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
    快取優先：有有效快取時直接回傳，減少 Redis 讀取與 Device/resource busy 風險。
    Redis 失敗時：先嘗試本地快取，僅在快取也無效時才 fallback 至 tcm。
    """
    try:
        cached = _get_cached_mode(user_id)
        if cached:
            return cached
        if not redis:
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=redis_none")
            return "tcm"
        key = _redis_user_mode_key(user_id)
        mode_val = None
        for attempt in range(3):
            try:
                mode_val = redis.get(key)
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                # Redis 重試後仍失敗：嘗試快取
                cached = _get_cached_mode(user_id)
                if cached:
                    err_detail = f"errno={getattr(e, 'errno', 'N/A')} type={type(e).__name__}"
                    print(f"[MODE] _safe_get_mode user_id={user_id} redis_fail using_cache={cached} {err_detail}")
                    return cached
                err_detail = f"errno={getattr(e, 'errno', 'N/A')} type={type(e).__name__}"
                print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=exception_after_retry {err_detail} err={e}")
                traceback.print_exc()
                return "tcm"
        if mode_val is None:
            cached = _get_cached_mode(user_id)
            if cached:
                print(f"[MODE] _safe_get_mode user_id={user_id} key_missing using_cache={cached}")
                return cached
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=key_missing_or_null")
            return "tcm"
        if isinstance(mode_val, bytes):
            mode_str = mode_val.decode("utf-8", errors="replace").strip()
        else:
            mode_str = str(mode_val).strip()
        if not mode_str:
            cached = _get_cached_mode(user_id)
            if cached:
                return cached
            print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=empty_value raw={repr(mode_val)}")
            return "tcm"
        result = mode_str.lower()
        _set_cached_mode(user_id, result)
        return result
    except Exception as e:
        cached = _get_cached_mode(user_id)
        if cached:
            print(f"[MODE] _safe_get_mode user_id={user_id} outer_exception using_cache={cached} err={e}")
            return cached
        print(f"[MODE] _safe_get_mode user_id={user_id} fallback=tcm reason=exception err={e}")
        return "tcm"

# --- AI 核心函數（模式路由器）---
def _process_tcm_openai_fallback(user_id, text):
    """OpenAI Assistant 作為 Gemini 不可用時的 fallback。Local First 匹配時用精簡 prompt。"""
    if not client or not assistant_id:
        line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        return
    try:
        local_ctx, matched = _local_first_tcm_context(text)
        if matched and local_ctx:
            user_content = f"[背景資料]\n{local_ctx}\n\n[問題]\n{text}\n\n[指令] 請根據背景資料在150字內快速精準回答，跳過所有開場白。"
            mode_instructions = "根據背景資料直接作答。"
        else:
            mode_instructions = get_rag_instructions()
            injected = _get_tcm_injected_knowledge(text)
            inj_append = f"\n[強制使用以下知識，直接作答勿重複] {injected}" if injected else ""
            user_content = f"{mode_instructions}\n\n【中醫問答】使用者：{text}{inj_append}\n(完整醫理分析與建議，末尾參考資料出處)"

        thread_id = None
        if redis:
            try:
                t_id = redis.get(f"user_thread:{user_id}")
                if t_id is not None:
                    thread_id = t_id.decode("utf-8") if hasattr(t_id, "decode") else str(t_id)
                    if thread_id in ("None", "") or not thread_id.strip():
                        thread_id = None
            except Exception:
                pass
        if not thread_id:
            new_thread = client.beta.threads.create()
            thread_id = new_thread.id
            if redis:
                try:
                    redis.set(f"user_thread:{user_id}", thread_id)
                except Exception:
                    pass
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_content)
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)
        start_time = time.time()
        while run.status in ('queued', 'in_progress'):
            if time.time() - start_time > TIMEOUT_SECONDS:
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value.rstrip() + SAFETY_DISCLAIMER
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            line_bot_api.push_message(user_id, text_with_quick_reply_quiz(ai_reply + "\n\n是否要進行一題小測驗？"))
        else:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
    except Exception:
        line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))


def _process_tcm_two_stage(user_id, text, reply_token=None):
    """
    中醫問答：Gemini 1.5 Flash 串流模擬。
    極簡 Prompt、temperature=0.2、top_p=0.8。
    第一步：偵測到換行或50字即發極速摘要；第二步：剩餘詳解。
    """
    try:
        if not _gemini_tcm_stream_send(user_id, text, reply_token):
            _process_tcm_openai_fallback(user_id, text)
    except Exception:
        try:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
        except Exception:
            pass


def _process_assistant_sync(user_id, text, reply_token=None):
    """Assistant API 邏輯：Thread/Run/RAG，完成後 push_message。供 process-text-async 背景呼叫。"""
    try:
        mode = _safe_get_mode(user_id)
        if mode == REVISION_MODE:
            _revision_handler(user_id, text)
            return
        if mode == "tcm":
            _process_tcm_two_stage(user_id, text, reply_token=reply_token)
            return

        # 口說練習：Gemini 1.5 Pro 保證語感品質
        if mode == "speaking":
            sys_inst = get_rag_instructions()
            content, ok = _gemini_generate(GEMINI_PRO, f"【口說練習】使用者：{text}", sys_inst, max_tokens=2048)
            if ok and content:
                log_question(redis, user_id, text)
                set_last_question(redis, user_id, text)
                set_last_assistant_message(redis, user_id, content)
                line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(content))
                return
            tag = "🗣️ 口說練習"
        else:
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

        mode_instructions = get_writing_mode_instructions() if mode == "writing" else get_rag_instructions()
        user_content = f"{mode_instructions}\n\n【{tag}】\n使用者的話：{text}"

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
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            line_bot_api.push_message(user_id, text_with_quick_reply(ai_reply))
        else:
            line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))


def process_ai_request(event, user_id, text, is_voice=False):
    """State-Based Router：依 user_state (mode) 切換。寫作模式走 revision_handler，其餘走 Assistant API。"""
    try:
        mode = _safe_get_mode(user_id)
        if mode != "tcm":
            print(f"[MODE] process_ai_request user_id={user_id} mode={mode}")
        if mode == REVISION_MODE:
            _revision_handler(user_id, text)
            return
        reply_token = (event.reply_token if event and hasattr(event, "reply_token") else None) or None
        _process_assistant_sync(user_id, text, reply_token=reply_token)
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        line_bot_api.push_message(user_id, text_with_quick_reply(TIMEOUT_MESSAGE))


def _run_text_background(user_id, text, task, base_url, cron_secret, reply_token=None):
    """Background Task：觸發 process-text-async 或本地執行，不阻塞 webhook。"""
    print(f"[TEXT_BG] start user_id={user_id} task={task} has_base={bool(base_url)} has_secret={bool(cron_secret)}")
    payload = {"user_id": user_id, "text": text, "task": task}
    if reply_token:
        payload["reply_token"] = reply_token
    if base_url and cron_secret:
        try:
            r = requests.post(
                f"{base_url}/api/process-text-async",
                json=payload,
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=120,
            )
            print(f"[TEXT_BG] POST result status={r.status_code}")
        except Exception as e:
            print(f"[TEXT_BG] POST failed, fallback local err={e}")
            traceback.print_exc()
            try:
                if task == "revision":
                    _revision_handler(user_id, text)
                else:
                    _process_assistant_sync(user_id, text, reply_token=reply_token)
            except Exception as inner:
                print(f"[TEXT_BG] fallback handler failed err={inner}")
                traceback.print_exc()
    else:
        try:
            if task == "revision":
                _revision_handler(user_id, text)
            else:
                _process_assistant_sync(user_id, text, reply_token=reply_token)
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
                print(f"[VOICE] done speaking Correct")
                return
            line_bot_api.push_message(
                user_id,
                text_with_quick_reply(f"📊 口說練習回饋\n\n{feedback}"),
            )
            text_for_tts = corrected_text.strip() if corrected_text else transcript_text
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=f"🔊 請跟著唸：「{text_for_tts}」"),
            )
            try:
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
                    line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(VOICE_ERROR_MSG))
            except Exception as tts_err:
                print(f"[VOICE] TTS/Cloudinary err={tts_err}")
                traceback.print_exc()
                line_bot_api.push_message(user_id, text_with_quick_reply_speak_practice(VOICE_ERROR_MSG))
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


@app.route("/api/process-text-async", methods=["POST"])
def process_text_async():
    """Background Task：接收文字 AI 任務，執行寫作修訂或 Assistant RAG，完成後 push_message。"""
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
        reply_token = (data.get("reply_token") or "").strip() or None
        skip_log = (task == "assistant" and _safe_get_mode(user_id) == "tcm")
        if not skip_log:
            print(f"[process-text-async] task={task} user_id={user_id!r}")
        if not user_id:
            return "Missing user_id", 400
        if task == "revision":
            _revision_handler(user_id, text)
        else:
            _process_assistant_sync(user_id, text, reply_token=reply_token)
        if not skip_log:
            print(f"[process-text-async] done task={task}")
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
    """LINE Webhook 唯一入口（Vercel rewrite → 本檔）。Postback / Message 皆由此處理。"""
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
                msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 KV_REST_API 環境變數。"
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
    try:
        # --- Rich Menu 按鈕：立即回覆，避免延遲 ---
        if user_text == "中醫問答":
            try:
                _set_cached_mode(user_id, "tcm")
                if redis:
                    redis.set(_redis_user_mode_key(user_id), "tcm")
            except Exception:
                pass
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply("已切換至【🩺 中醫問答】模式，有什麼想問的嗎？"),
            )
            return
        if user_text == "口說練習":
            try:
                _set_cached_mode(user_id, "speaking")
                if redis:
                    redis.set(_redis_user_mode_key(user_id), "speaking")
            except Exception:
                pass
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"))
            return
        if user_text in ("寫作修改", "寫作修訂"):
            try:
                _set_cached_mode(user_id, REVISION_MODE)
                if redis:
                    redis.set(_redis_user_mode_key(user_id), REVISION_MODE)
            except Exception:
                pass
            msg = REVISION_MODE_PROMPT
            if not redis:
                msg += "\n\n⚠️ 模式無法儲存（Redis 未設定），請確認 KV_REST_API 環境變數。"
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply_writing(msg))
            return
        if user_text == "課務查詢":
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        # --- 寫作修訂模式隔離：優先判斷，跳過中醫邏輯 ---
        current_mode = _safe_get_mode(user_id)
        print(f"[MODE] handle_message user_id={user_id} current_mode={current_mode} text_preview={user_text[:50]!r}")
        if current_mode == REVISION_MODE:
            print(f"[MODE] handle_message -> REVISION_MODE branch, skipping TCM Assistant")
            if user_text in ("寫作修改", "寫作修訂"):
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_writing(REVISION_MODE_PROMPT),
                )
                return
            if user_text == "繼續練習":
                line_bot_api.reply_message(
                    event.reply_token,
                    text_with_quick_reply_writing("請貼上要修改的段落。"),
                )
                return
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="正在分析你的寫作，請稍候... ✨"),
            )
            vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
            base_url = f"https://{vercel_url}" if vercel_url and not vercel_url.startswith("http") else (vercel_url or "")
            cron_secret = os.getenv("CRON_SECRET", "")
            async_ok = False
            if base_url and cron_secret:
                try:
                    r = requests.post(
                        f"{base_url}/api/process-text-async",
                        json={"user_id": user_id, "text": user_text, "task": "revision"},
                        headers={"Authorization": f"Bearer {cron_secret}"},
                        timeout=5,
                    )
                    if r.status_code == 200:
                        async_ok = True
                        print(f"[REVISION] async POST ok status=200")
                    else:
                        print(f"[REVISION] async POST non-2xx status={r.status_code} body={r.text[:200]}")
                except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
                    print(f"[REVISION] async POST timeout - fallback sync to guarantee response err={e}")
                except Exception as e:
                    print(f"[REVISION] async POST failed - fallback sync err={e}")
                    traceback.print_exc()
            if not async_ok:
                print(f"[REVISION] running synchronously user_id={user_id}")
                _revision_handler(user_id, user_text)
            return

        # 課務查詢／本週重點：統一以 Flex Message 回傳
        if is_course_inquiry_intent(user_text):
            send_course_inquiry_flex(user_id, reply_token=event.reply_token)
            return

        # 小測驗後（舊狀態相容）：學生的回答視為新問題，交由 AI 處理
        if get_user_state(redis, user_id) == STATE_QUIZ_WAITING:
            set_user_state(redis, user_id, STATE_NORMAL)
            clear_quiz_data(redis, user_id)
            clear_quiz_pending(redis, user_id)
            try:
                mode = _safe_get_mode(user_id)
                immediate_msg = "正在為您查詢中醫典籍，請稍候..." if mode == "tcm" else "正在分析中..."
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=immediate_msg))
            except Exception:
                try:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正在處理中，請稍候..."))
                except Exception:
                    pass
            vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
            base_url = f"https://{vercel_url}" if vercel_url and not vercel_url.startswith("http") else (vercel_url or "")
            cron_secret = os.getenv("CRON_SECRET", "")
            payload = {"user_id": user_id, "text": user_text, "task": "assistant", "reply_token": event.reply_token}
            if base_url and cron_secret:
                try:
                    requests.post(
                        f"{base_url}/api/process-text-async",
                        json=payload,
                        headers={"Authorization": f"Bearer {cron_secret}"},
                        timeout=5,
                    )
                except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
                    pass
                except Exception:
                    threading.Thread(
                        target=_run_text_background,
                        args=(user_id, user_text, "assistant", base_url, cron_secret, event.reply_token),
                        daemon=True,
                    ).start()
            else:
                threading.Thread(
                    target=_run_text_background,
                    args=(user_id, user_text, "assistant", base_url, cron_secret, event.reply_token),
                    daemon=True,
                ).start()
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
                _set_cached_mode(user_id, "tcm")
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

        # 立即回覆（第一個 try-except，完全不依賴 AI 套件，避免冷啟動卡頓）
        mode = "tcm"  # 預設
        try:
            mode = _safe_get_mode(user_id)
            if mode == "tcm":
                immediate_msg = "正在為您查詢中醫典籍，請稍候..."
            else:
                mode_name = {"speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, "🩺 中醫問答")
                immediate_msg = f"正在以【{mode_name}】模式分析中..."
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=immediate_msg))
        except Exception:
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正在處理中，請稍候..."))
            except Exception:
                pass

        if mode != "tcm":
            print(f"[MODE] handle_message async AI mode={mode!r}")

        # 觸發 process-text-async：直接 POST（fire-and-forget）確保 Vercel 上一定被呼叫
        # threading 在 serverless 回傳後可能被凍結，導致 AI 永不執行
        vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
        base_url = f"https://{vercel_url}" if vercel_url and not vercel_url.startswith("http") else (vercel_url or "")
        cron_secret = os.getenv("CRON_SECRET", "")
        payload = {"user_id": user_id, "text": user_text, "task": "assistant", "reply_token": event.reply_token}
        if base_url and cron_secret:
            try:
                requests.post(
                    f"{base_url}/api/process-text-async",
                    json=payload,
                    headers={"Authorization": f"Bearer {cron_secret}"},
                    timeout=5,
                )
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
                pass  # Timeout 預期：process-text-async 需 20+ 秒，請求已送達即觸發新 invocation
            except Exception:
                threading.Thread(
                    target=_run_text_background,
                    args=(user_id, user_text, "assistant", base_url, cron_secret, event.reply_token),
                    daemon=True,
                ).start()
        else:
            threading.Thread(
                target=_run_text_background,
                args=(user_id, user_text, "assistant", base_url, cron_secret, event.reply_token),
                daemon=True,
            ).start()
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
        TextSendMessage(text="正在轉換語音，請稍候... 🎙️"),
    )

    vercel_url = (os.getenv("VERCEL_URL") or "").strip().rstrip("/")
    base_url = f"https://{vercel_url}" if vercel_url and not vercel_url.startswith("http") else (vercel_url or "")
    cron_secret = os.getenv("CRON_SECRET", "")
    async_ok = False

    if base_url and cron_secret:
        try:
            r = requests.post(
                f"{base_url}/api/process-voice-async",
                json={"user_id": user_id, "message_id": message_id},
                headers={"Authorization": f"Bearer {cron_secret}"},
                timeout=5,
            )
            if r.status_code == 200:
                async_ok = True
                print("[VOICE] async POST ok status=200")
            else:
                print(f"[VOICE] async POST non-2xx status={r.status_code}")
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
            print(f"[VOICE] async POST timeout - fallback sync err={e}")
        except Exception as e:
            print(f"[VOICE] async POST failed - fallback sync err={e}")
            traceback.print_exc()

    if not async_ok:
        print(f"[VOICE] running synchronously user_id={user_id}")
        _process_voice_sync(user_id, message_id)


if __name__ == "__main__":
    # 本地快速測試：python -m api.index 或 python api/index.py（從專案根目錄）
    # 再開一個終端執行 ngrok http 5000，並將 LINE Webhook 改為 https://YOUR-NGROK-URL/callback
    app.run(host="0.0.0.0", port=5000, debug=True)
