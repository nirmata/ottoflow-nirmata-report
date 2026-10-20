#!/usr/bin/env bash
# run-report.sh — runs the nirmata-version-check workflow and auto-opens the HTML report

set -uo pipefail

OTTOFLOW_DIR="/Users/rahul/allrepos/main/ottoflow"
WORKFLOW_DIR="/Users/rahul/Downloads/nirmata-version-check-workflow"
REPORT_FILE="/tmp/nirmata-version-report.html"

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

# ── Fetch cluster data from MongoDB via a temporary pod ───────────────────────
# Queries MongoDB directly — no HTTP auth issues.
echo
CLUSTER_JSON_COMPACT="[]"

# Find the running MongoDB pod (exec is faster than spinning a new pod)
MONGO_POD=$(kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns=':metadata.name,:status.phase' 2>/dev/null \
  | grep -i mongo | grep Running | head -1 | awk '{print $1}' || true)

if [ -z "${MONGO_POD}" ]; then
  echo "WARNING: No running MongoDB pod found — cluster table will be empty."
else
  echo "Found MongoDB pod: ${MONGO_POD}"
  # Database name is namespace-scoped: Cluster-<namespace>
  MONGO_DB="Cluster-${NAMESPACE}"
  MONGO_QUERY='var cls=db.KubernetesCluster.find({},{name:1,state:1,connectionState:1,existingKubeControllerVersion:1}).toArray();var r=cls.map(function(c){var ni=db.NodeSystemInfo.findOne({"ancestors.id":c._id,kubeletVersion:{$exists:true,$ne:null}});return {name:c.name,state:c.state||c.connectionState||"unknown",nkcVersion:c.existingKubeControllerVersion,k8sVersion:ni?ni.kubeletVersion:"N/A"};});print(JSON.stringify(r));'

  echo "Querying MongoDB database '${MONGO_DB}'..."
  RAW=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -- \
    mongosh --quiet "mongodb://localhost:27017/${MONGO_DB}" \
    --eval "${MONGO_QUERY}" 2>/dev/null || echo "[]")

  CLUSTER_JSON_COMPACT=$(echo "${RAW}" | grep -o '\[.*\]' | tail -1 \
    | tr -d '\n\r' || echo "[]")
fi

# ── Fetch MongoDB RS status ───────────────────────────────────────────────────
RS_JSON="{}"
if [ -n "${MONGO_POD}" ]; then
  echo "Querying MongoDB RS status..."
  RS_QUERY='var s=db.getSiblingDB("admin").runCommand({replSetGetStatus:1});var m=(s.members||[]).map(function(m){return{name:String(m.name||""),stateStr:String(m.stateStr||"UNKNOWN"),health:parseInt(m.health)||0,uptime:String(parseInt(m.uptime)||0),pingMs:String(parseInt(m.pingMs)||0)};});print(JSON.stringify({set:String(s.set||"N/A"),members:m}));'
  RS_RAW=$(kubectl exec -n "${NAMESPACE}" "${MONGO_POD}" -c mongodb -- \
    mongosh --quiet "mongodb://localhost:27017/admin" \
    --eval "${RS_QUERY}" 2>/dev/null || echo "{}")
  RS_JSON=$(echo "${RS_RAW}" | grep -o '{.*}' | tail -1 | tr -d '\n\r' || echo "{}")
fi

# ── Run workflow ───────────────────────────────────────────────────────────────
echo
echo "Running workflow..."

OUTPUT=$("${OTTOFLOW_DIR}/bin/ottoflow" run nirmata-version-check \
  --workflow-dir "${WORKFLOW_DIR}" \
  --input "nirmataUrl=${NIRMATA_URL}" \
  --input apiKey="${API_KEY}" \
  --input "namespace=${NAMESPACE}" \
  --input clusterJson="${CLUSTER_JSON_COMPACT}" \
  --input rsJson="${RS_JSON}" 2>&1)

echo "$OUTPUT"

# ── Extract HTML from stdout ───────────────────────────────────────────────────
HTML=$(echo "$OUTPUT" | awk '/^  htmlReport:/{found=1; next} found && /^  [a-zA-Z]/{exit} found{print}' | sed 's/^[[:space:]]*//')

if [ -z "$HTML" ]; then
  echo "htmlReport not found in workflow output."
  exit 1
fi

# ── Save and open ──────────────────────────────────────────────────────────────
echo "$HTML" > "${REPORT_FILE}"
echo "Report saved to: ${REPORT_FILE}"

if command -v open &>/dev/null; then
  open "${REPORT_FILE}"
elif command -v xdg-open &>/dev/null; then
  xdg-open "${REPORT_FILE}"
else
  echo "Open manually: ${REPORT_FILE}"
fi
