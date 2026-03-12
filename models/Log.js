/**
 * models/Log.js
 * 對話記錄資料表（MongoDB）。
 */

const { mongoose } = require('../lib/db');

const { Schema } = mongoose;

const LogSchema = new Schema(
    {
        timestamp: { type: Date, required: true },
        userId: { type: String, index: true },
        mode: { type: String },
        // 舊欄位（向下相容）
        question: { type: String },
        answer: { type: String },

        // 研究用新欄位（明確定義）
        userQuery: { type: String }, // 學生問的話
        aiResponse: { type: String }, // AI 的回答（包含題目）
        isQuizMode: { type: Boolean, default: false }, // 是否觸發測驗
        quizAnswer: { type: String }, // 這題正確答案 A/B/C
        userQuizResult: {
            type: String,
            enum: ['correct', 'incorrect', 'skipped'],
        }, // 學生最終答對/答錯/跳過
        intent: { type: String }, // HEALTH_CONSULT / CHITCHAT / QUIZ_REQUEST / OTHER / KNOWLEDGE_QUERY / UNKNOWN
        platform: { type: String, default: 'line' },
        threadId: { type: String },
        rawRequest: { type: String },
        rawResponse: { type: String },
    },
    {
        timestamps: true, // createdAt, updatedAt
    }
);

const Log = mongoose.models.Log || mongoose.model('Log', LogSchema);

module.exports = Log;

