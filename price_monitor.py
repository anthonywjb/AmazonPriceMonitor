import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_TOPIC_PREFIX = "price_monitor"
MQTT_DISCOVERY_PREFIX = "homeassistant"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PRICE_BLOCK_SELECTORS = [
    "#corePrice_feature_div",
    "#corePriceDisplay_desktop_feature_div",
    "#corePriceDisplay_mobile_feature_div",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
]

PRICE_INNER_SELECTOR = (
    "span.priceToPay span.a-offscreen, "
    "span.priceToPay, "
    "span.a-price span.a-offscreen, "
    "span#priceblock_ourprice, span#priceblock_dealprice"
)

NUMBER_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)"

PRICE_RE = re.compile(rf"£\s*({NUMBER_PATTERN}(?:\.\d{{1,2}})?)")

VOUCHER_AMOUNT_RE = re.compile(
    rf"£\s*({NUMBER_PATTERN}(?:\.\d{{1,2}})?)\s*-?\s*off\s*voucher",
    re.IGNORECASE,
)

APPLY_VOUCHER_RE = re.compile(
    rf"apply\s+£\s*({NUMBER_PATTERN}(?:\.\d{{1,2}})?)\s*voucher",
    re.IGNORECASE,
)

OUT_OF_STOCK_RE = re.compile(
    r"(currently unavailable|temporarily out of stock|not in stock|"
    r"no longer available|out of stock)",
    re.IGNORECASE,
)

