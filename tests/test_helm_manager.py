"""Tests for BlackRoad Helm Chart Manager.

Run with:
    pytest tests/ -v
    pytest tests/ -v --tb=short --cov=helm_manager
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from helm_manager import (
    Chart,
    ChartTemplate,
    HelmManager,
    Release,
    ReleaseRevision,
    _deep_merge,
    _merge,
    _render,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mgr(tmp_path: Path) -> HelmManager:
    """Isolated HelmManager backed by a temp SQLite DB."""
    m = HelmManager(db_path=tmp_path / "test.db")
    yield m
    m.close()


@pytest.fixture
def simple_chart(mgr: HelmManager) -> Chart:
    return mgr.create_chart(
        name="my-app",
        version="1.0.0",
        description="Test chart",
        values_yaml="replicaCount: 1\nimage:\n  tag: latest\n  repo: nginx\n",
        templates=[
            {
                "name": "deploy.yaml",
                "kind": "Deployment",
                "content": (
                    "replicas: {{ .Values.replicaCount }}\n"
                    "image: {{ .Values.image.repo }}:{{ .Values.image.tag }}\n"
                ),
            }
        ],
    )


@pytest.fixture
def installed_release(mgr: HelmManager, simple_chart: Chart) -> Release:
    return mgr.install(simple_chart.name, "my-release", namespace="staging")


# ─── Helper: _render ──────────────────────────────────────────────────────────

class TestRenderHelper:
    def test_simple_substitution(self):
        out = _render("value: {{ .Values.foo }}", {"foo": "bar"})
        assert out == "value: bar"

    def test_nested_key(self):
        out = _render("img: {{ .Values.image.tag }}", {"image": {"tag": "1.2.3"}})
        assert out == "img: 1.2.3"

    def test_deeply_nested_key(self):
        out = _render(
            "cpu: {{ .Values.resources.requests.cpu }}",
            {"resources": {"requests": {"cpu": "500m"}}},
        )
        assert out == "cpu: 500m"

    def test_missing_key_renders_empty_string(self):
        out = _render("x: {{ .Values.nonexistent }}", {})
        assert out == "x: "

    def test_multiple_placeholders_on_one_line(self):
        out = _render(
            "{{ .Values.repo }}:{{ .Values.tag }}",
            {"repo": "nginx", "tag": "1.25"},
        )
        assert out == "nginx:1.25"

    def test_whitespace_variants_in_placeholder(self):
        out = _render("v: {{  .Values.x  }}", {"x": "hello"})
        assert out == "v: hello"

    def test_integer_value_rendered_as_string(self):
        out = _render("replicas: {{ .Values.count }}", {"count": 3})
        assert out == "replicas: 3"


# ─── Helper: _merge ───────────────────────────────────────────────────────────

class TestMergeHelper:
    def test_override_scalar(self):
        m = _merge("foo: 1\nbar: 2\n", {"foo": 99})
        assert m["foo"] == 99
        assert m["bar"] == 2

    def test_deep_merge_preserves_sibling(self):
        m = _merge("image:\n  tag: old\n  repo: nginx\n", {"image": {"tag": "new"}})
        assert m["image"]["tag"] == "new"
        assert m["image"]["repo"] == "nginx"

    def test_none_override_returns_base(self):
        m = _merge("foo: 1\n", None)
        assert m == {"foo": 1}

    def test_yaml_string_override(self):
        m = _merge("a: 1\n", "a: 2\nb: 3\n")
        assert m["a"] == 2
        assert m["b"] == 3

    def test_deep_merge_function_directly(self):
        result = _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"x": 99}})
        assert result["a"]["x"] == 99
        assert result["a"]["y"] == 2

    def test_new_keys_from_override_added(self):
        m = _merge("foo: 1\n", {"bar": 2})
        assert m["foo"] == 1
        assert m["bar"] == 2


# ─── Chart CRUD ───────────────────────────────────────────────────────────────

class TestChartCreation:
    def test_create_returns_chart_instance(self, mgr):
        c = mgr.create_chart("test", "0.1.0", "Desc", "key: val\n", [])
        assert isinstance(c, Chart)
        assert c.name == "test"
        assert c.version == "0.1.0"
        assert c.id  # non-empty UUID

    def test_chart_persisted_and_retrievable(self, mgr, simple_chart):
        charts = mgr.list_charts()
        assert any(c.id == simple_chart.id for c in charts)

    def test_list_includes_all_three_builtins(self, mgr):
        names = {c.name for c in mgr.list_charts()}
        assert "nginx-deployment" in names
        assert "postgres-statefulset" in names
        assert "redis-deployment" in names

    def test_builtin_nginx_has_three_templates(self, mgr):
        nginx = next(c for c in mgr.list_charts() if c.name == "nginx-deployment")
        assert len(nginx.templates) == 3
        kinds = {t.kind for t in nginx.templates}
        assert "Deployment" in kinds
        assert "Service" in kinds
        assert "HPA" in kinds

    def test_builtin_postgres_has_pvc_template(self, mgr):
        pg = next(c for c in mgr.list_charts() if c.name == "postgres-statefulset")
        kinds = {t.kind for t in pg.templates}
        assert "PVC" in kinds

    def test_builtin_redis_has_configmap(self, mgr):
        redis = next(c for c in mgr.list_charts() if c.name == "redis-deployment")
        kinds = {t.kind for t in redis.templates}
        assert "ConfigMap" in kinds

    def test_chart_keywords_persisted(self, mgr):
        c = mgr.create_chart("kw-chart", "1.0.0", "", "", [], keywords=["a", "b"])
        fetched = next(x for x in mgr.list_charts() if x.id == c.id)
        assert "a" in fetched.keywords
        assert "b" in fetched.keywords

    def test_chart_maintainers_persisted(self, mgr):
        maint = [{"name": "Alice", "email": "alice@example.com"}]
        c = mgr.create_chart("maint-chart", "1.0.0", "", "", [], maintainers=maint)
        fetched = next(x for x in mgr.list_charts() if x.id == c.id)
        assert fetched.maintainers[0]["name"] == "Alice"


# ─── Release Install ──────────────────────────────────────────────────────────

class TestInstall:
    def test_install_by_chart_name(self, mgr, simple_chart):
        rel = mgr.install(simple_chart.name, "rel-1", namespace="default")
        assert rel.name == "rel-1"
        assert rel.namespace == "default"
        assert rel.status == "deployed"
        assert rel.revision == 1

    def test_install_by_chart_id(self, mgr, simple_chart):
        rel = mgr.install(simple_chart.id, "rel-by-id")
        assert rel.chart_id == simple_chart.id

    def test_install_with_values_override(self, mgr, simple_chart):
        rel = mgr.install(simple_chart.name, "rel-vals", values={"replicaCount": 5})
        vals = yaml.safe_load(rel.values_override)
        assert vals["replicaCount"] == 5

    def test_install_unknown_chart_raises(self, mgr):
        with pytest.raises(ValueError, match="Chart not found"):
            mgr.install("does-not-exist", "bad-release")

    def test_install_creates_initial_history_entry(self, mgr, simple_chart):
        rel = mgr.install(simple_chart.name, "hist-rel")
        assert len(rel.history) == 1
        assert rel.history[0].revision == 1
        assert rel.history[0].status == "deployed"

    def test_release_appears_in_list(self, mgr, installed_release):
        releases = mgr.list_releases()
        assert any(r.id == installed_release.id for r in releases)

    def test_release_filtered_by_namespace(self, mgr, simple_chart):
        mgr.install(simple_chart.name, "rel-ns-a", namespace="ns-a")
        mgr.install(simple_chart.name, "rel-ns-b", namespace="ns-b")
        ns_a = mgr.list_releases(namespace="ns-a")
        assert all(r.namespace == "ns-a" for r in ns_a)
        assert len(ns_a) == 1

    def test_get_release_by_id(self, mgr, installed_release):
        fetched = mgr.get_release(installed_release.id)
        assert fetched is not None
        assert fetched.id == installed_release.id

    def test_get_nonexistent_release_returns_none(self, mgr):
        assert mgr.get_release("no-such-id") is None


# ─── Upgrade ──────────────────────────────────────────────────────────────────

class TestUpgrade:
    def test_upgrade_increments_revision(self, mgr, installed_release):
        updated = mgr.upgrade(installed_release.id, {"replicaCount": 3})
        assert updated.revision == 2

    def test_upgrade_updates_values(self, mgr, installed_release):
        updated = mgr.upgrade(installed_release.id, {"replicaCount": 7})
        vals = yaml.safe_load(updated.values_override)
        assert vals["replicaCount"] == 7

    def test_upgrade_appends_to_history(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        mgr.upgrade(installed_release.id, {"replicaCount": 3})
        history = mgr.get_history(installed_release.id)
        assert len(history) == 3  # initial install + 2 upgrades

    def test_previous_revision_marked_superseded(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        history = mgr.get_history(installed_release.id)
        assert history[0].status == "superseded"

    def test_upgrade_unknown_release_raises(self, mgr):
        with pytest.raises(ValueError, match="Release not found"):
            mgr.upgrade("nonexistent-id", {})

    def test_upgrade_status_remains_deployed(self, mgr, installed_release):
        updated = mgr.upgrade(installed_release.id, {"replicaCount": 2})
        assert updated.status == "deployed"


# ─── Rollback ─────────────────────────────────────────────────────────────────

class TestRollback:
    def test_rollback_creates_new_revision(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 3})
        rolled = mgr.rollback(installed_release.id, revision=1)
        assert rolled.revision == 3  # rev 2 after upgrade, then 3 after rollback

    def test_rollback_restores_target_values(self, mgr, installed_release):
        original_override = installed_release.values_override
        mgr.upgrade(installed_release.id, {"replicaCount": 99})
        rolled = mgr.rollback(installed_release.id, revision=1)
        assert rolled.values_override == original_override

    def test_rollback_status_is_deployed(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        rolled = mgr.rollback(installed_release.id, revision=1)
        assert rolled.status == "deployed"

    def test_rollback_invalid_revision_raises(self, mgr, installed_release):
        with pytest.raises(ValueError, match="Revision 99 not found"):
            mgr.rollback(installed_release.id, revision=99)

    def test_rollback_adds_history_entry(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        mgr.rollback(installed_release.id, revision=1)
        history = mgr.get_history(installed_release.id)
        notes = [h.notes for h in history]
        assert any("Rollback" in n for n in notes)


# ─── Uninstall ────────────────────────────────────────────────────────────────

class TestUninstall:
    def test_uninstall_returns_true(self, mgr, installed_release):
        assert mgr.uninstall(installed_release.id) is True

    def test_uninstalled_release_absent_from_list(self, mgr, installed_release):
        mgr.uninstall(installed_release.id)
        active = mgr.list_releases()
        assert not any(r.id == installed_release.id for r in active)

    def test_uninstall_nonexistent_returns_false(self, mgr):
        assert mgr.uninstall("does-not-exist") is False

    def test_uninstall_marks_last_revision(self, mgr, installed_release):
        mgr.uninstall(installed_release.id)
        # Release should have status uninstalling
        row = mgr._conn.execute(
            "SELECT status FROM releases WHERE id=?", (installed_release.id,)
        ).fetchone()
        assert row["status"] == "uninstalling"


# ─── Template Rendering ───────────────────────────────────────────────────────

class TestRenderTemplates:
    def test_render_substitutes_values(self, mgr, simple_chart):
        rendered = mgr.render_templates(simple_chart.id, {"replicaCount": 5})
        assert len(rendered) == 1
        assert "5" in rendered[0]["rendered"]

    def test_render_uses_chart_defaults_when_no_override(self, mgr, simple_chart):
        rendered = mgr.render_templates(simple_chart.id)
        assert "1" in rendered[0]["rendered"]  # replicaCount default = 1

    def test_render_merges_override_with_defaults(self, mgr, simple_chart):
        # Override replicaCount only; image.tag should remain "latest"
        rendered = mgr.render_templates(simple_chart.id, {"replicaCount": 3})
        assert "latest" in rendered[0]["rendered"]
        assert "3" in rendered[0]["rendered"]

    def test_render_returns_name_and_kind(self, mgr, simple_chart):
        rendered = mgr.render_templates(simple_chart.id)
        assert rendered[0]["name"] == "deploy.yaml"
        assert rendered[0]["kind"] == "Deployment"

    def test_render_unknown_chart_raises(self, mgr):
        with pytest.raises(ValueError, match="Chart not found"):
            mgr.render_templates("no-such-chart")

    def test_render_nginx_deployment_with_replica_count(self, mgr):
        rendered = mgr.render_templates("nginx-deployment", {"replicaCount": 4})
        deploy = next(r for r in rendered if r["name"] == "deployment.yaml")
        assert "replicas: 4" in deploy["rendered"]

    def test_render_sets_fullname_override_default(self, mgr, simple_chart):
        rendered = mgr.render_templates(simple_chart.id)
        # fullnameOverride defaults to chart name; no unresolved placeholders
        for item in rendered:
            assert "{{ .Values." not in item["rendered"]


# ─── Export ───────────────────────────────────────────────────────────────────

class TestExport:
    def test_export_creates_chart_yaml(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        assert (path / "Chart.yaml").exists()

    def test_export_creates_values_yaml(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        assert (path / "values.yaml").exists()

    def test_export_creates_templates_dir(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        assert (path / "templates").is_dir()

    def test_export_writes_template_files(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        assert (path / "templates" / "deploy.yaml").exists()

    def test_export_chart_yaml_contains_metadata(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        meta = yaml.safe_load((path / "Chart.yaml").read_text())
        assert meta["name"] == simple_chart.name
        assert meta["version"] == simple_chart.version
        assert meta["apiVersion"] == "v2"

    def test_export_unknown_chart_raises(self, mgr):
        with pytest.raises(ValueError, match="Chart not found"):
            mgr.export_helm_chart("no-such-chart")

    def test_export_helmignore_exists(self, mgr, simple_chart):
        path = Path(mgr.export_helm_chart(simple_chart.id))
        assert (path / ".helmignore").exists()


# ─── Diff ─────────────────────────────────────────────────────────────────────

class TestDiff:
    def test_diff_shows_changed_value(self, mgr, installed_release):
        result = mgr.diff(installed_release.id, {"replicaCount": 99})
        assert result != "No changes detected."
        assert "99" in result

    def test_diff_no_changes(self, mgr, installed_release):
        result = mgr.diff(installed_release.id, {})
        assert result == "No changes detected."

    def test_diff_unknown_release_raises(self, mgr):
        with pytest.raises(ValueError, match="Release not found"):
            mgr.diff("nonexistent", {})

    def test_diff_output_is_unified_format(self, mgr, installed_release):
        result = mgr.diff(installed_release.id, {"replicaCount": 5})
        assert "---" in result or "+++" in result


# ─── History ──────────────────────────────────────────────────────────────────

class TestHistory:
    def test_history_grows_with_upgrades(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        mgr.upgrade(installed_release.id, {"replicaCount": 3})
        history = mgr.get_history(installed_release.id)
        assert len(history) == 3

    def test_history_unknown_release_raises(self, mgr):
        with pytest.raises(ValueError, match="Release not found"):
            mgr.get_history("nonexistent")

    def test_history_revisions_are_sequential(self, mgr, installed_release):
        mgr.upgrade(installed_release.id, {"replicaCount": 2})
        history = mgr.get_history(installed_release.id)
        revs = [h.revision for h in history]
        assert revs == sorted(revs)

    def test_history_contains_revision_revision_objects(self, mgr, installed_release):
        history = mgr.get_history(installed_release.id)
        assert all(isinstance(h, ReleaseRevision) for h in history)


# ─── Context Manager ──────────────────────────────────────────────────────────

class TestContextManager:
    def test_context_manager_closes_connection(self, tmp_path):
        with HelmManager(db_path=tmp_path / "cm.db") as m:
            c = m.create_chart("ctx-chart", "1.0.0", "", "", [])
            assert c.id
        # Connection closed; subsequent use should raise
        import pytest
        with pytest.raises(Exception):
            m.list_charts()
