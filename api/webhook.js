/**
 * api/webhook.js
 * 
 * Main entry point for the Vercel Serverless Function.
 * Handles LINE Webhook events.
 * LINE 金鑰由 services/line.js 讀取，使用 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET。
 */

require('dotenv').config();

const express = require('express');
const lineService = require('../services/line');
const openaiService = require('../services/openai');
const quizService = require('../services/quizService');
const state = require('../services/state');
const loggingService = require('../services/loggingService');
const { getRedisClient } = require('../lib/redisClient');
const { mongoose } = require('../lib/db');

const app = express();

// JSON Body 解析：
// 注意：LINE 的簽章驗證需要讀取原始 body，@line/bot-sdk middleware 會自行處理 /api/webhook。
// 因此這裡只對「非 /api/webhook」路徑啟用 express.json()，避免破壞簽章驗證。
app.use((req, res, next) => {
    if (req.path === '/api/webhook') return next();
    return express.json()(req, res, next);
});

// LINE Middleware for Signature Validation
// Note: We use the middleware on the specific route or globally if it's the only route.
// For Vercel, we export the app, but we need to handle the body parsing carefully.
// @line/bot-sdk middleware handles body parsing and signature validation.

app.post('/api/webhook', lineService.line.middleware(lineService.config), async (req, res) => {
    try {
        try {
            const events = req.body.events || [];

            // Process all events asynchronously
            const results = await Promise.all(events.map(async (event) => {
                // Handle text messages and postbacks
                if ((event.type === 'message' && event.message.type === 'text') || event.type === 'postback') {
                    return handleEvent(event);
                }
                return Promise.resolve(null);
            }));

            res.status(200).json({
                status: 'success',
                results,
            });
        } catch (error) {
            console.error('[Webhook] Error processing events:', error);
            // 重要：即使內部錯誤也回傳 200，避免 LINE 重試與 Node 進程異常
            if (!res.headersSent) {
                res.sendStatus(200);
            }
        }
    } catch (fatalError) {
        // 最外層保護，避免任何未捕捉錯誤讓 Node 進程終止
        console.error('[Webhook] FATAL error:', fatalError);
        if (!res.headersSent) {
            res.sendStatus(200);
        }
    }
});

/**
 * Handle a single LINE event.
 * @param {object} event - The LINE event object.
 */
