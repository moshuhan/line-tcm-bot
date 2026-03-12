/**
 * services/answerSafety.js
 * 針對「幻覺抑制」與「劑量/配伍禁忌」等高風險內容做輸出前自我檢核與修正。
 */

const OpenAI = require('openai');

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

function isDoseOrContraindicationQuery(text) {
  const t = String(text || '');
  return /(?:劑量|幾克|幾錢|克數|mg|毫克|公克|g\b|配伍|禁忌|相反|十八反|十九畏|孕婦禁用|哺乳|交互作用)/i.test(
    t
  );
}

function buildDisclaimer() {
  return '⚠️ 以上為中醫學習與衛教性資訊，非個人化醫療建議；若有身體不適、用藥需求或特殊族群（孕婦、慢性病、兒童），請務必諮詢合格中醫師。';
}

/**
 * 用 LLM 做一次「自我修正」：移除不可靠偏方、過度武斷、或不應出現的具體劑量/禁忌數值。
 */
async function selfCorrectAnswer({ userQuery, draftAnswer, hadCourseMatch }) {
  const q = String(userQuery || '').trim();
  const a = String(draftAnswer || '').trim();
  if (!a) return a;

  const resp = await openai.chat.completions.create({
    model: 'gpt-4o-mini',
    temperature: 0.1,
    max_tokens: 700,
    messages: [
      {
        role: 'system',
        content:
          '你是回覆品質檢核器。你的工作是「只修正」內容，不要加入新知識。請遵守：' +
          '1) 移除/改寫任何看起來像偏方、迷信、或未經證實的治療承諾。' +
          '2) 若出現具體藥材劑量數值或明確配伍禁忌，但沒有明確課程依據，改為原則性建議並提醒諮詢專業。' +
          '3) 保留原本的標註（如 [課程核心知識] / [通用中醫參考]）與原本的語氣與結構。' +
          '4) 輸出只要「修正後的最終回覆」，不要解釋你做了什麼。',
      },
      {
        role: 'user',
        content:
          `【學生問題】\n${q}\n\n` +
          `【系統資訊】\nhadCourseMatch=${hadCourseMatch ? 'true' : 'false'}\n\n` +
          `【草稿回覆】\n${a}`,
      },
    ],
  });

  return String(resp.choices?.[0]?.message?.content || a).trim();
}

module.exports = {
  isDoseOrContraindicationQuery,
  buildDisclaimer,
  selfCorrectAnswer,
};

