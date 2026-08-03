"""DHP source adapters — fetch index + spec from a source.

Two adapters, one interface:

* :class:`DhpHttpAdapter` — fetches ``index.json`` + ``spec.yaml`` from an HTTP URL (the DHP GitHub
  Pages site, or a self-hosted mirror). This is the default path: no local clone needed, so the
  store works out of the box even when the sidecar has no ``OPENWORKER_DHP_REPO`` env (the root cause
  of the empty-store bug).
* :class:`LocalRepoAdapter` — reads the same files from a local ``digital-human-protocol`` checkout.
  Used in dev (the repo is cloned) and as a fallback when a local source is configured.

Both return :class:`~coworker.digital_human.store.RegistryEntry` lists and
:class:`~coworker.digital_human.spec.DigitalHumanSpec` objects, so the registry stays
source-agnostic.
"""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import httpx

from .spec import DigitalHumanSpec, SpecError, load_spec_file, parse_spec
from .store import RegistryEntry, _entry_from_index

# Spec fetch timeout. DHP spec.yaml files are small (<50 KB); 10s is generous even on a slow link.
_HTTP_TIMEOUT = 10.0


def assert_safe_rel_path(path: str) -> str:
    """Reject path-traversal / absolute paths before joining to a base URL.

    A DHP index entry's ``path`` is repo-relative (e.g. ``packages/digital-humans/foo``). A
    malicious or corrupt entry could try ``../../etc/passwd`` or an absolute path to escape the
    base URL. We allow only forward-slash relative paths with no ``..`` segments.
    """
    if not path or not isinstance(path, str):
        raise SpecError("empty spec path")
    # Normalize backslashes (Windows) — a remote index should use POSIX, but be defensive.
    p = path.replace("\\", "/")
    if p.startswith("/"):
        raise SpecError(f"absolute path not allowed: {path!r}")
    parts = p.split("/")
    if any(part == ".." for part in parts):
        raise SpecError(f"path traversal not allowed: {path!r}")
    return p


def _strip_index_suffix(url: str) -> str:
    """Remove a trailing ``/index.json`` (or ``/index.yml``) from a source URL.

    The adapter joins ``{self.url}/index.json`` to fetch the catalog, so a source URL that
    already ends in ``/index.json`` would request ``…/index.json/index.json`` and 404 silently
    (the empty-store bug). Both the full-file and bare-directory forms should work.
    """
    if not url:
        return url
    lower = url.lower().rstrip("/")
    for suffix in ("/index.json", "/index.yml", "/index.yaml"):
        if lower.endswith(suffix):
            return url[: -len(suffix)]
    return url


