"""Record and replay the model path so it can be evaluated offline.

CassetteLLM wraps any LLMClient. LLM_CASSETTE_MODE selects what it does with each call:

- off (default): passthrough, the wrapper is not even constructed by get_client().
- record: call the inner client (the live AnthropicLLM when a key exists), append one JSON
  line per call to the cassette, return the response unchanged. Exceptions are not recorded.
- replay: never call anything; look the request up in the cassette by key and return the
  recorded response, or raise CassetteMiss, which the two call sites turn into their usual
  fallbacks (llm_cassette_miss->human_queue / ->template). get_client() wraps a NullLLM in
  this mode, so a replay run cannot reach the network even on a bug.

Key: sha256 of (call site, system text, user text, schema JSON with sorted keys). Both system
texts are byte-stable across processes (docs/llm.md section 2) and the user turn is a pure
function of the event fields, so a replay of the same event is an exact hit; a repair call has
its own key because its user turn differs. When a key is recorded twice, the last line wins.

File format: JSON Lines, one record per call:
  {"key", "site", "client", "model", "request": {"system", "user", "schema"},
   "response": {"text", "model", "latency_ms", "stop_reason", "input_tokens", "output_tokens",
                "cache_read_input_tokens"}, "recorded_at"}

PII: the cassette stores the prompts verbatim. By design neither prompt carries a contact
number or an email address (classification carries no customer field at all; the nudge
carries the first name only); tests/test_llm_cassette.py asserts that on a recorded file.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .llm import CLASSIFY_SCHEMA, NUDGE_SCHEMA, CassetteMiss, LLMResponse  # noqa: F401  (CassetteMiss re-exported)

log = logging.getLogger("app.llm_cassette")

CASSETTE_MODES = ("off", "record", "replay")
DEFAULT_CASSETTE_PATH = config.BASE_DIR / "tests" / "fixtures" / "llm_cassettes" / "default.jsonl"
_RESPONSE_FIELDS = ("text", "model", "latency_ms", "stop_reason", "input_tokens", "output_tokens",
                    "cache_read_input_tokens")


def _setting(name: str, default: str) -> str:
    """A declared config attribute wins, then the environment, then the default (same rule as llm._setting)."""
    value = getattr(config, name, None)
    if value in (None, ""):
        value = os.getenv(name, "")
    return str(value).strip() or default


def cassette_mode() -> str:
    mode = _setting("LLM_CASSETTE_MODE", "off").lower()
    if mode not in CASSETTE_MODES:
        raise ValueError(f"LLM_CASSETTE_MODE={mode!r} is not one of {', '.join(CASSETTE_MODES)}")
    return mode


def cassette_path() -> Path:
    """LLM_CASSETTE_PATH; a relative value is relative to the repository root, not the cwd."""
    p = Path(_setting("LLM_CASSETTE_PATH", str(DEFAULT_CASSETTE_PATH)))
    return p if p.is_absolute() else config.BASE_DIR / p


def site_for(schema: dict) -> str:
    """The call site is identified by the schema it asks for: the wrapper sees nothing else."""
    if schema == CLASSIFY_SCHEMA:
        return "classify_unmapped"
    if schema == NUDGE_SCHEMA:
        return "draft_nudge"
    return "unknown"


def cassette_key(site: str, system: str, user: str, schema: dict) -> str:
    payload = json.dumps({"site": site, "system": system, "user": user, "schema": schema},
                         sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NullLLM:
    """The inner client in replay mode: any call is a bug, so it raises instead of reaching the network."""
    name = "null"

    def __init__(self):
        self.model = config.LLM_MODEL
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse:
        self.calls += 1
        raise RuntimeError("LLM_CASSETTE_MODE=replay: no model call is made; this request was not in the cassette")


class CassetteLLM:
    """See the module docstring. `calls` lists every call this wrapper saw, with whether it was a hit."""

    def __init__(self, inner, mode: str, path: str | Path | None = None):
        if mode not in CASSETTE_MODES:
            raise ValueError(f"unknown cassette mode {mode!r}")
        self.inner = inner
        self.mode = mode
        self.path = Path(path) if path else cassette_path()
        self.name = f"cassette-{mode}"
        self.model = getattr(inner, "model", None) or config.LLM_MODEL
        self.calls: list[dict] = []
        self._index: dict[str, dict] | None = None

    # ---- the cassette file ---------------------------------------------------------------

    def entries(self) -> list[dict]:
        """Every record in file order (an empty list when the file does not exist)."""
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{self.path}:{n}: not a JSON record ({exc})") from exc
        return out

    def _load_index(self) -> dict[str, dict]:
        if self._index is None:
            self._index = {}
            for entry in self.entries():  # last record for a key wins
                self._index[entry["key"]] = entry
            log.info("cassette %s: %d records loaded for replay", self.path, len(self._index))
        return self._index

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        if self._index is not None:
            self._index[entry["key"]] = entry

    # ---- the client protocol -------------------------------------------------------------

    def complete_json(self, system: str, user: str, schema: dict) -> LLMResponse:
        if self.mode == "off":
            return self.inner.complete_json(system, user, schema)
        site = site_for(schema)
        key = cassette_key(site, system, user, schema)
        if self.mode == "replay":
            entry = self._load_index().get(key)
            self.calls.append({"key": key, "site": site, "hit": entry is not None})
            if entry is None:
                where = f"in {self.path}" if self.path.exists() else f"({self.path} does not exist)"
                raise CassetteMiss(f"no recorded response for this {site} request {where}", key=key, site=site)
            r = entry["response"]
            return LLMResponse(text=str(r.get("text", "")), model=str(r.get("model") or self.model),
                               latency_ms=int(r.get("latency_ms", 0) or 0), stop_reason=str(r.get("stop_reason", "")),
                               input_tokens=int(r.get("input_tokens", 0) or 0),
                               output_tokens=int(r.get("output_tokens", 0) or 0),
                               cache_read_input_tokens=int(r.get("cache_read_input_tokens", 0) or 0))
        resp = self.inner.complete_json(system, user, schema)  # record: exceptions propagate unrecorded
        entry = {
            "key": key, "site": site, "client": getattr(self.inner, "name", type(self.inner).__name__),
            "model": resp.model,
            "request": {"system": system, "user": user, "schema": schema},
            "response": {f: getattr(resp, f) for f in _RESPONSE_FIELDS},
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._append(entry)
        self.calls.append({"key": key, "site": site, "hit": True})
        return resp


def wrap_for_cassette(inner):
    """What get_client() returns after faults are settled: `inner` is the live client or None.
    replay: a CassetteLLM over a NullLLM, key or no key (the point is offline evaluation).
    record: the live client wrapped, or None when there is no key (nothing to record).
    off: `inner` untouched."""
    mode = cassette_mode()
    if mode == "replay":
        return CassetteLLM(NullLLM(), "replay")
    if mode == "record" and inner is not None:
        return CassetteLLM(inner, "record")
    return inner
