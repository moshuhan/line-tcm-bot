/**
 * services/openai.js
 * 
 * Handles interactions with the OpenAI Assistant API.
 * Manages Threads, Runs, and retrieves responses.
 */

const OpenAI = require('openai');
const dotenv = require('dotenv');
const state = require('./state');

dotenv.config();

const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

const ASSISTANT_ID = process.env.OPENAI_ASSISTANT_ID;

/**
 * Handles an incoming user message by sending it to the OpenAI Assistant
 * and waiting for a response.
 * 
 * @param {string} userId - The LINE User ID.
 * @param {string} userMessage - The text message from the user.
 * @returns {Promise<string>} - The Assistant's response text.
 */
async function handleMessage(userId, userMessage) {
    try {
        // 1. Get or Create Thread
        let threadId = await state.getThreadId(userId);

        if (!threadId) {
            console.log(`[OpenAI] Creating new thread for user ${userId}`);
            const thread = await openai.beta.threads.create();
            threadId = thread.id;
            await state.saveThreadId(userId, threadId);
        } else {
            console.log(`[OpenAI] Using existing thread ${threadId} for user ${userId}`);
        }

        // 2. Add Message to Thread
        await openai.beta.threads.messages.create(threadId, {
            role: 'user',
            content: userMessage,
        });

        // 3. Run Assistant
        console.log(`[OpenAI] Starting run for thread ${threadId}`);
        const run = await openai.beta.threads.runs.create(threadId, {
            assistant_id: ASSISTANT_ID,
            instructions: "你是中醫學院的助教。請用專業、親切且富有耐心的語氣回答同學的問題。多使用「同學你好」、「根據課程內容」等字眼。請根據知識庫內容回答。",
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
                throw new Error(`OpenAI Run failed with status: ${runStatus.status}`);
            }

            // Wait 1 second before checking again
            await new Promise(resolve => setTimeout(resolve, 1000));
            runStatus = await openai.beta.threads.runs.retrieve(threadId, run.id);
        }

        // 5. Get Messages
        const messages = await openai.beta.threads.messages.list(threadId);

        // The latest message is at index 0
        const lastMessage = messages.data[0];

        if (lastMessage.role === 'assistant' && lastMessage.content.length > 0) {
            const content = lastMessage.content[0];
            if (content.type === 'text') {
                return content.text.value;
            }
        }

        return "抱歉，我現在無法回答您的問題。";

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
