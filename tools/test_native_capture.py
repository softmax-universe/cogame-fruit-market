"""Complete native games against a loopback-only Messages fixture, never a teacher."""

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _ws_connect(host: str, port: int, path: str) -> tuple[socket.socket, bytearray]:
    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(b"fruit-market-ext").decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    sock.sendall(handshake)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        assert chunk, "WebSocket handshake closed before HTTP headers completed"
        buf.extend(chunk)
    head, rest = bytes(buf).split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 101"), head.decode(errors="replace")
    return sock, bytearray(rest)


def _ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    mask = b"\x37\xfa\x21\x3d"
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _ws_recv_frame(sock: socket.socket, buf: bytearray) -> tuple[int, bytes] | None:
    def ensure(size: int) -> bool:
        while len(buf) < size:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            buf.extend(chunk)
        return True

    if not ensure(2):
        return None
    opcode = buf[0] & 0x0F
    masked = (buf[1] & 0x80) != 0
    length = buf[1] & 0x7F
    offset = 2
    if length == 126:
        if not ensure(4):
            return None
        length = struct.unpack("!H", buf[2:4])[0]
        offset = 4
    elif length == 127:
        if not ensure(10):
            return None
        length = struct.unpack("!Q", buf[2:10])[0]
        offset = 10
    mask_bytes = b""
    if masked:
        if not ensure(offset + 4):
            return None
        mask_bytes = bytes(buf[offset : offset + 4])
        offset += 4
    if not ensure(offset + length):
        return None
    data = bytes(buf[offset : offset + length])
    del buf[: offset + length]
    if masked:
        data = bytes(b ^ mask_bytes[i & 3] for i, b in enumerate(data))
    return opcode, data


def _market_offer_for_farm(farm: str) -> dict[str, object]:
    if farm == "apple":
        return {"give": {"fruit": "apple", "n": 3}, "want": {"fruit": "banana", "n": 2}}
    return {"give": {"fruit": "banana", "n": 2}, "want": {"fruit": "apple", "n": 3}}


def _run_external_malformed_then_valid_seat(
    host: str, port: int, slot: int, token: str, summary_out: dict[str, int]
) -> None:
    sock, buf = _ws_connect(host, port, f"/player?slot={slot}&token={token}")
    try:
        _ws_send_text(sock, json.dumps({"type": "register", "control": "external"}))
        submitted_rounds: set[int] = set()
        while True:
            frame = _ws_recv_frame(sock, buf)
            if frame is None:
                break
            opcode, data = frame
            if opcode == 0x9:
                sock.sendall(bytes([0x8A, 0x80]) + b"\x00\x00\x00\x00")
                continue
            if opcode == 0x8:
                break
            if opcode != 0x1:
                continue
            msg = json.loads(data.decode())
            mtype = msg.get("type")
            if mtype == "welcome":
                _ws_send_text(sock, json.dumps({"type": "register", "control": "external"}))
            elif mtype == "state" and not msg.get("done"):
                round_no = int(msg["round"])
                if round_no in submitted_rounds:
                    continue
                submitted_rounds.add(round_no)
                farm = msg["you"]["farmType"]
                # First send malformed fruit & null order frames through the live /player socket;
                # the server must reject both without crashing and without latching orderReceived.
                _ws_send_text(
                    sock,
                    json.dumps(
                        {
                            "type": "order",
                            "round": round_no,
                            "order": {
                                "job": "market",
                                "stall": "north",
                                "eat": "crave",
                                "offer": {"give": {"n": 3}, "want": {"fruit": "banana", "n": 2}},
                            },
                        }
                    ),
                )
                _ws_send_text(
                    sock,
                    json.dumps({"type": "order", "round": round_no, "order": None}),
                )
                summary_out["malformed_sent"] += 2
                if round_no % 2 == 1:
                    valid_order: dict[str, object] = {
                        "job": "harvest",
                        "eat": "any",
                        "say": f"ext-r{round_no}",
                        "notes": f"ext-{round_no}",
                    }
                else:
                    valid_order = {
                        "job": "market",
                        "stall": "north",
                        "eat": "crave",
                        "offer": _market_offer_for_farm(farm),
                        "say": f"ext-m{round_no}",
                        "notes": f"ext-{round_no}",
                    }
                _ws_send_text(
                    sock,
                    json.dumps({"type": "order", "round": round_no, "order": valid_order}),
                )
                summary_out["valid_sent"] += 1
            elif mtype == "final":
                summary_out["saw_final"] = 1
                break
    finally:
        sock.close()


