# -*- coding: utf-8 -*-
import os
import re
import time
import difflib
import tempfile
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent,
    AudioMessage, QuickReply, QuickReplyButton, MessageAction,
)
from upstash_redis import Redis
from openai import OpenAI

app = Flask(__name__)

# ========== 1. 初始化（保留原有 upstash_redis 連線設定）==========
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
redis = Redis(url=os.getenv("KV_REST_API_URL"), token=os.getenv("KV_REST_API_TOKEN"))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

# 安全聲明：涉及中醫診斷之回覆必須附加
SAFETY_DISCLAIMER = "\n\n⚠️ 僅供教學用途，不具醫療建議。"

# ========== 教材與術語（可依週次更新）==========
# 原始教材文本：用於 Shadowing 對比
SHADOWING_REFERENCE = (
    "Traditional Chinese Medicine (TCM) emphasizes the balance of qi and the flow of energy "
    "through meridians. Acupuncture and herbal medicine are used to restore this balance."
)
# 本週需掌握的 TCM 關鍵術語（用於比對漏唸/唸錯）
TCM_TERMS = [
    "qi", "meridian", "meridians", "acupuncture", "herbal", "balance",
    "Traditional Chinese Medicine", "TCM", "energy",
]
# 本週重點摘要（課務用）
WEEKLY_FOCUS = "本週重點：TCM 基礎—氣 (qi)、經絡 (meridians)、針灸 (acupuncture) 與中藥的平衡觀念。"

# ========== 課務助教模組 (Course Ops) ==========
def get_course_info(message_text):
    """根據關鍵字（評分、課表、作業等）回傳課綱資訊。"""
    if not message_text or not message_text.strip():
        return None
    text = message_text.strip()
    # 評分標準
    if "評分" in text or "成績" in text or "grading" in text.lower():
        return (
            "📋 評分標準\n"
            "・期末專題：30%\n"
            "・課堂參與：30%\n"
            "・出席：40%\n"
            "如有疑問請洽課程助教。"
        )
    # 課表
    if "課表" in text or "schedule" in text.lower() or "上課時間" in text:
        return (
            "📅 課表\n"
            "請以學校公布之當學期課表為準；EMI 中醫課程通常為週間排課，詳見選課系統。"
        )
    # 作業
    if "作業" in text or "assignment" in text.lower() or "繳交" in text:
        return (
            "📝 作業\n"
            "作業與繳交期限依教師當週公告為準；期末專題格式與說明將於期中後公布。"
        )
    return None

# ========== Shadowing：比對辨識結果與教材，產出回饋報告 ==========
def build_shadowing_report(transcript, reference_text, tcm_terms):
    """
    使用 difflib 比對學生語音辨識文字與教材，找出漏唸/唸錯的 TCM 術語，
    回傳 (正確率百分比, 需改進單字列表, 發音建議文字)。
    """
    transcript_lower = (transcript or "").strip().lower()
    reference_lower = reference_text.strip().lower()
    # 正規化：只保留字母與空白，方便比對
    def norm(s):
        return re.sub(r"[^a-z\s]", " ", s).strip()
    transcript_norm = norm(transcript_lower)
    ref_norm = norm(reference_lower)
    transcript_words = set(transcript_norm.split())
    ref_words = set(ref_norm.split())

    # 從 reference 中出現的術語（取小寫、拆成單字或整詞）
    terms_in_ref = []
    for term in tcm_terms:
        t_lower = term.lower()
        if t_lower in reference_lower:
            terms_in_ref.append(t_lower)
    if not terms_in_ref:
        terms_in_ref = [w for w in ref_words if len(w) > 2][:15]  # fallback

    correct_count = 0
    words_to_improve = []
    for term in terms_in_ref:
        term_words = set(term.split())
        # 檢查：整詞有出現在辨識結果中，或每個字都有出現
        if term in transcript_lower:
            correct_count += 1
            continue
        # 模糊比對：學生可能唸錯
        matches = difflib.get_close_matches(term, transcript_norm.split(), n=1, cutoff=0.6)
        if matches:
            correct_count += 1
            continue
        words_to_improve.append(term)

    total_terms = len(terms_in_ref) if terms_in_ref else 1
    correct_rate = round(100 * correct_count / total_terms)

    # 整體相似度（可選）
    similarity = difflib.SequenceMatcher(None, transcript_norm, ref_norm).ratio()
    similarity_pct = round(100 * similarity)

    # 發音建議
    if not words_to_improve:
        pronunciation_tip = "發音與關鍵術語掌握良好，請持續練習整段流暢度。"
    else:
        pronunciation_tip = (
            "建議多聽教材音檔並跟讀以下術語："
            + "、".join(words_to_improve[:10])
            + "。可善用線上發音字典確認重音與音節。"
        )

    report = (
        f"📊 Shadowing 回饋報告\n"
        f"・正確率：{correct_rate}%（關鍵術語）\n"
        f"・整體與教材相似度：{similarity_pct}%\n"
        f"・需改進單字：{', '.join(words_to_improve) if words_to_improve else '無'}\n"
        f"・發音建議：{pronunciation_tip}"
    )
    return report

