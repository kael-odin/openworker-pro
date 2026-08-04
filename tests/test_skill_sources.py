"""Skill source management + install/uninstall (批次 E1)."""

from __future__ import annotations

from pathlib import Path

from coworker.skills import (
    SkillInstallError,
    SkillSource,
    SkillSourceManager,
    install_skill,
    list_catalog,
    uninstall_skill,
)


# -- SkillSourceManager (prefs persistence, builtin guard) ---------------------


def _prefs_and_mgr():
    prefs: dict = {}
    save = lambda: None  # noqa: E731 — in-memory; save is a no-op
    return prefs, SkillSourceManager(prefs, save)


def test_source_manager_ensure_builtins_seeds_official_source():
    prefs, mgr = _prefs_and_mgr()
    assert prefs.get("skill_sources") is None  # empty before
    mgr.ensure_builtins()
    sources = mgr.list()
    assert any(s.id == "anthropic-official" for s in sources)
    assert sources[0].is_default  # default sorts first


def test_source_manager_builtin_deletable_and_not_reasserted():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    # Builtins are now deletable (user chose "deleted means deleted, no auto-restore").
    assert mgr.remove("anthropic-official") is True
    assert mgr.get("anthropic-official") is None
    # ensure_builtins() must NOT re-assert a deleted builtin — the deletion is recorded
    # in the deleted_builtin_skill_sources pref so it survives restarts.
    mgr.ensure_builtins()
    assert mgr.get("anthropic-official") is None
    # The deleted-builtin record is persisted in prefs.
    deleted = prefs.get("deleted_builtin_skill_sources") or []
    assert "anthropic-official" in deleted


