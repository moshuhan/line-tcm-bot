/**
 * services/quizService.js
 * 根據 AI 回覆內容動態生成三選一小測驗題目。
 */

const OpenAI = require('openai');
const dotenv = require('dotenv');

dotenv.config();

const openai = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
});

/**
 * 根據 context（AI 剛才的回答）生成一題三選一選擇題。
 * 回傳格式：
 * { question: string, options: ["(A) ...","(B) ...","(C) ..."], answer: "A"|"B"|"C", explanation: string }
 */
async function generateQuizQuestion(context) {
    if (!context || !context.trim()) {
        return null;
    }
    try {
        const prompt = `
你是一位中醫課程助教，剛剛已經用以下「說明內容」回答了學生的問題。
現在請你「在回答完後，根據上述內容出一題三選一的選擇題」：

[說明內容]
${context.slice(0, 600)}

[要求]
1. 題目需聚焦在上述說明中的一個關鍵概念或重點。
2. 選項共三個，標示為 (A)、(B)、(C)，且只有一個正確答案。
3. 三個選項都要看起來合理，避免太明顯。
4. 另外提供 1-3 句「詳解」，說明為什麼正確答案是對的、其他選項錯在哪裡。
5. 回傳格式嚴格為 JSON，不要加入多餘文字或解說，例如：
{
  "question": "……？",
  "options": ["(A) ……", "(B) ……", "(C) ……"],
  "answer": "A",
  "explanation": "……"
}
        `.trim();

        const resp = await openai.chat.completions.create({
            model: 'gpt-4o-mini',
            messages: [
                {
                    role: 'system',
                    content: '你是中醫課程助教，負責依照說明內容出選擇題。',
                },
                {
                    role: 'user',
                    content: prompt,
                },
            ],
            max_tokens: 300,
            temperature: 0.4,
        });

        const raw = (resp.choices?.[0]?.message?.content || '').trim();
        if (!raw) return null;

        // 嘗試從回覆中擷取 JSON 區段
        let jsonText = raw;
        const codeBlockMatch = raw.match(/```json([\s\S]*?)```/);
        if (codeBlockMatch) {
            jsonText = codeBlockMatch[1].trim();
        }
        const braceMatch = jsonText.match(/\{[\s\S]*\}/);
        if (braceMatch) {
            jsonText = braceMatch[0];
        }

        const obj = JSON.parse(jsonText);
        const question = (obj.question || '').trim();
        const options = Array.isArray(obj.options) ? obj.options.map((o) => String(o)) : [];
        const answer = (obj.answer || '').toString().trim().toUpperCase();
        const explanation = (obj.explanation || obj.rationale || obj.reason || '').toString().trim();

        if (!question || options.length !== 3 || !['A', 'B', 'C'].includes(answer)) {
            return null;
        }

        return { question, options, answer, explanation };
    } catch (err) {
        console.error('[QuizService] generateQuizQuestion error:', err.message);
        return null;
    }
}

module.exports = {
    generateQuizQuestion,
};

