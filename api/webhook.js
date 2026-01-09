/**
 * api/webhook.js
 * 
 * Main entry point for the Vercel Serverless Function.
 * Handles LINE Webhook events.
 */

const express = require('express');
const lineService = require('../services/line');
const openaiService = require('../services/openai');
const state = require('../services/state');

const app = express();

// LINE Middleware for Signature Validation
// Note: We use the middleware on the specific route or globally if it's the only route.
// For Vercel, we export the app, but we need to handle the body parsing carefully.
// @line/bot-sdk middleware handles body parsing and signature validation.

app.post('/api/webhook', lineService.line.middleware(lineService.config), async (req, res) => {
    try {
        const events = req.body.events;

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
        res.status(500).end();
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

            // 1. Get user mode
            const mode = await state.getUserMode(userId);

            // 2. Get answer from OpenAI
            const aiResponse = await openaiService.handleMessage(userId, userMessage, mode);

            // 3. Reply to user
            await lineService.replyMessage(replyToken, aiResponse);
        }

    } catch (error) {
        console.error(`[Event] Error handling event for user ${userId}:`, error);
        // Optional: Reply with error message
        await lineService.replyMessage(replyToken, "抱歉，系統目前繁忙，請稍後再試。");
    }
}

// For local development
if (require.main === module) {
    const port = process.env.PORT || 3000;
    app.listen(port, () => {
        console.log(`listening on ${port}`);
    });
}

// Export for Vercel
module.exports = app;
