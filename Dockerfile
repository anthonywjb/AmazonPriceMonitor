FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends mosquitto-clients tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY price_monitor.py monitor_web.py ./
COPY templates/ templates/
COPY static/ static/

RUN useradd --uid 1000 --create-home monitor \
 && mkdir -p /app/config \
 && chown -R monitor:monitor /app
USER monitor

ENV HOST=0.0.0.0 PORT=5020 PYTHONUNBUFFERED=1
EXPOSE 5020

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c 'import os,urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/monitor/status" % os.environ.get("PORT","5020"), timeout=4)' || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "monitor_web.py"]
