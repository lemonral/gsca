import asyncio
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.provider import ProviderError
from app.runtime import AgentRunner
from app.store import Store
from app.tools import ToolRegistry


class FakeProvider:
    def __init__(self, replies, delay=0):
        self.replies, self.delay, self.calls = list(replies), delay, []
        self.tool_sets = []

    async def chat(self, messages, tools, remaining):
        self.calls.append(list(messages))
        self.tool_sets.append(tools)
        await asyncio.sleep(self.delay)
        if not self.replies:
            raise ProviderError("Simulated model failure")
        message = self.replies.pop(0)
        return message, {"prompt_tokens": 100, "completion_tokens": 30}


def call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name))
        self.store = Store(Path(self.tmp.name))
        self.tools = ToolRegistry(Path(self.tmp.name) / "workspaces")
        self.session = self.store.create_session("Test")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_with(self, provider, prompt="Test", settings=None):
        runner = AgentRunner(settings or self.settings, self.store, self.tools, provider)
        run = self.store.create_run(self.session["id"], prompt)
        runner.submit(run)
        await asyncio.gather(*list(runner.tasks.values()))
        return self.store.get_run(run["id"]), runner

    async def test_real_tool_loop_and_saved_evidence(self):
        p = FakeProvider([call("calculate", {"expression": "23*19"}),
                          {"role": "assistant", "content": "The result is 437."}])
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "completed")
        events = self.store.events(run["id"])
        result = next(e["payload"]["result"] for e in events if e["kind"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertIn("437", str(result))
        self.assertEqual(p.calls[1][-1]["role"], "tool")
        self.assertEqual(run["usage"]["completion_tokens"], 60)
        self.assertEqual(self.store.messages(self.session["id"])[-1]["content"], "The result is 437.")

    async def test_unknown_tool_and_escape_never_executed(self):
        p = FakeProvider([call("shell", {"command": "touch /tmp/unsafe"}),
                          call("write_file", {"path": "../escape.txt", "content": "bad"}),
                          {"role": "assistant", "content": "The tool rejected the operation."}])
        run, _ = await self.run_with(p)
        results = [e["payload"]["result"] for e in self.store.events(run["id"]) if e["kind"] == "tool_result"]
        self.assertTrue(all(not r["ok"] for r in results))
        self.assertFalse((Path(self.tmp.name) / "escape.txt").exists())

    async def test_provider_failure_does_not_fabricate_reply(self):
        run, _ = await self.run_with(FakeProvider([]))
        self.assertEqual(run["status"], "failed")
        self.assertFalse(any(m["role"] == "assistant" for m in self.store.messages(self.session["id"])))

    async def test_cancel_before_model_completion(self):
        p = FakeProvider([call("write_file", {"path": "never.txt", "content": "bad"})], delay=5)
        runner = AgentRunner(self.settings, self.store, self.tools, p)
        run = self.store.create_run(self.session["id"], "Write later")
        runner.submit(run)
        await asyncio.sleep(.01)
        task = runner.tasks[run["id"]]
        runner.cancel(run["id"])
        await task
        self.assertEqual(self.store.get_run(run["id"])["status"], "cancelled")
        self.assertEqual(self.tools.execute("list_files", {}, self.session["id"])["files"], [])

    async def test_step_budget_blocks_late_side_effect(self):
        settings = Settings(Path(self.tmp.name), max_steps=1)
        p = FakeProvider([call("write_file", {"path": "never.txt", "content": "bad"})])
        run, _ = await self.run_with(p, settings=settings)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.tools.execute("list_files", {}, self.session["id"])["files"], [])

    async def test_context_window_rejects_oversize_input_before_model(self):
        for repeat in (300, 1500):
            with self.subTest(repeat=repeat):
                p = FakeProvider([{"role": "assistant", "content": "should not happen"}])
                run, _ = await self.run_with(p, prompt="A very long research question " * repeat)
                self.assertEqual(run["status"], "failed")
                self.assertIn("context window", run["error"])
                self.assertEqual(p.calls, [])

    async def test_non_english_response_is_rewritten_without_tools(self):
        p = FakeProvider([{"role": "assistant", "content": "\u4f60\u597d"},
                          {"role": "assistant", "content": "Hello from GSCA."}])
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(p.tool_sets[1], [])
        self.assertEqual(self.store.messages(self.session["id"])[-1]["content"], "Hello from GSCA.")
        self.assertEqual(sum(e["kind"] == "response_language_retry" for e in self.store.events(run["id"])), 1)

    async def test_language_retry_is_bounded_and_does_not_store_bad_reply(self):
        p = FakeProvider([{"role": "assistant", "content": "\u4f60\u597d"}] * 2)
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(p.calls), 2)
        self.assertFalse(any(m["role"] == "assistant" for m in self.store.messages(self.session["id"])))

    async def test_language_rewrite_cannot_execute_new_tools(self):
        p = FakeProvider([{"role": "assistant", "content": "\u4f60\u597d"},
                          call("write_file", {"path": "never.txt", "content": "bad"})])
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.tools.execute("list_files", {}, self.session["id"])["files"], [])

    async def test_verified_unicode_artifact_path_is_preserved(self):
        path = "\u6d4b\u8bd5.txt"
        p = FakeProvider([call("write_file", {"path": path, "content": "Verified data"}),
                          {"role": "assistant", "content": "Saved " + path + "."}])
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.tools.execute("read_file", {"path": path}, self.session["id"])["content"], "Verified data")
        self.assertFalse(any(e["kind"] == "response_language_retry" for e in self.store.events(run["id"])))


if __name__ == "__main__":
    unittest.main()