ALTERNATIVE_ITEMS_RE = re.compile(r"consider these alternative items", re.IGNORECASE)

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Za-z0-9]{10})(?=[/?#]|$)")

BOT_CHECK_FORM_ACTION = "/errors/validateCaptcha"

VOUCHER_CONTAINER_SELECTOR = (
    "#corePrice_feature_div, "
    "#corePriceDisplay_desktop_feature_div, "
    "#corePriceDisplay_mobile_feature_div, "
    ".promoPriceBlockMessage, "
    "[id*='voucher'], "
    "[class*='voucher']"
)

CONFIG_DIR = "config"
CSV_PATH = os.path.join(CONFIG_DIR, "monitor.csv")
REQUIRED_COLUMNS = ("URL", "Threshold")
OPTIONAL_COLUMNS = (
    "InStock",
    "MQTTTopic",
    "Title",
    "CurrentPrice",
    "TargetMet",
    "TimeStamp",
)


def read_csv(path=CSV_PATH):
    with open(path, newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def write_csv(path, fieldnames, rows):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def ensure_columns(fieldnames):
    for column in OPTIONAL_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)


@contextmanager
def locked_csv(csv_path=CSV_PATH):
    lock_path = csv_path + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def parse_price(text):
    return float(text.replace(",", ""))


def looks_like_bot_check(soup):
    title = soup.title.get_text().strip().lower() if soup.title else ""
    if "captcha" in title or "robot check" in title:
        return True
    body = soup.get_text(" ", strip=True).lower()
    if len(body) < 2000 and "continue shopping" in body:
        return True
    form = soup.find(
        "form", action=lambda action: action and BOT_CHECK_FORM_ACTION in action
    )
    return form is not None


def has_out_of_stock_signal(soup):
    if soup.find(id="outOfStock"):
        return True
    availability = soup.find(id="availability")
    if availability and OUT_OF_STOCK_RE.search(availability.get_text(" ", strip=True)):
        return True
    return soup.find(string=ALTERNATIVE_ITEMS_RE) is not None


def find_price(soup):
    for selector in PRICE_BLOCK_SELECTORS:
        price_root = soup.select_one(selector)
        if price_root is None:
            continue

        if price_root.name == "span":
            elements = [price_root]
        else:
            elements = price_root.select(PRICE_INNER_SELECTOR)
        for element in elements:
            text = re.sub(r"\s+", "", element.get_text())
            match = PRICE_RE.search(text)
            if match:
                return parse_price(match.group(1))
    return None


def voucher_discount(soup):
    for element in soup.select(VOUCHER_CONTAINER_SELECTOR):
        text = element.get_text(" ", strip=True)
        for pattern in (VOUCHER_AMOUNT_RE, APPLY_VOUCHER_RE):
            match = pattern.search(text)
            if match:
                return parse_price(match.group(1))
    return None


FETCH_HEADERS = {"Accept-Language": "en-GB,en;q=0.9"}
BOT_CHECK_RETRIES = 2
BOT_CHECK_RETRY_DELAY_SECONDS = 10
ITEM_DELAY_SECONDS = 5


def get_page_data(url):
    last_error = None
    for attempt in range(1 + BOT_CHECK_RETRIES):
        response = curl_requests.get(
            url,
            headers=FETCH_HEADERS,
            timeout=15,
            impersonate="chrome",
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        title = soup.title.get_text().strip() if soup.title else ""
        title = re.sub(r"\s*:\s*Amazon\.co\.uk:.*$", "", title)

        if looks_like_bot_check(soup):
            last_error = ValueError(
                "Amazon bot-check page returned instead of product data"
            )
            if attempt < BOT_CHECK_RETRIES:
                time.sleep(BOT_CHECK_RETRY_DELAY_SECONDS)
            continue

        if has_out_of_stock_signal(soup):
            return title, None, False

        price = find_price(soup)
        if price is None:
            raise ValueError("could not find a price on the page")

        discount = voucher_discount(soup)
        if discount:
            price = max(0.0, round(price - discount, 2))
        return title, price, True
    raise last_error


def item_id(url):
    match = ASIN_RE.search(url)
    if match:
        return match.group(1).upper()
    return hashlib.md5(url.encode()).hexdigest()[:10].upper()


def mqtt_topic(url):
    return f"{MQTT_TOPIC_PREFIX}/{item_id(url)}"


def entity_name(topic):
    return topic.rstrip("/").rsplit("/", 1)[-1]


STOCK_TOPIC_SUFFIX = "/stock"
AVAILABILITY_TOPIC_SUFFIX = "/availability"


_mqtt_disabled_notice_shown = False


def mqtt_publish(topic, message, retain=True):
    global _mqtt_disabled_notice_shown
    if not MQTT_HOST:
        if not _mqtt_disabled_notice_shown:
            print("MQTT_HOST is not set; skipping MQTT publishing")
            _mqtt_disabled_notice_shown = True
        return
    command = ["mosquitto_pub", "-h", MQTT_HOST, "-t", topic]
    command += ["-m", message] if message else ["-n"]
    if retain:
        command.append("-r")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"mqtt publish failed: {error}") from error


def publish_price(topic, price):
    mqtt_publish(topic, f"{price:.2f}")


def publish_stock(topic, in_stock):
    mqtt_publish(topic + STOCK_TOPIC_SUFFIX, "ON" if in_stock else "OFF")


def publish_availability(topic):
    mqtt_publish(topic + AVAILABILITY_TOPIC_SUFFIX, "online")


def publish_discovery(asin, title, threshold, target_met, state_topic, in_stock):
    name = entity_name(state_topic)
    attributes_topic = f"{state_topic}/attributes"
    config = {
        "name": name,
        "object_id": f"price_monitor_{name}",
        "unique_id": f"price_monitor_{asin}",
        "state_topic": state_topic,
        "json_attributes_topic": attributes_topic,
        "unit_of_measurement": "GBP",
        "device_class": "monetary",
        "icon": "mdi:tag-check" if target_met else "mdi:tag",
        "availability_topic": state_topic + AVAILABILITY_TOPIC_SUFFIX,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": ["price_monitor"],
            "name": "Amazon Price Monitor",
            "manufacturer": "Amazon",
            "model": "Amazon.co.uk",
        },
    }
    topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/price_monitor/{asin}/config"
    attributes = {
        "title": title,
        "threshold": f"{threshold:.2f}",
        "target_met": target_met,
        "in_stock": "Yes" if in_stock else "No",
    }
    mqtt_publish(topic, json.dumps(config))
    mqtt_publish(attributes_topic, json.dumps(attributes))


def publish_stock_discovery(asin, state_topic):
    name = entity_name(state_topic)
    config = {
        "name": f"{name} in stock",
        "unique_id": f"price_monitor_{asin}_stock",
        "object_id": f"price_monitor_{name}_stock",
        "state_topic": state_topic + STOCK_TOPIC_SUFFIX,
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": {
            "identifiers": ["price_monitor"],
            "name": "Amazon Price Monitor",
            "manufacturer": "Amazon",
            "model": "Amazon.co.uk",
        },
    }
    topic = f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/price_monitor/{asin}/config"
    mqtt_publish(topic, json.dumps(config))


LOG_MAX_LINES = 100


