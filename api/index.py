# -*- coding: utf-8 -*-
import os
import re
import time
import base64
import secrets
import difflib
import tempfile
import traceback
from flask import Flask, request, abort, Response
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage,
    QuickReply, QuickReplyButton, MessageAction,
)
from linebot.models.send_messages import AudioSendMessage
from upstash_redis import Redis
from openai import OpenAI

try:
    from api.syllabus import (
        get_future_topic_reply,
        is_off_topic,
        get_rag_instructions,
    )
except ImportError:
    from syllabus import (
        get_future_topic_reply,
        is_off_topic,
        get_rag_instructions,
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

# 安全聲明：涉及中醫診斷之回覆必須附加
SAFETY_DISCLAIMER = "\n\n⚠️ 僅供教學用途，不具醫療建議。"

# 教材與術語（可依週次更新）
SHADOWING_REFERENCE = (
    "Traditional Chinese Medicine (TCM) emphasizes the balance of qi and the flow of energy "
    "through meridians. Acupuncture and herbal medicine are used to restore this balance."
)
TCM_TERMS = [
    "qi", "meridian", "meridians", "acupuncture", "herbal", "balance",
    "Traditional Chinese Medicine", "TCM", "energy",
]
WEEKLY_FOCUS = "本週重點：TCM 基礎—氣 (qi)、經絡 (meridians)、針灸 (acupuncture) 與中藥的平衡觀念。"

# 口說練習 Shadowing 用句庫（可依週次擴充）
TCM_EMI_SENTENCES = [
    "Traditional Chinese Medicine (TCM) emphasizes the balance of qi and the flow of energy through meridians.",
    "Acupuncture and herbal medicine are used to restore this balance.",
    "TCM views the body as an integrated whole, with organs and meridians connected.",
    "The concept of yin and yang is fundamental to understanding TCM.",
    "Herbal prescriptions are often combined to enhance therapeutic effects.",
]

# --- 口說練習：新句/重複判斷、評分、TTS ---
def _norm_text(s):
    return re.sub(r"[^a-z\s]", " ", (s or "").strip().lower()).strip()

def _get_shadowing_sentence(user_id):
    """取得目前練習句（Redis）。"""
    try:
        if not redis:
            return None
        val = redis.get(f"shadowing_sentence:{user_id}")
        if val is None:
            return None
        return val.decode("utf-8") if hasattr(val, "decode") else str(val)
    except Exception:
        return None

def _set_shadowing_sentence(user_id, sentence):
    try:
        if redis:
            redis.set(f"shadowing_sentence:{user_id}", sentence)
    except Exception:
        pass

def _clear_shadowing_sentence(user_id):
    try:
        if redis:
            redis.delete(f"shadowing_sentence:{user_id}")
    except Exception:
        pass

def _shadowing_similarity(a, b):
    """0~1，愈高愈像。"""
    an, bn = _norm_text(a), _norm_text(b)
    if not an or not bn:
        return 0.0
    return difflib.SequenceMatcher(None, an, bn).ratio()

def _is_repeat_practice(transcript, stored_sentence):
    """是否為「重複練習上一句」而非新句子。"""
    if not stored_sentence or not (transcript or "").strip():
        return False
    return _shadowing_similarity(transcript, stored_sentence) >= 0.5

def _score_shadowing(transcript, reference):
    """依與參考句相似度給 0~100 分。"""
    r = _shadowing_similarity(transcript, reference)
    return min(100, round(r * 100))

def _build_speaking_feedback(transcript, reference, score):
    """評分 + 需改進單字 + 發音建議。"""
    ref_lower = reference.strip().lower()
    terms_in_ref = [t for t in TCM_TERMS if t.lower() in ref_lower]
    transcript_lower = (transcript or "").strip().lower()
    transcript_norm = _norm_text(transcript)
    ref_norm = _norm_text(reference)
    words_to_improve = []
    for term in terms_in_ref:
        t_lower = term.lower()
        if t_lower in transcript_lower:
            continue
        if difflib.get_close_matches(t_lower, transcript_norm.split(), n=1, cutoff=0.6):
            continue
        words_to_improve.append(term)
    tip = "發音與關鍵術語掌握良好。" if not words_to_improve else "建議多聽並跟讀以下術語：" + "、".join(words_to_improve[:8]) + "。"
    return (
        f"📊 口說練習回饋\n"
        f"・評分：{score} 分\n"
        f"・需改進單字：{', '.join(words_to_improve) if words_to_improve else '無'}\n"
        f"・建議：{tip}"
    )

def _get_next_speaking_sentence(user_id):
    """輪流取下一句練習句。"""
    try:
        if redis:
            idx_val = redis.get(f"shadowing_index:{user_id}")
            idx = 0
            if idx_val is not None:
                idx = int(idx_val.decode("utf-8") if hasattr(idx_val, "decode") else idx_val)
            sentence = TCM_EMI_SENTENCES[idx % len(TCM_EMI_SENTENCES)]
            redis.set(f"shadowing_index:{user_id}", str((idx + 1) % len(TCM_EMI_SENTENCES)))
            return sentence
    except Exception:
        pass
    return TCM_EMI_SENTENCES[0]

def _generate_tts_and_store(sentence):
    """OpenAI TTS 產生語音，存 Redis，回傳 (url, duration_ms)。"""
    token = secrets.token_urlsafe(12)
    base_url = (os.getenv("VERCEL_URL") and f"https://{os.getenv('VERCEL_URL').rstrip('/')}") or request.host_url.rstrip("/")
    try:
        resp = client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=sentence[:4096],
        )
        path = tempfile.mktemp(suffix=".mp3")
        resp.stream_to_file(path)
        with open(path, "rb") as f:
            audio_bytes = f.read()
        try:
            os.remove(path)
        except OSError:
            pass
        duration_ms = max(1000, int(len(sentence.split()) / 2.2 * 1000))
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        try:
            if redis:
                redis.set(f"tts_audio:{token}", b64, ex=600)
        except Exception:
            pass
        return (f"{base_url}/audio/{token}", duration_ms)
    except Exception as e:
        traceback.print_exc()
        return (None, 0)

