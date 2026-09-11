
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

## 產品發展規劃（教育化轉型 / 大學光中醫AI基金申請書用）

> 本節記錄與顏老師討論後的模組定位與階段規劃，尚未實作，僅供之後撰寫申請書與排開發優先序參考。內文若與上方「功能特色」有出入，以上方為現況、本節為規劃方向。

### 整體定位

整個系統要從「通用中醫聊天機器人」收斂成 **FOR 中醫教育** 的專用系統，三個模組各自有「保留的核心」與「要新開發的延伸方向」。

### 助教 / QA 模組

- **核心（保留不變）**：回答學生的中醫問題，沿用現有的語義向量 RAG。
- **要拿掉**：現有「每次回答後自動附加一題測驗」的功能（內部原本包裝為「蘇格拉底式提問」，但實際只是即時生成單題 MCQ，非結構化教學設計，予以移除）。
- **延伸方向：國考學習系統**——從「回答中醫問題」這個基礎能力延伸開發，不是從舊的附加測驗長出來。目標是讓 AI 像真正的老師一樣出題：
  - 教科書每一頁都要平均出題，建立完整覆蓋的題庫。
  - 依考試委員希望的難度＋章節比重，加權挑選題數，組成模擬試題系統。
  - 觸發方式雙軌：學生問問題時可延伸出對應章節的練習題；也可獨立進入刷題系統，不必透過 QA 觸發。
- **現有基礎可延伸利用**：`api/learning.py` 的 `generate_mcq_quiz()`（單題即時生成雛形）、`benchmark.py` + `benchmark_questions.json`（20 題台灣中醫師執照考古題測試，`results/summary_*.csv`：目前「我們的系統」70% 正確率，反而低於裸 GPT-4o 的 85%，顯示現有的臨時抓 context 出題機制還不足以支撐國考題，需要投入資源做正規題庫）。
- **市場競品**：考古豹 CougarBot（最接近，已有智慧測驗、弱點追蹤、每題可跟 AI 即時討論）、智慧題庫 atquiz.com、高點題庫網／高點醫護、醫師國考題庫 App、中國市場執業醫師考試題庫 App——多數屬傳統題庫模式或 AI 層次不明，差異化要打在「真正個人化組卷＋理解導向」。

### Speaking Coach 口說教練

- **新定位：NPC 對話式口說教練**（取代現有的發音矯正邏輯）。
  - 類似遊戲 NPC 的互動對話夥伴（理想為 3D 人物即時對話，而非對著錄音室說話）。
  - 對話中即時顯示逐字稿，並標示中醫專有名詞的中文翻譯。
  - 使用者卡住時可先講中文，系統偵測後標示：文法錯誤／不會的中醫醫學英文正確說法／**中醫邏輯或臨床推理上的錯誤**（不只語言層面）。
  - 對話結束後提供本次學習重點回顧。
- **延伸方向**：接入階段式 OSCE（臨床技能測驗）教學模組。
- **現有基礎可延伸利用**：Azure Pronunciation Assessment 真人發音評分（準確度／流暢度／完整度／語調）、Whisper＋GPT fallback、TTS 示範語音、依上一句動態生成下一句練習句。
- **缺口**：現有機制是「單句錄音評分」，離「連續對話」還差一整套對話引擎；NPC／角色化呈現、中醫臨床推理錯誤偵測都要從零開始。
- **市場競品**：TEEMI-Bot（教育部／台師大，中國醫大學生可免費申請，已有 General／ESP 兩種對話模式）、Cool English（教育部＋台師大＋Azure OpenAI，全球約 150 萬註冊用戶）——都是免費、通用型平台，差異化必須落在「中醫專業深度＋臨床邏輯校正」與「NPC 對話真實感」，單純做發音／流暢度矯正等於重複免費資源。

### Writing Coach 寫作教練

- **新定位**：
  1. 通俗中醫文章、衛教文章寫作。
  2. 論文寫作——(1) 架構參考顏老師的內容／框架 (2) 附教學課程影片。
- **現有基礎**：純語法／用詞／修辭批改（chat.completions，非結構化題目）。
- **缺口**：缺乏明確寫作題目／目標導致使用率低；視覺呈現差（純聊天室黑字，需改網頁形式搭配排版強調重點）；衛教文／論文寫作的分眾題型與教學影片都還沒開發。
- **市場競品**：TEEMI（教育部／台師大，中國醫大學生可免費申請，6 題寫作題型＋CEFR 分級＋文法修正建議）——一般英文寫作層面已被免費資源覆蓋，這塊要嘛做出中醫寫作特化深度，要嘛非這次申請的主打重點。

