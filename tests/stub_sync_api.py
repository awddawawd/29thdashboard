"""Serveur d'API factice pour valider le conteneur locataire durci (hors production).

Répond aux deux endpoints internes de synchronisation et journalise les appels.
Usage : python tests/stub_sync_api.py [port]
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECORDS: list[dict] = []


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        token = self.headers.get("X-WAAS-Sync-Token")
        print(f"[stub] GET {self.path} token={'oui' if token else 'NON'}", flush=True)
        if self.path != "/internal/sync/content":
            self._send({"detail": "not found"}, 404)
            return
        self._send({"tenant_id": "boutique-dupont", "total": len(RECORDS), "records": RECORDS})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        records = payload.get("records") or []
        print(f"[stub] POST {self.path} : {len(records)} enregistrements", flush=True)
        RECORDS.clear()
        RECORDS.extend(records)
        self._send({"tenant_id": "boutique-dupont", "upserted": len(records)})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18000
    print(f"[stub] écoute sur 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Stub).serve_forever()
