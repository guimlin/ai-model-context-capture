"""Work out how the captured rounds relate to each other.

The proxy keeps every request body, which is enough to answer:

  - is this round part of the same conversation as an earlier one?
  - which round does it continue, and how many messages were added since?
  - same root (system) prompt as the round it continues, or did that change?
  - same tool definitions, i.e. the same agent configuration?
  - which client made the call, as far as the headers show?

Method: the message list is hashed as a chain, so hash(k) covers the first k
messages. A round whose history starts with an earlier round's entire history is a
continuation of it - that is what an agent does when it sends turn N+1 - and the
newest such match wins, so a retry of a turn hangs off the same parent instead of
starting a thread of its own. Tool call ids are normalised away first, because
clients are free to regenerate them. Two chains are kept: one over the whole
request, one over the conversation without its leading system messages, so a
session that changed its root prompt part way through is still recognised as the
same thread - and reported as a root change.
"""

import hashlib
import json
import os

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _fp(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def thread_name(index):
    """A, B, ... Z, then AA, BB, ... for the (unlikely) case of more than 26."""
    return ALPHABET[index % 26] * (index // 26 + 1)


def _text_of(content):
    """Plain text of a message body, however it is shaped."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("text", "input_text", "output_text"):
                    out.append(part.get("text") or "")
                elif part.get("type") in ("input_image", "image_url", "image"):
                    out.append("[image]")
                elif part.get("content") is not None:
                    out.append(_text_of(part.get("content")))
        return "\n".join(out)
    if isinstance(content, dict):
        return json.dumps(content, sort_keys=True, ensure_ascii=False)
    return str(content)


def _blocks(content):
    """Canonical blocks for a body: typed, ordered, ids removed."""
    if isinstance(content, str):
        return [["t", content]] if content else []
    if not isinstance(content, list):
        text = _text_of(content)
        return [["t", text]] if text else []
    out = []
    for index, part in enumerate(content):
        if isinstance(part, str):
            out.append(["t", part])
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "tool_use":
            out.append(["u", index, part.get("name"),
                        json.dumps(part.get("input"), sort_keys=True, ensure_ascii=False)])
        elif kind == "tool_result":
            out.append(["r", index, _text_of(part.get("content"))])
        elif kind in ("image", "input_image", "image_url"):
            out.append(["i"])
        else:
            text = part.get("text")
            out.append(["t", _text_of(part) if text is None else text])
    return out


def _canon_message(message):
    """One message as canonical JSON, with tool call ids normalised away."""
    if not isinstance(message, dict):
        return json.dumps(message, sort_keys=True, ensure_ascii=False)
    role = message.get("role") or message.get("type") or "?"
    body = _blocks(message.get("content"))
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") or call
        arguments = function.get("arguments")
        if arguments is None:
            arguments = json.dumps(call.get("input"), sort_keys=True, ensure_ascii=False)
        calls.append([index, function.get("name") or call.get("name"), arguments])
    paired = 1 if (message.get("tool_call_id") or message.get("tool_use_id")) else 0
    return json.dumps([role, body, calls, paired], sort_keys=True, ensure_ascii=False)


def _messages_of(body):
    """The conversation array, whichever API shape the request used."""
    for key in ("messages", "input"):
        if isinstance(body.get(key), list):
            return [item for item in body[key] if isinstance(item, dict)]
    return []


def _root_count(messages):
    count = 0
    while count < len(messages) and messages[count].get("role") in ("system", "developer"):
        count += 1
    return count


def root_prompt(body, messages):
    """The root (system) text, wherever that API keeps it."""
    system = body.get("system")
    if isinstance(system, list):
        return "\n".join(part.get("text", "") for part in system if isinstance(part, dict))
    if isinstance(system, str) and system:
        return system
    if isinstance(body.get("instructions"), str):
        return body["instructions"]
    if messages and messages[0].get("role") in ("system", "developer"):
        return _text_of(messages[0].get("content"))
    return ""


FULL_PROMPT_CAP = 4000        # most of one prompt kept around for the hover box


def _human_text(message):
    """Text of a message a person wrote, or "" when that message is plumbing.

    A user message is only worth showing when it carries text the human typed:
    Claude clients send tool results as user messages, and those are the agent's
    own output coming back, not a prompt."""
    if (message.get("role") or message.get("type")) != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        keep = []
        for part in content:
            if isinstance(part, str):
                keep.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("tool_result", "tool_use", "function_call_output"):
                return ""
            if kind in ("image", "input_image", "image_url"):
                keep.append("[image]")
            elif part.get("text") is not None or kind in ("text", "input_text", "output_text"):
                keep.append(part.get("text") or "")
        return "\n".join(keep).strip()
    return ""


def user_prompts(messages):
    """(the first thing a person typed, the newest thing a person typed).

    System / developer messages, tool definitions and tool results are all left
    out - this is the prompt a human actually wrote, which is what the page shows
    in place of the root prompt's text."""
    first = last = ""
    for message in messages:
        text = _human_text(message)
        if not text:
            continue
        if not first:
            first = text
        last = text
    return first, last


def tool_fingerprint(body):
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return "", 0
    canonical = []
    for tool in tools:
        if isinstance(tool, dict):
            function = tool.get("function") or tool
            canonical.append(json.dumps(
                {"n": function.get("name") or tool.get("name"), "d": function.get("description"),
                 "p": function.get("parameters") or function.get("input_schema")},
                sort_keys=True, ensure_ascii=False))
        else:
            canonical.append(str(tool))
    return _fp("\n".join(sorted(canonical))), len(canonical)


def _chain_of(items):
    chain, acc = [], ""
    for text in items:
        acc = _fp(acc + text)
        chain.append(acc)
    return chain


def fingerprint(body):
    """Everything needed to relate one round to the others."""
    messages = _messages_of(body)
    canon = [_canon_message(message) for message in messages]
    skipped = _root_count(messages)
    root = root_prompt(body, messages)
    opened, asked = user_prompts(messages)
    tools_fp, tool_count = tool_fingerprint(body)
    hints = {}
    for key in ("user", "prompt_cache_key", "previous_response_id", "metadata"):
        value = body.get(key)
        if isinstance(value, dict):
            value = ";".join("%s=%s" % item for item in sorted(value.items())
                             if isinstance(item[1], (str, int, float)))
        if value:
            hints[key] = str(value)[:120]
    return {"n_msgs": len(messages), "root_skipped": skipped,
            "chain": _chain_of(canon), "body_chain": _chain_of(canon[skipped:]),
            "last_fp": _chain_of(canon)[-1] if canon else "",
            "root_fp": _fp(root) if root else "", "root_chars": len(root),
            "root_head": " ".join(root.split())[:160],
            "open_prompt": opened[:FULL_PROMPT_CAP], "open_chars": len(opened),
            "open_head": " ".join(opened.split())[:120],
            "user_prompt": asked[:FULL_PROMPT_CAP], "user_chars": len(asked),
            "user_head": " ".join(asked.split())[:120],
            "tools_fp": tools_fp, "n_tools": tool_count, "hints": hints}


def client_of(record):
    """What made the call, as far as the headers show."""
    headers = {str(key).lower(): value for key, value in (record.get("headers") or {}).items()}
    parts = []
    for key in ("user-agent", "x-app", "x-stainless-runtime", "x-coding-assistant"):
        if headers.get(key):
            parts.append(str(headers[key])[:60])
    for key, value in sorted(headers.items()):
        if value and any(word in key for word in ("session", "conversation", "thread")):
            parts.append("%s=%s" % (key, str(value)[:60]))
    return " ".join(parts) or "-"


def _unknown(record):
    return {"thread": "?", "thread_id": None, "parent": None, "added": None, "shared": 0,
            "root_fp": "", "root_head": "", "root_chars": 0, "open_prompt": "", "open_chars": 0,
            "open_head": "", "user_prompt": "", "user_chars": 0, "user_head": "",
            "tools_fp": "", "n_tools": 0,
            "n_msgs": 0, "hints": {}, "client": client_of(record), "root_changed": False,
            "tools_changed": False, "forked": [], "body_only": False, "chained_by_id": False,
            "same_history": []}


def logdir_join(root, name):
    return os.path.join(root, name)


def _text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def attach(reqs, source, resps=None):
    """Relate every request record to the rounds before it. Returns {seq: link},
    plus links["_threads"] describing each thread that was found. Pass the response
    records as well, so a Responses-API client that chains with
    previous_response_id links up even when it does not resend the earlier items.
    `source` is the log folder, or anything that can hand back a payload by name,
    such as an open ctxstore.Capture."""
    links, whole, body_only, threads = {}, {}, {}, []
    same_history, by_response_id = {}, {}
    for seq, record in (resps or {}).items():
        if isinstance(record, dict) and record.get("response_id"):
            by_response_id[record["response_id"]] = seq
    for record in reqs:
        seq = record.get("seq")
        body = None
        payload = record.get("payload_file")
        if payload:
            text = source.payload(payload) if hasattr(source, "payload") else _text(logdir_join(source, payload))
            try:
                body = json.loads(text) if text else None
            except Exception:
                body = None
        if not isinstance(body, dict):
            links[seq] = _unknown(record)
            continue
        info = fingerprint(body)
        parent, shared, match = None, None, ""
        chained = info["hints"].get("previous_response_id")
        if chained:
            hit = by_response_id.get(chained)
            if hit is not None and hit != seq:
                parent, shared, match = hit, None, "id"
        if parent is None:
            parent, shared, match = None, 0, "whole"
            for cut in range(info["n_msgs"] - 1, 0, -1):
                hit = whole.get(info["chain"][cut - 1])
                if hit is not None and hit != seq:
                    parent, shared, match = hit, cut, "whole"
                    break
        if parent is None and info["root_skipped"] and info["n_msgs"] > info["root_skipped"]:
            offset = info["root_skipped"]
            for cut in range(info["n_msgs"] - offset - 1, 0, -1):
                hit = body_only.get(info["body_chain"][cut - 1])
                if hit is not None and hit != seq:
                    parent, shared, match = hit, cut + offset, "body"
                    break
        thread_id = links[parent]["thread_id"] if parent is not None and parent in links else None
        if thread_id is None:
            thread_id = len(threads)
            threads.append({"id": thread_id, "rounds": [], "root_fp": info["root_fp"],
                            "root_head": info["root_head"], "root_chars": info["root_chars"],
                            "open_prompt": info["open_prompt"], "open_chars": info["open_chars"],
                            "open_head": info["open_head"],
                            "tools_fp": info["tools_fp"], "n_tools": info["n_tools"],
                            "client": client_of(record), "first": seq, "last": seq})
        threads[thread_id]["rounds"].append(seq)
        threads[thread_id]["last"] = seq
        parent_link = links.get(parent) or {}
        root_changed = match == "body" or bool(
            parent_link.get("root_fp") and parent_link["root_fp"] != info["root_fp"])
        tools_changed = bool(parent_link.get("tools_fp") and parent_link["tools_fp"] != info["tools_fp"])
        links[seq] = {"thread": thread_name(thread_id), "thread_id": thread_id, "parent": parent,
                      "added": None if shared is None else info["n_msgs"] - shared,
                      "shared": shared, "chained_by_id": match == "id",
                      "root_fp": info["root_fp"], "root_head": info["root_head"],
                      "root_chars": info["root_chars"],
                      "open_prompt": info["open_prompt"], "open_chars": info["open_chars"],
                      "open_head": info["open_head"],
                      "user_prompt": info["user_prompt"], "user_chars": info["user_chars"],
                      "user_head": info["user_head"],
                      "tools_fp": info["tools_fp"],
                      "n_tools": info["n_tools"], "n_msgs": info["n_msgs"], "hints": info["hints"],
                      "client": client_of(record), "root_changed": root_changed,
                      "tools_changed": tools_changed, "forked": [], "body_only": match == "body",
                      "same_history": []}
        if parent is not None and parent in links:
            links[parent]["forked"].append(seq)
        if info["last_fp"]:
            same_history.setdefault(info["last_fp"], []).append(seq)
            whole.setdefault(info["last_fp"], seq)
        if info["root_skipped"] and info["body_chain"]:
            body_only.setdefault(info["body_chain"][-1], seq)
    for fingerprints, seqs in same_history.items():
        if len(seqs) > 1:
            for s in seqs:
                if s in links:
                    links[s]["same_history"] = [x for x in seqs if x != s]
    for thread in threads:
        thread["name"] = thread_name(thread["id"])
        if not thread["open_prompt"]:      # a first round that opened with tool plumbing
            for s in thread["rounds"]:
                if links[s]["open_prompt"]:
                    thread["open_prompt"] = links[s]["open_prompt"]
                    thread["open_chars"] = links[s]["open_chars"]
                    thread["open_head"] = links[s]["open_head"]
                    break
        thread["repeats"] = sorted(s for s in thread["rounds"] if links[s]["same_history"])
        thread["rounds_with_root_change"] = [s for s in thread["rounds"] if links[s]["root_changed"]]
        thread["rounds_with_tool_change"] = [s for s in thread["rounds"] if links[s]["tools_changed"]]
        thread["forks"] = sorted(s for s in thread["rounds"] if len(links[s]["forked"]) > 1)
    links["_threads"] = threads
    return links
