"""You.com Search API (ydc-index.io) — web search + full-page extraction plugin.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`.

Config keys::

    web:
      search_backend: "youcom"     # explicit per-capability
      extract_backend: "youcom"
      backend: "youcom"            # shared fallback

Auth env var::

    YDC_API_KEY=ydc-sk-...    # https://api.you.com / https://ydc-index.io
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_YDC_ENDPOINT = "https://ydc-index.io/v1/search"


class YouComWebSearchProvider(WebSearchProvider):
    """You.com search provider with optional full-page markdown extraction."""

    @property
    def name(self) -> str:
        return "youcom"

    @property
    def display_name(self) -> str:
        return "You.com Search"

    def is_available(self) -> bool:
        from agent.web_search_provider import get_provider_env

        return bool(get_provider_env("YDC_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _headers(self, api_key: str) -> Dict[str, str]:
        return {"X-API-Key": api_key, "Content-Type": "application/json"}

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a search against the You.com API (no page extraction).

        Returns ``{"success": True, "data": {"web": [{title,url,description,position}]}}``.
        """
        import httpx

        from agent.web_search_provider import get_provider_env

        api_key = get_provider_env("YDC_API_KEY")
        if not api_key:
            return {"success": False, "error": "YDC_API_KEY is not set"}

        count = max(1, min(int(limit), 20))
        try:
            resp = httpx.post(
                _YDC_ENDPOINT,
                headers=self._headers(api_key),
                json={"query": query, "count": count},
                timeout=20,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning("You.com search HTTP error: %s", exc)
            return {"success": False, "error": f"You.com returned HTTP {exc.response.status_code}"}
        except httpx.RequestError as exc:
            logger.warning("You.com search request error: %s", exc)
            return {"success": False, "error": f"Could not reach You.com: {exc}"}

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("You.com search parse error: %s", exc)
            return {"success": False, "error": "Could not parse You.com response as JSON"}

        raw = (data.get("results") or {}).get("web", []) or []
        truncated = raw[:limit]
        web_results = []
        for i, r in enumerate(truncated):
            desc = str(r.get("description") or "")
            if not desc:
                snips = r.get("snippets") or []
                if isinstance(snips, list) and snips:
                    desc = str(snips[0])
            web_results.append(
                {
                    "title": str(r.get("title", "")),
                    "url": str(r.get("url", "")),
                    "description": desc,
                    "position": i + 1,
                }
            )

        logger.info("You.com search '%s': %d results (limit %d)", query, len(web_results), limit)
        return {"success": True, "data": {"web": web_results}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract full-page markdown for the given URLs via You.com.

        You.com's search endpoint returns page content when ``extraction`` is
        requested. We query each URL's host with a targeted search and match the
        exact URL; when a direct match isn't found we return the first result's
        content. Per-URL failures become items with an ``error`` field.
        """
        import httpx

        from agent.web_search_provider import get_provider_env

        api_key = get_provider_env("YDC_API_KEY")
        if not api_key:
            return [{"url": u, "title": "", "content": "", "error": "YDC_API_KEY is not set"} for u in urls]

        try:
            from tools.interrupt import is_interrupted

            if is_interrupted():
                return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        except Exception:  # noqa: BLE001 — interrupt helper optional
            pass

        results: List[Dict[str, Any]] = []
        payload_extraction = {
            "extraction_mode": "full_page",
            "full_page": {"extraction_formats": ["markdown"]},
        }
        for url in urls:
            try:
                resp = httpx.post(
                    _YDC_ENDPOINT,
                    headers=self._headers(api_key),
                    json={"query": url, "count": 3, "extraction": payload_extraction},
                    timeout=45,
                )
                resp.raise_for_status()
                data = resp.json()
                web = (data.get("results") or {}).get("web", []) or []
                # Prefer an exact URL match, else the closest / first result.
                match = next((r for r in web if str(r.get("url", "")).rstrip("/") == url.rstrip("/")), None)
                if match is None:
                    match = next((r for r in web if url.rstrip("/") in str(r.get("url", "")).rstrip("/")), None)
                if match is None and web:
                    match = web[0]
                if match is None:
                    raise ValueError("no result returned")
                content = ""
                contents = match.get("contents") or {}
                if isinstance(contents, dict):
                    content = str(contents.get("markdown") or "")
                if not content:
                    snips = match.get("snippets") or []
                    content = "\n\n".join(str(s) for s in snips) if isinstance(snips, list) else ""
                title = str(match.get("title") or "")
                results.append(
                    {
                        "url": str(match.get("url") or url),
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {"sourceURL": url, "title": title},
                    }
                )
            except Exception as exc:  # noqa: BLE001 — per-URL error entry
                logger.warning("You.com extract error for %s: %s", url, exc)
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": f"You.com extract failed: {exc}",
                    }
                )
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "You.com Search",
            "badge": "api-key",
            "tag": "You.com Search API — web search + full-page markdown extraction.",
            "env_vars": [
                {
                    "key": "YDC_API_KEY",
                    "prompt": "You.com Search API key (ydc-sk-...)",
                    "url": "https://api.you.com",
                },
            ],
        }
