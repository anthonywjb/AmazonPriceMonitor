import csv
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from price_monitor import (
    ASIN_RE,
    AVAILABILITY_TOPIC_SUFFIX,
    CSV_PATH,
    MQTT_DISCOVERY_PREFIX,
    MQTT_TOPIC_PREFIX,
    REQUIRED_COLUMNS,
    STOCK_TOPIC_SUFFIX,
    ensure_columns,
    get_page_data,
    item_id,
    locked_csv,
    read_csv,
    write_csv,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

TOPIC_PREFIX = MQTT_TOPIC_PREFIX + "/"
BASE_COLUMNS = ("URL", "Threshold")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(PROJECT_ROOT, "price_monitor.py")
CONFIG_DIR = os.path.dirname(CSV_PATH) or "."
PID_PATH = os.path.join(CONFIG_DIR, "monitor.pid")


def _mqtt_publish(topic, message):
    """Publish an MQTT message using the current environment MQTT_HOST."""
    host = os.environ.get("MQTT_HOST", "")
    if not host:
        return
    command = ["mosquitto_pub", "-h", host, "-t", topic, "-r"]
    if message:
        command += ["-m", message]
    else:
        command.append("-n")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        pass
LOG_PATH = os.path.join(CONFIG_DIR, "monitor.log")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
DEFAULT_INTERVAL_MINUTES = 30

DISPLAY_NAMES = {
    "MQTTTopic": "MQTT Topic",
}


@app.template_filter("display_name")
def display_name(name):
    return DISPLAY_NAMES.get(name, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name))


def csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(16)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return None
    sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("_csrf_token")
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(400, description="Invalid or missing CSRF token.")


def valid_topic(topic):
    if not topic or any(char.isspace() for char in topic):
        return False
    if "#" in topic or "+" in topic:
        return False
    if topic.startswith("/") or topic.endswith("/") or "//" in topic:
        return False
    return True


def normalize_topic(topic):
    topic = topic.strip()
    if topic and not topic.startswith(TOPIC_PREFIX):
        topic = TOPIC_PREFIX + topic
    return topic


def normalize_url(url):
    match = ASIN_RE.search(url)
    if match:
        return f"https://www.amazon.co.uk/dp/{match.group(1).upper()}/"
    return url


def load_rows():
    try:
        fieldnames, rows = read_csv(CSV_PATH)
    except FileNotFoundError:
        try:
            fresh_fields = list(REQUIRED_COLUMNS)
            ensure_columns(fresh_fields)
            write_csv(CSV_PATH, fresh_fields, [])
        except OSError:
            return None, None
        fieldnames, rows = fresh_fields, []
    except OSError:
        return None, None
    if any(column not in fieldnames for column in BASE_COLUMNS):
        return None, None
    ensure_columns(fieldnames)
    return fieldnames, rows


CSV_WRITE_ERROR = (
    "Cannot write monitor.csv: {error}. Check ownership of the config folder."
)