# ========== QuickReply：對話結束後提供快捷選項 ==========
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
    """產生帶 QuickReply 按鈕的文字訊息。"""
    return TextSendMessage(text=content, quick_reply=quick_reply_items())

# ========== 2. LINE Webhook 進入點 ==========
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature') or request.headers.get('x-line-signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ========== 3. 處理模式切換 (Postback) ==========
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    mode = event.postback.data.split('=')[1] if '=' in event.postback.data else "tcm"
    redis.set(f"user_mode:{user_id}", mode)
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
    reply_msg = f"已切換至【{mode_map.get(mode, '未知')}】模式，請開始輸入！"
    line_bot_api.reply_message(event.reply_token, text_with_quick_reply(reply_msg))

# ========== 4. 處理文字訊息 ==========
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = (event.message.text or "").strip()

    # 課務查詢：關鍵字觸發 get_course_info
    course_info = get_course_info(user_text)
    if course_info is not None:
        line_bot_api.reply_message(event.reply_token, text_with_quick_reply(course_info))
        return

    # 本週重點
    if user_text == "本週重點":
        line_bot_api.reply_message(event.reply_token, text_with_quick_reply(WEEKLY_FOCUS))
        return

    # 口說練習 / 寫作修改：切換模式並回覆
    if user_text == "口說練習":
        redis.set(f"user_mode:{user_id}", "speaking")
        line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【🗣️ 口說練習】模式，可傳送語音或文字。"))
        return
    if user_text == "寫作修改":
        redis.set(f"user_mode:{user_id}", "writing")
        line_bot_api.reply_message(event.reply_token, text_with_quick_reply("已切換至【✍️ 寫作修訂】模式，請貼上要修改的段落。"))
        return

    # 從 Redis 讀取模式（保留原有邏輯）
    mode_val = redis.get(f"user_mode:{user_id}")
    mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")

    prompts = {
        "tcm": "你是中醫專家，請針對以下問題提供專業建議：",
        "speaking": "你是 EMI 英文口說教練，請分析以下句子的發音重點與醫學術語：",
        "writing": "你是學術寫作顧問，請針對以下段落提供 Grammar, Terminology, Logic 三方面的修訂建議：",
    }
    mode_name = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, "🩺 中醫問答")

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"正在以【{mode_name}】模式分析中..."),
    )
    process_ai_request(event, user_id, user_text, is_voice=False)

# ========== 5. 處理語音訊息（含 Shadowing）==========
@handler.add(MessageEvent, message=AudioMessage)
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

        # Shadowing：與教材對比，產出回饋報告
        report = build_shadowing_report(transcript_text, SHADOWING_REFERENCE, TCM_TERMS)
        line_bot_api.push_message(user_id, text_with_quick_reply(report))

        # 再依目前模式送 AI 分析（口說模式可給額外建議）
        process_ai_request(event, user_id, transcript_text, is_voice=True)
    except Exception as e:
        line_bot_api.push_message(user_id, text_with_quick_reply("❌ 語音辨識失敗，請再試一次。"))

# ========== 6. AI 請求（Assistant API + 安全聲明 + QuickReply）==========
def process_ai_request(event, user_id, text, is_voice=False):
    try:
        mode_val = redis.get(f"user_mode:{user_id}")
        mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")
        tag = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, "🩺 中醫問答")

        thread_key = f"user_thread:{user_id}"
        t_id = redis.get(thread_key)
        thread_id = t_id.decode('utf-8') if hasattr(t_id, 'decode') else (str(t_id) if t_id and str(t_id) != "None" else None)

        if not thread_id:
            new_thread = client.beta.threads.create()
            thread_id = new_thread.id
            redis.set(thread_key, thread_id)

        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"【目前模式：{tag}】\n(提醒：請務必在回答末尾提供參考資料出處)\n使用者的話：{text}",
        )
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)

        start = time.time()
        while run.status in ('queued', 'in_progress'):
            if time.time() - start > 8.5:
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            # 中醫問答模式附加安全聲明
            if mode == "tcm":
                ai_reply = ai_reply.rstrip() + SAFETY_DISCLAIMER
            line_bot_api.push_message(user_id, text_with_quick_reply(ai_reply))
        else:
            line_bot_api.push_message(user_id, text_with_quick_reply("⏳ AI 仍在思考中，請稍後再傳一則訊息以取得結果。"))
    except Exception as e:
        line_bot_api.push_message(user_id, text_with_quick_reply(f"❌ 處理失敗：{str(e)[:80]}"))

if __name__ == "__main__":
    app.run()
