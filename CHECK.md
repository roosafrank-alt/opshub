# CHECK: fact-check and fix one lesson (you did NOT write it)
Lesson: /home/claude/opshub/ground_lessons/<CODE>.json. Read /home/claude/work/SPEC.md (and RFSPEC.md for RF.*) so you know what a good one is.
You are the independent checker. Do not trust the writer. Fix problems directly in the file; keep the voice and the structure.

1. Citations: for EVERY [[RES:PAGE|LABEL]] read the page (`faa.py page RES PAGE`) and confirm the cited sentence is on that page and says what the lesson says. Fix wrong pages and labels, remove unsupported claims.
2. Numbers: recompute every number, calc answer, table value, tolerance in Python or from the source chart. Fix rounding slips and answers outside their own tolerance.
3. Regulations: check each 14 CFR / 49 CFR 830 statement against the current text in res 60 (and AIM text). If a handbook (PHAK/AFH/IFH/AIH) says something older, teach the CFR/AIM, add a cfi_notes entry naming the outdated line.
4. ACS (Private ACS text for comparisons: /home/claude/work/faatext/private_airplane_acs_6-4.txt): (Commercial) every tolerance matches `faa.py grepCAACS`; every element code is covered; ASEL only.
5. Questions: each quiz/final_quiz/game question has exactly ONE defensible answer; check wrong options are really wrong; no duplicates between quiz, final_quiz and games; label pins sit on the right thing (render the figure page, Read it) and the answer is not printed beside its pin.
6. Figures: each figure matches its caption and the page; alt text is true; caption names the real figure number and source page.
7. Style: no em/en dashes, no "TL;DR", simple words, no verbatim FAA paragraphs.
8. validate.py prints CHECK lines for citations whose label number is not in the page text (some pages, e.g. AFH 18-8, have no printed footer). Read each such page and its neighbors and keep the correct printed label (e.g. AFH 18-8); never weaken a label to silence the check.
9. Run `python3 /home/claude/work/validate.py lesson ground_lessons/<CODE>.json` until OK.

Reply in at most 8 lines: how many fixes, the biggest 2-3 (what was wrong, what it says now), and anything you could not verify.
