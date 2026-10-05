import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            for a in json.loads(body).get("alerts", []):
                labels = a.get("labels", {})
                print(f"[{self.path}] {a.get('status', '?').upper()} {labels.get('alertname')} "
                      f"job={labels.get('job', '-')} {a.get('annotations', {}).get('summary', '')}", flush=True)
        except Exception:
            print(f"[{self.path}] unparseable payload: {body[:200]!r}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