@app.post("/add")
def add_row():
    parsed = parse_row_form()
    if parsed is None:
        return redirect(url_for("index"))
    url, threshold, topic = parsed

    try:
        with locked_csv(CSV_PATH):
            fieldnames, rows = load_rows()
            if fieldnames is None:
                flash(
                    "monitor.csv is missing or has no URL/Threshold columns.",
                    "error",
                )
                return redirect(url_for("index"))
            if any((row.get("URL") or "").strip() == url for row in rows):
                flash("That URL is already being monitored.", "error")
                return redirect(url_for("index"))

            new_row = {name: "" for name in fieldnames}
            new_row["URL"] = url
            new_row["Threshold"] = threshold
            new_row["MQTTTopic"] = topic
            new_row["TargetMet"] = "No"
            new_row["InStock"] = "No"
            try:
                title, price, in_stock = get_page_data(url)
                new_row["Title"] = title
                new_row["InStock"] = "Yes" if in_stock else "No"
                new_row["TimeStamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if in_stock:
                    new_row["CurrentPrice"] = f"{price:.2f}"
                    new_row["TargetMet"] = "Yes" if price < float(threshold) else "No"
            except Exception as error:
                flash(f"Could not fetch page details: {error}", "error")
            rows.append(new_row)
            write_csv(CSV_PATH, fieldnames, rows)
    except OSError as error:
        flash(CSV_WRITE_ERROR.format(error=error), "error")
        return redirect(url_for("index"))

    try:
        asin = item_id(url)
        target_met = new_row.get("TargetMet", "No") == "Yes"
        in_stock = new_row.get("InStock", "No") == "Yes"
        name = topic.rstrip("/").rsplit("/", 1)[-1]
        _mqtt_publish(
            f"{MQTT_DISCOVERY_PREFIX}/sensor/apm/{asin}/config",
            json.dumps({
                "name": name,
                "object_id": f"apm_{name}",
                "unique_id": f"apm_{asin}",
                "state_topic": topic,
                "json_attributes_topic": f"{topic}/attributes",
                "unit_of_measurement": "GBP",
                "device_class": "monetary",
                "icon": "mdi:tag-check" if target_met else "mdi:tag",
                "availability_topic": topic + AVAILABILITY_TOPIC_SUFFIX,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": {
                    "identifiers": ["apm"],
                    "name": "APM",
                    "manufacturer": "Amazon",
                    "model": "Amazon.co.uk",
                },
            }),
        )
        _mqtt_publish(
            f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/apm/{asin}/config",
            json.dumps({
                "name": f"{name} in stock",
                "unique_id": f"apm_{asin}_stock",
                "object_id": f"apm_{name}_stock",
                "state_topic": topic + STOCK_TOPIC_SUFFIX,
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": {
                    "identifiers": ["apm"],
                    "name": "APM",
                    "manufacturer": "Amazon",
                    "model": "Amazon.co.uk",
                },
            }),
        )
        _mqtt_publish(topic + AVAILABILITY_TOPIC_SUFFIX, "online")
        _mqtt_publish(
            topic + STOCK_TOPIC_SUFFIX, "ON" if in_stock else "OFF"
        )
        if in_stock:
            price_str = new_row.get("CurrentPrice", "")
            if price_str:
                _mqtt_publish(topic, price_str)
        _mqtt_publish(
            f"{topic}/attributes",
            json.dumps({
                "title": new_row.get("Title", ""),
                "threshold": f"{float(threshold):.2f}",
                "target_met": target_met,
                "in_stock": "Yes" if in_stock else "No",
            }),
        )
    except Exception:
        pass

    flash("Row added.", "success")
    return redirect(url_for("index"))


@app.post("/edit/<int:index>")
def edit_row(index):
    parsed = parse_row_form()
    if parsed is None:
        return redirect(url_for("index"))
    url, threshold, topic = parsed

    try:
        with locked_csv(CSV_PATH):
            fieldnames, rows = load_rows()
            if fieldnames is None:
                flash(
                    "monitor.csv is missing or has no URL/Threshold columns.",
                    "error",
                )
                return redirect(url_for("index"))
            if index < 0 or index >= len(rows):
                flash("Row not found.", "error")
                return redirect(url_for("index"))

            rows[index]["URL"] = url
            rows[index]["Threshold"] = threshold
            rows[index]["MQTTTopic"] = topic
            write_csv(CSV_PATH, fieldnames, rows)
    except OSError as error:
        flash(CSV_WRITE_ERROR.format(error=error), "error")
        return redirect(url_for("index"))
    flash("Row updated.", "success")
    return redirect(url_for("index"))


_children = {}


def _reap_children():
    for pid, proc in list(_children.items()):
        if proc.poll() is not None:
            del _children[pid]


def _pid_matches(pid, script):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as proc_file:
            cmdline = proc_file.read().decode(errors="replace")
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    needle = os.path.basename(script) if script else "price_monitor.py"
    return needle in cmdline


def _read_pid():
    """Return the (pid, script) pair recorded in the pid file."""
    try:
        with open(PID_PATH) as pid_file:
            data = json.load(pid_file)
        return int(data["pid"]), str(data.get("script") or "")
    except (OSError, ValueError, KeyError, TypeError):
        return None, ""


