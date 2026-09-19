"""Exercise dashboard SQL grouping with synthetic, independently reset counters."""

import json
import sqlite3
from pathlib import Path

import pytest


DASHBOARD = json.loads((Path(__file__).resolve().parents[1] / "tools/observability/dashboards/links.json").read_text())
PANELS = DASHBOARD["definition"]["spec"]["panels"]


def query(panel):
    return PANELS[panel]["spec"]["queries"][0]["spec"]["plugin"]["spec"]["query"]


class CounterIncrease:
    """Logfire owns reset arithmetic; this stand-in rejects merged streams."""

    def __init__(self):
        self.streams = set()
        self.readings = []

    def step(self, value, timestamp):
        sample = json.loads(value)
        self.streams.add(sample["stream"])
        self.readings.append(sample["value"])

    def finalize(self):
        assert len(self.streams) == 1, "Counter streams must be aggregated independently"
        return max(self.readings) - min(self.readings)


class CounterRate(CounterIncrease):
    def finalize(self):
        return super().finalize() / 60


@pytest.fixture
def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.create_aggregate("metric_increase", 2, CounterIncrease)
    db.create_aggregate("metric_rate", 2, CounterRate)
    db.create_function("time_bucket", 2, lambda resolution, timestamp: timestamp // 60 * 60)
    db.create_function("date_trunc", 2, lambda resolution, timestamp: timestamp)
    db.create_function("json_get_int", 2, lambda value, key: json.loads(value).get(key))
    db.executescript("""
        CREATE TABLE metrics (
            service_name TEXT, deployment_environment TEXT, service_version TEXT,
            service_instance_id TEXT, process_pid INTEGER, metric_name TEXT,
            attributes TEXT, value TEXT, recorded_timestamp INTEGER
        );
        CREATE TABLE records (
            service_name TEXT, deployment_environment TEXT, kind TEXT,
            span_name TEXT, attributes TEXT, start_timestamp INTEGER,
            trace_id TEXT, span_id TEXT
        );
    """)

    def metric(provider, stage, reason, outcome, count, *, name="bot.links.attempts", version="a", instance="one", pid=1, **scope):
        attributes = {"provider": provider, "link.stage": stage, "link.reason": reason, "outcome": outcome}
        stream = repr((version, instance, pid, attributes))
        for timestamp, value in ((0, 0), (30, count)):
            db.execute(
                "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.get("service", "msu-hub-bot"),
                    scope.get("environment", "production"),
                    version,
                    instance,
                    pid,
                    name,
                    json.dumps(attributes),
                    json.dumps({"stream": stream, "value": value}),
                    timestamp,
                ),
            )

    metric("youtube", "delivery", "ready", "success", 15)
    metric("youtube", "delivery", "ready", "success", 5, version="b")
    metric("youtube", "delivery", "ready", "success", 3, instance="two")
    metric("youtube", "delivery", "ready", "success", 2, pid=2)
    metric("youtube", "extract", "empty", "unavailable", 2)
    metric("youtube", "extract", "invalid_response", "unavailable", 1)
    metric("youtube", "video", "timeout", "timeout", 2)
    metric("youtube", "route", "busy", "rejected", 1)
    metric("youtube", "route", "disabled", "ignored", 20)
    metric("youtube", "route", "policy", "ignored", 3)
    metric("youtube", "delivery", "cancelled", "cancelled", 1)
    metric("ytdlp", "extract", "unsupported", "ignored", 100)
    metric("instagram", "route", "disabled", "ignored", 4)
    metric("tiktok", "delivery", "ready", "success", 8)
    metric("tiktok", "extract", "empty", "unavailable", 2)
    metric("fxembed", "delivery", "ready", "success", 4)
    metric("vk", "delivery", "ready", "success", 1)
    metric("fxembed", "request", "http_error", "unavailable", 7, name="bot.links.steps")
    metric("fxembed", "adapter", "invalid_response", "unavailable", 2, name="bot.links.steps")
    metric("youtube", "delivery", "ready", "success", 1000, environment="testing")
    metric("youtube", "delivery", "ready", "success", 1000, service="unrelated")
    yield db
    db.close()


def test_provider_totals_keep_skips_and_recoverable_steps_out_of_failure_rate(database):
    rows = {row["provider"]: dict(row) for row in database.execute(query("providers"))}
    assert set(rows) == {"youtube", "instagram", "tiktok", "fxembed", "vk", "ytdlp"}
    assert rows["youtube"] == {
        "provider": "youtube",
        "previews": 55,
        "delivered": 25,
        "failed": 5,
        "declined": 1,
        "skipped": 23,
        "cancelled": 1,
        "delivered_percent": pytest.approx(100 * 25 / 31),
    }
    assert rows["fxembed"]["failed"] == 0
    assert rows["fxembed"]["delivered_percent"] == 100
    assert rows["tiktok"]["delivered_percent"] == 80
    assert rows["ytdlp"]["delivered_percent"] is None
    assert rows["instagram"]["delivered_percent"] is None


