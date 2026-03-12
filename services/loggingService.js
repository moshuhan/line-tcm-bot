/**
 * services/loggingService.js
 * 數據採集服務：將對話記錄寫入 Redis List research_logs。
 */

const { getRedisClient } = require('../lib/redisClient');
const { connect } = require('../lib/db');
const Log = require('../models/Log');

const RESEARCH_LOGS_KEY = 'research_logs';

/**
 * 建立一筆研究用 Log（Redis + MongoDB）。
 * 會回傳 MongoDB `_id`，方便後續針對同一筆資料做更新（例如作答結果）。
 */
async function createLog(logData) {
    const payload = {
        _id: logData?._id || undefined,
        timestamp: new Date(),
        userId: logData?.userId || '',
        mode: logData?.mode || '',
        intent: logData?.intent || '',

        // 舊欄位（向下相容）
        question: logData?.question || logData?.userQuery || '',
        answer: logData?.answer || logData?.aiResponse || '',

        // 新欄位
        userQuery: logData?.userQuery || logData?.question || '',
        aiResponse: logData?.aiResponse || logData?.answer || '',
        isQuizMode: Boolean(logData?.isQuizMode),
        quizAnswer: logData?.quizAnswer || undefined,
        userQuizResult: logData?.userQuizResult || undefined,

        platform: logData?.platform || 'line',
        threadId: logData?.threadId || undefined,
        rawRequest: logData?.rawRequest || undefined,
        rawResponse: logData?.rawResponse || undefined,
    };

    try {
        // 1. Redis（同步推入，供後續批次分析）
        const redis = getRedisClient();
        await redis.rpush(RESEARCH_LOGS_KEY, JSON.stringify({
            ...payload,
            timestamp: payload.timestamp.toISOString(),
        }));
    } catch (err) {
        console.error('[LoggingService] Redis createLog error:', err.message);
    }

    // 2. MongoDB（確保寫入成功，回傳 _id 供後續 update）
    try {
        await connect();
        const doc = await Log.create(payload);
        return String(doc?._id || '');
    } catch (err) {
        console.error('[LoggingService] MongoDB createLog error:', err.message);
        return '';
    }
}

/**
 * 依照 logId 更新學生的作答結果（correct/incorrect/skipped）。
 */
async function updateQuizResult(logId, userQuizResult) {
    if (!logId) return false;
    try {
        await connect();
        await Log.findByIdAndUpdate(
            logId,
            { userQuizResult },
            { new: false }
        );
        return true;
    } catch (err) {
        console.error('[LoggingService] MongoDB updateQuizResult error:', err.message);
        return false;
    }
}

module.exports = {
    createLog,
    updateQuizResult,
};