def _write_pid(pid):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp_path = PID_PATH + ".tmp"
    with open(tmp_path, "w") as pid_file:
        json.dump({"pid": pid, "script": SCRIPT_PATH}, pid_file)
    os.replace(tmp_path, PID_PATH)


def _clear_pid():
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def load_settings():
    try:
        with open(SETTINGS_PATH) as settings_file:
            data = json.load(settings_file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(update):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    settings = load_settings()
    settings.update(update)
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as settings_file:
        json.dump(settings, settings_file)
    os.replace(tmp_path, SETTINGS_PATH)


def get_interval():
    value = load_settings().get("interval")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return value if value > 0 else DEFAULT_INTERVAL_MINUTES


def get_ui_mqtt_host():
    """Return the MQTT host saved via the web app, or None if never set here."""
    settings = load_settings()
    if "mqtt_host" in settings:
        return str(settings["mqtt_host"]).strip()
    return None


def effective_mqtt_host():
    """The host the monitor will actually use: web-app setting first, then env."""
    ui_value = get_ui_mqtt_host()
    if ui_value is not None:
        return ui_value
    return os.environ.get("MQTT_HOST", "").strip()


def monitor_status():
    _reap_children()
    pid, script = _read_pid()
    running = bool(pid and _pid_matches(pid, script))
    if pid and not running:
        _clear_pid()
    interval = get_interval()
    try:
        csv_mtime = os.path.getmtime(CSV_PATH)
    except OSError:
        csv_mtime = 0
    return {
        "running": running,
        "pid": pid if running else None,
        "interval": interval,
        "interval_display": f"{interval:g}",
        "mqtt_host": effective_mqtt_host(),
        "checking": bool(
            _manual_check_proc is not None and _manual_check_proc.poll() is None
        ),
        "csv_mtime": csv_mtime,
    }


def _monitor_child_env():
    """Environment for spawned monitor processes, honouring the web-set MQTT host."""
    child_env = dict(os.environ)
    ui_host = get_ui_mqtt_host()
    if ui_host is not None:
        if ui_host:
            child_env["MQTT_HOST"] = ui_host
        else:
            child_env.pop("MQTT_HOST", None)
    return child_env


def start_monitor_process():
    status = monitor_status()
    if status["running"]:
        return False, f"Monitor is already running (pid {status['pid']})."
    if not os.path.exists(SCRIPT_PATH):
        return False, f"Monitor script not found: {SCRIPT_PATH}"
    interval = get_interval()

    child_env = _monitor_child_env()

    try:
        log_handle = open(LOG_PATH, "a")
    except OSError as error:
        return False, f"Cannot write {LOG_PATH}: {error}. Check ownership of the config folder."
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                SCRIPT_PATH,
                "--interval",
                f"{interval:g}",
                "--log-file",
                os.path.abspath(LOG_PATH),
            ],
            cwd=PROJECT_ROOT,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        log_handle.close()
        return False, f"Could not start monitor: {error}"
    log_handle.close()
    try:
        _write_pid(proc.pid)
    except OSError as error:
        proc.terminate()
        proc.wait(timeout=5)
        return False, f"Cannot write {PID_PATH}: {error}. Check ownership of the config folder."
    _children[proc.pid] = proc
    return True, f"Monitor started (pid {proc.pid}), checking every {interval:g} min."


_manual_check_proc = None


