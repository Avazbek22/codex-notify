from __future__ import annotations

import json
import sys
from typing import Any


def send(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
if mode == "stderr":
    print("secret fake-secret-value-for-redaction", file=sys.stderr, flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake", "platformFamily": "unix"}})
    elif method == "initialized":
        continue
    elif mode == "timeout" and method == "account/read":
        continue
    elif mode == "die" and method == "account/read":
        raise SystemExit(17)
    elif mode == "malformed" and method == "account/read":
        print("not-json", flush=True)
    elif mode == "rpc_error" and method == "account/read":
        send(
            {
                "id": request_id,
                "error": {
                    "code": 429,
                    "message": "rate limited",
                    "data": {"retryAfter": 17},
                },
            }
        )
    elif mode == "auth_error" and method == "account/read":
        send(
            {
                "id": request_id,
                "error": {"code": 401, "message": "authentication token expired"},
            }
        )
    elif method == "account/read":
        send(
            {
                "id": request_id,
                "result": {
                    "account": {
                        "type": "chatgpt",
                        "email": "fake@example.invalid",
                        "planType": "plus",
                    },
                    "requiresOpenaiAuth": True,
                },
            }
        )
    elif method == "account/login/start":
        send(
            {
                "id": request_id,
                "result": {
                    "type": "chatgptDeviceCode",
                    "loginId": "login-1",
                    "verificationUrl": "https://auth.openai.com/codex/device",
                    "userCode": "ABCD-1234",
                },
            }
        )
        send(
            {
                "method": "account/login/completed",
                "params": {"loginId": "login-1", "success": True, "error": None},
            }
        )
    elif method in {"account/login/cancel", "account/logout"}:
        send({"id": request_id, "result": {}})
    elif method == "account/rateLimits/read":
        send(
            {
                "id": request_id,
                "result": {
                    "accountId": "fake-account",
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 25,
                            "windowDurationMins": 300,
                            "resetsAt": 2_000_000_000,
                        },
                    },
                    "rateLimitsByLimitId": None,
                    "ordinaryUsageAllowed": True,
                    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
                },
            }
        )
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "not allowed"}})
