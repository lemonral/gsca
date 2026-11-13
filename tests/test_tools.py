import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.tools import ToolError, ToolRegistry, MAX_FILE_BYTES, MAX_TOTAL_BYTES


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.tools = ToolRegistry(self.root)

    def call(self, name, args=None, session="session-1"):
        return self.tools.execute(name, args or {}, session)

    def test_files_and_session_isolation(self):
        created = self.call("write_file", {"path": "notes/result.txt", "content": "结果84\nagent ready"})
        self.assertEqual(created, {"path": "notes/result.txt", "bytes": 20})
        self.assertEqual(self.call("read_file", {"path": created["path"]})["content"], "结果84\nagent ready")
        self.assertEqual(self.call("list_files", {"path": "notes"})["files"][0]["type"], "file")
        self.assertEqual(self.call("search_files", {"query": "AGENT"})["matches"][0]["line"], 2)
        self.assertEqual(self.call("list_files", session="session-2")["files"], [])
        with self.assertRaises(ToolError):
            self.call("read_file", {"path": created["path"]}, session="session-2")
        with self.assertRaises(ToolError):
            self.call("write_file", {"path": created["path"], "content": "replace"})
        self.assertEqual(self.call("read_file", {"path": created["path"]})["content"], "结果84\nagent ready")

    def test_reject_escaping_paths_and_invalid_sessions(self):
        for path in ["/etc/passwd", "../outside", "a/../x", "a\\x", "C:/outside", "\x00", "."]:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.call("write_file", {"path": path, "content": "unsafe"})
        for sid in ["", "../s", "/tmp/s", "a.b", "a" * 81]:
            with self.subTest(session=sid), self.assertRaises(ToolError):
                self.call("current_time", session=sid)

    def test_links_never_followed(self):
        self.call("list_files")
        session = self.root / "session-1"
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private")
        os.symlink(outside, session / "escape")
        for name, args in [("read_file", {"path": "escape/private.txt"}),
                           ("write_file", {"path": "escape/new.txt", "content": "x"}),
                           ("list_files", {}), ("search_files", {"query": "private"})]:
            with self.subTest(tool=name), self.assertRaises(ToolError):
                self.call(name, args)
        self.assertFalse((outside / "new.txt").exists())
        (session / "escape").unlink()
        os.link(outside / "private.txt", session / "hardlink")
        with self.assertRaises(ToolError):
            self.call("read_file", {"path": "hardlink"})
        os.symlink(outside, self.root / "linked-session")
        with self.assertRaises(ToolError):
            self.call("list_files", session="linked-session")

    def test_file_and_total_quota(self):
        self.assertEqual(MAX_FILE_BYTES, 65536)
        self.assertEqual(MAX_TOTAL_BYTES, 10 * 1024 * 1024)
        with self.assertRaises(ToolError):
            self.call("write_file", {"path": "too-big.txt", "content": "中" * 30000})
        self.call("write_file", {"path": "ok.txt", "content": "a" * 10})
        with patch("app.tools.MAX_TOTAL_BYTES", 15):
            with self.assertRaises(ToolError):
                self.call("write_file", {"path": "full.txt", "content": "b" * 6})
        self.assertFalse((self.root / "session-1/full.txt").exists())
        (self.root / "session-1/large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ToolError):
            self.call("read_file", {"path": "large.txt"})

    def test_bounded_listing_and_search(self):
        self.call("list_files")
        session = self.root / "session-1"
        for n in range(205):
            (session / f"{n:03}.txt").write_text("found\n" * 60)
        listing = self.call("list_files")
        self.assertEqual(len(listing["files"]), 200)
        self.assertTrue(listing["truncated"])
        matches = self.call("search_files", {"query": "found"})
        self.assertEqual(len(matches["matches"]), 50)
        self.assertTrue(matches["truncated"])
        (session / "binary.bin").write_bytes(b"\xff\x00")
        no_matches = self.call("search_files", {"query": "unmatched"})
        self.assertLessEqual(no_matches["scanned_files"], 200)
        self.assertTrue(no_matches["truncated"])

    def test_schemas_and_math_boundaries(self):
        for name, args in [("missing", {}), ("calculate", {}), ("calculate", {"expression": "1", "extra": "x"}),
                           ("read_file", {"path": 123}), ("search_files", {"query": " "})]:
            with self.subTest(tool=name, args=args), self.assertRaises(ToolError):
                self.call(name, args)
        self.assertEqual(self.call("calculate", {"expression": "sqrt(81) + 2**3 * 4"})["result"], 41)
        self.assertAlmostEqual(self.call("calculate", {"expression": "sin(pi/2)"})["result"], 1)
        for expr in ["__import__('os').system('id')", "open('/etc/passwd')", "True+1", "[1,2]", "9**999", "10**100**100", "1/0", "sqrt(-1)", "'secret'", "abs(-1, 2)"]:
            with self.subTest(expression=expr), self.assertRaises(ToolError):
                self.call("calculate", {"expression": expr})
        self.assertIn("+08:00", self.call("current_time")["iso"])
        with self.assertRaises(ToolError):
            self.call("current_time", {"timezone": "not/a/timezone"})
        schemas = self.tools.schemas
        schemas.clear()
        self.assertEqual(len(self.tools.schemas), 6)


if __name__ == "__main__":
    unittest.main()
