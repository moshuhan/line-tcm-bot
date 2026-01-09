import os
import time
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, PostbackEvent, AudioMessage
from upstash_redis import Redis
from openai import OpenAI

app = Flask(__name__)
app.debug = True # 選配：方便看更多詳細錯誤
app = app
line_webhook_handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# 1. 初始化所有連線資訊 (金鑰會自動從 Vercel 環境變數讀取)
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))

redis = Redis(url=os.getenv("KV_REST_API_URL"), token=os.getenv("KV_REST_API_TOKEN"))
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("OPENAI_ASSISTANT_ID")
# 測試用：直接 print 出來（部署後在 Log 看有沒有印出 asst_...）
print(f"DEBUG: Current Assistant ID is {assistant_id}")

# 2. LINE Webhook 進入點
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        # 修改這裡
        line_webhook_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 3. 處理模式切換 (Postback)
@line_webhook_handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    # 取得 data (例如 'mode=speaking')
    mode = event.postback.data.split('=')[1] if '=' in event.postback.data else "tcm"
    
    # 將狀態存入 Vercel KV
    redis.set(f"user_mode:{user_id}", mode)
    
    # 模式名稱對照
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}
    reply_msg = f"已切換至【{mode_map.get(mode, '未知')}】模式，請開始輸入！"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

# 4. 處理文字訊息
@line_webhook_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    
    # A. 取得模式 (單純為了在回覆中顯示)
    mode_val = redis.get(f"user_mode:{user_id}")
    # 如果已經是字串就直接用，如果是 bytes 才 decode
mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")
    mode_map = {"tcm": "🩺 中醫問答", "speaking": "🗣️ 口說練習", "writing": "✍️ 寫作修訂"}

    # B. 立即回覆，防止 LINE Webhook 超時
    line_bot_api.reply_message(
        event.reply_token, 
        TextSendMessage(text=f"已收到您的訊息，正在以【{mode_map.get(mode, '中醫專家')}】模式分析中...")
    )
    
    # C. 呼叫後台 AI 處理 (內部會用 push_message 回傳答案)
    process_ai_request(event, user_id, user_text)

# 5. 處理語音訊息
@line_webhook_handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    user_id = event.source.user_id
    message_id = event.message.id
    
    # A. 立即回覆，防止 LINE Webhook 超時
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="🎙️ 收到語音！正在轉換並分析中，請稍候...")
    )
    
    # B. 下載語音檔到 Vercel 的暫存空間
    message_content = line_bot_api.get_message_content(message_id)
    temp_path = f"/tmp/{message_id}.m4a"
    with open(temp_path, 'wb') as f:
        for chunk in message_content.iter_content():
            f.write(chunk)
    
    try:
        # C. 語音轉文字
        with open(temp_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        user_voice_text = transcript.text
        os.remove(temp_path) # 刪除暫存
        
        # D. 告知辨識結果 (用 push)
        line_bot_api.push_message(user_id, TextSendMessage(text=f"🎤 辨識內容：\n「{user_voice_text}」"))
        
        # E. 串接 AI 處理 (用 push)
        process_ai_request(event, user_id, user_voice_text, is_voice=True)

    except Exception as e:
        print(f"語音處理出錯: {e}")
        line_bot_api.push_message(user_id, TextSendMessage(text="❌ 語音辨識失敗，請確認錄音品質後再試一次。"))

# 6. 整合 AI 處理邏輯 (統一處理文字與語音轉出的文字)
# 3. 傳送訊息
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"【請切換至：{tag}】學生的話：{text}"
        )
        
        # 4. 執行 Run (這是最耗時的地方)
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        
        # 輪詢狀態
        start_time = time.time()
        while run.status in ['queued', 'in_progress']:
            # 如果跑超過 8 秒，手動停止避免 Vercel 崩潰，這能讓你看到錯誤
            if time.time() - start_time > 8:
                print("⚠️ AI 思考太久，可能觸發 Vercel 10s 限制")
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text="⏳ AI 還在思考中，請稍後再問我一次，我就能把剛才的答案給你！"))

    except Exception as e:
        print(f"❌ AI 處理發生崩潰: {str(e)}")
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 系統錯誤: {str(e)[:50]}"))
    # 根據模式決定標籤
    tag = "[中醫專家模式]"
    if mode == "speaking": tag = "[口說教練模式]"
    elif mode == "writing": tag = "[寫作顧問模式]"
    
    # 1. 建立 Thread (為了簡化，每次都建新的或抓舊的，這裡先示範建新的)
    thread = client.beta.threads.create()
    
    # 2. 傳送訊息
    client.beta.threads.messages.create(
    thread_id=thread.id,
    role="user",
    content=f"【請切換至以下身分：{tag}】\n\n學生的訊息內容如下：{text}"
)
    
    # 3. 執行 Run
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id
    )
    
    # 4. 等待結果 (Vercel 有時間限制，這裡用簡單的輪詢)
    while run.status in ['queued', 'in_progress']:
        time.sleep(1)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
    
    # 5. 取得回答並回傳
    if run.status == 'completed':
        messages = client.beta.threads.messages.list(thread_id=thread.id)
        ai_reply = messages.data[0].content[0].text.value
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))

# --- AI 處理核心函數 ---
def process_ai_request(event, user_id, text, is_voice=False):
    try:
        # 模式讀取
        mode_val = redis.get(f"user_mode:{user_id}")
        mode = mode_val.decode('utf-8') if hasattr(mode_val, 'decode') else str(mode_val or "tcm")

        # Thread ID 讀取
        t_id = redis.get(f"user_thread:{user_id}")
        thread_id = t_id.decode('utf-8') if hasattr(t_id, 'decode') else (str(t_id) if t_id and t_id != "None" else None)
        if not thread_id:
            new_thread = client.beta.threads.create()
            thread_id = new_thread.id
            redis.set(f"user_thread:{user_id}", thread_id)
        
        # 3. 將使用者的話傳送給 OpenAI Assistant
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=text
        )
        
        # 4. 啟動 AI 回答 (Run)
        run = client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=assistant_id
        )
        
        # 5. 等待 AI 回答完畢 (輪詢)
        start_time = time.time()
        while run.status in ['queued', 'in_progress']:
            # Vercel 免費版 10 秒限制：若跑 8.5 秒還沒好就先結束，避免系統直接崩潰
            if time.time() - start_time > 8.5:
                break
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id)
            ai_reply = messages.data[0].content[0].text.value
            # 使用 push_message 回傳真正的答案
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_reply))
        else:
            line_bot_api.push_message(user_id, TextSendMessage(text="⏳ AI 仍在處理中，請稍候 5 秒再傳送任何文字，我就能顯示剛才的分析結果！"))

    except Exception as e:
        import traceback
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        # 萬一出錯，至少讓你知道是什麼原因
        line_bot_api.push_message(user_id, TextSendMessage(text=f"❌ 處理失敗：{str(e)[:50]}"))