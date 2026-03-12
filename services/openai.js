/**
 * services/openai.js
 * 
 * Handles interactions with the OpenAI Assistant API.
 * Manages Threads, Runs, and retrieves responses.
 */

const OpenAI = require('openai');
const dotenv = require('dotenv');
const state = require('./state');
const { retrieveCourseContext } = require('./courseRetrieval');
const { isDoseOrContraindicationQuery, buildDisclaimer, selfCorrectAnswer } = require('./answerSafety');

dotenv.config();

const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

const ASSISTANT_ID = process.env.OPENAI_ASSISTANT_ID;

/**
 * Handles an incoming user message by sending it to the OpenAI Assistant
 * and waiting for a response，並同時回傳意圖分類結果。
 * 
 * @param {string} userId - The LINE User ID.
 * @param {string} userMessage - The text message from the user.
 * @param {string} mode - The current user mode ('tcm', 'speaking', 'writing').
 * @returns {Promise<{ text: string, intent: string }>} - The Assistant's response text and detected intent.
 */
async function handleMessage(userId, userMessage, mode = 'tcm') {
    try {
        // 1. Get or Create Thread
        let threadId = await state.getThreadId(userId);

        if (!threadId) {
            const thread = await openai.beta.threads.create();
            threadId = thread.id;
            await state.saveThreadId(userId, threadId);
        }

        // Adjust prompt based on mode
        let finalMessage = userMessage;
        if (mode === 'speaking') {
            finalMessage = `你現在是口說助教，請分析以下逐字稿並給予發音與專業術語建議：\n\n${userMessage}`;
        }

        // ---- Tiered Knowledge Architecture（僅 tcm 模式啟用）----
        // 以課綱做 Hybrid+HyDE 檢索，決定是否先嘗試「課程核心知識」回答。
        let courseHint = null;
        let shouldTryCourseFirst = false;
        if (mode === 'tcm') {
            try {
                const ret = await retrieveCourseContext(finalMessage, { useHyde: true });
                const best = ret?.best;
                if (best?.entry) {
                    courseHint = {
                        date: best.entry.date,
                        topic: best.entry.topic,
                        hasHandout: Boolean(best.entry.hasHandout),
                        score: best.score,
                        keyword: best.keyword,
                        vector: best.vector,
                    };
                    // 閾值：keyword 命中（>=6）或混合分數足夠，且該週標記有講義時，優先走課程層
                    shouldTryCourseFirst =
                        (courseHint.keyword >= 6 || courseHint.score >= 9) && courseHint.hasHandout;
                }
            } catch (e) {
                console.warn('[TieredRAG] retrieveCourseContext failed:', e.message);
            }
        }

        // 2. Add Message to Thread
        await openai.beta.threads.messages.create(threadId, {
            role: 'user',
            content: finalMessage,
        });

        const baseIntentInstr =
            "你是中醫學院的助教，同時也是對話意圖分類器。請依照下列步驟處理「這一次」使用者訊息：" +
            "1) 先判斷此次訊息的 intent，必須在四種中擇一：HEALTH_CONSULT（個人健康/症狀/用藥/調理相關）、" +
            "CHITCHAT（與中醫與健康無關的純聊天或閒聊）、QUIZ_REQUEST（明確要求要測驗、小測驗、考題）、OTHER（與中醫或健康有關，但不是個人症狀諮詢）。" +
            "2) 再依 intent 產生要給學生看的回覆內容。" +
            "3) 輸出格式一定要嚴格遵守：第一行為 `[INTENT=XXX]`；第二行開始才是要給學生看的自然語言回覆內容。" +
            "4) 若 intent=HEALTH_CONSULT：用中醫衛教角度說明，語氣溫暖但勿過度保證療效。" +
            "5) 若 intent=CHITCHAT：禮貌拒絕並引導回中醫課程。" +
            "6) 若 intent=QUIZ_REQUEST：簡短回覆表示收到需求即可，勿自行出題。" +
            "7) 若 intent=OTHER：以中醫理論與課程知識回答即可。" +
            "8) 無論 intent 為何，都必須在第一行輸出 `[INTENT=...]`，不能遺漏；之後用繁體中文回答。";

        const riskFlag = isDoseOrContraindicationQuery(finalMessage);
        const safetyHardRules =
            "在你輸出最終答案前，請先在腦中依序做：分析問題→檢索資料→比對邏輯→生成回答（不要把思考過程寫出來）。" +
            "嚴格限制：若問題涉及「藥材劑量」或「配伍禁忌」，且課程資料未提及，你不得給出具體數值或斷言，只能提供原則性建議並建議諮詢合格中醫師。" +
            "避免幻覺：若你不確定，請明確說明『課程資料未提及/我無法確認』，不要編造。";

        const courseTierInstr = () => {
            const hint = courseHint?.topic ? `（優先參考：${courseHint.date || ''} ${courseHint.topic}）` : '';
            const risk = riskFlag ? "本題涉及劑量/禁忌風險，請特別遵守限制。" : "";
            return (
                baseIntentInstr +
                "你必須採用「分層信任架構」：第一層為課程資料（最高優先）。" +
                "請先嘗試用你可取得的內部課程資料/講義（File Search）回答；若能找到足夠依據，回覆內容第二行開頭必須加上 `[課程核心知識]`。" +
                "若課程資料不足以支持回答，請在第二行只輸出 `[NO_COURSE_MATCH]`，不要提供任何通用推理內容。" +
                "衝突處理：若課程資料與你內建知識衝突，以課程資料為準。" +
                hint +
                risk +
                safetyHardRules
            );
        };

        const generalTierInstr =
            baseIntentInstr +
            "你必須採用「分層信任架構」：當課程資料不足時，才可使用通用中醫知識推理。" +
            "回覆內容第二行開頭必須加上 `[通用中醫參考]`，並在回覆末尾附上免責聲明。" +
            "衝突處理：若你知道課程資料可能不同，請提示以課程內容為準。" +
            (riskFlag ? "本題涉及劑量/禁忌風險，請只提供原則性建議，不給具體數值。" : "") +
            safetyHardRules;

        // 3. Run Assistant（依分層策略選擇指令）
        let run = await openai.beta.threads.runs.create(threadId, {
            assistant_id: ASSISTANT_ID,
            instructions: shouldTryCourseFirst ? courseTierInstr() : generalTierInstr,
        });

        // 4. Poll for Completion
        // Vercel functions have a timeout (usually 10s-60s). 
        // We need to poll efficiently.
        let runStatus = await openai.beta.threads.runs.retrieve(threadId, run.id);

        // Polling loop
        // TODO: In a production serverless env, consider using a queue or separate worker if runs take too long.
        // For now, we poll for up to ~20-30 seconds.
        const startTime = Date.now();
        const TIMEOUT_MS = 25000; // 25 seconds safety buffer for Vercel default 10s (needs config) or 60s

        while (runStatus.status !== 'completed') {
            if (Date.now() - startTime > TIMEOUT_MS) {
                throw new Error('OpenAI Run timed out');
            }

            if (runStatus.status === 'failed' || runStatus.status === 'cancelled') {
                console.error('Run failed:', runStatus.last_error);
                throw new Error(`OpenAI Run failed with status: ${runStatus.status}`);
            }

            // Wait 1 second before checking again
            await new Promise(resolve => setTimeout(resolve, 1000));
            runStatus = await openai.beta.threads.runs.retrieve(threadId, run.id);
        }

        // 5. Get Messages
        let messages = await openai.beta.threads.messages.list(threadId);

        // The latest message is at index 0
        const lastMessage = messages.data[0];

        if (lastMessage.role === 'assistant' && lastMessage.content.length > 0) {
            const content = lastMessage.content[0];
            if (content.type === 'text') {
                const raw = content.text.value || "";
                let intent = "KNOWLEDGE_QUERY";
                let text = raw.trim();
                const match = raw.match(/^\[INTENT=([A-Z_]+)\]\s*\n([\s\S]*)$/);
                if (match) {
                    intent = match[1] || "KNOWLEDGE_QUERY";
                    text = (match[2] || "").trim();
                }
                // 若走課程層但判定課程不足，則自動降級到通用中醫參考層再跑一次
                if (shouldTryCourseFirst && /^\[NO_COURSE_MATCH\]\s*$/m.test(text)) {
                    run = await openai.beta.threads.runs.create(threadId, {
                        assistant_id: ASSISTANT_ID,
                        instructions: generalTierInstr,
                    });
                    let st = await openai.beta.threads.runs.retrieve(threadId, run.id);
                    const stStart = Date.now();
                    while (st.status !== 'completed') {
                        if (Date.now() - stStart > TIMEOUT_MS) throw new Error('OpenAI Run timed out (fallback)');
                        if (st.status === 'failed' || st.status === 'cancelled') {
                            throw new Error(`OpenAI Run failed with status: ${st.status}`);
                        }
                        await new Promise((resolve) => setTimeout(resolve, 1000));
                        st = await openai.beta.threads.runs.retrieve(threadId, run.id);
                    }
                    messages = await openai.beta.threads.messages.list(threadId);
                    const lm = messages.data[0];
                    if (lm?.role === 'assistant' && lm.content?.[0]?.type === 'text') {
                        const raw2 = lm.content[0].text.value || '';
                        const match2 = raw2.match(/^\[INTENT=([A-Z_]+)\]\s*\n([\s\S]*)$/);
                        if (match2) {
                            intent = match2[1] || intent;
                            text = (match2[2] || '').trim();
                        } else {
                            text = raw2.trim();
                        }
                    }
                }

                // 通用層強制加免責（避免模型漏掉）
                if (!text.includes('⚠️') && text.includes('[通用中醫參考]')) {
                    text = `${text}\n\n${buildDisclaimer()}`;
                }

                // 輸出前自我修正（抑制幻覺/不當劑量禁忌）
                try {
                    const corrected = await selfCorrectAnswer({
                        userQuery: finalMessage,
                        draftAnswer: text,
                        hadCourseMatch: shouldTryCourseFirst,
                    });
                    text = corrected || text;
                } catch (e) {
                    console.warn('[Safety] selfCorrectAnswer failed:', e.message);
                }
                return { text, intent };
            }
        }

        return { text: "抱歉，我現在無法回答您的問題。", intent: "KNOWLEDGE_QUERY" };

    } catch (error) {
        console.error('[OpenAI] Error handling message:', error);
        // If thread is invalid, maybe clear it?
        // await state.saveThreadId(userId, null);
        throw error;
    }
}

module.exports = {
    handleMessage,
};
