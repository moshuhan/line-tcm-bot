import os
import time
import traceback
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage
from upstash_redis import Redis
from openai import OpenAI

# 1. 初始化
app = Flask(__name__)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
line_webhook_handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")

kv_url = os.getenv("KV_REST_API_URL")
kv_token = os.getenv("KV_REST_API_TOKEN")
redis = Redis(url=kv_url, token=kv_token) if kv_url and kv_token else None

# --- AI 核心函數 (放在前面確保被讀取) ---
def process_ai_request(event, user_id, text, is_voice=False):
    try:
        # 模式讀取
        mode_val = redis.get(f"user_mode:{user_id}") if redis else None
        mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")
        
        # 決定 AI 身分標籤
        tag = "🩺 中醫問答"
        if mode == "speaking": tag = "🗣️ 口說練習"
        elif mode == "writing": tag = "✍️ 寫作修訂"

        # Thread ID 管理
        t_id = redis.get(f"user_thread:{user_id}") if redis else None
        thread_id = t_id.decode('utf-8') if hasattr(t_id, 'decode') else (str(t_id) if t_id and t_id != "None" else None)
        
        if not thread_id:
            new_thread = client.beta.threads.create()
            thread_id = new_thread.id
            if redis: redis.set(f"user_thread:{user_id}", thread_id)
        
        # 傳送訊息給 Assistant
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"【目前模式：{tag}】使用者的話：{text}"
        )
        
        run = client.beta.threads.runs.create(thread_id=thread_id, assistant_id=assistant_id)
        
        # 輪詢結果
        start_time = time.time()
        while run.status in ['queued', 'in_progress']:
            if time.time() - start_time > 8.5: break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text="⏳ AI 仍在思考中，請 5 秒後傳送隨意文字，我就能顯示結果！"))

    except Exception as e:
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 處理失敗：{str(e)[:50]}"))

# --- 路由設定 ---

@app.route("/", methods=['GET'])
def home():
    return 'Line Bot Server is running!', 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        line_webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

# --- 事件處理 ---

@line_webhook_handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    mode = event.postback.data.split('=')[1] if '=' in event.postback.data else "tcm"
    if redis: redis.set(f"user_mode:{user_id}", mode)
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"已切換至【{mode_map.get(mode)}】模式"))

@line_webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    # 模式讀取 (用於顯示回覆)
    mode_val = redis.get(f"user_mode:{user_id}") if redis else None
    mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")
    mode_name = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}.get(mode, "🩺 中醫問答")
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"正在以【{mode_name}】模式分析中..."))
    process_ai_request(event, user_id, event.message.text)

@line_webhook_handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    user_id = event.source.user_id
    # 修正點：確保這裡是 line_bot_api
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🎙️ 正在轉換語音..."))
    
    # 修正點：確保這裡是 line_bot_api
    message_content = line_bot_api.get_message_content(event.message.id)
    temp_path = f"/tmp/{event.message.id}.m4a"
    with open(temp_path, 'wb') as f:
        for chunk in message_content.iter_content(): f.write(chunk)
    
    try:
        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
        os.remove(temp_path)
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🎤 辨識內容：「{transcript.text}」"))
        process_ai_request(event, user_id, transcript.text, is_voice=True)
    except Exception as e:
        line_bot_api.push_message(user_id, TextSendMessage(text="❌ 語音辨識失敗"))