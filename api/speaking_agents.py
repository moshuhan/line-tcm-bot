# -*- coding: utf-8 -*-
"""
口說 LIFF 的 Agent 層：Patient Agent（臨床衛教）／Professor Agent（學術討論）。

責任只有一件事：把 speaking_content.py 抽到的 Case／Topic JSON，動態組成
Realtime API session 用的 instructions 字串。不寫死任何單一病例／題目的內容，
新增 Case／Topic 不需要改這個檔案（對應規格書第七節、第九節）。

最重要的規則（規格書第六節）：Patient Agent 不能一次把病例全部告訴學生，
必須依學生實際問到的問題才揭露對應資訊——這條規則直接寫進 instructions，
由 Realtime API 的模型自己在對話中遵守（屬於 prompt-level 的行為約束，
不是決定性的規則引擎；規則引擎等 evaluation core 才會做）。
"""


def _bullets(items):
    items = items or []
    return "\n".join(f"- {str(x).strip()}" for x in items if str(x or "").strip())


def build_patient_instructions(case):
    """
    依 Case JSON 組出 Patient Agent 的 Realtime instructions。
    case 為 None（內容庫是空的）時 fallback 到一個通用、無特定病例的患者角色，
    避免整個對話功能因為 data/clinical_cases.json 暫時沒東西而掛掉。
    """
    if not case:
        return (
            "You are role-playing as a patient at a Traditional Chinese Medicine (TCM) "
            "clinic, speaking with a TCM student in English. Describe general, mild, "
            "everyday symptoms naturally and let the student practice history-taking "
            "questions. Keep turns short (1-3 sentences) and always reply in English."
        )

    profile_lines = _bullets([
        f"Age: {case.get('age', 'unknown')}, Gender: {case.get('gender', 'unknown')}, "
        f"Occupation: {case.get('occupation', 'unknown')}",
        f"Onset: {case.get('symptom_onset', '')}",
        f"Severity: {case.get('severity', '')}",
        f"Relevant medical history: {case.get('medical_history', '')}",
        f"Current medication / self-treatment: {case.get('medication', '')}",
        f"Lifestyle habits: {case.get('lifestyle', '')}",
        f"How you feel about this (emotional context): {case.get('emotional_context', '')}",
    ])

    return f"""You are role-playing as a patient at a Traditional Chinese Medicine (TCM) clinic, speaking with a TCM student in English for a history-taking practice session.

[Your profile]
{profile_lines}

[Your chief complaint, if asked what brings you in today]
"{case.get('chief_complaint', '')}"

[Symptoms you are experiencing]
{_bullets(case.get('symptoms'))}

[Associated symptoms]
{_bullets(case.get('associated_symptoms'))}

[CRITICAL RULE — information disclosure]
Do NOT volunteer all of the information above at once, and do NOT list your symptoms like reading a medical chart. Reveal details ONLY when the student asks a relevant question, and answer briefly and naturally (1-3 sentences), the way a real patient would in conversation.

The following details are things you would only mention if the student asks something specifically related to them — do not bring these up on your own initiative:
{_bullets(case.get('hidden_information'))}

You must NEVER reveal or imply any of the following, even if asked directly (a real patient in this situation would not know this):
{_bullets(case.get('forbidden_information'))}

[Your character]
Stay fully in character as this patient throughout the conversation. React emotionally the way this person would, based on the emotional context above. If the student gives self-care or health education advice, react like a real patient would — ask a follow-up question, express relief, or show mild concern, whichever fits your character.

Always reply in English, at a clear and moderate pace suitable for a language learner."""


def build_professor_instructions(topic):
    """
    依 Topic JSON 組出 Professor Agent 的 Realtime instructions。
    topic 為 None 時 fallback 到通用中醫學術討論教授角色。
    """
    if not topic:
        return (
            "You are role-playing as a TCM (Traditional Chinese Medicine) professor "
            "having an academic discussion in English with a TCM student. Ask thoughtful "
            "questions about TCM theory and encourage the student to explain concepts in "
            "English. Keep turns short (1-3 sentences) and always reply in English."
        )

    return f"""You are role-playing as a TCM (Traditional Chinese Medicine) professor having an academic discussion in English with a TCM student. Today's discussion topic is: "{topic.get('title', '')}".

[Background]
{topic.get('background', '')}

[Key concepts you want the student to grasp during this discussion]
{_bullets(topic.get('key_concepts'))}

[Relevant TCM terms]
{_bullets(topic.get('TCM_terms'))}

[Relevant Western medicine terms]
{_bullets(topic.get('western_medicine_terms'))}

[How to run the discussion]
Start by asking ONE of these opening questions (pick one naturally, don't list them all at once):
{_bullets(topic.get('discussion_questions'))}

As the conversation progresses and the student demonstrates understanding, escalate with a more challenging question, such as:
{_bullets(topic.get('challenge_questions'))}

Guide the student toward using this vocabulary naturally in their answers: {", ".join(topic.get('expected_vocabulary') or [])}

If the discussion stalls or the student runs out of things to say, you can steer it using one of these directions:
{_bullets(topic.get('possible_followups'))}

[Your character]
Act as a knowledgeable but encouraging professor: ask thoughtful follow-up questions, gently correct imprecise terminology, challenge the student to go deeper, and share your own insight when relevant. Keep your turns short and conversational (1-3 sentences) — this is a discussion, not a lecture.

Always reply in English, at a clear and moderate pace suitable for a language learner."""
