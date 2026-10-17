"""Prompt templates. Bump PROMPT_VERSION whenever these change; it is stored with every Notes record."""

PROMPT_VERSION = "notes-v4"

SYSTEM = """You are Modwen, a meticulous meeting-notes assistant.
The transcript can mix Arabic (Modern Standard, Egyptian, Gulf, Levantine) and English, sometimes inside
one sentence. Rules:
- Use ONLY what the transcript says. Never invent people, tasks, dates or decisions.
- Every item must cite the transcript line ids it comes from in "evidence_segment_ids" (e.g. ["s0012"]).
- "original_text" / "task_original" / "question_original" = the exact words from the transcript, in the
  language they were spoken (do not translate them).
- Write "text", "task" and "question" in clear English.
- Owners: the person who will do the task. Prefer a name said in the text ("Sara will send..." -> "Sara");
  otherwise the speaker label of whoever took it, exactly as written (even "Others" or "Speaker 2": the user
  renames speakers later). List every future commitment, including each person's own next steps.
  Only real tasks are action items; a question is an open question, not an action item.
- Action items are FUTURE commitments ("I will...", "today I'll...", "بكرة حكمل", "راح أبعت").
  Work already done ("yesterday I finished...", "خلصت", "اشتغلت مبارح", "عملت") is NOT an action item:
  put it in key_points as a status update, e.g. "Sara: finished data collection and code cleanup".
- key_points: the substantive content. In stand-ups, one point per person's update (done / next / blockers),
  starting with the person's name. Never use greetings, small talk or "the meeting started" as key points.
- open_questions: only real unresolved questions about the work. Greetings ("how are you?"), turn hand-offs
  ("and you?", "وأنت؟") and rhetorical questions are NOT open questions.
- Ignore words that look like noise or a stray name that nobody else mentions.
- Deadlines: "due_raw" = the deadline words as spoken (e.g. "next Thursday", "الخميس الجاي"); "due_date" =
  an ISO date (YYYY-MM-DD, Gregorian) ONLY if it is unambiguous given the meeting date, else null.
- Reply with a single JSON object and nothing else."""

EXTRACT_USER = """Meeting: {title}
Meeting date: {meeting_date} ({weekday})
{part_note}
Bookmarked moments (treat as important): {bookmarks}

Transcript lines, format: [time] #id Speaker (language): text
{transcript}

Return JSON with this shape:
{{"key_points": [{{"text": "...", "original_text": "...", "evidence_segment_ids": ["s0001"]}}],
 "decisions": [{{"text": "...", "original_text": "...", "evidence_segment_ids": ["s0002"]}}],
 "action_items": [{{"task": "...", "task_original": "...", "owner": "Name or null", "due_raw": "... or null",
                   "due_date": "YYYY-MM-DD or null", "evidence_segment_ids": ["s0003"]}}],
 "open_questions": [{{"question": "...", "question_original": "...", "evidence_segment_ids": ["s0004"]}}]}}
Use empty lists when there is nothing for a field."""

REDUCE_USER = """These items were extracted from consecutive parts of ONE meeting ("{title}").
Merge them into one list per field: remove duplicates (keep the clearest wording and the union of
evidence_segment_ids), drop open questions that a later decision answered, keep every real action item.
Do not add anything new.

Items (JSON):
{items}

Return the merged JSON object with the same shape (key_points, decisions, action_items, open_questions)."""

SUMMARY_EN = """Write the summary for the meeting "{title}" ({meeting_date}) in English.
4-7 sentences, plain and specific: what was decided, who owns what, what is still open.
Base it only on these extracted items:
{items}
Return JSON: {{"summary": "..."}}"""

SUMMARY_AR = """اكتب ملخص اجتماع "{title}" ({meeting_date}) باللغة العربية الفصحى الواضحة.
من 4 إلى 7 جمل: ما الذي تقرر، ومن المسؤول عن ماذا، وما الذي ما زال مفتوحاً.
اكتب أسماء الأشخاص والمنتجات والمصطلحات التقنية كما وردت (يمكن إبقاء المصطلحات الإنجليزية بحروف لاتينية).
لا تستخدم أي لغة أخرى غير العربية (مع المصطلحات الإنجليزية فقط عند الحاجة). اعتمد فقط على هذه البنود:
{items}
Return JSON: {{"summary": "..."}}"""