def check_now_process():
    """Run a single check pass in the background without touching the loop."""
    global _manual_check_proc
    if _manual_check_proc is not None:
        if _manual_check_proc.poll() is None:
            return False, "A check is already running."
        _manual_check_proc = None
    if not os.path.exists(SCRIPT_PATH):
        return False, f"Monitor script not found: {SCRIPT_PATH}"

    try:
        log_handle = open(LOG_PATH, "a")
    except OSError as error:
        return False, f"Cannot write {LOG_PATH}: {error}. Check ownership of the config folder."
    try:
        proc = subprocess.Popen(
            [sys.executable, SCRIPT_PATH],
            cwd=PROJECT_ROOT,
            env=_monitor_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        log_handle.close()
        return False, f"Could not run check: {error}"
    log_handle.close()
    _children[proc.pid] = proc
    _manual_check_proc = proc
    return True, f"Check started (pid {proc.pid})."


def stop_monitor_process():
    pid, script = _read_pid()
    if not pid or not _pid_matches(pid, script):
        _clear_pid()
        return False, "Monitor is not running."
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as error:
        return False, f"Could not signal monitor process (pid {pid}): {error}"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_matches(pid, script):
        time.sleep(0.1)
    if _pid_matches(pid, script):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc = _children.pop(pid, None)
    if proc is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    _clear_pid()
    return True, "Monitor stopped."


@app.get("/")
def index():
    fieldnames, rows = load_rows()
    if fieldnames is None:
        fieldnames, rows = [], []
        flash("monitor.csv is missing or has no URL/Threshold columns.", "error")
    return render_template(
        "index.html", fieldnames=fieldnames, rows=rows, monitor=monitor_status()
    )


def parse_row_form():
    url = normalize_url(request.form.get("url", "").strip())
    threshold = request.form.get("threshold", "").strip()
    topic = normalize_topic(request.form.get("mqtt_topic", "") or "")
    if not url or not threshold:
        flash("URL and Threshold are required.", "error")
        return None
    try:
        float(threshold)
    except ValueError:
        flash("Threshold must be a number.", "error")
        return None
    if not valid_topic(topic):
        flash(
            "MQTT topic must not be empty and cannot contain spaces, '/', '#' or '+'.",
            "error",
        )
        return None
    return url, threshold, topic


@app.post("/monitor/start")
def start_monitor():
    started, message = start_monitor_process()
    flash(message, "success" if started else "error")
    return redirect(url_for("index"))


@app.post("/monitor/stop")
def stop_monitor():
    stopped, message = stop_monitor_process()
    flash(message, "success" if stopped else "error")
    return redirect(url_for("index"))


@app.post("/monitor/check-now")
def check_now_route():
    started, message = check_now_process()
    flash(message, "success" if started else "error")
    return redirect(url_for("index"))


@app.post("/monitor/settings")
def save_settings_route():
    raw_interval = request.form.get("interval", "").strip()
    try:
        minutes = float(raw_interval)
    except ValueError:
        flash("Interval must be a number.", "error")
        return redirect(url_for("index"))
    if minutes <= 0:
        flash("Interval must be greater than zero.", "error")
        return redirect(url_for("index"))
    mqtt_host = request.form.get("mqtt_host", "").strip()

    old_interval = get_interval()
    old_host = effective_mqtt_host()
    updates = {"interval": minutes}
    changed_host = mqtt_host != old_host
    if changed_host:
        updates["mqtt_host"] = mqtt_host

    was_running = monitor_status()["running"]
    try:
        save_settings(updates)
    except OSError as error:
        flash(f"Cannot write settings: {error}. Check ownership of the config folder.", "error")
        return redirect(url_for("index"))
    if was_running and (minutes != old_interval or changed_host):
        stop_monitor_process()
        start_monitor_process()
        flash(
            f"Settings saved (every {minutes:g} min"
            + (f", MQTT host {mqtt_host or 'off'}" if changed_host else "")
            + "); monitor restarted.",
            "success",
        )
    else:
        flash(f"Settings saved (check every {minutes:g} min).", "success")
    return redirect(url_for("index"))


@app.get("/monitor/status")
def monitor_status_api():
    return monitor_status()


@app.post("/reorder")
def reorder_rows():
    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list):
        return {"ok": False}
    try:
        order = [int(i) for i in order]
    except (TypeError, ValueError):
        return {"ok": False}
    try:
        with locked_csv(CSV_PATH):
            fieldnames, rows = load_rows()
            if fieldnames is None:
                return {"ok": False}
            if sorted(order) != list(range(len(rows))):
                return {"ok": False}
            rows = [rows[i] for i in order]
            write_csv(CSV_PATH, fieldnames, rows)
    except OSError:
        return {"ok": False}
    return {"ok": True}


