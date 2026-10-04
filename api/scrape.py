import json
import asyncio

from http.server import BaseHTTPRequestHandler

from scraper.fetcher import fetch_page
from scraper.parser import parse_page


TARGET = "https://desihub.sh/"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):

        try:

            result = asyncio.run(fetch_page(TARGET))

            parsed = parse_page(
                result["html"],
                result["url"]
            )

            response = {
                "source": TARGET,
                "status": result["status"],
                "final_url": result["url"],
                **parsed,
            }

            payload = json.dumps(
                response,
                ensure_ascii=False,
                default=str
            ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Cache-Control",
                "public, max-age=60"
            )

            self.send_header(
                "Content-Length",
                str(len(payload))
            )

            self.end_headers()

            self.wfile.write(payload)

        except Exception as e:

            payload = json.dumps({
                "error": True,
                "message": str(e)
            }).encode("utf-8")

            self.send_response(500)

            self.send_header(
                "Content-Type",
                "application/json"
            )

            self.end_headers()

            self.wfile.write(payload)
