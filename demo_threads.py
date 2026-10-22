"""Demo traffic that shows how rounds group: separate agents, shared root prompt,
a root prompt that changes mid-session, and one history sent out twice."""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788"

# the last conversation deliberately reuses another conversation's root prompt, so
# "shares its root prompt with ..." has something to find. Fall back to our own text
# when the log is empty.
try:
    A_ROOT = json.load(open("logs/payloads/req-000001.json",
                            encoding="utf-8"))["messages"][0]["content"]
except OSError:
    A_ROOT = ("You are a coding agent working in C:/demo/src. Rules: never touch files "
              "outside src; run tests before finishing; keep diffs small; prefer tools "
              "over guessing; answer in one short sentence.")

CLAUDE_ROOT = ("You are Claude Code, an agent pair-programming in a TypeScript repo. "
               "Prefer editing existing files over creating new ones, run the test suite "
               "after each change, and never commit without being asked.")
CODEX_ROOT = ("You are Codex, a coding agent running in a sandboxed container. Work from "
              "the repository root, use the shell tool for everything, and report the "
              "commands you ran at the end of the turn.")


def post(path, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream" if body.get("stream") else "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except Exception as exc:
        print("  ! %s" % exc)
        return ""
    rid = ""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except Exception:
            continue
        if obj.get("id"):
            rid = obj["id"]
        inner = obj.get("response")
        if isinstance(inner, dict) and inner.get("id"):
            rid = inner["id"]
    return rid


def claude_messages(extra):
    msgs = [{"role": "user", "content": "The build is failing on Node 22. Find out why and fix it."}]
    if extra:
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "bash",
             "input": {"command": "npm test 2>&1 | tail -30"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": "TypeError: fs.cpSync is not a function\n    at copy (build.js:14:7)"}]})
    return msgs


TOOLS_CLAUDE = [
    {"name": "bash", "description": "Run a shell command and return its output",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "edit_file", "description": "Replace an exact string in a file",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                     "new": {"type": "string"}},
                      "required": ["path", "old", "new"]}},
]
TOOLS_CODEX = [
    {"type": "function", "name": "shell", "description": "Run a command in the sandbox",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                    "required": ["command"]}},
]

print("a Claude Code conversation about a build failing on Node 22: /v1/messages,")
print("  root prompt changes on the third round")
history = claude_messages(False)
for round_no in range(1, 5):
    root = CLAUDE_ROOT if round_no < 3 else CLAUDE_ROOT + "\nReminder: run `npm test` before finishing."
    body = {"model": "iq3_s", "system": [{"type": "text", "text": root}], "messages": history,
            "tools": TOOLS_CLAUDE, "max_tokens": 80, "stream": True}
    rid = post("/v1/messages", body, {"user-agent": "claude-cli/1.0.28 (external, cli)",
                                      "x-app": "cli", "anthropic-version": "2023-06-01"})
    print("  round %d sent (%d messages)%s" % (round_no, len(history), " id=%s" % rid if rid else ""))
    if round_no == 1:
        history = claude_messages(True)
    # round 4 deliberately resends exactly what round 3 sent

print("a Codex conversation about adding a --dry-run flag: /v1/responses, chained by")
print("  previous_response_id")
prev, items = None, [{"role": "user", "content": "Add a --dry-run flag to the CLI and test it."}]
for round_no in range(1, 4):
    body = {"model": "iq3_s", "instructions": CODEX_ROOT, "input": items,
            "tools": TOOLS_CODEX, "stream": True}
    if prev:
        body["previous_response_id"] = prev
    rid = post("/v1/responses", body, {"user-agent": "codex_cli_rs/0.46.0 (Mac OS 14.0.0)"})
    print("  round %d sent (%d items) -> %s" % (round_no, len(items), rid or "no id back"))
    if rid:
        prev = rid
    items = items + [{"role": "assistant", "content": "ran: cargo build --release; edited src/cli.rs"}]

print("an OpenCode conversation about retry logic that reuses the root prompt of the")
print("  first round in the log")
conv = [{"role": "system", "content": A_ROOT},
        {"role": "user", "content": "Which module holds the retry logic, and is it covered?"}]
for round_no in range(1, 3):
    body = {"model": "iq3_s", "messages": conv, "tools": [
        {"type": "function", "function": {"name": "grep", "description": "Search the repo",
         "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}}],
        "max_tokens": 60, "stream": True}
    post("/v1/chat/completions", body, {"user-agent": "opencode/0.5.9", "x-app": "opencode"})
    conv = conv + [{"role": "assistant", "content": "retry.py, covered by tests/test_retry.py"},
                   {"role": "user", "content": "Show the backoff constants."}]

print()
print("those rounds are now in the log. Look at them with:")
print("    .\\Start-ContextMonitor.ps1    tab 3 Threads shows the four conversations")
print("    python ctxpage.py --md report.md")
