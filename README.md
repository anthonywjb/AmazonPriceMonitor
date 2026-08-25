# Amazon Price Monitor (UK) for Home Assistant

A self-hosted Amazon UK price watcher. It checks product pages on a
schedule, publishes price and stock status over MQTT (with Home
Assistant MQTT discovery), and ships with a small web app for managing
everything — no config files to hand-edit.

**Features**

- Web UI: add products by URL, set a target price, edit/reorder/delete rows
- Background checker that keeps running on its own (default: every 30 minutes)
- MQTT publishing with Home Assistant auto-discovery (`price_monitor/…` topics)
- One-container deployment; your list survives updates and restarts
- CSV export/import for backups and moving between machines

---

## 1. Get the container onto your machine

Pick whichever applies:

**Pull from GitHub Container Registry** (recommended — nothing to download by hand):

```bash
docker pull ghcr.io/anthonywjb/amazon-price-monitor:latest
```

**From an image archive** (if you were given an `apm.tar.gz`):

```bash
docker load < apm.tar.gz
```

**From source** (in a checkout of this project):

```bash
docker build -t ghcr.io/anthonywjb/amazon-price-monitor:latest .
```

Check it arrived: `docker images | grep amazon-price-monitor`

## 2. Deploy it

### With Portainer

1. Generate a secret key (any long random string works):

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Portainer → **Stacks → Add stack** → name it `price-monitor` → paste:

   ```yaml
   services:
     price-monitor:
       image: ghcr.io/anthonywjb/amazon-price-monitor:latest
       container_name: price-monitor
       ports:
         - "5020:5020"
       volumes:
         - price-monitor-config:/app/config
        environment:
          - SECRET_KEY=${SECRET_KEY}
          - MQTT_HOST=${MQTT_HOST}
          - AUTO_START=${AUTO_START:-1}
          - TZ=Europe/London
        restart: unless-stopped

   volumes:
     price-monitor-config:
   ```

3. Under **Environment variables**, add `SECRET_KEY` with the value from
   step 1. `MQTT_HOST` is optional — you can set the broker later in the
   web UI.
4. Click **Deploy the stack**, then wait until the container shows as
   **healthy** (it has a built-in health check).

### Without Portainer

```bash
SECRET_KEY=<paste-your-key> docker compose up -d    # from the project folder
```

## 3. First look at the web app

Open `http://<your-host>:5020`.

- **Your monitoring list will initially be empty.** That is normal —
  the app creates a fresh, empty configuration the first time it runs,
  ready for you to add products.
- The status dot at the top should already be green (**Running**) —
  the background checker starts automatically and will re-check
  everything every 30 minutes.

## 4. Add some items to monitor

1. Scroll to the **Add a product** row at the bottom of the page.
2. Paste any Amazon UK product URL into the **URL** box. Long links
   with tags and tracking junk are fine — they're shortened
   automatically to the bare product page.
3. Enter your target price as the **Threshold**. When the current price
   drops below this, the item is flagged as *target met*.
4. Give the MQTT topic a short name in the third box (e.g.
   `garden_kneeler`). The `price_monitor/` prefix is added for you if
   you leave it off.
5. Press the **+** button. The product title and current price are
   fetched straight away so you can confirm it found the right item.

Repeat for each product you want to watch. You can fix mistakes by
editing URL/Threshold/topic directly in the table (press the floppy-disk
icon to save), reorder rows by dragging the grip handle, or remove items
with the bin icon.

## 5. Connect Home Assistant (optional)

Set the **MQTT host** field in the top bar to your broker's address and
press Save — publishing turns on immediately. Once Home Assistant points
at the same broker, sensor entities for every monitored product appear
automatically via MQTT discovery. Leave the field empty to keep
publishing switched off.

## Where your data lives

The stack stores everything (product list, settings, logs) in a Docker
named volume called `price-monitor-config`. Nothing to create, nothing
to chown — it just works, and it survives image updates and container
recreation.

Prefer plain files on the host? Replace the volume line in the stack
with `- /opt/price-monitor/config:/app/config`, then prepare once (the
app runs as uid/gid 1000):

```bash
sudo mkdir -p /opt/price-monitor/config
sudo chown -R 1000:1000 /opt/price-monitor/config
```

Moving to a new machine or switching storage types: press **Export CSV**
in the old setup, then **Import CSV** in the new one.

## Configuration reference

| Variable     | Default | Purpose                                            |
|--------------|---------|----------------------------------------------------|
| `SECRET_KEY` | random  | Session signing; set a fixed value so settings survive restarts |
| `AUTO_START` | `1`     | Start the checker when the container boots         |
| `MQTT_HOST`  | *(off)* | Broker hostname; can also be set later in the web UI |
| `TZ`         | UTC     | Container timezone; set to `Europe/London` (or similar) for correct timestamps |
| `PORT`       | `5020`  | Web UI port inside the container                   |

## Troubleshooting

- **"address already in use" on port 5020** — something else already
  owns that port. Change the mapping to e.g. `"8080:5020"` and browse to
  port 8080 instead.
- **Stack shows "Limited" in Portainer** — leftovers from an earlier
  attempt. Delete the stack, run
  `docker rm -f $(docker ps -aq --filter name=price-monitor)`, then
  redeploy.
- **Permission errors mentioning config/** — only possible with a
  host-folder bind mount; make sure the folder is owned by uid 1000
  (see above). The default named volume cannot hit this.
- **No prices appearing in Home Assistant** — check the MQTT host field;
  empty means publishing is off. Container log:
  `docker logs price-monitor --tail 20`.
