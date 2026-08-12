# Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

import json
import re
import logging
import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional, Tuple
from contextlib import asynccontextmanager
from html import unescape
from urllib.parse import quote as url_quote
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
import google.generativeai as genai
try:
    from spellchecker import SpellChecker
except ImportError:
    SpellChecker = None
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.gzip import GZipMiddleware

# ============================
# 1. CONFIGURATION & SHARED MODULES
# ============================
try:
    from backend.cache import article_cache, conversation_memory, search_cache
    from backend.config import (
        AI_QUERY_EXPANSION_TIMEOUT_SECONDS,
        ALLOWED_HOSTS,
        ALLOWED_ORIGINS,
        BASE_URL_CI,
        CORE_API_KEY,
        CSRF_HEADER_NAME,
        ENABLE_AI_QUERY_EXPANSION,
        ENABLE_SEMANTIC_EMBEDDINGS,
        ENVIRONMENT,
        FINAL_RECOMMENDATIONS,
        GEMINI_API_KEY,
        GEMINI_EMBEDDING_MODEL,
        GEMINI_MAX_CONCURRENCY,
        HTTP_LIMITS,
        HTTP_TIMEOUT,
        MAX_AI_EXPANDED_QUERIES,
        MAX_EMBEDDING_CANDIDATES,
        MAX_QUERY_LENGTH,
        MAX_RETRIES,
        MAX_RESULTS_LIMIT,
        NCBI_API_KEY,
        OCLC_CLIENT_ID,
        OCLC_CLIENT_SECRET,
        OCLC_FILTERED_MAX_PAGES,
        OCLC_SYMBOL,
        OCLC_TOKEN_URL,
        RATE_LIMIT_REQUESTS,
        RATE_LIMIT_STORAGE_URI,
        SEARCH_LIMIT,
        SEMANTIC_SCHOLAR_KEY,
    )
    from backend.models import (
        AISearchRequest,
        AISearchResponse,
        ArticleOut,
        Book,
        ErrorResponse,
        HealthResponse,
        SearchFilters,
        SessionResponse,
    )
    from backend.security import create_signed_session, require_admin_key, verify_signed_session
    from backend.search_planner import SearchPlannerService
except ImportError:
    from cache import article_cache, conversation_memory, search_cache
    from config import (
        AI_QUERY_EXPANSION_TIMEOUT_SECONDS,
        ALLOWED_HOSTS,
        ALLOWED_ORIGINS,
        BASE_URL_CI,
        CORE_API_KEY,
        CSRF_HEADER_NAME,
        ENABLE_AI_QUERY_EXPANSION,
        ENABLE_SEMANTIC_EMBEDDINGS,
        ENVIRONMENT,
        FINAL_RECOMMENDATIONS,
        GEMINI_API_KEY,
        GEMINI_EMBEDDING_MODEL,
        GEMINI_MAX_CONCURRENCY,
        HTTP_LIMITS,
        HTTP_TIMEOUT,
        MAX_AI_EXPANDED_QUERIES,
        MAX_EMBEDDING_CANDIDATES,
        MAX_QUERY_LENGTH,
        MAX_RETRIES,
        MAX_RESULTS_LIMIT,
        NCBI_API_KEY,
        OCLC_CLIENT_ID,
        OCLC_CLIENT_SECRET,
        OCLC_FILTERED_MAX_PAGES,
        OCLC_SYMBOL,
        OCLC_TOKEN_URL,
        RATE_LIMIT_REQUESTS,
        RATE_LIMIT_STORAGE_URI,
        SEARCH_LIMIT,
        SEMANTIC_SCHOLAR_KEY,
    )
    from models import (
        AISearchRequest,
        AISearchResponse,
        ArticleOut,
        Book,
        ErrorResponse,
        HealthResponse,
        SearchFilters,
        SessionResponse,
    )
    from security import create_signed_session, require_admin_key, verify_signed_session
    from search_planner import SearchPlannerService
HTTP_CLIENT: Optional[httpx.AsyncClient] = None
APP_VERSION = "0.1.0-beta"

# ============================
# SAFE HTTP HELPER
# ============================
def _shared_http_client() -> Optional[httpx.AsyncClient]:
    if HTTP_CLIENT is None or HTTP_CLIENT.is_closed:
        return None
    return HTTP_CLIENT


async def _http_get(url: str, **kwargs) -> httpx.Response:
    client = _shared_http_client()
    if client:
        return await client.get(url, **kwargs)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                 limits=HTTP_LIMITS) as temp_client:
        return await temp_client.get(url, **kwargs)


async def _http_post(url: str, **kwargs) -> httpx.Response:
    client = _shared_http_client()
    if client:
        return await client.post(url, **kwargs)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                                 limits=HTTP_LIMITS) as temp_client:
        return await temp_client.post(url, **kwargs)


async def _safe_get(url: str, params: dict = None, headers: dict = None,
                    timeout: float = 15.0) -> Optional[dict]:
    """GET with timeout + error handling. Returns parsed JSON or None."""
    try:
        resp = await _http_get(url, params=params, headers=headers or {}, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} from {url}")
            return None
        return resp.json()
    except httpx.TimeoutException:
        logger.warning(f"Timeout fetching {url}")
        return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


