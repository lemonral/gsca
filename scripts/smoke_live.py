#!/usr/bin/env python3
"""Bounded live acceptance against this agent's API. Does not print credentials."""
from pathlib import Path
import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:3189")
parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
parser.add_argument("--output", type=Path, default=Path("verification/live.json"))
args = parser.parse_args()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def request(path, body=None):
    req = urllib.request.Request(args.base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with opener.open(req, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw.decode()


health = request("/api/health")
assert health["ok"] and not health["authenticated"]
request("/api/login", {"token": (args.data_dir / "access-token").read_text().strip()})
upstream = request("/api/model-health")
assert upstream["ok"]
session = request("/api/sessions", {"title": "验收 · 千问计算与文件闭环"})
run = request(f'/api/sessions/{session["id"]}/runs', {
    "prompt": "请先调用calculate工具计算 (123+456)*7。将表达式和结果写入arithmetic-check.txt，然后必须调用read_file读回该文件验证。最后告诉我核实的结果和文件路径。"})
deadline = time.monotonic() + 260
last_status = None
while time.monotonic() < deadline:
    detail = request("/api/runs/" + run["id"])
    status = detail["run"]["status"]
    if status != last_status:
        print("RUN_STATUS", status, flush=True)
        last_status = status
    if status not in {"queued", "running"}:
        break
    time.sleep(1)
else:
    raise SystemExit("Acceptance timed out; inspect the persisted run, do not auto-resubmit.")
assert status == "completed", detail["run"]
calls = [e["payload"]["name"] for e in detail["events"] if e["kind"] == "tool_call"]
assert all(name in calls for name in ["calculate", "write_file", "read_file"]), calls
file_text = request(f'/api/sessions/{session["id"]}/files/content?path=arithmetic-check.txt')
assert "4053" in file_text, file_text
messages = request('/api/sessions/' + session["id"])["messages"]
assert messages[-1]["role"] == "assistant" and "4053" in messages[-1]["content"]
report = {"status": "pass", "base": args.base, "model": health["model"], "session_id": session["id"],
          "run_id": run["id"], "tool_calls": calls, "verified_result": 4053,
          "file": "arithmetic-check.txt", "steps": detail["run"]["steps"], "usage": detail["run"]["usage"]}
args.output.parent.mkdir(exist_ok=True, parents=True)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False))
