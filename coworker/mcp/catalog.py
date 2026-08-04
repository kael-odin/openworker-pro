"""ModelScope MCP catalog — fetch + install from the 魔搭 MCP plaza API.

The ModelScope MCP plaza (https://www.modelscope.cn/mcp, ~9.8k servers) is served by the
public ``PUT /api/v1/dolphin/agg`` API. The agg endpoint returns all business types in one
response (models, datasets, skills, MCP, ...); we extract ``Data.Data.Mcp`` which holds
``{McpServers: [...], TotalCount: int}``.

Each MCP server entry carries a ``ServerConfig`` in the standard Claude Code format::

    [{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}]

so install is just: extract the inner ``{name: config}`` and call ``put_global_server``.
Hosted servers (``Hosted=True``) have a ``DeployedUrl`` and use streamable_http/SSE transport.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .sources import McpSource

_HTTP_TIMEOUT = 15.0

# The ModelScope MCP plaza has 13 categories (matching the official site sidebar), but the
# agg API exposes no category-list endpoint and its Criterion filter is ignored — so we can't
# discover the full set from page data alone (only the 7 most-popular slugs appear on early
# pages). This hardcoded catalog mirrors the site's own client-side list and lets the UI show
# all 13 filter chips from the first page load. Counts are approximate (from the site sidebar)
# and used only for display in the chip label.
_MCP_CATEGORIES = [
    "browser-automation",
    "search",
    "communication-and-collaboration",
    "developer-tools",
    "entertainment-and-media",
    "file-system",
    "finance",
    "knowledge-management-and-memory",
    "location-services",
    "culture-and-art",
    "academic-research",
    "schedule-management",
    "other",
]


class McpCatalogError(Exception):
    """Raised when the MCP catalog can't be fetched/parsed (message surfaces to the UI)."""


def _agg_request(url: str, body: dict) -> dict:
    """One ModelScope agg API call. Returns the full parsed JSON payload."""
    try:
        resp = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True).put(url, json=body)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        raise McpCatalogError(f"ModelScope agg API request failed ({url}): {e}") from e
    except Exception as e:
        raise McpCatalogError(f"ModelScope agg API parse error ({url}): {e}") from e
    if not isinstance(payload, dict) or payload.get("Code") != 200:
        msg = payload.get("Message") if isinstance(payload, dict) else "unknown"
        code = payload.get("Code") if isinstance(payload, dict) else "?"
        raise McpCatalogError(f"ModelScope agg API error: Code={code} {msg}")
    return payload