def trim_log(path, max_lines=LOG_MAX_LINES):
    """Keep a log file at the last max_lines lines.

    Rewrites the file in place (same inode) so processes holding an
    append-mode file descriptor keep writing to the right file.
    Creates the file if it does not exist.
    """
    try:
        with open(path, "a+") as log_file:
            log_file.seek(0)
            lines = log_file.readlines()
            if len(lines) > max_lines:
                log_file.seek(0)
                log_file.truncate()
                log_file.writelines(lines[-max_lines:])
    except OSError:
        pass


def run_checks():
    """Run one monitoring pass over every row in the CSV.

    Prints errors for individual failing rows and keeps going. Returns
    False only if the CSV itself is missing or unusable.
    """
    try:
        fieldnames, rows = read_csv(CSV_PATH)
    except FileNotFoundError:
        print(f"Error: {CSV_PATH} not found")
        return False

    missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        print(f"Error: {CSV_PATH} is missing required column(s): {', '.join(missing)}")
        return False

    results = []
    for row_index, row in enumerate(rows):
        if row_index:
            time.sleep(ITEM_DELAY_SECONDS)
        url = (row.get("URL") or "").strip()
        if not url:
            print("Error: skipping row with empty URL")
            results.append(None)
            continue

        row["MQTTTopic"] = (row.get("MQTTTopic") or "").strip() or mqtt_topic(url)
        entry = None
        try:
            threshold = float(row["Threshold"])
            title, price, in_stock = get_page_data(url)
        except KeyError as error:
            print(f"Error: skipping malformed row for {url}: missing column {error}")
            results.append(None)
            continue
        except (requests.RequestException, CurlRequestException, ValueError) as error:
            print(f"Error for {url}: {error}")
            results.append(entry)
            continue

        target_met = in_stock and price < threshold

        topic = row["MQTTTopic"]
        asin = item_id(url)
        try:
            publish_discovery(asin, title, threshold, target_met, topic, in_stock)
            publish_stock_discovery(asin, topic)
            publish_availability(topic)
            publish_stock(topic, in_stock)
            if in_stock:
                publish_price(topic, price)
        except RuntimeError as error:
            print(f"Error: {error}")

        if in_stock:
            print(f"{title} - Price: £{price:.2f} - {'Yes' if target_met else 'No'}")
        else:
            print(f"{title} - NOT IN STOCK")

        results.append(
            {
                "Title": title,
                "InStock": "Yes" if in_stock else "No",
                "CurrentPrice": f"{price:.2f}" if in_stock else "",
                "TimeStamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    with locked_csv(CSV_PATH):
        try:
            fieldnames, fresh_rows = read_csv(CSV_PATH)
        except FileNotFoundError:
            fresh_rows = []
        ensure_columns(fieldnames)

        pending = {}
        for row, entry in zip(rows, results):
            if entry is not None:
                key = (row.get("URL") or "").strip()
                pending.setdefault(key, []).append(entry)

        for row in fresh_rows:
            url = (row.get("URL") or "").strip()
            if url:
                row["MQTTTopic"] = (
                    (row.get("MQTTTopic") or "").strip() or mqtt_topic(url)
                )
            queue = pending.get(url)
            if not queue:
                continue
            entry = queue.pop(0)
            row["Title"] = entry["Title"]
            row["InStock"] = entry["InStock"]
            row["CurrentPrice"] = entry["CurrentPrice"]
            row["TimeStamp"] = entry["TimeStamp"]
            row["TargetMet"] = "No"
            if entry["InStock"] == "Yes":
                try:
                    row["TargetMet"] = (
                        "Yes"
                        if float(entry["CurrentPrice"]) < float(row["Threshold"])
                        else "No"
                    )
                except (KeyError, TypeError, ValueError):
                    pass

        write_csv(CSV_PATH, fieldnames, fresh_rows)

    return True


def main():
    parser = argparse.ArgumentParser(description="Monitor Amazon product prices")
    parser.add_argument(
        "--interval",
        type=float,
        default=0,
        metavar="MINUTES",
        help="keep running and check every MINUTES minutes "
        "(default: run one pass and exit)",
    )
    parser.add_argument(
        "--log-file",
        default="",
        metavar="PATH",
        help="log file to keep trimmed to the last 100 lines while looping",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        if not run_checks():
            sys.exit(1)
        return

    if args.log_file:
        trim_log(args.log_file)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    print(f"Monitoring every {args.interval:g} minute(s). Press Ctrl+C to stop.")
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"--- check started at {stamp} ---")
        try:
            run_checks()
        except Exception as error:
            print(f"Unexpected error during check: {error}")
        if args.log_file:
            trim_log(args.log_file)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
