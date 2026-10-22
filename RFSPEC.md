# RFSPEC: changes for the Part 91 Refresher course (RF.*)  (these win over SPEC.md)
- Course: "Part 91 Refresher" for already-certificated pilots. NO FAA ACS exists: the outline is /home/claude/opshub/ground_lessons/courses/RF.json.
  Find your Task there (areas[].tasks[]): it gives title, objective, references (the main sources) and element codes
  (RF.<Area>.<Task>.K1.., R1.., S1..). Cover EVERY element code in chunks/skills/scenario (validate.py checks).
- skills: one entry per S element ("do" = what a pilot must be able to do, no ACS tolerances; "practice" = how to practice it).
- Skip all ACS-tolerance talk. Audience: pilots with a certificate: shorter lesson is fine (minutes 45-60, 8-11 chunks), still 7 games, quiz 9, final_quiz 12-13, 5-6 figures.
- Sources: 14 CFR (res 60 = combined Parts 1, 21, 47, 61, 67, 91...; check pages with faa.py grep), 49 CFR 830 (inside the General Aviation CFR text file: faa.py grep "830\." 60), AIM (33/34), the ACs in the outline's references, Chart Users' Guide (2).
- "Changed:" callouts only when the sources themselves show a change; never state a date you cannot see in a source page.
- Where the CFR is the rule, teach the CFR and call interpretation "interpretation". Keep "common mistake" callouts to ones the sources support.
