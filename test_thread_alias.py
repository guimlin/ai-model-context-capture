"""What the conversations get called (issue #3): the alias built from what a thread
asked for, and how a thread filter turns what someone types into those threads.

Run with:  python -m unittest test_thread_alias
"""

import json
import unittest

import ctxlink
import ctxstore


class FakeSource:
    """Enough of ctxstore.Capture for ctxlink.attach: hand back a payload by name."""

    def __init__(self, payloads):
        self.payloads = payloads

    def payload(self, name):
        return self.payloads.get(name)


def round_record(seq, body, headers=None):
    """One captured round, the way the log writes it, plus its payload text."""
    name = "payloads/req-%06d.json" % seq
    if headers is None:
        headers = {"user-agent": "claude-cli/1.0.28"}
    record = {"seq": seq, "type": "request", "api": "anthropic-messages", "ts": "2026-10-20T10:00:00",
              "payload_file": name, "headers": headers}
    return record, {name: body}


def anthropic(system, messages):
    """A /v1/messages body: the root prompt sits outside the message list, so these
    rounds only chain when their messages really do line up."""
    body = {"messages": messages}
    if system:
        body["system"] = system
    return json.dumps(body)


def build(specs, headers=None):
    """specs: [(seq, body_json)] -> (records, reqs, order, source) for attach/choose."""
    records, payloads = [], {}
    for seq, body in specs:
        record, payload = round_record(seq, body, headers)
        records.append(record)
        payloads.update(payload)
    reqs = {record["seq"]: record for record in records}
    order = [record["seq"] for record in records]
    return records, reqs, order, FakeSource(payloads)


class AliasFromWords(unittest.TestCase):
    def test_the_words_that_say_what_it_is_about(self):
        self.assertEqual(
            ctxlink.thread_alias("The build is failing on Node 22. Find out why and fix it."),
            "build-failing-node-22")

    def test_the_clients_tag_blocks_are_skipped(self):
        text = ("<system-reminder>Always answer in one word.</system-reminder>"
                "<env># IDE Context\nopen file: src/auth.ts\n</env>"
                "Refactor the auth middleware to use JWT")
        self.assertEqual(ctxlink.thread_alias(text), "refactor-auth-middleware")

    def test_it_stops_at_a_word_boundary(self):
        name = ctxlink.thread_alias("Add a --dry-run flag to the CLI and cover it with tests")
        self.assertLessEqual(len(name), ctxlink.MAX_ALIAS)
        self.assertFalse(name.endswith("-"))
        self.assertEqual(name, "add-dry-run-flag-cli")

    def test_a_single_long_word_is_cut(self):
        name = ctxlink.thread_alias("Supercalifragilisticexpialidocious")
        self.assertEqual(name, "supercalifragilisticexpi")
        self.assertEqual(len(name), ctxlink.MAX_ALIAS)

    def test_nothing_to_say_gives_nothing(self):
        self.assertEqual(ctxlink.thread_alias(""), "")
        self.assertEqual(ctxlink.thread_alias(None), "")
        self.assertEqual(ctxlink.thread_alias("<env></env>"), "")

    def test_names_are_short_and_typeable(self):
        for sentence in (
                "Which module holds the retry logic, and is it covered by tests?",
                "<env># User Prompt</env>Please look at the failing snapshot in Checkout.test.tsx",
                "Add a --dry-run flag to the CLI and test it.",
                "c:\\repo\\src\\build.js line 14 throws TypeError: fs.cpSync is not a function"):
            name = ctxlink.thread_alias(sentence)
            self.assertTrue(name, sentence)
            self.assertLessEqual(len(name), ctxlink.MAX_ALIAS, sentence)
            self.assertNotIn(" ", name, sentence)
            self.assertEqual(name, name.lower(), sentence)


