"""Vercel adapter for the existing route handler."""

from urllib.parse import parse_qs, urlencode, urlparse

from main import TriageHandler


class handler(TriageHandler):
    def _restore_original_path(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        original_path = query.pop("__request_path", [None])[0]
        if not original_path or not original_path.startswith("/"):
            return
        # Vercel adds the wildcard rewrite parameter named path to the function query.
        # Remove it when it duplicates the API path already restored above, so exact
        # route checks in POST handlers keep matching.
        route_suffix = original_path[len("/api/"):] if original_path.startswith("/api/") else None
        if route_suffix and query.get("path", [None])[0] == route_suffix:
            query.pop("path", None)
        original_query = urlencode(query, doseq=True)
        self.path = original_path + ("?" + original_query if original_query else "")

    def do_GET(self):
        self._restore_original_path()
        return super().do_GET()

    def do_POST(self):
        self._restore_original_path()
        return super().do_POST()

    def do_PATCH(self):
        self._restore_original_path()
        return super().do_PATCH()

    def do_DELETE(self):
        self._restore_original_path()
        return super().do_DELETE()

    def do_OPTIONS(self):
        self._restore_original_path()
        return super().do_OPTIONS()
