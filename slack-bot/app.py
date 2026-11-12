"""
Nirmata Report Slack Bot
Accepts a kubeconfig upload, shows context picker, runs the OttoFlow report.
"""

import os
import re
import json
import yaml
import tempfile
import threading
import subprocess
import requests

from flask import Flask, request, make_response
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

# ── Config ────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN    = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
NIRMATA_URL        = os.environ.get("NIRMATA_URL", "")       # e.g. p2.nirmata.co
NIRMATA_API_KEY    = os.environ.get("NIRMATA_API_KEY", "")
OTTOFLOW_BIN       = os.environ.get("OTTOFLOW_BIN", "ottoflow")
WORKFLOW_DIR       = os.environ.get("WORKFLOW_DIR", "/app/workflow")

bolt = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(bolt)

# ── In-memory session store (use Redis in production) ─────────────────────────
# sessions[user_id] = {kubeconfig_path, contexts, current_context, selected_context}
sessions: dict = {}

COMMON_NAMESPACES = ["nirmata", "p2", "p3", "default"]


# ─────────────────────────────────────────────────────────────────────────────
# /nirmata-report slash command
# ─────────────────────────────────────────────────────────────────────────────
@bolt.command("/nirmata-report")
def report_command(ack, body, client):
    ack()
    user_id = body["user_id"]
    client.chat_postMessage(
        channel=user_id,
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🔍 Nirmata Version & Health Report"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Upload your *kubeconfig* file here and I'll detect all available contexts.\n\n_Your kubeconfig is used only for this request and deleted immediately after._"
                }
            }
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# File upload in DM — parse kubeconfig and show context picker
# ─────────────────────────────────────────────────────────────────────────────
@bolt.event("message")
def handle_dm_file(event, client, logger):
    if event.get("channel_type") != "im":
        return
    if "files" not in event:
        return

    user_id = event["user"]
    file_info = event["files"][0]

    # Download the file
    resp = requests.get(
        file_info["url_private_download"],
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        timeout=10,
    )
    kubeconfig_text = resp.text

    # Parse kubeconfig
    try:
        kube = yaml.safe_load(kubeconfig_text)
        contexts = [c["name"] for c in kube.get("contexts", [])]
        current  = kube.get("current-context", "")
    except Exception as exc:
        client.chat_postMessage(channel=user_id,
            text=f":x: Couldn't parse kubeconfig: {exc}")
        return

    if not contexts:
        client.chat_postMessage(channel=user_id,
            text=":x: No contexts found in the uploaded file.")
        return

    # Persist kubeconfig to temp file
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    tmp.write(kubeconfig_text)
    tmp.close()

    sessions[user_id] = {
        "kubeconfig_path": tmp.name,
        "contexts": contexts,
        "current_context": current,
    }

    # Build dropdown options (Slack caps at 100)
    options = [
        {
            "text": {"type": "plain_text", "text": f"{'★ ' if c == current else ''}{_short(c)}"},
            "value": c,
        }
        for c in contexts[:100]
    ]
    initial = next((o for o in options if o["value"] == current), None)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":white_check_mark: Found *{len(contexts)} context{'s' if len(contexts) != 1 else ''}*. Select a cluster:"
            }
        },
        {
            "type": "actions",
            "block_id": "context_picker",
            "elements": [
                {
                    "type": "static_select",
                    "placeholder": {"type": "plain_text", "text": "Choose a context…"},
                    "options": options,
                    "action_id": "context_selected",
                    **({"initial_option": initial} if initial else {}),
                }
            ],
        },
    ]
    client.chat_postMessage(channel=user_id, text="Pick a context:", blocks=blocks)


# ─────────────────────────────────────────────────────────────────────────────
# Context selected → show namespace picker
# ─────────────────────────────────────────────────────────────────────────────
@bolt.action("context_selected")
def context_selected(ack, body, client):
    ack()
    user_id  = body["user"]["id"]
    selected = body["actions"][0]["selected_option"]["value"]

    if user_id not in sessions:
        client.chat_postMessage(channel=user_id,
            text=":x: Session expired — please upload your kubeconfig again.")
        return

    sessions[user_id]["selected_context"] = selected

    ns_buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": ns},
            "value": ns,
            "action_id": f"ns_{ns}",
            "style": "primary" if ns in ("nirmata", "p2") else None,
        }
        for ns in COMMON_NAMESPACES
    ] + [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ Other…"},
            "value": "other",
            "action_id": "ns_other",
        }
    ]
    # Remove None style keys
    for b in ns_buttons:
        if b.get("style") is None:
            b.pop("style", None)

    client.chat_postMessage(
        channel=user_id,
        text="Pick a namespace:",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":white_check_mark: Context: `{_short(selected)}`\n\n*Select namespace:*"
                }
            },
            {
                "type": "actions",
                "block_id": "namespace_picker",
                "elements": ns_buttons,
            },
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Namespace buttons
# ─────────────────────────────────────────────────────────────────────────────
for _ns in COMMON_NAMESPACES:
    @bolt.action(f"ns_{_ns}")
    def handle_ns(ack, body, client, action=_ns):
        ack()
        _kick_off_report(body["user"]["id"], action, client)