# --- 課務助教模組 (Course Ops) ---
def get_course_info(message_text):
    """根據關鍵字（評分、課表、作業等）回傳課綱資訊。"""
    if not message_text or not message_text.strip():
        return None
    text = message_text.strip()
    if "評分" in text or "成績" in text or "grading" in text.lower():
        return (
            "📋 評分標準\n"
            "・期末專題：30%\n"
            "・課堂參與：30%\n"
            "・出席：40%\n"
            "如有疑問請洽課程助教。"
        )
    if "課表" in text or "schedule" in text.lower() or "上課時間" in text:
        return (
            "📅 課表\n"
            "請以學校公布之當學期課表為準；EMI 中醫課程通常為週間排課，詳見選課系統。"
        )
    if "作業" in text or "assignment" in text.lower() or "繳交" in text:
        return (
            "📝 作業\n"
            "作業與繳交期限依教師當週公告為準；期末專題格式與說明將於期中後公布。"
        )
    return None

def get_course_overview():
    """課務總覽（選單「課務查詢」用）。"""
    return (
        "📋 課務總覽\n\n"
        "・評分標準：期末專題 30%、課堂參與 30%、出席 40%\n"
        "・課表：以學校當學期課表為準，詳見選課系統\n"
        "・作業：依教師當週公告；期末專題說明期中後公布\n\n"
        "如有疑問請洽課程助教。"
    )