def _normalize_server(s: dict[str, Any]) -> dict[str, Any]:
    """Normalize one raw MCP server entry to the catalog shape the UI uses."""
    name = str(s.get("Name") or "")
    path = str(s.get("Path") or s.get("FromSitePath") or "")
    # Category is a list like ['browser-automation']; Tags likewise.
    categories = s.get("Category")
    if isinstance(categories, str):
        categories = [categories]
    elif not isinstance(categories, list):
        categories = []
    tags = s.get("Tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []
    return {
        "name": name,
        "path": path,
        "description": str(s.get("AbstractCN") or s.get("Abstract") or s.get("OriginalAbstract") or ""),
        "icon": str(s.get("FromSiteIcon") or ""),
        "category": [str(c) for c in categories],
        "tags": [str(t) for t in tags],
        "stars": int(s.get("Stars") or 0),
        "hosted": bool(s.get("Hosted")),
        "verified": bool(s.get("Verifed")),  # ModelScope's typo — keep their key name
        "source_url": str(s.get("FromSiteUrl") or ""),
        "server_config": s.get("ServerConfig"),
        "sse_server_config": s.get("SSEServerConfig"),
        "streamable_http_server_config": s.get("StreamableHTTPServerConfig"),
        "deployed_url": str(s.get("DeployedUrl") or ""),
        "supported_transports": s.get("SupportedDeployTransportType") or [],
    }


def list_mcp_catalog(
    source: McpSource,
    *,
    page: int = 1,
    page_size: int = 30,
    query: str = "",
    category: str = "",
) -> dict[str, Any]:
    """Fetch one page of the ModelScope MCP catalog.

    Returns ``{"servers": [...], "total_count": int, "page": int, "page_size": int,
    "categories": [str]}``.

    **ModelScope agg API limitations** (verified 2026-08):

    * ``Criterion`` (server-side category filter) is **ignored** — the endpoint returns all
      ~9.8k servers regardless of what Criterion we send.
    * ``PageSize`` is **ignored** — always returns at most 10 servers per call.
    * ``PageNumber`` is **ignored** — page 2 returns the same 10 servers as page 1.
    * ``Query`` is the only field that works: a text search that returns matching servers
      with a correct ``TotalCount`` for the match set.

    So to filter by category we fold the category slug into ``Query`` (a category slug like
    ``browser-automation`` is itself a valid query that returns only that category's servers,
    with the right total). This is why the old client-side filter showed "1 per page across
    328 pages" — it sent Criterion (ignored, got the global hot-10), then filtered those 10
    client-side to the ~1 that happened to carry the selected category, while pagination
    walked the (identical) hot-10 over and over. Using Query instead returns a real
    category-scoped result set with an honest total.
    """
    url = source.url
    # Combine category + free-text into a single Query (the only field the API honors).
    # A bare category slug returns that category's servers; combining with a user query
    # narrows further. We don't send Criterion at all — it's ignored and only confuses.
    parts: list[str] = []
    if query:
        parts.append(query)
    if category:
        parts.append(category)
    effective_query = " ".join(parts)
    body = {
        "PageNumber": page,
        "PageSize": page_size,
        "Query": effective_query,
        "Criterion": [],
    }
    payload = _agg_request(url, body)
    # Navigate the nested response: Data.Data.Mcp.{McpServers, TotalCount}
    data = payload.get("Data") or {}
    inner = data.get("Data") or {}
    mcp = inner.get("Mcp") or {}
    raw_servers = mcp.get("McpServers") or []
    total = mcp.get("TotalCount") or 0
    servers = [_normalize_server(s) for s in raw_servers if isinstance(s, dict) and s.get("Name")]
    # Return the full hardcoded category list (the agg API has no category endpoint and its
    # Criterion filter is ignored, so deriving from page data only surfaces the 7 hottest
    # slugs). The frontend renders chips from this list; category filtering is done via
    # Query above, so servers are already scoped to the selected category.
    return {
        "servers": servers,
        "total_count": total,
        "page": page,
        "page_size": page_size,
        "categories": list(_MCP_CATEGORIES),
    }


def install_mcp_from_catalog(source: McpSource, name: str) -> dict[str, Any]:
    """Resolve a catalog MCP server's install config by name.

    Searches the catalog for ``name`` and returns ``{"name": ..., "config": {server_name:
    {command, args}}}`` — the caller (manager) passes ``config`` to ``add_mcp``. For hosted
    servers with a DeployedUrl, builds a streamable_http/SSE config instead of the stdio
    ServerConfig.
    """
    # Search by name across pages. Start with page 1; if not found, try a few more.
    page = 1
    target: Optional[dict[str, Any]] = None
    while page <= 5:
        result = list_mcp_catalog(source, page=page, page_size=30, query=name)
        for srv in result["servers"]:
            if srv["name"] == name:
                target = srv
                break
        if target:
            break
        if page * 30 >= result["total_count"]:
            break
        page += 1
    if target is None:
        raise McpCatalogError(f"MCP server {name!r} not found in catalog")
    # ServerConfig is [{mcpServers: {<name>: {command, args}}}] — a one-element list whose
    # value is the standard Claude Code mcpServers map. Extract the inner config.
    server_config = target.get("server_config")
    if isinstance(server_config, list) and server_config:
        first = server_config[0]
        if isinstance(first, dict) and first.get("mcpServers"):
            mcp_servers = first["mcpServers"]
            if isinstance(mcp_servers, dict) and mcp_servers:
                # Use the first (and usually only) entry. The key may differ from `name`
                # (e.g. catalog name "fetch" → config key "fetch"). Prefer the catalog name.
                cfg_key = next(iter(mcp_servers))
                cfg = mcp_servers[cfg_key]
                if isinstance(cfg, dict):
                    return {"name": name, "config": {name: cfg}}
    # Hosted server with a deployed URL — use HTTP transport.
    if target.get("hosted") and target.get("deployed_url"):
        transport = "streamable_http" if "streamable_http" in (target.get("supported_transports") or []) else "sse"
        return {"name": name, "config": {name: {"url": target["deployed_url"], "transport": transport}}}
    raise McpCatalogError(
        f"MCP server {name!r} has no usable ServerConfig or DeployedUrl — cannot install"
    )