### 六階段開發路線圖（多年願景，非單次申請承諾範圍）

整體原則：介面遊戲化。

1. **階段一**：中醫基礎學科題庫
2. **階段二**：婦、兒、內、針傷等臨床學科
3. **階段三**：整合成台灣中醫師國考系統（分病歷、診斷、用藥、取穴等不同題型攻克）
4. **階段四**：延伸到 OSCE 應用系統完整開發
5. **階段五**：開發含美國針灸師、越南、新加坡等各國學習系統
6. **階段六**：公司合作與技術轉移

> 顏老師同時也在指導撰寫論文，準備投期刊與國際研討會——可作為團隊實力佐證，但申請書明訂不以論文發表為主要評量依據。

### 待確認事項

- 這次「一年期／100 萬」的申請要承諾做到六階段路線圖的哪個範圍，尚未最終拍板。初步討論方向：**階段一（中醫基礎學科題庫）＋ Speaking Coach NPC 原型**，Writing Coach 列為次要產出；階段二以後寫成後續三年願景，不當這次的承諾範圍。

### 平台策略：LIFF 先行，架構留一手給未來獨立 App

- **短期（這次申請／階段一～二）**：不整套拉出 LINE，改用 **LIFF（LINE Front-end Framework）**——Rich Menu 點下去在 LINE App 內開一個完整網頁，可以做 3D NPC 對話、組卷系統的遊戲化介面、Writing 的排版強調，不受聊天泡泡限制，同時沿用現有 LINE 身份、Rich Menu 導流、Redis／Mongo 後端。開發量最小，適合一年期的 Demo 規模。
- **長期（階段五：美國針灸師、越南、新加坡）**：國外使用者不在 LINE 生態圈，屆時再把核心邏輯抽成獨立 App／跨平台網站，LIFF 版本自然變成「台灣／日本專屬入口」，獨立 App 是「國際版入口」，兩者並存，不是取代關係。
- **現在就該做的準備**：業務邏輯不要寫死在 LINE webhook 裡，讓 LIFF 網頁跟未來的獨立 App 都能呼叫同一組 API，屆時階段五只是多寫一個前端，後端不用大改。

#### 考題 LIFF：目前進度

後端與前端已經動工，架構上跟現有 LINE webhook 業務邏輯完全分開（沒有共用 `line_bot_api.push_message`，符合上面「業務邏輯不要寫死在 LINE webhook 裡」的原則）：

- **`api/exam_quiz.py`**：題庫載入（`data/exam_questions.json`，目前是 20 題考古題種子資料，比照 `tcm_master_knowledge.json` 的記憶體快取模式）、交卷評分、錯題本 CRUD（寫入 MongoDB `exam_wrong_questions` / `exam_sessions`）。
- **`api/liff_auth.py`**：LIFF ID Token 驗證（呼叫 LINE 官方 `oauth2/v2.1/verify` 端點，不信任前端自稱的 userId）。
- **`api/index.py`** 新增路由（都掛在 `/api/liff/quiz/*`，全部要求 LIFF ID Token）：`categories`、`questions`（依章節）、`questions/<id>`（按需查答案，給「顯示答案」開關用）、`submit`（交卷評分＋自動存錯題）、`bookmark`（手動標記／取消）、`wrong-questions`（GET 清單／DELETE 移除）、`explain`（AI 詳解＋追問，重用既有 `_semantic_search` RAG，純函式不寫 LINE）。
- **`api/templates/liff_quiz.html`**：前端頁面，涵蓋線框圖三頁（考題頁／評分＋詳解頁／錯題本），用 localStorage 記住上次選的章節。
- 後端邏輯已用 Flask test client＋直接呼叫函式測試過（題庫載入、分類篩選、評分計算、AI 詳解都實測跑過一次），MongoDB 因本機連不到 Railway internal 網路無法實測寫入，但已確認 `mongo_db=None` 時的 fallback 不會噴例外。

**LIFF ID 已申請並填好（2026-09-11）：**
三個 LIFF App 都掛在同一個 Messaging API Channel 下，共用同一組 `LIFF_CHANNEL_ID=2011558628`。

| 頁面 | LIFF ID | Endpoint URL |
|---|---|---|
| 考題 | `2011558628-8li3MSJw` | `/liff/quiz` |
| 口說 | `2011558628-VDHP3SsC` | `/liff/speaking` |
| 寫作 | `2011558628-VKytwC2e` | `/liff/writing` |

三個 `.html` 檔案裡的 `LIFF_ID` 常數、本機 `.env` 的 `LIFF_CHANNEL_ID` 都已經填好正式值。

