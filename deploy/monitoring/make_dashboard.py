"""Writes home-noc-dashboard.json, the Grafana dashboard for the NOC's /metrics (app/metrics.py).

Kept as code so the panels stay consistent: python3 deploy/monitoring/make_dashboard.py
"""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "noc-prom"}
ISP = 'isp=~"$isp"'
panels, pid = [], 0


def panel(kind, title, targets, x, y, w, h, unit=None, **extra):
    global pid
    pid += 1
    p = {"id": pid, "type": kind, "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"datasource": DS, "refId": chr(65 + i), **t} for i, t in enumerate(targets)],
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}, "options": {}}
    for k, v in extra.items():
        if k == "defaults":
            p["fieldConfig"]["defaults"].update(v)
        else:
            p[k] = v
    panels.append(p)


def row(title, y):
    global pid
    pid += 1
    panels.append({"id": pid, "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})


def ts(expr, legend, **kw):
    return {"expr": expr, "legendFormat": legend, **kw}


STACK = {"custom": {"stacking": {"mode": "normal", "group": "A"}, "fillOpacity": 25, "lineWidth": 1}}
BARS = {"custom": {"drawStyle": "bars", "fillOpacity": 80, "stacking": {"mode": "normal", "group": "A"}}}

row("Right now", 0)
panel("stat", "Download now", [ts(f'sum(noc_wan_rate_bits{{direction="down",{ISP}}})', "")], 0, 1, 4, 4, "bps")
panel("stat", "Upload now", [ts(f'sum(noc_wan_rate_bits{{direction="up",{ISP}}})', "")], 4, 1, 4, 4, "bps")
panel("stat", "Data used this month", [ts(f"sum(noc_wan_month_bytes{{{ISP}}})", "")], 8, 1, 4, 4, "decbytes")
panel("stat", "WANs up", [ts(f"sum(noc_wan_up{{{ISP}}})", "up"), ts(f"count(noc_wan_up{{{ISP}}})", "of")], 12, 1, 4, 4, None,
      options={"textMode": "value_and_name", "colorMode": "none"})
panel("state-timeline", "WAN up / down", [ts(f"noc_wan_up{{{ISP}}}", "{{wan}}")], 16, 1, 8, 8, None,
      defaults={"mappings": [{"type": "value", "options": {"0": {"text": "down", "color": "red"}, "1": {"text": "up", "color": "green"}}}],
                "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}})
panel("timeseries", "Download per WAN", [ts(f'noc_wan_rate_bits{{direction="down",{ISP}}}', "{{wan}}")], 0, 5, 8, 8, "bps", defaults=STACK)
panel("timeseries", "Upload per WAN", [ts(f'noc_wan_rate_bits{{direction="up",{ISP}}}', "{{wan}}")], 8, 5, 8, 8, "bps", defaults=STACK)

row("Data used", 13)
panel("bargauge", "This month per ISP", [ts(f"sum by (isp) (noc_wan_month_bytes{{{ISP}}})", "{{isp}}", instant=True)], 0, 14, 8, 8, "decbytes",
      options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True})
panel("bargauge", "This month per WAN", [ts(f"sum by (wan) (noc_wan_month_bytes{{{ISP}}})", "{{wan}}", instant=True)], 8, 14, 8, 8, "decbytes",
      options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True})
panel("bargauge", "Top devices this month", [ts("topk(15, sum by (name) (noc_device_month_bytes))", "{{name}}", instant=True)], 16, 14, 8, 8,
      "decbytes", options={"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True})
panel("timeseries", "Data per day per ISP", [ts(f"sum by (isp) (increase(noc_wan_bytes_total{{{ISP}}}[1d]))", "{{isp}}", interval="1d")],
      0, 22, 12, 8, "decbytes", defaults=BARS)
panel("timeseries", "Data per hour per WAN", [ts(f"sum by (wan) (increase(noc_wan_bytes_total{{{ISP}}}[1h]))", "{{wan}}", interval="1h")],
      12, 22, 12, 8, "decbytes", defaults=BARS)

row("Link quality", 30)
panel("timeseries", "Latency per WAN (mwan3 check)", [ts("noc_wan_latency_ms", "{{wan}}")], 0, 31, 8, 8, "ms")
panel("timeseries", "Packet loss per WAN", [ts("noc_wan_loss_percent", "{{wan}}")], 8, 31, 8, 8, "percent")
panel("timeseries", "Hourly speed test", [ts(f"noc_wan_speedtest_mbps{{{ISP}}}", "{{wan}}")], 16, 31, 8, 8, "Mbits",
      defaults={"custom": {"drawStyle": "points", "pointSize": 6}})
panel("timeseries", "Fibre received power", [ts("noc_ont_rx_dbm", "{{ont}}")], 0, 39, 12, 7, "dBm")
panel("timeseries", "Fibre module temperature", [ts("noc_ont_temp_celsius", "{{ont}}")], 12, 39, 12, 7, "celsius")

dashboard = {
    "uid": "home-noc", "title": "Home NOC", "tags": ["home-noc"], "timezone": "browser", "schemaVersion": 39, "version": 1,
    "refresh": "30s", "time": {"from": "now-24h", "to": "now"}, "editable": True, "graphTooltip": 1,
    "templating": {"list": [{"name": "isp", "label": "ISP", "type": "query", "datasource": DS, "query": "label_values(noc_wan_up, isp)",
                             "refresh": 2, "multi": True, "includeAll": True, "current": {"text": "All", "value": "$__all"},
                             "allValue": ".*"}]},
    "panels": panels,
}
out = Path(__file__).with_name("home-noc-dashboard.json")
out.write_text(json.dumps(dashboard, indent=1) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(panels)} panels)")