@bolt.action("ns_other")
def ns_other(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    # Open a modal to capture a custom namespace
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "custom_namespace_modal",
            "title": {"type": "plain_text", "text": "Custom Namespace"},
            "submit": {"type": "plain_text", "text": "Run Report"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "ns_input",
                    "label": {"type": "plain_text", "text": "Namespace"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "namespace_value",
                        "placeholder": {"type": "plain_text", "text": "e.g. nirmata"},
                    },
                }
            ],
            "private_metadata": user_id,
        },
    )


@bolt.view("custom_namespace_modal")
def custom_namespace_submit(ack, body, client):
    ack()
    user_id   = body["view"]["private_metadata"]
    namespace = body["view"]["state"]["values"]["ns_input"]["namespace_value"]["value"].strip()
    _kick_off_report(user_id, namespace, client)


# ─────────────────────────────────────────────────────────────────────────────
# Core: run the report in a background thread
# ─────────────────────────────────────────────────────────────────────────────
def _kick_off_report(user_id: str, namespace: str, client):
    session = sessions.get(user_id)
    if not session:
        client.chat_postMessage(channel=user_id,
            text=":x: Session expired — please upload your kubeconfig again.")
        return

    context   = session.get("selected_context", "")
    kubeconfig = session.get("kubeconfig_path", "")

    client.chat_postMessage(
        channel=user_id,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":hourglass_flowing_sand: Running report...\n"
                        f"• *Context:* `{_short(context)}`\n"
                        f"• *Namespace:* `{namespace}`\n\n"
                        f"_This takes ~30 seconds._"
                    )
                }
            }
        ]
    )

    threading.Thread(
        target=_run_report,
        args=(user_id, context, namespace, kubeconfig, client),
        daemon=True,
    ).start()


def _run_report(user_id, context, namespace, kubeconfig, client):
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig

    def kube(*args):
        return subprocess.run(
            ["kubectl", *args], env=env, capture_output=True, text=True, timeout=60
        )

    def mongo_exec(pod, container, db, query):
        result = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod, "-c", container,
             "--", "mongosh", "--quiet", f"mongodb://localhost:27017/{db}",
             "--eval", query],
            env=env, capture_output=True, text=True, timeout=60
        )
        return result.stdout

    try:
        # Switch context
        kube("config", "use-context", context)

        # ── Cluster + NKC data from MongoDB ──────────────────────────────────
        mongo_pod = ""
        pods_out = kube("get", "pods", "-n", namespace, "--no-headers",
                        "-o", "custom-columns=:metadata.name,:status.phase").stdout
        for line in pods_out.splitlines():
            if "mongo" in line.lower() and "Running" in line:
                mongo_pod = line.split()[0]
                break

        cluster_json = "[]"
        rs_json      = "{}"
        mongo_disk   = "N/A"

        if mongo_pod:
            cluster_q = (
                'var cls=db.KubernetesCluster.find({},{name:1,state:1,connectionState:1,'
                'existingKubeControllerVersion:1}).toArray();'
                'var r=cls.map(function(c){'
                'var ni=db.NodeSystemInfo.findOne({"ancestors.id":c._id,kubeletVersion:{$exists:true,$ne:null}});'
                'return {name:c.name,state:c.state||c.connectionState||"unknown",'
                'nkcVersion:c.existingKubeControllerVersion,k8sVersion:ni?ni.kubeletVersion:"N/A"};});'
                'print(JSON.stringify(r));'
            )
            raw = mongo_exec(mongo_pod, "mongodb", f"Cluster-{namespace}", cluster_q)
            m = re.search(r'\[.*\]', raw, re.DOTALL)
            if m:
                cluster_json = m.group(0).replace('\n', '').replace('\r', '')

            rs_q = (
                'var s=db.getSiblingDB("admin").runCommand({replSetGetStatus:1});'
                'var m=(s.members||[]).map(function(m){'
                'return{name:String(m.name||""),stateStr:String(m.stateStr||"UNKNOWN"),'
                'health:parseInt(m.health)||0,uptime:String(parseInt(m.uptime)||0),'
                'pingMs:String(parseInt(m.pingMs)||0)};});'
                'print(JSON.stringify({set:String(s.set||"N/A"),members:m}));'
            )
            raw_rs = mongo_exec(mongo_pod, "mongodb", "admin", rs_q)
            m2 = re.search(r'\{.*\}', raw_rs, re.DOTALL)
            if m2:
                rs_json = m2.group(0).replace('\n', '').replace('\r', '')

            disk_out = subprocess.run(
                ["kubectl", "exec", "-n", namespace, mongo_pod, "-c", "mongodb",
                 "--", "df", "/data/db", "--output=pcent"],
                env=env, capture_output=True, text=True, timeout=30
            ).stdout
            lines = [l.strip() for l in disk_out.splitlines() if l.strip() and "%" in l]
            if lines:
                mongo_disk = lines[-1].replace("%", "").strip()

        # ── Run OttoFlow workflow ─────────────────────────────────────────────
        result = subprocess.run(
            [OTTOFLOW_BIN, "run", "nirmata-version-check",
             "--workflow-dir", WORKFLOW_DIR,
             "--input", f"nirmataUrl={NIRMATA_URL}",
             "--input", f"apiKey={NIRMATA_API_KEY}",
             "--input", f"namespace={namespace}",
             "--input", f"clusterJson={cluster_json}",
             "--input", f"rsJson={rs_json}",
             "--input", f"mongoHealthJson={{\"diskPct\":\"{mongo_disk}\"}}",
             "--input", "kafkaHealthJson={}",
             "--input", "mongoLogErrorsJson=[]",
             "--input", "kafkaLogErrorsJson={}",
             "--input", "slackWebhookUrl=",
             "--input", "slackToken=",
             "--input", "slackChannel=",
             "--input", "slackMentions=",
             ],
            env=env, capture_output=True, text=True, timeout=120
        )
        output = result.stdout + result.stderr

        # Extract HTML
        html = ""
        in_html = False
        for line in output.splitlines():
            if line.strip() == "htmlReport:":
                in_html = True
                continue
            if in_html:
                if re.match(r'^\s{2}[a-zA-Z]', line) and not line.strip().startswith("<"):
                    break
                html += line.lstrip() + "\n"

        # Extract key metrics for the Slack summary
        summary = _build_summary(output, context, namespace)

        # Post summary to Slack
        client.chat_postMessage(channel=user_id, blocks=summary)

        # Upload HTML report as a file
        if html.strip():
            html_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", delete=False, prefix="nirmata-report-"
            )
            html_file.write(html.strip())
            html_file.close()

            with open(html_file.name, "rb") as f:
                client.files_upload_v2(
                    channel=user_id,
                    file=f,
                    filename=f"nirmata-report-{namespace}.html",
                    title=f"Nirmata Report — {_short(context)} / {namespace}",
                )
            os.unlink(html_file.name)

    except Exception as exc:
        client.chat_postMessage(channel=user_id,
            text=f":x: Report failed: {exc}")

    finally:
        # Clean up kubeconfig temp file
        if kubeconfig and os.path.exists(kubeconfig):
            os.unlink(kubeconfig)
        sessions.pop(user_id, None)


