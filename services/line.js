/**
 * services/line.js
 * 
 * Handles interactions with the LINE Messaging API.
 */

const line = require('@line/bot-sdk');
const dotenv = require('dotenv');

dotenv.config();

const config = {
    channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN,
    channelSecret: process.env.LINE_CHANNEL_SECRET,
};

// Create LINE SDK client
const client = new line.Client(config);

/**
 * Reply to a user's message.
 * @param {string} replyToken - The token for replying to the event.
 * @param {string} text - The text message to send.
 */
async function replyMessage(replyToken, text) {
    try {
        await client.replyMessage(replyToken, {
            type: 'text',
            text: text,
        });
    } catch (error) {
        console.error('[LINE] Error sending reply:', error);
        throw error;
    }
}

module.exports = {
    config,
    client,
    replyMessage,
    line,
};
