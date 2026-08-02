import asyncio
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import app
from backend.cache import SearchCache
from backend.models import SearchFilters
from backend.search_planner import SearchPlannerService
from backend.security import create_signed_session, verify_signed_session


def test_signed_session_round_trip_and_tamper_rejection():
    session = create_signed_session()
    raw_session_id = verify_signed_session(session.session_id, session.csrf_token)
    assert raw_session_id

    replacement = "a" if session.session_id[-1] != "a" else "b"
    tampered = session.session_id[:-1] + replacement
    with pytest.raises(HTTPException) as exc_info:
        verify_signed_session(tampered, session.csrf_token)
    assert exc_info.value.status_code == 401


def test_search_filter_rejects_reversed_year_range():
    with pytest.raises(ValidationError):
        SearchFilters(year_from=2025, year_to=2020)


def test_search_planner_extracts_isbn_and_doi():
    isbn_plan = SearchPlannerService.build(
        "Find ISBN 978-1-4028-9462-6",
        {"core_topic": "", "search_query": ""},
        None,
        "books",
    )
    assert isbn_plan["isbn"] == "9781402894626"
    assert isbn_plan["search_type"] == "identifier"

    doi_plan = SearchPlannerService.build(
        "Find https://doi.org/10.1000/example.123",
        {"core_topic": "", "search_query": ""},
        None,
        "research",
    )
    assert doi_plan["doi"] == "10.1000/example.123"
    assert doi_plan["search_type"] == "identifier"


def test_cache_returns_defensive_copies_and_reports_size():
    async def scenario():
        cache = SearchCache(max_size=2, ttl=60)
        original = {"items": ["one"]}
        await cache.set("topic", original)

        first = await cache.get("topic")
        first["items"].append("changed")
        second = await cache.get("topic")

        assert second == original
        assert await cache.size() == 1

    asyncio.run(scenario())


def test_health_response_has_request_and_security_headers():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_session_endpoint_returns_signed_session_fields():
    with TestClient(app) as client:
        response = client.post("/session")

    assert response.status_code == 200
    assert set(response.json()) == {"session_id", "csrf_token", "expires_at"}


@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [("invalid", 400), ("1048577", 413)],
)
def test_invalid_request_sizes_are_rejected_with_security_headers(
    content_length, expected_status
):
    with TestClient(app) as client:
        response = client.get("/health", headers={"content-length": content_length})

    assert response.status_code == expected_status
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_direct_python_launcher_starts_on_localhost(monkeypatch):
    uvicorn_run = Mock()
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=uvicorn_run))

    app_path = Path(__file__).resolve().parents[1] / "backend" / "app.py"
    runpy.run_path(str(app_path), run_name="__main__")

    uvicorn_run.assert_called_once()
    assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
