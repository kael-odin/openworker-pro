"""MCP marketplace source management + catalog parsing (魔搭 MCP plaza).

Tests the McpSourceManager (prefs persistence, builtin guard, deleted-means-deleted)
and the ModelScope agg API catalog parser (nested Data.Data.Mcp extraction,
ServerConfig → add_mcp config extraction, hosted HTTP fallback). All network calls
are mocked — no real requests to modelscope.cn.

Mirrors tests/test_skill_sources.py and tests/test_plugins.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coworker.mcp.sources import BUILTIN_SOURCES, McpSource, McpSourceManager
from coworker.mcp.catalog import (
    McpCatalogError,
    _normalize_server,
    install_mcp_from_catalog,
    list_mcp_catalog,
)


# -- McpSourceManager (prefs persistence, builtin guard) ---------------------


def _prefs_and_mgr():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, McpSourceManager(prefs, save)


def test_source_manager_ensure_builtins_seeds_modelscope():
    prefs, mgr = _prefs_and_mgr()
    assert prefs.get("mcp_sources") is None
    mgr.ensure_builtins()
    sources = mgr.list()
    assert any(s.id == "modelscope-mcp" for s in sources)
    assert sources[0].is_default
    assert "modelscope.cn" in sources[0].url
    assert sources[0].source_type == "modelscope"


def test_source_manager_builtin_deletable_and_not_reasserted():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    assert mgr.remove("modelscope-mcp") is True
    assert mgr.get("modelscope-mcp") is None
    # ensure_builtins() must NOT re-assert a deleted builtin.
    mgr.ensure_builtins()
    assert mgr.get("modelscope-mcp") is None
    deleted = prefs.get("deleted_builtin_mcp_sources") or []
    assert "modelscope-mcp" in deleted


def test_source_manager_reset_restores_deleted_builtin():
    """reset() clears the deleted-builtin record and brings back all builtins."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    mgr.remove("modelscope-mcp")
    assert mgr.get("modelscope-mcp") is None
    sources = mgr.reset()
    assert any(s.id == "modelscope-mcp" for s in sources)
    assert "modelscope-mcp" not in (prefs.get("deleted_builtin_mcp_sources") or [])
    # Idempotent — calling again is a no-op.
    sources2 = mgr.reset()
    assert any(s.id == "modelscope-mcp" for s in sources2)


def test_source_manager_add_update_remove_user_source():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    src = mgr.add("Custom", "https://example.com/mcp-catalog")
    assert src.id.startswith("src-")
    assert src.source_type == "modelscope"  # default
    updated = mgr.update(src.id, {"name": "Renamed", "enabled": False})
    assert updated.name == "Renamed"
    assert updated.enabled is False
    assert mgr.remove(src.id) is True
    assert mgr.get(src.id) is None


def test_source_manager_add_rejects_empty_name_or_url():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    with pytest.raises(ValueError):
        mgr.add("", "http://x")
    with pytest.raises(ValueError):
        mgr.add("x", "")


def test_source_manager_ensure_builtins_re_asserts_url_and_type():
    """If a builtin's URL or source_type was edited (or persisted from an older
    release), ensure_builtins() re-asserts the current builtin values."""
    prefs, mgr = _prefs_and_mgr()
    prefs["mcp_sources"] = [
        McpSource(
            id="modelscope-mcp",
            name="Old name",
            url="https://old-url.example.com",
            is_default=True,
            source_type="http",
        ).to_dict()
    ]
    mgr.ensure_builtins()
    src = mgr.get("modelscope-mcp")
    assert src is not None
    assert src.url == BUILTIN_SOURCES[0].url  # re-asserted to current builtin URL
    assert src.source_type == "modelscope"  # re-asserted to current builtin type
    assert src.name == BUILTIN_SOURCES[0].name


# -- catalog._normalize_server -----------------------------------------------


def _raw_server(**overrides):
    base = {
        "Name": "fetch",
        "Path": "@modelcontextprotocol",
        "AbstractCN": "Fetch web pages as markdown",
        "FromSiteIcon": "https://cdn.example.com/fetch.png",
        "Category": ["browser-automation"],
        "Tags": ["web", "scrape"],
        "Stars": 840,
        "Hosted": True,
        "Verifed": True,  # ModelScope's typo — the key is literally "Verifed"
        "FromSiteUrl": "https://github.com/modelcontextprotocol/servers",
        "ServerConfig": [{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}],
        "SSEServerConfig": None,
        "StreamableHTTPServerConfig": None,
        "DeployedUrl": "https://mcp.modelscope.cn/sse/fetch",
        "SupportedDeployTransportType": ["streamable_http", "sse"],
    }
    base.update(overrides)
    return base


def test_normalize_server_full_fields():
    s = _normalize_server(_raw_server())
    assert s["name"] == "fetch"
    assert s["path"] == "@modelcontextprotocol"
    assert s["description"] == "Fetch web pages as markdown"
    assert s["icon"] == "https://cdn.example.com/fetch.png"
    assert s["category"] == ["browser-automation"]
    assert s["tags"] == ["web", "scrape"]
    assert s["stars"] == 840
    assert s["hosted"] is True
    assert s["verified"] is True
    assert s["deployed_url"] == "https://mcp.modelscope.cn/sse/fetch"
    assert s["supported_transports"] == ["streamable_http", "sse"]
    assert s["server_config"] == [{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}]