async function handleEvent(event) {
    const userId = event.source.userId;
    const replyToken = event.replyToken;

    try {
        if (event.type === 'postback') {
            const data = event.postback.data; // e.g., "mode=speaking"
            const params = new URLSearchParams(data);
            const mode = params.get('mode');

            if (mode) {
                await state.saveUserMode(userId, mode);
                let modeName = '中醫問答';
                if (mode === 'speaking') modeName = '口說練習';
                if (mode === 'writing') modeName = '寫作修改';

                await lineService.replyMessage(replyToken, `已切換至「${modeName}」模式。`);
            }
            return;
        }

        if (event.type === 'message' && event.message.type === 'text') {
            const userMessage = event.message.text;

            // 0. 測驗狀態機（最高優先）：先查 Redis 是否有未完成測驗
            try {
                const redis = getRedisClient();
                const quizKey = `quiz:${userId}:answer`;
                const pendingAnswer = await redis.get(quizKey);
                // 修復判定：統一去頭尾空白 + 大小寫轉換
                const trimmed = String(userMessage || '').trim();
                const textUpper = trimmed.toUpperCase();
                // 額外正規化：移除常見包裹符號（例如【A】、［B］、「C」、以及標點）
                const normalized = trimmed
                    .replace(/^[\s【\[\(（『「〈《<"'`，。、．\.\!！\?？；;：:、]+/g, '')
                    .replace(/[\s】\]\)）』」〉》>"'`，。、．\.\!！\?？；;：:、]+$/g, '');
                const normalizedUpper = normalized.toUpperCase();
                // 全形轉半形（僅處理 ABC 常見全形）
                const fullWidthToHalf = (s) =>
                    String(s || '')
                        .replace(/[Ａ]/g, 'A')
                        .replace(/[Ｂ]/g, 'B')
                        .replace(/[Ｃ]/g, 'C');
                const normalizedUpperHalf = fullWidthToHalf(normalizedUpper);
                const textUpperHalf = fullWidthToHalf(textUpper);

                // 第一優先：Redis 有答案，且用戶輸入為 A/B/C 相關字眼 → 判定對錯並 return
                if (pendingAnswer) {
                    // pendingAnswer 可能是：
                    // - 舊格式："A"|"B"|"C"
                    // - 新格式：JSON 字串 {"answer":"A","explanation":"..."}
                    let pending = { answer: '', explanation: '', logId: '' };
                    try {
                        if (typeof pendingAnswer === 'string' && pendingAnswer.trim().startsWith('{')) {
                            const obj = JSON.parse(pendingAnswer);
                            pending.answer = String(obj.answer || '').trim().toUpperCase();
                            pending.explanation = String(obj.explanation || '').trim();
                            pending.logId = String(obj.logId || '').trim();
                        } else {
                            pending.answer = String(pendingAnswer || '').trim().toUpperCase();
                        }
                    } catch (e) {
                        pending.answer = String(pendingAnswer || '').trim().toUpperCase();
                    }

                    // 精準選項判定：只有完全符合或「開頭為」下列形式才算作答
                    // - 單獨 A/B/C
                    // - 選A/選B/選C（允許「選 A」）
                    // - (A)/(B)/(C) 或 （A）/（B）/（C）
                    const isOptionReply =
                        normalizedUpperHalf === 'A' ||
                        normalizedUpperHalf === 'B' ||
                        normalizedUpperHalf === 'C' ||
                        /^選\s*[ABC]/.test(normalizedUpperHalf) ||
                        /^\([ABC]\)/.test(textUpperHalf) ||
                        /^（[ABC]）/.test(textUpperHalf);

                    if (isOptionReply) {
                        const answer = pending.answer;
                        const chosen =
                            (normalizedUpperHalf.match(/[ABC]/) || [])[0] ||
                            (textUpperHalf.match(/[ABC]/) || [])[0] ||
                            '';
                        const isCorrect = chosen === answer;

                        if (isCorrect) {
                            await lineService.replyMessage(replyToken, '恭喜!回答正確!歡迎繼續提問。');
                        } else {
                            const explanation = pending.explanation ? `\n\n詳解：\n${pending.explanation}` : '';
                            await lineService.replyMessage(replyToken, `回答不正確。\n正確答案：${answer}${explanation}`);
                        }

                        // 更新 MongoDB：同一筆 Log 寫入作答結果（correct/incorrect）
                        if (pending.logId) {
                            loggingService
                                .updateQuizResult(pending.logId, isCorrect ? 'correct' : 'incorrect')
                                .catch((e) => console.error('[Webhook] Failed to update quiz result:', e.message));
                        }

                        try {
                            await redis.del(quizKey);
                        } catch (e) {
                            console.error('[Webhook] Failed to clear quiz state:', e.message);
                        }
                        return;
                    }

                    // 第二優先：Redis 有答案，但用戶輸入不是選項（新提問）→ 先刪狀態，再進入 AI 諮詢
                    // 更新 MongoDB：這題被視為跳過（skipped）
                    if (pending.logId) {
                        loggingService
                            .updateQuizResult(pending.logId, 'skipped')
                            .catch((e) => console.error('[Webhook] Failed to update quiz skipped:', e.message));
                    }
                    try {
                        await redis.del(quizKey);
                    } catch (e) {
                        console.error('[Webhook] Failed to clear quiz state (before new question):', e.message);
                    }
                }
                // 第三優先：Redis 沒答案 → 直接進入 AI 諮詢（不做任何事）
            } catch (e) {
                console.error('[Webhook] Quiz state check failed:', e.message);
            }

            // 1. Get user mode
            const mode = await state.getUserMode(userId);

            // 2. Get answer from OpenAI（含意圖預分類與 Guardrails）
            const { text: aiText, intent } = await openaiService.handleMessage(userId, userMessage, mode);

            let finalReply = aiText;
            let quizMeta = null;
            let quizLogId = '';

            // 3. 若為中醫模式，強制嘗試根據回覆內容產生一題三選一小測驗
            if (mode === 'tcm') {
                try {
                    const quiz = await quizService.generateQuizQuestion(aiText);
                    if (quiz && quiz.question && Array.isArray(quiz.options) && quiz.options.length === 3) {
                        // 先產生 logId，避免使用者秒回覆導致狀態/Log 對不上
                        quizLogId = String(new mongoose.Types.ObjectId());
                        const quizText =
                            '\n\n——\n📝 小測驗\n' +
                            quiz.question +
                            '\n' +
                            quiz.options.join('\n') +
                            '\n\n(回覆選項來挑戰，或直接輸入新問題繼續學習喔！)';
                        finalReply = aiText + quizText;
                        quizMeta = {
                            answer: quiz.answer,
                            explanation: quiz.explanation || '',
                        };

                        // 立刻把測驗狀態寫進 Redis（不要等 Mongo 寫入，避免 race）
                        const redis = getRedisClient();
                        const quizKey = `quiz:${userId}:answer`;
                        try {
                            const payload = JSON.stringify({
                                answer: quizMeta.answer,
                                explanation: quizMeta.explanation || '',
                                logId: quizLogId,
                            });
                            await redis.set(quizKey, payload, 'EX', 600);
                        } catch (e) {
                            console.error('[Webhook] Failed to set quiz state in Redis:', e.message);
                        }
                    }
                } catch (e) {
                    console.error('[Webhook] generateQuizQuestion failed:', e.message);
                }
            }

            // 4. Reply to user
            await lineService.replyMessage(replyToken, finalReply);

            // 5. 回覆後記錄對話到 MongoDB（測驗題會使用預先產生的 quizLogId）
            loggingService
                .createLog({
                    _id: quizLogId || undefined,
                    userId,
                    mode,
                    userQuery: userMessage,
                    aiResponse: finalReply,
                    intent,
                    isQuizMode: Boolean(quizMeta),
                    quizAnswer: quizMeta?.answer,
                    platform: 'line',
                })
                .catch((err) => {
                    console.error('[Webhook] createLog failed:', err.message);
                });
        }

    } catch (error) {
        console.error(`[Event] Error handling event for user ${userId}:`, error);
        // Optional: Reply with error message
        await lineService.replyMessage(replyToken, "抱歉，系統目前繁忙，請稍後再試。");
    }
}

// For local development：若 3000 被佔用則改試 3001，避免 EADDRINUSE 直接當機
if (require.main === module) {
    const defaultPort = Number(process.env.PORT) || 3000;
    function tryListen(port) {
        const server = app.listen(port, () => {});
        server.on('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                server.close();
                console.warn(`Port ${port} in use, trying ${port + 1}...`);
                tryListen(port + 1);
            } else {
                console.error('Server error:', err);
                process.exit(1);
            }
        });
    }
    tryListen(defaultPort);
}

// Export for Vercel
module.exports = app;
