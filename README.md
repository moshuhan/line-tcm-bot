# LINE TCM AI Bot (中醫課程助教)

這是一個基於 Node.js 的 LINE Bot 後端程式，專為中醫課程設計。它使用 OpenAI Assistant API 來回答學生的問題，並扮演專業助教的角色。

## 功能特色

- **LINE Messaging API 整合**：接收並回覆使用者訊息。
- **OpenAI Assistant API**：使用強大的 AI 模型與知識庫進行回答。
- **角色設定**：預設為「中醫學院助教」，語氣專業親切。
- **Serverless Ready**：專為 Vercel 佈署設計。
- **安全性**：包含 LINE 簽章驗證 (X-Line-Signature)。

## 專案結構

```
.
├── api
│   └── webhook.js       # Vercel Serverless Function 入口點
├── services
│   ├── line.js          # LINE API 處理邏輯
│   ├── openai.js        # OpenAI Assistant API 處理邏輯
│   └── state.js         # 對話狀態管理 (目前為 Mock，需自行對接資料庫)
├── .env.example         # 環境變數範例
├── package.json         # 專案依賴設定
└── README.md            # 說明文件
```

## 快速開始

### 1. 安裝依賴

```bash
npm install
```

### 2. 設定環境變數

```bash
cp .env.example .env
```

- `LINE_CHANNEL_ACCESS_TOKEN`: LINE Developers Console 取得。
- `LINE_CHANNEL_SECRET`: LINE Developers Console 取得。
- `OPENAI_API_KEY`: OpenAI Platform 取得。
- `OPENAI_ASSISTANT_ID`: OpenAI Assistants 頁面建立 Assistant 後取得。

### 3. 本地開發

```bash
npm run dev
```

伺服器將啟動於 `http://localhost:3000`。
您可以使用 ngrok 將本地端口暴露到公網，以便在 LINE Developers Console 中設定 Webhook URL (例如: `https://xxxx.ngrok.io/api/webhook`)。

## Vercel 佈署步驟（手機端測試）

1. 程式已推送至 **GitHub**（本專案使用 Python + `api/index.py` 作為 Vercel 入口）。
2. 登入 [Vercel](https://vercel.com) → **Add New Project** → 匯入 **GitHub** 上的 `line-tcm-bot` 倉庫。
3. **Environment Variables** 請設定：
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
   - `OPENAI_API_KEY`
   - `OPENAI_ASSISTANT_ID`
   - `KV_REST_API_URL`（Upstash Redis）
   - `KV_REST_API_TOKEN`（Upstash Redis）
4. 點擊 **Deploy**，等待佈署完成。
5. 複製 Vercel 提供的網域，例如：`https://your-project.vercel.app`。
6. 到 **LINE Developers Console** → 您的 Channel → Messaging API：
   - **Webhook URL** 設為：`https://your-project.vercel.app/callback`
   - 開啟 **Use webhook**。
7. 用手機加入 Bot 為好友並傳送訊息，即可確認執行結果。

## 重要注意事項

### 對話記憶 (Thread Persistence)

由於 Vercel Serverless Functions 是無狀態的 (Stateless)，本專案目前的 `services/state.js` 使用記憶體來暫存對話 ID。這意味著：
- 當函數重新啟動 (Cold Start) 時，對話記憶會消失。
- 使用者可能會遇到「新對話」的情況。

**建議改進**：
請修改 `services/state.js`，將 `getThreadId` 和 `saveThreadId` 方法對接到持久化資料庫 (如 MongoDB, Redis, Vercel KV 或 Supabase)。

```javascript
// services/state.js 範例
async function getThreadId(userId) {
  // return await redis.get(userId);
}
```
