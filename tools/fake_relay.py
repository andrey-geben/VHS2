"""Фейковое реле на localhost:9999 для тестов."""
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"[RELAY] Получен запрос: {self.path}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        self.do_GET()

    def log_message(self, *args):
        pass  # тихий режим

if __name__ == "__main__":
    print("Фейковое реле на http://localhost:9999")
    HTTPServer(("localhost", 9999), Handler).serve_forever()