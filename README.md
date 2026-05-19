# LINE TCM AI Bot（中醫課程助教）

以 **Python（Flask）+ OpenAI** 為主的 LINE Bot，專為中醫課程設計。部署於 **Railway**，使用 Redis 儲存狀態、MongoDB 記錄研究資料，具備語義向量搜尋、語音教練、AI 動態測驗、主動複習與每週學習報告。

---

## 功能特色

- **LINE Messaging API**：接收／回覆文字、語音、Postback（Rich Menu）。
- **OpenAI**：gpt-4o-mini（中醫問答、測驗、複習筆記）、Whisper（語音轉文字）、TTS（示範發音）、text-embedding-3-small（語義向量搜尋）。
- **語義向量 RAG**（`data/tcm_master_knowledge.json` + `data/tcm_embeddings.json`）：
  - 啟動時將預計算向量載入記憶體，每次問答只多一次 embedding API 呼叫（~100–200ms）。
  - 以 cosine similarity 找出最相關的 Top-3 知識點作為 context，解決關鍵字比對 miss 的問題。
  - embeddings 檔案不存在時自動 fallback 至全量 context，不影響服務。
- **時間感知課綱**（`config/syllabus.json` + `api/syllabus.py`）：
  - 與中醫／醫療相關問題皆可依知識庫或學術資源回答，不鎖定課程進度。
  - 精準過濾：僅對與中醫／醫療學術完全無關的內容回覆「本機器人僅供學業使用」。
- **語音教練**：Azure Cognitive Services Pronunciation Assessment 分析實際發音，回傳整體分數、準確度、流暢度、完整度、語調（各 0–100）與需加強的字；分數 ≥ 80 視為通過，送出下一句；未設定 Azure 金鑰時自動 fallback 至 Whisper + GPT 文字評估。每月免費額度 5 小時。
- **AI 動態測驗**：依 AI 回覆內容即時出題（MCQ），不使用靜態題庫；回覆後由 GPT 判斷並記錄弱項。
- **主動複習**：若某學生在特定領域表現不佳達門檻，主動詢問是否整理複習筆記。
- **每週學習報告（Cron）**：彙整所有使用者提問，統計前十大困惑觀念，產出 PDF 並寄送報告。

---

## 專案結構

```
.
├── api/
│   ├── index.py            # Railway 入口（Flask）：Webhook、語音、測驗、複習
│   ├── syllabus.py         # 時間感知檢索與課綱（離題過濾、RAG 說明）
│   ├── learning.py         # 問題記錄、AI 動態測驗、弱項、複習筆記
│   ├── research_logging.py # 研究資料記錄（MongoDB）
│   ├── weekly_report.py    # 每週報告：概念統計、PDF、SMTP
│   └── webhook.js          # Node 版 Webhook（備用，目前未作主要入口）
├── config/
│   ├── syllabus.json       # 課綱日期、關鍵字
│   └── syllabus_full.json  # 完整課綱（含 start_time/end_time/has_handout）
├── data/
│   ├── tcm_master_knowledge.json  # TCM 知識庫
│   ├── tcm_embeddings.json        # 預計算向量（由 generate_embeddings.py 產生）
│   └── ai_weekly_summary.json     # AI 預處理的每週重點
├── scripts/
│   ├── generate_embeddings.py  # 一次性：為知識庫產生 embedding 向量
│   ├── setup_rich_menu.js      # Rich Menu 設定（Node）
│   └── run_local.ps1           # 本地啟動腳本（Windows）
├── services/                   # Node 用（line / openai / state），備用
├── docs/
│   └── ARCHITECTURE.md         # 技術架構概覽
├── tests/
│   ├── test_is_off_topic.py    # 離題過濾邏輯測試
│   └── test_tcm_latency.py     # TCM 知識庫載入延遲測試
├── benchmark.py                # 模型比較（our system vs GPT-4o vs Gemini）
├── Procfile                    # Railway 部署指令（gunicorn + gevent）
├── requirements.txt
├── .env.example
└── README.md
```

---

## 環境變數

複製 `.env.example` 為 `.env` 並填入：

| 變數 | 說明 |
|------|------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developers Console |
| `LINE_CHANNEL_SECRET` | LINE Developers Console |
| `OPENAI_API_KEY` | OpenAI API Key |
| `OPENAI_ASSISTANT_ID` | OpenAI Assistants 建立的助理 ID |
| `REDIS_URL` | Railway Redis 連線字串 |
| `MONGO_URL` | Railway MongoDB 連線字串 |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | TTS 語音檔雲端儲存 |
| `AZURE_SPEECH_KEY` | Azure Speech Service 金鑰（口說教練發音評估用） |
| `AZURE_SPEECH_REGION` | Azure 部署區域，例如 `eastasia`（口說教練發音評估用） |
| `REPORT_EMAIL` | 每週 PDF 報告寄送信箱 |
| `CRON_SECRET` | 保護 /api/cron/weekly 的密鑰 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | 每週報告 SMTP |

---

## 語義向量 RAG 初始化（首次或知識庫更新後執行）

每次更新 `data/tcm_master_knowledge.json` 後，需重新產生向量：

```bash
python scripts/generate_embeddings.py
```

執行完成後將 `data/tcm_embeddings.json` 一同 commit 並部署，Railway 啟動後會自動載入至記憶體。

---

## 本地開發與快速測試

### 1. 啟動本地伺服器