# --- Shadowing：比對辨識結果與教材，產出回饋報告 ---
def build_shadowing_report(transcript, reference_text, tcm_terms):
    transcript_lower = (transcript or "").strip().lower()
    reference_lower = reference_text.strip().lower()

    def norm(s):
        return re.sub(r"[^a-z\s]", " ", s).strip()

    transcript_norm = norm(transcript_lower)
    ref_norm = norm(reference_lower)
    ref_words = set(ref_norm.split())

    terms_in_ref = [t.lower() for t in tcm_terms if t.lower() in reference_lower]
    if not terms_in_ref:
        terms_in_ref = [w for w in ref_words if len(w) > 2][:15]

    correct_count = 0
    words_to_improve = []
    for term in terms_in_ref:
        if term in transcript_lower:
            correct_count += 1
            continue
        matches = difflib.get_close_matches(term, transcript_norm.split(), n=1, cutoff=0.6)
        if matches:
            correct_count += 1
            continue
        words_to_improve.append(term)

    total_terms = len(terms_in_ref) if terms_in_ref else 1
    correct_rate = round(100 * correct_count / total_terms)
    similarity = difflib.SequenceMatcher(None, transcript_norm, ref_norm).ratio()
    similarity_pct = round(100 * similarity)

    if not words_to_improve:
        pronunciation_tip = "發音與關鍵術語掌握良好，請持續練習整段流暢度。"
    else:
        pronunciation_tip = (
            "建議多聽教材音檔並跟讀以下術語："
            + "、".join(words_to_improve[:10])
            + "。可善用線上發音字典確認重音與音節。"
        )

    return (
        f"📊 Shadowing 回饋報告\n"
        f"・正確率：{correct_rate}%（關鍵術語）\n"
        f"・整體與教材相似度：{similarity_pct}%\n"
        f"・需改進單字：{', '.join(words_to_improve) if words_to_improve else '無'}\n"
        f"・發音建議：{pronunciation_tip}"
    )

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

def _safe_get_mode(user_id):
    """安全取得使用者模式，Redis 失敗時回傳 tcm。"""
    try:
        if not redis:
            return "tcm"
        mode_val = redis.get(f"user_mode:{user_id}")
        if mode_val is None:
            return "tcm"
        if hasattr(mode_val, "decode"):
            return mode_val.decode("utf-8").strip() or "tcm"
        return str(mode_val).strip() or "tcm"
    except Exception:
        return "tcm"

# --- AI 核心函數 ---
def process_ai_request(event, user_id, text, is_voice=False):
    try:
        mode = _safe_get_mode(user_id)
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

        rag_instructions = get_rag_instructions()
        user_content = (
            f"{rag_instructions}\n\n"
            f"【目前模式：{tag}】\n(提醒：請務必在回答末尾提供參考資料出處)\n使用者的話：{text}"
        )
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_content,
        )
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)

        start_time = time.time()
        while run.status in ['queued', 'in_progress']:
            if time.time() - start_time > 8.5:
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            if mode == "tcm":
                ai_reply = ai_reply.rstrip() + SAFETY_DISCLAIMER
            line_bot_api.push_message(user_id, text_with_quick_reply(ai_reply))
        else:
            line_bot_api.push_message(user_id, text_with_quick_reply("⏳ AI 仍在思考中，請 5 秒後傳送隨意文字，我就能顯示結果！"))
    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        line_bot_api.push_message(user_id, text_with_quick_reply(f"❌ 處理失敗：{str(e)[:80]}"))

