# OttoFlow Nirmata Version Report

An [OttoFlow](https://github.com/nirmata/ottoflow) workflow that generates a rich HTML report of your Nirmata platform:

- **Table 1 — NDP Service Versions**: every Deployment and StatefulSet in the Nirmata namespace with image tags (cluster, config, orchestrator, environment, mongo, kafka, kafka-controller, tunnel, etc.)
- **Table 2 — Clusters & NKC Versions**: all clusters registered in the Nirmata management plane with Kubernetes version and Nirmata Kube Controller version
- **Table 3 — MongoDB Replica Set Status**: RS member roles, health, uptime, and ping

---

## How it works

```
run-report.sh  ──── kubectl exec mongodb-0 ──► Cluster data (KubernetesCluster collection)
                                               RS status (rs.status())
                │
                └── ottoflow run ────────────► getNDPWorkloads    (resource.List K8s API)
                                               getNirmataClusters (json.unmarshal input)
                                               getMongoRSStatus   (json.unmarshal input)
                                               generateHTMLReport (CEL string builder)
                                               └── saves to /tmp/nirmata-version-report.html
                                                   opens in default browser
```

OttoFlow handles all Kubernetes API queries and HTML generation. The wrapper script bridges the gap for MongoDB access (which requires `kubectl exec`) and user input.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| [OttoFlow CLI](https://github.com/nirmata/ottoflow) | Build with `make build-cli` in the ottoflow repo |
| `kubectl` | Pointed at the Nirmata management cluster |
| `jq` | For JSON processing (`brew install jq`) |
| Nirmata API key | Settings → API Keys in the Nirmata UI |

---

## Quick Start

### 1. Build the OttoFlow CLI

```bash
git clone https://github.com/nirmata/ottoflow
cd ottoflow
make build-cli
# Binary: ./bin/ottoflow
```

### 2. Point kubectl at the Nirmata management cluster

```bash
kubectl config get-contexts          # list available contexts
kubectl config use-context <context> # switch to the management cluster
```

### 3. Clone this repo

```bash
git clone https://github.com/nirmata/ottoflow-nirmata-report
cd ottoflow-nirmata-report
```

### 4. Update the script path

Edit `run-report.sh` and set `OTTOFLOW_DIR` to the path where you built OttoFlow:

```bash
OTTOFLOW_DIR="/path/to/ottoflow"
```

### 5. Run the report

```bash
bash run-report.sh
```

You will be prompted for:

| Prompt | Example | Notes |
|--------|---------|-------|
| Nirmata URL | `p2.nirmata.co` | No `https://` prefix |
| Nirmata API Key | `JjUh...` | From Settings → API Keys |
| Namespace | `nirmata` or `p2` or `default` | Namespace where Nirmata NDP services run |
| HTTPS Proxy | `http://proxy:8080` | Leave blank if not needed |
| NO_PROXY | `nirmata.co,localhost` | Hosts that bypass the proxy |

The report opens automatically in your default browser and is saved to `/tmp/nirmata-version-report.html`.

---

## Files

| File | Purpose |
|------|---------|
| `workflow.yaml` | OttoFlow Workflow CRD — the core logic |
| `agents.yaml` | OttoFlow Agent CRDs — included for reference (not required for current workflow) |
| `secret.yaml` | Kubernetes Secret template for scheduled runs |
| `run.yaml` | WorkflowRun example + scheduled cron Workflow |
| `run-report.sh` | Wrapper script — MongoDB queries, user prompts, HTML extraction |

---

## Namespace

Nirmata services may run in different namespaces depending on the deployment:

| Deployment | Namespace |
|-----------|-----------|
| Standard install | `nirmata` |
| Multi-tenant (e.g. p2 plan) | `p2`, `p3`, etc. |
| Single-namespace install | `default` |

The script prompts for the namespace and defaults to `nirmata`. The MongoDB database name is automatically derived as `Cluster-<namespace>` (e.g. `Cluster-p2`, `Cluster-default`).

---

## Running on a Schedule (in-cluster)

To run the report daily at 09:00 UTC without the wrapper script:

### 1. Create credentials secret

```bash
kubectl apply -f secret.yaml
# Edit secret.yaml first with your actual URL, API key, etc.
```

### 2. Apply the scheduled workflow

```bash
kubectl apply -f workflow.yaml
kubectl apply -f run.yaml   # contains the cron-triggered Workflow
```

### 3. Pre-fetch MongoDB data via a CronJob

Since OttoFlow cannot `kubectl exec` into pods, MongoDB data must be pre-fetched externally and passed as workflow inputs. Create a Kubernetes CronJob that:

1. Runs `kubectl exec` to query MongoDB
2. Creates a `WorkflowRun` with `clusterJson` and `rsJson` inputs

See the [Gap & Roadmap](#gap--roadmap) section for the planned MCPServer approach that removes this requirement.

---

## What OttoFlow handles vs the script

| Responsibility | OttoFlow (workflow.yaml) | Script (run-report.sh) |
|---------------|--------------------------|----------------------|
| K8s API queries | ✅ `resource.List()` for Deployments/StatefulSets | — |
| Cluster + NKC data | ✅ Parses JSON input via `json.unmarshal()` | ✅ Fetches via `kubectl exec mongodb-0` |
| MongoDB RS status | ✅ Parses JSON input | ✅ Fetches via `kubectl exec` + `rs.status()` |
| HTML report | ✅ CEL string expressions | — |
| User prompts | — | ✅ Interactive `read` prompts |
| File I/O + browser open | — | ✅ `open /tmp/report.html` |

---

## Gap & Roadmap

The only reason a wrapper script is needed is that OttoFlow CEL does not have a `kubectl exec` / shell execution step. Two paths to close this:

**Option A — Custom MCPServer (recommended)**
Deploy a small HTTP service that wraps MongoDB queries, register it as an `MCPServer` CRD, and call it from the workflow via `mcpToolCall`. This would make the workflow fully self-contained with zero wrapper script.

**Option B — OttoFlow shell step**
If OttoFlow adds a `shellExec` step type in a future release, the MongoDB queries move directly into the workflow.

---

## Example Output

The generated HTML report includes:

- **Header** with Nirmata URL, namespace, and generation timestamp
- **Summary pills**: NDP service count, StatefulSet count, cluster count, MongoDB RS set name, RS member count
- **Table 1** — NDP services with kind badges (blue `Deployment`, amber `StatefulSet`), version tags in monospace, image repositories
- **Table 2** — Clusters with Kubernetes version, NKC version `v3.x.x`, and state badges (green `ready`, red `failed`, amber `unknown`)
- **Table 3** — MongoDB RS members with role (green `PRIMARY`, blue `SECONDARY`), health status, uptime, and ping

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No cluster data — MongoDB query may have failed` | Wrong namespace or MongoDB pod not running | Check `kubectl get pods -n <namespace> \| grep mongo` |
| `htmlReport not found in workflow output` | Workflow failed before HTML step | Check step error messages in the output |
| `getNDPWorkloads` returns 0 services | kubectl pointing at wrong cluster or wrong namespace | Run `kubectl get deployments -n <namespace>` to verify |
| RS table shows "No RS data" | MongoDB RS query failed | Run `kubectl exec -n <ns> mongodb-0 -c mongodb -- mongosh --eval 'rs.status()'` manually |
| `saveReport` step fails | ConfigMap doesn't exist — this step is optional | Safe to ignore; report is still saved to `/tmp/` |

---

Built with ❤️ by the Nirmata team using [OttoFlow](https://github.com/nirmata/ottoflow).
