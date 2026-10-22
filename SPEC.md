# SPEC: writing one Flight Academy ground school lesson

You write ONE lesson file: /home/claude/opshub/ground_lessons/<CODE>.json (e.g. CA.V.A.json).
Work dir: /home/claude/work. Repo: /home/claude/opshub (branch chat/ir-commercial-ground-school). Do not touch git.

## Models to copy (read them first)
- ground_lessons/CA.IV.D.json (best Commercial example) and one more CA.* lesson of the same Area if it exists.
- Same format as the Private lessons (PA.*.json). Write FRESH content, never copy text.

## Tools
- `python3 /home/claude/work/faa.py page RES PAGE`   text of a source page (PDF page number)
- `python3 /home/claude/work/faa.py grep "regex" [RES]`   find where something is said
- Private ACS text (for private vs commercial comparisons): /home/claude/work/faatext/private_airplane_acs_6-4.txt (grep it)
- `python3 /home/claude/work/faa.py ids`   resource ids (1=PHAK 23=IFH 24=AFH 28=AWH 29=IPH 30=RMH 22=W&B 25=AIH 33/34=AIM 60=14 CFR combined 2=Chart Users' Guide, ACs: see ids)
- `python3 /home/claude/work/faa.py grepCAACS "CA.V.A"`   Commercial ACS lines (tolerances are in the Skills S elements)
- (CHECK lines in validate output are hints, not failures: keep the true printed label, e.g. "AFH 18-8", even when a page has no footer.)
- `python3 /home/claude/work/validate.py lesson ground_lessons/<CODE>.json`   MUST end with OK (WARN lines are fine)
- Figures (you do this yourself): first decide pages from the text (faa.py grep "Figure 10-" 24 etc.). Render them on the user's Mac with
  mcp__remote-devices__device_bash: `python3 mnt/Claude/Projects/flight-academy-lessons/tools/render.py 24:194 24:195`   (RES:PAGE, PDF page numbers; run from the shell's start folder, it has mnt/)
  then bring them here with mcp__remote-devices__device_stage_files paths ["/Users/frankroosa/Documents/Claude/Projects/flight-academy-lessons/pages/24-194.jpg", ...]
  (they land in /mnt/user-data/uploads/Claude/Projects/flight-academy-lessons/pages/). Request all pages in ONE render call and ONE stage call. Look at each with Read.
  Crop with `python3 /home/claude/work/fig.py <page.jpg> X0 Y0 X1 Y1 <CODE>-<n>.jpg` (fractions of page width/height; writes 1030 px wide into /home/claude/opshub/static/lessons/). Check the result with Read.
  FAA figures are public domain. Never use a figure whose number/caption you could not read on the page.

## File shape (JSON object, keys exactly)
task_code, task_title, area, minutes (80-95), objective (1-2 sentences, "By the end of this lesson you can ..."), why (2-3 sentences),
prereading: 6-7 x {res, pages:[n,...], printed, what, label}   (res is a STRING id; label like "AFH 9-23 to 9-24")
warmup: a "Think first:" question paragraph
key_terms: 10-12 x {term, def}
chunks: 9-14 x {title, kind: knowledge|risk|skills, codes:[ACS element codes], minutes, body:[ blocks ], check:{q,a}}
  blocks (exactly these five): {"p": text} | {"bullets":[...]} | {"table":{"head":[...],"rows":[[...]]}} | {"figure":"f1"} | {"callout":{"type":"reg|warn|tip|memory","text":...}}  (reg = a rule, warn = danger/mistake, tip = technique, memory = memory aid)
  check: {"q":..., "a":...} one short question and answer at the end of each chunk
  Last chunk is usually "Common errors and fixes" (kind skills). Chunks cover EVERY ACS element code of the Task (K, R and S).
figures: 5-7 x fN: {file, res, page, caption, alt, source}
scenario: {title, codes, setup, questions:[{q,a}]}  (6 questions, answers cite sources)
skills: one per ACS S element: {code, do, practice}   ("do" says what the checkride examiner sees, with the ACS tolerance in plain words)
quiz: 10 x {q, options:[4], answer:index, why}   (spread through the lesson as quick checks)
final_quiz: 13 x same shape (different questions from quiz; harder, scenario style)
takeaways: 6-7 short strings; cfi_notes: 7-9 strings (teaching tips and "the sources disagree/are silent" notes); gaps: 4-6 strings (things the sources do not give; POH/AFM or CFI needed)
activities: 7 hand-written games with keys h1..h7, each with "after": chunk number, types used once each where it fits:
  label (figure + pins {x,y in 0-100,label}), sort (buckets, items {t,b}), order (steps), tf (cards {s,a,why}), match (pairs {l,r}),
  calc (items {q,answer,tol,unit,why}: numbers worked from source charts/formulas), decide (intro + steps {s,options,...}: a flight that unfolds one decision at a time).
  decide = {type,key,after,title,intro,steps:[{s,options:[4 strings],answer:index,why}]} (4-5 steps; each why cites sources).
  Skip a game type only when it truly does not fit the topic.

## Content rules (ASEL only, Commercial level; Refresher: see RFSPEC.md)
- Single-engine land only. Skip seaplane/multiengine-only elements. A passing remark is fine.
- Voice: plain, simple words, short sentences, no jargon without a definition. NO em dashes or en dashes anywhere (use commas, periods or "to"). Never write "TL;DR".
- Every fact, number, tolerance, regulation and procedure carries a citation [[RES:PAGE|LABEL]] on the page where it is stated (LABEL like "AFH 9-23", "14 CFR 91.155", "AIM 4-3-8"). Use the PDF page number in the citation, the printed number in the label.
- Quote ACS tolerances exactly as the ACS says (grepCAACS). Say the private/commercial difference only when both ACS lines were read.
- When 14 CFR or the AIM disagrees with an older handbook line, TEACH the CFR/AIM and add a cfi_notes entry naming the outdated handbook line. Never teach the older line.
- Numbers you compute (calc items, worked examples) must be computed in Python, shown with the arithmetic in "why", and match the source chart or formula.
- If a source is silent, say so in gaps/cfi_notes. Do not invent. Anything that is your own reasoning is labeled as "application" and kept short.
- Questions must have exactly one right answer; wrong options plausible, not silly. Quiz and final_quiz questions must not repeat each other or the games.
- Label games: pins land on the thing named; do not print the answer on the figure next to its pin.
- Do not copy FAA text verbatim beyond a short quoted phrase.

Finish: run validate.py until OK, then reply in at most 3 lines: lesson code, what was written, anything the fact-checker should look at hardest.
