"""Local, privacy-first long-term memory adapter for the POS AI assistant.

Mem0 is deliberately optional.  This module never configures a hosted vector
database or hosted model: it only starts when explicitly enabled and supplied a
local Mem0 configuration (for example Chroma plus an on-device Ollama model).
Callers can therefore use it unconditionally without making AI chat depend on
the memory feature.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

LOG = logging.getLogger(__name__)

_SCOPES = {"private", "branch_shared"}
_MAX_TEXT = 1_000
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 12
_SENSITIVE_PATTERNS = (
    r"\b(?:password|passcode|pin|api[_ -]?key|secret|access[_ -]?token|bearer)\b",
    r"\b(?:cvv|cvc)\s*[:#-]?\s*\d{3,4}\b",
    r"\b(?:card(?:\s+number)?|credit\s+card)\s*[:#-]?\s*(?:\d[ -]?){12,19}\b",
    r"\b(?:account\s*(?:number|no\.?|#)|iban|swift)\s*[:#-]?\s*[a-z0-9 -]{6,}\b",
    r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN-like values
)
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_PATTERNS), re.IGNORECASE)


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class MemoryService:
    """Small compatibility wrapper around Mem0 with scoped, bounded results.

    ``client`` is injectable for tests and supports the public Mem0 methods
    ``add``, ``search``, ``get_all`` and ``delete``.  A database/session and
    SQLAlchemy models are optional integration hooks; failures in those hooks
    are recorded in logs but never make an assistant request fail.
    """

    def __init__(self, client: Any = None, *, enabled: Optional[bool] = None,
                 db: Any = None, models: Optional[Mapping[str, Any]] = None,
                 registry_model: Any = None, audit_model: Any = None):
        self.db = db
        self.models = dict(models or {})
        self.registry_model = registry_model or self.models.get("MemoryRegistry")
        self.audit_model = audit_model or self.models.get("MemoryAudit")
        self._lock = threading.RLock()
        self._client = client
        self.enabled = bool(client) if enabled is None else bool(enabled)
        self.unavailable_reason: Optional[str] = None
        if self.enabled and self._client is None:
            self._client = self._create_local_mem0_client()
        if self._client is None:
            self.enabled = False

    def _create_local_mem0_client(self) -> Any:
        """Load Mem0 only on opt-in; reject configs that imply hosted services."""
        if not _truthy(os.getenv("AI_MEMORY_ENABLED")):
            self.unavailable_reason = "disabled"
            return None
        raw_config = os.getenv("AI_MEMORY_MEM0_CONFIG", "").strip()
        if not raw_config:
            self.unavailable_reason = "local configuration missing"
            return None
        try:
            config = json.loads(raw_config)
            self._assert_embedded_config(config)
            from mem0 import Memory  # type: ignore[import-not-found]
            return Memory.from_config(config)
        except ImportError:
            self.unavailable_reason = "mem0 package unavailable"
        except Exception as exc:  # bad config/dependency must not break POS
            self.unavailable_reason = "local Mem0 unavailable"
            LOG.warning("AI memory is unavailable: %s", exc)
        return None

    @staticmethod
    def _assert_embedded_config(config: Mapping[str, Any]) -> None:
        encoded = json.dumps(config).lower()
        forbidden = ("openai", "azure", "pinecone", "qdrant", "weaviate", "supabase")
        if any(marker in encoded for marker in forbidden):
            raise ValueError("AI_MEMORY_MEM0_CONFIG must use embedded/local providers only")
        urls = re.findall(r"https?://[^\"'\\\s,}]+", encoded)
        local_hosts = ("localhost", "127.0.0.1", "host.docker.internal", "ollama")
        if any(not any(host in url for host in local_hosts) for url in urls):
            raise ValueError("AI_MEMORY_MEM0_CONFIG may only connect to a local Ollama endpoint")

    @staticmethod
    def _scope(user_id: Any, branch_id: Any, scope: str) -> Dict[str, Any]:
        if user_id is None or branch_id is None:
            raise ValueError("user_id and branch_id are required for memory isolation")
        if scope not in _SCOPES:
            raise ValueError("scope must be private or branch_shared")
        return {"user_id": str(user_id), "branch_id": str(branch_id), "scope": scope}

    @staticmethod
    def _mem0_namespace(namespace: Mapping[str, str]) -> str:
        """Keep personal records private while making shared records branch-wide."""
        if namespace["scope"] == "branch_shared":
            return "branch:" + namespace["branch_id"]
        return "user:" + namespace["user_id"]

    @property
    def available(self) -> bool:
        return self.enabled and self._client is not None

    @staticmethod
    def _clean_text(content: Any) -> str:
        if not isinstance(content, str):
            raise ValueError("memory content must be text")
        text = " ".join(content.split())
        if not text:
            raise ValueError("memory content cannot be empty")
        if len(text) > _MAX_TEXT:
            raise ValueError("memory content exceeds 1000 characters")
        if _SENSITIVE_RE.search(text):
            raise ValueError("sensitive data must not be stored in AI memory")
        return text

    def is_sensitive(self, content: Any) -> bool:
        return not isinstance(content, str) or bool(_SENSITIVE_RE.search(content))

    def should_auto_save(self, content: Any) -> bool:
        """Conservative auto-save: only short, preference-like, non-sensitive facts."""
        try:
            text = self._clean_text(content)
        except ValueError:
            return False
        return len(text) <= 300 and bool(re.match(
            r"^(?:remember\s+that\s+)?(?:i\s+(?:prefer|like|want)|my\s+(?:preference|default)\s+is|always\s+use)\b",
            text, re.IGNORECASE,
        ))

    def remember(self, content: str, *, user_id: Any, branch_id: Any,
                 scope: str = "private", source: str = "explicit",
                 explicit: bool = True, allow_auto: bool = False, **hooks: Any) -> Dict[str, Any]:
        text, namespace = self._clean_text(content), self._scope(user_id, branch_id, scope)
        if not explicit and not (allow_auto and self.should_auto_save(text)):
            return {"saved": False, "reason": "auto-save policy", "memory_id": None}
        if not self.enabled or self._client is None:
            return {"saved": False, "reason": self.unavailable_reason or "disabled", "memory_id": None}
        try:
            with self._lock:
                result = self._client.add([{ "role": "user", "content": text }], user_id=self._mem0_namespace(namespace), metadata=namespace)
            memory = self._normalise_one(result, fallback=text)
            memory_id = memory.get("id") or memory.get("memory_id")
            self._registry("create", memory_id, text, namespace, source, hooks)
            return {"saved": True, "memory_id": memory_id, "memory": memory}
        except Exception as exc:
            LOG.warning("AI memory write failed: %s", exc)
            return {"saved": False, "reason": "backend unavailable", "memory_id": None}

    def retrieve(self, query: str, *, user_id: Any, branch_id: Any,
                 scope: Optional[str] = None, limit: int = _DEFAULT_LIMIT, **_: Any) -> List[Dict[str, Any]]:
        text = self._clean_text(query)
        scopes = [scope] if scope else ["private", "branch_shared"]
        if any(item not in _SCOPES for item in scopes):
            raise ValueError("invalid scope")
        namespace = self._scope(user_id, branch_id, scopes[0])
        if not self.enabled or self._client is None:
            return []
        capped = max(1, min(int(limit), _MAX_LIMIT))
        found: List[Dict[str, Any]] = []
        try:
            # Fetching a few extra permits reliable client-side isolation across
            # Mem0 versions whose filter syntax differs.  branch_shared records
            # intentionally use a branch namespace so peers can retrieve them.
            for requested_scope in scopes:
                scoped_namespace = self._scope(user_id, branch_id, requested_scope)
                with self._lock:
                    raw = self._client.search(text, user_id=self._mem0_namespace(scoped_namespace), limit=min(capped * 3, _MAX_LIMIT * 3))
                for item in self._normalise_many(raw):
                    metadata = item.get("metadata") or {}
                    if str(metadata.get("branch_id")) == namespace["branch_id"] and metadata.get("scope") == requested_scope:
                        found.append(item)
                    if len(found) == capped:
                        break
                if len(found) == capped:
                    break
        except Exception as exc:
            LOG.warning("AI memory retrieval failed: %s", exc)
        return found

    def build_context(self, query: str, *, user_id: Any, branch_id: Any,
                      scope: Optional[str] = None, limit: int = _DEFAULT_LIMIT,
                      max_characters: int = 1_500, **hooks: Any) -> str:
        budget, lines = max(0, min(int(max_characters), 4_000)), []
        used = 0
        for item in self.retrieve(query, user_id=user_id, branch_id=branch_id, scope=scope, limit=limit, **hooks):
            value = str(item.get("memory") or item.get("content") or "").strip()
            line = "- " + value
            separator = 1 if lines else 0
            if not value or used + separator + len(line) > budget:
                continue
            lines.append(line)
            used += separator + len(line)
        return "\n".join(lines)

    def list_memories(self, *, user_id: Any, branch_id: Any, scope: Optional[str] = None,
                      limit: int = _MAX_LIMIT, **_: Any) -> List[Dict[str, Any]]:
        # Registry is authoritative for management UI; this remains a safe
        # backend fallback when registry persistence is not supplied.
        if self.registry_model is not None:
            try:
                query = self.registry_model.query.filter_by(user_id=user_id, branch_id=branch_id)
                if scope:
                    query = query.filter_by(scope=scope)
                return [self._model_dict(row) for row in query.order_by(self.registry_model.updated_at.desc()).limit(max(1, min(int(limit), _MAX_LIMIT))).all()]
            except Exception as exc:
                LOG.warning("AI memory registry listing failed: %s", exc)
        return []

    def forget(self, memory_id: Any, *, user_id: Any, branch_id: Any, scope: Optional[str] = None, **hooks: Any) -> Dict[str, Any]:
        if not memory_id:
            raise ValueError("memory_id is required")
        scopes = [scope] if scope else ["private", "branch_shared"]
        self._scope(user_id, branch_id, scopes[0])
        if not self.enabled or self._client is None:
            return {"deleted": False, "reason": self.unavailable_reason or "disabled"}
        try:
            if not self._owns_memory(str(memory_id), user_id, branch_id, scopes, hooks):
                return {"deleted": False, "reason": "memory not found"}
            with self._lock:
                self._client.delete(memory_id=str(memory_id))
            self._registry("delete", str(memory_id), "", {"user_id": str(user_id), "branch_id": str(branch_id), "scope": scope or "private"}, "user", hooks)
            return {"deleted": True, "memory_id": str(memory_id)}
        except Exception as exc:
            LOG.warning("AI memory delete failed: %s", exc)
            return {"deleted": False, "reason": "backend unavailable"}

    def _owns_memory(self, memory_id: str, user_id: Any, branch_id: Any,
                     scopes: List[str], hooks: Mapping[str, Any]) -> bool:
        """Fail closed: an opaque id must not become a cross-tenant delete key."""
        registry = hooks.get("registry_model") or self.registry_model
        if registry is not None:
            try:
                row = registry.query.filter_by(memory_id=memory_id, user_id=user_id, branch_id=branch_id).first()
                return bool(row and getattr(row, "scope", None) in scopes)
            except Exception as exc:
                LOG.warning("AI memory ownership check failed: %s", exc)
                return False
        try:
            record = self._normalise_one(self._client.get(memory_id=memory_id))
            metadata = record.get("metadata") or {}
            return (str(metadata.get("user_id")) == str(user_id)
                    and str(metadata.get("branch_id")) == str(branch_id)
                    and metadata.get("scope") in scopes)
        except Exception:
            return False

    def _registry(self, action: str, memory_id: Any, summary: str, namespace: Dict[str, Any], source: str, hooks: Mapping[str, Any]) -> None:
        db = hooks.get("db") or self.db
        registry = hooks.get("registry_model") or self.registry_model
        audit = hooks.get("audit_model") or self.audit_model
        if not db or not getattr(db, "session", None): return
        try:
            if action == "create" and registry and memory_id:
                db.session.add(registry(memory_id=str(memory_id), user_id=int(namespace["user_id"]), branch_id=int(namespace["branch_id"]), scope=namespace["scope"], summary=summary[:500], source=source))
            if audit:
                db.session.add(audit(actor_user_id=int(namespace["user_id"]), branch_id=int(namespace["branch_id"]), memory_id=str(memory_id) if memory_id else None, action=action, scope=namespace["scope"], details=json.dumps({"source": source})))
        except Exception as exc:
            LOG.warning("AI memory registry/audit hook failed: %s", exc)

    @staticmethod
    def _normalise_one(value: Any, fallback: str = "") -> Dict[str, Any]:
        if isinstance(value, Mapping):
            if isinstance(value.get("results"), list) and value["results"]: value = value["results"][0]
            elif isinstance(value.get("memories"), list) and value["memories"]: value = value["memories"][0]
        return dict(value) if isinstance(value, Mapping) else {"memory": fallback}

    @classmethod
    def _normalise_many(cls, value: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(value, Mapping): value = value.get("results", value.get("memories", []))
        return [cls._normalise_one(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _model_dict(row: Any) -> Dict[str, Any]:
        return {key: getattr(row, key) for key in ("id", "memory_id", "user_id", "branch_id", "scope", "summary", "source", "created_at", "updated_at") if hasattr(row, key)}


_service: Optional[MemoryService] = None
_service_lock = threading.Lock()

def get_memory_service(**kwargs: Any) -> MemoryService:
    """Return the process singleton; optional hooks refresh without reloading Mem0."""
    global _service
    with _service_lock:
        if _service is None:
            _service = MemoryService(**kwargs)
        else:
            for name in ("db", "registry_model", "audit_model"):
                if kwargs.get(name) is not None: setattr(_service, name, kwargs[name])
            if kwargs.get("models"): _service.models.update(kwargs["models"])
    return _service
