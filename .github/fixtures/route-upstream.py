"""CI-only upstream: distinct statuses prove routing, without a Codex account."""
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = {'/healthz': 200, '/device': 418}.get(self.path, 599)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true,"deviceConnected":false}')


HTTPServer(('127.0.0.1', 8787), Handler).serve_forever()