def test_failure_causes_count_posts_and_steps_separately(database):
    causes = {row["cause"]: row["previews"] for row in database.execute(query("terminal-causes"))}
    assert causes == {
        "youtube · extract · empty": 2,
        "youtube · extract · invalid_response": 1,
        "youtube · video · timeout": 2,
        "youtube · route · busy": 1,
        "tiktok · extract · empty": 2,
    }
    steps = [dict(row) for row in database.execute(query("step-causes"))]
    assert [row["steps"] for row in steps] == [7, 2]
    assert all(row["provider"] == "fxembed" for row in steps)


@pytest.mark.parametrize(
    "panel,dimension,measure", [("outcomes", "outcome", "previews_per_minute"), ("deliveries", "provider", "delivered_per_minute")]
)
def test_trends_use_counter_rates_and_explicit_chart_dimensions(database, panel, dimension, measure):
    plugin = PANELS[panel]["spec"]["queries"][0]["spec"]["plugin"]["spec"]
    assert plugin["groupBy"] == dimension and plugin["metrics"] == [measure]
    rows = {row[dimension]: row[measure] for row in database.execute(query(panel), {"resolution": "1m"})}
    if panel == "deliveries":
        assert rows == {"youtube": 25, "tiktok": 8, "fxembed": 4, "vk": 1}
    else:
        assert rows == {"success": 38, "unavailable": 5, "timeout": 2, "rejected": 1, "ignored": 127, "cancelled": 1}


@pytest.mark.parametrize(
    "panel,event,operation",
    [("recent-failures", "bot.link.completed", "links.preview"), ("recent-steps", "bot.link.step.failed", "links.step")],
)
def test_recent_incidents_are_bounded_scoped_and_keep_source_context(database, panel, event, operation):
    def record(timestamp, *, service="msu-hub-bot", environment="production", outcome="timeout", name=event, kind="log"):
        attributes = {
            "operation": operation,
            "provider": "tiktok",
            "link.stage": "request",
            "link.reason": "timeout",
            "link.source_url": "https://www.tiktok.com/@example/video/1234567890123456789",
            "outcome": outcome,
            "telegram.chat_id": -100123,
            "telegram.user_id": 456,
            "telegram.message_id": 7,
            "telegram.thread_id": 8,
            "http.response.status_code": 504,
            "duration_ms": 15000,
        }
        database.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (service, environment, kind, name, json.dumps(attributes), timestamp, "synthetic-trace", "synthetic-span"),
        )

    for timestamp in range(60):
        record(timestamp)
    for override in (
        {"service": "unrelated"},
        {"environment": "testing"},
        {"outcome": "success"},
        {"outcome": "ignored"},
        {"outcome": "cancelled"},
        {"name": "bot.operation.failed"},
        {"kind": "span"},
    ):
        record(100, **override)
    rows = [dict(row) for row in database.execute(query(panel))]
    assert len(rows) == 40 and rows[0]["time_utc"] == 59 and rows[-1]["time_utc"] == 20
    assert rows[0] | {"time_utc": 0} == {
        "time_utc": 0,
        "provider": "tiktok",
        "stage": "request",
        "reason": "timeout",
        "outcome": "timeout",
        "source_url": "https://www.tiktok.com/@example/video/1234567890123456789",
        "chat_id": -100123,
        "user_id": 456,
        "message_id": 7,
        "thread_id": 8,
        "http_status": 504,
        "duration_ms": 15000,
        "trace_id": "synthetic-trace",
        "span_id": "synthetic-span",
    }


def test_dashboard_is_portable_and_layout_shows_each_panel_without_overlap():
    definition = DASHBOARD["definition"]
    assert definition["metadata"]["project"] == "PROJECT_NAME"
    items = definition["spec"]["layouts"][0]["spec"]["items"]
    assert {item["content"]["$ref"] for item in items} == {f"#/spec/panels/{key}" for key in PANELS}
    occupied = set()
    for item in items:
        assert item["width"] > 0 and item["height"] > 0 and 0 <= item["x"] < item["x"] + item["width"] <= 24
        cells = {(x, y) for x in range(item["x"], item["x"] + item["width"]) for y in range(item["y"], item["y"] + item["height"])}
        assert not occupied & cells
        occupied.update(cells)
    for panel in PANELS.values():
        spec = panel["spec"]
        time_series = spec["plugin"]["kind"] == "TimeSeriesChart"
        assert spec["queries"][0]["kind"] == ("TimeSeriesQuery" if time_series else "NonTimeSeriesQuery")
        sql = spec["queries"][0]["spec"]["plugin"]["spec"]["query"]
        if "FROM metrics" in sql:
            assert "link.source_url" not in sql and "telegram." not in sql
