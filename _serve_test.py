"""Live check of the local serving path: response framing, keep alive reuse, and that a
bunch of idle (or parallel) browser connections can not stall the server."""

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

HOST, PORT = "127.0.0.1", 8129
PAGE = sys.argv[1] if len(sys.argv) > 1 else "LW76epGFFVF"  # any model you have downloaded
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


def req(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    return r, data


def main():
    proc = subprocess.Popen([sys.executable, "matterport-dl.py", PAGE, HOST, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            socket.create_connection((HOST, PORT), 0.5).close()
            break
        except OSError:
            time.sleep(0.25)
    try:
        return run_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_checks():
    # A) keep alive: several requests down one connection, every response must be framed
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    for path in ["/index.html", "/JSNetProxy.js", "/css/showcase.css", "/js/core.js"]:
        r, data = req(conn, "GET", path)
        cl = r.getheader("Content-Length")
        check(f"GET {path} {r.status} framed", r.status == 200 and cl is not None and int(cl) == len(data), f"cl={cl} len={len(data)} proto={r.version}")
    check("http/1.1 in use", r.version == 11, r.version)

    # B) 404 then reuse of the same connection
    r, data = req(conn, "GET", "/js/definitely-not-here.js")
    check("404 for missing js", r.status == 404 and int(r.getheader("Content-Length") or -1) == len(data), r.status)
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("connection reusable after 404", r.status == 200, r.status)

    # C) graph request we do handle, plus one we do not (must still answer, then reuse)
    q = urllib.parse.urlencode({"operationName": "GetModelDetails", "variables": "{}"})
    r, data = req(conn, "GET", "/api/mp/models/graph?" + q)
    check("handled graph GET", r.status == 200 and len(data) > 10 and r.getheader("Content-Type", "").startswith("application/json"), r.status)
    r, data = req(conn, "POST", "/api/mp/models/graph", body=json.dumps({"operationName": "SomeNewQueryWeDoNotKnow"}).encode(), headers={"Content-Type": "application/json"})
    check("unknown graph POST answers", r.status == 200 and b"data" in data, (r.status, data[:40]))
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("connection reusable after unknown graph", r.status == 200, r.status)
    r, data = req(conn, "POST", "/api/v1/event", body=b'{"batch":[{"a":1}]}', headers={"Content-Type": "application/json"})
    check("non graph POST served (body drained)", r.status in (200, 404), r.status)
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("connection reusable after POST body", r.status == 200, r.status)
    r, data = req(conn, "OPTIONS", "/js/core.js")
    check("OPTIONS has content length", r.status == 200 and r.getheader("Content-Length") == "0", r.getheader("Content-Length"))
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("connection reusable after OPTIONS", r.status == 200, r.status)
    conn.close()

    # chunked post bodies can not be drained, the server must close that connection instead of
    # reading the leftover body bytes as the next request
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request("POST", "/api/v1/event", body=b"5\r\nhello\r\n0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})
    r, data = conn.getresponse(), None
    data = r.read()
    check("chunked POST answered and connection closed", r.status in (200, 404) and (r.getheader("Connection") or "").lower() == "close", (r.status, r.getheader("Connection")))
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("next request after chunked post works", r.status == 200, r.status)
    conn.close()

    # D) sockets the browser opens but never sends a request on must not starve the server
    idlers = [socket.create_connection((HOST, PORT), 5) for _ in range(12)]
    start = time.time()
    conn = http.client.HTTPConnection(HOST, PORT, timeout=5)
    try:
        r, data = req(conn, "GET", "/index.html")
        ok = r.status == 200
    except Exception as e:
        ok, data = False, repr(e)
    took = time.time() - start
    check("request answered while 12 idle sockets open", ok and took < 3, f"{took:.2f}s {data if not ok else ''}")
    conn.close()
    for s in idlers:
        s.close()

    # E) the burst of parallel requests a viewer makes on load
    paths = ["/index.html", "/JSNetProxy.js", "/js/core.js", "/js/init.js", "/css/showcase.css", "/js/definitely-not-here.js"]
    results = []

    def worker(i):
        try:
            c = http.client.HTTPConnection(HOST, PORT, timeout=15)
            r, _ = req(c, "GET", paths[i % len(paths)])
            results.append(r.status)
            c.close()
        except Exception as e:
            results.append(repr(e))

    start = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(120)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.time() - start
    bad = [r for r in results if r not in (200, 404)]
    check("120 parallel requests all answered", len(results) == 120 and not bad, f"{took:.2f}s bad={bad[:3]}")

    # F) /__diag is what you (or the pages own watchdog) ask when the viewer never comes up
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    r, data = req(conn, "GET", "/__diag")
    info = json.loads(data.decode())
    check("__diag reports this very request as in flight", r.status == 200 and any(i["path"] == "/__diag" for i in info["inFlight"]), data[:150])
    r, data = req(conn, "POST", "/__diag", body=json.dumps({"reason": "self test from _serve_test.py, not a real hang", "inflightCount": 0}).encode(), headers={"Content-Type": "application/json"})
    info = json.loads(data.decode())
    check("__diag POST answers with the server side view", r.status == 200 and "secondsSinceLastRequest" in info, (r.status, data[:150]))
    r, data = req(conn, "GET", "/JSNetProxy.js")
    check("connection reusable after __diag post", r.status == 200, r.status)
    check("proxy ships the stuck watchdog", b"_trackRequest" in data and b"loading-gui" in data)
    conn.close()

    logPath = os.path.join("downloads", PAGE, "server.log")
    try:
        with open(logPath, encoding="utf-8") as f:
            logged = f.read()
    except OSError as e:
        logged = ""
        check("server.log readable", False, e)
    check("pages stuck report lands in server.log", "viewer is stuck" in logged, logPath)

    print(f"\n{'ALL GOOD' if not fails else 'FAILURES: ' + str(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