**Rich Menu 已換版（2026-09-11）：**
原本 4 個聊天模式切換鈕（中醫問答/口說練習/寫作修訂/課務查詢）已整個替換成 3 個 LIFF 入口，
使用者現在點 Rich Menu 直接開對應的 LIFF 頁面，不再是純聊天室模式切換。

- 圖：`assets/rich_menu_background_v2.jpg`（原始設計稿 `rich_menu_background_v2.png` 1.94MB 超過 LINE
  1MB 上限，已轉存 JPEG quality=95 壓到 377KB，肉眼看不出畫質差異）
- 腳本：`scripts/setup_rich_menu.js`，3 欄等寬（2148x732 圖，每欄 716px），`type: 'uri'` 連到
  `line://app/{LIFF-ID}`，已用真實 `LINE_CHANNEL_ACCESS_TOKEN` 實際執行過，確認新選單
  `richmenu-686b2863dd29e0db77e16a112383e491` 已是目前帳號的預設 Rich Menu。
- 舊的 Rich Menu 沒有被刪除，只是不再是預設，要換回去還救得回來。
- 帳號底下另外還有 13 個過去測試留下的 `TCM_EMI_Menu`（都是舊的 2500x843 版型），沒有造成問題，
  之後想清可以用 `client.deleteRichMenu(richMenuId)` 自行清理，這次沒有動它們。

**還缺、要你自己在後台補的：**
1. **Railway 環境變數加 `LIFF_CHANNEL_ID=2011558628`**（本機 `.env` 不會自動同步到 Railway，這是兩個獨立設定；沒加的話 `verify_liff_id_token` 會一律驗證失敗，所有 `/api/liff/*` 都回 401）。
2. 確認 LINE Developers Console 裡三個 LIFF App 的 Endpoint URL 對應正確（尤其「寫作」那個中途調整過 ID，容易對錯）。
4. 題庫目前只有 20 題種子資料，「依年度」篩選還沒做（缺年度/級別 metadata），之後題庫擴充時要在 `data/exam_questions.json` 補齊相關欄位再回來接這段邏輯。

#### 口說 LIFF：目前進度

語音對話走 **OpenAI Realtime API**，前端瀏覽器直接用 **WebRTC** 連到 OpenAI（我們的伺服器不中繼音訊，延遲最低）；架構同樣跟 LINE webhook 業務邏輯完全分開。

- **`api/speaking_liff.py`**：兩種情境（`clinical` 臨床衛教／患者、`academic` 學術討論／教授）的角色 instructions 與提示框內容（話題／詞彙，MVP 先用固定內容，不用 AI 動態產生）、ephemeral client secret 換發（`mint_ephemeral_session`，主 API Key 絕不下發給瀏覽器）、逐輪錯誤標註（`annotate_errors`，回傳原句、錯誤片段用「«」「»」包住）、角色台詞即時中譯（`translate_to_zh`）、結算頁摘要（`build_session_summary`，只回傳「有錯誤」的使用者發言，原句/修正句成對）。
- **`api/index.py`** 新增路由（都掛在 `/api/liff/speaking/*`，全部要求 LIFF ID Token）：`scenarios`（主畫面兩個主題按鈕資料）、`session`（開始對話，換發 ephemeral secret）、`translate`、`analyze-turn`、`summary`。
- **`api/templates/liff_speaking.html`**：三頁線框圖（主畫面／對話畫面／結算頁）。對話畫面先做「靜態版」（emoji 頭像佔位，wireframe 說之後才做 canvas/3D 版）；雙語字幕、錯誤標色、可收合提示框、麥克風開關（靜音／取消靜音，搭配 Realtime API 的 server VAD 自動偵測語音起訖）都已實作。
- **刻意不存 MongoDB／Redis**：整段對話逐字稿只存在瀏覽器記憶體這次 session，符合「口說練習資料只記錄到結束對話之前，離開結算頁就全部清空」——正常掛斷才會呼叫 `summary` 端點產生結算內容；用左上「← 回主頁」中途離開則直接斷線、不產生摘要。
- **已實測**（Flask test client + 直接呼叫函式，都用真實 API Key 打過一次）：頁面正常回傳、未帶 Token 正確擋 401、情境資料正確、**ephemeral session 換發成功**、錯誤標註正確抓出文法錯誤（例如漏掉冠詞/be動詞）、中譯正確、結算頁摘要正確整理出兩句話的錯誤與修正版。