class DhpHttpAdapter:
    """Fetch DHP index + specs from an HTTP source URL.

    The URL points at the directory containing ``index.json`` (e.g. the GitHub Pages root). Spec
    paths in the index are joined to this base URL.
    """

    def __init__(self, url: str) -> None:
        # Normalize: the source URL may point at the directory OR at the index file itself.
        # fetch_index appends "/index.json", so strip a trailing index.json/.json here to
        # avoid a double-suffix request (…/index.json/index.json → 404 → empty store).
        # This is the silent empty-store root cause: a user pastes the full index URL and the
        # store shows no digital humans because the doubled path 404s and the error is swallowed.
        self.url = _strip_index_suffix(url).rstrip("/")
        self._index: Optional[list[RegistryEntry]] = None
        self._index_fetched_at: float = 0.0
        # LRU-ish spec cache: slug → (spec, fetched_at). Bounded by the number of slugs (≤ a few
        # hundred), so no explicit eviction is needed; entries stale out after the TTL.
        self._spec_cache: dict[str, tuple[DigitalHumanSpec, float]] = {}
        self._spec_ttl = 300.0  # 5 min

    def _client(self):
        # httpx is a core dep (sync senders), imported at module level so tests can patch it.
        return httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)

    def _join(self, rel: str) -> str:
        safe = assert_safe_rel_path(rel)
        return f"{self.url}/{safe}"

    def fetch_index(self, *, force: bool = False) -> list[RegistryEntry]:
        """Return the catalog entries. Cached for the process lifetime unless ``force``.

        On network failure, returns the stale cache if one exists; on a first-load failure
        (no cache), **raises** so :class:`DhpRegistry._ensure_loaded` records the error and the
        store UI shows a diagnostic instead of a silently-empty list.
        """
        if self._index is not None and not force and (time.monotonic() - self._index_fetched_at) < self._spec_ttl:
            return self._index or []
        url = f"{self.url}/index.json"
        try:
            resp = self._client().get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # Stale cache is better than nothing — return it so the store stays usable
            # during a transient outage.
            if self._index is not None:
                return self._index
            # First load with no cache: raise so the registry records the error for the UI.
            raise RuntimeError(f"failed to fetch {url}: {e}") from e
        entries: list[RegistryEntry] = []
        for item in (data.get("apps") or []) if isinstance(data, dict) else []:
            if isinstance(item, dict):
                entry = _entry_from_index(item)
                if entry.slug:
                    entries.append(entry)
        self._index = entries
        self._index_fetched_at = time.monotonic()
        return entries

    def fetch_spec(self, slug: str, path: str) -> DigitalHumanSpec:
        """Fetch + parse the spec for ``slug`` at index-relative ``path``."""
        cached = self._spec_cache.get(slug)
        if cached is not None and (time.monotonic() - cached[1]) < self._spec_ttl:
            return cached[0]
        # Path may point at the package dir (…/foo) or the spec file (…/foo/spec.yaml).
        if not path.endswith(".yaml") and not path.endswith(".yml") and not path.endswith(".json"):
            path = f"{path}/spec.yaml"
        url = self._join(path)
        try:
            resp = self._client().get(url)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            raise SpecError(f"failed to fetch spec for {slug!r} from {url}: {e}") from e
        spec = parse_spec(text, source=url)
        self._spec_cache[slug] = (spec, time.monotonic())
        return spec

    def reload(self) -> None:
        self._index = None
        self._spec_cache.clear()


class LocalRepoAdapter:
    """Read DHP index + specs from a local ``digital-human-protocol`` checkout.

    This wraps the pre-existing local-read logic from :class:`DhpRegistry` so a local source type
    works under the same multi-source registry. ``OPENWORKER_DHP_REPO`` still points here when set.
    """

    def __init__(self, repo_dir: str | Path) -> None:
        self.repo_dir = Path(repo_dir)
        self._entries: dict[str, RegistryEntry] = {}
        self._spec_cache: dict[str, DigitalHumanSpec] = {}
        if self.repo_dir.is_dir():
            self._load_index(self.repo_dir / "index.json")

    def _load_index(self, index_path: Path) -> None:
        import json

        if not index_path.is_file():
            return
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in data.get("apps") or []:
            if not isinstance(item, dict):
                continue
            entry = _entry_from_index(item)
            if entry.slug:
                self._entries[entry.slug] = entry

    def fetch_index(self, *, force: bool = False) -> list[RegistryEntry]:
        return list(self._entries.values())

    def fetch_spec(self, slug: str, path: str) -> DigitalHumanSpec:
        if slug in self._spec_cache:
            return self._spec_cache[slug]
        entry = self._entries.get(slug)
        if entry is None:
            raise SpecError(f"digital human {slug!r} is not in this local source")
        # Reuse the index `path`, falling back to the conventional location.
        pkg_dir = self.repo_dir / entry.path if entry.path else self.repo_dir / "packages" / "digital-humans" / slug
        spec_path = pkg_dir / "spec.yaml"
        if not spec_path.is_file():
            alt = pkg_dir / "spec.json"
            if alt.is_file():
                spec_path = alt
            else:
                raise SpecError(f"no spec.yaml found for {slug!r} at {pkg_dir}")
        spec = load_spec_file(spec_path)
        self._spec_cache[slug] = spec
        return spec

    def reload(self) -> None:
        self._entries.clear()
        self._spec_cache.clear()
        if self.repo_dir.is_dir():
            self._load_index(self.repo_dir / "index.json")


def make_adapter(source) -> Optional[DhpHttpAdapter | LocalRepoAdapter]:
    """Build the right adapter for a :class:`~coworker.digital_human.sources.RegistrySource`.

    Returns None for an HTTP source whose URL is empty (shouldn't happen — validation rejects it,
    but the registry treats None as "skip this source" rather than crashing).
    """
    if source.source_type == "local":
        if not source.url:
            return None
        return LocalRepoAdapter(source.url)
    # Default: HTTP.
    if not source.url:
        return None
    return DhpHttpAdapter(source.url)
