"""Deterministic OpenAI-compatible endpoint for offline orchestration checks.

THIS IS NOT A MODEL. It follows a fixed script read from the agent's own
prompt, so the real ChatOpenAI client, agent graph, MCP server, runner and
artifacts can be exercised without a GPU, credentials or a paid API. Nothing
it produces is evidence of diagnostic quality, and runs made with it must
never be scored (use a separate --runs-dir).

Script: with 13 calls left (only the baseline done) request query_logs for
go-enricher; with 12 left request get_metrics; otherwise conclude. The final
report is always "inconclusive" and cites the two baseline observations.

Usage: python -m experiments.fake_model --port 18999
       then MODEL_BASE_URL=http://127.0.0.1:18999/v1 MODEL_ID=offline-fake
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "offline-fake"
FINAL_REPORT = {
    "root_cause_id": "none",
    "summary": "Offline control-flow fixture; no diagnosis was attempted.",
    "confidence": "inconclusive",
    "reasoning": "Scripted reply from experiments/fake_model.py, not a model.",
    "cited_evidence_indices": [0, 1],
}
SCRIPTED_CALLS = {
    13: ("query_logs", {"service": "go-enricher", "since": "5m"}),
    12: ("get_metrics", {"promql": "rate(fraud_enricher_messages_processed_total[1m])", "window": "5m"}),
}


def text_of(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):  # content parts
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def reply(request: dict) -> dict:
    """The scripted chat-completion response for one request body."""
    prompt = " ".join(text_of(message) for message in request.get("messages", []))
    message: dict = {"role": "assistant", "content": ""}
    if "Respond with ONLY" in prompt:
        message["content"] = json.dumps(FINAL_REPORT)
        finish = "stop"
    else:
        remaining = re.search(r"Tool calls remaining: (\d+) of", prompt)
        name, args = SCRIPTED_CALLS.get(int(remaining.group(1)) if remaining else -1,
                                        ("ConcludeInvestigation", {"reason": "offline script complete"}))
        message["tool_calls"] = [{"id": f"call_{name}", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args)}}]
        finish = "tool_calls"
    prompt_tokens = max(1, len(prompt) // 4)
    return {"id": "chatcmpl-offline", "object": "chat.completion", "created": int(time.time()),
            "model": request.get("model", MODEL_ID),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20, "total_tokens": prompt_tokens + 20}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self._send(200, reply(body))

    def log_message(self, format, *args):  # keep test output quiet
        pass


def serve_in_background(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start on 127.0.0.1 (port 0 = any free port); returns the server and its /v1 URL."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=18999)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"offline fake model (not a model) at http://127.0.0.1:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
