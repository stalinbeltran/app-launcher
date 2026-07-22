#!/usr/bin/env python3
"""Demo trivial: confirma puertos asignados y el directorio de trabajo (cwd)."""
import argparse
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--api", type=int, default=0)
args = parser.parse_args()

API_FROM_ENV = os.environ.get("API_PORT", "?")
CWD = os.getcwd()

PAGE = f"""<!doctype html><meta charset=utf-8>
<title>Hello App demo</title>
<body style="font-family:system-ui;background:#0f1117;color:#e6e8ee;padding:40px">
<h1>La demo esta corriendo</h1>
<p>Puerto web (asignado por el launcher): <b>{args.port}</b></p>
<p>Puerto API por argumento: <b>{args.api}</b></p>
<p>Puerto API por variable de entorno: <b>{API_FROM_ENV}</b></p>
<p>Directorio de trabajo (cwd): <b>{CWD}</b></p>
<p>Si el cwd termina en <code>web</code>, la opcion cwd por proceso funciona.</p>
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


print(f"Hello demo escuchando en http://localhost:{args.port} (api={args.api}, cwd={CWD})")
HTTPServer(("127.0.0.1", args.port), H).serve_forever()