```bash
pip install -r requirements.txt
# 確保 .env 已設定

python -m api.index
# 或
python test_local.py
```

Flask 會啟動於 `http://0.0.0.0:5000`。

### 2. 用 ngrok 暴露本機

```bash
ngrok http 5000
```

### 3. 設定 LINE Webhook

到 **LINE Developers Console** → Messaging API → Webhook URL 設為：

```
https://你的ngrok網址/callback
```

---

## Railway 部署

1. 將本專案推送到 **GitHub**。
2. 登入 [Railway](https://railway.app) → **New Project** → 從 GitHub 匯入。
3. 新增 Redis 與 MongoDB plugin，複製連線字串填入環境變數。
4. 設定所有環境變數後，Railway 會自動依 `Procfile` 啟動：
   ```
   gunicorn --worker-class gevent --workers 2 --timeout 120 --bind 0.0.0.0:$PORT api.index:app
   ```
5. 部署完成後，到 **LINE Developers Console** 將 Webhook URL 設為 Railway 提供的網域：
   ```
   https://你的railway網址/callback
   ```

---

## 語音教練測試（口說練習）

1. 在 LINE 切換至「口說練習」模式。
2. 傳送語音訊息（.m4a）；Bot 回覆辨識結果並分析發音與文法。
3. 需修正：回饋文字 ＋ TTS 示範正確發音；正確：鼓勵語 ＋ Quick Reply。

---

## AI 動態測驗與主動複習

- **測驗**：中醫問答模式下，每次 AI 回覆後由 GPT 依回覆內容即時生成 MCQ 小測驗（不使用靜態題庫）。學生以 A/B/C 回覆後批改，並記錄弱項。
- **弱項追蹤**：不論使用中文或英文，只要答錯就記錄該題目的類別到 Redis（`user_weak:{user_id}`）。
- **主動複習**：某領域累計 ≥ 1 次答錯，且距離上次詢問超過 1 天（`REVIEW_ASK_COOLDOWN_DAYS`），Bot 在下一次問答結束後主動推播「需要幫你整理複習筆記嗎？」【要 / 不要複習筆記】。冷卻天數可在 `api/learning.py` 的 `REVIEW_ASK_COOLDOWN_DAYS` 調整。
- **個人化複習筆記**：點「要複習筆記」後，Bot 查詢 MongoDB 中該使用者在該類別答錯的測驗紀錄與相關問答，餵給 GPT 產生針對個人弱點的複習筆記（非通用知識點）。MongoDB 不可用時自動 fallback 至通用版本。

---

## 技術說明

- **入口**：Railway 依 `Procfile` 執行 gunicorn，所有請求由 `api/index.py`（Flask）處理。
- **狀態儲存**：對話模式、測驗、弱項、問題記錄存於 **Redis**；研究資料（互動紀錄、測驗結果）存於 **MongoDB**。
- **RAG 流程**：`_semantic_search()` 在記憶體中對預計算向量做 cosine similarity，取 Top-3 知識點組成 context 送給 gpt-4o-mini。
- **架構細節**：見 `docs/ARCHITECTURE.md`。

---

## 授權與注意事項

- 本專案供教學使用；涉及中醫內容之回覆會附加「僅供教學用途，不具醫療建議」聲明。
- 請勿將 `.env`、API Key 或 SMTP 密碼提交至版控。

---

## 最近更新 (2026-05-20)

- **口說模式 AI 回覆記錄至 MongoDB**：語音辨識完成後寫入 `interactions`（`mode: "Speaking"`）的紀錄，原本 `answer` 欄位為 `null`。現在 AI 透過 Assistant API 產生回覆後，會以 `update_speaking_answer` 更新該筆紀錄的 `answer` 欄位，完整保留口說問答的問與答。

---

## 最近更新 (2026-05-17)

- **修正弱項追蹤 Bug**：`record_weak_category` 原本只在中文路徑呼叫，英文模式答錯不會被記錄。現已移至語言判斷之外，答錯即記錄，不分語言。
- **降低主動複習門檻**：答錯次數門檻從 2 次降為 1 次（`min_count=1`）；冷卻期從 7 天縮短為 1 天（`REVIEW_ASK_COOLDOWN_DAYS=1`），讓功能實際可被觸發。
- **冷卻期改用常數**：`_maybe_send_review_prompt` 的冷卻判斷改為讀取 `REVIEW_ASK_COOLDOWN_DAYS`，往後只需改 `api/learning.py` 一個地方。
- **個人化複習筆記**：新增 `generate_personalized_review_note`（`api/research_logging.py`），查詢 MongoDB 中該使用者在該弱項類別答錯的測驗紀錄與相關問答，產生針對個人實際弱點的複習筆記；MongoDB 不可用時自動 fallback 通用版本。

---

- **語義向量 RAG**：以 `text-embedding-3-small` 預計算知識庫向量，取代原本的關鍵字比對。問答時對使用者問題做 embedding，cosine similarity 找出 Top-3 最相關知識點作為 context，解決「虎口 vs 合谷」等換說法就找不到的問題。向量檔在 Railway 啟動後載入記憶體，每次問答僅多 ~100–200ms。
- **移除時間解鎖小測驗**：靜態題庫（tcm_quiz_all.json）與時間解鎖機制已移除，改採 AI 動態出題，不限制學生的學習範圍。
- **部署遷移至 Railway**：從 Vercel serverless 遷移至 Railway（gunicorn + gevent），解決冷啟動延遲問題，記憶體 cache 持久有效。