def test_source_manager_reset_restores_deleted_builtin():
    """reset() clears the deleted-builtin record and brings back all builtins,
    including the mattpocock-skills source added to BUILTIN_SOURCES."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    mgr.remove("anthropic-official")
    mgr.remove("mattpocock-skills")
    assert mgr.get("anthropic-official") is None
    assert mgr.get("mattpocock-skills") is None
    sources = mgr.reset()
    ids = [s.id for s in sources]
    assert "anthropic-official" in ids
    assert "mattpocock-skills" in ids
    # The deleted-builtin record is cleared.
    deleted = prefs.get("deleted_builtin_skill_sources") or []
    assert "anthropic-official" not in deleted
    assert "mattpocock-skills" not in deleted


def test_source_manager_add_update_remove_user_source():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    src = mgr.add("My local", "/tmp/skills", source_type="local")
    assert src.id.startswith("src-")
    assert src.source_type == "local"
    # Update name.
    updated = mgr.update(src.id, {"name": "Renamed"})
    assert updated.name == "Renamed"
    # User source CAN be deleted (unlike builtins).
    assert mgr.remove(src.id) is True
    assert mgr.get(src.id) is None


def test_source_manager_add_rejects_empty_name_or_url():
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    try:
        mgr.add("", "http://x")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        mgr.add("x", "")
        assert False, "expected ValueError"
    except ValueError:
        pass


# -- install/uninstall from a local source -------------------------------------


def _seed_local_repo(repo: Path) -> dict:
    """Build a fake skill repo: two SKILL.md subfolders."""
    for name, desc, body in [
        ("pdf", "extract text from PDFs", "Use pdfplumber."),
        ("slack", "send a Slack message", "Use the webhook URL."),
    ]:
        d = repo / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8"
        )
    return {"pdf": "extract text from PDFs", "slack": "send a Slack message"}


def test_list_catalog_local_source_discovers_skills(tmp_path):
    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")
    catalog = list_catalog(src, tmp_path / "cache")
    names = {s["name"] for s in catalog}
    assert names == {"pdf", "slack"}
    assert all("description" in s and "path" in s for s in catalog)


def test_install_from_local_source_copies_folder_and_loader_sees_it(tmp_path):
    from coworker.skills import SkillLoader

    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    skills_dir = tmp_path / "installed" / "skills"
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")

    result = install_skill(src, "pdf", skills_dir=skills_dir, cache_root=tmp_path / "cache")
    assert result["ok"] is True
    assert (skills_dir / "pdf" / "SKILL.md").is_file()

    # The installed skill is discoverable by SkillLoader (the runtime path).
    loader = SkillLoader([skills_dir])
    catalog = {s["name"]: s["description"] for s in loader.catalog()}
    assert catalog == {"pdf": "extract text from PDFs"}


def test_install_is_idempotent_overwrites(tmp_path):
    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    skills_dir = tmp_path / "skills"
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")

    install_skill(src, "pdf", skills_dir=skills_dir, cache_root=tmp_path / "cache")
    # Mutate the installed copy, then re-install — should be replaced, not merged.
    (skills_dir / "pdf" / "extra.txt").write_text("junk", encoding="utf-8")
    install_skill(src, "pdf", skills_dir=skills_dir, cache_root=tmp_path / "cache")
    assert not (skills_dir / "pdf" / "extra.txt").exists()
    assert (skills_dir / "pdf" / "SKILL.md").is_file()


def test_install_missing_skill_raises(tmp_path):
    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    skills_dir = tmp_path / "skills"
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")
    try:
        install_skill(src, "nonexistent", skills_dir=skills_dir, cache_root=tmp_path / "cache")
        assert False, "expected SkillInstallError"
    except SkillInstallError:
        pass


def test_uninstall_removes_folder(tmp_path):
    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    skills_dir = tmp_path / "skills"
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")
    install_skill(src, "pdf", skills_dir=skills_dir, cache_root=tmp_path / "cache")

    result = uninstall_skill("pdf", skills_dir=skills_dir)
    assert result["ok"] is True
    assert not (skills_dir / "pdf").exists()


def test_uninstall_missing_raises(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    try:
        uninstall_skill("nope", skills_dir=skills_dir)
        assert False, "expected SkillInstallError"
    except SkillInstallError:
        pass


def test_uninstall_rejects_path_traversal(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    for bad in ["..", "../etc", "/abs/path", ""]:
        try:
            uninstall_skill(bad, skills_dir=skills_dir)
            assert False, f"expected SkillInstallError for {bad!r}"
        except SkillInstallError:
            pass


def test_install_rejects_path_traversal_name(tmp_path):
    repo = tmp_path / "repo"
    _seed_local_repo(repo)
    skills_dir = tmp_path / "skills"
    src = SkillSource(id="s1", name="local", url=str(repo), source_type="local")
    for bad in ["..", "../etc", "/abs/path", "name/with/.."]:
        try:
            install_skill(src, bad, skills_dir=skills_dir, cache_root=tmp_path / "cache")
            assert False, f"expected SkillInstallError for {bad!r}"
        except SkillInstallError:
            pass


def test_builtin_skill_protected_from_uninstall(tmp_path):
    """A SKILL.md with x-openworker-builtin: true cannot be uninstalled."""
    skills_dir = tmp_path / "skills"
    bdir = skills_dir / "shipped"
    bdir.mkdir(parents=True)
    (bdir / "SKILL.md").write_text(
        "---\nname: shipped\ndescription: builtin\nx-openworker-builtin: true\n---\nbody",
        encoding="utf-8",
    )
    try:
        uninstall_skill("shipped", skills_dir=skills_dir)
        assert False, "expected SkillInstallError for builtin"
    except SkillInstallError as e:
        assert "built-in" in str(e)
    # Folder is still there.
    assert (bdir / "SKILL.md").is_file()


# -- SkillHub source (腾讯 SkillHub — public GET search + ZIP download) --------


def test_skillhub_builtin_source_seeded():
    """ensure_builtins() seeds the skillhub source alongside anthropic + modelscope."""
    prefs, mgr = _prefs_and_mgr()
    mgr.ensure_builtins()
    sources = mgr.list()
    ids = [s.id for s in sources]
    assert "skillhub" in ids
    sh = next(s for s in sources if s.id == "skillhub")
    assert sh.source_type == "skillhub"
    assert "skillhub.cn" in sh.url
    assert sh.is_default


def test_list_skillhub_skills_normalizes_search_results():
    """_list_skillhub_skills parses the SkillHub search API response into the catalog shape."""
    from unittest.mock import patch
    from coworker.skills.installer import _list_skillhub_skills

    source = SkillSource(
        id="skillhub", name="test", url="https://api.skillhub.cn", source_type="skillhub",
    )
    fake_payload = {
        "results": [
            {
                "slug": "skill-creator",
                "displayName": "Skill Creator",
                "description": "English desc",
                "description_zh": "中文描述",
                "category": "ai-agent",
                "icon_url": "https://cdn.example.com/icon.png",
                "installs": 3453,
                "stars": 445,
                "namespace": {"canonicalName": "@chindden/skill-creator"},
                "version": "0.1.0",
                "homepage": "https://skillhub.cn/skills/skill-creator",
            },
        ]
    }
    with patch("coworker.skills.installer._skillhub_get", return_value=fake_payload):
        result = _list_skillhub_skills(source, page=1, page_size=10, query="skill")
    assert result["total_count"] == 1
    assert result["page"] == 1
    assert "ai-agent" in result["categories"]
    item = result["skills"][0]
    assert item["name"] == "skill-creator"
    assert item["display_name"] == "Skill Creator"
    assert item["description"] == "中文描述"
    assert item["icon"] == "https://cdn.example.com/icon.png"
    assert item["downloads"] == 3453
    assert item["stars"] == 445
    assert item["author"] == "@chindden/skill-creator"


def test_install_skillhub_skill_extracts_zip_bundle(tmp_path):
    """_install_skillhub_skill downloads a ZIP and extracts the full bundle (SKILL.md + scripts)."""
    import io
    import zipfile
    from unittest.mock import patch, MagicMock
    from coworker.skills.installer import _install_skillhub_skill

    # Build a fake SkillHub ZIP with SKILL.md + a script + references.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: test-skill\ndescription: test\n---\nbody")
        zf.writestr("scripts/run.py", "print('hi')")
        zf.writestr("references/guide.md", "# Guide")
        zf.writestr("_meta.json", '{"version": "1.0"}')
    zip_bytes = buf.getvalue()

    source = SkillSource(
        id="skillhub", name="test", url="https://api.skillhub.cn", source_type="skillhub",
    )
    fake_resp = MagicMock()
    fake_resp.content = zip_bytes
    fake_resp.raise_for_status = lambda: None
    with patch("coworker.skills.installer.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.get.return_value = fake_resp
        result = _install_skillhub_skill(source, "test-skill", skills_dir=tmp_path / "skills")
    assert result["ok"] is True
    dest = Path(result["path"])
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "run.py").is_file()
    assert (dest / "references" / "guide.md").is_file()
    assert (dest / "_meta.json").is_file()
    # SKILL.md content is the real ZIP content, not empty.
    assert "test-skill" in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_install_skillhub_skill_rejects_non_zip(tmp_path):
    """If the download isn't a ZIP (PK header), install raises SkillInstallError."""
    from unittest.mock import patch, MagicMock
    from coworker.skills.installer import _install_skillhub_skill, SkillInstallError

    source = SkillSource(
        id="skillhub", name="test", url="https://api.skillhub.cn", source_type="skillhub",
    )
    fake_resp = MagicMock()
    fake_resp.content = b"not a zip"
    fake_resp.raise_for_status = lambda: None
    with patch("coworker.skills.installer.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.get.return_value = fake_resp
        try:
            _install_skillhub_skill(source, "bad", skills_dir=tmp_path / "skills")
            assert False, "expected SkillInstallError"
        except SkillInstallError as e:
            assert "ZIP" in str(e) or "not a ZIP" in str(e)


# -- Codex 3-field plugin source (ref + sparse_path) --------------------------