def _build_summary(output: str, context: str, namespace: str) -> list:
    """Extract key metrics from workflow output and build Slack blocks."""

    def _extract(key):
        m = re.search(rf'^\s+{key}:\s*\n\s*(.+)', output, re.MULTILINE)
        return m.group(1).strip() if m else "N/A"

    svc_count      = _extract("serviceCount")
    cluster_count  = _extract("clusterCount")
    node_count     = _extract("nodeCount")
    unhealthy_dep  = _extract("unhealthyDeployments")
    mongo_errors   = _extract("mongoLogErrors")
    kafka_errors   = _extract("kafkaLogErrors")

    # Determine overall health
    has_issues = any([
        unhealthy_dep not in ("0", "N/A"),
        mongo_errors not in ("0", "N/A"),
        kafka_errors not in ("0", "N/A"),
    ])
    icon   = ":rotating_light:" if has_issues else ":white_check_mark:"
    status = "Issues detected" if has_issues else "All healthy"

    lines = [
        f"• *NDP Services:* {svc_count}",
        f"• *Registered Clusters:* {cluster_count}",
        f"• *Nodes:* {node_count}",
        f"• *Unhealthy Deployments:* {unhealthy_dep}",
        f"• *MongoDB log errors (1h):* {mongo_errors}",
        f"• *Kafka log errors (1h):* {kafka_errors}",
    ]

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon} Nirmata Report — {status}"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Context:* `{_short(context)}`\n"
                    f"*Namespace:* `{namespace}`\n\n"
                    + "\n".join(lines)
                )
            }
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_Full HTML report attached below ↓_"}]
        }
    ]


def _short(context: str) -> str:
    """Shorten long ARN-style context names for display."""
    if "cluster/" in context:
        return context.split("cluster/")[-1]
    return context


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────
@flask_app.route("/slack/events",   methods=["POST"])
def events():   return handler.handle(request)

@flask_app.route("/slack/actions",  methods=["POST"])
def actions():  return handler.handle(request)

@flask_app.route("/slack/commands", methods=["POST"])
def commands(): return handler.handle(request)

@flask_app.route("/health")
def health():   return "ok"


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