# --- 路由設定 ---
@app.route("/", methods=['GET'])
def home():
    return 'Line Bot Server is running!', 200

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
        if data == "action=course":
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(get_course_overview()))
            return
        if data == "action=weekly":
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(WEEKLY_FOCUS))
            return
        # mode=tcm / mode=speaking / mode=writing
        mode = data.split("=")[1] if "=" in data else "tcm"
        try:
            if redis:
                redis.set(f"user_mode:{user_id}", mode)
        except Exception:
            pass
        mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
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
        course_info = get_course_info(user_text)
        if course_info is not None:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(course_info))
            return

        if user_text == "本週重點":
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(WEEKLY_FOCUS))
            return

        if user_text == "口說練習":
            try:
                if redis:
                    redis.set(f"user_mode:{user_id}", "speaking")
            except Exception:
                pass
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"))
            return
        if user_text == "寫作修改":
            try:
                if redis:
                    redis.set(f"user_mode:{user_id}", "writing")
            except Exception:
                pass
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【✍️ 寫作修訂】模式，請貼上要修改的段落。"))
            return

        # 時間感知檢索與課綱鎖定：未來課程主題 → 引導訊息
        future_reply = get_future_topic_reply(user_text)
        if future_reply:
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(future_reply))
            return
        # 學術過濾：非課綱且與中醫無關 → 僅供學業使用
        if is_off_topic(user_text):
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply("本機器人僅供學業使用。"))
            return

        mode = _safe_get_mode(user_id)
        mode_name = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, "🩺 中醫問答")

        # 先回覆「正在分析」，再同步執行 AI（Vercel 背景執行緒可能被終止，改回同步以確保有回覆）
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"正在以【{mode_name}】模式分析中..."))
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
    user_id = event.source.user_id
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🎙️ 正在轉換語音..."))

    message_content = line_bot_api.get_message_content(event.message.id)
    tmp_dir = tempfile.gettempdir()
    temp_path = os.path.join(tmp_dir, f"{event.message.id}.m4a")
    try:
        with open(temp_path, 'wb') as f:
            for chunk in message_content.iter_content():
                f.write(chunk)
    except Exception:
        temp_path = os.path.join(os.path.dirname(__file__) or ".", f"{event.message.id}.m4a")
        with open(temp_path, 'wb') as f:
            for chunk in message_content.iter_content():
                f.write(chunk)

    try:
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

        if mode == "speaking":
            # 口說練習：新句 vs 重複練習 → 評分與意見 → 未滿 100 分才送 Shadowing 語音
            stored = _get_shadowing_sentence(user_id)
            is_repeat = _is_repeat_practice(transcript_text, stored)

            if not is_repeat:
                # 新句子：給一句練習句 + TTS 供 Shadowing
                sentence = _get_next_speaking_sentence(user_id)
                _set_shadowing_sentence(user_id, sentence)
                line_bot_api.push_message(
                    user_id,
                    text_with_quick_reply(f"🆕 新的練習句，請跟著唸：\n\n「{sentence}」"),
                )
                audio_url, duration_ms = _generate_tts_and_store(sentence)
                if audio_url and duration_ms:
                    line_bot_api.push_message(
                        user_id,
                        AudioSendMessage(original_content_url=audio_url, duration=duration_ms),
                    )
            else:
                # 重複練習上一句：評分、回饋
                score = _score_shadowing(transcript_text, stored)
                feedback = _build_speaking_feedback(transcript_text, stored, score)
                line_bot_api.push_message(user_id, text_with_quick_reply(feedback))

                if score >= 100:
                    _clear_shadowing_sentence(user_id)
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply(
                            "🎉 恭喜達標！100 分。不會再播放 Shadowing 語音，傳送下一則語音即可開始下一句練習。"
                        ),
                    )
                else:
                    # 未滿 100：再送一次同一句 TTS 供繼續跟讀
                    audio_url, duration_ms = _generate_tts_and_store(stored)
                    if audio_url and duration_ms:
                        line_bot_api.push_message(
                            user_id,
                            AudioSendMessage(original_content_url=audio_url, duration=duration_ms),
                        )
        else:
            # 非口說模式：Shadowing 報告 + 課綱鎖定檢查 + AI
            report = build_shadowing_report(transcript_text, SHADOWING_REFERENCE, TCM_TERMS)
            line_bot_api.push_message(user_id, text_with_quick_reply(report))
            future_reply = get_future_topic_reply(transcript_text)
            if future_reply:
                line_bot_api.push_message(user_id, text_with_quick_reply(future_reply))
                return
            if is_off_topic(transcript_text):
                line_bot_api.push_message(user_id, text_with_quick_reply("本機器人僅供學業使用。"))
                return
            process_ai_request(event, user_id, transcript_text, is_voice=True)
    except Exception as e:
        traceback.print_exc()
        line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))
