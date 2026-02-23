# 🚢 BlackRoad Helm Chart Manager

[![CI](https://github.com/BlackRoad-OS/blackroad-helm-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/BlackRoad-OS/blackroad-helm-manager/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Production-grade Helm chart lifecycle management in pure Python — no Kubernetes cluster required.

---

## ✨ Features

| Feature | Description |
|---|---|
| 📦 **Chart Registry** | Create, list, and manage versioned Helm charts in a local SQLite store |
| 🚀 **Release Lifecycle** | `install`, `upgrade`, `rollback`, `uninstall` with full audit history |
| 🔁 **Revision History** | Every state change is immutably recorded; roll back to any revision |
| 🎨 **Template Rendering** | `{{ .Values.x }}` and `{{ .Values.nested.key }}` substitution with deep value merging |
| 📊 **Unified Diff** | Preview exactly what a proposed upgrade would change before applying |
| 📁 **Helm Export** | Export any chart as a Helm-compatible directory (`Chart.yaml` + `values.yaml` + `templates/`) |
| 🎯 **3 Built-in Charts** | `nginx-deployment`, `postgres-statefulset`, `redis-deployment` ready to use |
| 💻 **Rich CLI** | Beautiful terminal UI with tables, syntax highlighting, and coloured output |
| 🗄️ **SQLite Persistence** | Zero-config storage at `~/.blackroad/helm-manager.db` |

---

## 🗂️ Built-in Charts

### `nginx-deployment`
NGINX 1.25 Deployment with HPA, Service, health probes, and configurable resources.

**Templates:** `deployment.yaml` · `service.yaml` · `hpa.yaml`

### `postgres-statefulset`
PostgreSQL 16 StatefulSet with persistent volume, security context, readiness probe, and PVC template.

**Templates:** `statefulset.yaml` · `service.yaml` · `pvc.yaml`

### `redis-deployment`
Redis 7.2 Deployment with ConfigMap-driven `redis.conf`, liveness probe, and optional auth.

**Templates:** `deployment.yaml` · `service.yaml` · `configmap.yaml`

---

## 📦 Installation

```bash
git clone https://github.com/BlackRoad-OS/blackroad-helm-manager.git
cd blackroad-helm-manager
pip install -r requirements.txt
```

---

## 🖥️ CLI Usage

### Chart Commands

```bash
# List all registered charts (includes 3 built-ins)
python helm_manager.py chart list

# Create a new chart from a values file
python helm_manager.py chart create myapp 2.1.0 --description "My application" --values values.yaml
```

### Release Lifecycle

```bash
# Install a chart as a named release
python helm_manager.py install nginx-deployment my-nginx -n production

# Install with value overrides
python helm_manager.py install nginx-deployment my-nginx -n production \
    --set replicaCount=3 \
    --set image.tag=1.26

# Upgrade an existing release
python helm_manager.py upgrade <release-id> --set replicaCount=5

# Preview what an upgrade would change (dry-run diff)
python helm_manager.py diff <release-id> --set replicaCount=5

# Roll back to a previous revision
python helm_manager.py rollback <release-id> 1

# List all active releases
python helm_manager.py list

# Filter by namespace
python helm_manager.py list -n production

# Inspect revision history
python helm_manager.py history <release-id>

# Uninstall a release
python helm_manager.py uninstall <release-id>
```

### Template & Export

```bash
# Render chart templates to stdout with value overrides
python helm_manager.py render nginx-deployment --set replicaCount=3

# Export chart as a Helm-compatible directory
python helm_manager.py export nginx-deployment
# → /tmp/helm-export-XXXX/nginx-deployment/
#     Chart.yaml  values.yaml  .helmignore  templates/
```

---

## 🐍 Python API

```python
from helm_manager import HelmManager

with HelmManager() as mgr:
    # ── Charts ─────────────────────────────────────────────────────────────
    charts = mgr.list_charts()

    custom = mgr.create_chart(
        name="my-api",
        version="1.0.0",
        description="My REST API",
        values_yaml="replicaCount: 2\nimage:\n  tag: latest\n",
        templates=[
            {
                "name": "deployment.yaml",
                "kind": "Deployment",
                "content": "replicas: {{ .Values.replicaCount }}\n",
            }
        ],
        keywords=["api", "rest"],
        maintainers=[{"name": "Alice", "email": "alice@example.com"}],
    )

    # ── Releases ────────────────────────────────────────────────────────────
    release = mgr.install(
        "nginx-deployment",
        release_name="web-prod",
        namespace="production",
        values={"replicaCount": 3, "image": {"tag": "1.26"}},
    )

    # Upgrade
    updated = mgr.upgrade(release.id, {"replicaCount": 5})

    # Preview diff before upgrading
    print(mgr.diff(release.id, {"replicaCount": 10}))

    # Roll back to revision 1
    rolled = mgr.rollback(release.id, revision=1)

    # Full history
    for rev in mgr.get_history(release.id):
        print(rev.revision, rev.status, rev.notes)

    # Render templates
    rendered = mgr.render_templates("nginx-deployment", {"replicaCount": 2})
    for item in rendered:
        print(f"--- {item['name']} ({item['kind']}) ---")
        print(item["rendered"])

    # Export as Helm directory
    path = mgr.export_helm_chart("nginx-deployment")
    print(f"Exported to: {path}")

    # Uninstall
    mgr.uninstall(release.id)
```

---

## 🧪 Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov ruff

# Run all tests
pytest tests/ -v

# With coverage report
pytest tests/ -v --cov=helm_manager --cov-report=term-missing

# Lint
ruff check helm_manager.py tests/
```

---

## 🏗️ Architecture

```
blackroad-helm-manager/
├── helm_manager.py          # Core implementation (500+ lines)
│   ├── ChartTemplate        # dataclass: name, kind, content
│   ├── Chart                # dataclass: id, name, version, templates, values_yaml, …
│   ├── ReleaseRevision      # dataclass: revision, chart_version, values_override, status
│   ├── Release              # dataclass: id, name, namespace, status, history
│   ├── HelmManager          # SQLite-backed manager class
│   │   ├── create_chart()
│   │   ├── list_charts()
│   │   ├── install()
│   │   ├── upgrade()
│   │   ├── rollback()
│   │   ├── uninstall()
│   │   ├── list_releases()
│   │   ├── get_release()
│   │   ├── get_history()
│   │   ├── render_templates()
│   │   ├── export_helm_chart()
│   │   └── diff()
│   └── Typer CLI app
├── tests/
│   └── test_helm_manager.py # 55+ pytest tests
├── requirements.txt
└── .github/workflows/ci.yml
```

### Database Schema

```sql
-- Charts: versioned chart descriptors with JSON-encoded templates
CREATE TABLE charts (
    id TEXT PRIMARY KEY, name TEXT, version TEXT,
    description TEXT, app_version TEXT,
    keywords TEXT,    -- JSON array
    maintainers TEXT, -- JSON array of {name, email}
    values_yaml TEXT, templates TEXT, -- JSON array
    created_at TEXT, home_url TEXT, icon_url TEXT
);

-- Releases: lifecycle state with full immutable history
CREATE TABLE releases (
    id TEXT PRIMARY KEY, name TEXT, chart_id TEXT,
    namespace TEXT, values_override TEXT,
    status TEXT,  -- deployed | failed | pending | superseded | uninstalling
    installed_at TEXT, updated_at TEXT,
    revision INTEGER,
    history TEXT  -- JSON array of ReleaseRevision dicts
);
```

---

## 🔧 Value Override Syntax

Values follow Helm's `.Values` notation with dot-separated nesting:

```bash
# Scalar override
--set replicaCount=3

# Nested key override
--set image.tag=1.26
--set resources.requests.cpu=500m

# Multiple overrides
--set replicaCount=3 --set image.tag=1.26 --set service.type=LoadBalancer
```

Python API accepts a plain nested dict:

```python
mgr.install("nginx-deployment", "my-nginx", values={
    "replicaCount": 3,
    "image": {"tag": "1.26"},
    "service": {"type": "LoadBalancer"},
})
```

---

## 📄 License

MIT © [BlackRoad OS, Inc.](https://blackroad.ai)
