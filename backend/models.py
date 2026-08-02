import re
# Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

try:
    from backend.config import FINAL_RECOMMENDATIONS, MAX_QUERY_LENGTH
except ImportError:
    from config import FINAL_RECOMMENDATIONS, MAX_QUERY_LENGTH


class SearchFilters(BaseModel):
    format: Optional[str] = Field(None, description="Format: ebook, print, audiobook, or any")
    year_from: Optional[int] = Field(None, ge=1800, le=datetime.now().year + 2)
    year_to: Optional[int] = Field(None, ge=1800, le=datetime.now().year + 2)
    language: Optional[str] = Field(None, description="Language: en, ar, or any")
    open_access_only: Optional[bool] = Field(None, description="Only return open-access articles")

    @model_validator(mode="after")
    def validate_year_range(self):
        if (
            self.year_from is not None
            and self.year_to is not None
            and self.year_from > self.year_to
        ):
            raise ValueError("year_from cannot be greater than year_to")
        return self


class AISearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=FINAL_RECOMMENDATIONS, ge=1, le=10)
    session_id: Optional[str] = Field(None, max_length=256, description="Signed session token")
    filters: Optional[SearchFilters] = None
    search_mode: Optional[str] = Field(default="all", description="Search mode: 'books', 'research', or 'all'")
    include_related_books: bool = Field(default=True, description="Include related catalog books in research mode")

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query cannot be empty")
        sanitized = value.strip()
        dangerous_patterns = [r"<script", r"javascript:", r"onerror=", r"onload="]
        for pattern in dangerous_patterns:
            if re.search(pattern, sanitized, re.IGNORECASE):
                raise ValueError("Query contains invalid characters")
        return sanitized

    @field_validator("search_mode")
    @classmethod
    def validate_search_mode(cls, value: str) -> str:
        if value not in ["books", "research", "all"]:
            return "all"
        return value


class Book(BaseModel):
    title: str
    author: str
    format: str
    call_number: Optional[str] = None
    availability_status: Optional[str] = None
    branch_location: Optional[str] = None
    shelf_location: Optional[str] = None
    year: str
    link: str
    oclc_number: Optional[str] = None
    relevance_score: Optional[float] = None
    why_recommended: Optional[str] = None


class ArticleOut(BaseModel):
    title: str
    authors: str = "Unknown"
    year: str = ""
    journal: str = ""
    database: str = ""
    doi: str = ""
    link: str = ""
    direct_link: str = ""
    open_access: bool = False
    relevance_score: Optional[float] = None
    why_recommended: str = ""


class AISearchResponse(BaseModel):
    ai_response: str
    books: List[Book]
    articles: List[ArticleOut] = Field(default_factory=list)
    query_used: str
    total_found: int
    total_analyzed: int
    filters_applied: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    suggestions: Optional[List[str]] = None


class SessionResponse(BaseModel):
    session_id: str
    csrf_token: str
    expires_at: int


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    services: Dict[str, bool]
    cache_size: int


class ErrorResponse(BaseModel):
    detail: str
    timestamp: str
    suggestions: Optional[List[str]] = None