def test_normalize_server_missing_fields_defaults():
    s = _normalize_server({"Name": "minimal"})
    assert s["name"] == "minimal"
    assert s["path"] == ""
    assert s["description"] == ""
    assert s["icon"] == ""
    assert s["category"] == []
    assert s["tags"] == []
    assert s["stars"] == 0
    assert s["hosted"] is False
    assert s["verified"] is False
    assert s["server_config"] is None


def test_normalize_server_category_as_string():
    s = _normalize_server(_raw_server(Category="search"))
    assert s["category"] == ["search"]


# -- catalog.list_mcp_catalog (mocked agg API) -------------------------------


def _agg_payload(servers, total=1):
    """Build a mock agg API response with the nested Data.Data.Mcp structure."""
    return {
        "Code": 200,
        "Data": {
            "Data": {
                "Mcp": {
                    "McpServers": servers,
                    "TotalCount": total,
                },
                "Skill": {"SkillList": [], "TotalCount": 0},
                "Model": {"ModelList": [], "TotalCount": 0},
            }
        },
    }


def test_list_mcp_catalog_extracts_nested_mcp():
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([_raw_server()], total=9831)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        result = list_mcp_catalog(source, page=1, page_size=30)
    assert result["total_count"] == 9831
    assert result["page"] == 1
    assert result["page_size"] == 30
    assert len(result["servers"]) == 1
    assert result["servers"][0]["name"] == "fetch"
    assert "browser-automation" in result["categories"]


def test_list_mcp_catalog_filters_unnamed_entries():
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([_raw_server(), {"Path": "no-name"}, {"Name": "real"}], total=3)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        result = list_mcp_catalog(source)
    names = [s["name"] for s in result["servers"]]
    assert "fetch" in names
    assert "real" in names
    assert "" not in names  # the unnamed entry was filtered


def test_list_mcp_catalog_category_filter_uses_query():
    """A category filter is folded into the agg API ``Query`` field (the only field the API
    honors — Criterion/PageSize/PageNumber are all ignored). The old approach put the category
    in ``Criterion``, which the API silently dropped, causing "1 per page across 328 pages"."""
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([], total=0)
    captured = {}

    def fake_agg(url, body):
        captured["body"] = body
        return payload

    with patch("coworker.mcp.catalog._agg_request", side_effect=fake_agg):
        list_mcp_catalog(source, category="search")
    # Category appears in Query, not Criterion (Criterion must be empty — it's ignored).
    assert "search" in captured["body"]["Query"]
    assert captured["body"]["Criterion"] == []


def test_list_mcp_catalog_combines_query_and_category():
    """When both a free-text query and a category are given, both are folded into ``Query``."""
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([], total=0)
    captured = {}

    def fake_agg(url, body):
        captured["body"] = body
        return payload

    with patch("coworker.mcp.catalog._agg_request", side_effect=fake_agg):
        list_mcp_catalog(source, query="fetch", category="browser-automation")
    q = captured["body"]["Query"]
    assert "fetch" in q
    assert "browser-automation" in q
    assert captured["body"]["Criterion"] == []


def test_list_mcp_catalog_raises_on_api_error():
    """The agg request helper raises McpCatalogError when the API returns a non-200 Code.
    list_mcp_catalog propagates that error to the caller."""
    from coworker.mcp import catalog as cat_mod

    source = BUILTIN_SOURCES[0]
    payload = {"Code": 500, "Message": "internal error"}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    with patch.object(cat_mod.httpx.Client, "put", return_value=_FakeResp()):
        with pytest.raises(McpCatalogError, match="Code=500"):
            list_mcp_catalog(source)


# -- catalog.install_mcp_from_catalog (mocked) -------------------------------


def test_install_extracts_server_config():
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([_raw_server()], total=1)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        result = install_mcp_from_catalog(source, "fetch")
    assert result["name"] == "fetch"
    assert result["config"] == {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}


def test_install_not_found_raises():
    source = BUILTIN_SOURCES[0]
    payload = _agg_payload([_raw_server()], total=1)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        with pytest.raises(McpCatalogError, match="not found"):
            install_mcp_from_catalog(source, "nonexistent-server")


def test_install_hosted_with_deployed_url_uses_http_transport():
    source = BUILTIN_SOURCES[0]
    # No ServerConfig, but Hosted=True + DeployedUrl → streamable_http config
    raw = _raw_server(ServerConfig=None, Hosted=True, DeployedUrl="https://mcp.example.cn/sse/srv")
    payload = _agg_payload([raw], total=1)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        result = install_mcp_from_catalog(source, "fetch")
    assert result["name"] == "fetch"
    assert result["config"]["fetch"]["url"] == "https://mcp.example.cn/sse/srv"
    assert result["config"]["fetch"]["transport"] == "streamable_http"


def test_install_no_config_no_deployed_url_raises():
    source = BUILTIN_SOURCES[0]
    raw = _raw_server(ServerConfig=None, Hosted=False, DeployedUrl="")
    payload = _agg_payload([raw], total=1)
    with patch("coworker.mcp.catalog._agg_request", return_value=payload):
        with pytest.raises(McpCatalogError, match="no usable ServerConfig"):
            install_mcp_from_catalog(source, "fetch")
