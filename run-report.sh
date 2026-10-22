#!/usr/bin/env bash
# run-report.sh — Nirmata version & health report via OttoFlow

set -uo pipefail

# Workflow files live alongside this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_DIR="${SCRIPT_DIR}"
REPORT_FILE="/tmp/nirmata-version-report.html"

# Find ottoflow binary: prefer PATH, then common locations
if command -v ottoflow &>/dev/null; then
  OTTOFLOW_BIN="ottoflow"
else
  # Search relative to the script dir and common build output paths
  for candidate in \
    "${SCRIPT_DIR}/../ottoflow/bin/ottoflow" \
    "${SCRIPT_DIR}/bin/ottoflow" \
    "${HOME}/ottoflow/bin/ottoflow"; do
    if [ -x "${candidate}" ]; then
      OTTOFLOW_BIN="${candidate}"
      break
    fi
  done
  if [ -z "${OTTOFLOW_BIN:-}" ]; then
    echo "ERROR: ottoflow binary not found."
    echo "Either add it to PATH or place it at: ${SCRIPT_DIR}/bin/ottoflow"
    echo "Build it with: cd ottoflow && make build-cli"
    exit 1
  fi
fi

# ── Inputs ────────────────────────────────────────────────────────────────────
read -rp  "Nirmata URL [p2.nirmata.co]: "              NIRMATA_URL;  NIRMATA_URL="${NIRMATA_URL:-p2.nirmata.co}"
read -rsp "Nirmata API Key: "                          API_KEY;      echo
read -rp  "Namespace [nirmata]: "                      NAMESPACE;    NAMESPACE="${NAMESPACE:-nirmata}"
read -rp  "HTTPS Proxy [leave blank if none]: "        PROXY_URL
read -rp  "NO_PROXY [${NIRMATA_URL},localhost,127.0.0.1]: " NO_PROXY_LIST
NO_PROXY_LIST="${NO_PROXY_LIST:-${NIRMATA_URL},localhost,127.0.0.1}"

if [ -n "${PROXY_URL:-}" ]; then
  export HTTPS_PROXY="$PROXY_URL" https_proxy="$PROXY_URL"
  export NO_PROXY="$NO_PROXY_LIST" no_proxy="$NO_PROXY_LIST"
fi

echo

# ── Find MongoDB pod ──────────────────────────────────────────────────────────
MONGO_POD=$(kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns=':metadata.name,:status.phase' 2>/dev/null \
  | grep -i mongo | grep Running | head -1 | awk '{print $1}' || true)

CLUSTER_JSON_COMPACT="[]"
RS_JSON="{}"
MONGO_HEALTH_JSON="{}"

if [ -z "${MONGO_POD}" ]; then
  echo "WARNING: No running MongoDB pod found — cluster, RS, and disk data will be empty."
else
  echo "Found MongoDB pod: ${MONGO_POD}"
  MONGO_DB="Cluster-${NAMESPACE}"

  # ── Cluster data + K8s version (joined with NodeSystemInfo) ─────────────
  echo "Querying MongoDB cluster data (db: ${MONGO_DB})..."
  MONGO_QUERY='var cls=db.KubernetesCluster.find({},{name:1,state:1,connectionState:1,existingKubeControllerVersion:1}).toArray();var r=cls.map(function(c){var ni=db.NodeSystemInfo.findOne({"ancestors.id":c._id,kubeletVersion:{$exists:true,$ne:null}});return {name:c.name,state:c.state||c.connectionState||"unknown",nkcVersion:c.existingKubeControllerVersion,k8sVersion:ni?ni.kubeletVersion:"N/A"};});print(JSON.stringify(r));'
  RAW=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongodb -- \
    mongosh --quiet "mongodb://localhost:27017/${MONGO_DB}" \
    --eval "${MONGO_QUERY}" 2>/dev/null || echo "[]")
  CLUSTER_JSON_COMPACT=$(echo "${RAW}" | grep -o '\[.*\]' | tail -1 | tr -d '\n\r' || echo "[]")

  # ── RS status ─────────────────────────────────────────────────────────────
  echo "Querying MongoDB RS status..."
  RS_QUERY='var s=db.getSiblingDB("admin").runCommand({replSetGetStatus:1});var m=(s.members||[]).map(function(m){return{name:String(m.name||""),stateStr:String(m.stateStr||"UNKNOWN"),health:parseInt(m.health)||0,uptime:String(parseInt(m.uptime)||0),pingMs:String(parseInt(m.pingMs)||0)};});print(JSON.stringify({set:String(s.set||"N/A"),members:m}));'
  RS_RAW=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongodb -- \
    mongosh --quiet "mongodb://localhost:27017/admin" \
    --eval "${RS_QUERY}" 2>/dev/null || echo "{}")
  RS_JSON=$(echo "${RS_RAW}" | grep -o '{.*}' | tail -1 | tr -d '\n\r' || echo "{}")

  # ── MongoDB disk usage ────────────────────────────────────────────────────
  echo "Checking MongoDB disk usage..."
  MONGO_DISK_PCT=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongodb -- \
    df /data/db --output=pcent 2>/dev/null | tail -1 | tr -d ' %' || echo "N/A")

  MONGO_WIREDTIGER=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongodb -- \
    du -sh /data/db/WiredTigerHS.wt 2>/dev/null | awk '{print $1}' || echo "N/A")

  # WiredTiger is a concern when it reaches GB scale
  MONGO_WT_WARN="false"
  if echo "${MONGO_WIREDTIGER}" | grep -qE '^[0-9]+(\.[0-9]+)?G'; then
    MONGO_WT_WARN="true"
  fi

  # Disk status label
  MONGO_DISK_STATUS="unknown"
  if [ "${MONGO_DISK_PCT}" != "N/A" ] && [ "${MONGO_DISK_PCT}" -ge 0 ] 2>/dev/null; then
    if   [ "${MONGO_DISK_PCT}" -gt 80 ]; then MONGO_DISK_STATUS="critical"
    elif [ "${MONGO_DISK_PCT}" -gt 60 ]; then MONGO_DISK_STATUS="warning"
    else MONGO_DISK_STATUS="ok"
    fi
  fi

  MONGO_HEALTH_JSON="{\"diskPct\":\"${MONGO_DISK_PCT}\",\"wiredtigerSize\":\"${MONGO_WIREDTIGER}\",\"wiredtigerWarn\":${MONGO_WT_WARN},\"diskStatus\":\"${MONGO_DISK_STATUS}\"}"
