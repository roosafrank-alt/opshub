# AREA: write one Area review (a mixed quiz across the Tasks of one Area)
File: /home/claude/opshub/ground_lessons/areas/<AREA>.json, e.g. CA.V.json. Model: ground_lessons/areas/IR.VI.json and IR.I.json.
Shape: {"area": "CA.V", "title": "Area V review: <Area name>", "intro": "1-2 sentences on what it mixes",
        "questions": [ {"task": "CA.V.A", "q", "options": [4], "answer": index, "why", "figure"?: "CA.V.A:f3"} ... ]}
- 20-25 questions, spread over all the Area's taught Tasks (ASEL only; for RF areas, all Tasks in courses/RF.json).
- Build ONLY from what the Area's lesson files (ground_lessons/<TASK>.json) now teach, after their fact-check. Never teach something the lessons do not.
- Questions must be NEW: not copies or close rewrites of any quiz, final_quiz or game question in those lessons. Many should join two Tasks in one realistic flight.
- Each "why" explains the right answer, says why a tempting wrong one is wrong, and cites sources [[RES:PAGE|LABEL]] (re-read the page with faa.py page to confirm).
- Exactly one defensible answer. Numbers computed in Python. A "figure" may reuse a lesson figure as "<TASK>:fN" only when the question really needs it.
- Style: plain words, no em/en dashes, no "TL;DR".
- Check: python3 /home/claude/work/validate.py area ground_lessons/areas/<AREA>.json must end OK.
Reply in at most 3 lines.
