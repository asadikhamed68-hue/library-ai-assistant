# Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

import re
from typing import Any, Dict, Optional

try:
    from backend.models import SearchFilters
except ImportError:
    from models import SearchFilters


class SearchPlannerService:
    """Translate prepared user intent into safe backend search targets.

    Gemini and query preparation can infer intent, but this planner keeps the
    actual catalog/article query choices deterministic and auditable.
    """

    DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
    ISBN_RE = re.compile(
        r"(?:(?:97[89])[-\s]?)?(?:\d[-\s]?){9,12}[\dXx]"
    )

    @staticmethod
    def _clean_identifier(value: str) -> str:
        return re.sub(r"[^0-9Xx]", "", value or "")

    @staticmethod
    def _extract_isbn(text: str) -> Optional[str]:
        for match in SearchPlannerService.ISBN_RE.finditer(text or ""):
            isbn = SearchPlannerService._clean_identifier(match.group(0))
            if len(isbn) in {10, 13}:
                return isbn.upper()
        return None

    @staticmethod
    def _extract_doi(text: str) -> Optional[str]:
        match = SearchPlannerService.DOI_RE.search(text or "")
        if not match:
            return None
        return match.group(0).rstrip(".,;)]")

    @staticmethod
    def _extract_quoted_title(text: str) -> Optional[str]:
        match = re.search(r"['\"]([^'\"]{4,160})['\"]", text or "")
        return match.group(1).strip() if match else None

    @staticmethod
    def build(
        user_query: str,
        query_info: Dict[str, Any],
        filters: Optional[SearchFilters],
        requested_mode: str,
    ) -> Dict[str, Any]:
        original = (user_query or "").strip()
        core_topic = (query_info.get("core_topic") or original).strip()
        prepared_catalog_query = (query_info.get("search_query") or core_topic or original).strip()
        search_type = query_info.get("search_type") or "topic"

        isbn = SearchPlannerService._extract_isbn(original)
        doi = SearchPlannerService._extract_doi(original)
        quoted_title = SearchPlannerService._extract_quoted_title(original)

        catalog_query = prepared_catalog_query
        article_query = core_topic or original
        exact_identifier = isbn or doi
        plan_notes = []

        if isbn:
            catalog_query = isbn
            article_query = isbn
            search_type = "identifier"
            plan_notes.append("isbn_detected")
        elif doi:
            catalog_query = doi
            article_query = doi
            search_type = "identifier"
            plan_notes.append("doi_detected")
        elif search_type == "author":
            catalog_query = prepared_catalog_query
            article_query = core_topic or query_info.get("author_name") or original
            plan_notes.append("author_search")
        elif quoted_title:
            catalog_query = quoted_title
            article_query = quoted_title
            search_type = "title"
            plan_notes.append("quoted_title")

        format_hint = filters.format if filters and filters.format else query_info.get("format_preference")
        year_from = filters.year_from if filters and filters.year_from else query_info.get("year_from")
        year_to = filters.year_to if filters and filters.year_to else query_info.get("year_to")

        return {
            "original_query": original,
            "requested_mode": requested_mode or "all",
            "search_type": search_type,
            "catalog_query": catalog_query[:500],
            "article_query": article_query[:500],
            "database_topic": (core_topic or article_query or original)[:500],
            "exact_identifier": exact_identifier,
            "isbn": isbn,
            "doi": doi,
            "format_hint": format_hint,
            "year_from": year_from,
            "year_to": year_to,
            "notes": plan_notes,
        }
