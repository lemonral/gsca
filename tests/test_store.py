import concurrent.futures
import tempfile
import unittest
from app.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.session = self.store.create_session("计算任务")

    def test_persistence_and_incremental_events(self):
        sid = self.session["id"]
        run = self.store.create_run(sid, "计算42*2")
        one = self.store.event(run["id"], "tool_call", {"name": "calculate"})
        two = self.store.event(run["id"], "tool_result", {"result": 84})
        self.store.add_message(sid, "assistant", "84", run["id"])
        self.store.set_run(run["id"], status="completed", steps=2, usage={"total_tokens": 12})
        reopened = Store(self.tmp.name)
        self.assertEqual(reopened.get_run(run["id"])["usage"], {"total_tokens": 12})
        self.assertEqual([m["content"] for m in reopened.messages(sid)], ["计算42*2", "84"])
        self.assertEqual(reopened.events(run["id"], after=one["id"]), [two])
        self.assertEqual(reopened.list_runs(sid)[0]["status"], "completed")
        self.assertEqual(reopened.list_sessions()[0]["id"], sid)

    def test_active_run_unique_is_atomic_across_connections(self):
        sid = self.session["id"]
        def create(i):
            try:
                return self.store.create_run(sid, f"请求{i}")
            except ValueError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(8)))
        successful = [r for r in results if r]
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(self.store.messages(sid)), 1)
        self.store.set_run(successful[0]["id"], status="completed")
        self.assertEqual(self.store.create_run(sid, "后续请求")["status"], "queued")

    def test_recover_and_terminal_state_cannot_resurrect(self):
        sid = self.session["id"]
        first = self.store.create_run(sid, "旧任务")
        self.store.set_run(first["id"], status="completed")
        second = self.store.create_run(sid, "新任务")
        self.store.set_run(second["id"], status="running")
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get_run(first["id"])["status"], "completed")
        self.assertEqual(self.store.get_run(second["id"])["status"], "interrupted")
        self.assertEqual(self.store.events(second["id"])[0]["kind"], "interrupted")
        self.assertEqual(self.store.set_run(second["id"], status="running")["status"], "interrupted")
        self.assertEqual(self.store.recover(), 0)

    def test_missing_ids_and_session_consistency(self):
        with self.assertRaises(KeyError):
            self.store.create_run("missing", "hello")
        with self.assertRaises(KeyError):
            self.store.get_run("missing")
        with self.assertRaises(KeyError):
            self.store.events("missing")
        with self.assertRaises(KeyError):
            self.store.messages("missing")
        run = self.store.create_run(self.session["id"], "hello")
        second = self.store.create_session("另一个会话")
        with self.assertRaises(ValueError):
            self.store.add_message(second["id"], "assistant", "跨会话", run["id"])
        self.assertEqual(self.store.messages(second["id"]), [])

    def test_rename_and_validation(self):
        renamed = self.store.rename_session(self.session["id"], " 新标题 ")
        self.assertEqual(renamed["title"], "新标题")
        with self.assertRaises(ValueError):
            self.store.rename_session(self.session["id"], " ")
        run = self.store.create_run(self.session["id"], "hello")
        for options in [{"status": "unknown"}, {"steps": True}, {"steps": -1}, {"usage": []}]:
            with self.assertRaises(ValueError):
                self.store.set_run(run["id"], **options)
        with self.assertRaises(ValueError):
            self.store.create_run(self.session["id"], "")


if __name__ == "__main__":
    unittest.main()