@app.post("/delete/<int:index>")
def delete_row(index):
    try:
        with locked_csv(CSV_PATH):
            fieldnames, rows = load_rows()
            if fieldnames is None:
                flash(
                    "monitor.csv is missing or has no URL/Threshold columns.",
                    "error",
                )
                return redirect(url_for("index"))
            if index < 0 or index >= len(rows):
                flash("Row not found.", "error")
                return redirect(url_for("index"))

            row = rows.pop(index)
            write_csv(CSV_PATH, fieldnames, rows)
    except OSError as error:
        flash(CSV_WRITE_ERROR.format(error=error), "error")
        return redirect(url_for("index"))

    try:
        url = (row.get("URL") or "").strip()
        topic = (row.get("MQTTTopic") or "").strip() or (
            f"{MQTT_TOPIC_PREFIX}/{item_id(url)}" if url else None
        )
        if topic:
            asin = item_id(url) if url else None
            _mqtt_publish(topic, "")
            _mqtt_publish(topic + AVAILABILITY_TOPIC_SUFFIX, "")
            _mqtt_publish(topic + STOCK_TOPIC_SUFFIX, "")
            if asin:
                _mqtt_publish(
                    f"{MQTT_DISCOVERY_PREFIX}/sensor/apm/{asin}/config", ""
                )
                _mqtt_publish(
                    f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/apm/{asin}/config",
                    "",
                )
    except Exception:
        pass

    flash("Row deleted.", "success")
    return redirect(url_for("index"))


@app.get("/export")
def export_csv():
    try:
        with open(CSV_PATH, newline="") as csv_file:
            content = csv_file.read()
    except OSError:
        flash("No monitor.csv to export yet.", "error")
        return redirect(url_for("index"))
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=monitor-export.csv"},
    )


IMPORT_MAX_BYTES = 2 * 1024 * 1024


@app.post("/import")
def import_csv():
    upload = request.files.get("csv_file")
    if upload is None or not upload.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("index"))
    data = upload.read()
    if len(data) > IMPORT_MAX_BYTES:
        flash("That file is too large (2 MB limit).", "error")
        return redirect(url_for("index"))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() if h else h for h in (reader.fieldnames or [])]
    if any(column not in headers for column in BASE_COLUMNS):
        flash("Import failed: the CSV must contain URL and Threshold columns.", "error")
        return redirect(url_for("index"))

    ensure_columns(headers)
    clean_rows = []
    skipped = 0
    for position, row in enumerate(reader, start=1):
        url = normalize_url((row.get("URL") or "").strip())
        threshold = (row.get("Threshold") or "").strip()
        if not url:
            skipped += 1
            continue
        try:
            float(threshold)
        except ValueError:
            skipped += 1
            continue
        topic = (row.get("MQTTTopic") or "").strip()
        if not topic:
            asin = item_id(url)
            topic = f"{MQTT_TOPIC_PREFIX}/{asin or f'row{position}'}"
        clean = {name: "" for name in headers}
        clean.update({key: (value or "").strip() for key, value in row.items() if key})
        clean["URL"] = url
        clean["Threshold"] = threshold
        clean["MQTTTopic"] = topic
        seen = {r["URL"] for r in clean_rows}
        if url in seen:
            skipped += 1
            continue
        clean_rows.append(clean)

    if not clean_rows:
        flash("Import failed: no valid rows found.", "error")
        return redirect(url_for("index"))

    try:
        with locked_csv(CSV_PATH):
            write_csv(CSV_PATH, headers, clean_rows)
    except OSError as error:
        flash(f"Cannot write monitor.csv: {error}", "error")
        return redirect(url_for("index"))

    message = f"Imported {len(clean_rows)} row{'s' if len(clean_rows) != 1 else ''}."
    if skipped:
        message += f" Skipped {skipped} invalid row{'s' if skipped != 1 else ''}."
    flash(message, "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    if os.environ.get("AUTO_START", "").strip().lower() in ("1", "true", "yes", "on"):
        started, message = start_monitor_process()
        print(f"[startup] {message}", flush=True)
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5020")),
        debug=False,
    )
