"""Cloud ingestion and processing HTTP service.

Provides:
  - GET /api/v1/health: health check for connectivity detection
  - POST /api/v1/ingest: high-throughput idempotent batch ingestion (supports gzip)
  - GET /api/v1/status: cloud status and statistics
  - GET /api/v1/export?session=...: streaming export of samples and losses
  - GET /api/v1/results?session=...: streaming window results
"""
import argparse
import gzip
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, Optional

from src.cloud.storage import CloudStorage
from src.cloud.processing import CloudProcessor
from src.common.logging import setup_logging

logger = setup_logging("cloud-server")


class CloudRequestHandler(BaseHTTPRequestHandler):

    storage: CloudStorage
    processor: CloudProcessor

    def log_message(self, format, *args):
        # Redirect request logs to diagnostic logger (stderr)
        logger.debug(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        if path == "/api/v1/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "time": time.time()}).encode("utf-8"))

        elif path == "/api/v1/status":
            session_id = query.get("session", [None])[0]
            stats = self.storage.get_session_stats(session_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(stats).encode("utf-8"))

        elif path == "/api/v1/export":
            session_id = query.get("session", [None])[0]
            if not session_id:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing session query parameter"}\n')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in self.storage.stream_export(session_id):
                self.wfile.write(line.encode("utf-8"))

        elif path == "/api/v1/results":
            session_id = query.get("session", [None])[0]
            if not session_id:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing session query parameter"}\n')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for line in self.storage.stream_results(session_id):
                self.wfile.write(line.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_url = urlparse(self.path)
        if parsed_url.path != "/api/v1/ingest":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Decompress if gzipped
        if self.headers.get("Content-Encoding") == "gzip":
            try:
                raw_body = gzip.decompress(raw_body)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f'{{"error": "Gzip decompression failed: {e}"}}\n'.encode("utf-8"))
                return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f'{{"error": "JSON parse error: {e}"}}\n'.encode("utf-8"))
            return

        samples = payload.get("samples", [])
        losses = payload.get("losses", [])

        s_ins, l_ins = self.storage.ingest_batch(samples, losses)

        # Background processor worker handles online window computation asynchronously

        resp = {
            "status": "ok",
            "samples_inserted": s_ins,
            "losses_inserted": l_ins
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode("utf-8"))


class CloudServer:
    """Multi-threaded cloud ingestion server."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, db_path: str = "cloud_data.db"):
        self.host = host
        self.port = port
        self.storage = CloudStorage(db_path)
        self.processor = CloudProcessor(self.storage)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None

    def start(self):
        CloudRequestHandler.storage = self.storage
        CloudRequestHandler.processor = self.processor

        self.processor.start()
        self.httpd = ThreadingHTTPServer((self.host, self.port), CloudRequestHandler)
        logger.info(f"Cloud server listening on {self.host}:{self.port}")

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def stop(self):
        logger.info("Stopping cloud server...")
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.processor.stop()
        logger.info("Cloud server stopped.")


def main():
    parser = argparse.ArgumentParser(description="MayaCardio Cloud Ingestion Service")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument("--db", default="cloud_data.db", help="Path to cloud SQLite database")
    args = parser.parse_args()

    server = CloudServer(host=args.host, port=args.port, db_path=args.db)
    server.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
