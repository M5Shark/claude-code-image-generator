#!/usr/bin/env python3
"""Stand-in for `codex exec --json` used by the tests.

Logs its arguments and prompt, then writes an image where Codex would
($CODEX_HOME/generated_images/<thread id>/) and prints Codex-style JSON events.
"""
import json
import os
import re
import shutil
import sys
import uuid

args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt}) + "\n")

if os.environ.get("FAKE_FAIL"):
    print(json.dumps({"type": "thread.started", "thread_id": "fail"}))
    print(json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized: please log in"}}))
    sys.exit(1)

images = os.environ["FAKE_IMG_DIR"]
if "#00FF00" in prompt:
    source = os.path.join(images, "green.png")
elif "#FF00FF" in prompt:
    source = os.path.join(images, "magenta.png")
else:
    source = os.path.join(images, "plain.png")

thread_id = str(uuid.uuid4())
out_dir = os.path.join(os.environ["CODEX_HOME"], "generated_images", thread_id)
os.makedirs(out_dir)
match = re.search(r"exactly (\d+) separate", prompt)
for i in range(int(match.group(1)) if match else 1):
    shutil.copy(source, os.path.join(out_dir, f"exec-{i}.png"))

print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
