#!/usr/bin/env python3
"""Simple HTTP server with correct WASM MIME type."""
import http.server
import socketserver
import os
import sys

PORT = 3000
DIR = os.path.dirname(os.path.abspath(__file__))

class WasmHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def guess_type(self, path):
        if path.endswith('.wasm'):
            return 'application/wasm'
        if path.endswith('.db'):
            return 'application/octet-stream'
        return super().guess_type(path)

    def end_headers(self):
        # Allow loading the DB and WASM from any origin
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

if __name__ == '__main__':
    print(f'Serving Atmos Paper Tracker at http://localhost:{PORT}')
    print(f'Directory: {DIR}')
    with socketserver.TCPServer(('', PORT), WasmHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nServer stopped.')