# ══════════════════════════════════════════════════════════════
# ARTICLE SEARCH SERVICE  (EXPANDED – 8 SEARCH ENGINES)
# ══════════════════════════════════════════════════════════════
class ArticleSearchService:
    """
    Search for real scholarly articles across MULTIPLE free APIs,
    then map each result to the best UAEU-subscribed database.

    Sources (8 total):
      1. CrossRef          – DOIs, broad coverage
      2. OpenAlex          – Open academic articles
      3. Semantic Scholar   – CS, biomedical, NLP papers  ★ NEW
      4. PubMed / NCBI     – Premier biomedical/medicine  ★ NEW
      5. Europe PMC        – Biomedical open-access       ★ NEW
      6. CORE              – Largest OA aggregator         ★ NEW
      7. DOAJ              – Open access journals          ★ NEW
      8. OCLC WorldCat     – Articles held by UAEU         ★ NEW
    """

    CROSSREF_API  = "https://api.crossref.org/works"
    OPENALEX_API  = "https://api.openalex.org/works"
    S2_API        = "https://api.semanticscholar.org/graph/v1/paper/search"
    PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    PUBMED_FETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
    EPMC_API      = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    CORE_API      = "https://api.core.ac.uk/v3/search/works"
    DOAJ_API      = "https://doaj.org/api/search/articles"
    OCLC_BRIEF    = "https://discovery.api.oclc.org/worldcat-org-ci/search/brief-bibs"
    OCLC_SYMBOL   = "UAE"
    ARTICLE_WORK_TYPES = {
        "journal-article", "proceedings-article", "posted-content",
        "peer-review", "report", "dissertation", "article", "preprint",
    }
    EXCLUDED_WORK_TYPES = {
        "book", "book-chapter", "book-section", "book-part", "book-series",
        "monograph", "reference-book", "reference-entry", "book-track",
    }
    QUERY_STOPWORDS = {
        "a", "an", "and", "are", "as", "at", "about", "article", "articles",
        "book", "books", "by", "for", "from", "give", "in", "journal",
        "journals", "latest", "me", "more", "new", "newer", "of", "on",
        "paper", "papers", "recent", "research", "show", "the", "to",
        "with", "عن", "في", "من", "على", "إلى", "و", "أو", "مقال",
        "مقالات", "بحث", "أبحاث", "كتب", "كتاب", "المزيد", "حديثة",
        "جديدة", "اعطني", "أعطني", "ابحث",
    }
    SHORT_QUERY_TERMS = {
        "ai", "ml", "nlp", "vr", "ar", "xr", "ui", "ux", "iot", "api",
    }
    SOURCE_QUALITY_SCORES = {
        "PubMed": 3.2,
        "Europe PMC": 3.0,
        "Semantic Scholar": 2.8,
        "CrossRef": 2.6,
        "OpenAlex": 2.5,
        "DOAJ": 2.3,
        "CORE": 2.0,
        "OCLC WorldCat": 1.8,
    }

    # Publisher → UAEU database mapping
    PUBLISHER_DATABASE_MAP = {
        "ieee": {"db": "IEEE Xplore", "url": "https://ieeexplore.ieee.org/",
                 "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "institute of electrical": {"db": "IEEE Xplore", "url": "https://ieeexplore.ieee.org/",
                                    "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "acm": {"db": "ACM Digital Library", "url": "https://dl.acm.org/",
                "search": "https://dl.acm.org/action/doSearch?AllField="},
        "association for computing": {"db": "ACM Digital Library", "url": "https://dl.acm.org/",
                                      "search": "https://dl.acm.org/action/doSearch?AllField="},
        "elsevier": {"db": "ScienceDirect", "url": "https://www.sciencedirect.com/",
                     "search": "https://www.sciencedirect.com/search?qs="},
        "sciencedirect": {"db": "ScienceDirect", "url": "https://www.sciencedirect.com/",
                          "search": "https://www.sciencedirect.com/search?qs="},
        "springer": {"db": "SpringerLink", "url": "https://link.springer.com/",
                     "search": "https://link.springer.com/search?query="},
        "lecture notes": {"db": "SpringerLink", "url": "https://link.springer.com/",
                          "search": "https://link.springer.com/search?query="},
        "wiley": {"db": "Wiley Online Library", "url": "https://onlinelibrary.wiley.com/",
                  "search": "https://onlinelibrary.wiley.com/action/doSearch?AllField="},
        "taylor": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                   "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "taylor & francis": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                             "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "informa": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                    "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "routledge": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                      "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "crc press": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                      "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "10.1201": {"db": "Taylor & Francis", "url": "https://www.tandfonline.com/",
                    "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "sage": {"db": "SAGE Journals", "url": "https://journals.sagepub.com/",
                 "search": "https://journals.sagepub.com/action/doSearch?AllField="},
        "nature": {"db": "Nature", "url": "https://www.nature.com/",
                   "search": "https://www.nature.com/search?q="},
        "mdpi": {"db": "MDPI", "url": "https://www.mdpi.com/",
                 "search": "https://www.mdpi.com/search?q="},
        "pubmed": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                   "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "ncbi": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                 "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "medical": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                    "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "clinical": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                     "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "nursing": {"db": "CINAHL (EBSCO)", "url": "https://www.ebsco.com/",
                    "search": "https://www.ebsco.com/products/research-databases/cinahl-database"},
        "emerald": {"db": "Emerald Insight", "url": "https://www.emerald.com/insight/",
                    "search": "https://www.emerald.com/insight/search?q="},
        "jstor": {"db": "JSTOR", "url": "https://www.jstor.org/",
                  "search": "https://www.jstor.org/action/doBasicSearch?Query="},
        "proquest": {"db": "ProQuest", "url": "https://www.proquest.com/",
                     "search": "https://www.proquest.com/"},
        "american chemical": {"db": "ACS Publications", "url": "https://pubs.acs.org/",
                              "search": "https://pubs.acs.org/action/doSearch?AllField="},
        "acs": {"db": "ACS Publications", "url": "https://pubs.acs.org/",
                "search": "https://pubs.acs.org/action/doSearch?AllField="},
        "physical review": {"db": "APS Physics", "url": "https://journals.aps.org/",
                            "search": "https://journals.aps.org/search?q="},
        "american physical": {"db": "APS Physics", "url": "https://journals.aps.org/",
                              "search": "https://journals.aps.org/search?q="},
        "oxford": {"db": "Oxford Academic", "url": "https://academic.oup.com/",
                   "search": "https://academic.oup.com/journals/search-results?q="},
        "cambridge": {"db": "Cambridge Core", "url": "https://www.cambridge.org/core/",
                      "search": "https://www.cambridge.org/core/search?q="},
        "bmc": {"db": "SpringerLink", "url": "https://link.springer.com/",
                "search": "https://link.springer.com/search?query="},
        "frontiers": {"db": "Frontiers", "url": "https://www.frontiersin.org/",
                      "search": "https://www.frontiersin.org/search?query="},
        "plos": {"db": "PLOS", "url": "https://journals.plos.org/",
                 "search": "https://journals.plos.org/plosone/search?q="},
        "hindawi": {"db": "Wiley Online Library", "url": "https://onlinelibrary.wiley.com/",
                    "search": "https://onlinelibrary.wiley.com/action/doSearch?AllField="},
        "iop": {"db": "IOPscience", "url": "https://iopscience.iop.org/",
                "search": "https://iopscience.iop.org/nsearch?terms="},
        "royal society": {"db": "Royal Society", "url": "https://royalsocietypublishing.org/",
                          "search": "https://royalsocietypublishing.org/action/doSearch?AllField="},
    }

    SUBJECT_DEFAULT_DB = {
        "computer": {"db": "IEEE Xplore", "url": "https://ieeexplore.ieee.org/",
                     "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "engineering": {"db": "IEEE Xplore", "url": "https://ieeexplore.ieee.org/",
                        "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "medical": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                    "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "health": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                   "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "business": {"db": "Business Source Ultimate (EBSCO)", "url": "https://www.ebsco.com/",
                     "search": "https://www.ebsco.com/products/research-databases/business-source-ultimate"},
        "psychology": {"db": "PsycINFO (EBSCO)", "url": "https://www.ebsco.com/",
                       "search": "https://www.ebsco.com/products/research-databases/psycinfo"},
        "education": {"db": "ERIC", "url": "https://eric.ed.gov/", "search": "https://eric.ed.gov/?q="},
        "default": {"db": "ProQuest One Academic", "url": "https://www.proquest.com/",
                    "search": "https://www.proquest.com/"},
    }

    UAEU_DATABASE_DIRECTORY = {
        "Academic Search Ultimate": {"db": "Academic Search Ultimate", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "ProQuest One Academic": {"db": "ProQuest One Academic", "url": "https://www.proquest.com/", "search": "https://www.proquest.com/"},
        "Scopus": {"db": "Scopus", "url": "https://www.scopus.com/", "search": "https://www.scopus.com/search/form.uri"},
        "Web of Science": {"db": "Web of Science", "url": "https://www.webofscience.com/", "search": "https://www.webofscience.com/wos/woscc/basic-search"},
        "ScienceDirect": {"db": "ScienceDirect", "url": "https://www.sciencedirect.com/", "search": "https://www.sciencedirect.com/search?qs="},
        "IEEE Xplore": {"db": "IEEE Xplore", "url": "https://ieeexplore.ieee.org/", "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "IEEE/IET Electronic Library (IEL)": {"db": "IEEE/IET Electronic Library (IEL)", "url": "https://ieeexplore.ieee.org/", "search": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText="},
        "ACM Digital Library": {"db": "ACM Digital Library", "url": "https://dl.acm.org/", "search": "https://dl.acm.org/action/doSearch?AllField="},
        "SpringerLink": {"db": "SpringerLink", "url": "https://link.springer.com/", "search": "https://link.springer.com/search?query="},
        "Taylor & Francis Online": {"db": "Taylor & Francis Online", "url": "https://www.tandfonline.com/", "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "Taylor & Francis": {"db": "Taylor & Francis Online", "url": "https://www.tandfonline.com/", "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "ASCE Library": {"db": "ASCE Library", "url": "https://ascelibrary.org/", "search": "https://ascelibrary.org/action/doSearch?AllField="},
        "ASME Digital Collections": {"db": "ASME Digital Collections", "url": "https://asmedigitalcollection.asme.org/", "search": "https://asmedigitalcollection.asme.org/search-results?page=1&q="},
        "ASTM Compass": {"db": "ASTM Compass", "url": "https://www.astm.org/", "search": "https://www.astm.org/search/fullsite-search.html?query="},
        "PubMed": {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/", "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "MEDLINE (PubMed interface)": {"db": "MEDLINE (PubMed interface)", "url": "https://pubmed.ncbi.nlm.nih.gov/", "search": "https://pubmed.ncbi.nlm.nih.gov/?term="},
        "CINAHL (EBSCOhost)": {"db": "CINAHL (EBSCOhost)", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "Cochrane Library": {"db": "Cochrane Library", "url": "https://www.cochranelibrary.com/", "search": "https://www.cochranelibrary.com/search?q="},
        "Access Medicine": {"db": "Access Medicine", "url": "https://accessmedicine.mhmedical.com/", "search": "https://accessmedicine.mhmedical.com/searchresults.aspx?q="},
        "BMJ Journals": {"db": "BMJ Journals", "url": "https://www.bmj.com/", "search": "https://www.bmj.com/search/advanced/"},
        "Embase": {"db": "Embase", "url": "https://www.embase.com/", "search": "https://www.embase.com/"},
        "ERIC": {"db": "ERIC", "url": "https://eric.ed.gov/", "search": "https://eric.ed.gov/?q="},
        "EduSearch": {"db": "EduSearch", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "Business Source Ultimate": {"db": "Business Source Ultimate", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "Emerald": {"db": "Emerald", "url": "https://www.emerald.com/insight/", "search": "https://www.emerald.com/insight/search?q="},
        "EIU Viewpoint": {"db": "EIU Viewpoint", "url": "https://viewpoint.eiu.com/", "search": "https://viewpoint.eiu.com/"},
        "Financial Times": {"db": "Financial Times", "url": "https://www.ft.com/", "search": "https://www.ft.com/search?q="},
        "WRDS": {"db": "Wharton Research Data Services", "url": "https://wrds-www.wharton.upenn.edu/", "search": "https://wrds-www.wharton.upenn.edu/"},
        "Nexis Uni": {"db": "Nexis Uni", "url": "https://www.nexisuni.com/", "search": "https://www.nexisuni.com/"},
        "East Law": {"db": "East Law", "url": "https://www.eastlaws.com/", "search": "https://www.eastlaws.com/"},
        "Qistas": {"db": "Qistas", "url": "https://qistas.com/", "search": "https://qistas.com/"},
        "Kluwer Arbitration online": {"db": "Kluwer Arbitration online", "url": "https://www.kluwerarbitration.com/", "search": "https://www.kluwerarbitration.com/"},
        "Index to Legal Periodicals & Books": {"db": "Index to Legal Periodicals & Books", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "AL Manhal": {"db": "AL Manhal", "url": "https://platform.almanhal.com/", "search": "https://platform.almanhal.com/Search/Advanced"},
        "AraBase (Dar Almandumah)": {"db": "AraBase (Dar Almandumah)", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "EcoLink (Dar Almandumah)": {"db": "EcoLink (Dar Almandumah)", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "HumanIndex": {"db": "HumanIndex", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "IslamicInfo": {"db": "IslamicInfo", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "Dissertations (Dar Almandumah)": {"db": "Dissertations (Dar Almandumah)", "url": "https://search.mandumah.com/", "search": "https://search.mandumah.com/Search/Results"},
        "PsycINFO": {"db": "PsycINFO", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "PsycARTICLES": {"db": "PsycARTICLES", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "SAGE Journals": {"db": "SAGE Journals", "url": "https://journals.sagepub.com/", "search": "https://journals.sagepub.com/action/doSearch?AllField="},
        "ACS Publications": {"db": "ACS Publications", "url": "https://pubs.acs.org/", "search": "https://pubs.acs.org/action/doSearch?AllField="},
        "SciFinder": {"db": "SciFinder", "url": "https://scifinder.cas.org/", "search": "https://scifinder.cas.org/"},
        "APS online": {"db": "APS online", "url": "https://journals.aps.org/", "search": "https://journals.aps.org/search?q="},
        "IOP": {"db": "IOP", "url": "https://iopscience.iop.org/", "search": "https://iopscience.iop.org/nsearch?terms="},
        "NASA Astrophysics Data System": {"db": "NASA Astrophysics Data System", "url": "https://ui.adsabs.harvard.edu/", "search": "https://ui.adsabs.harvard.edu/search/q="},
        "JSTOR": {"db": "JSTOR", "url": "https://www.jstor.org/", "search": "https://www.jstor.org/action/doBasicSearch?Query="},
        "Project Muse": {"db": "Project Muse", "url": "https://muse.jhu.edu/", "search": "https://muse.jhu.edu/search?action=search&query="},
        "Gale Resources": {"db": "Gale Resources", "url": "https://link.gale.com/apps/menu", "search": "https://link.gale.com/apps/menu"},
        "Oxford Journals Online": {"db": "Oxford Journals Online", "url": "https://academic.oup.com/", "search": "https://academic.oup.com/journals/search-results?q="},
        "Cambridge Core": {"db": "Cambridge Core", "url": "https://www.cambridge.org/core/", "search": "https://www.cambridge.org/core/search?q="},
        "ProQuest Dissertations and Theses": {"db": "ProQuest Dissertations and Theses", "url": "https://www.proquest.com/", "search": "https://www.proquest.com/"},
        "Agricola": {"db": "Agricola", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "AGRIS": {"db": "AGRIS", "url": "https://agris.fao.org/", "search": "https://agris.fao.org/search/en?query="},
        "CABI": {"db": "CABI", "url": "https://www.cabi.org/", "search": "https://www.cabi.org/cabebooks/search/?q="},
        "AgEcon": {"db": "AgEcon", "url": "https://ageconsearch.umn.edu/", "search": "https://ageconsearch.umn.edu/simple-search?query="},
        "FSTA": {"db": "Food Science and Technology Abstracts (FSTA)", "url": "https://www.ifis.org/fsta", "search": "https://www.ifis.org/fsta"},
        "GreenFILE": {"db": "GreenFILE", "url": "https://search.ebscohost.com/", "search": "https://search.ebscohost.com/"},
        "MathSciNet": {"db": "MathSciNet", "url": "https://mathscinet.ams.org/", "search": "https://mathscinet.ams.org/mathscinet/search/publications.html?pg1=ALLF&s1="},
        "Mathematics Zentralblatt Math": {"db": "Mathematics Zentralblatt Math", "url": "https://zbmath.org/", "search": "https://zbmath.org/?q="},
        "American Statistical Association": {"db": "American Statistical Association", "url": "https://www.tandfonline.com/", "search": "https://www.tandfonline.com/action/doSearch?AllField="},
        "Academic Video Online": {"db": "Academic Video Online", "url": "https://video.alexanderstreet.com/", "search": "https://video.alexanderstreet.com/search?searchstring="},
        "JoVE": {"db": "JoVE", "url": "https://www.jove.com/", "search": "https://www.jove.com/search?q="},
    }

    GENERAL_RESEARCH_DATABASES = (
        "Academic Search Ultimate", "ProQuest One Academic", "Scopus", "Web of Science"
    )

    SUBJECT_DATABASE_RULES = [
        (("ai", "artificial intelligence", "machine learning", "deep learning", "data science", "cyber", "security", "cryptography", "rsa", "software", "programming", "computer", "algorithm", "network", "iot"), ("IEEE/IET Electronic Library (IEL)", "ACM Digital Library", "Scopus", "ScienceDirect", "SpringerLink")),
        (("water", "hydrology", "hydraulic", "river", "basin", "climate", "environment", "sustainability", "ecology", "pollution", "renewable", "energy"), ("ScienceDirect", "Scopus", "Web of Science", "ASCE Library", "Taylor & Francis Online", "GreenFILE")),
        (("engineering", "civil", "mechanical", "materials", "construction", "geotechnical", "structural", "aerospace", "standard", "standards"), ("IEEE/IET Electronic Library (IEL)", "ASCE Library", "ASME Digital Collections", "ASTM Compass", "ScienceDirect", "SpringerLink")),
        (("medicine", "medical", "clinical", "health", "patient", "nursing", "hospital", "disease", "cancer", "diabetes", "drug", "therapy", "diagnosis", "pediatric", "pediatrics"), ("PubMed", "MEDLINE (PubMed interface)", "CINAHL (EBSCOhost)", "Cochrane Library", "Access Medicine", "BMJ Journals", "Embase")),
        (("education", "teaching", "learning", "student", "curriculum", "pedagogy", "school", "teacher", "classroom", "e-learning"), ("ERIC", "EduSearch", "Academic Search Ultimate", "ProQuest One Academic", "SAGE Journals")),
        (("business", "finance", "economics", "marketing", "accounting", "entrepreneur", "company", "commerce", "investment"), ("Business Source Ultimate", "Emerald", "EIU Viewpoint", "Financial Times", "ProQuest One Academic", "WRDS")),
        (("law", "legal", "court", "criminal", "justice", "arbitration", "legislation", "contract", "case law"), ("Nexis Uni", "East Law", "Qistas", "Kluwer Arbitration online", "Index to Legal Periodicals & Books")),
        (("arabic", "islamic", "quran", "hadith", "sharia", "fiqh", "literature", "أدب", "عربي", "إسلام", "فقه", "شريعة", "حديث", "قرآن"), ("AL Manhal", "AraBase (Dar Almandumah)", "IslamicInfo", "HumanIndex", "EcoLink (Dar Almandumah)")),
        (("psychology", "mental", "behavior", "cognitive", "therapy", "counseling", "psychiatry"), ("PsycINFO", "PsycARTICLES", "SAGE Journals", "Academic Search Ultimate", "ProQuest One Academic")),
        (("chemistry", "chemical", "molecule", "compound", "polymer", "reaction", "pharmaceutical"), ("ACS Publications", "SciFinder", "ScienceDirect", "SpringerLink", "Scopus")),
        (("physics", "astronomy", "astrophysics", "quantum", "particle", "electrochemical"), ("APS online", "IOP", "NASA Astrophysics Data System", "SpringerLink", "Web of Science")),
        (("humanities", "history", "philosophy", "literature", "culture", "media", "language", "translation"), ("JSTOR", "Project Muse", "Gale Resources", "Oxford Journals Online", "Cambridge Core")),
        (("dissertation", "thesis", "doctoral", "phd"), ("ProQuest Dissertations and Theses", "Dissertations (Dar Almandumah)", "ProQuest One Academic")),
        (("agriculture", "food", "nutrition", "fisheries", "forestry", "animal", "veterinary", "crop", "soil"), ("Agricola", "AGRIS", "CABI", "AgEcon", "FSTA")),
        (("statistics", "mathematics", "math", "probability", "statistical"), ("MathSciNet", "Mathematics Zentralblatt Math", "American Statistical Association", "Scopus", "Web of Science")),
        (("video", "documentary", "performance", "experiment", "laboratory"), ("Academic Video Online", "JoVE", "Gale Resources")),
    ]

    # ── Shared helpers ──

    @staticmethod
    def _is_junk_title(title: str) -> bool:
        if not title or title == "Unknown":
            return True
        junk = ["acknowledgment", "acknowledgement", "reviewer list",
                "table of contents", "editorial board", "front matter",
                "back matter", "erratum", "corrigendum", "correction"]
        t = title.lower()
        return any(j in t for j in junk) and len(title) < 80

    @staticmethod
    def _normalize_text(text: str) -> str:
        cleaned = re.sub(r"[^\w\u0600-\u06FF]+", " ", str(text or "").lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        tokens = []
        for token in ArticleSearchService._normalize_text(query).split():
            if token in ArticleSearchService.QUERY_STOPWORDS:
                continue
            if len(token) < 3 and token not in ArticleSearchService.SHORT_QUERY_TERMS:
                continue
            if token not in tokens:
                tokens.append(token)
        return tokens[:8]

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        text = ArticleSearchService._normalize_text(text)
        term = ArticleSearchService._normalize_text(term)
        if not text or not term:
            return False
        if " " in term:
            return term in text
        return f" {term} " in f" {text} "

    @staticmethod
    def _query_phrases(query: str) -> List[str]:
        tokens = ArticleSearchService._query_tokens(query)
        phrases = []
        normalized = ArticleSearchService._normalize_text(query)
        if len(normalized.split()) >= 2:
            phrases.append(normalized)
        for size in (4, 3, 2):
            if len(tokens) < size:
                continue
            for i in range(len(tokens) - size + 1):
                phrase = " ".join(tokens[i:i + size])
                if phrase not in phrases:
                    phrases.append(phrase)
        return phrases[:8]

    @staticmethod
    def _as_text_list(value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [str(v) for v in value.values() if v]
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    for key in ("name", "display_name", "title", "term", "descriptorName", "label"):
                        if item.get(key):
                            result.append(str(item[key]))
                            break
                elif item:
                    result.append(str(item))
            return result
        return [str(value)]

    @staticmethod
    def _article_subject_text(article: Dict[str, Any]) -> str:
        subject_parts = []
        for key in ("subjects", "subject", "keywords", "concepts", "fields_of_study"):
            subject_parts.extend(ArticleSearchService._as_text_list(article.get(key)))
        subject_parts.extend([
            article.get("journal", ""),
            article.get("publisher", ""),
            article.get("database", ""),
            article.get("article_type", ""),
        ])
        return ArticleSearchService._normalize_text(" ".join(subject_parts))

    @staticmethod
    def _semantic_expansion_terms(query: str) -> List[str]:
        normalized = ArticleSearchService._normalize_text(query)
        tokens = set(ArticleSearchService._query_tokens(query))

        def has_any(words: List[str]) -> bool:
            return any(word in tokens or ArticleSearchService._contains_term(normalized, word) for word in words)

        expansions = []
        topic_rules = [
            (
                has_any(["ai", "artificial intelligence", "machine learning", "deep learning"]) and
                has_any(["health", "healthcare", "medical", "medicine", "clinical", "patient"]),
                [
                    "machine learning in medicine", "clinical decision support",
                    "medical image analysis", "diagnosis", "health informatics",
                    "predictive analytics", "electronic health records",
                ],
            ),
            (
                has_any(["water", "hydrology", "river", "basin", "reservoir"]) and
                has_any(["management", "adaptive", "governance", "policy", "climate"]),
                [
                    "adaptive water management", "water governance", "integrated water resources",
                    "climate adaptation", "water security", "river basin management",
                    "reservoir operation", "drought management", "flood risk",
                ],
            ),
            (
                has_any(["cyber", "security", "cryptography", "network"]) and
                has_any(["attack", "intrusion", "threat", "malware", "privacy"]),
                [
                    "intrusion detection", "network security", "threat detection",
                    "malware analysis", "secure systems", "privacy preserving",
                ],
            ),
            (
                has_any(["education", "learning", "teaching", "student"]) and
                has_any(["technology", "online", "ai", "digital", "adaptive"]),
                [
                    "learning analytics", "educational technology", "adaptive learning",
                    "online learning", "student engagement", "intelligent tutoring",
                ],
            ),
        ]
        for enabled, terms in topic_rules:
            if enabled:
                expansions.extend(terms)

        if has_any(["ai", "artificial intelligence"]):
            expansions.extend(["machine learning", "deep learning", "neural networks", "intelligent systems"])
        if has_any(["sustainability", "climate", "environment"]):
            expansions.extend(["resilience", "adaptation", "environmental management", "sustainable development"])

        unique = []
        for term in expansions:
            normalized_term = ArticleSearchService._normalize_text(term)
            if normalized_term and normalized_term not in unique:
                unique.append(normalized_term)
        return unique[:14]

    @staticmethod
    def _semantic_search_query(query: str) -> str:
        expansions = ArticleSearchService._semantic_expansion_terms(query)
        if not expansions:
            return query
        expanded = f"{query} {' '.join(expansions[:5])}"
        return expanded[:MAX_QUERY_LENGTH]

    @staticmethod
    def _clean_expanded_query(candidate: Any, original_query: str) -> str:
        value = str(candidate or "").strip()
        value = re.sub(r"[`<>{}\[\]]", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" .,:;|-")
        value = value[:140].strip(" .,:;|-")
        if not value:
            return ""
        lowered = value.lower()
        if re.search(r"https?://|doi\.org|\bdoi\b|10\.\d{4,9}/", lowered):
            return ""
        if ArticleSearchService._normalize_text(value) == ArticleSearchService._normalize_text(original_query):
            return ""
        if len(ArticleSearchService._query_tokens(value)) < 2:
            return ""
        return value

    @staticmethod
    async def generate_ai_query_expansions(query: str,
                                          limit: int = None) -> List[str]:
        """Use Gemini only to create better article search phrases, never article records."""
        if not ENABLE_AI_QUERY_EXPANSION or not GEMINI_MODEL:
            return []

        max_queries = max(0, min(limit or MAX_AI_EXPANDED_QUERIES, 5))
        if max_queries <= 0:
            return []

        safe_query = re.sub(r"[`<>{}\[\]]", " ", str(query or ""))[:180].strip()
        if not safe_query:
            return []

        prompt = f"""You are improving search queries for scholarly article databases.

Original user topic:
"{safe_query}"

Return {max_queries} alternative academic search queries that preserve the same meaning but use better scholarly terminology.

Rules:
- Return ONLY valid JSON.
- Do NOT invent article titles, authors, DOI numbers, URLs, citations, or database names.
- Each query should be concise: 3 to 9 words.
- Prefer terms that work in CrossRef, OpenAlex, PubMed, DOAJ, and WorldCat.
- If the topic is Arabic, include useful English academic equivalents.
- Avoid generic filler like "research about" or "articles about".

JSON format:
{{
  "expanded_queries": [
    "academic search phrase"
  ]
}}"""

        try:
            result = await asyncio.wait_for(
                generate_gemini_content(prompt, temperature=0.2, max_output_tokens=260),
                timeout=AI_QUERY_EXPANSION_TIMEOUT_SECONDS,
            )
            if not result or not getattr(result, "text", ""):
                return []

            data = parse_llm_json(result.text)
            raw_queries = data.get("expanded_queries", [])
            if not isinstance(raw_queries, list):
                return []

            expanded = []
            seen = {ArticleSearchService._normalize_text(safe_query)}
            for raw in raw_queries:
                cleaned = ArticleSearchService._clean_expanded_query(raw, safe_query)
                normalized = ArticleSearchService._normalize_text(cleaned)
                if cleaned and normalized not in seen:
                    seen.add(normalized)
                    expanded.append(cleaned)
                if len(expanded) >= max_queries:
                    break

            if expanded:
                logger.info(f"AI query expansion for '{safe_query[:40]}': {expanded}")
            return expanded
        except asyncio.TimeoutError:
            logger.warning("AI query expansion timed out; continuing with normal article search")
            return []
        except json.JSONDecodeError as e:
            logger.warning(f"AI query expansion JSON fallback: {e}")
            return []
        except Exception as e:
            logger.warning(f"AI query expansion unavailable: {e}")
            return []

    @staticmethod
    def _year_freshness_score(year_value: Any) -> float:
        try:
            year = int(str(year_value or "0")[:4])
        except (ValueError, TypeError):
            return 0.0
        current_year = datetime.now().year
        if year >= current_year - 1:
            return 2.0
        if year >= current_year - 5:
            return 1.5
        if year >= current_year - 10:
            return 0.8
        if year >= current_year - 20:
            return 0.3
        return 0.0

    @staticmethod
    def _source_quality_score(article: Dict[str, Any]) -> float:
        source = article.get("source", "")
        score = ArticleSearchService.SOURCE_QUALITY_SCORES.get(source, 1.4)
        if article.get("doi"):
            score += 0.5
        if article.get("journal"):
            score += 0.4
        if article.get("article_type") in {"journal-article", "proceedings-article", "JournalArticle"}:
            score += 0.3
        return score

    @staticmethod
    def _is_valid_article_type(article_type: str) -> bool:
        if not article_type:
            return True
        normalized = article_type.lower().strip()
        if normalized in ArticleSearchService.EXCLUDED_WORK_TYPES:
            return False
        return normalized in ArticleSearchService.ARTICLE_WORK_TYPES

    @staticmethod
    def _article_relevance_score(article: Dict[str, Any], query: str) -> float:
        tokens = ArticleSearchService._query_tokens(query)
        if not tokens:
            return 0.0

        title_text = ArticleSearchService._normalize_text(article.get("title", ""))
        subject_text = ArticleSearchService._article_subject_text(article)
        authors_text = ArticleSearchService._normalize_text(article.get("authors", ""))
        searchable_text = ArticleSearchService._normalize_text(" ".join([
            article.get("title", ""),
            article.get("journal", ""),
            article.get("publisher", ""),
            article.get("authors", ""),
            article.get("abstract", ""),
            subject_text,
        ]))
        normalized_query = ArticleSearchService._normalize_text(query)

        title_matches = [token for token in tokens if ArticleSearchService._contains_term(title_text, token)]
        subject_matches = [token for token in tokens if ArticleSearchService._contains_term(subject_text, token)]
        author_matches = [token for token in tokens if ArticleSearchService._contains_term(authors_text, token)]
        all_matches = [token for token in tokens if ArticleSearchService._contains_term(searchable_text, token)]
        token_coverage = len(set(all_matches)) / max(len(tokens), 1)

        title_score = (len(set(title_matches)) / len(tokens)) * 9.0
        subject_score = (len(set(subject_matches)) / len(tokens)) * 4.5
        author_score = min(3.0, len(set(author_matches)) * 0.9)

        exact_phrase_score = 0.0
        for phrase in ArticleSearchService._query_phrases(query):
            if ArticleSearchService._contains_term(title_text, phrase):
                exact_phrase_score = max(exact_phrase_score, 8.0)
            elif ArticleSearchService._contains_term(subject_text, phrase):
                exact_phrase_score = max(exact_phrase_score, 5.5)
            elif ArticleSearchService._contains_term(searchable_text, phrase):
                exact_phrase_score = max(exact_phrase_score, 3.0)

        semantic_score = 0.0
        semantic_terms = ArticleSearchService._semantic_expansion_terms(query)
        for term in semantic_terms:
            if ArticleSearchService._contains_term(title_text, term):
                semantic_score += 2.6
            elif ArticleSearchService._contains_term(subject_text, term):
                semantic_score += 2.0
            elif ArticleSearchService._contains_term(searchable_text, term):
                semantic_score += 1.0
        semantic_score = min(7.0, semantic_score)

        freshness_score = ArticleSearchService._year_freshness_score(article.get("year"))
        source_quality_score = ArticleSearchService._source_quality_score(article)
        open_access_score = 1.1 if article.get("open_access") else 0.0
        link_score = 0.4 if article.get("link") else 0.0

        score = (
            title_score + subject_score + author_score + exact_phrase_score +
            semantic_score + freshness_score + source_quality_score +
            open_access_score + link_score
        )
        if len(tokens) >= 3 and not title_matches and not subject_matches and semantic_score < 2.0:
            score -= 3.0

        article["relevance_details"] = {
            "title": round(title_score, 2),
            "subject": round(subject_score, 2),
            "author": round(author_score, 2),
            "freshness": round(freshness_score, 2),
            "source_quality": round(source_quality_score, 2),
            "open_access": round(open_access_score, 2),
            "exact_phrase": round(exact_phrase_score, 2),
            "semantic": round(semantic_score, 2),
            "token_coverage": round(token_coverage, 2),
            "matched_terms": sorted(set(all_matches))[:8],
            "semantic_terms": semantic_terms[:8],
        }
        return max(score, 0.0)

    @staticmethod
    def _passes_article_relevance(article: Dict[str, Any], query: str, score: float) -> bool:
        tokens = ArticleSearchService._query_tokens(query)
        if not tokens:
            return True

        details = article.get("relevance_details", {})
        title_score = float(details.get("title", 0))
        subject_score = float(details.get("subject", 0))
        exact_score = float(details.get("exact_phrase", 0))
        semantic_score = float(details.get("semantic", 0)) + float(article.get("embedding_score", 0))
        coverage = float(details.get("token_coverage", 0))

        if exact_score >= 5.5:
            return True
        if semantic_score >= 4.0 and (title_score + subject_score) >= 3.0:
            return True

        minimum_score = max(7.5, len(tokens) * 2.4)
        if len(tokens) >= 3 and coverage < 0.45 and semantic_score < 4.0:
            return False
        if title_score == 0 and subject_score == 0 and semantic_score < 3.0:
            return False
        return score >= minimum_score

    @staticmethod
    def _db_entry(name: str) -> Dict:
        entry = ArticleSearchService.UAEU_DATABASE_DIRECTORY.get(name)
        if entry:
            return entry.copy()
        return {
            "db": name,
            "url": "https://www.uaeu.ac.ae/en/library/databases.shtml",
            "search": "https://www.uaeu.ac.ae/en/library/databases.shtml",
        }

    @staticmethod
    def _dedupe_db_entries(entries: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for entry in entries:
            name = entry.get("db", "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            unique.append(entry)
        return unique

    @staticmethod
    def recommend_uaeu_databases(query: str, article: Dict = None, limit: int = 4) -> List[Dict]:
        article = article or {}
        text = " ".join(str(part or "") for part in [
            query,
            article.get("title"),
            article.get("journal"),
            article.get("publisher"),
            article.get("doi"),
        ])
        normalized = ArticleSearchService._normalize_text(text)
        scores: Dict[str, float] = {}

        def add(names, amount: float):
            for name in names:
                scores[name] = scores.get(name, 0.0) + amount

        for keywords, db_names in ArticleSearchService.SUBJECT_DATABASE_RULES:
            hits = sum(1 for keyword in keywords if keyword.lower() in normalized)
            if hits:
                add(db_names, 10.0 + min(hits, 4) * 2.0)

        publisher_text = f"{article.get('publisher', '')} {article.get('doi', '')}".lower()
        for keyword, db_info in ArticleSearchService.PUBLISHER_DATABASE_MAP.items():
            if keyword in publisher_text:
                add((db_info.get("db", ""),), 8.0)

        if re.search(r"[\u0600-\u06FF]", text):
            add(("AL Manhal", "AraBase (Dar Almandumah)", "IslamicInfo", "HumanIndex"), 14.0)

        add(ArticleSearchService.GENERAL_RESEARCH_DATABASES, 1.0)

        ordered = sorted(
            scores,
            key=lambda name: (
                -scores[name],
                list(ArticleSearchService.UAEU_DATABASE_DIRECTORY).index(name)
                if name in ArticleSearchService.UAEU_DATABASE_DIRECTORY else 999,
                name,
            ),
        )
        return ArticleSearchService._dedupe_db_entries(
            [ArticleSearchService._db_entry(name) for name in ordered]
        )[:limit]

    @staticmethod
    def _find_database_for_article(journal: str, publisher: str,
                                   doi: str, title: str) -> Dict:
        article_context = {
            "title": title,
            "journal": journal,
            "publisher": publisher,
            "doi": doi,
        }
        suggested = ArticleSearchService.recommend_uaeu_databases(
            f"{title} {journal}", article_context, limit=4
        )

        matched_db = None
        match_type = "suggested"
        publisher_text = f"{publisher} {doi}".lower()
        for keyword, db_info in ArticleSearchService.PUBLISHER_DATABASE_MAP.items():
            if keyword in publisher_text:
                matched_db = db_info.copy()
                match_type = "publisher"
                break

        if not matched_db:
            matched_db = suggested[0].copy() if suggested else ArticleSearchService.SUBJECT_DEFAULT_DB["default"].copy()

        safe_doi = ""
        if doi:
            safe_doi = doi.strip()
            if any(c in safe_doi for c in '<>"{}|\\^`'):
                safe_doi = ""
            else:
                safe_doi = url_quote(safe_doi, safe='/:@')

        direct_link = ""
        if safe_doi and match_type == "publisher" and not safe_doi.lower().startswith("10.1201"):
            db_name = matched_db["db"].lower()
            doi_link_map = {
                "ieee":          f"https://ieeexplore.ieee.org/document/{safe_doi.split('/')[-1]}" if '/' in safe_doi else "",
                "acm":           f"https://dl.acm.org/doi/{safe_doi}",
                "springer":      f"https://link.springer.com/article/{safe_doi}",
                "wiley":         f"https://onlinelibrary.wiley.com/doi/{safe_doi}",
                "taylor":        f"https://www.tandfonline.com/doi/abs/{safe_doi}",
                "sage":          f"https://journals.sagepub.com/doi/{safe_doi}",
                "nature":        f"https://www.nature.com/articles/{safe_doi.split('/')[-1]}" if safe_doi.startswith("10.1038/") else "",
                "pubmed":        f"https://pubmed.ncbi.nlm.nih.gov/?term={safe_doi}",
                "oxford":        f"https://academic.oup.com/doi/{safe_doi}",
                "cambridge":     f"https://www.cambridge.org/core/journals/article/{safe_doi}",
                "emerald":       f"https://www.emerald.com/insight/content/doi/{safe_doi}",
                "acs":           f"https://pubs.acs.org/doi/{safe_doi}",
                "jstor":         f"https://www.jstor.org/stable/{safe_doi.split('/')[-1]}" if '/' in safe_doi else "",
            }
            for key, link in doi_link_map.items():
                if key in db_name and link:
                    direct_link = link
                    break

        suggestion_seed = [matched_db] if match_type == "publisher" else []
        matched_db["direct_link"] = direct_link
        matched_db["match_type"] = match_type
        matched_db["suggested_databases"] = ArticleSearchService._dedupe_db_entries(
            suggestion_seed + suggested
        )[:4]
        return matched_db

    # ═══════════════════════════════════════
    # 1. CrossRef
    # ═══════════════════════════════════════
    @staticmethod
    async def search_crossref(query: str, limit: int = 5,
                              year_from: int = None) -> List[Dict]:
        params = {
            "query": query, "rows": limit, "sort": "relevance",
            "select": "DOI,title,author,published-print,published-online,"
                      "container-title,publisher,type,subject"
        }
        if year_from:
            params["filter"] = f"from-pub-date:{year_from}"

        headers = {"User-Agent": "UAEU-Research-Assistant/2.0 (mailto:library@uaeu.ac.ae)"}
        data = await _safe_get(ArticleSearchService.CROSSREF_API,
                               params=params, headers=headers)
        if not data:
            return []

        articles = []
        for item in data.get("message", {}).get("items", []):
            work_type = (item.get("type", "") or "").lower()
            if not ArticleSearchService._is_valid_article_type(work_type):
                continue
            title = item.get("title", ["Unknown"])[0] if item.get("title") else "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = ", ".join([
                f"{a.get('given', '')} {a.get('family', '')}".strip()
                for a in item.get("author", [])[:3]
            ]) or "Unknown"
            year = ""
            for date_key in ("published-print", "published-online"):
                dp = item.get(date_key, {}).get("date-parts", [[]])
                if dp and dp[0]:
                    year = str(dp[0][0])
                    break
            journal = item.get("container-title", [""])[0] if item.get("container-title") else ""
            publisher = item.get("publisher", "")
            doi = item.get("DOI", "")
            db_info = ArticleSearchService._find_database_for_article(journal, publisher, doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": publisher, "doi": doi,
                "link": f"https://doi.org/{doi}" if doi else "",
                "source": "CrossRef",
                "article_type": work_type,
                "subjects": item.get("subject", [])[:8],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 2. OpenAlex
    # ═══════════════════════════════════════
    @staticmethod
    async def search_openalex(query: str, limit: int = 5,
                              year_from: int = None) -> List[Dict]:
        filters = [f"default.search:{query}"]
        if year_from:
            filters.append(f"publication_year:>{year_from - 1}")
        params = {"filter": ",".join(filters), "per_page": limit,
                  "sort": "relevance_score:desc"}
        headers = {"User-Agent": "UAEU-Research-Assistant/2.0"}
        data = await _safe_get(ArticleSearchService.OPENALEX_API,
                               params=params, headers=headers)
        if not data:
            return []
        articles = []
        for item in data.get("results", []):
            work_type = (item.get("type", "") or "").lower()
            if work_type and not ArticleSearchService._is_valid_article_type(work_type):
                continue
            title = item.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = ", ".join([
                a.get("author", {}).get("display_name", "")
                for a in item.get("authorships", [])[:3]
            ]) or "Unknown"
            year = str(item.get("publication_year", ""))
            source = item.get("primary_location", {})
            journal, publisher = "", ""
            if source and source.get("source"):
                journal = source["source"].get("display_name", "")
                publisher = source["source"].get("publisher", "") or ""
            subjects = [
                c.get("display_name", "")
                for c in item.get("concepts", [])[:8]
                if c.get("display_name")
            ]
            doi = item.get("doi", "") or ""
            if doi.startswith("https://doi.org/"):
                doi = doi.replace("https://doi.org/", "")
            oa_url = item.get("open_access", {}).get("oa_url", "")
            link = oa_url or (f"https://doi.org/{doi}" if doi else item.get("id", ""))
            db_info = ArticleSearchService._find_database_for_article(journal, publisher, doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": publisher, "doi": doi,
                "link": link, "open_access": bool(oa_url),
                "source": "OpenAlex",
                "article_type": work_type,
                "subjects": subjects,
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 3. Semantic Scholar  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_semantic_scholar(query: str, limit: int = 5,
                                      year_from: int = None) -> List[Dict]:
        """Semantic Scholar – great for CS, biomedical, and NLP papers."""
        params = {
            "query": query, "limit": limit,
            "fields": "title,authors,year,venue,externalIds,publicationTypes,"
                      "journal,isOpenAccess,openAccessPdf,fieldsOfStudy,s2FieldsOfStudy,abstract",
        }
        if year_from:
            params["year"] = f"{year_from}-"
        headers = {"User-Agent": "UAEU-Research-Assistant/2.0"}
        if SEMANTIC_SCHOLAR_KEY:
            headers["x-api-key"] = SEMANTIC_SCHOLAR_KEY

        data = await _safe_get(ArticleSearchService.S2_API,
                               params=params, headers=headers)
        if not data:
            return []
        articles = []
        for item in data.get("data", []):
            pub_types = item.get("publicationTypes", []) or []
            if pub_types and not any("article" in str(t).lower() or "conference" in str(t).lower()
                                     or "review" in str(t).lower() for t in pub_types):
                continue
            title = item.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = ", ".join([
                a.get("name", "") for a in item.get("authors", [])[:3]
            ]) or "Unknown"
            year = str(item.get("year", ""))
            journal_info = item.get("journal") or {}
            journal = journal_info.get("name", "") or item.get("venue", "")
            subjects = item.get("fieldsOfStudy", []) or []
            for field in item.get("s2FieldsOfStudy", []) or []:
                category = field.get("category") if isinstance(field, dict) else str(field)
                if category and category not in subjects:
                    subjects.append(category)
            ext_ids = item.get("externalIds", {}) or {}
            doi = ext_ids.get("DOI", "")
            oa_pdf = ""
            if item.get("openAccessPdf"):
                oa_pdf = item["openAccessPdf"].get("url", "")
            link = oa_pdf or (f"https://doi.org/{doi}" if doi else "")
            s2_id = ext_ids.get("CorpusId", "")
            if not link and s2_id:
                link = f"https://api.semanticscholar.org/CorpusID:{s2_id}"
            db_info = ArticleSearchService._find_database_for_article(
                journal, "", doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": "", "doi": doi,
                "link": link, "open_access": bool(oa_pdf),
                "source": "Semantic Scholar",
                "article_type": ", ".join(str(t) for t in pub_types[:3]),
                "subjects": subjects[:8],
                "abstract": item.get("abstract", "") or "",
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 4. PubMed / NCBI  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_pubmed(query: str, limit: int = 5,
                            year_from: int = None) -> List[Dict]:
        """PubMed – premier biomedical & life-science literature."""
        search_params = {
            "db": "pubmed", "term": query, "retmax": limit,
            "retmode": "json", "sort": "relevance",
        }
        if year_from:
            search_params["mindate"] = f"{year_from}/01/01"
            search_params["maxdate"] = f"{datetime.now().year}/12/31"
            search_params["datetype"] = "pdat"
        if NCBI_API_KEY:
            search_params["api_key"] = NCBI_API_KEY

        search_data = await _safe_get(ArticleSearchService.PUBMED_SEARCH,
                                      params=search_params)
        if not search_data:
            return []
        id_list = search_data.get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return []

        fetch_params = {"db": "pubmed", "id": ",".join(id_list), "retmode": "json"}
        if NCBI_API_KEY:
            fetch_params["api_key"] = NCBI_API_KEY
        fetch_data = await _safe_get(ArticleSearchService.PUBMED_FETCH,
                                     params=fetch_params)
        if not fetch_data:
            return []

        result_map = fetch_data.get("result", {})
        articles = []
        for pmid in id_list:
            item = result_map.get(pmid, {})
            if not item or isinstance(item, str):
                continue
            title = item.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = ", ".join([
                a.get("name", "") for a in item.get("authors", [])[:3]
            ]) or "Unknown"
            year = item.get("pubdate", "")[:4]
            journal = item.get("fulljournalname", "") or item.get("source", "")
            subjects = item.get("pubtype", []) or []
            doi = ""
            for aid in item.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break
            link = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            db_info = {"db": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/",
                       "search": "https://pubmed.ncbi.nlm.nih.gov/?term=",
                       "direct_link": link}
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": "NLM/NCBI", "doi": doi,
                "link": link, "pmid": pmid,
                "source": "PubMed",
                "subjects": subjects[:8] if isinstance(subjects, list) else [str(subjects)],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 5. Europe PMC  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_europe_pmc(query: str, limit: int = 5,
                                year_from: int = None) -> List[Dict]:
        """Europe PMC – biomedical & life-science open-access literature."""
        search_q = query
        if year_from:
            search_q += f" FIRST_PDATE:[{year_from} TO {datetime.now().year}]"
        params = {"query": search_q, "format": "json", "pageSize": limit,
                  "sort": "RELEVANCE", "resultType": "core"}
        data = await _safe_get(ArticleSearchService.EPMC_API, params=params)
        if not data:
            return []
        articles = []
        for item in data.get("resultList", {}).get("result", []):
            title = item.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = item.get("authorString", "Unknown") or "Unknown"
            if len(authors) > 100:
                authors = ", ".join(authors.split(", ")[:3]) + " et al."
            year = str(item.get("pubYear", ""))
            journal = item.get("journalTitle", "")
            doi = item.get("doi", "") or ""
            pmid = item.get("pmid", "")
            mesh_headings = item.get("meshHeadingList", {}).get("meshHeading", [])
            subjects = []
            for heading in mesh_headings[:8]:
                if isinstance(heading, dict):
                    subjects.append(heading.get("descriptorName", ""))
                else:
                    subjects.append(str(heading))
            is_oa = item.get("isOpenAccess", "N") == "Y"
            link = ""
            if is_oa and item.get("fullTextUrlList", {}).get("fullTextUrl"):
                for u in item["fullTextUrlList"]["fullTextUrl"]:
                    if u.get("documentStyle") == "pdf":
                        link = u.get("url", "")
                        break
                    if u.get("documentStyle") == "html":
                        link = u.get("url", "")
            if not link:
                link = f"https://doi.org/{doi}" if doi else (
                    f"https://europepmc.org/article/MED/{pmid}" if pmid else "")
            db_info = ArticleSearchService._find_database_for_article(journal, "", doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": "", "doi": doi,
                "link": link, "open_access": is_oa,
                "source": "Europe PMC",
                "subjects": [s for s in subjects if s],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 6. CORE  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_core(query: str, limit: int = 5,
                          year_from: int = None) -> List[Dict]:
        """CORE – world's largest aggregator of open-access research."""
        if not CORE_API_KEY:
            return []
        headers = {"Authorization": f"Bearer {CORE_API_KEY}"}
        q = f"{query} yearPublished:>={year_from}" if year_from else query
        params = {"q": q, "limit": limit}
        data = await _safe_get(ArticleSearchService.CORE_API,
                               params=params, headers=headers)
        if not data:
            return []
        articles = []
        for item in data.get("results", []):
            title = item.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors_list = item.get("authors", [])
            if authors_list and isinstance(authors_list[0], dict):
                authors = ", ".join([a.get("name", "") for a in authors_list[:3]])
            elif authors_list:
                authors = ", ".join(str(a) for a in authors_list[:3])
            else:
                authors = "Unknown"
            year = str(item.get("yearPublished", ""))
            journal_info = item.get("journals", [])
            journal = journal_info[0].get("title", "") if journal_info else ""
            publisher = item.get("publisher", "") or ""
            subjects = item.get("topics", []) or item.get("subjects", []) or []
            doi = item.get("doi", "") or ""
            if doi.startswith("https://doi.org/"):
                doi = doi.replace("https://doi.org/", "")
            download_url = item.get("downloadUrl", "") or ""
            link = download_url or (f"https://doi.org/{doi}" if doi else
                                    item.get("sourceFulltextUrls", [""])[0] if item.get(
                                        "sourceFulltextUrls") else "")
            db_info = ArticleSearchService._find_database_for_article(
                journal, publisher, doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": publisher, "doi": doi,
                "link": link, "open_access": True,
                "source": "CORE",
                "subjects": ArticleSearchService._as_text_list(subjects)[:8],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 7. DOAJ  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_doaj(query: str, limit: int = 5,
                          year_from: int = None) -> List[Dict]:
        """DOAJ – Directory of Open Access Journals. All results are OA."""
        search_url = f"{ArticleSearchService.DOAJ_API}/{query}"
        params = {"page": 1, "pageSize": limit}
        data = await _safe_get(search_url, params=params)
        if not data:
            return []
        articles = []
        for item in data.get("results", []):
            bibjson = item.get("bibjson", {})
            title = bibjson.get("title", "Unknown") or "Unknown"
            if ArticleSearchService._is_junk_title(title):
                continue
            authors = ", ".join([
                a.get("name", "") for a in bibjson.get("author", [])[:3]
            ]) or "Unknown"
            year = str(bibjson.get("year", ""))
            if year_from and year.isdigit() and int(year) < year_from:
                continue
            journal_info = bibjson.get("journal", {})
            journal = journal_info.get("title", "")
            publisher = journal_info.get("publisher", "")
            subjects = bibjson.get("subject", []) or []
            doi = ""
            for ident in bibjson.get("identifier", []):
                if ident.get("type") == "doi":
                    doi = ident.get("id", "")
                    break
            link = ""
            for lnk in bibjson.get("link", []):
                if lnk.get("type") == "fulltext":
                    link = lnk.get("url", "")
                    break
            if not link and doi:
                link = f"https://doi.org/{doi}"
            db_info = ArticleSearchService._find_database_for_article(
                journal, publisher, doi, title)
            articles.append({
                "title": title, "authors": authors, "year": year,
                "journal": journal, "publisher": publisher, "doi": doi,
                "link": link, "open_access": True,
                "source": "DOAJ",
                "subjects": ArticleSearchService._as_text_list(subjects)[:8],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        return articles

    # ═══════════════════════════════════════
    # 8. OCLC WorldCat Articles  ★ NEW
    # ═══════════════════════════════════════
    @staticmethod
    async def search_oclc_articles(query: str, limit: int = 5,
                                   year_from: int = None,
                                   oclc_token_manager=None) -> List[Dict]:
        """Search OCLC WorldCat for journal articles held by UAEU."""
        if not oclc_token_manager:
            return []
        token = await oclc_token_manager.get_token()
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/json",
                   "User-Agent": "UAEU-Library-AI/3.0"}
        params = {"q": query[:500], "heldBySymbol": ArticleSearchService.OCLC_SYMBOL,
                  "limit": limit, "orderBy": "bestMatch",
                  "itemSubType": "artchap"}
        try:
            resp = await _http_get(ArticleSearchService.OCLC_BRIEF,
                                   headers=headers, params=params, timeout=15.0)
            if resp.status_code != 200:
                del params["itemSubType"]
                resp = await _http_get(ArticleSearchService.OCLC_BRIEF,
                                       headers=headers, params=params, timeout=15.0)
                if resp.status_code != 200:
                    return []
            data = resp.json()
        except Exception as e:
            logger.error(f"OCLC article search error: {e}")
            return []

        articles = []
        for record in data.get("briefRecords", []):
            title = record.get("title", "Unknown")
            if ArticleSearchService._is_junk_title(title):
                continue
            gen_format = (record.get("generalFormat", "") or "").lower()
            spec_format = (record.get("specificFormat", "") or "").lower()
            combined_format = f"{gen_format} {spec_format}"
            is_article_format = any(kw in combined_format for kw in
                                     ["article", "journal", "serial", "periodical"])
            if not is_article_format:
                continue
            author = record.get("creator", "Unknown")
            year = record.get("date", "")[:4]
            if year_from and year.isdigit() and int(year) < year_from:
                continue
            oclc_number = record.get("oclcNumber", "")
            link = f"https://uaeu.on.worldcat.org/search?queryString={oclc_number}" if oclc_number else ""
            publisher = record.get("publisher", "") or ""
            db_info = ArticleSearchService._find_database_for_article("", publisher, "", title)
            articles.append({
                "title": title, "authors": author, "year": year,
                "journal": "", "publisher": publisher, "doi": "",
                "link": link, "source": "OCLC WorldCat",
                "_is_article": is_article_format,
                "subjects": ArticleSearchService._as_text_list(record.get("subjects", []))[:8],
                "database": db_info["db"], "database_url": db_info["url"],
                "database_search": db_info["search"],
                "direct_link": db_info.get("direct_link", ""),
                "database_match_type": db_info.get("match_type", "suggested"),
                "suggested_databases": db_info.get("suggested_databases", []),
            })
        articles.sort(key=lambda a: (0 if a.get("_is_article") else 1))
        for a in articles:
            a.pop("_is_article", None)
        return articles

    # ═══════════════════════════════════════
    # COMBINED SEARCH  (searches all 8 engines)
    # ═══════════════════════════════════════
    @staticmethod
    async def search_all(query: str, limit: int = 10,
                         year_from: int = None,
                         token_manager=None) -> Dict[str, Any]:
        """
        Search available article APIs, then use AI-expanded queries only when
        the first pass returns too few relevant articles.
        """
        results: Dict[str, Any] = {
            "articles": [], "total_found": 0,
            "sources_searched": [], "databases_found": set(),
            "expanded_queries": [],
        }

        cache_filters = {
            "limit": limit,
            "year_from": year_from,
            "with_oclc": bool(token_manager),
            "ai_query_expansion": ENABLE_AI_QUERY_EXPANSION,
        }
        cached_results = await article_cache.get(query, cache_filters)
        if cached_results is not None:
            return cached_results

        query_lower = query.lower()
        is_biomedical = any(kw in query_lower for kw in [
            "medical", "medicine", "health", "disease", "drug", "clinical",
            "patient", "nursing", "cancer", "diabetes", "therapy", "hospital",
            "pharmaceutical", "biomedical", "surgery", "diagnosis", "virus",
            "infection", "treatment", "dental", "cardio", "neuro",
        ])

        per_source = max(3, limit // 3)
        seen_titles = set()
        seen_dois = set()
        candidate_articles = []

        def add_task(task_list: List, name_list: List[str], query_list: List[str],
                     coro, source_name: str, search_query: str):
            task_list.append(coro)
            name_list.append(source_name)
            query_list.append(search_query)

        def absorb_search_results(search_results, source_names, source_queries):
            for i, articles in enumerate(search_results):
                source_name = source_names[i]
                source_query = source_queries[i]
                if isinstance(articles, Exception):
                    logger.error(f"Search error from {source_name}: {articles}")
                    continue
                if not articles:
                    continue
                if source_name not in results["sources_searched"]:
                    results["sources_searched"].append(source_name)

                for article in articles:
                    score = ArticleSearchService._article_relevance_score(article, query)
                    if source_query != query:
                        expanded_score = ArticleSearchService._article_relevance_score(article, source_query)
                        score = max(score, expanded_score * 0.82)
                        article["expanded_query"] = source_query

                    article["match_score"] = round(score, 2)

                    doi = str(article.get("doi", "")).lower().strip()
                    if doi and doi in seen_dois:
                        continue
                    if doi:
                        seen_dois.add(doi)

                    title_key = ArticleSearchService._normalize_text(article.get("title", ""))[:90]
                    if title_key in seen_titles:
                        continue
                    if title_key:
                        seen_titles.add(title_key)

                    candidate_articles.append(article)
                    results["databases_found"].add(article.get("database", ""))

        async def select_relevant_articles() -> Tuple[List[Dict], List[Dict]]:
            ranked_candidates = await SemanticEmbeddingService.apply_article_embeddings(
                query, candidate_articles
            )
            selected = []
            fallback = []
            for article in ranked_candidates:
                score = float(article.get("match_score", 0))
                if ArticleSearchService._passes_article_relevance(article, query, score):
                    selected.append(article)
                else:
                    fallback.append(article)
            return selected, fallback

        tasks = []
        source_names = []
        source_queries = []

        add_task(tasks, source_names, source_queries,
                 ArticleSearchService.search_crossref(query, per_source + 2, year_from),
                 "CrossRef", query)
        add_task(tasks, source_names, source_queries,
                 ArticleSearchService.search_openalex(query, per_source + 2, year_from),
                 "OpenAlex", query)
        add_task(tasks, source_names, source_queries,
                 ArticleSearchService.search_semantic_scholar(query, per_source, year_from),
                 "Semantic Scholar", query)
        add_task(tasks, source_names, source_queries,
                 ArticleSearchService.search_doaj(query, per_source, year_from),
                 "DOAJ", query)

        semantic_query = ArticleSearchService._semantic_search_query(query)
        if semantic_query != query:
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_openalex(semantic_query, per_source, year_from),
                     "OpenAlex Semantic", semantic_query)
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_semantic_scholar(semantic_query, per_source, year_from),
                     "Semantic Scholar Semantic", semantic_query)

        if is_biomedical:
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_pubmed(query, per_source + 2, year_from),
                     "PubMed", query)
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_europe_pmc(query, per_source, year_from),
                     "Europe PMC", query)
        else:
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_pubmed(query, 3, year_from),
                     "PubMed", query)

        if CORE_API_KEY:
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_core(query, per_source, year_from),
                     "CORE", query)

        if token_manager:
            add_task(tasks, source_names, source_queries,
                     ArticleSearchService.search_oclc_articles(query, per_source, year_from, token_manager),
                     "OCLC WorldCat", query)

        search_results = await asyncio.gather(*tasks, return_exceptions=True)
        absorb_search_results(search_results, source_names, source_queries)

        all_articles, fallback_articles = await select_relevant_articles()

        expansion_threshold = min(limit, 8)
        if len(all_articles) < expansion_threshold:
            expanded_queries = await ArticleSearchService.generate_ai_query_expansions(
                query, limit=MAX_AI_EXPANDED_QUERIES
            )
            if expanded_queries:
                results["expanded_queries"] = expanded_queries
                expanded_tasks = []
                expanded_source_names = []
                expanded_source_queries = []

                for expanded_query in expanded_queries:
                    add_task(
                        expanded_tasks, expanded_source_names, expanded_source_queries,
                        ArticleSearchService.search_crossref(expanded_query, per_source + 1, year_from),
                        "CrossRef AI Expansion", expanded_query,
                    )
                    add_task(
                        expanded_tasks, expanded_source_names, expanded_source_queries,
                        ArticleSearchService.search_openalex(expanded_query, per_source + 1, year_from),
                        "OpenAlex AI Expansion", expanded_query,
                    )
                    add_task(
                        expanded_tasks, expanded_source_names, expanded_source_queries,
                        ArticleSearchService.search_doaj(expanded_query, 2, year_from),
                        "DOAJ AI Expansion", expanded_query,
                    )
                    if is_biomedical:
                        add_task(
                            expanded_tasks, expanded_source_names, expanded_source_queries,
                            ArticleSearchService.search_pubmed(expanded_query, 2, year_from),
                            "PubMed AI Expansion", expanded_query,
                        )

                expanded_results = await asyncio.gather(*expanded_tasks, return_exceptions=True)
                absorb_search_results(
                    expanded_results, expanded_source_names, expanded_source_queries
                )
                all_articles, fallback_articles = await select_relevant_articles()

        def sort_key(art):
            yr = 0
            try:
                yr = int(art.get("year", "0"))
            except ValueError:
                pass
            oa_bonus = 1 if art.get("open_access") else 0
            return (float(art.get("match_score", 0)), yr, oa_bonus)

        if not all_articles and fallback_articles:
            fallback_articles.sort(key=sort_key, reverse=True)
            all_articles = fallback_articles[:limit]

        all_articles.sort(key=sort_key, reverse=True)
        results["articles"] = all_articles[:limit]
        results["total_found"] = len(all_articles)
        results["databases_found"] = list(results["databases_found"])

        expanded_note = (
            f" with AI expansions {results['expanded_queries']}"
            if results["expanded_queries"] else ""
        )
        logger.info(
            f"Article search: {len(all_articles)} total from "
            f"{results['sources_searched']}{expanded_note} → returning {len(results['articles'])}"
        )
        await article_cache.set(query, results, cache_filters)
        return results


# Logging setup
class SensitiveDataFilter(logging.Filter):
    SENSITIVE_PATTERN = re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|token|secret|client[_-]?secret)([\s:=]+)([^\s&]+)"
    )
    SENSITIVE_QUERY_PATTERN = re.compile(
        r"(?i)([?&](?:api_key|apikey|key|access_token|token|client_secret|secret)=)([^&#\s]+)"
    )

    @classmethod
    def redact(cls, value: Any) -> str:
        text = str(value)
        text = cls.SENSITIVE_PATTERN.sub(r"\1\2***REDACTED***", text)
        text = cls.SENSITIVE_QUERY_PATTERN.sub(r"\1***REDACTED***", text)
        return text

    def filter(self, record):
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
_sensitive_filter = SensitiveDataFilter()
logging.getLogger().addFilter(_sensitive_filter)
logger = logging.getLogger("uaeu-library-ai")
logger.addFilter(_sensitive_filter)
for noisy_logger_name in ("httpx", "httpcore"):
    noisy_logger = logging.getLogger(noisy_logger_name)
    noisy_logger.addFilter(_sensitive_filter)
    noisy_logger.setLevel(logging.WARNING)


# ============================
# 2-3. CACHE & CONVERSATION MEMORY
# ============================
# Implemented in cache.py and imported above.

# ============================
# 4. GEMINI AI SETUP
# ============================
GEMINI_MODEL = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        GEMINI_MODEL = genai.GenerativeModel("gemini-2.5-flash")
        logger.info("Gemini AI initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini AI: {e}")
        GEMINI_MODEL = None
else:
    logger.warning("Gemini API key not provided - AI features disabled")


GEMINI_SEMAPHORE = asyncio.Semaphore(GEMINI_MAX_CONCURRENCY)
EMBEDDING_SEMAPHORE = asyncio.Semaphore(max(GEMINI_MAX_CONCURRENCY, 2))


async def generate_gemini_content(prompt: str, temperature: float,
                                  max_output_tokens: int):
    if not GEMINI_MODEL:
        return None

    config_kwargs = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    try:
        generation_config = genai.types.GenerationConfig(
            **config_kwargs,
            response_mime_type="application/json",
        )
    except TypeError:
        generation_config = genai.types.GenerationConfig(**config_kwargs)

    async with GEMINI_SEMAPHORE:
        return await asyncio.to_thread(
            GEMINI_MODEL.generate_content,
            prompt,
            generation_config=generation_config,
        )


def safe_llm_field(value: Any, max_length: int = 120) -> str:
    text = str(value or "")[:max_length]
    text = re.sub(r'[`<>{}\\]', ' ', text)
    text = re.sub(
        r'\b(system|assistant|developer|user|ignore|disregard|forget)\s*:',
        '',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def parse_llm_json(raw_text: Any) -> Any:
    """Parse JSON even when the model wraps it in markdown or extra text."""
    text = str(raw_text or "").strip()
    if not text:
        raise json.JSONDecodeError("Empty LLM JSON response", "", 0)

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as original_error:
        last_error = original_error

    matching = {"{": "}", "[": "]"}
    opening = set(matching)
    closing = set(matching.values())

    for start, first_char in enumerate(text):
        if first_char not in opening:
            continue

        stack = [matching[first_char]]
        in_string = False
        escaped = False

        for pos in range(start + 1, len(text)):
            char = text[pos]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char in opening:
                stack.append(matching[char])
            elif char in closing:
                if not stack or char != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    candidate = text[start:pos + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError as candidate_error:
                        last_error = candidate_error
                    break

    raise last_error


# ============================
# 4A. OPTIONAL SEMANTIC EMBEDDINGS
# ============================
class SemanticEmbeddingService:
    _cache: Dict[str, List[float]] = {}
    _lock = asyncio.Lock()

    @staticmethod
    def _embedding_cache_key(text: str, task_type: str) -> str:
        raw = f"{task_type}|{ArticleSearchService._normalize_text(text)[:1200]}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _article_text(article: Dict[str, Any]) -> str:
        parts = [
            article.get("title", ""),
            article.get("journal", ""),
            article.get("publisher", ""),
            article.get("abstract", ""),
            " ".join(ArticleSearchService._as_text_list(article.get("subjects", []))),
        ]
        return " | ".join(part for part in parts if part)[:1200]

    @classmethod
    async def _embed(cls, text: str, task_type: str) -> List[float]:
        if not ENABLE_SEMANTIC_EMBEDDINGS or not GEMINI_API_KEY:
            return []
        text = (text or "").strip()
        if not text:
            return []

        key = cls._embedding_cache_key(text, task_type)
        async with cls._lock:
            cached = cls._cache.get(key)
            if cached is not None:
                return cached

        try:
            async with EMBEDDING_SEMAPHORE:
                result = await asyncio.to_thread(
                    genai.embed_content,
                    model=GEMINI_EMBEDDING_MODEL,
                    content=text[:1200],
                    task_type=task_type,
                )
            embedding = result.get("embedding") if isinstance(result, dict) else getattr(result, "embedding", None)
            if not embedding:
                return []
            vector = [float(v) for v in embedding]
        except Exception as e:
            logger.warning(f"Semantic embedding unavailable: {e}")
            return []

        async with cls._lock:
            if len(cls._cache) > 500:
                cls._cache.clear()
            cls._cache[key] = vector
        return vector

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(y * y for y in b) ** 0.5
        if not norm_a or not norm_b:
            return 0.0
        return dot / (norm_a * norm_b)

    @classmethod
    async def apply_article_embeddings(cls, query: str, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not articles or not ENABLE_SEMANTIC_EMBEDDINGS or not GEMINI_API_KEY:
            return articles

        query_embedding = await cls._embed(query, "retrieval_query")
        if not query_embedding:
            return articles

        candidates = sorted(
            articles,
            key=lambda item: float(item.get("match_score", 0)),
            reverse=True,
        )[:MAX_EMBEDDING_CANDIDATES]
        embeddings = await asyncio.gather(
            *[cls._embed(cls._article_text(article), "retrieval_document") for article in candidates],
            return_exceptions=True,
        )

        for article, embedding in zip(candidates, embeddings):
            if isinstance(embedding, Exception) or not embedding:
                continue
            similarity = max(0.0, cls._cosine_similarity(query_embedding, embedding))
            embedding_score = round(similarity * 6.0, 2)
            article["embedding_similarity"] = round(similarity, 3)
            article["embedding_score"] = embedding_score
            article["match_score"] = round(float(article.get("match_score", 0)) + embedding_score, 2)
            details = article.setdefault("relevance_details", {})
            details["embedding"] = embedding_score

        return articles


# ============================
# 5-6. MODELS & SESSION SECURITY
# ============================
# Implemented in models.py and security.py and imported above.

# ============================
# 7. TOKEN MANAGER
# ============================
class OCLCTokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._expires_at: datetime = datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    async def get_token(self) -> Optional[str]:
        async with self._lock:
            if self._token and datetime.now(timezone.utc) < self._expires_at:
                return self._token
            return await self._refresh_token()

    async def _refresh_token(self) -> Optional[str]:
        data = {"grant_type": "client_credentials", "scope": "WorldCatDiscoveryAPI"}
        for attempt in range(MAX_RETRIES):
            try:
                response = await _http_post(
                    OCLC_TOKEN_URL, data=data,
                    auth=(OCLC_CLIENT_ID, OCLC_CLIENT_SECRET),
                    timeout=HTTP_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()
                self._token = payload["access_token"]
                expires_in = int(payload.get("expires_in", 1200))
                self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
                logger.info("OCLC token refreshed")
                return self._token
            except httpx.HTTPError as e:
                logger.error(f"Token refresh failed (attempt {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return None
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.exception(f"Unexpected token error: {e}")
                return None


token_manager = OCLCTokenManager()


# ============================
# 7. QUERY PREPARATION SERVICE
# ============================
class QueryPreparationService:
    FOLLOWUP_PATTERNS = [
        r"more like", r"similar to", r"more by", r"more about",
        r"show me more", r"give me more", r"more books", r"more results",
        r"المزيد", r"أعطني المزيد", r"كتب أخرى", r"نتائج أكثر", r"المزيد من الكتب",
    ]
    MORE_BOOKS_PATTERNS = [
        r"more books", r"give me more", r"show me more", r"more results",
        r"more articles", r"more recent", r"more recent articles",
        r"recent articles", r"newer articles", r"latest articles",
        r"show more articles", r"give me more articles",
        r"المزيد من الكتب", r"أعطني المزيد", r"المزيد", r"كتب أخرى",
    ]
    RECENT_FOLLOWUP_PATTERNS = [
        r"more recent", r"recent articles", r"newer articles", r"latest articles",
        r"new articles", r"مقالات أحدث", r"أحدث المقالات",
    ]
    NEW_BOOKS_PATTERNS_EN = [
        r"new books?", r"recent books?", r"latest books?", r"newest",
        r"new articles?", r"recent articles?", r"latest articles?", r"newer articles?",
        r"more recent",
        r"published recently", r"from (\d{4})", r"after (\d{4})",
        r"last (\d+) years?", r"modern", r"contemporary", r"up.?to.?date",
        r"new release", r"newly released", r"new realsed",
    ]
    NEW_BOOKS_PATTERNS_AR = [
        r"كتب جديدة", r"كتب حديثة", r"أحدث الكتب", r"كتب معاصرة",
        r"من سنة (\d{4})", r"بعد سنة (\d{4})", r"آخر (\d+) سنوات?",
        r"إصدارات جديدة", r"صدر حديثاً",
    ]
    FORMAT_PATTERNS = {
        'ebook': [r"ebook", r"e-book", r"digital", r"online", r"إلكتروني", r"رقمي"],
        'print': [r"print\b", r"printed", r"physical", r"hardcover", r"paperback", r"مطبوع", r"ورقي"],
        'article': [
            r"articles?", r"articals?", r"articels?",
            r"journals?", r"journel", r"jornal",
            r"papers?", r"publications?", r"periodicals?",
            r"مقال", r"مقالات", r"بحث", r"أبحاث", r"دورية", r"دوريات"
        ],
        'audiobook': [r"audio", r"audiobook", r"صوتي"],
    }
    ALL_RESOURCE_PATTERNS_EN = [
        r"\ball resources\b",
        r"\ball library resources\b",
        r"\beverything in the library\b",
        r"\bbooks and articles\b",
        r"\bbooks,?\s+articles\b",
        r"\bbooks and journals\b",
        r"\ball materials\b",
    ]
    ALL_RESOURCE_PATTERNS_AR = [
        r"كل المصادر",
        r"جميع المصادر",
        r"كل موارد المكتبة",
        r"كتب ومقالات",
        r"كتب ودوريات",
    ]
    TYPO_CORRECTIONS_EN = (
        (r"\b(seaerch|serach|sreach|seach|saerch|searh|serch)\b", "search"),
        (r"\b(resrver|resrve|reserver|resrving)\b", "reserve"),
        (r"\b(resrved)\b", "reserved"),
        (r"\b(artical|articel|articl)\b", "article"),
        (r"\b(articals|articels|articls)\b", "articles"),
        (r"\b(journel|jornal|jouranl)\b", "journal"),
        (r"\b(journels|jornals|jouranls)\b", "journals"),
        (r"\b(databse|datebase|databaes)\b", "database"),
        (r"\b(databses|databeses|datebases|databaes)\b", "databases"),
        (r"\b(libary|libray|libarary|libaray)\b", "library"),
        (r"\b(browwing|borowing|borrowng)\b", "borrowing"),
        (r"\b(brrow|borow|broow)\b", "borrow"),
        (r"\b(broowed|borowed|brrowed)\b", "borrowed"),
        (r"\b(renwe|reanew|reneww)\b", "renew"),
        (r"\b(acess|acces|accses|accsess|acsess)\b", "access"),
        (r"\b(avaiable|avalible|availble|avilable)\b", "available"),
        (r"\b(loaction|locaton|locaiton)\b", "location"),
        (r"\b(faculity|faculity member|facuty)\b", "faculty"),
        (r"\b(coursee|corse|cours)\b", "course"),
        (r"\b(catlog|catlogue|catalouge)\b", "catalog"),
        (r"\b(compuer|comptuer|computor)\b", "computer"),
        (r"\b(scinece|sceince|sience)\b", "science"),
        (r"\b(phisics|phsyics|physcis)\b", "physics"),
        (r"\b(bilogy|biolgy|boilogy)\b", "biology"),
        (r"\b(chemestry|chemisty|chemitry)\b", "chemistry"),
        (r"\b(managment|mangement|managemnt)\b", "management"),
        (r"\b(enginering|enginnering|engineeing)\b", "engineering"),
    )

    SPELLCHECK_ACCEPTED_CORRECTIONS = {
        "account", "access", "agriculture", "article", "articles", "astronomy",
        "author", "available", "biology", "borrow", "borrowed", "borrowing",
        "business", "catalog", "checkout", "checkouts", "chemistry", "citation",
        "citations", "computer", "course", "database", "databases", "economics",
        "education", "ebook", "ebooks", "engineering", "environment", "faculty",
        "fee", "fees", "finance", "fine", "fines", "health", "history", "hours",
        "journal", "journals", "law", "library", "literature", "loan", "loans",
        "location", "management", "mathematics", "medical", "medicine", "nursing",
        "physics", "psychology", "renew", "reserve", "reserved", "resource",
        "resources", "science", "search", "security", "software", "statistics",
        "technology", "textbook", "textbooks", "water"
    }
    SPELLCHECK_PROTECTED_TERMS = {
        "uaeu", "oclc", "worldcat", "libchat", "doi", "isbn", "issn", "orcid",
        "pubmed", "crossref", "openalex", "scopus", "jstor", "ebsco", "proquest",
        "ieee", "acm", "sage", "wiley", "springer", "elsevier"
    }
    SPELLCHECK_MAX_TOKENS = 18
    _spellchecker = None
    _spellchecker_initialized = False
    @staticmethod
    def is_followup_query(query: str) -> bool:
        query_lower = query.lower()
        return any(re.search(pattern, query_lower, re.IGNORECASE)
                   for pattern in QueryPreparationService.FOLLOWUP_PATTERNS)

    @staticmethod
    def is_more_results_request(query: str) -> bool:
        query_lower = query.lower()
        return any(re.search(pattern, query_lower, re.IGNORECASE)
                   for pattern in QueryPreparationService.MORE_BOOKS_PATTERNS)

    @staticmethod
    def is_recent_followup_request(query: str) -> bool:
        query_lower = query.lower()
        return any(re.search(pattern, query_lower, re.IGNORECASE)
                   for pattern in QueryPreparationService.RECENT_FOLLOWUP_PATTERNS)

    @staticmethod
    def find_previous_content_query(history: List[Dict]) -> Optional[str]:
        for entry in reversed(history or []):
            previous_query = entry.get("query", "")
            if previous_query and not QueryPreparationService.is_more_results_request(previous_query):
                return previous_query
        return None

    @staticmethod
    def detect_year_preference(query: str, is_arabic: bool) -> Dict[str, Any]:
        current_year = datetime.now().year
        year_from = None
        year_to = current_year
        wants_new = False

        explicit_year_patterns = [
            r'from\s*(?:year)?\s*(\d{4})',
            r'(\d{4})\s*(?:and)?\s*(?:higher|above|onwards|or\s*later|to\s*now)',
            r'after\s*(?:year)?\s*(\d{4})',
            r'since\s*(?:year)?\s*(\d{4})',
            r'(\d{4})\s*[-–]\s*(?:present|now|\d{4})',
            r'(?:year|yr)\s*(\d{4})',
            r'من\s*(?:سنة)?\s*(\d{4})',
            r'بعد\s*(?:سنة)?\s*(\d{4})',
        ]

        for pattern in explicit_year_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                try:
                    extracted = int(match.group(1))
                    if 1900 < extracted <= current_year:
                        year_from = extracted
                        wants_new = True
                        break
                except (ValueError, IndexError):
                    pass

        if not wants_new:
            patterns = QueryPreparationService.NEW_BOOKS_PATTERNS_AR if is_arabic else QueryPreparationService.NEW_BOOKS_PATTERNS_EN
            for pattern in patterns:
                match = re.search(pattern, query, re.IGNORECASE)
                if match:
                    wants_new = True
                    if match.groups():
                        try:
                            extracted = int(match.group(1))
                            if extracted <= 20:
                                year_from = current_year - extracted
                        except (ValueError, IndexError):
                            pass
                    if not year_from:
                        year_from = current_year - 5
                    break

        return {"wants_new": wants_new, "year_from": year_from,
                "year_to": year_to if wants_new else None}

    @staticmethod
    def detect_format_preference(query: str) -> Optional[str]:
        query_lower = query.lower()
        for format_type, patterns in QueryPreparationService.FORMAT_PATTERNS.items():
            if any(re.search(p, query_lower) for p in patterns):
                return format_type
        return None

    @staticmethod
    def wants_all_resources(query: str, is_arabic: bool = False) -> bool:
        patterns = (QueryPreparationService.ALL_RESOURCE_PATTERNS_AR
                    if is_arabic else QueryPreparationService.ALL_RESOURCE_PATTERNS_EN)
        return any(re.search(pattern, query or "", re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _get_spellchecker():
        if QueryPreparationService._spellchecker_initialized:
            return QueryPreparationService._spellchecker
        QueryPreparationService._spellchecker_initialized = True
        if SpellChecker is None:
            return None
        try:
            checker = SpellChecker(language="en")
            checker.word_frequency.load_words(QueryPreparationService.SPELLCHECK_PROTECTED_TERMS)
            QueryPreparationService._spellchecker = checker
        except Exception as e:
            logger.warning(f"pyspellchecker unavailable: {e}")
            QueryPreparationService._spellchecker = None
        return QueryPreparationService._spellchecker

    @staticmethod
    def _restore_spellcheck_case(original: str, corrected: str) -> str:
        if original.isupper():
            return corrected.upper()
        if original[:1].isupper():
            return corrected.capitalize()
        return corrected

    @staticmethod
    def _spellcheck_protected_indices(tokens: List[str]) -> set:
        """Protect quoted titles and author names from dictionary corrections."""
        protected = set()
        inside_quote = False
        author_mode = False
        author_stop_words = {"about", "on", "from", "for", "with", "and"}

        for index, token in enumerate(tokens):
            quote_count = token.count('"')
            if inside_quote or quote_count:
                protected.add(index)

            cleaned = re.sub(r"[^A-Za-z]", "", token).lower()
            if cleaned in {"by", "author"}:
                author_mode = True
                protected.add(index)
            elif author_mode:
                if cleaned in author_stop_words:
                    author_mode = False
                else:
                    protected.add(index)

            if quote_count % 2:
                inside_quote = not inside_quote

        return protected

    @staticmethod
    def _apply_spellchecker_to_query(text: str) -> str:
        checker = QueryPreparationService._get_spellchecker()
        if not checker:
            return text

        tokens = text.split()
        if len(tokens) > QueryPreparationService.SPELLCHECK_MAX_TOKENS:
            return text

        protected_indices = QueryPreparationService._spellcheck_protected_indices(tokens)
        corrected_tokens = []
        for index, token in enumerate(tokens):
            if index in protected_indices:
                corrected_tokens.append(token)
                continue
            match = re.match(r"^([^A-Za-z]*)([A-Za-z]+)([^A-Za-z]*)$", token)
            if not match:
                corrected_tokens.append(token)
                continue

            prefix, word, suffix = match.groups()
            word_lower = word.lower()
            if (
                len(word_lower) < 4
                or word.isupper()
                or word_lower in QueryPreparationService.SPELLCHECK_PROTECTED_TERMS
            ):
                corrected_tokens.append(token)
                continue

            if not checker.unknown([word_lower]):
                corrected_tokens.append(token)
                continue

            suggestion = checker.correction(word_lower)
            if not suggestion:
                corrected_tokens.append(token)
                continue

            suggestion = suggestion.lower()
            if suggestion not in QueryPreparationService.SPELLCHECK_ACCEPTED_CORRECTIONS:
                corrected_tokens.append(token)
                continue
            if SequenceMatcher(None, word_lower, suggestion).ratio() < 0.72:
                corrected_tokens.append(token)
                continue

            fixed = QueryPreparationService._restore_spellcheck_case(word, suggestion)
            corrected_tokens.append(f"{prefix}{fixed}{suffix}")

        return " ".join(corrected_tokens)
    @staticmethod
    def normalize_user_input(query: str, is_arabic: bool = False) -> str:
        """Correct common library-search typos before routing and search planning."""
        text = re.sub(r"\s+", " ", (query or "").strip())
        if is_arabic or not text:
            return text

        for pattern, replacement in QueryPreparationService.TYPO_CORRECTIONS_EN:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

        text = QueryPreparationService._apply_spellchecker_to_query(text)

        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def extract_core_topic(query: str, is_arabic: bool) -> str:
        remove_words_en = [
            'give', 'me', 'show', 'find', 'search', 'get', 'want', 'need', 'looking', 'for',
            'please', 'can', 'you', 'i', 'would', 'like', 'could',
            'help', 'assist', 'assistance', 'finding', 'locate', 'located',
            'seaerch', 'serach', 'sreach', 'seach', 'saerch', 'searh', 'serch',
            'more', 'all', 'every', 'everything',
            'the', 'a', 'an', 'some', 'any',
            'new', 'recent', 'latest', 'newest', 'old', 'classic', 'modern', 'contemporary',
            'released', 'realsed', 'published', 'updated',
            'books', 'book', 'ebook', 'ebooks', 'print', 'printed', 'digital',
            'articles', 'article', 'articals', 'journal', 'journals', 'paper', 'papers',
            'publications', 'publication', 'resources', 'resource', 'materials',
            'library', 'catalog',
            'about', 'on', 'in', 'of', 'from', 'to', 'with', 'and', 'or', 'higher', 'above', 'below',
            'best', 'good', 'top', 'recommended', 'popular', 'famous',
            'beginner', 'beginners', 'advanced', 'intermediate', 'basic', 'intro', 'introduction',
            'learn', 'learning', 'study', 'studying', 'teach', 'teaching', 'tutorial', 'guide',
            'year', 'years',
        ]
        remove_words_ar = [
            'كتب', 'كتاب', 'جديدة', 'جديد', 'حديثة', 'حديث', 'أحدث',
            'عن', 'في', 'من', 'إلى', 'على', 'و', 'أو',
            'إلكتروني', 'مطبوع', 'رقمي',
            'أريد', 'أبحث', 'أعطني', 'ابحث', 'أظهر',
            'أفضل', 'أحسن', 'مميز',
            'تعلم', 'دراسة', 'للمبتدئين', 'للمتقدمين',
            'مقالات', 'مقال', 'بحث', 'أبحاث', 'دورية',
            'سنة', 'سنوات', 'أعلى', 'فوق',
        ]
        remove_words = remove_words_ar if is_arabic else remove_words_en

        cleaned = re.sub(r'\b(from|after|since|year)\s*\d{4}\b', '', query, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b\d{4}\s*(and)?\s*(higher|above|onwards|or later)\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(last|past)\s*\d+\s*(years?)\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\bبعد\s*(سنة)?\s*\d{4}\b', '', cleaned)
        cleaned = re.sub(r'\bمن\s*سنة\s*\d{4}\b', '', cleaned)
        cleaned = re.sub(r'\bآخر\s*\d+\s*سنوات?\b', '', cleaned)

        words = [
            re.sub(r"^[^\w\u0600-\u06FF]+|[^\w\u0600-\u06FF]+$", "", word)
            for word in cleaned.split()
        ]
        core_words = [
            w for w in words
            if w
            and w.lower() not in remove_words
            and (len(w) > 2 or (not is_arabic and w.isupper() and len(w) > 1))
        ]
        result = ' '.join(core_words).strip()

        if not result:
            basic_remove = set(remove_words + [
                'give', 'me', 'show', 'find', 'the', 'a', 'an',
                'أريد', 'أعطني',
            ])
            words = query.split()
            fallback_words = []
            for word in words:
                cleaned_word = re.sub(r"^[^\w\u0600-\u06FF]+|[^\w\u0600-\u06FF]+$", "", word)
                if not cleaned_word or cleaned_word.lower() in basic_remove or cleaned_word.isdigit():
                    continue
                fallback_words.append(cleaned_word)
            result = ' '.join(fallback_words)

        return result.strip() if result else query.strip()

    @staticmethod
    def prepare_query(user_input: str, history: List[Dict] = None) -> Dict[str, Any]:
        original = user_input.strip()
        is_arabic = bool(re.search(r"[\u0600-\u06FF]", original))
        normalized = QueryPreparationService.normalize_user_input(original, is_arabic)
        is_followup = QueryPreparationService.is_followup_query(normalized)
        year_info = QueryPreparationService.detect_year_preference(normalized, is_arabic)
        format_preference = QueryPreparationService.detect_format_preference(normalized)
        core_topic = QueryPreparationService.extract_core_topic(normalized, is_arabic)
        all_resources = QueryPreparationService.wants_all_resources(normalized, is_arabic)

        author_name = None
        search_type = "topic"
        author_patterns = [
            (r"books?\s+by\s+([a-zA-Z\s.\-']+?)(?:\s*$|\s+about|\s+on)", "en"),
            (r"by\s+author\s+([a-zA-Z\s.\-']+)", "en"),
            (r"([a-zA-Z\s.\-']+?)'s\s+books?", "en"),
            (r"author[:\s]+([a-zA-Z\s.\-']+)", "en"),
            (r'كتب\s+([^\s]+(?:\s+[^\s]+){0,2})', "ar"),
            (r'للكاتب\s+([^\s]+(?:\s+[^\s]+){0,2})', "ar"),
            (r'للمؤلف\s+([^\s]+(?:\s+[^\s]+){0,2})', "ar"),
        ]

        for pattern, lang in author_patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                potential_author = match.group(1).strip()
                stop_words = ['the', 'a', 'an', 'in', 'on', 'about', 'for', 'with', 'and', 'new', 'recent']
                author_words = [w for w in potential_author.split()
                                if w.lower() not in stop_words and len(w) > 1]
                if author_words and len(author_words) <= 4:
                    author_name = ' '.join(author_words)
                    search_type = "author"
                    break

        if search_type == "author" and author_name:
            search_query = f'au:"{author_name}"'
        else:
            search_query = core_topic if core_topic else normalized

        return {
            "original_query": original, "normalized_query": normalized,
            "search_query": search_query,
            "core_topic": core_topic, "search_type": search_type,
            "author_name": author_name, "is_arabic": is_arabic,
            "is_followup": is_followup,
            "wants_new_books": year_info["wants_new"],
            "year_from": year_info["year_from"], "year_to": year_info["year_to"],
            "format_preference": format_preference,
            "wants_all_resources": all_resources,
            "history_context": history[-3:] if history else []
        }


# ============================
# 8. OCLC DISCOVERY SERVICE
# ============================
class OCLCDiscoveryService:
    @staticmethod
    def _dedupe_page_records(page_records: List[Dict[str, Any]], seen_records: set) -> List[Dict[str, Any]]:
        unique_records = []
        for record in page_records:
            record_key = (
                record.get("oclcNumber")
                or f"{record.get('title', '')}|{record.get('creator', '')}|{record.get('date', '')}"
            )
            if record_key in seen_records:
                continue
            seen_records.add(record_key)
            unique_records.append(record)
        return unique_records

    @staticmethod
    async def search_books(query: str, limit: int = SEARCH_LIMIT,
                           filters: SearchFilters = None,
                           use_cache: bool = True) -> List[Dict[str, Any]]:
        filter_dict = filters.model_dump() if filters else None
        if use_cache:
            cached = await search_cache.get(query, filter_dict)
            if cached is not None:
                return cached

        token = await token_manager.get_token()
        if not token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to connect to library system. Please try again in a moment."
            )

        url = f"{BASE_URL_CI}/brief-bibs"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "UAEU-Library-AI/3.0"
        }

        search_query = query
        if filters and filters.language and filters.language != "any":
            lang_code = "ara" if filters.language == "ar" else "eng"
            search_query += f" la:{lang_code}"

        has_year_filter = bool(filters and (filters.year_from or filters.year_to))
        has_format_filter = bool(filters and filters.format and filters.format != "any")
        needs_client_filter = has_year_filter or has_format_filter
        page_limit = MAX_RESULTS_LIMIT if needs_client_filter else min(limit, MAX_RESULTS_LIMIT)
        max_pages = OCLC_FILTERED_MAX_PAGES if needs_client_filter else 1

        base_params = {
            "q": search_query[:MAX_QUERY_LENGTH],
            "heldBySymbol": OCLC_SYMBOL,
            "limit": page_limit,
            "orderBy": "bestMatch"
        }

        records: List[Dict[str, Any]] = []
        seen_records = set()
        pagination_param = "offset"

        try:
            for page_index in range(max_pages):
                params = base_params.copy()
                if page_index > 0:
                    page_start = page_index * page_limit
                    params[pagination_param] = page_start

                response = await _http_get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
                if response.status_code == 400 and page_index > 0 and pagination_param == "offset":
                    pagination_param = "startIndex"
                    params = base_params.copy()
                    params[pagination_param] = page_index * page_limit
                    response = await _http_get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)

                if response.status_code == 400 and page_index > 0:
                    logger.warning("OCLC pagination was not accepted; using available filtered records")
                    break

                response.raise_for_status()
                data = response.json()
                page_records = data.get("briefRecords", []) or []
                if not page_records:
                    break

                unique_page_records = OCLCDiscoveryService._dedupe_page_records(
                    page_records, seen_records
                )

                if page_index > 0 and page_records and not unique_page_records and pagination_param == "offset":
                    pagination_param = "startIndex"
                    params = base_params.copy()
                    params[pagination_param] = page_index * page_limit
                    response = await _http_get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
                    if response.status_code == 400:
                        logger.warning("OCLC pagination was not accepted; using available filtered records")
                        break
                    response.raise_for_status()
                    data = response.json()
                    page_records = data.get("briefRecords", []) or []
                    unique_page_records = OCLCDiscoveryService._dedupe_page_records(
                        page_records, seen_records
                    )
                    if not unique_page_records:
                        break

                filtered_page = unique_page_records
                if has_year_filter:
                    filtered_page = OCLCDiscoveryService._filter_by_year(
                        filtered_page, filters.year_from, filters.year_to)

                if has_format_filter:
                    filtered_page = OCLCDiscoveryService._filter_by_format(filtered_page, filters.format)

                records.extend(filtered_page)

                if len(records) >= limit:
                    records = records[:limit]
                    break
                if len(page_records) < page_limit:
                    break

            logger.info(f"OCLC search: {len(records)} results for '{query[:30]}...'")

            if use_cache and records:
                await search_cache.set(query, records, filter_dict)
            return records

        except httpx.HTTPStatusError as e:
            logger.error(f"OCLC API error: {e.response.status_code}")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="Library search service is temporarily unavailable.")
        except httpx.TimeoutException:
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                                detail="Search is taking too long. Please try a simpler search.")
        except Exception as e:
            logger.exception(f"Search error: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="An unexpected error occurred.")
    @staticmethod
    def _filter_by_format(records: List[Dict], format_type: str) -> List[Dict]:
        format_type = {
            "book": "print",
            "print_book": "print",
            "e-book": "ebook",
            "audio": "audiobook",
        }.get(format_type, format_type)

        if format_type not in {"ebook", "print", "audiobook", "article"}:
            return records

        filtered = []
        for record in records:
            book_format = (
                record.get("specificFormat", "") + " " +
                record.get("generalFormat", "") + " " +
                record.get("materialType", "")
            ).lower()
            title = record.get("title", "").lower()
            combined = book_format + " " + title

            is_ebook = any(kw in combined for kw in [
                "ebook", "e-book", "e book", "digital", "electronic",
                "online resource", "internet resource"
            ])
            is_audio = any(kw in combined for kw in ["audiobook", "audio book", "spoken", "sound recording"])
            is_article = any(kw in combined for kw in [
                "article", "journal", "periodical", "academic journal", "scholarly", "paper"
            ])
            is_print = (
                any(kw in combined for kw in ["print", "printbook", "print book", "hardcover", "paperback"])
                or ("book" in book_format and not is_ebook and not is_audio and not is_article)
            )

            if format_type == "ebook" and is_ebook:
                filtered.append(record)
            elif format_type == "print" and is_print:
                filtered.append(record)
            elif format_type == "audiobook" and is_audio:
                filtered.append(record)
            elif format_type == "article" and is_article:
                filtered.append(record)
        return filtered

    @staticmethod
    def _filter_by_year(records: List[Dict], year_from: int = None,
                        year_to: int = None) -> List[Dict]:
        filtered = []
        has_year_filter = bool(year_from or year_to)
        for record in records:
            try:
                match = re.search(r"(18|19|20)\d{2}", str(record.get("date", "")))
                if not match:
                    if has_year_filter:
                        continue
                    filtered.append(record)
                    continue

                year = int(match.group(0))
                if year_from and year < year_from:
                    continue
                if year_to and year > year_to:
                    continue
                filtered.append(record)
            except Exception:
                if not has_year_filter:
                    filtered.append(record)
        return filtered

    @staticmethod
    def _stringify_detail_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            for item in value:
                text = OCLCDiscoveryService._stringify_detail_value(item)
                if text:
                    return text
            return ""
        if isinstance(value, dict):
            preferred_keys = (
                "displayCallNumber", "callNumber", "displayName", "name",
                "description", "label", "value", "text", "locationName",
                "branchName", "shelvingLocation", "holdingLocation",
            )
            for key in preferred_keys:
                if key in value:
                    text = OCLCDiscoveryService._stringify_detail_value(value.get(key))
                    if text:
                        return text
            for nested in value.values():
                text = OCLCDiscoveryService._stringify_detail_value(nested)
                if text:
                    return text
        return ""

    @staticmethod
    def _find_holding_value(obj: Any, keys: Tuple[str, ...]) -> str:
        if isinstance(obj, dict):
            lower_map = {str(key).lower(): key for key in obj.keys()}
            for wanted in keys:
                actual = lower_map.get(wanted.lower())
                if actual is not None:
                    text = OCLCDiscoveryService._stringify_detail_value(obj.get(actual))
                    if text:
                        return text
            for value in obj.values():
                text = OCLCDiscoveryService._find_holding_value(value, keys)
                if text:
                    return text
        elif isinstance(obj, list):
            for item in obj:
                text = OCLCDiscoveryService._find_holding_value(item, keys)
                if text:
                    return text
        return ""

    @staticmethod
    def _normalize_availability(raw_status: str, is_digital: bool = False) -> Optional[str]:
        if is_digital:
            return "Online"
        text = str(raw_status or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if any(term in lowered for term in [
            "checked out", "on loan", "loaned", "borrowed", "due ", "not available",
            "unavailable", "غير متاح", "معار",
        ]):
            return "Checked out"
        if any(term in lowered for term in [
            "library use only", "in library", "in-library", "non circulating", "non-circulating",
            "reference only", "short loan", "room use", "داخل المكتبة",
        ]):
            return "Library use only"
        if any(term in lowered for term in [
            "online", "electronic", "ebook", "e-book", "full text", "available online", "رقمي",
        ]):
            return "Online"
        if any(term in lowered for term in [
            "available", "on shelf", "shelf", "متاح",
        ]):
            return "Available"
        # OCLC detailed holdings can return vague placeholders when live item
        # availability is not exposed; hide them instead of showing Unknown.
        return None

    @staticmethod
    def _clean_patron_location(value: Any) -> Optional[str]:
        text = OCLCDiscoveryService._stringify_detail_value(value)
        if not text:
            return None
        text = text.strip()
        generic_locations = {
            "uae",
            "uaeu",
            "united arab emirates",
            "united arab emirates university",
            "uaeu library",
            "uaeu libraries",
        }

        def normalize_location(candidate: str) -> str:
            return re.sub(r"[\W_]+", " ", candidate or "").lower().strip()

        normalized = normalize_location(text)
        label_stripped = re.sub(
            r"^(?:location|branch|holding location|library|site|campus|الموقع|الفرع)\s*[:：\-]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        normalized_label_stripped = normalize_location(label_stripped)

        # The OCLC symbol/institution name is not a patron-usable location, even
        # when it arrives as a labelled fallback such as "Location: UAE".
        if normalized in generic_locations or normalized_label_stripped in generic_locations:
            return None
        if normalized.startswith("location ") and normalize_location(text[9:]) in generic_locations:
            return None
        return text

    @staticmethod
    def _extract_holding_details(record: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        holdings = record.get("institutionHolding", {}).get("detailedHoldings", []) or []
        candidates = holdings if holdings else [record]

        call_keys = ("callNumber", "displayCallNumber", "classification", "shelfMark")
        branch_keys = ("branch", "library", "holdingLocation")
        shelf_keys = ("shelvingLocation", "shelfLocation", "collection", "subLocation", "location")
        availability_keys = (
            "availability", "availabilityStatus", "status", "circulationStatus",
            "itemAvailability", "availabilityDescription", "itemStatus",
        )

        for holding in candidates:
            if not result.get("call_number"):
                result["call_number"] = OCLCDiscoveryService._find_holding_value(holding, call_keys)
            if not result.get("branch_location"):
                result["branch_location"] = OCLCDiscoveryService._clean_patron_location(
                    OCLCDiscoveryService._find_holding_value(holding, branch_keys)
                )
            if not result.get("shelf_location"):
                result["shelf_location"] = OCLCDiscoveryService._clean_patron_location(
                    OCLCDiscoveryService._find_holding_value(holding, shelf_keys)
                )

            raw_availability = OCLCDiscoveryService._find_holding_value(holding, availability_keys)
            availability = OCLCDiscoveryService._normalize_availability(raw_availability)
            if availability:
                result["availability_status"] = availability

            if (
                result.get("call_number")
                and result.get("branch_location")
                and result.get("availability_status")
            ):
                break

        return {key: value for key, value in result.items() if value}

    @staticmethod
    async def get_book_details(oclc_number: str) -> Dict[str, Any]:
        token = await token_manager.get_token()
        if not token or not oclc_number or not oclc_number.isdigit():
            return {}

        url = f"{BASE_URL_CI}/bibs-detailed-holdings"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "UAEU-Library-AI/3.0",
        }
        params = {"oclcNumber": oclc_number, "heldBySymbol": OCLC_SYMBOL}

        try:
            response = await _http_get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            if response.status_code != 200:
                return {}
            data = response.json()
            records = data.get("briefRecords", [])
            if not records:
                return {}
            record = records[0]
            result = OCLCDiscoveryService._extract_holding_details(record)
            if record.get("subjects"):
                result["subjects"] = record["subjects"][:5]
            return result
        except Exception as e:
            logger.error(f"Failed to fetch book details: {e}")
            return {}

# ============================
# 9. AI ANALYSIS SERVICE
# ============================
class AIAnalysisService:
    @staticmethod
    def _sanitize_for_prompt(text: str) -> str:
        if not text:
            return ""
        sanitized = text[:MAX_QUERY_LENGTH]
        patterns_to_remove = [
            r'ignore\s+(previous|above|all)\s+instructions?',
            r'disregard\s+(previous|above|all)',
            r'forget\s+(previous|above|all)',
            r'new\s+instructions?:',
            r'system\s*:', r'assistant\s*:', r'user\s*:',
            r'\[INST\]', r'\[/INST\]', r'<\|.*?\|>',
        ]
        for pattern in patterns_to_remove:
            sanitized = re.sub(pattern, '', sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r'[{}\[\]<>]{3,}', '', sanitized)
        return sanitized.strip()

    # ── Generic description patterns to reject ──
    _GENERIC_PATTERNS = [
        r'^Written by\b', r'^By\b', r'^Published in\b', r'^From\b',
        r'^A book by\b', r'^An article\b', r'^This is\b',
        r'^كتاب للمؤلف\b', r'^مقالة منشورة\b', r'^للمؤلف\b', r'^من تأليف\b',
    ]

    @staticmethod
    def _is_generic_description(text: str) -> bool:
        """Check if a description is just 'Written by X' or 'Published in Y'."""
        if not text or len(text) < 15:
            return True
        for pattern in AIAnalysisService._GENERIC_PATTERNS:
            if re.match(pattern, text.strip(), re.IGNORECASE):
                # Short match = generic  (e.g. "Written by John (2023).")
                if len(text) < 60:
                    return True
        return False

    @staticmethod
    def _is_title_echo_description(text: str, title: str) -> bool:
        if not text or not title:
            return False
        text_norm = ArticleSearchService._normalize_text(text)
        title_norm = ArticleSearchService._normalize_text(title)
        if not text_norm or not title_norm:
            return False
        short_title = title_norm[:70]
        starts_like_echo = text_norm.startswith(("covers ", "examines ", "discusses ", "explores "))
        return starts_like_echo and short_title[:45] in text_norm and len(text_norm) < len(title_norm) + 14

    @staticmethod
    def _generate_description_from_title(
            title: str, author: str, year: str,
            extra: str, core_topic: str,
            is_arabic: bool, is_article: bool = False
    ) -> str:
        """
        Generate a meaningful description by analysing the TITLE.
        Used by fallbacks AND as a safety net when AI returns junk.
        """
        if not title or title.lower() in ("unknown", "untitled"):
            if is_arabic:
                return f"مصدر أكاديمي متعلق بـ {core_topic}." if core_topic else ""
            return f"Academic resource related to {core_topic}." if core_topic else ""

        title_lower = title.lower()

        # Detect edition markers
        edition = ""
        ed_match = re.search(r'(\d+)(?:st|nd|rd|th)\s+edition', title_lower)
        if ed_match:
            edition = f" ({ed_match.group(0)})"

        # Detect "introduction", "handbook", "guide", etc.
        # Ordered longest-first so "systematic review" matches before "review"
        content_type = ""
        type_map = [
            ("systematic review", "مراجعة منهجية في", "A systematic review of"),
            ("meta-analysis", "تحليل تجميعي في", "A meta-analysis of"),
            ("case study", "دراسة حالة في", "A case study on"),
            ("introduction", "مقدمة في", "An introduction to"),
            ("handbook", "مرجع شامل في", "A comprehensive handbook on"),
            ("fundamentals", "أساسيات", "Fundamentals of"),
            ("principles", "مبادئ", "Principles of"),
            ("proceedings", "أبحاث مؤتمر في", "Conference proceedings on"),
            ("comparison", "مقارنة في", "A comparison of"),
            ("evaluation", "تقييم في", "An evaluation of"),
            ("framework", "إطار عمل في", "A framework for"),
            ("tutorial", "شرح تعليمي في", "A tutorial on"),
            ("survey", "مسح بحثي في", "A research survey of"),
            ("review", "مراجعة علمية في", "A review of"),
            ("guide", "دليل في", "A practical guide to"),
        ]

        matched_keyword = ""
        # Only match if keyword appears in the first ~60 chars (near the title start)
        title_start = title_lower[:60]
        for keyword, ar_label, en_label in type_map:
            if keyword in title_start:
                content_type = ar_label if is_arabic else en_label
                matched_keyword = keyword
                break

        # Extract the subject from the title
        subject = title.strip()
        if matched_keyword:
            # Find the keyword in the title and take everything AFTER it + connector
            # e.g. "A Systematic Review of Deep Learning" → "Deep Learning"
            # e.g. "Practical Guide to Cloud Security"    → "Cloud Security"
            pattern = rf'(?i).*?\b{re.escape(matched_keyword)}\b\s*(?:of|on|in|for|to|:|–|-)?\s*'
            cleaned = re.sub(pattern, '', subject, count=1).strip()
            if cleaned and len(cleaned) > 5:
                subject = cleaned
            else:
                # Keyword was at the end — don't use content_type (would duplicate)
                content_type = ""
                subject = re.sub(r'^(?:the|a|an)\s+', '', subject, flags=re.IGNORECASE).strip()
        else:
            # No keyword matched — just remove leading articles
            subject = re.sub(r'^(?:the|a|an)\s+', '', subject, flags=re.IGNORECASE).strip()

        # Trim to a reasonable length
        if len(subject) > 140:
            subject = subject[:137] + "..."
        # Capitalize first letter
        if subject and subject[0].islower():
            subject = subject[0].upper() + subject[1:]

        def clean_piece(value: str) -> str:
            value = re.sub(r'\s+', ' ', value or '').strip(" .:-")
            return value[:117] + "..." if len(value) > 120 else value

        pattern_desc = ""
        colon_match = re.match(r'^(.+?)\s*:\s*(.+)$', subject)
        response_match = re.match(r'^(.+?)\s+in response to\s+(.+)$', subject, re.IGNORECASE)
        using_match = re.match(r'^(.+?)\s+(?:using|through|via)\s+(.+)$', subject, re.IGNORECASE)
        for_match = re.match(r'^(.+?)\s+for\s+(.+)$', subject, re.IGNORECASE)
        case_match = re.match(r'^(.+?)\s+(?:the case of|case study of|case study on)\s+(.+)$', subject, re.IGNORECASE)

        if response_match:
            left, right = clean_piece(response_match.group(1)), clean_piece(response_match.group(2))
            pattern_desc = (
                f"\u064a\u062d\u0644\u0644 \u0643\u064a\u0641 \u064a\u0633\u062a\u062c\u064a\u0628 {left} \u0644\u0640 {right}"
                if is_arabic else f"Examines how {left} responds to {right}"
            )
        elif case_match:
            left, right = clean_piece(case_match.group(1)), clean_piece(case_match.group(2))
            pattern_desc = (
                f"\u064a\u0633\u062a\u062e\u062f\u0645 {right} \u0643\u062f\u0631\u0627\u0633\u0629 \u062d\u0627\u0644\u0629 \u0644\u062a\u062d\u0644\u064a\u0644 {left}"
                if is_arabic else f"Uses {right} as a case study to analyze {left}"
            )
        elif colon_match:
            left, right = clean_piece(colon_match.group(1)), clean_piece(colon_match.group(2))
            right = re.sub(r'^(?:on\s+)?the\s+need\s+for\s+', '', right, flags=re.IGNORECASE).strip()
            if right and right[0].isupper():
                right = right[0].lower() + right[1:]
            pattern_desc = (
                f"\u064a\u0628\u062d\u062b {left} \u0645\u0639 \u0627\u0644\u062a\u0631\u0643\u064a\u0632 \u0639\u0644\u0649 {right}"
                if is_arabic else f"Explores {left}, focusing on {right}"
            )
        elif using_match:
            left, right = clean_piece(using_match.group(1)), clean_piece(using_match.group(2))
            pattern_desc = (
                f"\u064a\u0648\u0636\u062d \u0643\u064a\u0641 \u064a\u064f\u0633\u062a\u062e\u062f\u0645 {right} \u0644\u062f\u0631\u0627\u0633\u0629 {left}"
                if is_arabic else f"Shows how {right} is used to study or improve {left}"
            )
        elif for_match and len(for_match.group(1)) > 6:
            left, right = clean_piece(for_match.group(1)), clean_piece(for_match.group(2))
            pattern_desc = (
                f"\u064a\u0631\u0643\u0632 \u0639\u0644\u0649 {left} \u0644\u062a\u0637\u0628\u064a\u0642\u0627\u062a {right}"
                if is_arabic else f"Focuses on {left} for applications in {right}"
            )

        # Build the description
        if is_arabic:
            if pattern_desc:
                desc = pattern_desc
            elif content_type:
                desc = f"{content_type} {subject}"
            else:
                item = "مقالة" if is_article else "كتاب"
                desc = f"{item} يحلل {subject}"
            if year:
                desc += f" ({year})"
            desc += "."
            if edition:
                desc = desc.rstrip(".") + f"، طبعة محدّثة{edition}."
        else:
            if pattern_desc:
                desc = pattern_desc
            elif content_type:
                desc = f"{content_type} {subject}"
            else:
                desc = f"Examines {subject}" if is_article else f"Introduces and explains {subject}"
            if year:
                desc += f" ({year})"
            desc += "."
            if edition:
                desc = desc.rstrip(".") + f", updated{edition}."

        return desc

    @staticmethod
    def _clean_abstract_summary(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", str(text or ""))
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"(?i)^(abstract|background|objective|objectives)[:\s-]+", "", text)
        text = re.sub(r"(?i)\b(display omitted|copyright .*|all rights reserved).*", "", text).strip()
        if len(text) < 70:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        summary = sentences[0].strip()
        if len(summary) < 55 and len(sentences) > 1:
            summary = f"{summary} {sentences[1].strip()}"
        if len(summary) > 260:
            summary = summary[:257].rsplit(" ", 1)[0] + "..."
        return summary

    @staticmethod
    def _generate_article_description_from_metadata(
            article: Dict[str, Any], core_topic: str, is_arabic: bool
    ) -> str:
        if not is_arabic:
            abstract_summary = AIAnalysisService._clean_abstract_summary(
                article.get("abstract", "")
            )
            if abstract_summary:
                return abstract_summary

        base = AIAnalysisService._generate_description_from_title(
            title=article.get("title", ""),
            author=article.get("authors", ""),
            year=article.get("year", ""),
            extra=article.get("journal", ""),
            core_topic=core_topic,
            is_arabic=is_arabic,
            is_article=True,
        )

        subjects = [
            str(subject).strip()
            for subject in ArticleSearchService._as_text_list(article.get("subjects", []))[:3]
            if str(subject).strip()
        ]
        if subjects and not is_arabic:
            subject_text = ", ".join(subjects)
            if subject_text.lower() not in base.lower():
                return base.rstrip(".") + f", with subject focus on {subject_text}."
        return base

    @staticmethod
    def _sanitize_ai_descriptions(
            analysis: Dict, books_data: List[Dict],
            core_topic: str, is_arabic: bool
    ) -> Dict:
        """
        Post-process AI output: replace any generic 'Written by X' descriptions
        with meaningful ones generated from the title.
        """
        recs = analysis.get("recommendations", [])
        for rec in recs:
            why = rec.get("why_recommended", "")
            idx = rec.get("index", 0)
            if isinstance(idx, int) and 0 <= idx < len(books_data):
                book = books_data[idx]
                if (AIAnalysisService._is_generic_description(why) or
                        AIAnalysisService._is_title_echo_description(why, book.get("title", ""))):
                    rec["why_recommended"] = AIAnalysisService._generate_description_from_title(
                        title=book.get("title", ""),
                        author=book.get("creator", ""),
                        year=book.get("date", "")[:4],
                        extra=book.get("specificFormat", ""),
                        core_topic=core_topic,
                        is_arabic=is_arabic,
                    )
        return analysis

    @staticmethod
    def _sanitize_ai_article_descriptions(
            selected: List[Dict], core_topic: str, is_arabic: bool
    ) -> List[Dict]:
        """
        Post-process AI article output: replace generic descriptions.
        """
        for art in selected:
            why = art.get("why_recommended", "")
            if (AIAnalysisService._is_generic_description(why) or
                    AIAnalysisService._is_title_echo_description(why, art.get("title", ""))):
                art["why_recommended"] = AIAnalysisService._generate_article_description_from_metadata(
                    art, core_topic, is_arabic
                )
        return selected

    @staticmethod
    def _score_book_for_query(book: Dict[str, Any], query_info: Dict[str, Any]) -> float:
        query = query_info.get("core_topic") or query_info.get("original_query", "")
        resource = {
            "title": book.get("title", ""),
            "authors": book.get("creator", ""),
            "year": book.get("date", ""),
            "publisher": book.get("publisher", ""),
            "subjects": book.get("subjects", []) or book.get("subject", []),
            "article_type": book.get("specificFormat") or book.get("generalFormat") or "",
            "source": "OCLC WorldCat",
            "link": book.get("oclcNumber", ""),
            "open_access": is_digital_format(book.get("specificFormat") or book.get("generalFormat") or ""),
        }
        return round(ArticleSearchService._article_relevance_score(resource, query), 2)

    @staticmethod
    def _book_duplicate_key(book: Dict[str, Any]) -> str:
        title = ArticleSearchService._normalize_text(book.get("title", ""))
        title = re.sub(r'\s+', ' ', title).strip()
        if not title:
            return str(book.get("oclcNumber", ""))
        return title[:180]


    @staticmethod
    def _rank_book_candidates(
            books_data: List[Dict[str, Any]],
            query_info: Dict[str, Any],
            max_candidates: int = 25,
    ) -> List[Tuple[int, Dict[str, Any], float]]:
        """Rank real OCLC records before Gemini sees them; the score remains internal."""
        ranked = []
        for index, book in enumerate(books_data):
            match_score = book.get("_match_score")
            if match_score is None:
                match_score = AIAnalysisService._score_book_for_query(book, query_info)
                book["_match_score"] = match_score
            try:
                publication_year = int(str(book.get("date", "0"))[:4])
            except (ValueError, TypeError):
                publication_year = 0
            freshness = ArticleSearchService._year_freshness_score(book.get("date", ""))
            ranked.append((index, book, float(match_score), freshness, publication_year))

        ranked.sort(key=lambda row: (row[2], row[3], row[4], -row[0]), reverse=True)
        unique_ranked = []
        seen_titles = set()
        for row in ranked:
            key = AIAnalysisService._book_duplicate_key(row[1])
            if key in seen_titles:
                continue
            seen_titles.add(key)
            unique_ranked.append(row)
            if len(unique_ranked) >= max_candidates:
                break
        return [(index, book, score) for index, book, score, _, _ in unique_ranked]

    @staticmethod
    def _finalize_book_analysis_selection(
            analysis: Dict[str, Any], books_data: List[Dict[str, Any]],
            query_info: Dict[str, Any], limit: int,
            core_topic: str, is_arabic: bool,
    ) -> Dict[str, Any]:
        """Keep Gemini explanations, but make final choices follow real catalog ranking."""
        ranked = AIAnalysisService._rank_book_candidates(books_data, query_info, max(25, limit))
        ranked_indices = [index for index, _, _ in ranked]
        existing_recs = {
            rec.get("index"): rec
            for rec in analysis.get("recommendations", [])
            if isinstance(rec.get("index"), int)
        }

        final_indices = []
        for index in analysis.get("selected_indices", []):
            if isinstance(index, int) and index in ranked_indices and index not in final_indices:
                final_indices.append(index)
        for index in ranked_indices:
            if len(final_indices) >= limit:
                break
            if index not in final_indices:
                final_indices.append(index)

        final_recommendations = []
        for index in final_indices[:limit]:
            book = books_data[index]
            rec = dict(existing_recs.get(index, {}))
            rec["index"] = index
            if not rec.get("why_recommended"):
                rec["why_recommended"] = AIAnalysisService._generate_description_from_title(
                    title=book.get("title", ""),
                    author=book.get("creator", ""),
                    year=book.get("date", "")[:4],
                    extra=book.get("specificFormat") or book.get("generalFormat") or "",
                    core_topic=core_topic,
                    is_arabic=is_arabic,
                )
            if not rec.get("relevance_score"):
                rec["relevance_score"] = round(
                    min(0.98, max(0.55, float(book.get("_match_score", 0)) / 24)), 2
                )
            final_recommendations.append(rec)

        analysis["selected_indices"] = final_indices[:limit]
        analysis["recommendations"] = final_recommendations
        return analysis


    @staticmethod
    async def analyze_and_rank_books(
            user_query: str, books_data: List[Dict[str, Any]],
            query_info: Dict[str, Any], limit: int = FINAL_RECOMMENDATIONS,
            conversation_history: List[Dict] = None
    ) -> Dict[str, Any]:
        if not GEMINI_MODEL:
            return AIAnalysisService._fallback_ranking(books_data, query_info, limit)
        if not books_data:
            return {"selected_indices": [], "recommendations": [],
                    "summary": "No books found.", "follow_up_suggestions": []}

        is_arabic = query_info.get("is_arabic", False)
        language = "Arabic" if is_arabic else "English"
        author_name = query_info.get("author_name")
        is_followup = query_info.get("is_followup", False)
        core_topic = query_info.get("core_topic", "")
        wants_new = query_info.get("wants_new_books", False)
        year_from = query_info.get("year_from")
        format_pref = query_info.get("format_preference")

        sanitized_query = AIAnalysisService._sanitize_for_prompt(user_query)
        sanitized_author = AIAnalysisService._sanitize_for_prompt(author_name) if author_name else None
        sanitized_topic = AIAnalysisService._sanitize_for_prompt(core_topic)

        ranked_candidates = AIAnalysisService._rank_book_candidates(books_data, query_info, 25)
        books_for_analysis = []
        for original_index, book, match_score in ranked_candidates:
            books_for_analysis.append({
                "index": original_index,
                "title": safe_llm_field(book.get("title", "Unknown"), 150),
                "author": safe_llm_field(book.get("creator", "Unknown"), 80),
                "year": safe_llm_field(book.get("date", "N/A"), 10),
                "format": safe_llm_field(book.get("specificFormat") or book.get("generalFormat") or "Unknown", 40),
                "match_score": match_score,
            })


        history_context = ""
        if conversation_history:
            recent = conversation_history[-3:]
            sanitized_history = [AIAnalysisService._sanitize_for_prompt(h['query']) for h in recent]
            history_context = f"\nPREVIOUS SEARCHES: {sanitized_history}"

        preferences = []
        if wants_new and year_from:
            if is_arabic:
                preferences.append(f"يريد كتب جديدة من سنة {year_from} وما بعد")
            else:
                preferences.append(f"Wants NEW/RECENT books from {year_from} onwards")
        if format_pref:
            format_labels = {'ebook': 'eBooks/Digital', 'print': 'Print books', 'article': 'Articles',
                             'audiobook': 'Audiobooks'}
            preferences.append(f"Prefers: {format_labels.get(format_pref, format_pref)}")

        pref_text = "\nUSER PREFERENCES: " + ", ".join(preferences) if preferences else ""

        prompt = f"""You are an expert librarian helping find the perfect books.

USER'S SEARCH: "{sanitized_query}"
CORE TOPIC: "{sanitized_topic}"
RESPONSE LANGUAGE: {language}
{f"LOOKING FOR AUTHOR: {sanitized_author}" if sanitized_author else ""}
{f"THIS IS A FOLLOW-UP QUERY" if is_followup else ""}
{pref_text}
{history_context}

AVAILABLE BOOKS:
{json.dumps(books_for_analysis, ensure_ascii=False, indent=2)}

TASK:
1. Understand what the user REALLY wants to learn or find
2. Select the {limit} BEST books that match their ACTUAL need
3. For EACH book, write a specific 1-2 sentence explanation IN {language} about what the book covers
4. Suggest 2-3 follow-up searches IN {language}

CRITICAL RULES:
- ALL text in "why_recommended", "summary", and "follow_up_suggestions" MUST be in {language}
- Prefer books with higher match_score when title, subject, author, freshness, and semantic signals match the user's need
- For AUTHOR searches: ONLY select books written BY that author (not books ABOUT them)
- If user wants NEW/RECENT books: PRIORITIZE books from {year_from if year_from else 'recent years'} onwards
- If user has a format preference: PRIORITIZE that format
- Focus on the CORE TOPIC: "{sanitized_topic}" - ignore filler words

DESCRIPTION RULES (VERY IMPORTANT):
- The "why_recommended" MUST describe the book's CONTENT, not just who wrote it
- READ THE TITLE CAREFULLY — it tells you what the book is about
- Explain the topic coverage, method, level, case study, or practical value when the title gives enough clues
- NEVER write generic phrases like "Written by X" or "A book by X" or "Published in Y"
- NEVER just restate the author name and year — that info is already shown separately

❌ BAD descriptions (NEVER write these):
- "Written by Qiushi Yang (2023)."
- "A book by Smith about the topic."
- "Published by Springer in 2022."
- "Relevant to the user's search."
- "كتاب للمؤلف أحمد (2023)."

✅ GOOD descriptions (write like these):
- "Covers deep learning architectures for medical image segmentation, including U-Net and transformer-based models."
- "Practical guide to building REST APIs with Python Flask, with chapters on authentication and deployment."
- "Explores the economic impact of renewable energy policies in Gulf states with case studies from the UAE."
- "يتناول تقنيات التعلم العميق لتحليل الصور الطبية، مع تطبيقات عملية باستخدام بايثون."

RESPOND IN JSON:
{{
    "user_intent": "What user wants in {language}",
    "selected_indices": [0, 3, 5],
    "recommendations": [
        {{"index": 0, "relevance_score": 0.95, "why_recommended": "Specific description in {language}"}}
    ],
    "summary": "Brief message in {language}",
    "follow_up_suggestions": ["suggestion in {language}"]
}}

Return ONLY valid JSON."""

        try:
            result = await generate_gemini_content(prompt, temperature=0.3, max_output_tokens=1500)
            if not result or not result.text:
                return AIAnalysisService._fallback_ranking(books_data, query_info, limit)
            analysis = parse_llm_json(result.text)
            if not isinstance(analysis, dict):
                logger.warning("AI returned non-object JSON for books; using fallback ranking")
                return AIAnalysisService._fallback_ranking(books_data, query_info, limit)
            # ★ Post-process: replace any generic descriptions AI might return
            analysis = AIAnalysisService._sanitize_ai_descriptions(
                analysis, books_data, sanitized_topic, is_arabic)
            analysis = AIAnalysisService._finalize_book_analysis_selection(
                analysis, books_data, query_info, limit, sanitized_topic, is_arabic)
            logger.info(f"AI selected {len(analysis.get('selected_indices', []))} books")
            return analysis
        except json.JSONDecodeError as e:
            logger.warning(f"AI returned invalid JSON for books; using fallback ranking: {e}")
            return AIAnalysisService._fallback_ranking(books_data, query_info, limit)
        except Exception as e:
            logger.error(f"AI analysis error: {e}")
            return AIAnalysisService._fallback_ranking(books_data, query_info, limit)

    @staticmethod
    def _fallback_ranking(books_data: List[Dict], query_info: Dict, limit: int) -> Dict:
        original_query = query_info.get("original_query", "")
        is_arabic = query_info.get("is_arabic", False)
        wants_new = query_info.get("wants_new_books", False)
        core_topic = query_info.get("core_topic", original_query)

        ranked_books = AIAnalysisService._rank_book_candidates(books_data, query_info, max(25, limit))

        selected = []
        for original_idx, book, match_score in ranked_books[:limit]:
            title = book.get("title", "")
            author = book.get("creator", "Unknown")
            year = book.get("date", "")[:4]
            fmt = book.get("specificFormat") or book.get("generalFormat") or ""

            description = AIAnalysisService._generate_description_from_title(
                title, author, year, fmt, core_topic, is_arabic
            )

            selected.append({
                "index": original_idx,
                "relevance_score": round(min(0.98, max(0.55, float(match_score) / 24)), 2),
                "why_recommended": description
            })


        if is_arabic:
            summary = f"تم العثور على {len(books_data)} كتب."
            suggestions = ["جرب كلمات بحث أكثر تحديداً", "ابحث باسم المؤلف", "المزيد من الكتب"]
        else:
            summary = f"Found {len(books_data)} books."
            suggestions = ["Try more specific keywords", "Search by author name", "Give me more books"]

        return {
            "user_intent": original_query,
            "selected_indices": [s["index"] for s in selected],
            "recommendations": selected,
            "summary": summary,
            "follow_up_suggestions": suggestions
        }

    @staticmethod
    async def analyze_and_rank_articles(
            user_query: str,
            raw_articles: List[Dict],
            query_info: Dict[str, Any],
            limit: int = 8,
    ) -> Dict[str, Any]:
        """
        AI ranks and curates articles from all APIs.
        Picks the best ones and writes a why_recommended for each.
        Falls back to a simple sort when Gemini is unavailable.
        """
        if not raw_articles:
            return {"selected": [], "summary": ""}

        is_arabic = query_info.get("is_arabic", False)
        language = "Arabic" if is_arabic else "English"
        core_topic = query_info.get("core_topic", user_query)
        year_from = query_info.get("year_from")

        # ── Prepare compact article list for the prompt ──
        articles_for_ai = []
        for i, art in enumerate(raw_articles[:30]):
            articles_for_ai.append({
                "i": i,
                "title": safe_llm_field(art.get("title"), 150),
                "authors": safe_llm_field(art.get("authors"), 80),
                "year": safe_llm_field(art.get("year"), 10),
                "journal": safe_llm_field(art.get("journal"), 80),
                "source": safe_llm_field(art.get("source"), 40),
                "oa": art.get("open_access", False),
                "match_score": art.get("match_score", 0),
                "embedding_similarity": art.get("embedding_similarity", 0),
                "subjects": [
                    safe_llm_field(subject, 60)
                    for subject in ArticleSearchService._as_text_list(art.get("subjects", []))[:5]
                ],
                "abstract_hint": safe_llm_field(art.get("abstract"), 220),
                "relevance_signals": art.get("relevance_details", {}),
            })

        if not GEMINI_MODEL:
            return AIAnalysisService._fallback_article_ranking(
                raw_articles, query_info, limit)

        sanitized_query = AIAnalysisService._sanitize_for_prompt(user_query)
        sanitized_topic = AIAnalysisService._sanitize_for_prompt(core_topic)

        prompt = f"""You are an expert research librarian selecting the best scholarly articles.

USER'S RESEARCH NEED: "{sanitized_query}"
CORE TOPIC: "{sanitized_topic}"
RESPONSE LANGUAGE: {language}
{"PREFERS RECENT ARTICLES from " + str(year_from) + " onwards" if year_from else ""}

CANDIDATE ARTICLES (from multiple databases):
{json.dumps(articles_for_ai, ensure_ascii=False, indent=1)}

TASK:
1. Understand the user's actual research need
2. Select the {limit} BEST articles that genuinely help the user
3. For EACH selected article, write a 1-sentence explanation IN {language} about what value it provides
4. Prioritise: relevance first, then recency, then open-access availability

SELECTION RULES:
- Pick articles that DIRECTLY address the topic — skip loosely related ones
- Prefer candidates with higher match_score when title, subject, exact phrase, or semantic signals align
- Use embedding_similarity and relevance_signals to catch semantic matches such as "AI in healthcare" matching "clinical decision support"
- Prefer peer-reviewed journals over unknown sources
- Prefer newer articles unless a classic is essential
- Prefer open-access (oa=true) when relevance is equal
- NEVER select articles with missing or vague titles
- ALL "why" text MUST be in {language}

DESCRIPTION RULES (VERY IMPORTANT):
- The "why" MUST describe what the article COVERS or CONTRIBUTES
- READ THE TITLE — it tells you the article's content
- Use abstract_hint and subjects when available; otherwise infer carefully from the title
- Mention the method, population/domain, case study, or contribution when the metadata provides it
- NEVER write generic phrases like "Published in Journal X" or "An article about the topic"
- NEVER just restate the author/journal/year — that info is already shown separately

❌ BAD (NEVER write):
- "Published in Medical Imaging 2024: Image Processing (2024)."
- "An article about machine learning."
- "Relevant to the user's query."
- "مقالة منشورة في مجلة (2024)."

✅ GOOD (write like these):
- "Proposes a novel transformer architecture that improves CT scan tumour detection accuracy by 12%."
- "Compares five encryption algorithms for IoT devices, benchmarking speed vs. security trade-offs."
- "يقترح خوارزمية جديدة لكشف الاحتيال المالي باستخدام الشبكات العصبية العميقة."

RESPOND IN JSON ONLY:
{{
    "selected_indices": [0, 3, 7],
    "recommendations": [
        {{"index": 0, "relevance": 0.95, "why": "Explanation in {language}"}},
        {{"index": 3, "relevance": 0.90, "why": "Explanation in {language}"}}
    ],
    "summary": "One-line overview in {language}"
}}

Return ONLY valid JSON."""

        try:
            result = await generate_gemini_content(prompt, temperature=0.2, max_output_tokens=1200)
            if not result or not result.text:
                return AIAnalysisService._fallback_article_ranking(
                    raw_articles, query_info, limit)

            analysis = parse_llm_json(result.text)
            if not isinstance(analysis, dict):
                logger.warning("AI returned non-object JSON for articles; using fallback ranking")
                return AIAnalysisService._fallback_article_ranking(raw_articles, query_info, limit)

            # Build curated list
            recs = {
                r.get("index"): r
                for r in analysis.get("recommendations", [])
                if isinstance(r.get("index"), int)
            }
            selected = []
            for idx in analysis.get("selected_indices", []):
                if isinstance(idx, int) and 0 <= idx < len(raw_articles):
                    art = raw_articles[idx].copy()
                    rec = recs.get(idx, {})
                    art["why_recommended"] = rec.get("why", "")
                    art["relevance_score"] = rec.get("relevance", 0.8)
                    selected.append(art)

            # ★ Post-process: replace any generic descriptions
            selected = AIAnalysisService._sanitize_ai_article_descriptions(
                selected, sanitized_topic, is_arabic)

            logger.info(f"AI selected {len(selected)} articles from {len(raw_articles)} candidates")
            return {
                "selected": selected,
                "summary": analysis.get("summary", ""),
            }

        except json.JSONDecodeError as e:
            logger.warning(f"AI returned invalid JSON for articles; using fallback ranking: {e}")
            return AIAnalysisService._fallback_article_ranking(
                raw_articles, query_info, limit)
        except Exception as e:
            logger.error(f"Article AI error: {e}")
            return AIAnalysisService._fallback_article_ranking(
                raw_articles, query_info, limit)

    @staticmethod
    def _fallback_article_ranking(
            raw_articles: List[Dict], query_info: Dict, limit: int
    ) -> Dict[str, Any]:
        """Fallback: sort by year+OA, generate descriptions from titles."""
        is_arabic = query_info.get("is_arabic", False)
        core_topic = query_info.get("core_topic", "")

        def score(art):
            yr = 0
            try:
                yr = int(art.get("year", "0"))
            except ValueError:
                pass
            oa = 1 if art.get("open_access") else 0
            return (float(art.get("match_score", 0)), yr, oa)

        sorted_arts = sorted(raw_articles, key=score, reverse=True)
        selected = []
        for art in sorted_arts[:limit]:
            a = art.copy()
            title = art.get("title", "")
            author = art.get("authors", "")
            year = art.get("year", "")
            a["why_recommended"] = AIAnalysisService._generate_article_description_from_metadata(
                art, core_topic, is_arabic
            )
            selected.append(a)

        summary = (f"تم العثور على {len(raw_articles)} مقالة." if is_arabic
                   else f"Found {len(raw_articles)} articles.")
        return {"selected": selected, "summary": summary}

# ============================
# 10. RESPONSE GENERATOR SERVICE
# ============================
class ResponseGeneratorService:
    @staticmethod
    def _database_search_link(article: Dict) -> str:
        search_base = article.get("database_search") or article.get("database_url") or ""
        title = article.get("title") or ""
        if not search_base:
            return ""
        if title and not search_base.endswith(("/", "form.uri", "Welcome", "basic-search", "Search/Advanced", "Search/Results", "apps/menu")):
            return search_base + url_quote(title)
        return search_base

    @staticmethod
    def _article_database_suggestion_lines(article: Dict, is_arabic: bool) -> List[str]:
        db_name = article.get("database", "")
        direct = article.get("direct_link", "")
        source = article.get("source", "")
        match_type = article.get("database_match_type", "suggested")
        trusted_direct_sources = {"PubMed", "Europe PMC", "DOAJ", "CORE"}

        if direct and "doi.org/" not in direct and (
            match_type == "publisher" or source in trusted_direct_sources
        ):
            label = "افتح في" if is_arabic else "Open in"
            return [f"   {label} {db_name}: {direct}"]

        suggestions = article.get("suggested_databases") or []
        if not suggestions and db_name:
            suggestions = [{
                "db": db_name,
                "url": article.get("database_url", ""),
                "search": article.get("database_search", ""),
            }]

        suggestions = ArticleSearchService._dedupe_db_entries(suggestions)[:3]
        if not suggestions:
            return []

        names = ", ".join(db.get("db", "") for db in suggestions if db.get("db"))
        if is_arabic:
            lines = [f"    قواعد بيانات جامعة الإمارات المقترحة للبحث: {names}"]
            first_label = "ابحث في"
        else:
            lines = [f"    Suggested UAEU databases to search: {names}"]
            first_label = "Search in"

        first = suggestions[0]
        search_link = ResponseGeneratorService._database_search_link({
            "title": article.get("title", ""),
            "database_search": first.get("search", ""),
            "database_url": first.get("url", ""),
        })
        if search_link:
            lines.append(f"    {first_label} {first.get('db', 'database')}: {search_link}")
        return lines

    @staticmethod
    async def generate_research_response(
            user_query: str, selected_books: List[Book],
            analysis: Dict[str, Any], query_info: Dict[str, Any],
            api_articles: List[Dict] = None,
            suggested_databases: List[Dict] = None,
            search_mode: str = "all"
    ) -> str:
        is_arabic = query_info.get("is_arabic", False)
        core_topic = query_info.get("core_topic", user_query)
        year_from = query_info.get("year_from")
        response_parts = []

        # INTRO
        if search_mode == "books":
            if is_arabic:
                intro = f" إليك الكتب المتوفرة حول \"{core_topic}\":"
            else:
                intro = f" Here are books available for \"{core_topic}\":"
        elif search_mode == "research":
            if is_arabic:
                intro = f"📄 إليك المقالات والدوريات العلمية حول \"{core_topic}\":"
            else:
                intro = f"📄 Here are scholarly articles and journals for \"{core_topic}\":"
        else:
            if is_arabic:
                intro = f"إليك نتائج البحث العلمي حول \"{core_topic}\":"
            else:
                intro = f"Here are research results for \"{core_topic}\":"

        if year_from:
            if is_arabic:
                intro += f" (من سنة {year_from} فأحدث)"
            else:
                intro += f" (from {year_from} onwards)"
        response_parts.append(intro)

        # ARTICLES SECTION — AI-curated (api_articles already ranked by AI)
        if search_mode in ["research", "all"] and api_articles:
            separator = "\n\n" + "=" * 60
            if is_arabic:
                response_parts.append(separator)
                response_parts.append("📄 مقالات علمية مُختارة لك:")
                response_parts.append("=" * 60)
                for i, article in enumerate(api_articles, 1):
                    response_parts.append(f"\n{i}. {article.get('title', 'Unknown')}")
                    response_parts.append(f"   المؤلفون: {article.get('authors', 'Unknown')}")
                    if article.get('year'):
                        response_parts.append(f"   السنة: {article.get('year')}")
                    if article.get('journal'):
                        response_parts.append(f"   المجلة: {article.get('journal')}")
                    if article.get('why_recommended'):
                        response_parts.append(f"   لماذا هذه المقالة: {article.get('why_recommended')}")
                    if article.get('open_access'):
                        response_parts.append(f"   ✅ متاح مجاناً (Open Access)")
                    if article.get('link'):
                        response_parts.append(f"   🔗 رابط المقال: {article.get('link')}")
                    response_parts.extend(
                        ResponseGeneratorService._article_database_suggestion_lines(article, True)
                    )
            else:
                response_parts.append(separator)
                response_parts.append("📄 SCHOLARLY ARTICLES (selected for you):")
                response_parts.append("=" * 60)
                for i, article in enumerate(api_articles, 1):
                    response_parts.append(f"\n{i}. {article.get('title', 'Unknown')}")
                    response_parts.append(f"   Authors: {article.get('authors', 'Unknown')}")
                    if article.get('year'):
                        response_parts.append(f"   Year: {article.get('year')}")
                    if article.get('journal'):
                        response_parts.append(f"   Journal: {article.get('journal')}")
                    if article.get('why_recommended'):
                        response_parts.append(f"    Why This Article: {article.get('why_recommended')}")
                    if article.get('open_access'):
                        response_parts.append(f"   ✅ Open Access (Free)")
                    if article.get('link'):
                        response_parts.append(f"   🔗 Article Link: {article.get('link')}")
                    response_parts.extend(
                        ResponseGeneratorService._article_database_suggestion_lines(article, False)
                    )

        # DATABASE RECOMMENDATIONS
        if suggested_databases:
            separator = "\n\n" + "=" * 60
            if is_arabic:
                response_parts.append(separator)
                response_parts.append("📚 أفضل قواعد بيانات جامعة الإمارات لهذا الموضوع:")
                response_parts.append("=" * 60)
                response_parts.append("هذه اقتراحات للبحث المتقدم داخل قواعد بيانات الجامعة، وليست فحصاً مباشراً للتوفر.")
                response_parts.append("")
                response_parts.append("| قاعدة البيانات | رابط البحث |")
                response_parts.append("|---|---|")
                for db in suggested_databases[:5]:
                    search_link = ResponseGeneratorService._database_search_link({
                        "title": core_topic,
                        "database_search": db.get("search", ""),
                        "database_url": db.get("url", ""),
                    })
                    response_parts.append(f"| {db.get('db', 'Unknown')} | {search_link or db.get('url', '')} |")
            else:
                response_parts.append(separator)
                response_parts.append("📚 Best UAEU databases for this topic:")
                response_parts.append("=" * 60)
                response_parts.append("These are recommended places to continue searching inside UAEU databases; they are not live database API checks.")
                response_parts.append("")
                response_parts.append("| Database | Search link |")
                response_parts.append("|---|---|")
                for db in suggested_databases[:5]:
                    search_link = ResponseGeneratorService._database_search_link({
                        "title": core_topic,
                        "database_search": db.get("search", ""),
                        "database_url": db.get("url", ""),
                    })
                    response_parts.append(f"| {db.get('db', 'Unknown')} | {search_link or db.get('url', '')} |")

        # BOOKS SECTION
        if search_mode in ["books", "all", "research"] and selected_books:
            separator = "\n\n" + "=" * 60
            if search_mode == "research":
                label_en = "📖 RELATED RESOURCES FROM LIBRARY (Books, Journals, References):"
                label_ar = "📖 موارد ذات صلة من كتالوج المكتبة (كتب، دوريات، مراجع):"
            else:
                label_en = "📖 BOOKS FROM LIBRARY CATALOG:"
                label_ar = "📖 كتب من كتالوج المكتبة:"

            response_parts.append(separator)
            response_parts.append(label_ar if is_arabic else label_en)
            response_parts.append("=" * 60)

            for i, book in enumerate(selected_books, 1):
                if is_arabic:
                    response_parts.append(f"\n{i}. العنوان: {book.title}")
                    response_parts.append(f"   المؤلف: {book.author}")
                    response_parts.append(f"   سنة النشر: {book.year}")
                    response_parts.append(f"   الصيغة: {book.format}")

                    # This is public catalog metadata, not a live patron or circulation check.
                    if book.availability_status == "Online":
                        response_parts.append("   طريقة الوصول: مورد إلكتروني")
                    elif book.availability_status:
                        response_parts.append(f"   حالة الفهرس: {book.availability_status}")
                    if book.branch_location:
                        response_parts.append(f"   الموقع/الفرع: {book.branch_location}")
                    if book.shelf_location:
                        response_parts.append(f"   الرف/المجموعة: {book.shelf_location}")
                    if book.why_recommended:
                        response_parts.append(f"   لماذا هذا الكتاب: {book.why_recommended}")
                    if book.call_number:
                        response_parts.append(f"   رقم التصنيف: {book.call_number}")
                    response_parts.append(f"   🔗 الرابط: {book.link}")
                else:
                    response_parts.append(f"\n{i}. Title: {book.title}")
                    response_parts.append(f"   Author: {book.author}")
                    response_parts.append(f"   Year: {book.year}")
                    response_parts.append(f"   Format: {book.format}")


                    # This is public catalog metadata, not a live patron or circulation check.
                    if book.availability_status == "Online":
                        response_parts.append("   Access format: Online resource")
                    elif book.availability_status:
                        response_parts.append(f"   Catalog status: {book.availability_status}")
                    if book.branch_location:
                        response_parts.append(f"   Location: {book.branch_location}")
                    if book.shelf_location:
                        response_parts.append(f"   Shelf/Collection: {book.shelf_location}")
                    if book.why_recommended:
                        response_parts.append(f"   Why This Book: {book.why_recommended}")
                    if book.call_number:
                        response_parts.append(f"   Call Number: {book.call_number}")
                    response_parts.append(f"   🔗 Link: {book.link}")

        # ACCESS INSTRUCTIONS
        if search_mode == "research" or (search_mode == "all" and api_articles):
            response_parts.append("\n\n" + "-" * 60)
            if is_arabic:
                response_parts.append("🔐 كيفية الوصول للمقالات:")
                response_parts.append("-" * 60)
                response_parts.append("للوصول إلى المقالات من قواعد البيانات:")
                response_parts.append("1. اضغط على رابط قاعدة البيانات")
                response_parts.append("2. اختر 'Login with Institution' أو 'تسجيل الدخول المؤسسي'")
                response_parts.append("3. ابحث عن 'United Arab Emirates University'")
                response_parts.append("4. سجل دخولك ببيانات جامعة الإمارات")
                response_parts.append("\nأو قم بزيارة صفحة قواعد البيانات مباشرة:")
                response_parts.append("https://www.uaeu.ac.ae/en/library/databases.shtml")
            else:
                response_parts.append("🔐 HOW TO ACCESS ARTICLES:")
                response_parts.append("-" * 60)
                response_parts.append("To access articles from the databases:")
                response_parts.append("1. Click on the database search link")
                response_parts.append("2. Select 'Login with Institution' or 'Institutional Login'")
                response_parts.append("3. Search for 'United Arab Emirates University'")
                response_parts.append("4. Login with your UAEU credentials")
                response_parts.append("\nOr visit the databases page directly:")
                response_parts.append("https://www.uaeu.ac.ae/en/library/databases.shtml")

        return "\n".join(response_parts)

    @staticmethod
    async def _generate_no_results_response(query_info: Dict, search_mode: str = "all") -> str:
        is_arabic = query_info.get("is_arabic", False)
        original = query_info.get("original_query", "")
        core_topic = query_info.get("core_topic", original)
        response_parts = []

        if search_mode == "books":
            if is_arabic:
                response_parts.append(f" لم أجد كتباً في كتالوج المكتبة لـ \"{original}\".")
                response_parts.append("\n اقتراحات:")
                response_parts.append("• جرب كلمات بحث مختلفة")
                response_parts.append("• تحقق من التهجئة")
                response_parts.append("• استخدم مصطلحات أكثر عمومية")
                response_parts.append(f"\n🔗 أو ابحث مباشرة في كتالوج المكتبة:")
                response_parts.append(f"https://uaeu.on.worldcat.org/search?queryString={core_topic.replace(' ', '+')}")
                response_parts.append('\nإذا لم يكن المصدر متوفراً، يمكنك إرسال طلب شراء/توفير مصدر:')
                response_parts.append('https://www.uaeu.ac.ae/ar/library/forms/requesttitle.shtml')
            else:
                response_parts.append(f" I couldn't find books in the library catalog for \"{original}\".")
                response_parts.append("\n Suggestions:")
                response_parts.append("• Try different search terms")
                response_parts.append("• Check your spelling")
                response_parts.append("• Use more general terms")
                response_parts.append(f"\n🔗 Or search the library catalog directly:")
                response_parts.append(f"https://uaeu.on.worldcat.org/search?queryString={core_topic.replace(' ', '+')}")
                response_parts.append('\nIf the title is not available, submit a Resource Purchase Request:')
                response_parts.append('https://www.uaeu.ac.ae/en/library/forms/requesttitle.shtml')
        elif search_mode == "research":
            if is_arabic:
                response_parts.append(f"📄 لم أجد مقالات علمية لـ \"{original}\".")
                response_parts.append("\n اقتراحات:")
                response_parts.append("• جرب كلمات بحث مختلفة")
                response_parts.append("• استخدم مصطلحات أكاديمية")
                response_parts.append("• جرب البحث بالإنجليزية")
                response_parts.append(f"\n🔗 أو ابحث مباشرة في قواعد البيانات:")
                response_parts.append("https://www.uaeu.ac.ae/en/library/databases.shtml")
            else:
                response_parts.append(f"📄 I couldn't find scholarly articles for \"{original}\".")
                response_parts.append("\n Suggestions:")
                response_parts.append("• Try different search terms")
                response_parts.append("• Use academic terminology")
                response_parts.append("• Try broader topics")
                response_parts.append(f"\n🔗 Or search the databases directly:")
                response_parts.append("https://www.uaeu.ac.ae/en/library/databases.shtml")
        else:
            if is_arabic:
                response_parts.append(f"لم أجد نتائج لـ \"{original}\".")
                response_parts.append("\n اقتراحات:")
                response_parts.append("• جرب كلمات بحث مختلفة")
                response_parts.append("• تحقق من التهجئة")
                response_parts.append(f"\n🔗 روابط مفيدة:")
                response_parts.append(f"• كتالوج المكتبة: https://uaeu.on.worldcat.org/")
                response_parts.append("• قواعد البيانات: https://www.uaeu.ac.ae/en/library/databases.shtml")
            else:
                response_parts.append(f"I couldn't find results for \"{original}\".")
                response_parts.append("\n Suggestions:")
                response_parts.append("• Try different search terms")
                response_parts.append("• Check your spelling")
                response_parts.append(f"\n🔗 Useful links:")
                response_parts.append(f"• Library Catalog: https://uaeu.on.worldcat.org/")
                response_parts.append("• Databases: https://www.uaeu.ac.ae/en/library/databases.shtml")

        return "\n".join(response_parts)




# ============================
# 11. LIBRARY POLICY ANSWERS
# ============================
class LibraryPolicyAnswerService:
    # Official UAEU Library service sources. These links are used for chatbot
    # service answers so the assistant does not invent forms, contacts, or policy pages.
    SERVICE_SOURCE_LINKS = {
        "lending": {
            "en": "https://www.uaeu.ac.ae/en/library/pdf/2_access_lending-en.pdf",
            "ar": "https://www.uaeu.ac.ae/ar/library/pdf/2_access_lending-ar.pdf",
        },
        "borrowing": {
            "en": "https://www.uaeu.ac.ae/en/library/borrowing.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/borrowing.shtml",
        },
        "comments": {
            "en": "https://www.uaeu.ac.ae/en/library/forms/commentsandsuggestions.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/forms/commentsandsuggestions.shtml",
        },
        "course_reserve": {
            "en": "https://www.uaeu.ac.ae/en/library/forms/coursereserve.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/forms/coursereserve.shtml",
        },
        "purchase_request": {
            "en": "https://www.uaeu.ac.ae/en/library/forms/requesttitle.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/forms/requesttitle.shtml",
        },
        "pod_services": {
            "en": "https://www.uaeu.ac.ae/en/library/forms/pod_services.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/forms/pod_services.shtml",
        },
        "staff_contacts": {
            "en": "https://www.uaeu.ac.ae/en/library/staffdirstaic.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/staffdirstaic.shtml",
        },
        "faq": {
            "en": "https://www.uaeu.ac.ae/en/library/faqs.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/faqs.shtml",
        },
        "hours_location": {
            "en": "https://www.uaeu.ac.ae/en/library/libraryhours.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/libraryhours.shtml",
        },
        "collection": {
            "en": "https://www.uaeu.ac.ae/en/library/pdf/1_collection_management-en.pdf",
            "ar": "https://www.uaeu.ac.ae/ar/library/pdf/1_collection_management-ar.pdf",
        },
        "borrowing_history": {
            "en": "https://uaeu.account.worldcat.org/account/borrowing-history",
            "ar": "https://uaeu.account.worldcat.org/account/borrowing-history",
        },
        "account_fees": {
            "en": "https://uaeu.account.worldcat.org/account/fees",
            "ar": "https://uaeu.account.worldcat.org/account/fees",
        },
        "library_account": {
            "en": "https://uaeu.account.worldcat.org/account/checkouts?continue",
            "ar": "https://uaeu.account.worldcat.org/account/checkouts?continue",
        },
        "catalog_search": {
            "en": "https://uaeu.on.worldcat.org/search",
            "ar": "https://uaeu.on.worldcat.org/search",
        },
        "new_arrivals": {
            "en": "https://search.worldcat.org/libraries/65572?registryId=65572&q=United+Arab+Emirates+University",
            "ar": "https://search.worldcat.org/libraries/65572?registryId=65572&q=United+Arab+Emirates+University",
        },
        "database_list": {
            "en": "https://www.uaeu.ac.ae/en/library/databases.shtml",
            "ar": "https://www.uaeu.ac.ae/ar/library/databases.shtml",
        },
    }

    SERVICE_PAGE_CACHE_TTL_SECONDS = 6 * 60 * 60
    _service_page_cache: Dict[str, Any] = {}
    LIBCHAT_PAGE_URL_EN = "https://www.uaeu.ac.ae/en/library/ask-us.shtml"
    LIBCHAT_PAGE_URL_AR = "https://www.uaeu.ac.ae/ar/library/ask-us.shtml"

    SERVICE_TOPIC_RULES = {
        "human_help": {
            "en": (
                "talk to a real person", "talk to real person", "real person",
                "talk to a person", "talk to person", "talk to human", "speak to human",
                "talk to someone", "speak to someone", "talk with someone",
                "talk with staff", "speak to staff", "staff member", "support agent",
                "support person", "library staff", "representative",
                "human help", "human support", "live chat", "libchat",
                "ask a librarian", "chat with librarian", "chat with a librarian",
                "speak to librarian", "speak to a librarian", "contact librarian",
                "contact a librarian", "need a person", "need human",
            ),
            "ar": (
                "التحدث مع شخص", "تحدث مع شخص", "أتحدث مع شخص", "اكلم شخص", "أكلم شخص",
                "موظف حقيقي", "شخص حقيقي", "دردشة مباشرة", "دردشة المكتبة",
                "أمين مكتبة", "تواصل مع أمين", "تواصل مع موظف", "مساعدة بشرية",
            ),
        },
        "borrowing_history": {
            "en": (
                "borrowing history", "loan history", "checkout history",
                "old borrowed books", "past borrowed books", "previously borrowed books",
                "books i borrowed before", "what i borrowed before",
            ),
            "ar": (
                "سجل الاستعارة", "تاريخ الاستعارة", "الكتب التي استعرتها سابقا",
                "الكتب التي استعرتها سابقاً", "استعاراتي السابقة",
            ),
        },
        "account_fees": {
            "en": (
                "my fines", "check my fines", "fines on my account",
                "fees on my account", "my library fees", "account fees",
            ),
            "ar": (
                "غراماتي", "الغرامات على حسابي", "رسوم حسابي",
                "غرامات حسابي", "اريد معرفة غراماتي", "أريد معرفة غراماتي",
            ),
        },
        "library_account": {
            "en": (
                "my account", "library account", "worldcat account", "my worldcat",
                "my checkouts", "checkouts", "checked out books", "borrowed books",
                "books i borrowed", "my borrowed books", "my books", "my loans",
                "search history",
                "renew my books", "renew books online", "renew items",
            ),
            "ar": (
                "حسابي", "حساب المكتبة", "حساب وورلدكات", "إعاراتي", "كتبي المستعارة",
                "الكتب التي استعرتها", "المواد المستعارة",
                "سجل البحث", "تجديد كتبي", "تجديد الإعارة",
            ),
        },
        "catalog_search": {
            "en": (
                "search catalog", "catalog search", "library catalog", "worldcat search",
                "open catalog", "catalog link", "search worldcat", "search the library",
            ),
            "ar": (
                "البحث في الفهرس", "فهرس المكتبة", "رابط الفهرس", "افتح الفهرس",
                "بحث وورلدكات", "البحث في المكتبة",
            ),
        },
        "new_arrivals": {
            "en": (
                "new arrivals", "recent arrivals", "latest arrivals",
                "newly added", "recently added books", "new items",
            ),
            "ar": (
                "وصل حديثا", "وصل حديثاً", "الإضافات الجديدة", "المقتنيات الجديدة",
            ),
        },
        "database_list": {
            "en": (
                "list of databases", "database list", "databases list", "all databases",
                "databases page", "database page", "available databases",
            ),
            "ar": (
                "قائمة قواعد البيانات", "كل قواعد البيانات", "صفحة قواعد البيانات",
            ),
        },
        "purchase_request": {
            "en": (
                "resource purchase", "purchase request", "request a title", "request title",
                "book not available", "resource not available", "not in the library", "not in catalog",
                "suggest a book", "recommend a book", "order a book", "buy a book",
            ),
            "ar": ("طلب شراء", "طلب عنوان", "اقتراح كتاب", "توصية بكتاب", "غير متوفر", "غير موجود"),
        },
        "course_reserve": {
            "en": (
                "course reserve", "course reservation", "reserve form", "faculty reserve",
                "reading list", "course materials", "reserve books for the course",
                "reserve books for course", "reserve materials for course",
                "faculty member reserve", "faculty member resrver", "faculty reserve books",
                "resrver books for the course", "resrver books for course",
                "resrve books for course", "reserver books for course",
            ),
            "ar": ("احتياطي", "حجز مقرر", "حجز مادة", "المقرر", "أعضاء هيئة التدريس"),
        },
        "comments": {
            "en": ("comments", "suggestions", "feedback", "complaint", "complaints"),
            "ar": ("ملاحظات", "اقتراحات", "شكوى", "تعليق", "تغذية راجعة"),
        },
        "pod_services": {
            "en": ("people of determination", "determination", "disability", "disabled", "accessibility", "special needs"),
            "ar": ("أصحاب الهمم", "ذوي الإعاقة", "إعاقة", "احتياجات خاصة", "إتاحة"),
        },
        "staff_contacts": {
            "en": ("phone", "email", "contact", "contacts", "staff directory", "staff", "librarian", "telephone"),
            "ar": ("هاتف", "رقم", "بريد", "تواصل", "اتصال", "موظفين", "أمين مكتبة"),
        },
        "faq": {
            "en": ("faq", "faqs", "frequently asked", "common questions"),
            "ar": ("أسئلة شائعة", "الأسئلة الشائعة", "استفسارات شائعة"),
        },
        "hours_location": {
            "en": ("library hours", "opening hours", "working hours", "library time", "when does the library open", "when is the library open", "library open", "open today", "closing time", "hours", "library location", "library locations", "where is the library", "directions to the library", "library map"),
            "ar": ("ساعات العمل", "أوقات العمل", "مواعيد", "متى تفتح", "تفتح المكتبة", "يفتح", "دوام", "أوقات الدوام", "مغلقة اليوم", "موقع المكتبة", "مواقع المكتبة", "مكان المكتبة", "وين المكتبة", "خريطة المكتبة"),
        },
        "borrowing": {
            "en": ("borrowing page", "borrowing services", "borrow page", "lending services page"),
            "ar": ("صفحة الإعارة", "خدمات الإعارة", "صفحة الاستعارة"),
        },
        "lending": {
            "en": ("lending policy", "access lending policy", "loan policy"),
            "ar": ("سياسة الإعارة", "سياسة الاستعارة", "سياسة إتاحة المصادر"),
        },
    }

    ACCESS_TERMS_EN = [
        "borrow", "borrowing", "lending", "loan", "loans", "renew", "renewal",
        "overdue", "fine", "fines", "hold", "holds", "place a hold",
        "reserve", "reserve book", "reserve books", "reserve a book",
        "course reserve", "short loan",
        "library membership", "library card", "liwa", "alumni", "community user",
        "remote access", "off-campus access", "off campus access", "e-resource", "e-resources", "electronic resources", "online resources", "database access", "access database", "access databases", "access the database", "access the databases", "database login", "databases login", "institutional login", "login with institution", "single sign",
        "licensed resources", "access to resources", "access to library", "opening hours"
    ]
    ACCESS_TERMS_AR = [
        "إعارة", "استعارة", "استعير", "أستعير", "اقدر استعير", "أقدر أستعير",
        "كم كتاب", "كم كتب", "كم مادة", "عدد الكتب", "مدة الاستعارة", "مدة الإعارة",
        "الاعارة", "الإعارة", "تجديد", "غرامة", "غرامات",
        "تأخير", "متأخر", "أجدد", "اجدد", "تجدد", "تمديد", "حجز", "الحجز", "إعارة قصيرة", "مصادر إلكترونية",
        "المصادر الإلكترونية", "الوصول عن بعد", "الدخول الموحد", "بطاقة المكتبة",
        "بطاقة العضوية", "الخريجون", "خريجو", "ليوا", "ساعات العمل"
    ]
    COLLECTION_TERMS_EN = [
        "collection management", "collection development", "recommend a book",
        "recommend books", "acquisition", "weeding", "deselection", "library collection"
    ]
    COLLECTION_TERMS_AR = [
        "تطوير المجموعات", "إدارة المجموعات", "اقتناء", "استبعاد", "توصية", "المجموعات"
    ]

    POLICY_HINT_TERMS_EN = [
        "library", "borrow", "borrowing", "checkout", "check out", "loan", "renew",
        "fine", "overdue", "hold", "reserve", "membership", "library card",
        "remote access", "off-campus access", "off campus access", "database access", "access database", "access databases", "access the database", "access the databases", "database login", "databases login", "institutional login", "login with institution", "e-resource", "electronic resources", "online resources", "opening hours",
        "how many books", "due date", "return books", "circulation"
    ]
    POLICY_HINT_TERMS_AR = [
        "مكتبة", "استعير", "أستعير", "إعارة", "اعارة",
        "تجديد", "غرامة", "تأخير", "حجز", "بطاقة",
        "عضوية", "ساعات", "كم كتاب", "الوصول عن بعد"
    ]

    @staticmethod
    def might_be_policy_query(query: str) -> bool:
        q = (query or "").lower()
        if LibraryPolicyAnswerService.detect_policy_topic(query):
            return True
        if LibraryPolicyAnswerService._contains_any(q, LibraryPolicyAnswerService.POLICY_HINT_TERMS_EN):
            return True
        return any(term in (query or "") for term in LibraryPolicyAnswerService.POLICY_HINT_TERMS_AR)

    @staticmethod
    def _contains_any(text: str, terms: List[str]) -> bool:
        normalized = (text or "").lower()
        for term in terms:
            clean_term = str(term or "").lower().strip()
            if not clean_term:
                continue
            if re.search(r"[\u0600-\u06FF]", clean_term):
                if clean_term in normalized:
                    return True
                continue
            if re.search(r"\W", clean_term):
                pattern = r"(?<!\w)" + re.escape(clean_term) + r"(?!\w)"
            else:
                pattern = r"\b" + re.escape(clean_term) + r"\b"
            if re.search(pattern, normalized, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _detect_service_topic(query: str) -> Optional[str]:
        q = (query or "").lower()
        raw_query = query or ""
        for topic, rules in LibraryPolicyAnswerService.SERVICE_TOPIC_RULES.items():
            if LibraryPolicyAnswerService._contains_any(q, list(rules.get("en", ()))):
                return topic
            if any(term in raw_query for term in rules.get("ar", ())):
                return topic
        return None

    @staticmethod
    def detect_policy_topic(query: str) -> Optional[str]:
        q = (query or "").lower()
        has_arabic = bool(re.search(r"[\u0600-\u06FF]", query or ""))
        database_advice_patterns = (
            r"\b(what|which|best|recommended?)\s+databases?\b",
            r"\b(recommend|suggest)\s+(?:some\s+)?databases?\b",
            r"\bdatabases?\s+(?:are\s+)?(?:best\s+)?for\b",
        )
        database_advice_ar = (
            "أفضل قواعد البيانات", "ما قواعد البيانات", "ما هي قواعد البيانات",
            "أي قواعد بيانات", "اقترح قواعد بيانات", "قواعد بيانات مناسبة",
            "قواعد البيانات للحاسب", "قواعد البيانات للكمبيوتر",
        )
        if any(re.search(pattern, q) for pattern in database_advice_patterns) or any(
                term in (query or "") for term in database_advice_ar):
            return "database_recommendation"
        service_topic = LibraryPolicyAnswerService._detect_service_topic(query)
        if service_topic:
            return service_topic
        if (LibraryPolicyAnswerService._contains_any(q, [t.lower() for t in LibraryPolicyAnswerService.COLLECTION_TERMS_EN]) or
                any(t in (query or "") for t in LibraryPolicyAnswerService.COLLECTION_TERMS_AR)):
            return "collection"
        if (LibraryPolicyAnswerService._contains_any(q, [t.lower() for t in LibraryPolicyAnswerService.ACCESS_TERMS_EN]) or
                any(t in (query or "") for t in LibraryPolicyAnswerService.ACCESS_TERMS_AR)):
            return "access"
        if "access" in q and any(word in q for word in ["library", "database", "databases", "resource", "resources", "ebook", "journal", "article", "remote", "online"]):
            return "access"
        if any(word in q for word in ["database", "databases", "e-resource", "e-resources", "electronic resources"]) and any(verb in q for verb in ["access", "login", "log in", "use", "open", "connect", "remote", "home", "off campus", "off-campus"]):
            return "access"
        if has_arabic:
            arabic_query = query or ""
            lending_verbs = ["استعير", "أستعير", "استلف", "اقترض", "اعير", "أعير"]
            lending_objects = ["كتاب", "كتب", "مادة", "مواد", "مصدر", "مصادر"]
            if any(verb in arabic_query for verb in lending_verbs):
                return "access"
            if "كم" in arabic_query and any(obj in arabic_query for obj in lending_objects):
                return "access"
            if "عدد" in arabic_query and any(obj in arabic_query for obj in lending_objects):
                return "access"
            if "وصول" in arabic_query and any(word in arabic_query for word in ["المكتبة", "المصادر", "الإلكترونية", "عن بعد"]):
                return "access"
        return None

    @staticmethod
    async def classify_policy_topic(query: str, is_arabic: bool = False) -> Optional[str]:
        """Use rules first, then Gemini only to classify policy intent."""
        direct_topic = LibraryPolicyAnswerService.detect_policy_topic(query)
        if direct_topic or not GEMINI_MODEL:
            return direct_topic
        if not LibraryPolicyAnswerService.might_be_policy_query(query):
            return None

        safe_query = safe_llm_field(query, MAX_QUERY_LENGTH)
        language_hint = "Arabic" if is_arabic else "English"
        prompt = f"""You classify the user's intent for a UAEU Library assistant.

USER MESSAGE ({language_hint}): "{safe_query}"

Allowed intents:
- access: borrowing, lending, renewals, fines, holds, course reserves, library access, remote/database/e-resource access, opening hours, alumni/community access, IDs, loan duration, or how many items/books can be borrowed.
- collection: collection management/development policy, acquisitions, recommending library materials, weeding, deselection, or library collection principles.
- book_search: user wants books on a topic, author, title, or subject.
- article_search: user wants scholarly articles, journals, papers, publications, or databases for a research topic.
- other: anything else.

Return ONLY JSON:
{{"intent":"access|collection|book_search|article_search|other","confidence":0.0}}

Do not answer the user. Do not explain."""
        try:
            result = await asyncio.wait_for(
                generate_gemini_content(prompt, temperature=0.0, max_output_tokens=80),
                timeout=4.0,
            )
            if not result or not result.text:
                return None
            data = parse_llm_json(result.text)
            intent = str(data.get("intent", "")).strip().lower()
            confidence = float(data.get("confidence", 0))
            if confidence < 0.68:
                return None
            if intent == "access":
                return "access"
            if intent == "collection":
                return "collection"
            return None
        except Exception as e:
            logger.warning(f"Policy intent classifier fallback skipped: {e}")
            return None

    @staticmethod
    async def answer_for_topic(query: str, topic: Optional[str], is_arabic: bool = False) -> Optional[str]:
        if topic == "human_help":
            return LibraryPolicyAnswerService._human_help_answer(is_arabic)
        if topic == "database_recommendation":
            return LibraryPolicyAnswerService._database_recommendation_answer(query, is_arabic)
        if topic in LibraryPolicyAnswerService.SERVICE_SOURCE_LINKS and topic not in {"collection", "lending"}:
            return await LibraryPolicyAnswerService._service_answer(topic, is_arabic)
        if topic == "lending":
            return LibraryPolicyAnswerService._access_answer_ar(query) if is_arabic else LibraryPolicyAnswerService._access_answer_en(query)
        if topic == "collection":
            return LibraryPolicyAnswerService._collection_answer_ar() if is_arabic else LibraryPolicyAnswerService._collection_answer_en()
        if topic == "access":
            return LibraryPolicyAnswerService._access_answer_ar(query) if is_arabic else LibraryPolicyAnswerService._access_answer_en(query)
        return None

    @staticmethod
    async def answer(query: str, is_arabic: bool = False) -> Optional[str]:
        topic = LibraryPolicyAnswerService.detect_policy_topic(query)
        return await LibraryPolicyAnswerService.answer_for_topic(query, topic, is_arabic)

    @staticmethod
    def _human_help_answer(is_arabic: bool = False) -> str:
        if is_arabic:
            return "\n".join([
                "يمكنك التحدث مع موظف من مكتبات جامعة الإمارات مباشرة عبر LibChat.",
                "",
                "اضغط على زر 💬 LibChat في الشريط العلوي، أو افتح صفحة اسألنا الرسمية:",
                LibraryPolicyAnswerService.LIBCHAT_PAGE_URL_AR,
            ])
        return "\n".join([
            "You can talk to a UAEU Libraries staff member through LibChat.",
            "",
            "Click the 💬 LibChat button in the top bar, or open the official Ask Us page:",
            LibraryPolicyAnswerService.LIBCHAT_PAGE_URL_EN,
        ])

    @staticmethod
    def _database_recommendation_answer(query: str, is_arabic: bool = False) -> str:
        databases = ArticleSearchService.recommend_uaeu_databases(query, limit=5)
        directory_url = LibraryPolicyAnswerService._service_url("database_list", is_arabic)
        if is_arabic:
            lines = [
                "أفضل قواعد بيانات جامعة الإمارات لهذا الموضوع:",
                "",
            ]
            lines.extend(
                f"- {db.get('db', 'قاعدة بيانات')}: {db.get('search') or db.get('url', '')}"
                for db in databases
            )
            lines.extend([
                "",
                "هذه توصيات موضوعية وليست فحصاً مباشراً لمحتوى قواعد البيانات.",
                f"قائمة قواعد البيانات الرسمية: {directory_url}",
            ])
            return "\n".join(lines)

        lines = [
            "Best UAEU databases for this topic:",
            "",
        ]
        lines.extend(
            f"- {db.get('db', 'Database')}: {db.get('search') or db.get('url', '')}"
            for db in databases
        )
        lines.extend([
            "",
            "These are subject recommendations, not live searches inside the databases.",
            f"Official databases list: {directory_url}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _service_url(topic: str, is_arabic: bool = False) -> str:
        lang = "ar" if is_arabic else "en"
        entry = LibraryPolicyAnswerService.SERVICE_SOURCE_LINKS.get(topic, {})
        return entry.get(lang) or entry.get("en", "")

    @staticmethod
    async def _fetch_service_page_text(topic: str, is_arabic: bool = False) -> str:
        url = LibraryPolicyAnswerService._service_url(topic, is_arabic)
        response = await _http_get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return response.text

    @staticmethod
    def _html_to_lines(html_text: str) -> List[str]:
        content = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html_text or "")
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</(p|div|li|tr|td|th|h[1-6]|section|article)>", "\n", content)
        content = re.sub(r"(?s)<[^>]+>", " ", content)
        content = unescape(content)
        lines = []
        for raw_line in content.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" -*•\t")
            if line and line not in lines:
                lines.append(line)
        return lines

    @staticmethod
    def _extract_hours_lines(html_text: str, is_arabic: bool = False) -> List[str]:
        def fragment_text(fragment: str) -> str:
            return " ".join(LibraryPolicyAnswerService._html_to_lines(fragment)).strip()

        lines: List[str] = []
        page = html_text or ""
        marker = "المكتبة مفتوحة" if is_arabic else "Library Reference Phone No"
        marker_index = page.find(marker)
        if marker_index < 0:
            marker_index = page.find("pcschedule")
        if marker_index >= 0:
            page = page[marker_index:marker_index + 12000]

        # The official hours page stores the schedule in a stable table
        # (`pcschedule`). Parsing that table is cleaner than sending the whole
        # UAEU page chrome through a generic HTML-to-text extractor.
        table_start = page.find("<table")
        pre_table = page[:table_start] if table_start >= 0 else page[:2500]
        pre_text = " ".join(LibraryPolicyAnswerService._html_to_lines(pre_table))
        if is_arabic:
            patterns = (
                r"المكتبة مفتوحة.*?\.",
                r"مكتب الاستعلامات\s*:\s*[0-9\s]+",
                r"يتواجد المختصون.*?الجمع\s*ة\s*\.?",
            )
        else:
            patterns = (
                r"Library Reference Phone No:\s*[0-9\s]+",
                r"Reference staff are available.*?Friday\.",
            )
        for pattern in patterns:
            match = re.search(pattern, pre_text)
            if match:
                line = re.sub(r"\s+", " ", match.group(0)).strip()
                line = line.replace("الجمع ة", "الجمعة")
                if line and line not in lines:
                    lines.append(line)

        table_match = re.search(r'(?is)<table[^>]*id=["\']pcschedule["\'][^>]*>(.*?)</table>', page)
        if table_match:
            for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table_match.group(1)):
                cells = [fragment_text(cell) for cell in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
                cells = [cell for cell in cells if cell]
                if cells:
                    row_text = " | ".join(cells)
                    if row_text not in lines:
                        lines.append(row_text)

        location_match = re.search(r"(?is)<h2[^>]*>\s*<em>\s*(\*.*?located.*?)</em>\s*</h2>", page)
        if location_match:
            location = fragment_text(location_match.group(1))
            if location and location not in lines:
                lines.append(location)

        if not lines:
            # Last-resort fallback if the official page changes its markup.
            all_lines = LibraryPolicyAnswerService._html_to_lines(page)
            keywords = (
                "ساعات", "مفتوحة", "مغلقة", "الأحد", "الإثنين", "الجمعة", "صباح", "مساء"
            ) if is_arabic else (
                "Library Hours", "Reference", "Mon", "Fri", "Sat", "Sun", "Closed", "AM", "PM"
            )
            for line in all_lines:
                if any(keyword in line for keyword in keywords) and "div]:" not in line:
                    lines.append(line)
                if len(lines) >= 12:
                    break

        return lines[:14]

    @staticmethod
    def _format_hours_answer(lines: List[str], url: str, is_arabic: bool = False) -> str:
        if is_arabic:
            if not lines:
                return "\n".join([
                    "لم أتمكن من قراءة ساعات العمل الحالية من الصفحة الرسمية الآن.",
                    "يرجى مراجعة الرابط الرسمي لأن ساعات العمل قد تتغير حسب الفصل الدراسي أو رمضان.",
                    "",
                    f"الرابط الرسمي: {url}",
                ])
            return "\n".join([
                "حسب صفحة ساعات عمل مكتبات جامعة الإمارات الرسمية:",
                "",
                *[f"- {line}" for line in lines],
                "",
                "ملاحظة: قد تتغير ساعات العمل حسب الفصل الدراسي أو رمضان، لذلك يُفضل التأكد من الصفحة الرسمية.",
                f"الرابط الرسمي: {url}",
            ])

        if not lines:
            return "\n".join([
                "I could not read the current hours from the official page right now.",
                "Please check the official page because hours may change by semester or Ramadan.",
                "",
                f"Official link: {url}",
            ])
        return "\n".join([
            "According to the official UAEU Library Hours page:",
            "",
            *[f"- {line}" for line in lines],
            "",
            "Note: library hours may change by semester, summer term, or Ramadan, so please confirm on the official page.",
            f"Official link: {url}",
        ])

    @staticmethod
    def _cache_key(topic: str, is_arabic: bool) -> str:
        return f"{topic}:{'ar' if is_arabic else 'en'}"

    @staticmethod
    def _cached_service_answer(topic: str, is_arabic: bool) -> Optional[str]:
        key = LibraryPolicyAnswerService._cache_key(topic, is_arabic)
        cached = LibraryPolicyAnswerService._service_page_cache.get(key)
        if not cached:
            return None
        age = datetime.now(timezone.utc).timestamp() - cached.get("fetched_at", 0)
        if age < LibraryPolicyAnswerService.SERVICE_PAGE_CACHE_TTL_SECONDS:
            return cached.get("answer")
        LibraryPolicyAnswerService._service_page_cache.pop(key, None)
        return None

    @staticmethod
    def _store_service_answer(topic: str, is_arabic: bool, answer: str) -> str:
        key = LibraryPolicyAnswerService._cache_key(topic, is_arabic)
        LibraryPolicyAnswerService._service_page_cache[key] = {
            "fetched_at": datetime.now(timezone.utc).timestamp(),
            "answer": answer,
        }
        return answer

    @staticmethod
    def _main_content_html(html_text: str) -> str:
        html_text = html_text or ""
        marker_index = html_text.find("link-uaeu")
        if marker_index >= 0:
            return html_text[marker_index:marker_index + 45000]
        main_index = html_text.find("main-content")
        if main_index >= 0:
            return html_text[main_index:main_index + 45000]
        return html_text[:45000]

    @staticmethod
    def _extract_service_lines(html_text: str, is_arabic: bool = False, limit: int = 8) -> List[str]:
        content = LibraryPolicyAnswerService._main_content_html(html_text)
        lines = LibraryPolicyAnswerService._html_to_lines(content)
        skip_terms = (
            "Customer pulse", "Home ›", "Popular Searches", "link-uaeu", "Quick Links", "Follow Us",
            "Copyright", "United Arab Emirates University", "Close modal", "Top of page",
            "About Overview", "Library Services", "Information Resources", "Media News",
            "الصفحة الرئيسة", "الروابط السريعة", "نبض العملاء", "عن المكتبات الجامعية",
            "خدمات المكتبة", "مصادر المعلومات", "وسائل الإعلام", "حقوق النشر",
        )
        useful = []
        for line in lines:
            line = re.sub(r"\s+", " ", line).strip()
            if not line or any(term in line for term in skip_terms):
                continue
            if "div]:" in line or len(line) < 4:
                continue
            if len(line) > 260:
                line = line[:257].rstrip() + "..."
            if line not in useful:
                useful.append(line)
            if len(useful) >= limit:
                break
        return useful

    @staticmethod
    def _static_service_message(topic: str, is_arabic: bool = False) -> str:
        if is_arabic:
            messages = {
                "borrowing": "يمكنك مراجعة صفحة الإعارة الرسمية لمعرفة خدمات الإعارة وطريقة الاستعارة.",
                "comments": "يمكنك إرسال الملاحظات أو الاقتراحات من خلال النموذج الرسمي.",
                "course_reserve": "يمكن لأعضاء هيئة التدريس استخدام نموذج الاحتياطي الدراسي لإتاحة مواد المقرر للطلبة.",
                "purchase_request": "إذا لم يكن الكتاب أو المصدر متوفراً في المكتبة، يمكنك إرسال طلب شراء/توفير مصدر من خلال النموذج الرسمي.",
                "pod_services": "تتوفر خدمات مخصصة لأصحاب الهمم من خلال نموذج الخدمات الرسمي.",
                "staff_contacts": "يمكنك العثور على أرقام الهواتف والبريد الإلكتروني لموظفي المكتبة في دليل الموظفين الرسمي.",
                "faq": "يمكنك مراجعة صفحة الأسئلة الشائعة للحصول على إجابات رسمية عن خدمات المكتبة.",
                "hours_location": "يرجى مراجعة صفحة ساعات العمل والمواقع الرسمية لأن المواعيد قد تتغير.",
                "borrowing_history": (
                    "يمكنك عرض الكتب التي استعرتها سابقاً من خلال فتح صفحة سجل الاستعارة الرسمية "
                    "وتسجيل الدخول باستخدام بيانات اعتماد جامعة الإمارات."
                ),
                "account_fees": (
                    "يمكنك عرض الغرامات أو الرسوم المسجلة على حسابك من خلال فتح صفحة الرسوم الرسمية "
                    "وتسجيل الدخول باستخدام بيانات اعتماد جامعة الإمارات."
                ),
                "library_account": (
                    "يمكنك عرض الكتب الموجودة في حسابك والإعارات الحالية من خلال فتح حساب WorldCat الرسمي "
                    "وتسجيل الدخول باستخدام بيانات اعتماد جامعة الإمارات."
                ),
                "catalog_search": "يمكنك البحث مباشرة في فهرس مكتبات جامعة الإمارات عبر WorldCat.",
                "new_arrivals": "يمكنك مراجعة صفحة المواد والكتب الجديدة المضافة لمكتبات جامعة الإمارات.",
                "database_list": "يمكنك فتح قائمة قواعد البيانات الإلكترونية المتاحة من مكتبات جامعة الإمارات.",
            }
            return messages.get(topic, "يمكنك استخدام الرابط الرسمي التالي للحصول على المعلومات.")

        messages = {
            "borrowing": "Use the official Borrowing page for borrowing services and checkout information.",
            "comments": "Use the official Comments and Suggestions form to send feedback to UAEU Libraries.",
            "course_reserve": "Faculty members can use the Course Reserve form to request course materials for students.",
            "purchase_request": "If a book or resource is not available, submit a Resource Purchase Request using the official form.",
            "pod_services": "Services for People of Determination are available through the official service request form.",
            "staff_contacts": "Use the staff directory for UAEU Libraries phone numbers and email contacts.",
            "faq": "Use the official Frequently Asked Questions page for library service answers.",
            "hours_location": "Check the official library hours and locations page because hours may change.",
            "borrowing_history": (
                "You can view books you previously borrowed by opening the official borrowing-history page "
                "and signing in with your UAEU credentials."
            ),
            "account_fees": (
                "You can view fines or fees on your account by opening the official fees page "
                "and signing in with your UAEU credentials."
            ),
            "library_account": (
                "You can view the books in your account and your current checkouts by opening the official "
                "WorldCat account page and signing in with your UAEU credentials."
            ),
            "catalog_search": "Search the UAEU Libraries catalog directly through WorldCat.",
            "new_arrivals": "View newly added books and materials for UAEU Libraries.",
            "database_list": "Open the UAEU Libraries databases list to browse available electronic databases.",
        }
        return messages.get(topic, "Use the official UAEU Libraries page below for this service.")

    @staticmethod
    def _format_service_page_answer(topic: str, lines: List[str], url: str, is_arabic: bool = False) -> str:
        if is_arabic:
            if not lines:
                return "\n".join([
                    LibraryPolicyAnswerService._static_service_message(topic, True),
                    "",
                    f"الرابط الرسمي: {url}",
                ])
            return "\n".join([
                "راجعت الصفحة الرسمية لمكتبات جامعة الإمارات ووجدت:",
                "",
                *[f"- {line}" for line in lines],
                "",
                "ملاحظة: يتم تحديث هذه المعلومات من الصفحة الرسمية وتُخزن مؤقتاً لمدة 6 ساعات.",
                f"الرابط الرسمي: {url}",
            ])

        if not lines:
            return "\n".join([
                LibraryPolicyAnswerService._static_service_message(topic, False),
                "",
                f"Official link: {url}",
            ])
        return "\n".join([
            "I checked the official UAEU Libraries page and found:",
            "",
            *[f"- {line}" for line in lines],
            "",
            "Note: this information is pulled from the official page and cached for 6 hours.",
            f"Official link: {url}",
        ])

    @staticmethod
    async def _service_answer(topic: str, is_arabic: bool = False) -> str:
        cached = LibraryPolicyAnswerService._cached_service_answer(topic, is_arabic)
        if cached:
            return cached

        url = LibraryPolicyAnswerService._service_url(topic, is_arabic)
        if topic in {
            "borrowing_history", "account_fees", "library_account",
            "catalog_search", "new_arrivals", "database_list",
        }:
            answer = "\n".join([
                LibraryPolicyAnswerService._static_service_message(topic, is_arabic),
                "",
                f"{'الرابط الرسمي' if is_arabic else 'Official link'}: {url}",
            ])
            return LibraryPolicyAnswerService._store_service_answer(topic, is_arabic, answer)

        if topic == "hours_location":
            try:
                html_text = await LibraryPolicyAnswerService._fetch_service_page_text(topic, is_arabic)
                lines = LibraryPolicyAnswerService._extract_hours_lines(html_text, is_arabic)
                answer = LibraryPolicyAnswerService._format_hours_answer(lines, url, is_arabic)
            except Exception as e:
                logger.warning(f"Could not fetch UAEU library hours page: {e}")
                answer = LibraryPolicyAnswerService._format_hours_answer([], url, is_arabic)
            return LibraryPolicyAnswerService._store_service_answer(topic, is_arabic, answer)

        if not url.lower().endswith((".shtml", ".html", ".htm")):
            answer = "\n".join([
                LibraryPolicyAnswerService._static_service_message(topic, is_arabic),
                "",
                f"{'الرابط الرسمي' if is_arabic else 'Official link'}: {url}",
            ])
            return LibraryPolicyAnswerService._store_service_answer(topic, is_arabic, answer)

        try:
            html_text = await LibraryPolicyAnswerService._fetch_service_page_text(topic, is_arabic)
            lines = LibraryPolicyAnswerService._extract_service_lines(html_text, is_arabic)
            answer = LibraryPolicyAnswerService._format_service_page_answer(topic, lines, url, is_arabic)
        except Exception as e:
            logger.warning(f"Could not fetch UAEU service page for {topic}: {e}")
            answer = LibraryPolicyAnswerService._format_service_page_answer(topic, [], url, is_arabic)

        return LibraryPolicyAnswerService._store_service_answer(topic, is_arabic, answer)

    @staticmethod
    def _access_answer_en(query: str) -> str:
        q = (query or "").lower()
        source = (
            "Official sources:\n"
            "https://www.uaeu.ac.ae/en/library/pdf/2_access_lending-en.pdf\n"
            "https://www.uaeu.ac.ae/en/library/borrowing.shtml"
        )
        if any(term in q for term in ["renew", "renewal"]):
            body = [
                "Users may renew borrowed items with their ID card and PIN:",
                "- Online through the library website under Lending Services, available 24/7.",
                "- Renew online: https://uaeu.account.worldcat.org/account/checkouts",
                "- By email: circ_lib@uaeu.ac.ae.",
                "- By telephone to any Loans Desk.",
                "- In person at any Loans Desk or Self-check machine.",
                "Items cannot be renewed if another user placed a hold, the item is on Course Reserves, the maximum renewals were used, or the user has accumulated fines."
            ]
        elif any(term in q for term in ["fine", "fines", "overdue", "lost", "damaged", "damage"]):
            body = [
                "The policy gives a 7-day grace period after the due date for library materials.",
                "- Overdue books: 1 DH per item per day.",
                "- Special Collections and multimedia: 4 DH per item per day.",
                "- Maximum overdue fine per item: 45 DH.",
                "- Lost items: replacement cost plus a 100 DH administration fee.",
                "- Damaged items: 30 DH for older items, or up to replacement cost plus 100 DH for recent items that need replacement.",
                "Course Reserve fines are charged hourly: 3 DH/hour for 24-hour loans, and 5 DH/hour for 3-hour or 4-hour loans."
            ]
        elif any(term in q for term in ["hold", "holds", "reserve", "course reserve"]):
            body = [
                "Borrowers may place holds on circulating items, including items available at another library location or checked out by another user.",
                "When a held item becomes available, it is kept at the Loans Desk for 3 days and the requester is notified by email.",
                "If it is not picked up after 3 days, it is re-shelved.",
                "Course Reserve materials are placed by faculty request so students in a course have equal access. Faculty should submit Course Reserve requests 3 weeks before the semester."
            ]
        elif any(term in q for term in ["remote", "e-resource", "e-resources", "database", "online", "single sign", "licensed"]):
            body = [
                "UAEU Libraries provide online and remote access to e-resources through University identification and Single Sign-On authentication.",
                "Remote access requires a current UAEU sign-on or UAEU email.",
                "Some databases, journals, and e-books may have limits because access depends on publisher license agreements, including concurrent-user limits or remote-access restrictions.",
                "Community users, adjunct professors, and alumni may access e-resources and collections where publisher licenses permit."
            ]
        elif any(term in q for term in ["alumni", "community", "membership", "liwa"]):
            body = [
                "UAEU graduate alumni are exempt from library membership subscription fees.",
                "Alumni may use licensed electronic resources and special collections on-site in UAEU Libraries, and may borrow printed published materials as full members of the university community.",
                "Remote access for alumni to externally licensed resources requires an individual application to library management and depends on publisher license agreements.",
                "LIWA Consortium members may request up to 10 items through LIWA; checkout rules depend on the home institution."
            ]
        else:
            body = [
                "Required ID:",
                "| User group | Required ID |",
                "|---|---|",
                "| UAEU students, faculty, and staff | University ID |",
                "| Zayed University and Higher Colleges of Technology users | Valid home-institution ID or barcode |",
                "| UAE community users | UAEU Libraries membership ID |",
                "",
                "Loan rules:",
                "| User category | General books | Emirates books | Media materials | Bound journals |",
                "|---|---|---|---|---|",
                "| UAEU undergraduate and master's students | 10 items, 30 days, 2 renewals, 30 days each | 10 items, 14 days, 2 renewals, 14 days each | 7 days | Short-loan or in-library use |",
                "| Faculty, instructors, teaching assistants, and PhD students | 20 items, 120 days, 2 renewals, 120 days each | 20 items, 14 days, 2 renewals, 14 days each | 7 days | Short-loan or in-library use |",
                "| Staff, graduate alumni, and UAE community users | 10 items, 30 days, 2 renewals | 10 items, 14 days, 2 renewals | 7 days | Short-loan or in-library use |"
            ]
        return "\n".join(["Library Access and Lending Policy", "", *body, "", source])

    @staticmethod
    def _access_answer_ar(query: str) -> str:
        q = query or ""
        source = (
            "المصادر الرسمية:\n"
            "https://www.uaeu.ac.ae/ar/library/pdf/2_access_lending-ar.pdf\n"
            "https://www.uaeu.ac.ae/ar/library/borrowing.shtml"
        )
        if any(term in q for term in ["تجديد", "تمديد", "أجدد", "اجدد", "تجدد", "جدد"]):
            body = [
                "يمكن تجديد استعارة المواد باستخدام البطاقة الجامعية والرقم السري من خلال:",
                "- موقع المكتبة تحت خدمات الإعارة على مدار 24/7.",
                "- رابط التجديد الإلكتروني: https://uaeu.account.worldcat.org/account/checkouts",
                "- البريد الإلكتروني: circ_lib@uaeu.ac.ae.",
                "- الهاتف مع موظفي الإعارة.",
                "- الحضور إلى مكتب الإعارة أو استخدام جهاز الإعارة الذاتية.",
                "لا يتم التجديد إذا كانت المادة محجوزة لمستخدم آخر، أو مخصصة للاحتياطي الدراسي، أو استُنفدت مرات التجديد، أو توجد غرامات على المستخدم."
            ]
        elif any(term in q for term in ["غرامة", "غرامات", "تأخير", "مفقود", "تالف"]):
            body = [
                "توجد فترة سماح 7 أيام بعد تاريخ الاستحقاق لمواد المكتبة.",
                "- الكتب المتأخرة: 1 درهم لكل مادة عن كل يوم.",
                "- المجموعات الخاصة والوسائط المتعددة: 4 دراهم لكل مادة عن كل يوم.",
                "- الحد الأقصى لغرامة التأخير لكل مادة: 45 درهماً.",
                "- المواد المفقودة: تكلفة الاستبدال إضافة إلى 100 درهم رسوم إدارية.",
                "- المواد التالفة: 30 درهماً للمواد القديمة، أو تكلفة الاستبدال مع 100 درهم للمواد الحديثة التي يلزم استبدالها.",
                "غرامات الاحتياطي الدراسي: 3 دراهم لكل ساعة لقرض 24 ساعة، و5 دراهم لكل ساعة لقرض 3 أو 4 ساعات."
            ]
        elif any(term in q for term in ["حجز", "احتياطي", "المقرر", "المساق"]):
            body = [
                "يمكن للمستفيدين حجز المواد المتاحة للإعارة، سواء كانت على رفوف موقع آخر أو معارة لمستخدم آخر.",
                "عند توفر المادة المحجوزة، تحفظ في وحدة الإعارة لمدة 3 أيام ويتم إشعار المستفيد بالبريد الإلكتروني.",
                "إذا لم يتم استلامها خلال 3 أيام، تعاد إلى الرفوف.",
                "مواد الاحتياطي الدراسي توضع بناءً على طلب عضو هيئة التدريس لضمان وصول الطلبة إليها، ويفضل إرسال الطلب قبل بداية الفصل بثلاثة أسابيع."
            ]
        elif any(term in q for term in ["عن بعد", "إلكترونية", "قواعد البيانات", "الدخول الموحد", "اونلاين", "مرخصة"]):
            body = [
                "توفر عمادة المكتبات الوصول إلى المصادر الإلكترونية عن بعد باستخدام هوية الجامعة ونظام الدخول الموحد.",
                "يتطلب الوصول عن بعد حساباً جامعياً فعالاً أو بريد جامعة الإمارات.",
                "قد تخضع بعض قواعد البيانات والدوريات والكتب الإلكترونية لقيود تراخيص الناشرين، مثل عدد المستخدمين المتزامنين أو قيود الوصول عن بعد.",
                "يمكن للمجتمع والخريجين والأساتذة غير المتفرغين الوصول إلى المصادر والمجموعات عندما تسمح تراخيص الناشرين بذلك."
            ]
        elif any(term in q for term in ["خريج", "الخريجون", "المجتمع", "عضوية", "ليوا"]):
            body = [
                "يعفى خريجو جامعة الإمارات من رسوم الاشتراك للحصول على خدمات المكتبات.",
                "يمكن للخريجين استخدام المصادر الإلكترونية المرخصة والمجموعات الخاصة داخل مكتبات جامعة الإمارات، واستعارة المواد المطبوعة المنشورة مثل أعضاء مجتمع الجامعة.",
                "الوصول عن بعد للخريجين إلى المصادر المرخصة يخضع لطلب فردي لإدارة المكتبة وتراخيص الناشرين.",
                "منتسبو ليوا يمكنهم طلب 10 مصادر كحد أقصى، ويخضع عدد المواد المسموح بإعارتها للوائح جهة المستخدم الأصلية."
            ]
        else:
            body = [
                "بطاقة التعريف المطلوبة:",
                "| فئة المستخدم | بطاقة التعريف المطلوبة |",
                "|---|---|",
                "| طلبة وموظفو وأعضاء هيئة التدريس بجامعة الإمارات | البطاقة الجامعية |",
                "| منتسبو جامعة زايد وكليات التقنية العليا | بطاقة المؤسسة الأصلية |",
                "| مستخدمو المجتمع | بطاقة عضوية مكتبات جامعة الإمارات |",
                "",
                "قواعد الإعارة:",
                "| فئة المستخدم | الكتب العامة | كتب الإمارات | الوسائل السمعية والبصرية | مجلدات الدوريات |",
                "|---|---|---|---|---|",
                "| طلبة البكالوريوس والماجستير | 10 مواد، 30 يوماً، تجديدان، 30 يوماً لكل تجديد | 10 مواد، 14 يوماً، تجديدان، 14 يوماً لكل تجديد | 7 أيام | إعارة قصيرة أو استخدام داخل المكتبة |",
                "| أعضاء هيئة التدريس والمدرسون ومساعدو التدريس وطلبة الدكتوراه | 20 مادة، 120 يوماً، تجديدان | 20 مادة، 14 يوماً، تجديدان | 7 أيام | إعارة قصيرة أو استخدام داخل المكتبة |",
                "| الموظفون والخريجون ومستخدمو المجتمع | 10 مواد، 30 يوماً، تجديدان | 10 مواد، 14 يوماً، تجديدان | 7 أيام | إعارة قصيرة أو استخدام داخل المكتبة |"
            ]
        return "\n".join(["سياسة الإعارة وإتاحة المصادر", "", *body, "", source])

    @staticmethod
    def _collection_answer_en() -> str:
        return "\n".join([
            "Collection Management Policy",
            "",
            "UAEU Libraries maintain collections that support teaching, research, and service for the University community.",
            "The collection includes materials for course preparation, student assignments, faculty and student research, UAE and regional history, and national development agenda areas.",
            "The Libraries collect or license books, reference works, periodicals, databases, special collections, archival materials, maps, guides, indexes, abstracts, and emerging formats, with a preference for electronic resources where suitable.",
            "Faculty and the UAEU community are encouraged to recommend materials, but final selection and deselection decisions are held by UAEU Libraries and the Content and Scholarly Communication Section.",
            "",
            "https://www.uaeu.ac.ae/en/library/pdf/1_collection_management-en.pdf"
        ])

    @staticmethod
    def _collection_answer_ar() -> str:
        return "\n".join([
            "سياسة تطوير المجموعات",
            "",
            "تحافظ عمادة المكتبات على مجموعات تدعم التعليم والبحث العلمي وخدمة الجامعة والمجتمع.",
            "تشمل المجموعات المواد التي تخدم إعداد المساقات، واجبات الطلبة، أبحاث أعضاء هيئة التدريس والطلبة، وتاريخ دولة الإمارات والمنطقة، والمجالات المرتبطة بالأجندة الوطنية.",
            "تجمع العمادة أو ترخص الكتب والمراجع والدوريات وقواعد البيانات والمجموعات الخاصة والمواد الأرشيفية والخرائط والأدلة والفهارس والمستخلصات والصيغ الحديثة، مع تفضيل المصادر الإلكترونية متى كانت مناسبة.",
            "يمكن لأعضاء هيئة التدريس ومجتمع الجامعة التوصية بالمواد، لكن القرار النهائي للاختيار أو الاستبعاد يكون لدى عمادة المكتبات وقسم المصادر والتواصل العلمي.",
            "",
            "https://www.uaeu.ac.ae/ar/library/pdf/1_collection_management-ar.pdf"
        ])


# ============================
# 10A. INTENT ROUTER SERVICE
# ============================
class IntentRouterService:
    """Pre-search router for chatbot behavior.

    The router decides whether a message should be answered directly, sent to
    Gemini as a general question, or sent to the book/article search pipeline.
    This prevents support questions from becoming accidental catalog searches.
    """

    SEARCH_INTENT_PATTERNS = {
        "all": [
            r"\b(all resources|everything|books and articles|articles and books|all sources|all library resources)\b",
            r"(كل المصادر|جميع المصادر|كل الموارد|كتب ومقالات|مقالات وكتب)",
        ],
        "research": [
            r"\b(articles?|journals?|research papers?|papers?|publications?|doi|pubmed|peer reviewed)\b",
            r"(مقال|مقالات|بحث|أبحاث|دورية|دوريات|محكمة)",
        ],
        "books": [
            r"\b(books?|ebooks?|e-books?|textbooks?|catalog|author|isbn)\b",
            r"(كتاب|كتب|كتاب إلكتروني|كتب إلكترونية|كتالوج|مؤلف|ردمك)",
        ],
    }

    GENERAL_AI_PATTERNS = [
        r"^(what is|what are|explain|define|summarize|compare|how does|why does|why is)\b",
        r"^(ما هو|ما هي|اشرح|عرّف|عرف|لخص|قارن|كيف يعمل|لماذا)\b",
    ]

    LIBRARY_RELATED_PATTERNS = [
        r"\b(library|libraries|uaeu|book|books|ebook|ebooks|article|articles|journal|journals|paper|papers|database|databases|catalog|worldcat|borrow|borrowing|loan|loans|renew|fine|fines|hours|location|locations|libchat|librarian|citation|citations|faq|faqs|frequently asked|resource|resources|research resource|research resources|resource request)\b",
        r"(مكتبة|جامعة الإمارات|كتاب|كتب|مقال|مقالات|دورية|دوريات|قاعدة بيانات|قواعد البيانات|فهرس|استعارة|إعارة|تجديد|غرامة|ساعات العمل|موقع المكتبة|أمين مكتبة|مصادر|المصادر)",
    ]

    @staticmethod
    def _matches_any(query: str, patterns: List[str]) -> bool:
        return any(re.search(pattern, query or "", re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _is_library_related(query: str) -> bool:
        return IntentRouterService._matches_any(query, IntentRouterService.LIBRARY_RELATED_PATTERNS)

    @staticmethod
    def classify(query: str, query_info: Dict[str, Any], requested_mode: str = "all") -> Dict[str, Any]:
        policy_topic = LibraryPolicyAnswerService.detect_policy_topic(query)
        if policy_topic:
            return {
                "intent": policy_topic,
                "confidence": 0.95,
                "policy_topic": policy_topic,
                "search_mode": None,
            }

        for mode, patterns in IntentRouterService.SEARCH_INTENT_PATTERNS.items():
            if IntentRouterService._matches_any(query, patterns):
                return {
                    "intent": f"{mode}_search",
                    "confidence": 0.9,
                    "policy_topic": None,
                    "search_mode": mode,
                }

        if IntentRouterService._matches_any(query, IntentRouterService.GENERAL_AI_PATTERNS):
            return {
                "intent": "general_ai",
                "confidence": 0.8,
                "policy_topic": None,
                "search_mode": None,
            }

        format_preference = query_info.get("format_preference")
        if format_preference == "article":
            return {
                "intent": "research_search",
                "confidence": 0.85,
                "policy_topic": None,
                "search_mode": "research",
            }
        if format_preference in {"ebook", "print", "audiobook"}:
            return {
                "intent": "books_search",
                "confidence": 0.85,
                "policy_topic": None,
                "search_mode": "books",
            }

        return {
            "intent": "topic_search",
            "confidence": 0.6,
            "policy_topic": None,
            "search_mode": requested_mode if requested_mode in {"books", "research", "all"} else "all",
        }

    @staticmethod
    def resolve_search_mode(requested_mode: str, route: Dict[str, Any]) -> str:
        routed_mode = route.get("search_mode")
        if routed_mode in {"books", "research", "all"}:
            return routed_mode
        if requested_mode in {"books", "research", "all"}:
            return requested_mode
        return "all"

    @staticmethod
    async def build_general_ai_response(user_query: str, is_arabic: bool = False) -> Dict[str, Any]:
        if not GEMINI_MODEL:
            answer = (
                "Gemini is not configured right now. I can still assist with UAEU Library services, "
                "including books, articles, databases, library hours, borrowing, and LibChat."
            )
            return {
                "answer": answer,
                "suggestions": ["Library hours", "Search for books", "List of databases"],
            }

        if not IntentRouterService._is_library_related(user_query):
            if is_arabic:
                return {
                    "answer": "يمكنني مساعدتك في الأسئلة المتعلقة بمكتبات جامعة الإمارات، مثل البحث عن الكتب والمقالات، قواعد البيانات، الإعارة، ساعات العمل، وحساب المكتبة. يرجى إعادة صياغة السؤال ضمن خدمات المكتبة.",
                    "suggestions": ["ساعات عمل المكتبة", "البحث عن كتب", "قائمة قواعد البيانات"],
                }
            return {
                "answer": "I can assist with UAEU Library-related questions, including books, articles, databases, borrowing, library hours, and library accounts. Please rephrase your question within library services.",
                "suggestions": ["Library hours", "Search for books", "List of databases"],
            }

        language = "Arabic" if is_arabic else "English"
        safe_query = safe_llm_field(user_query, MAX_QUERY_LENGTH)
        prompt = f"""You are a formal UAEU Libraries staff assistant.

Answer ONLY library-related questions in {language}.
Use a formal, clear, library-staff tone.
Do not invent UAEU Library rules, fees, hours, links, or policies.
If the question requires official policy details, advise the user to check the official UAEU Libraries page or ask through LibChat.
If the user asks outside the library scope, politely state that you can assist only with UAEU Library services.

USER QUESTION:
{safe_query}

Return ONLY JSON:
{{
  "answer": "concise formal answer in {language}",
  "suggestions": ["short library-related next action 1", "short library-related next action 2", "short library-related next action 3"]
}}"""
        try:
            result = await asyncio.wait_for(
                generate_gemini_content(prompt, temperature=0.3, max_output_tokens=600),
                timeout=8.0,
            )
            if not result or not result.text:
                raise ValueError("Gemini returned an empty response")
            data = parse_llm_json(result.text)
            answer = str(data.get("answer", "")).strip()
            suggestions = data.get("suggestions", [])
            if not isinstance(suggestions, list):
                suggestions = []
            suggestions = [str(item).strip() for item in suggestions[:4] if str(item).strip()]
            if not answer:
                raise ValueError("Gemini JSON did not include an answer")
            return {
                "answer": answer,
                "suggestions": suggestions or ["Library hours", "Search for books", "List of databases"],
            }
        except Exception as e:
            logger.warning(f"General AI intent fallback: {e}")
            return {
                "answer": (
                    "I could not generate a library-service answer right now. I can still help you search UAEU Library "
                    "books, articles, databases, or connect you with a UAEU Libraries staff member."
                ),
                "suggestions": ["Library hours", "Search for books", "List of databases"],
            }

# ============================
# 10B. INTERACTIVE AGENT LAYER
# ============================
class InteractiveAgentService:
    """Small deterministic agent layer for conversational UX.

    It asks a focused follow-up when the user intent is too vague for a useful
    catalog/API search. This keeps the assistant interactive without spending
    an extra Gemini call just to classify greetings or incomplete searches.
    """

    GENERIC_TOPICS_EN = {
        "", "book", "books", "ebook", "ebooks", "article", "articles", "journal",
        "journals", "paper", "papers", "research", "source", "sources", "resource",
        "resources", "database", "databases", "catalog", "library", "help", "search",
        "find", "finding", "locate", "how", "use", "using", "more", "result", "results",
        "recent", "latest",
    }
    GENERIC_TOPICS_AR = {
        "", "كتاب", "كتب", "مقال", "مقالات", "بحث", "أبحاث", "دورية", "دوريات",
        "مصدر", "مصادر", "مورد", "موارد", "قاعدة", "قواعد", "المكتبة", "ساعدني",
        "ابحث", "بحث", "المزيد", "نتائج", "حديثة", "أحدث",
    }
    GREETING_PATTERNS_EN = [
        r"^(hi|hello|hey|good morning|good afternoon|good evening)\W*$",
        r"^(help|start|menu)\W*$",
        r"\bwhat can you do\b",
        r"\bhow can you help\b",
    ]
    GREETING_PATTERNS_AR = [
        r"^(مرحبا|مرحباً|اهلا|أهلا|السلام عليكم)\W*$",
        r"^(ساعدني|مساعدة|ابدأ|القائمة)\W*$",
        r"ماذا تستطيع",
        r"كيف تساعد",
    ]

    @staticmethod
    def _clean_topic(topic: str) -> str:
        return re.sub(r"[^\w\u0600-\u06FF]+", " ", topic or "").strip().lower()

    @staticmethod
    def _is_greeting_or_help(query: str, is_arabic: bool) -> bool:
        patterns = InteractiveAgentService.GREETING_PATTERNS_AR if is_arabic else InteractiveAgentService.GREETING_PATTERNS_EN
        return any(re.search(pattern, query or "", re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _is_search_guidance_request(query: str, query_info: Dict[str, Any], is_arabic: bool) -> bool:
        """Detect requests for help starting a search, not actual search topics."""
        if is_arabic:
            matched = bool(re.search(
                r"(ساعد|مساعدة|كيف).{0,30}(ابحث|أبحث|العثور|إيجاد|اجد).{0,30}"
                r"(كتاب|كتب|مقال|مقالات|دورية|دوريات|مصدر|مصادر|قاعدة|قواعد)",
                query or "",
                re.IGNORECASE,
            ))
        else:
            q = re.sub(r"\s+", " ", (query or "").strip().lower())
            matched = q == "check spelling" or bool(re.search(
                r"\b(help|assist|guide|how do i|can you|could you|i need help)\b.{0,40}"
                r"\b(find|finding|search|look for|locate|use)\b.{0,40}"
                r"\b(books?|ebooks?|articles?|journals?|papers?|resources?|databases?|catalog)\b",
                q,
                re.IGNORECASE,
            )) or bool(re.fullmatch(
                r"(search|find|look for|show me|get)\s+(?:for\s+)?(books?|ebooks?|articles?|journals?|papers?|resources?|databases?)",
                q,
                re.IGNORECASE,
            ))

        if not matched:
            return False

        topic = InteractiveAgentService._clean_topic(query_info.get("core_topic", ""))
        original = InteractiveAgentService._clean_topic(query)
        if not topic or topic == original:
            return True
        generic = InteractiveAgentService.GENERIC_TOPICS_AR if is_arabic else InteractiveAgentService.GENERIC_TOPICS_EN
        words = topic.split()
        return topic in generic or all(word in generic for word in words)

    @staticmethod
    def _topic_is_too_vague(query_info: Dict[str, Any]) -> bool:
        topic = InteractiveAgentService._clean_topic(query_info.get("core_topic", ""))
        if re.fullmatch(r"10\.\d{4,9}/\S+", topic, re.IGNORECASE):
            return False
        if re.fullmatch(r"(?:97[89])?\d{9,13}[xX]?", topic):
            return False
        if re.search(r"[a-zA-Z]\s+[a-zA-Z]", topic) and topic not in InteractiveAgentService.GENERIC_TOPICS_EN:
            return False
        generic = InteractiveAgentService.GENERIC_TOPICS_AR if query_info.get("is_arabic") else InteractiveAgentService.GENERIC_TOPICS_EN
        words = topic.split()
        return topic in generic or (len(words) <= 2 and all(word in generic for word in words))

    @staticmethod
    def _menu_response(is_arabic: bool) -> Tuple[str, List[str]]:
        if is_arabic:
            return (
                "أكيد. كيف يمكنني مساعدتك اليوم؟\n\n"
                "يمكنني العمل كمرشد تفاعلي لمساعدتك في:\n"
                "- البحث عن كتب أو كتب إلكترونية\n"
                "- إيجاد مقالات ودوريات علمية\n"
                "- اقتراح قواعد بيانات مناسبة لموضوعك\n"
                "- الإجابة عن خدمات المكتبة مثل الإعارة، ساعات العمل، المواقع، والنماذج\n\n"
                "اختر أحد الخيارات أو اكتب سؤالك مباشرة.",
                ["ساعات عمل المكتبة", "كم كتاب أقدر أستعير؟", "أريد مقالات عن الذكاء الاصطناعي", "أريد كتب عن الإدارة"],
            )
        return (
            "Sure. How can I help you today?\n\n"
            "I can act as an interactive library guide for:\n"
            "- Finding books or eBooks\n"
            "- Finding scholarly articles and journals\n"
            "- Recommending UAEU databases for your topic\n"
            "- Answering library service questions such as borrowing, hours, locations, and forms\n\n"
            "Choose an option or type your question.",
            ["Library hours", "How many books can I borrow?", "Find articles about AI", "Find books about management"],
        )

    @staticmethod
    def _clarify_search_response(search_mode: str, is_arabic: bool) -> Tuple[str, List[str]]:
        if is_arabic:
            mode_hint = "الكتب" if search_mode == "books" else "المقالات والدوريات" if search_mode == "research" else "الموارد"
            return (
                f"تمام، أستطيع البحث عن {mode_hint}. ما الموضوع أو العنوان أو اسم المؤلف الذي تريد البحث عنه؟\n\n"
                "يمكنك أيضاً كتابة ISBN أو DOI إذا كان لديك.",
                ["كتب عن الذكاء الاصطناعي", "مقالات عن الأمن السيبراني", "كتب للمؤلف نجيب محفوظ", "ساعات عمل المكتبة"],
            )
        mode_hint = "books" if search_mode == "books" else "articles and journals" if search_mode == "research" else "library resources"
        return (
            f"Sure, I can search for {mode_hint}. What topic, title, author, ISBN, or DOI should I use?\n\n"
            "A little more detail will help me return better results.",
            ["Books about artificial intelligence", "Articles about cybersecurity", "Books by Stephen Hawking", "Library hours"],
        )

    @staticmethod
    def _more_without_topic_response(is_arabic: bool) -> Tuple[str, List[str]]:
        if is_arabic:
            return (
                "أقدر أعطيك المزيد، لكن أحتاج أعرف المزيد عن أي موضوع. اكتب الموضوع أو اختر مثالاً.",
                ["مقالات أحدث عن الذكاء الاصطناعي", "المزيد من الكتب عن الإدارة", "أبحاث عن الطاقة المتجددة"],
            )
        return (
            "I can show more results, but I need to know the topic first. Type the topic or choose an example.",
            ["More recent articles about AI", "More books about management", "Research on renewable energy"],
        )

    @staticmethod
    def maybe_respond(
        query: str,
        query_info: Dict[str, Any],
        history: List[Dict],
        search_mode: str,
    ) -> Optional[Dict[str, Any]]:
        is_arabic = query_info.get("is_arabic", False)
        if InteractiveAgentService._is_greeting_or_help(query, is_arabic):
            answer, suggestions = InteractiveAgentService._menu_response(is_arabic)
        elif QueryPreparationService.is_more_results_request(query) and not QueryPreparationService.find_previous_content_query(history):
            answer, suggestions = InteractiveAgentService._more_without_topic_response(is_arabic)
        elif InteractiveAgentService._is_search_guidance_request(query, query_info, is_arabic):
            answer, suggestions = InteractiveAgentService._clarify_search_response(search_mode, is_arabic)
        elif InteractiveAgentService._topic_is_too_vague(query_info):
            answer, suggestions = InteractiveAgentService._clarify_search_response(search_mode, is_arabic)
        else:
            return None

        return {
            "answer": answer,
            "suggestions": suggestions,
        }


# ============================
# 11. BOOK PROCESSING
# ============================
def detect_format_type(format_string: str, is_arabic: bool = False) -> str:
    format_lower = format_string.lower()
    format_mappings = {
        'ebook': {'keywords': ['ebook', 'e-book', 'digital', 'electronic', 'online resource'],
                  'en': 'eBook (Digital)', 'ar': 'كتاب إلكتروني'},
        'print_book': {'keywords': ['print', 'book', 'printbook', 'hardcover', 'paperback', 'كتاب مطبوع'],
                       'en': 'Print Book', 'ar': 'كتاب مطبوع'},
        'article': {'keywords': ['article', 'journal', 'periodical', 'academic journal', 'scholarly'],
                    'en': 'Journal Article', 'ar': 'مقالة علمية'},
        'audiobook': {'keywords': ['audiobook', 'audio book', 'audio', 'spoken word', 'cd'],
                      'en': 'Audiobook', 'ar': 'كتاب صوتي'},
        'video': {'keywords': ['video', 'dvd', 'blu-ray', 'streaming video', 'film'],
                  'en': 'Video', 'ar': 'فيديو'},
        'thesis': {'keywords': ['thesis', 'dissertation', 'doctoral', 'masters'],
                   'en': 'Thesis/Dissertation', 'ar': 'رسالة علمية'},
        'magazine': {'keywords': ['magazine', 'news', 'newspaper'],
                     'en': 'Magazine/News', 'ar': 'مجلة'}
    }
    for format_type, config in format_mappings.items():
        if any(kw in format_lower for kw in config['keywords']):
            return config['ar'] if is_arabic else config['en']
    if format_string and format_string != "Unknown":
        return format_string
    return 'غير محدد' if is_arabic else 'Unknown'


def is_digital_format(format_string: str) -> bool:
    digital_keywords = ['ebook', 'e-book', 'digital', 'electronic', 'online', 'streaming', 'كتاب إلكتروني']
    return any(kw in format_string.lower() for kw in digital_keywords)


async def process_selected_books(raw_books: List[Dict], analysis: Dict, is_arabic: bool = False) -> List[Book]:
    recommendations = {
        r.get("index"): r
        for r in analysis.get("recommendations", [])
        if isinstance(r.get("index"), int)
    }
    selected_indices = analysis.get("selected_indices", [])
    selected_rows = []
    for idx in selected_indices:
        if not isinstance(idx, int) or idx < 0 or idx >= len(raw_books):
            continue
        selected_rows.append((idx, raw_books[idx], recommendations.get(idx, {})))

    def needs_detail_lookup(raw: Dict[str, Any]) -> bool:
        raw_format = raw.get("specificFormat") or raw.get("generalFormat") or ""
        return bool(raw.get("oclcNumber")) and not is_digital_format(raw_format)

    # Digital items already have an online access path; detailed-holdings calls
    # mostly add latency and can return generic placeholders like Location: UAE.
    detail_tasks = [
        OCLCDiscoveryService.get_book_details(raw.get("oclcNumber", ""))
        if needs_detail_lookup(raw) else asyncio.sleep(0, result={})
        for _, raw, _ in selected_rows
    ]
    details_list = await asyncio.gather(*detail_tasks, return_exceptions=True) if detail_tasks else []

    books = []
    for (idx, raw, rec), details in zip(selected_rows, details_list):
        if isinstance(details, Exception):
            logger.warning(f"Book detail lookup failed for selected index {idx}: {details}")
            details = {}
        oclc = raw.get("oclcNumber", "")
        raw_format = raw.get("specificFormat") or raw.get("generalFormat") or "Unknown"
        clean_format = detect_format_type(raw_format, is_arabic)
        is_digital = is_digital_format(raw_format)
        availability_status = OCLCDiscoveryService._normalize_availability(
            details.get("availability_status", ""), is_digital=is_digital)
        branch_location = (
            OCLCDiscoveryService._clean_patron_location(details.get("branch_location"))
            or ("Online resource" if is_digital else None)
        )
        shelf_location = OCLCDiscoveryService._clean_patron_location(details.get("shelf_location"))

        books.append(Book(
            title=raw.get("title", "Untitled")[:200],
            author=raw.get("creator", "Unknown Author")[:100],
            format=clean_format,
            call_number=None if is_digital else details.get("call_number"),
            availability_status=availability_status,
            branch_location=branch_location,
            shelf_location=shelf_location,
            year=raw.get("date", "N/A")[:10],
            link=f"https://uaeu.on.worldcat.org/search?queryString={oclc}" if oclc else "",
            oclc_number=oclc,
            relevance_score=rec.get("relevance_score"),
            why_recommended=rec.get("why_recommended")
        ))
    return books


def process_selected_articles(articles: List[Dict]) -> List[ArticleOut]:
    payload = []
    for article in articles or []:
        payload.append(ArticleOut(
            title=str(article.get("title", "Unknown"))[:220],
            authors=str(article.get("authors", "Unknown"))[:180],
            year=str(article.get("year", ""))[:10],
            journal=str(article.get("journal", ""))[:140],
            database=str(article.get("database", ""))[:80],
            doi=str(article.get("doi", ""))[:120],
            link=str(article.get("link", ""))[:500],
            direct_link=str(article.get("direct_link", ""))[:500],
            open_access=bool(article.get("open_access", False)),
            relevance_score=article.get("relevance_score"),
            why_recommended=str(article.get("why_recommended", ""))[:500],
        ))
    return payload


def filter_articles_by_user_filters(articles: List[Dict], filters: SearchFilters = None) -> List[Dict]:
    if not filters or not articles:
        return articles

    filtered = []
    has_year_filter = bool(filters.year_from or filters.year_to)
    for article in articles:
        if filters.open_access_only and not article.get("open_access"):
            continue

        if has_year_filter:
            year_text = str(article.get("year", ""))[:4]
            if not year_text.isdigit():
                continue
            year = int(year_text)
            if filters.year_from and year < filters.year_from:
                continue
            if filters.year_to and year > filters.year_to:
                continue

        filtered.append(article)
    return filtered


# ============================
# 12. RATE LIMITING & APP SETUP
# ============================
limiter = Limiter(key_func=get_remote_address, storage_uri=RATE_LIMIT_STORAGE_URI)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global HTTP_CLIENT
    logger.info("Starting UAEU Libraries AI Assistant %s (%s)", APP_VERSION, ENVIRONMENT)
    if ENVIRONMENT == "production" and RATE_LIMIT_STORAGE_URI == "memory://":
        logger.warning(
            "RATE_LIMIT_STORAGE_URI uses process memory; run one worker or configure shared storage"
        )
    HTTP_CLIENT = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        limits=HTTP_LIMITS,
    )
    app.state.http_client = HTTP_CLIENT
    try:
        yield
    finally:
        if HTTP_CLIENT and not HTTP_CLIENT.is_closed:
            await HTTP_CLIENT.aclose()
        HTTP_CLIENT = None
        logger.info("Shutting down")


app = FastAPI(
    title="UAEU Libraries AI Assistant",
    description="Independent bilingual library-services and academic-resource discovery prototype",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if ENVIRONMENT == "development" else None,
    redoc_url="/redoc" if ENVIRONMENT == "development" else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ENVIRONMENT == "production" else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type", "Authorization", "X-Session-ID", CSRF_HEADER_NAME,
        "X-Admin-Key", "X-Request-ID",
    ],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

if ENVIRONMENT == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            detail="An unexpected error occurred. Please try again.",
            timestamp=datetime.now(timezone.utc).isoformat(),
            suggestions=["Try a simpler search", "Check your internet connection"]
        ).model_dump()
    )


# ============================
# 13. API ENDPOINTS
# ============================
async def _combined_cache_size() -> int:
    return await search_cache.size() + await article_cache.size()


def _configured_services() -> Dict[str, bool]:
    """Report configured capabilities without claiming live provider availability."""
    return {
        "oclc": bool(OCLC_CLIENT_ID and OCLC_CLIENT_SECRET),
        "gemini": GEMINI_MODEL is not None,
        "semantic_scholar": True,
        "pubmed": True,
        "europe_pmc": True,
        "core": bool(CORE_API_KEY),
        "doaj": True,
        "crossref": True,
        "openalex": True,
    }


async def _probe_json_service(url: str, params: Optional[Dict[str, Any]] = None) -> bool:
    """Make a small, bounded readiness request without logging response content."""
    try:
        response = await _http_get(url, params=params, timeout=min(HTTP_TIMEOUT, 5.0))
        return 200 <= response.status_code < 400
    except (httpx.HTTPError, RuntimeError):
        return False


async def _dependency_readiness() -> Dict[str, bool]:
    checks = await asyncio.gather(
        _probe_json_service(
            ArticleSearchService.CROSSREF_API,
            {"query": "library", "rows": 1},
        ),
        _probe_json_service(
            ArticleSearchService.OPENALEX_API,
            {"search": "library", "per_page": 1},
        ),
        _probe_json_service(
            ArticleSearchService.PUBMED_SEARCH,
            {"db": "pubmed", "term": "library", "retmax": 1, "retmode": "json"},
        ),
        _probe_json_service(
            ArticleSearchService.EPMC_API,
            {"query": "library", "format": "json", "pageSize": 1},
        ),
        _probe_json_service(
            f"{ArticleSearchService.DOAJ_API}/library",
            {"page": 1, "pageSize": 1},
        ),
        return_exceptions=False,
    )

    services = _configured_services()
    services.update({
        "crossref": checks[0],
        "openalex": checks[1],
        "pubmed": checks[2],
        "europe_pmc": checks[3],
        "doaj": checks[4],
    })
    return services


@app.get("/", response_model=HealthResponse)
async def root():
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        services=_configured_services(),
        cache_size=await _combined_cache_size()
    )


@app.post("/session", response_model=SessionResponse)
@limiter.limit("30/minute")
async def issue_session(request: Request):
    return create_signed_session()


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Lightweight health check that does not call external services."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        services=_configured_services(),
        cache_size=await _combined_cache_size()
    )


@app.get("/ready", response_model=HealthResponse)
async def readiness_check(request: Request):
    require_admin_key(request)

    oclc_ok = False
    try:
        oclc_ok = await token_manager.get_token() is not None
    except Exception:
        pass

    services = await _dependency_readiness()
    services["oclc"] = oclc_ok
    required_services = ("oclc", "gemini", "crossref", "openalex")

    return HealthResponse(
        status="healthy" if all(services[name] for name in required_services) else "degraded",
        timestamp=datetime.now(timezone.utc).isoformat(),
        services=services,
        cache_size=await _combined_cache_size()
    )


@app.post("/ai-search", response_model=AISearchResponse, response_model_exclude_none=True)
@limiter.limit(RATE_LIMIT_REQUESTS)
async def ai_search(request: Request, search_request: AISearchRequest):
    try:
        user_query = search_request.query
        limit = search_request.limit
        signed_session = search_request.session_id or ""
        session_key = verify_signed_session(
            signed_session,
            request.headers.get(CSRF_HEADER_NAME, ""),
        )
        filters = search_request.filters
        search_mode = search_request.search_mode or "all"

        history = await conversation_memory.get_history(session_key)
        query_info = QueryPreparationService.prepare_query(user_query, history)
        routing_query = query_info.get("normalized_query") or user_query
        format_preference = query_info.get("format_preference")
        if query_info.get("wants_all_resources"):
            search_mode = "all"
        elif search_mode == "books" and format_preference == "article":
            search_mode = "research"
        elif search_mode == "research" and format_preference in {"ebook", "print", "audiobook"}:
            search_mode = "books"

        route = IntentRouterService.classify(routing_query, query_info, search_mode)
        search_mode = IntentRouterService.resolve_search_mode(search_mode, route)

        if route.get("intent") == "general_ai":
            direct = await IntentRouterService.build_general_ai_response(
                routing_query, query_info.get("is_arabic", False))
            await conversation_memory.add_exchange(session_key, user_query, [], direct["answer"])
            return AISearchResponse(
                ai_response=direct["answer"], books=[], articles=[], query_used=user_query,
                total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=direct.get("suggestions", []))

        policy_topic = route.get("policy_topic")
        if not policy_topic:
            policy_topic = await LibraryPolicyAnswerService.classify_policy_topic(
                routing_query, query_info.get("is_arabic", False))
        policy_answer = await LibraryPolicyAnswerService.answer_for_topic(
            routing_query, policy_topic, query_info.get("is_arabic", False))
        if policy_answer:
            suggestions = (["تجديد الإعارة", "غرامات التأخير", "الوصول للمصادر الإلكترونية"]
                           if query_info.get("is_arabic", False)
                           else ["How do I renew books?", "What are overdue fines?", "How do I access e-resources remotely?"])
            await conversation_memory.add_exchange(session_key, user_query, [], policy_answer)
            return AISearchResponse(
                ai_response=policy_answer, books=[], articles=[], query_used=user_query,
                total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=suggestions)

        agent_response = InteractiveAgentService.maybe_respond(
            routing_query, query_info, history, search_mode)
        if agent_response:
            return AISearchResponse(
                ai_response=agent_response["answer"], books=[], articles=[], query_used=user_query,
                total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=agent_response["suggestions"])

        is_more_results_request = QueryPreparationService.is_more_results_request(routing_query)
        if is_more_results_request and history:
            previous_query = QueryPreparationService.find_previous_content_query(history)
            if previous_query:
                query_info = QueryPreparationService.prepare_query(previous_query, history[:-1])
                if QueryPreparationService.is_recent_followup_request(routing_query):
                    current_year = datetime.now().year
                    query_info["wants_new_books"] = True
                    query_info["year_from"] = current_year - 5
                    query_info["year_to"] = current_year
            limit = min(limit + 4, 10)

        if not filters:
            filters = SearchFilters()
        if query_info.get("wants_new_books") and query_info.get("year_from"):
            if not filters.year_from:
                filters.year_from = query_info["year_from"]
        if query_info.get("format_preference"):
            if not filters.format or filters.format == "any":
                format_map = {'ebook': 'ebook', 'print': 'print', 'article': 'article', 'audiobook': 'audiobook'}
                filters.format = format_map.get(query_info["format_preference"], filters.format)

        search_plan = SearchPlannerService.build(routing_query, query_info, filters, search_mode)
        query_info["search_plan"] = search_plan
        search_query = search_plan.get("catalog_query") or query_info.get("search_query", user_query)
        core_topic = search_plan.get("article_query") or query_info.get("core_topic", user_query)
        database_topic = search_plan.get("database_topic") or query_info.get("core_topic", user_query)
        year_from = filters.year_from or search_plan.get("year_from") or query_info.get("year_from")
        is_arabic = query_info.get("is_arabic", False)

        logger.info(
            f"Search: '{user_query[:50]}' | Topic: '{query_info.get('core_topic', '')[:30]}' | "
            f"Year: {filters.year_from}-{filters.year_to} | Format: {filters.format} | "
            f"OpenAccess: {filters.open_access_only} | Mode: {search_mode} | Plan: {search_plan.get('search_type')}")

        articles = []
        raw_books = []

        def _log_parallel_error(label: str, exc: BaseException) -> None:
            if isinstance(exc, HTTPException):
                logger.warning(f"{label} failed: {exc.detail}")
                return
            logger.error(
                f"{label} failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        async def search_books_and_articles(article_limit: int) -> Tuple[List[Dict], Dict[str, Any]]:
            book_result, article_result = await asyncio.gather(
                OCLCDiscoveryService.search_books(search_query, SEARCH_LIMIT, filters),
                ArticleSearchService.search_all(
                    core_topic,
                    limit=article_limit,
                    year_from=year_from,
                    token_manager=token_manager,
                ),
                return_exceptions=True,
            )

            local_books: List[Dict] = []
            if isinstance(book_result, BaseException):
                _log_parallel_error("OCLC book search", book_result)
            else:
                local_books = book_result

            local_article_results: Dict[str, Any] = {"articles": [], "sources_searched": []}
            if isinstance(article_result, BaseException):
                _log_parallel_error("Article search", article_result)
            else:
                local_article_results = article_result

            return local_books, local_article_results

        if search_mode == "books":
            raw_books = await OCLCDiscoveryService.search_books(search_query, SEARCH_LIMIT, filters)

        elif search_mode == "research":
            if search_request.include_related_books:
                raw_books, article_results = await search_books_and_articles(12)
            else:
                article_results = await ArticleSearchService.search_all(
                    core_topic,
                    limit=12,
                    year_from=year_from,
                    token_manager=token_manager,
                )
                raw_books = []
            articles = filter_articles_by_user_filters(article_results.get("articles", []), filters)
            logger.info(
                f"Research mode: {len(articles)} articles from {article_results.get('sources_searched', [])}, "
                f"{len(raw_books)} related catalog items")

        else:  # "all" mode
            raw_books, article_results = await search_books_and_articles(10)
            articles = filter_articles_by_user_filters(article_results.get("articles", []), filters)
            logger.info(f"All mode: {len(articles)} articles, {len(raw_books)} books")

        # Broader author search fallback
        if search_mode != "research" and query_info["search_type"] == "author" and len(raw_books) < 5:
            author_name = query_info.get("author_name", "")
            if author_name:
                additional = await OCLCDiscoveryService.search_books(author_name, SEARCH_LIMIT, filters)
                seen = {b.get("oclcNumber") for b in raw_books}
                raw_books.extend([b for b in additional if b.get("oclcNumber") not in seen])

        total_found = len(raw_books) + len(articles)

        # Handle no results
        if search_mode == "books" and not raw_books:
            no_results_suggestions = (["البحث عن مقالات", "فتح فهرس المكتبة", "قائمة قواعد البيانات"]
                                      if is_arabic else ["Search for articles", "Open catalog search", "List of databases"])
            return AISearchResponse(
                ai_response=await ResponseGeneratorService._generate_no_results_response(query_info, search_mode),
                books=[], query_used=search_query, total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=no_results_suggestions)

        if search_mode == "research" and not articles and not raw_books:
            no_results_suggestions = (["البحث عن كتب", "فتح فهرس المكتبة", "قائمة قواعد البيانات"]
                                      if is_arabic else ["Search for books", "Open catalog search", "List of databases"])
            return AISearchResponse(
                ai_response=await ResponseGeneratorService._generate_no_results_response(query_info, search_mode),
                books=[], query_used=search_query, total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=no_results_suggestions)

        if search_mode == "all" and not raw_books and not articles:
            no_results_suggestions = (["فتح فهرس المكتبة", "قائمة قواعد البيانات", "ساعات عمل المكتبة"]
                                      if is_arabic else ["Open catalog search", "List of databases", "Library hours"])
            return AISearchResponse(
                ai_response=await ResponseGeneratorService._generate_no_results_response(query_info, search_mode),
                books=[], query_used=search_query, total_found=0, total_analyzed=0,
                filters_applied=filters.model_dump() if filters else None,
                session_id=signed_session, suggestions=no_results_suggestions)

        # ── AI analysis: BOOKS ──
        analysis = {}
        selected_books = []
        if raw_books:
            analysis = await AIAnalysisService.analyze_and_rank_books(
                user_query, raw_books, query_info, limit, history)
            selected_books = await process_selected_books(raw_books, analysis, is_arabic)

        # ── AI analysis: ARTICLES (★ NEW – AI picks the best ones) ──
        curated_articles = []
        if articles:
            article_limit = 8 if search_mode == "research" else 6
            article_analysis = await AIAnalysisService.analyze_and_rank_articles(
                user_query, articles, query_info, limit=article_limit)
            curated_articles = article_analysis.get("selected", [])
            logger.info(f"AI curated {len(curated_articles)} articles from {len(articles)} raw results")

        suggested_databases = []
        if search_mode in ["research", "all"]:
            db_candidates = ArticleSearchService.recommend_uaeu_databases(database_topic, limit=5)
            for article in curated_articles:
                db_candidates.extend(article.get("suggested_databases") or [])
                if article.get("database"):
                    db_candidates.append({
                        "db": article.get("database", ""),
                        "url": article.get("database_url", ""),
                        "search": article.get("database_search", ""),
                    })
            suggested_databases = ArticleSearchService._dedupe_db_entries(db_candidates)[:5]

        # Generate response (pass curated articles, not raw)
        ai_response = await ResponseGeneratorService.generate_research_response(
            user_query, selected_books, analysis, query_info,
            api_articles=curated_articles,
            suggested_databases=suggested_databases,
            search_mode=search_mode)
        article_payload = process_selected_articles(curated_articles)

        # Store in memory
        await conversation_memory.add_exchange(
            session_key, user_query, [b.model_dump() for b in selected_books], ai_response)

        # Suggestions
        suggestions = analysis.get("follow_up_suggestions", []) if analysis else []
        if search_mode == "books":
            extra = "المزيد من الكتب" if is_arabic else "Give me more books"
        elif search_mode == "research":
            extra = "مقالات أحدث" if is_arabic else "More recent articles"
        else:
            extra = "المزيد من النتائج" if is_arabic else "More results"
        if extra not in suggestions:
            suggestions.append(extra)
        deduped_suggestions = []
        seen_suggestions = set()
        for suggestion in suggestions:
            if not suggestion:
                continue
            key = suggestion.strip().lower()
            if key in seen_suggestions:
                continue
            seen_suggestions.add(key)
            deduped_suggestions.append(suggestion)
        suggestions = deduped_suggestions

        return AISearchResponse(
            ai_response=ai_response, books=selected_books, articles=article_payload,
            query_used=search_query,
            total_found=total_found, total_analyzed=min(25, total_found),
            filters_applied=filters.model_dump() if filters else None,
            session_id=signed_session, suggestions=suggestions)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Search error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="An error occurred. Please try again.")


@app.post("/clear-cache")
async def clear_cache(request: Request):
    """Clear the search cache (admin endpoint - key via header only)"""
    require_admin_key(request)
    await search_cache.clear()
    await article_cache.clear()
    logger.info("Search and article caches cleared by admin")
    return {"status": "Cache cleared", "timestamp": datetime.now(timezone.utc).isoformat()}


def _apply_security_headers(response, request_id: str) -> None:
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'none'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'none'"
    )
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "")[:64]
    request_id = supplied_request_id if re.fullmatch(r"[A-Za-z0-9._-]{8,64}", supplied_request_id) else uuid4().hex
    started_at = time.perf_counter()

    content_length = request.headers.get("content-length")
    response = None
    if content_length:
        try:
            body_size = int(content_length)
        except ValueError:
            response = JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        else:
            if body_size < 0:
                response = JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
            elif body_size > 1_048_576:
                response = JSONResponse(status_code=413, content={"detail": "Request body too large"})

    if response is None:
        response = await call_next(request)

    _apply_security_headers(response, request_id)
    duration_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request_complete method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
    )
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=ENVIRONMENT == "development",
        log_level="info",
    )
