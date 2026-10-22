#!/bin/bash
# Commit lesson work in progress and push to the review branch (never main).
cd /home/claude/opshub || exit 1
b=$(git rev-parse --abbrev-ref HEAD); [ "$b" = "chat/ir-commercial-ground-school" ] || { echo "wrong branch $b"; exit 1; }
git add ground_lessons static/lessons
git diff --cached --quiet && { echo "nothing new"; exit 0; }
git commit -q -m "WIP: ground school lessons (Commercial/Refresher, in progress)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011kt2quSSrJCnirPNEhV2W8"
git push -q origin HEAD:chat/ir-commercial-ground-school 2>&1 | tail -2
git log --oneline -1