class NamesComeFromTheConversation(unittest.TestCase):
    def test_two_conversations_are_named_after_what_they_asked(self):
        records, reqs, order, source = build([
            (1, anthropic("You are a coding agent.",
                          [{"role": "user", "content": "The build is failing on Node 22. Why?"}])),
            (2, anthropic("You are a coding agent.",
                          [{"role": "user", "content": "Add a --dry-run flag to the CLI"}])),
        ])
        links = ctxlink.attach(records, source)
        threads = links["_threads"]
        self.assertEqual([thread["name"] for thread in threads],
                         ["build-failing-node-22", "add-dry-run-flag-cli"])
        for seq, link in links.items():
            if seq == "_threads":
                continue
            self.assertEqual(link["thread"], threads[link["thread_id"]]["name"])

    def test_a_follow_up_round_keeps_its_threads_name(self):
        first = [{"role": "user", "content": "The build is failing on Node 22. Why?"}]
        second = first + [{"role": "assistant", "content": "fs.cpSync is missing on Node 22."},
                          {"role": "user", "content": "Then patch the copy step."}]
        records, _, _, source = build([
            (1, anthropic("You are a coding agent.", first)),
            (2, anthropic("You are a coding agent.", second)),
        ])
        links = ctxlink.attach(records, source)
        self.assertEqual(links[2]["parent"], 1)
        self.assertEqual(links[2]["thread"], "build-failing-node-22")
        self.assertEqual(links[1]["thread"], links[2]["thread"])

    def test_tool_output_does_not_name_a_conversation(self):
        records, _, _, source = build([
            (1, anthropic("You are Claude Code, an agent pair-programming in a TypeScript repo.",
                          [{"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": "tu_1",
                               "content": "npm test: 3 failures in the payment module"}]}])),
        ])
        threads = ctxlink.attach(records, source)["_threads"]
        self.assertTrue(threads[0]["name"].startswith("claude-code"))

    def test_two_conversations_that_open_alike_are_told_apart(self):
        records, _, _, source = build([
            (1, anthropic("You work in repo A.",
                          [{"role": "user", "content": "Fix the flaky test in test_login.py"}])),
            (2, anthropic("You work in repo B.",
                          [{"role": "user", "content": "Fix the flaky test in test_signup.py"}])),
        ])
        threads = ctxlink.attach(records, source)["_threads"]
        self.assertEqual([thread["name"] for thread in threads],
                         ["fix-flaky-test", "fix-flaky-test-2"])

    def test_a_round_with_nothing_to_say_still_gets_a_name(self):
        records, _, _, source = build([(1, json.dumps({"model": "iq3_s"}))], headers={})
        threads = ctxlink.attach(records, source)["_threads"]
        self.assertEqual(threads[0]["name"], "thread-1")

    def test_a_round_whose_payload_is_gone_is_not_named(self):
        record = {"seq": 1, "type": "request", "api": "openai-chat", "payload_file": None,
                  "headers": {"user-agent": "opencode/0.5.9"}}
        links = ctxlink.attach([record], FakeSource({}))
        self.assertEqual(links[1]["thread"], "?")
        self.assertEqual(links["_threads"], [])


class ThreadFilters(unittest.TestCase):
    THREADS = [{"id": 0, "name": "build-failing-node-22", "rounds": [1, 2, 3]},
               {"id": 1, "name": "add-dry-run-flag-cli", "rounds": [4, 5]},
               {"id": 2, "name": "retry-the-flaky-test", "rounds": [6]}]

    def names(self, spec):
        return [thread["name"] for thread in ctxlink.match_threads(self.THREADS, spec)]

    def test_a_name_named_in_full(self):
        self.assertEqual(self.names("add-dry-run-flag-cli"), ["add-dry-run-flag-cli"])

    def test_case_does_not_matter(self):
        self.assertEqual(self.names("Add-Dry-Run-Flag-CLI"), ["add-dry-run-flag-cli"])

    def test_the_start_of_a_name_is_enough_when_it_points_at_one(self):
        self.assertEqual(self.names("build-failing"), ["build-failing-node-22"])

    def test_several_threads_at_once(self):
        self.assertEqual(self.names("build-failing, retry"),
                         ["build-failing-node-22", "retry-the-flaky-test"])

    def test_a_start_that_points_at_two_is_not_a_guess(self):
        with self.assertRaises(ValueError) as caught:
            ctxlink.match_threads([{"id": 0, "name": "fix-login", "rounds": []},
                                   {"id": 1, "name": "fix-logout", "rounds": []}], "fix")
        self.assertIn("fix-login", str(caught.exception))
        self.assertIn("fix-logout", str(caught.exception))

    def test_a_name_that_is_not_here_says_what_is(self):
        with self.assertRaises(ValueError) as caught:
            ctxlink.match_threads(self.THREADS, "payments-refund")
        message = str(caught.exception)
        self.assertIn("payments-refund", message)
        for thread in self.THREADS:
            self.assertIn(thread["name"], message)

    def test_an_empty_spec_asks_for_nothing(self):
        self.assertEqual(ctxlink.match_threads(self.THREADS, ""), [])
        self.assertEqual(ctxlink.match_threads(self.THREADS, " , "), [])

    def test_choose_takes_the_rounds_of_the_named_conversation(self):
        first = [{"role": "user", "content": "The build is failing on Node 22. Why?"}]
        follow = first + [{"role": "assistant", "content": "fs.cpSync is missing."},
                          {"role": "user", "content": "Patch the copy step."}]
        records, reqs, order, source = build([
            (1, anthropic("You are a coding agent.", first)),
            (2, anthropic("You are a coding agent.", follow)),
            (3, anthropic("You are a coding agent.",
                          [{"role": "user", "content": "Add a --dry-run flag to the CLI"}])),
        ])
        self.assertEqual(ctxstore.choose(reqs, order, threads="build-failing-node", cap=source),
                         [1, 2])
        self.assertEqual(ctxstore.choose(reqs, order, threads="add-dry-run", cap=source), [3])
        self.assertEqual(ctxstore.choose(reqs, order, threads="", cap=source), [1, 2, 3])

    def test_choose_says_so_when_the_name_is_wrong(self):
        records, reqs, order, source = build([
            (1, anthropic("You are a coding agent.",
                          [{"role": "user", "content": "The build is failing on Node 22. Why?"}]))])
        with self.assertRaises(ValueError) as caught:
            ctxstore.choose(reqs, order, threads="payments-refund", cap=source)
        self.assertIn("build-failing-node-22", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