def _malformed_fruit_reply(slot: int, content: str) -> bytes:
    m_round = re.search(r"^ROUND (\d+) of (\d+)", content)
    assert m_round is not None, content[:120]
    round_no = int(m_round.group(1))
    is_retry = "Your previous reply was invalid." in content
    farm = "apple" if "apple farmer" in content else "banana"

    def wrap(order_obj: object) -> bytes:
        return json.dumps(
            {
                "model": "loopback-fixture",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(order_obj)}],
            }
        ).encode()

    if round_no == 1:
        return wrap({"job": "harvest", "eat": "any", "say": "r1", "notes": "stock"})
    if round_no == 2:
        if slot == 0:
            if not is_retry:
                return wrap(
                    {
                        "job": "market",
                        "stall": "north",
                        "eat": "crave",
                        "offer": {"give": {"n": 3}, "want": {"fruit": "banana", "n": 2}},
                    }
                )
            return wrap(
                {
                    "job": "market",
                    "stall": "north",
                    "eat": "crave",
                    "offer": _market_offer_for_farm(farm),
                    "say": "retry-ok",
                    "notes": "recovered",
                }
            )
        if not is_retry:
            return wrap(
                {
                    "job": "market",
                    "stall": "north",
                    "eat": "crave",
                    "offer": {"give": {"fruit": "apple", "n": 3}, "want": {"n": 2}},
                }
            )
        return wrap(
            {
                "job": "market",
                "stall": "north",
                "eat": "crave",
                "offer": {"give": {"fruit": None, "n": 3}, "want": {"fruit": "banana", "n": 2}},
            }
        )
    if round_no == 3:
        if slot == 0:
            if not is_retry:
                return wrap(
                    {
                        "job": "market",
                        "stall": "east",
                        "eat": "crave",
                        "offer": {"give": {"fruit": "apple"}, "want": {"fruit": "banana", "n": 2}},
                    }
                )
            return wrap(
                {
                    "job": "market",
                    "stall": "east",
                    "eat": "crave",
                    "offer": {"give": {"fruit": "apple", "n": 3}, "want": {"fruit": "banana"}},
                }
            )
        if not is_retry:
            return wrap(
                {
                    "job": "market",
                    "stall": "east",
                    "eat": "crave",
                    "offer": {
                        "give": {"fruit": ["apple"], "n": 3},
                        "want": {"fruit": "banana", "n": 2},
                    },
                }
            )
        return wrap({"job": "harvest", "fruit": 123, "eat": "any", "say": "bad", "notes": "bad"})
    if round_no == 4:
        if slot == 0:
            if not is_retry:
                # 200 JSON object without a "content" array
                return b"{}"
            return wrap(
                {
                    "job": "harvest",
                    "eat": "any",
                    "offer": None,
                    "say": "withdraw",
                    "notes": "clean",
                }
            )
        return wrap(
            {
                "job": "harvest",
                "fruit": None,
                "eat": "any",
                "say": "opt",
                "notes": "null-fruit",
            }
        )
    if round_no == 5 and slot == 0:
        if not is_retry:
            # 159 ASCII bytes + 3-byte em-dash crossing byte 160 without braces
            return json.dumps(
                {
                    "model": "loopback-fixture",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": ("x" * 159) + "— multibyte prose without braces"}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
        return wrap({"job": "harvest", "eat": "any", "say": "utf8-ok", "notes": "ok"})
    if round_no % 2 == 1:
        return wrap({"job": "harvest", "eat": "any", "say": f"r{round_no}", "notes": "ok"})
    return wrap(
        {
            "job": "market",
            "stall": "north",
            "eat": "crave",
            "offer": _market_offer_for_farm(farm),
            "say": f"m{round_no}",
            "notes": "ok",
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game", type=Path)
    parser.add_argument("player", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--mode",
        choices=("accepted", "invalid", "429", "normalized", "malformed-fruit"),
        default="accepted",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--no-capture", action="store_true")
    parser.add_argument("--source-revision", default="loopback-working-tree-not-a-published-source")
    args = parser.parse_args()
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    calls = []
    lock = threading.Lock()
    canary = "PRIVATE_OPERATOR_LOOPBACK_FIXTURE"

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/messages"
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            assert canary in request["messages"][0]["content"]
            with lock:
                call_id = f"00000000-0000-4000-8000-{len(calls) + 1:012d}"
                slot_no = int(self.headers["X-Coworld-Player-Slot"])
                if args.mode == "malformed-fruit":
                    body = _malformed_fruit_reply(slot_no, request["messages"][0]["content"])
                    status = 200
                else:
                    order = {
                        "job": "harvest",
                        "eat": "any",
                        "say": "fixture",
                        "notes": "local",
                    }
                    if args.mode == "invalid":
                        order["job"] = "not-a-native-job"
                    elif args.mode == "normalized":
                        order["notes"] = "local\rnormalized"
                    body = json.dumps(
                        {
                            "model": "loopback-fixture",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": json.dumps(order)}],
                        }
                    ).encode()
                    status = 429 if args.mode == "429" else 200
                    if status == 429:
                        body = b'{"error":{"type":"rate_limit_error","message":"local fixture"}}'
                calls.append(
                    {
                        "id": call_id,
                        "slot": self.headers["X-Coworld-Player-Slot"],
                        "request": request,
                        "request_raw": raw.decode(),
                        "response_raw": body.decode(),
                        "status": status,
                    }
                )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Softmax-Llm-Call-Id", call_id)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    config = {
        "seed": args.seed,
        "num_agents": 8,
        "tokens": [f"token-{s}" for s in range(8)],
        "players": [{"name": f"fixture-{s}"} for s in range(8)],
        "rounds": 12,
        "minTurnSeconds": 0,
        "playerConnectTimeoutSeconds": 10,
        "shutdownGraceSeconds": 0,
    }
    (output / "config.json").write_text(json.dumps(config))
    # Construct a clean environment: credentials and inherited remote endpoints never reach fixtures.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
    env.update(
        COGAME_HOST="127.0.0.1",
        COGAME_PORT=str(port),
        COGAME_CONFIG_URI=(output / "config.json").as_uri(),
        COGAME_RESULTS_URI=(output / "results.json").as_uri(),
        COGAME_SAVE_REPLAY_URI=(output / "replay.json").as_uri(),
        COGAME_SAVE_TRAJECTORY_URI=(output / "trajectory.jsonl").as_uri(),
        COWORLD_EPISODE_ID="fixture-only-not-a-hosted-episode",
        COWORLD_GAME_VERSION="fixture-only",
        COWORLD_SOURCE_REVISION=args.source_revision,
        COWORLD_LLM_ENDPOINT=f"http://127.0.0.1:{fixture.server_port}",
        COWORLD_LLM_MODEL="loopback-fixture",
    )
    if args.no_capture:
        del env["COGAME_SAVE_TRAJECTORY_URI"]
    processes, logs = [], []
    external_summary = {"malformed_sent": 0, "valid_sent": 0, "saw_final": 0}
    external_thread: threading.Thread | None = None
    try:
        game_log = (output / "game.log").open("w")
        logs.append(game_log)
        game = subprocess.Popen(
            [str(args.game.resolve())],
            env=env,
            stdout=game_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(game)
        deadline = time.monotonic() + 60
        while True:
            assert game.poll() is None, "Game exited before health probe"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                    break
            except OSError:
                assert time.monotonic() < deadline, "Game never became healthy"
                time.sleep(0.05)
        for seat in range(8):
            if args.mode == "malformed-fruit" and seat == 2:
                external_thread = threading.Thread(
                    target=_run_external_malformed_then_valid_seat,
                    args=("127.0.0.1", port, 2, "token-2", external_summary),
                )
                external_thread.start()
                continue
            player_env = {k: v for k, v in env.items() if k in ("PATH", "HOME", "TMPDIR")}
            player_env["COWORLD_PLAYER_WS_URL"] = f"ws://127.0.0.1:{port}/player?slot={seat}&token=token-{seat}"
            if seat < 2:
                player_env["PLAYER_PROMPT"] = canary
            else:
                player_env["PLAYER_SCRIPTED"] = "hauler"
            log = (output / f"player-{seat}.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    [str(args.player.resolve())],
                    env=player_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        assert game.wait(timeout=60) == 0
        for process in processes[1:]:
            assert process.wait(timeout=5) == 0
        if external_thread is not None:
            external_thread.join(timeout=5)
            assert not external_thread.is_alive()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()
        fixture.shutdown()
        fixture.server_close()
        (output / "fixture-calls.json").write_text(json.dumps(calls, indent=2) + "\n")
    results = json.loads((output / "results.json").read_text())
    assert results["reason"] == "complete" and results["rounds"] == 12, results
    assert canary not in (output / "replay.json").read_text()
    trajectory = output / "trajectory.jsonl"
    if args.no_capture:
        assert not trajectory.exists()
        print(
            json.dumps(
                {
                    "fixture_only": True,
                    "capture_disabled": True,
                    "fixture_calls": len(calls),
                }
            )
        )
        return
    assert trajectory.is_file(), "Complete native game did not retain its private trajectory"
    trajectory_raw = trajectory.read_bytes()
    events = [json.loads(line) for line in trajectory_raw.decode("utf-8").splitlines()]
    assert events[-1]["event_type"] == "episode" and events[-1]["status"] == "completed"
    decisions = events[:-1]
    assert len(decisions) == 96
    actual = [a for d in decisions for a in d["attempts"] if a["origin"] == "model"]
    expected_calls = 30 if args.mode == "malformed-fruit" else 24 if args.mode in ("accepted", "normalized") else 48
    assert len(actual) == len(calls) == expected_calls, (len(actual), len(calls), expected_calls)
    received = {c["id"]: c for c in calls}
    for attempt in actual:
        call = received[attempt["platform_call_id"]]
        assert attempt["request"] == call["request"]
        assert base64.b64decode(attempt["response_body_b64"]).decode() == call["response_raw"]
        assert attempt["http_status"] == call["status"]
    if args.mode == "malformed-fruit":
        assert external_summary == {
            "malformed_sent": 24,
            "valid_sent": 12,
            "saw_final": 1,
        }, external_summary
        assert results["total_trades"] > 0, results
        by_round_seat = {(int(d["observation"]["round"]), int(d["seat"])): d for d in decisions}
        # Round 2 seat 0: attempt 0 missing offer.give.fruit -> recovered on retry attempt 1
        r2s0 = by_round_seat[(2, 0)]
        assert r2s0["action_status"] == "accepted"
        assert r2s0["selected_attempt_id"].endswith("-1")
        assert "missing fruit in offer.give.fruit" in r2s0["attempts"][0]["rejection_reason"]
        # Round 2 seat 1: missing offer.want.fruit then null offer.give.fruit -> deterministic fallback
        r2s1 = by_round_seat[(2, 1)]
        assert r2s1["action_status"] == "fallback"
        assert "missing fruit in offer.want.fruit" in r2s1["attempts"][0]["rejection_reason"]
        assert "unknown fruit in offer.give.fruit: null" in r2s1["attempts"][1]["rejection_reason"]
        # Round 3 seat 0: missing offer.give.n then missing offer.want.n -> deterministic fallback
        r3s0 = by_round_seat[(3, 0)]
        assert r3s0["action_status"] == "fallback"
        assert "missing offer.give.n" in r3s0["attempts"][0]["rejection_reason"]
        assert "missing offer.want.n" in r3s0["attempts"][1]["rejection_reason"]
        # Round 3 seat 1: non-string offer.give.fruit then non-string top-level fruit -> fallback
        r3s1 = by_round_seat[(3, 1)]
        assert r3s1["action_status"] == "fallback"
        assert "unknown fruit in offer.give.fruit:" in r3s1["attempts"][0]["rejection_reason"]
        assert "unknown fruit in fruit: 123" in r3s1["attempts"][1]["rejection_reason"]
        # Round 4 seat 0: empty 200 envelope {} -> recovered on retry with legal null offer withdrawal
        r4s0 = by_round_seat[(4, 0)]
        assert r4s0["action_status"] == "accepted"
        assert r4s0["selected_attempt_id"].endswith("-1")
        assert "no JSON object in response" in r4s0["attempts"][0]["rejection_reason"]
        assert r4s0["executed_action"]["offer"] is None
        # Round 4 seat 1: optional top-level "fruit": null normalized cleanly without failing gameplay
        r4s1 = by_round_seat[(4, 1)]
        assert r4s1["action_status"] == "rejected"
        assert r4s1["selected_attempt_id"] is None
        assert "fruit" not in r4s1["executed_action"]
        # Round 5 seat 0: multibyte UTF-8 crossing byte 160 rejected cleanly with valid UTF-8 trajectory
        r5s0 = by_round_seat[(5, 0)]
        assert r5s0["action_status"] == "accepted"
        assert r5s0["selected_attempt_id"].endswith("-1")
        assert "no JSON object in response:" in r5s0["attempts"][0]["rejection_reason"]
        # Seat 2 (external player): all 12 rounds accepted valid external orders after rejecting malformed frames
        for r in range(1, 13):
            d_ext = by_round_seat[(r, 2)]
            assert d_ext["action_status"] == "accepted"
            assert d_ext["attempts"][0]["origin"] == "unknown"
            assert d_ext["executed_action"]["notes"] == f"ext-{r}"
        # Seats 3..7: scripted hauler teacher
        for r in range(1, 13):
            for s in range(3, 8):
                d_t = by_round_seat[(r, s)]
                assert d_t["attempts"][0]["origin"] == "teacher"
    else:
        for decision in decisions:
            if int(decision["seat"]) < 2 and args.mode == "normalized":
                assert decision["action_status"] == "rejected"
                assert decision["selected_attempt_id"] is None
                assert decision["attempts"][0]["parsed_action"]["notes"] == "local\rnormalized"
                assert decision["executed_action"]["notes"] == "local normalized"
                assert not decision["attempts"][0]["accepted"]
                continue
            selected = next(a for a in decision["attempts"] if a["attempt_id"] == decision["selected_attempt_id"])
            assert selected["accepted"] and selected["parsed_action"] == decision["executed_action"]
            origin = "teacher" if int(decision["seat"]) >= 2 else "model" if args.mode == "accepted" else "fallback"
            assert selected["origin"] == origin
    print(
        json.dumps(
            {
                "mode": args.mode,
                "rounds": 12,
                "fixture_calls": len(calls),
                "decisions": len(decisions),
                "family": events[-1]["seed_family"],
                "fixture_only": True,
            }
        )
    )


if __name__ == "__main__":
    main()
