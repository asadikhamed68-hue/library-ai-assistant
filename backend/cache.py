# Copyright (c) 2026 Asadik Hamed. All rights reserved. See LICENSE.

import asyncio
import copy
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    from backend.config import (
        ARTICLE_CACHE_MAX_SIZE,
        ARTICLE_CACHE_TTL_SECONDS,
        CACHE_TTL_SECONDS,
        CONVERSATION_TTL_SECONDS,
        MAX_CACHE_SIZE,
        MAX_CONVERSATION_HISTORY,
        MAX_CONVERSATIONS,
        MAX_QUERY_LENGTH,
    )
except ImportError:
    from config import (
        ARTICLE_CACHE_MAX_SIZE,
        ARTICLE_CACHE_TTL_SECONDS,
        CACHE_TTL_SECONDS,
        CONVERSATION_TTL_SECONDS,
        MAX_CACHE_SIZE,
        MAX_CONVERSATION_HISTORY,
        MAX_CONVERSATIONS,
        MAX_QUERY_LENGTH,
    )

logger = logging.getLogger("uaeu-library-ai")


class SearchCache:
    def __init__(self, max_size: int = MAX_CACHE_SIZE, ttl: int = CACHE_TTL_SECONDS):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._max_size = max_size
        self._ttl = ttl
        self._lock = asyncio.Lock()

    def _make_key(self, query: str, filters: Dict[str, Any] = None) -> str:
        key_data = f"{query.lower().strip()}|{json.dumps(filters or {}, sort_keys=True)}"
        return hashlib.sha256(key_data.encode()).hexdigest()

    async def get(self, query: str, filters: Dict[str, Any] = None) -> Optional[Any]:
        async with self._lock:
            key = self._make_key(query, filters)
            if key in self._cache:
                entry = self._cache[key]
                if time.time() - entry["timestamp"] < self._ttl:
                    logger.info(f"Cache hit for query: {query[:30]}...")
                    return copy.deepcopy(entry["data"])
                del self._cache[key]
            return None

    async def set(self, query: str, data: Any, filters: Dict[str, Any] = None):
        async with self._lock:
            if len(self._cache) >= self._max_size:
                oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["timestamp"])
                del self._cache[oldest_key]
            key = self._make_key(query, filters)
            self._cache[key] = {"data": copy.deepcopy(data), "timestamp": time.time()}
            logger.info(f"Cached results for query: {query[:30]}...")

    async def clear(self):
        async with self._lock:
            self._cache.clear()

    async def size(self) -> int:
        async with self._lock:
            return len(self._cache)


class ConversationMemory:
    def __init__(self, max_history: int = MAX_CONVERSATION_HISTORY,
                 ttl: int = CONVERSATION_TTL_SECONDS,
                 max_sessions: int = MAX_CONVERSATIONS):
        self._conversations: Dict[str, Dict[str, Any]] = {}
        self._max_history = max_history
        self._ttl = ttl
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()

    async def get_history(self, session_id: str) -> List[Dict[str, Any]]:
        async with self._lock:
            self._cleanup_expired()
            if session_id in self._conversations:
                return copy.deepcopy(self._conversations[session_id]["history"])
            return []

    async def add_exchange(self, session_id: str, user_query: str,
                           books: List[Dict], ai_response: str):
        async with self._lock:
            self._cleanup_expired()
            if session_id not in self._conversations and len(self._conversations) >= self._max_sessions:
                oldest = min(self._conversations, key=lambda k: self._conversations[k]["last_updated"])
                del self._conversations[oldest]

            if session_id not in self._conversations:
                self._conversations[session_id] = {
                    "history": [], "last_updated": time.time()
                }
            conv = self._conversations[session_id]
            conv["history"].append({
                "query": user_query[:MAX_QUERY_LENGTH],
                "books_count": len(books), "timestamp": time.time()
            })
            if len(conv["history"]) > self._max_history:
                conv["history"] = conv["history"][-self._max_history:]
            conv["last_updated"] = time.time()

    def _cleanup_expired(self):
        current_time = time.time()
        expired = [sid for sid, conv in self._conversations.items()
                   if current_time - conv["last_updated"] > self._ttl]
        for sid in expired:
            del self._conversations[sid]


search_cache = SearchCache()
article_cache = SearchCache(max_size=ARTICLE_CACHE_MAX_SIZE, ttl=ARTICLE_CACHE_TTL_SECONDS)
conversation_memory = ConversationMemory()
