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

    async def chat(self, messages, tools, remaining):
        self.calls.append(list(messages))
        await asyncio.sleep(self.delay)
        if not self.replies:
            raise ProviderError("模拟模型故障")
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
        self.session = self.store.create_session("测试")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_with(self, provider, prompt="测试", settings=None):
        runner = AgentRunner(settings or self.settings, self.store, self.tools, provider)
        run = self.store.create_run(self.session["id"], prompt)
        runner.submit(run)
        await asyncio.gather(*list(runner.tasks.values()))
        return self.store.get_run(run["id"]), runner

    async def test_real_tool_loop_and_saved_evidence(self):
        p = FakeProvider([call("calculate", {"expression": "23*19"}),
                          {"role": "assistant", "content": "计算结果是437。"}])
        run, _ = await self.run_with(p)
        self.assertEqual(run["status"], "completed")
        events = self.store.events(run["id"])
        result = next(e["payload"]["result"] for e in events if e["kind"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertIn("437", str(result))
        self.assertEqual(p.calls[1][-1]["role"], "tool")
        self.assertEqual(run["usage"]["completion_tokens"], 60)
        self.assertEqual(self.store.messages(self.session["id"])[-1]["content"], "计算结果是437。")

    async def test_unknown_tool_and_escape_never_executed(self):
        p = FakeProvider([call("shell", {"command": "touch /tmp/unsafe"}),
                          call("write_file", {"path": "../escape.txt", "content": "bad"}),
                          {"role": "assistant", "content": "工具拒绝了操作。"}])
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
        run = self.store.create_run(self.session["id"], "稍后写入")
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
        p = FakeProvider([{"role": "assistant", "content": "should not happen"}])
        run, _ = await self.run_with(p, prompt="很长的问题" * 1500)
        self.assertEqual(run["status"], "failed")
        self.assertIn("上下文", run["error"])
        self.assertEqual(p.calls, [])


if __name__ == "__main__":
    unittest.main()