**還沒測、也測不了的部分（需要你之後在真機／瀏覽器上驗證）：**
1. **實際的 WebRTC 語音對話**——這個環境沒有瀏覽器跟麥克風，沒辦法測試真人講話、AI 語音回覆、即時字幕这整條路徑。
2. **Realtime API 事件名稱**：`liff_speaking.html` 裡監聽的事件類型（如 `conversation.item.input_audio_transcription.completed`、`response.audio_transcript.delta/.done`）是依我目前所知的 Realtime API 事件命名寫的，但這個 API 版本更新較快，實際名稱可能有出入。程式碼裡已經把所有收到的事件都 `console.log` 出來，第一次真機測試時請打開瀏覽器 DevTools 的 Console，把實際看到的事件名稱回報給我，我再對照調整。
3. `LIFF_ID`（`2011558628-VDHP3SsC`）已填好，`LIFF_CHANNEL_ID` 已寫入本機 `.env`——**但 Railway 後台的環境變數、Rich Menu 入口都還沒接**，見上方「考題 LIFF」小節的統整表格。
4. 麥克風權限：LINE 內建瀏覽器（LIFF 執行環境）能不能正常跳出麥克風授權、iOS/Android 行為是否一致，也需要真機測過才能確定。

#### 寫作 LIFF：目前進度

決定要做「Grammarly 風格即時逐字標註」（開發量較大的那個選項）。核心技術問題是一般
`<textarea>` 沒辦法在輸入同時插入彩色標註，解法是業界常見的「隱形 textarea 疊在有顏色的
顯示層上面」：底層 `#highlight-layer` 顯示帶顏色標註的文字，`#editor` 這個 textarea 文字
是透明的（只看得到游標），兩層用完全一致的字型/行高/padding 對齊，使用者感覺是直接在有顏色
標註的文字上打字。

- **`api/writing_liff.py`**：題目/範本載入（`data/writing_prompts.json`，「自由寫作」＋兩篇
  中醫中文段落的中譯英摘要練習，跟考題 LIFF 一樣是佔位種子資料）、即時標註（`annotate_realtime`，
  沿用跟口說 LIFF 一樣的「錯誤片段用«»包住」慣例）、送出批改（`full_review`，回傳 0-100 評分＋
  細項＋修正版＋說明）、儲存練習紀錄（`save_practice`，寫入 MongoDB `writing_practice`——**跟口說
  LIFF 不同，寫作明確要求要保留紀錄**，不是 session 結束就清空）。
- **`api/index.py`** 新增路由（都掛在 `/api/liff/writing/*`）：`topics`、`check`（即時標註，前端
  debounce 0.9 秒才呼叫）、`review`（送出批改）、`save`（對應線框圖「儲存練習內容」傳
  `kind=draft`／「一鍵同意/確認儲存」傳 `kind=reviewed`）。
- **`api/templates/liff_writing.html`**：主畫面（題目清單）＋寫作畫面（隱形 textarea 疊層編輯器、
  即時標色、儲存/送出批改兩顆按鈕、批改結果面板含一鍵採用並儲存）。
- **已知簡化、非正式版本**：
  1. 「評分標準」是我暫時寫的 rubric（內容/學術語域/文法/結構各 25 分），**不是**跟 TEEMI 做出
     區隔用的正式評分標準——等有實際規準文件再替換 `writing_liff.py` 裡的 `_REVIEW_SYSTEM_PROMPT`。
  2. 「中譯英摘要」題目只有 2 篇我暫寫的短段落，之後要擴充直接加進 `data/writing_prompts.json`。
  3. 標色對齊有個已知風險：如果 AI 回傳的標註文字沒有嚴格保留原文字元（只是包 «»、不改動其他
     內容），標色層跟 textarea 就會對不齊。已經加了防呆——前端會檢查「去掉 «» 後是否跟目前輸入
     內容完全一致」，不一致就跳過這次標色更新，不會顯示錯位的標註，但代價是那一輪偶爾不會標色。
- **已實測**：頁面回傳、401 擋未授權、題目資料正確、**即時標註實測抓到「TCM believe」「human
  body are」兩個文法錯誤，且去除 «» 後跟原文逐字元一致（標色對齊防呆機制驗證成功）**、送出批改
  正確產出評分/修正版/說明。過程中抓到一個真的 bug：`explanation` 欄位有時候 AI 會回傳 list 而
  不是字串（因為 prompt 要求「條列」，模型直接回傳 JSON 陣列），原本會讓 `.strip()` 噴例外——已
  修成用 `_as_text()` 統一轉字串處理，並在 prompt 裡加強講清楚要單一字串。