fi

# ── Kafka pod health ──────────────────────────────────────────────────────────
echo "Checking Kafka pods..."

get_kafka_pod_disk() {
  local pod="$1"
  # Try the most common Nirmata kafka data paths
  for path in /var/lib/kafka /var/lib/kafka/data /kafka/data /data; do
    result=$(kubectl exec -n "${NAMESPACE}" "${pod}" -- \
      df "${path}" --output=pcent 2>/dev/null | tail -1 | tr -d ' %')
    if [ -n "${result}" ] && [ "${result}" != "Use%" ]; then
      echo "${result}"
      return
    fi
  done
  echo "N/A"
}

# Kafka brokers (exclude controller pods)
KAFKA_PODS=$(kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns=':metadata.name' 2>/dev/null \
  | grep -E "^kafka-[0-9]" | grep -v controller || true)

# Kafka controllers
KAFKA_CTRL_PODS=$(kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns=':metadata.name' 2>/dev/null \
  | grep kafka-controller || true)

KAFKA_POD_JSON="[]"
if [ -n "${KAFKA_PODS}" ]; then
  KAFKA_POD_JSON="["
  FIRST=1
  while IFS= read -r kpod; do
    [ -z "$kpod" ] && continue
    disk=$(get_kafka_pod_disk "${kpod}")
    [ "${FIRST}" -eq 0 ] && KAFKA_POD_JSON+=","
    KAFKA_POD_JSON+="{\"name\":\"${kpod}\",\"diskPct\":\"${disk}\"}"
    FIRST=0
  done <<< "${KAFKA_PODS}"
  KAFKA_POD_JSON+="]"
fi

KAFKA_CTRL_JSON="[]"
if [ -n "${KAFKA_CTRL_PODS}" ]; then
  KAFKA_CTRL_JSON="["
  FIRST=1
  while IFS= read -r kpod; do
    [ -z "$kpod" ] && continue
    disk=$(get_kafka_pod_disk "${kpod}")
    [ "${FIRST}" -eq 0 ] && KAFKA_CTRL_JSON+=","
    KAFKA_CTRL_JSON+="{\"name\":\"${kpod}\",\"diskPct\":\"${disk}\"}"
    FIRST=0
  done <<< "${KAFKA_CTRL_PODS}"
  KAFKA_CTRL_JSON+="]"
fi

KAFKA_HEALTH_JSON="{\"pods\":${KAFKA_POD_JSON},\"controllerPods\":${KAFKA_CTRL_JSON}}"

# ── Run OttoFlow workflow ─────────────────────────────────────────────────────
echo
echo "Running OttoFlow workflow..."

OUTPUT=$("${OTTOFLOW_BIN}" run nirmata-version-check \
  --workflow-dir "${WORKFLOW_DIR}" \
  --input nirmataUrl="${NIRMATA_URL}" \
  --input apiKey="${API_KEY}" \
  --input namespace="${NAMESPACE}" \
  --input clusterJson="${CLUSTER_JSON_COMPACT}" \
  --input rsJson="${RS_JSON}" \
  --input mongoHealthJson="${MONGO_HEALTH_JSON}" \
  --input kafkaHealthJson="${KAFKA_HEALTH_JSON}" \
  2>&1)

echo "$OUTPUT"

# ── Extract and save HTML ─────────────────────────────────────────────────────
HTML=$(echo "$OUTPUT" | awk '/^  htmlReport:/{found=1; next} found && /^  [a-zA-Z]/{exit} found{print}' | sed 's/^[[:space:]]*//')

if [ -z "$HTML" ]; then
  echo
  echo "ERROR: htmlReport not found in workflow output — check step errors above."
  exit 1
fi

echo "$HTML" > "${REPORT_FILE}"
echo
echo "Report saved: ${REPORT_FILE}"

if command -v open &>/dev/null; then
  open "${REPORT_FILE}"
elif command -v xdg-open &>/dev/null; then
  xdg-open "${REPORT_FILE}"
fi
