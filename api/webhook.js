/**
 * api/webhook.js
 * 
 * Main entry point for the Vercel Serverless Function.
 * Handles LINE Webhook events.
 */

const express = require('express');
const lineService = require('../services/line');
const openaiService = require('../services/openai');

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
            // Only handle text messages
            if (event.type === 'message' && event.message.type === 'text') {
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
    const userMessage = event.message.text;

    try {
        // 1. Get answer from OpenAI
        // We send a "thinking" message or just wait? 
        // LINE requires a reply within a few seconds, but OpenAI might take longer.
        // Ideally, we should use the "loading" animation feature of LINE or push messages later.
        // For simplicity in this MVP, we wait. If it times out, LINE will show error to user, 
        // but our backend might still finish.

        // Note: LINE Reply Token is valid for ~30 seconds.

        const aiResponse = await openaiService.handleMessage(userId, userMessage);

        // 2. Reply to user
        await lineService.replyMessage(replyToken, aiResponse);

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
