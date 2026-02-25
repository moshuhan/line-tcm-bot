# -*- coding: utf-8 -*-
import os
import re
import time
import base64
import json
import secrets
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
        get_future_topic_hint,
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        get_course_inquiry_instructions,
        is_course_inquiry_intent,
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
        record_weak_category,
        get_weak_categories,
        clear_weak_category,
        get_last_review_ask,
        set_last_review_ask,
        set_pending_review_category,
        get_pending_review_category,
        clear_pending_review_category,
        generate_socratic_question,
        judge_quiz_answer,
        generate_review_note,
    )
except ImportError:
    from syllabus import (
        get_future_topic_hint,
        is_off_topic,
        get_rag_instructions,
        get_writing_mode_instructions,
        get_course_inquiry_instructions,
        is_course_inquiry_intent,
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
        record_weak_category,
        get_weak_categories,
        clear_weak_category,
        get_last_review_ask,
        set_last_review_ask,
        set_pending_review_category,
        get_pending_review_category,
        clear_pending_review_category,
        generate_socratic_question,
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

# 安全聲明：涉及中醫診斷之回覆必須附加
SAFETY_DISCLAIMER = "\n\n⚠️ 僅供教學用途，不具醫療建議。"

WEEKLY_FOCUS = "本週重點：TCM 基礎—氣 (qi)、經絡 (meridians)、針灸 (acupuncture) 與中藥的平衡觀念。"
VOICE_COACH_TTS_VOICE = "shimmer"
TIMEOUT_SECONDS = 5
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

def _generate_tts_and_store(sentence, voice=None):
    """OpenAI TTS (model: tts-1) 產生語音，存 Redis，回傳 (url, duration_ms)。"""
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

# --- AI 核心函數（模式路由器）---
def process_ai_request(event, user_id, text, is_voice=False, course_inquiry=False):
    """State-Based Router：依 user_state (mode) 切換 System Prompt。"""
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

        if course_inquiry:
            mode_instructions = get_course_inquiry_instructions()
        elif mode == "writing":
            mode_instructions = get_writing_mode_instructions()
        else:
            mode_instructions = get_rag_instructions()

        user_content = f"{mode_instructions}\n\n【{tag}】\n使用者的話：{text}"
        if mode == "tcm" and not course_inquiry:
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
            if not course_inquiry and mode == "tcm":
                future_hint = get_future_topic_hint(text)
                if future_hint:
                    ai_reply = ai_reply.rstrip() + "\n\n" + future_hint
                ai_reply = ai_reply.rstrip() + SAFETY_DISCLAIMER
            log_question(redis, user_id, text)
            set_last_question(redis, user_id, text)
            set_last_assistant_message(redis, user_id, ai_reply)
            if mode == "tcm" and not course_inquiry:
                line_bot_api.push_message(user_id, text_with_quick_reply_quiz(ai_reply + "\n\n要來試試一題小測驗嗎？"))
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

        # 課務查詢：優先檢索 2026schedule.pdf、20260307courseintroduction.pdf，嚴禁拒絕
        if is_course_inquiry_intent(user_text):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="正在查詢課務資料..."))
            process_ai_request(event, user_id, user_text, is_voice=False, course_inquiry=True)
            return

        # 蘇格拉底測驗：正在等待測驗回答 → 判斷並回饋
        quiz_topic = get_quiz_pending(redis, user_id)
        if quiz_topic is not None:
            clear_quiz_pending(redis, user_id)
            feedback, category, was_correct = judge_quiz_answer(client, quiz_topic, user_text)
            if not was_correct:
                record_weak_category(redis, user_id, category)
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(feedback))
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

        # 蘇格拉底測驗：點擊「否」→ 按鈕消失，機器人保持沉默，不發送任何訊息
        if user_text == "否":
            return
        # 蘇格拉底測驗：點擊「是」→ 根據 last_assistant_message 即時生成題目（禁止靜態題庫）
        if user_text == "是":
            last_ctx = get_last_assistant_message(redis, user_id)
            socratic_q = generate_socratic_question(client, last_ctx)
            set_quiz_pending(redis, user_id, last_ctx or socratic_q)
            line_bot_api.reply_message(event.reply_token, text_with_quick_reply(socratic_q))
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
                    redis.set(f"user_mode:{user_id}", "tcm")
            except Exception:
                pass
            line_bot_api.reply_message(
                event.reply_token,
                text_with_quick_reply("已結束口說練習，已切換回中醫問答模式。"),
            )
            return

        # 精準過濾：僅完全與中醫/醫療學術無關（閒聊、娛樂、私人）→ 僅供學業使用
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
            # 口說練習：糾錯與分析 → Correct/NeedsImprovement → 強制 TTS 示範（NeedsImprovement）
            status, feedback, corrected_text = _evaluate_speech(transcript_text)
            if status == "Correct":
                line_bot_api.push_message(
                    user_id,
                    text_with_quick_reply_speak_practice("發音非常標準！太棒了！\n\n要再練習下一句嗎？"),
                )
            else:
                line_bot_api.push_message(
                    user_id,
                    text_with_quick_reply(f"📊 口說練習回饋\n\n{feedback}"),
                )
                text_for_tts = corrected_text.strip() if corrected_text else transcript_text
                audio_url, duration_ms = _generate_tts_and_store(text_for_tts, voice=VOICE_COACH_TTS_VOICE)
                if audio_url and duration_ms:
                    line_bot_api.push_message(
                        user_id,
                        AudioSendMessage(original_content_url=audio_url, duration=duration_ms),
                    )
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply_speak_practice(
                            f"🔊 示範語音請跟著唸：\n\n「{text_for_tts}」\n\n要再練習下一句嗎？"
                        ),
                    )
                else:
                    line_bot_api.push_message(
                        user_id,
                        text_with_quick_reply_speak_practice(
                            f"修正文本：{text_for_tts}\n\n要再練習下一句嗎？"
                        ),
                    )
        else:
            # 非口說模式：課務查詢或 AI
            if is_course_inquiry_intent(transcript_text):
                line_bot_api.push_message(user_id, TextSendMessage(text="正在查詢課務資料..."))
                process_ai_request(event, user_id, transcript_text, is_voice=True, course_inquiry=True)
            elif is_off_topic(transcript_text):
                line_bot_api.push_message(user_id, text_with_quick_reply("本機器人僅供學業使用。"))
            else:
                process_ai_request(event, user_id, transcript_text, is_voice=True)
    except Exception as e:
        traceback.print_exc()
        line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))
