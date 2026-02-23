#!/usr/bin/env python3
"""
BlackRoad Helm Chart Manager
============================
Production-grade Helm chart lifecycle management in pure Python.

Features:
  * Chart creation with {{ .Values.x }} template rendering
  * Release install / upgrade / rollback / uninstall
  * Persistent SQLite backend at ~/.blackroad/helm-manager.db
  * Unified diff between current and proposed release values
  * Helm-compatible chart export (Chart.yaml + values.yaml + templates/)
  * Rich terminal UI with tables and syntax highlighting
  * 3 built-in charts: nginx-deployment, postgres-statefulset, redis-deployment

CLI Usage:
  helm-manager chart list
  helm-manager chart create myapp 1.0.0
  helm-manager install nginx-deployment my-nginx -n production
  helm-manager upgrade <release-id> --set replicaCount=5
  helm-manager rollback <release-id> 1
  helm-manager list -n production
  helm-manager history <release-id>
  helm-manager render nginx-deployment --set replicaCount=3
  helm-manager export nginx-deployment
  helm-manager diff <release-id> --set replicaCount=5
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

# ─── Constants ────────────────────────────────────────────────────────────────

DB_PATH = Path.home() / ".blackroad" / "helm-manager.db"
APP_VERSION = "1.0.0"

console = Console()
app = typer.Typer(
    name="helm-manager",
    help="[bold cyan]BlackRoad Helm Chart Manager[/] – production-grade lifecycle tool",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
chart_app = typer.Typer(help="Chart management commands", no_args_is_help=True)
app.add_typer(chart_app, name="chart")


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class ChartTemplate:
    """A single Kubernetes manifest template within a chart."""

    name: str
    kind: str    # Deployment | Service | ConfigMap | Ingress | HPA | PVC | ServiceAccount
    content: str # YAML string; may contain {{ .Values.key }} placeholders


@dataclass
class Chart:
    """Helm chart descriptor with metadata and templates."""

    id: str
    name: str
    version: str          # semver e.g. "1.0.0"
    description: str
    app_version: str
    keywords: list[str]
    maintainers: list[dict]
    values_yaml: str      # YAML string of default values
    templates: list[ChartTemplate]
    created_at: str
    home_url: str = ""
    icon_url: str = ""


@dataclass
class ReleaseRevision:
    """An immutable snapshot of a release at a point in time."""

    revision: int
    chart_version: str
    values_override: str  # YAML
    applied_at: str
    status: str           # deployed | superseded | failed | uninstalled
    notes: str = ""


@dataclass
class Release:
    """A deployed instance of a chart in a Kubernetes namespace."""

    id: str
    name: str
    chart_id: str
    namespace: str
    values_override: str  # YAML
    status: str           # deployed | failed | pending | superseded | uninstalling
    installed_at: str
    updated_at: str
    revision: int
    history: list[ReleaseRevision] = field(default_factory=list)


# ─── Template rendering ───────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{\{\s*\.Values\.([\w.]+)\s*\}\}")


def _render(content: str, values: dict) -> str:
    """Substitute {{ .Values.key }} and {{ .Values.a.b.c }} placeholders."""

    def _lookup(keys: list[str], d: dict) -> Any:
        cur: Any = d
        for k in keys:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(k, "")
        return "" if cur is None else cur

    return _PLACEHOLDER.sub(
        lambda m: str(_lookup(m.group(1).split("."), values)),
        content,
    )


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _merge(base_yaml: str, override: "dict | str | None") -> dict:
    """Parse base_yaml and deep-merge with override dict or YAML string."""
    base: dict = yaml.safe_load(base_yaml) or {}
    if override is None:
        return base
    if isinstance(override, str):
        override = yaml.safe_load(override) or {}
    return _deep_merge(base, override)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Built-in chart: nginx-deployment ────────────────────────────────────────

_NGINX_VALUES = (
    "replicaCount: 2\n"
    "image:\n"
    "  repository: nginx\n"
    '  tag: "1.25"\n'
    "  pullPolicy: IfNotPresent\n"
    "service:\n"
    "  type: ClusterIP\n"
    "  port: 80\n"
    "  targetPort: 80\n"
    "ingress:\n"
    "  enabled: false\n"
    "  host: chart.example.com\n"
    "resources:\n"
    "  requests:\n"
    '    cpu: "100m"\n'
    '    memory: "128Mi"\n'
    "  limits:\n"
    '    cpu: "500m"\n'
    '    memory: "256Mi"\n'
    "autoscaling:\n"
    "  enabled: false\n"
    "  minReplicas: 2\n"
    "  maxReplicas: 10\n"
    "  targetCPUUtilizationPercentage: 80\n"
)

_NGINX_DEPLOY = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "    chart: nginx-deployment\n"
    "spec:\n"
    "  replicas: {{ .Values.replicaCount }}\n"
    "  selector:\n"
    "    matchLabels:\n"
    "      app: {{ .Values.fullnameOverride }}\n"
    "  template:\n"
    "    metadata:\n"
    "      labels:\n"
    "        app: {{ .Values.fullnameOverride }}\n"
    "    spec:\n"
    "      containers:\n"
    "        - name: nginx\n"
    "          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
    "          imagePullPolicy: {{ .Values.image.pullPolicy }}\n"
    "          ports:\n"
    "            - name: http\n"
    "              containerPort: {{ .Values.service.targetPort }}\n"
    "              protocol: TCP\n"
    "          resources:\n"
    "            requests:\n"
    "              cpu: {{ .Values.resources.requests.cpu }}\n"
    "              memory: {{ .Values.resources.requests.memory }}\n"
    "            limits:\n"
    "              cpu: {{ .Values.resources.limits.cpu }}\n"
    "              memory: {{ .Values.resources.limits.memory }}\n"
    "          livenessProbe:\n"
    "            httpGet:\n"
    "              path: /\n"
    "              port: http\n"
    "            initialDelaySeconds: 10\n"
    "            periodSeconds: 15\n"
    "          readinessProbe:\n"
    "            httpGet:\n"
    "              path: /\n"
    "              port: http\n"
    "            initialDelaySeconds: 5\n"
    "            periodSeconds: 10\n"
)

_NGINX_SVC = (
    "apiVersion: v1\n"
    "kind: Service\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-svc\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "spec:\n"
    "  type: {{ .Values.service.type }}\n"
    "  selector:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "  ports:\n"
    "    - name: http\n"
    "      port: {{ .Values.service.port }}\n"
    "      targetPort: {{ .Values.service.targetPort }}\n"
    "      protocol: TCP\n"
)

_NGINX_HPA = (
    "apiVersion: autoscaling/v2\n"
    "kind: HorizontalPodAutoscaler\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-hpa\n"
    "  namespace: {{ .Values.namespace }}\n"
    "spec:\n"
    "  scaleTargetRef:\n"
    "    apiVersion: apps/v1\n"
    "    kind: Deployment\n"
    "    name: {{ .Values.fullnameOverride }}\n"
    "  minReplicas: {{ .Values.autoscaling.minReplicas }}\n"
    "  maxReplicas: {{ .Values.autoscaling.maxReplicas }}\n"
    "  metrics:\n"
    "    - type: Resource\n"
    "      resource:\n"
    "        name: cpu\n"
    "        target:\n"
    "          type: Utilization\n"
    "          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}\n"
)

# ─── Built-in chart: postgres-statefulset ────────────────────────────────────

_PG_VALUES = (
    "image:\n"
    "  repository: postgres\n"
    '  tag: "16"\n'
    "  pullPolicy: IfNotPresent\n"
    "auth:\n"
    "  database: appdb\n"
    "  username: pguser\n"
    "  password: changeme\n"
    "persistence:\n"
    "  enabled: true\n"
    "  size: 10Gi\n"
    "  storageClass: standard\n"
    "service:\n"
    "  port: 5432\n"
    "resources:\n"
    "  requests:\n"
    '    cpu: "250m"\n'
    '    memory: "256Mi"\n'
    "  limits:\n"
    '    cpu: "1000m"\n'
    '    memory: "1Gi"\n'
)

_PG_STATEFULSET = (
    "apiVersion: apps/v1\n"
    "kind: StatefulSet\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "    chart: postgres-statefulset\n"
    "spec:\n"
    "  serviceName: {{ .Values.fullnameOverride }}\n"
    "  replicas: 1\n"
    "  selector:\n"
    "    matchLabels:\n"
    "      app: {{ .Values.fullnameOverride }}\n"
    "  template:\n"
    "    metadata:\n"
    "      labels:\n"
    "        app: {{ .Values.fullnameOverride }}\n"
    "    spec:\n"
    "      securityContext:\n"
    "        runAsUser: 999\n"
    "        fsGroup: 999\n"
    "      containers:\n"
    "        - name: postgres\n"
    "          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
    "          imagePullPolicy: {{ .Values.image.pullPolicy }}\n"
    "          env:\n"
    "            - name: POSTGRES_DB\n"
    "              value: {{ .Values.auth.database }}\n"
    "            - name: POSTGRES_USER\n"
    "              value: {{ .Values.auth.username }}\n"
    "            - name: POSTGRES_PASSWORD\n"
    "              value: {{ .Values.auth.password }}\n"
    "            - name: PGDATA\n"
    "              value: /var/lib/postgresql/data/pgdata\n"
    "          ports:\n"
    "            - name: postgres\n"
    "              containerPort: {{ .Values.service.port }}\n"
    "          volumeMounts:\n"
    "            - name: data\n"
    "              mountPath: /var/lib/postgresql/data\n"
    "          resources:\n"
    "            requests:\n"
    "              cpu: {{ .Values.resources.requests.cpu }}\n"
    "              memory: {{ .Values.resources.requests.memory }}\n"
    "            limits:\n"
    "              cpu: {{ .Values.resources.limits.cpu }}\n"
    "              memory: {{ .Values.resources.limits.memory }}\n"
    "          readinessProbe:\n"
    "            exec:\n"
    "              command: [pg_isready, -U, postgres]\n"
    "            initialDelaySeconds: 5\n"
    "            periodSeconds: 10\n"
    "  volumeClaimTemplates:\n"
    "    - metadata:\n"
    "        name: data\n"
    "      spec:\n"
    "        accessModes: [ReadWriteOnce]\n"
    "        storageClassName: {{ .Values.persistence.storageClass }}\n"
    "        resources:\n"
    "          requests:\n"
    "            storage: {{ .Values.persistence.size }}\n"
)

_PG_SVC = (
    "apiVersion: v1\n"
    "kind: Service\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-svc\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "spec:\n"
    "  type: ClusterIP\n"
    "  selector:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "  ports:\n"
    "    - name: postgres\n"
    "      port: {{ .Values.service.port }}\n"
    "      targetPort: {{ .Values.service.port }}\n"
    "      protocol: TCP\n"
)

_PG_PVC = (
    "apiVersion: v1\n"
    "kind: PersistentVolumeClaim\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-pvc\n"
    "  namespace: {{ .Values.namespace }}\n"
    "spec:\n"
    "  accessModes:\n"
    "    - ReadWriteOnce\n"
    "  storageClassName: {{ .Values.persistence.storageClass }}\n"
    "  resources:\n"
    "    requests:\n"
    "      storage: {{ .Values.persistence.size }}\n"
)

# ─── Built-in chart: redis-deployment ────────────────────────────────────────

_REDIS_VALUES = (
    "image:\n"
    "  repository: redis\n"
    '  tag: "7.2"\n'
    "  pullPolicy: IfNotPresent\n"
    "replicaCount: 1\n"
    "service:\n"
    "  port: 6379\n"
    "auth:\n"
    "  enabled: false\n"
    '  password: ""\n'
    "persistence:\n"
    "  enabled: true\n"
    "  size: 2Gi\n"
    "  storageClass: standard\n"
    "resources:\n"
    "  requests:\n"
    '    cpu: "100m"\n'
    '    memory: "128Mi"\n'
    "  limits:\n"
    '    cpu: "500m"\n'
    '    memory: "512Mi"\n'
)

_REDIS_DEPLOY = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "    chart: redis-deployment\n"
    "spec:\n"
    "  replicas: {{ .Values.replicaCount }}\n"
    "  selector:\n"
    "    matchLabels:\n"
    "      app: {{ .Values.fullnameOverride }}\n"
    "  template:\n"
    "    metadata:\n"
    "      labels:\n"
    "        app: {{ .Values.fullnameOverride }}\n"
    "    spec:\n"
    "      containers:\n"
    "        - name: redis\n"
    "          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
    "          imagePullPolicy: {{ .Values.image.pullPolicy }}\n"
    "          command: [redis-server, /etc/redis/redis.conf]\n"
    "          ports:\n"
    "            - name: redis\n"
    "              containerPort: {{ .Values.service.port }}\n"
    "          volumeMounts:\n"
    "            - name: config\n"
    "              mountPath: /etc/redis\n"
    "          resources:\n"
    "            requests:\n"
    "              cpu: {{ .Values.resources.requests.cpu }}\n"
    "              memory: {{ .Values.resources.requests.memory }}\n"
    "            limits:\n"
    "              cpu: {{ .Values.resources.limits.cpu }}\n"
    "              memory: {{ .Values.resources.limits.memory }}\n"
    "          livenessProbe:\n"
    "            exec:\n"
    "              command: [redis-cli, ping]\n"
    "            initialDelaySeconds: 10\n"
    "            periodSeconds: 15\n"
    "      volumes:\n"
    "        - name: config\n"
    "          configMap:\n"
    "            name: {{ .Values.fullnameOverride }}-config\n"
)

_REDIS_SVC = (
    "apiVersion: v1\n"
    "kind: Service\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-svc\n"
    "  namespace: {{ .Values.namespace }}\n"
    "  labels:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "spec:\n"
    "  type: ClusterIP\n"
    "  selector:\n"
    "    app: {{ .Values.fullnameOverride }}\n"
    "  ports:\n"
    "    - name: redis\n"
    "      port: {{ .Values.service.port }}\n"
    "      targetPort: {{ .Values.service.port }}\n"
    "      protocol: TCP\n"
)

_REDIS_CM = (
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: {{ .Values.fullnameOverride }}-config\n"
    "  namespace: {{ .Values.namespace }}\n"
    "data:\n"
    "  redis.conf: |\n"
    "    maxmemory 256mb\n"
    "    maxmemory-policy allkeys-lru\n"
    "    save 900 1\n"
    "    save 300 10\n"
    "    save 60 10000\n"
    "    loglevel notice\n"
)


# ─── HelmManager ──────────────────────────────────────────────────────────────

class HelmManager:
    """SQLite-backed Helm-compatible chart and release manager.

    All state is persisted to a local SQLite database at ``db_path``
    (default: ``~/.blackroad/helm-manager.db``).

    Example::

        with HelmManager() as mgr:
            mgr.install("nginx-deployment", "my-nginx", namespace="production")
    """

    SCHEMA_VERSION = 1

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._seed()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS charts (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                version      TEXT NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                app_version  TEXT NOT NULL DEFAULT '',
                keywords     TEXT NOT NULL DEFAULT '[]',
                maintainers  TEXT NOT NULL DEFAULT '[]',
                values_yaml  TEXT NOT NULL DEFAULT '',
                templates    TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                home_url     TEXT NOT NULL DEFAULT '',
                icon_url     TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS releases (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                chart_id        TEXT NOT NULL,
                namespace       TEXT NOT NULL DEFAULT 'default',
                values_override TEXT NOT NULL DEFAULT '{}',
                status          TEXT NOT NULL DEFAULT 'pending',
                installed_at    TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                revision        INTEGER NOT NULL DEFAULT 1,
                history         TEXT NOT NULL DEFAULT '[]'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_chart_name_version
                ON charts(name, version);
            CREATE INDEX IF NOT EXISTS idx_release_namespace
                ON releases(namespace);
            CREATE INDEX IF NOT EXISTS idx_release_status
                ON releases(status);
        """)
        self._conn.commit()

    # ── Seeding ────────────────────────────────────────────────────────────────

    def _seed(self) -> None:
        """Insert the three built-in charts on first run."""
        builtins = [
            {
                "name": "nginx-deployment",
                "version": "1.0.0",
                "description": "Production-ready NGINX web server with HPA and health probes",
                "app_version": "1.25",
                "keywords": ["nginx", "web", "http", "proxy"],
                "maintainers": [{"name": "BlackRoad OS", "email": "helm@blackroad.ai"}],
                "values_yaml": _NGINX_VALUES,
                "home_url": "https://nginx.org",
                "icon_url": "https://raw.githubusercontent.com/cncf/artwork/main/projects/nginx/icon/color/nginx-icon-color.svg",
                "templates": [
                    {"name": "deployment.yaml", "kind": "Deployment", "content": _NGINX_DEPLOY},
                    {"name": "service.yaml",    "kind": "Service",    "content": _NGINX_SVC},
                    {"name": "hpa.yaml",        "kind": "HPA",        "content": _NGINX_HPA},
                ],
            },
            {
                "name": "postgres-statefulset",
                "version": "1.0.0",
                "description": "PostgreSQL 16 StatefulSet with persistent storage and security context",
                "app_version": "16",
                "keywords": ["postgres", "postgresql", "database", "sql"],
                "maintainers": [{"name": "BlackRoad OS", "email": "helm@blackroad.ai"}],
                "values_yaml": _PG_VALUES,
                "home_url": "https://www.postgresql.org",
                "icon_url": "",
                "templates": [
                    {"name": "statefulset.yaml", "kind": "Deployment", "content": _PG_STATEFULSET},
                    {"name": "service.yaml",     "kind": "Service",    "content": _PG_SVC},
                    {"name": "pvc.yaml",         "kind": "PVC",        "content": _PG_PVC},
                ],
            },
            {
                "name": "redis-deployment",
                "version": "1.0.0",
                "description": "Redis 7.2 in-memory data store with ConfigMap-driven configuration",
                "app_version": "7.2",
                "keywords": ["redis", "cache", "nosql", "key-value"],
                "maintainers": [{"name": "BlackRoad OS", "email": "helm@blackroad.ai"}],
                "values_yaml": _REDIS_VALUES,
                "home_url": "https://redis.io",
                "icon_url": "",
                "templates": [
                    {"name": "deployment.yaml", "kind": "Deployment", "content": _REDIS_DEPLOY},
                    {"name": "service.yaml",    "kind": "Service",    "content": _REDIS_SVC},
                    {"name": "configmap.yaml",  "kind": "ConfigMap",  "content": _REDIS_CM},
                ],
            },
        ]
        for spec in builtins:
            exists = self._conn.execute(
                "SELECT id FROM charts WHERE name=? AND version=?",
                (spec["name"], spec["version"]),
            ).fetchone()
            if not exists:
                self.create_chart(
                    name=spec["name"],
                    version=spec["version"],
                    description=spec["description"],
                    values_yaml=spec["values_yaml"],
                    templates=spec["templates"],
                    app_version=spec["app_version"],
                    keywords=spec["keywords"],
                    maintainers=spec["maintainers"],
                    home_url=spec["home_url"],
                    icon_url=spec["icon_url"],
                )

    # ── Chart CRUD ─────────────────────────────────────────────────────────────

    def create_chart(
        self,
        name: str,
        version: str,
        description: str,
        values_yaml: str,
        templates: list[dict],
        *,
        app_version: str = "",
        keywords: Optional[list[str]] = None,
        maintainers: Optional[list[dict]] = None,
        home_url: str = "",
        icon_url: str = "",
    ) -> Chart:
        """Create and persist a new chart, returning its descriptor."""
        cid = str(uuid.uuid4())
        now = _now()
        tpl_list = [ChartTemplate(**t) for t in templates]
        self._conn.execute(
            """INSERT INTO charts
               (id, name, version, description, app_version, keywords, maintainers,
                values_yaml, templates, created_at, home_url, icon_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid, name, version, description, app_version or version,
                json.dumps(keywords or []),
                json.dumps(maintainers or []),
                values_yaml,
                json.dumps([{"name": t.name, "kind": t.kind, "content": t.content} for t in tpl_list]),
                now, home_url, icon_url,
            ),
        )
        self._conn.commit()
        return Chart(
            id=cid, name=name, version=version, description=description,
            app_version=app_version or version,
            keywords=keywords or [], maintainers=maintainers or [],
            values_yaml=values_yaml, templates=tpl_list,
            created_at=now, home_url=home_url, icon_url=icon_url,
        )

    def list_charts(self) -> list[Chart]:
        """Return all registered charts ordered by name then version."""
        return [
            self._row_to_chart(r)
            for r in self._conn.execute(
                "SELECT * FROM charts ORDER BY name, version"
            ).fetchall()
        ]

    def _find_chart(self, name_or_id: str) -> Optional[Chart]:
        """Look up by ID first, then by name (latest version)."""
        row = self._conn.execute(
            "SELECT * FROM charts WHERE id=? OR name=? ORDER BY created_at DESC LIMIT 1",
            (name_or_id, name_or_id),
        ).fetchone()
        return self._row_to_chart(row) if row else None

    def _find_chart_version(self, name: str, version: str) -> Optional[Chart]:
        row = self._conn.execute(
            "SELECT * FROM charts WHERE name=? AND version=?", (name, version)
        ).fetchone()
        return self._row_to_chart(row) if row else None

    def _row_to_chart(self, row: sqlite3.Row) -> Chart:
        raw = json.loads(row["templates"])
        return Chart(
            id=row["id"], name=row["name"], version=row["version"],
            description=row["description"], app_version=row["app_version"],
            keywords=json.loads(row["keywords"]),
            maintainers=json.loads(row["maintainers"]),
            values_yaml=row["values_yaml"],
            templates=[ChartTemplate(**t) for t in raw],
            created_at=row["created_at"],
            home_url=row["home_url"], icon_url=row["icon_url"],
        )

    # ── Release Lifecycle ──────────────────────────────────────────────────────

    def install(
        self,
        chart_name_or_id: str,
        release_name: str,
        namespace: str = "default",
        values: Optional[dict] = None,
    ) -> Release:
        """Install a chart as a named release in the given namespace."""
        chart = self._find_chart(chart_name_or_id)
        if chart is None:
            raise ValueError(f"Chart not found: {chart_name_or_id!r}")
        now = _now()
        override_yaml = yaml.dump(values or {})
        rev = ReleaseRevision(
            revision=1,
            chart_version=chart.version,
            values_override=override_yaml,
            applied_at=now,
            status="deployed",
            notes=f"Initial install of {chart.name}:{chart.version}",
        )
        rel = Release(
            id=str(uuid.uuid4()),
            name=release_name,
            chart_id=chart.id,
            namespace=namespace,
            values_override=override_yaml,
            status="deployed",
            installed_at=now,
            updated_at=now,
            revision=1,
            history=[rev],
        )
        self._conn.execute(
            """INSERT INTO releases
               (id, name, chart_id, namespace, values_override, status,
                installed_at, updated_at, revision, history)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel.id, rel.name, rel.chart_id, rel.namespace,
                rel.values_override, rel.status,
                rel.installed_at, rel.updated_at, rel.revision,
                json.dumps([self._rev_to_dict(rev)]),
            ),
        )
        self._conn.commit()
        return rel

    def upgrade(
        self,
        release_id: str,
        new_values: dict,
        chart_version: Optional[str] = None,
    ) -> Release:
        """Upgrade a release to new values or a new chart version.

        Increments the revision counter and archives the previous state
        in history with status ``superseded``.
        """
        rel = self.get_release(release_id)
        if rel is None:
            raise ValueError(f"Release not found: {release_id!r}")
        chart = self._find_chart(rel.chart_id)
        if chart is None:
            raise ValueError(f"Chart {rel.chart_id!r} no longer exists")
        if chart_version:
            versioned = self._find_chart_version(chart.name, chart_version)
            if versioned:
                chart = versioned
        now = _now()
        new_revision = rel.revision + 1
        override_yaml = yaml.dump(new_values)
        history = rel.history
        if history:
            history[-1].status = "superseded"
        new_rev = ReleaseRevision(
            revision=new_revision,
            chart_version=chart.version,
            values_override=override_yaml,
            applied_at=now,
            status="deployed",
            notes=f"Upgrade to {chart.name}:{chart.version} (revision {new_revision})",
        )
        history.append(new_rev)
        self._conn.execute(
            """UPDATE releases
               SET values_override=?, status='deployed', updated_at=?,
                   revision=?, history=?
               WHERE id=?""",
            (
                override_yaml, now, new_revision,
                json.dumps([self._rev_to_dict(r) for r in history]),
                release_id,
            ),
        )
        self._conn.commit()
        rel.values_override = override_yaml
        rel.status = "deployed"
        rel.updated_at = now
        rel.revision = new_revision
        rel.history = history
        return rel

    def rollback(self, release_id: str, revision: int) -> Release:
        """Roll back a release to a previously deployed revision.

        Creates a new revision entry mirroring the target revision's
        values and chart version. The current head is marked ``superseded``.
        """
        rel = self.get_release(release_id)
        if rel is None:
            raise ValueError(f"Release not found: {release_id!r}")
        target = next((r for r in rel.history if r.revision == revision), None)
        if target is None:
            available = [r.revision for r in rel.history]
            raise ValueError(
                f"Revision {revision} not found in release {release_id!r}. "
                f"Available revisions: {available}"
            )
        now = _now()
        new_revision = rel.revision + 1
        history = rel.history
        if history:
            history[-1].status = "superseded"
        rollback_rev = ReleaseRevision(
            revision=new_revision,
            chart_version=target.chart_version,
            values_override=target.values_override,
            applied_at=now,
            status="deployed",
            notes=f"Rollback to revision {revision}",
        )
        history.append(rollback_rev)
        self._conn.execute(
            """UPDATE releases
               SET values_override=?, status='deployed', updated_at=?,
                   revision=?, history=?
               WHERE id=?""",
            (
                target.values_override, now, new_revision,
                json.dumps([self._rev_to_dict(r) for r in history]),
                release_id,
            ),
        )
        self._conn.commit()
        rel.values_override = target.values_override
        rel.status = "deployed"
        rel.updated_at = now
        rel.revision = new_revision
        rel.history = history
        return rel

    def uninstall(self, release_id: str) -> bool:
        """Soft-delete a release by marking it ``uninstalling``.

        Returns ``True`` on success, ``False`` if release not found.
        """
        rel = self.get_release(release_id)
        if rel is None:
            return False
        now = _now()
        history = rel.history
        if history:
            history[-1].status = "uninstalled"
        self._conn.execute(
            """UPDATE releases
               SET status='uninstalling', updated_at=?, history=?
               WHERE id=?""",
            (now, json.dumps([self._rev_to_dict(r) for r in history]), release_id),
        )
        self._conn.commit()
        return True

    def list_releases(self, namespace: Optional[str] = None) -> list[Release]:
        """List active (non-uninstalled) releases, optionally by namespace."""
        if namespace:
            rows = self._conn.execute(
                "SELECT * FROM releases WHERE namespace=? AND status != 'uninstalling' ORDER BY name",
                (namespace,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM releases WHERE status != 'uninstalling' ORDER BY namespace, name"
            ).fetchall()
        return [self._row_to_release(r) for r in rows]

    def get_release(self, release_id: str) -> Optional[Release]:
        """Retrieve a single release by its ID."""
        row = self._conn.execute(
            "SELECT * FROM releases WHERE id=?", (release_id,)
        ).fetchone()
        return self._row_to_release(row) if row else None

    def get_history(self, release_id: str) -> list[ReleaseRevision]:
        """Return the full revision history for a release."""
        rel = self.get_release(release_id)
        if rel is None:
            raise ValueError(f"Release not found: {release_id!r}")
        return rel.history

    # ── Template Rendering ─────────────────────────────────────────────────────

    def render_templates(
        self,
        chart_id: str,
        values: Optional[dict] = None,
    ) -> list[dict]:
        """Render chart templates with merged default + provided values.

        Returns a list of dicts::

            [{"name": "deployment.yaml", "kind": "Deployment", "rendered": "..."}]
        """
        chart = self._find_chart(chart_id)
        if chart is None:
            raise ValueError(f"Chart not found: {chart_id!r}")
        merged = _merge(chart.values_yaml, values)
        merged.setdefault("fullnameOverride", chart.name)
        merged.setdefault("namespace", "default")
        return [
            {
                "name": tpl.name,
                "kind": tpl.kind,
                "rendered": _render(tpl.content, merged),
            }
            for tpl in chart.templates
        ]

    # ── Chart Export ───────────────────────────────────────────────────────────

    def export_helm_chart(self, chart_id: str) -> str:
        """Export a chart as a Helm-compatible directory.

        Structure::

            <tmpdir>/<chart-name>/
                Chart.yaml
                values.yaml
                .helmignore
                templates/
                    <template-name>.yaml

        Returns the path to the exported chart directory.
        """
        chart = self._find_chart(chart_id)
        if chart is None:
            raise ValueError(f"Chart not found: {chart_id!r}")
        tmp = Path(tempfile.mkdtemp(prefix="helm-export-"))
        chart_dir = tmp / chart.name
        tpl_dir = chart_dir / "templates"
        tpl_dir.mkdir(parents=True)
        meta: dict = {
            "apiVersion": "v2",
            "name": chart.name,
            "description": chart.description,
            "version": chart.version,
            "appVersion": chart.app_version,
            "keywords": chart.keywords,
            "maintainers": chart.maintainers,
        }
        if chart.home_url:
            meta["home"] = chart.home_url
        if chart.icon_url:
            meta["icon"] = chart.icon_url
        (chart_dir / "Chart.yaml").write_text(
            yaml.dump(meta, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        (chart_dir / "values.yaml").write_text(chart.values_yaml, encoding="utf-8")
        (chart_dir / ".helmignore").write_text(
            "# Helm ignore file\n*.pyc\n__pycache__/\n.DS_Store\n*.swp\n",
            encoding="utf-8",
        )
        for tpl in chart.templates:
            (tpl_dir / tpl.name).write_text(tpl.content, encoding="utf-8")
        return str(chart_dir)

    # ── Diff ───────────────────────────────────────────────────────────────────

    def diff(self, release_id: str, new_values: dict) -> str:
        """Show a unified diff of effective values: current vs proposed.

        Returns the diff string, or ``"No changes detected."`` if identical.
        """
        rel = self.get_release(release_id)
        if rel is None:
            raise ValueError(f"Release not found: {release_id!r}")
        chart = self._find_chart(rel.chart_id)
        if chart is None:
            raise ValueError(f"Chart {rel.chart_id!r} not found")
        current  = _merge(chart.values_yaml, yaml.safe_load(rel.values_override) or {})
        proposed = _merge(chart.values_yaml, new_values)
        lines_a = yaml.dump(current,  default_flow_style=False).splitlines(keepends=True)
        lines_b = yaml.dump(proposed, default_flow_style=False).splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"current (rev {rel.revision})",
            tofile="proposed",
        ))
        return "".join(diff) if diff else "No changes detected."

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _rev_to_dict(self, r: ReleaseRevision) -> dict:
        return {
            "revision":        r.revision,
            "chart_version":   r.chart_version,
            "values_override": r.values_override,
            "applied_at":      r.applied_at,
            "status":          r.status,
            "notes":           r.notes,
        }

    def _row_to_release(self, row: sqlite3.Row) -> Release:
        history = [
            ReleaseRevision(
                revision=h["revision"],
                chart_version=h["chart_version"],
                values_override=h["values_override"],
                applied_at=h["applied_at"],
                status=h["status"],
                notes=h.get("notes", ""),
            )
            for h in json.loads(row["history"] or "[]")
        ]
        return Release(
            id=row["id"], name=row["name"], chart_id=row["chart_id"],
            namespace=row["namespace"], values_override=row["values_override"],
            status=row["status"], installed_at=row["installed_at"],
            updated_at=row["updated_at"], revision=row["revision"],
            history=history,
        )

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def __enter__(self) -> "HelmManager":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# ─── CLI helpers ──────────────────────────────────────────────────────────────

_manager: Optional[HelmManager] = None


def _mgr() -> HelmManager:
    global _manager
    if _manager is None:
        _manager = HelmManager()
    return _manager


def _parse_set(set_values: list[str]) -> dict:
    """Convert ``["key=val", "a.b=val2"]`` into a nested dict."""
    values: dict = {}
    for kv in set_values:
        key, _, val = kv.partition("=")
        parts = key.strip().split(".")
        node = values
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = val.strip()
    return values


# ─── CLI commands ─────────────────────────────────────────────────────────────

@chart_app.command("create")
def cmd_chart_create(
    name: str = typer.Argument(..., help="Chart name"),
    version: str = typer.Argument(..., help="Semver version (e.g. 1.0.0)"),
    description: str = typer.Option("", "--description", "-d"),
    values_file: Optional[Path] = typer.Option(None, "--values", "-f", help="Path to values.yaml"),
) -> None:
    """Create and register a new chart."""
    values_yaml = values_file.read_text() if values_file else "# Add default values here\n"
    chart = _mgr().create_chart(name, version, description, values_yaml, [])
    console.print(Panel(
        f"[green bold]Chart created[/]\n"
        f"Name:    [cyan]{chart.name}[/]\n"
        f"Version: [yellow]{chart.version}[/]\n"
        f"ID:      [dim]{chart.id}[/]",
        title="helm-manager chart create",
    ))


@chart_app.command("list")
def cmd_chart_list() -> None:
    """List all available charts."""
    charts = _mgr().list_charts()
    if not charts:
        console.print("[yellow]No charts registered.[/]")
        return
    tbl = Table(title="Available Charts", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Name",        style="bold cyan",  no_wrap=True)
    tbl.add_column("Version",     style="green")
    tbl.add_column("App Version", style="dim green")
    tbl.add_column("Description")
    tbl.add_column("Templates",   style="dim", justify="right")
    tbl.add_column("ID",          style="dim")
    for c in charts:
        tbl.add_row(c.name, c.version, c.app_version, c.description,
                    str(len(c.templates)), c.id[:8])
    console.print(tbl)


@app.command("install")
def cmd_install(
    chart: str = typer.Argument(..., help="Chart name or ID"),
    release: str = typer.Argument(..., help="Unique release name"),
    namespace: str = typer.Option("default", "-n", "--namespace"),
    set_vals: list[str] = typer.Option([], "--set", help="key=value overrides"),
) -> None:
    """Install a chart as a named release."""
    rel = _mgr().install(chart, release, namespace, _parse_set(set_vals))
    console.print(Panel(
        f"[green bold]Release installed[/]\n"
        f"Name:      [cyan]{rel.name}[/]\n"
        f"Namespace: [blue]{rel.namespace}[/]\n"
        f"Revision:  [yellow]{rel.revision}[/]\n"
        f"Status:    [green]{rel.status}[/]\n"
        f"ID:        [dim]{rel.id}[/]",
        title="helm-manager install",
    ))


@app.command("upgrade")
def cmd_upgrade(
    release_id: str = typer.Argument(..., help="Release ID"),
    set_vals: list[str] = typer.Option([], "--set", help="key=value overrides"),
    chart_version: Optional[str] = typer.Option(None, "--version"),
) -> None:
    """Upgrade a release with new values or chart version."""
    rel = _mgr().upgrade(release_id, _parse_set(set_vals), chart_version)
    console.print(
        f"[green]Upgraded[/] [bold]{rel.name}[/] → "
        f"revision [yellow]{rel.revision}[/] ([dim]{rel.id[:8]}[/])"
    )


@app.command("rollback")
def cmd_rollback(
    release_id: str = typer.Argument(...),
    revision:   int  = typer.Argument(..., help="Target revision number"),
) -> None:
    """Roll back a release to a previous revision."""
    rel = _mgr().rollback(release_id, revision)
    console.print(
        f"[yellow]Rolled back[/] [bold]{rel.name}[/] to "
        f"revision [yellow]{revision}[/] (now revision [yellow]{rel.revision}[/])"
    )


@app.command("uninstall")
def cmd_uninstall(release_id: str = typer.Argument(...)) -> None:
    """Uninstall (soft-delete) a release."""
    ok = _mgr().uninstall(release_id)
    if ok:
        console.print(f"[red]Uninstalled[/] release [bold]{release_id[:8]}[/]")
    else:
        console.print(f"[bold red]Release not found:[/] {release_id}")
        raise typer.Exit(code=1)


@app.command("list")
def cmd_list(
    namespace: Optional[str] = typer.Option(None, "-n", "--namespace"),
) -> None:
    """List all active releases."""
    releases = _mgr().list_releases(namespace)
    if not releases:
        console.print("[yellow]No active releases found.[/]")
        return
    ns_label = f" in '{namespace}'" if namespace else ""
    tbl = Table(title=f"Active Releases{ns_label}", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Name",      style="bold cyan",  no_wrap=True)
    tbl.add_column("Namespace", style="blue")
    tbl.add_column("Status",    style="green")
    tbl.add_column("Rev",       style="yellow", justify="right")
    tbl.add_column("Updated",   style="dim")
    tbl.add_column("ID",        style="dim")
    for r in releases:
        colour = "green" if r.status == "deployed" else "red"
        tbl.add_row(
            r.name, r.namespace, f"[{colour}]{r.status}[/{colour}]",
            str(r.revision), r.updated_at[:19], r.id[:8],
        )
    console.print(tbl)


@app.command("history")
def cmd_history(release_id: str = typer.Argument(...)) -> None:
    """Show revision history for a release."""
    history = _mgr().get_history(release_id)
    tbl = Table(title=f"History: {release_id[:8]}", box=box.ROUNDED, show_lines=True)
    tbl.add_column("Revision",      style="yellow", justify="right")
    tbl.add_column("Chart Version", style="green")
    tbl.add_column("Status")
    tbl.add_column("Applied At",    style="dim")
    tbl.add_column("Notes")
    for h in history:
        colour = "green" if h.status == "deployed" else "dim"
        tbl.add_row(
            str(h.revision), h.chart_version,
            f"[{colour}]{h.status}[/{colour}]",
            h.applied_at[:19], h.notes,
        )
    console.print(tbl)


@app.command("render")
def cmd_render(
    chart_id: str = typer.Argument(..., help="Chart name or ID"),
    set_vals: list[str] = typer.Option([], "--set", help="key=value overrides"),
) -> None:
    """Render chart templates to stdout."""
    rendered = _mgr().render_templates(chart_id, _parse_set(set_vals))
    for item in rendered:
        console.print(f"\n[bold cyan]--- # {item['name']}  ({item['kind']})[/]")
        console.print(Syntax(item["rendered"], "yaml", theme="monokai", line_numbers=True))


@app.command("export")
def cmd_export(chart_id: str = typer.Argument(..., help="Chart name or ID")) -> None:
    """Export chart as a Helm-compatible directory structure."""
    path = _mgr().export_helm_chart(chart_id)
    console.print(f"[green]Exported to:[/] [bold]{path}[/]")


@app.command("diff")
def cmd_diff(
    release_id: str = typer.Argument(...),
    set_vals: list[str] = typer.Option([], "--set", help="Proposed key=value overrides"),
) -> None:
    """Show diff between current and proposed release values."""
    result = _mgr().diff(release_id, _parse_set(set_vals))
    if result == "No changes detected.":
        console.print("[green]No changes detected.[/]")
    else:
        console.print(Syntax(result, "diff", theme="monokai"))


if __name__ == "__main__":
    app()