- **沒測、要真機驗證**：疊層編輯器在 LINE 內建瀏覽器（尤其 iOS）的實際對齊效果、手機輸入法
  （注音/拼音組字中）搭配 debounce 會不會誤觸發標註。`LIFF_ID`（`2011558628-VKytwC2e`）已填好，
  其餘 Railway 環境變數／Rich Menu 入口見上方「考題 LIFF」小節的統整表格。

#### 現況盤點：業務邏輯與 LINE 傳訊耦合程度

`api/index.py` 全檔案有 **80 處**直接呼叫 `line_bot_api.push_message` / `reply_message`，而且大多數寫在「業務邏輯函式本體內」，不是集中在 webhook handler 那一層：

- `_tcm_openai_reply()`（QA／RAG 語意檢索＋AI 回覆）：邏輯跑完直接在函式內 push/reply，沒有把結果回傳出來。
- `_process_assistant_sync()`（Speaking／Writing 的 Responses API 呼叫）：同樣模式。
- `_process_voice_sync()`（語音處理）：辨識結果、發音回饋、TTS 示範、下一句練習，每個階段都各自插入 push_message，耦合最深。
- `_revision_handler()`（寫作批改）：同樣模式。

結果是：現在如果要讓 LIFF 網頁拿到「一樣的 AI 回覆」，沒辦法直接呼叫這些函式——它們的「輸出」是「發一則 LINE 訊息」，不是「回傳一個結果」。

#### 需要的重構（建議在寫 LIFF 之前，或跟 LIFF 一起做）

1. **業務邏輯函式改成「回傳結構化結果」，不要自己發 LINE 訊息**：例如 `_tcm_openai_reply` 應該回傳 `{"text": ..., "sources": ...}` 之類的結構，由呼叫端決定要 push 到 LINE、還是包成 JSON 回給 LIFF 網頁。
2. **加一層「傳訊 adapter」**：LINE webhook handler 拿到業務邏輯的回傳結果後，自己組 LINE 訊息格式（Quick Reply、Flex Message）並發送；LIFF／未來獨立 App 的 API route 則把同一個結果包成 JSON 回傳給前端。兩邊共用同一組業務邏輯，只有「怎麼呈現」不同。
3. **新增給 LIFF 用的 API 路由**：目前的內部路由（`/api/process-text-async` 等）是設計給「伺服器對伺服器」呼叫的，用 `CRON_SECRET` 共用密鑰驗證，不能直接讓瀏覽器呼叫。要給 LIFF 網頁用，需新增一組路由，改用 **LIFF ID Token**（前端 `liff.getIDToken()` 取得，伺服器端呼叫 LINE 的 `https://api.line.me/oauth2/v2.1/verify` 驗證）做身份驗證。目前 `requirements.txt` 還沒有 CORS／JWT 相關套件，要用到時需另外加。
4. **使用者識別保持彈性、不用急著改**：`user_id` 目前全部直接沿用 LINE 的 `event.source.user_id`，Redis／Mongo 的 key 也都是這個格式。LIFF 階段不受影響——LIFF 拿到的 LINE userId 跟 webhook event 的 user_id 是同一組 ID。等階段五要接非 LINE 使用者，業務邏輯只要繼續吃「一個字串 user_id」就好，不用假設它一定是 LINE 格式，現有資料結構已經符合這個條件。
5. **音訊／TTS 交付已經可重用**：`_generate_tts_and_store()` 產出的是 Cloudinary URL 或 `/audio/<token>` 這種公開網址，本來就不是 LINE 專屬格式，LIFF 網頁或未來 App 都能直接播放，這塊不用重做。
6. **組卷引擎（國考題庫系統）、`generate_mcq_quiz()` 等純業務邏輯本來就沒有 LINE 耦合**，是目前最乾淨、可以直接當未來 API 基礎的部分。

> 建議順序：先做上面第 1、2 點（拆分業務邏輯與傳訊），LINE 現有功能行為不變；之後不論加 LIFF 還是加獨立 App，都是「多寫一個 adapter」而不是「重寫一次邏輯」，才不會卡到階段五。

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

- **修正 Azure Pronunciation Assessment 無法取得評分的問題**：`_assess_pronunciation` 原本將 Content-Type 設為 `audio/x-m4a`（非標準 MIME），Azure REST API 不接受，導致 `RecognitionStatus` 非 `"Success"`，靜默 fallback 至 Whisper + GPT 評估，各項發音分數（準確度、流暢度、完整度、語調）都無法輸出。改為 `audio/mp4`（LINE M4A 音訊的正式 MIME type）後 Azure 可正確識別。同時加入 `RecognitionStatus` log，方便從 Railway 日誌確認 Azure 是否正常回傳。
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
