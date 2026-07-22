#!/usr/bin/env python3
"""Segundo demo: identico a hello-app pero se identifica como APP TWO.
   Prefiere el mismo puerto (4500) para probar que NO colisionan."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
args = parser.parse_args()

PAGE = f"""<!doctype html><meta charset=utf-8>
<title>APP TWO</title>
<body style="font-family:system-ui;background:#161018;color:#ffd7ee;padding:40px">
<h1>Soy la APP TWO</h1>
<p>Puerto asignado: <b>{args.port}</b></p>
</body>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


print(f"APP TWO escuchando en http://localhost:{args.port}")
HTTPServer(("127.0.0.1", args.port), H).serve_forever()
