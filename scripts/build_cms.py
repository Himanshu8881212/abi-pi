#!/usr/bin/env python3
"""The YouTube CMS demo project, built through ABI actions only (as the signed-in person): medallion layers on the
Parquet files in the local RustFS bucket (converters/gen_cms.py, generated data on YouTube's Reporting API schemas).

  bronze_*  one source per report folder, exactly as landed (batch history and the streamed days separately)
  silver_*  typed, renamed to readable names, batch + stream appended and de-duplicated; hidden from the model
  gold_*    business grain: daily facts and conformed dimensions; the semantic model sits on these only

    .venv/bin/python scripts/build_cms.py [workspace] [project]        (default: youtube yt_cms)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import abi  # noqa: E402

WS, P = (sys.argv[1:3] + ["youtube", "yt_cms"][len(sys.argv[1:3]):])[:2]
S3 = {"endpoint": "127.0.0.1:9100", "url_style": "path", "use_ssl": False, "region": "us-east-1"}
BUCKET = "s3://yt-cms"

BRONZE = {  # table: (folder, stream folder or None)
    "bronze_basic": "batch/content_owner_basic_a4", "bronze_basic_stream": "stream/content_owner_basic_a4",
    "bronze_revenue": "batch/content_owner_estimated_revenue_a1", "bronze_revenue_stream": "stream/content_owner_estimated_revenue_a1",
    "bronze_traffic": "batch/content_owner_traffic_source_a3", "bronze_device": "batch/content_owner_device_os_a3",
    "bronze_ad_rates": "batch/content_owner_ad_rates_a1", "bronze_videos": "batch/content_owner_video_metadata_a4",
    "bronze_assets": "batch/content_owner_asset_a3", "bronze_claims": "batch/content_owner_active_claims_a3",
    "ref_countries": "batch/reference/countries", "ref_traffic_sources": "batch/reference/traffic_source_types",
    "ref_devices": "batch/reference/device_types", "ref_operating_systems": "batch/reference/operating_systems",
    "ref_ad_types": "batch/reference/ad_types",
}
SILVER_BASIC = [{"type": "source", "source": "bronze_basic"}, {"type": "append", "tables": ["bronze_basic_stream"]},
                {"type": "deduplicate", "columns": ["date", "video_id", "country_code", "subscribed_status"]}]
SILVER_REVENUE = [{"type": "source", "source": "bronze_revenue"}, {"type": "append", "tables": ["bronze_revenue_stream"]},
                  {"type": "deduplicate", "columns": ["date", "video_id", "country_code"]}]
SILVER_VIDEOS = [{"type": "source", "source": "bronze_videos"},
                 {"type": "add_column", "name": "format", "expression": "CASE WHEN video_length <= 60 THEN 'Shorts' ELSE 'Long-form' END"},
                 {"type": "add_column", "name": "published_date", "expression": "CAST(time_published AS DATE)"},
                 {"type": "deduplicate", "columns": ["video_id"]}]
SUMS = lambda *cols: [{"name": c, "function": "sum", "column": c} for c in cols]  # noqa: E731
TABLES = {  # silver (hidden) and gold, in build order: (steps, kind or None for hidden)
    "silver_basic": (SILVER_BASIC, None), "silver_revenue": (SILVER_REVENUE, None), "silver_videos": (SILVER_VIDEOS, None),
    "gold_video_daily": ([*SILVER_BASIC,
                          {"type": "group", "by": ["date", "channel_id", "video_id", "country_code"],
                           "aggregations": SUMS("views", "engaged_views", "watch_time_minutes", "subscribers_gained", "subscribers_lost",
                                                "likes", "dislikes", "comments", "shares", "red_views")}], "fact"),
    "gold_revenue_daily": ([*SILVER_REVENUE,
                            {"type": "group", "by": ["date", "channel_id", "video_id", "country_code"],
                             "aggregations": SUMS("estimated_partner_revenue", "estimated_partner_ad_revenue", "estimated_youtube_ad_revenue",
                                                  "estimated_partner_red_revenue", "estimated_monetized_playbacks", "ad_impressions")}], "fact"),
    "gold_traffic_daily": ([{"type": "source", "source": "bronze_traffic"},
                            {"type": "group", "by": ["date", "channel_id", "traffic_source_type"],
                             "aggregations": SUMS("views", "watch_time_minutes")}], "fact"),
    "gold_device_daily": ([{"type": "source", "source": "bronze_device"},
                           {"type": "group", "by": ["date", "channel_id", "device_type", "operating_system"],
                            "aggregations": SUMS("views", "watch_time_minutes")}], "fact"),
    "gold_ad_rates_daily": ([{"type": "source", "source": "bronze_ad_rates"},
                             {"type": "group", "by": ["date", "country_code", "ad_type"],
                              "aggregations": SUMS("estimated_youtube_ad_revenue", "ad_impressions")}], "fact"),
    "gold_claims": ([{"type": "source", "source": "bronze_claims"},
                     {"type": "select", "columns": ["claim_id", "video_id", "asset_id", "channel_id", "claim_status", "claim_origin",
                                                    "claim_type", "uploader", "claim_policy_monetize", "claim_created_date"]}], "fact"),
    "dim_video": ([*SILVER_VIDEOS,
                   {"type": "select", "columns": ["video_id", "video_title", "category", "format", "published_date", "video_length",
                                                  "content_type", "video_privacy_status", "midrolls_enabled", "video_url"]}], "dimension"),
    "dim_channel": ([*SILVER_VIDEOS,
                     {"type": "group", "by": ["channel_id", "channel_display_name"], "aggregations": [{"name": "videos", "function": "count", "column": None}]}], "dimension"),
    "dim_asset": ([{"type": "source", "source": "bronze_assets"},
                   {"type": "select", "columns": ["asset_id", "custom_id", "asset_type", "asset_labels", "match_policy", "ownership"]}], "dimension"),
    "dim_country": ([{"type": "source", "source": "ref_countries"}, {"type": "select", "columns": ["country_code", "country_name", "region"]}], "dimension"),
    "dim_traffic_source": ([{"type": "source", "source": "ref_traffic_sources"}, {"type": "select", "columns": ["traffic_source_type", "traffic_source_name"]}], "dimension"),
    "dim_device": ([{"type": "source", "source": "ref_devices"}, {"type": "select", "columns": ["device_type", "device_name"]}], "dimension"),
    "dim_operating_system": ([{"type": "source", "source": "ref_operating_systems"},
                              {"type": "deduplicate", "columns": ["operating_system"]},
                              {"type": "select", "columns": ["operating_system", "operating_system_name"]}], "dimension"),
    "dim_ad_type": ([{"type": "source", "source": "ref_ad_types"}, {"type": "select", "columns": ["ad_type", "ad_type_name"]}], "dimension"),
}
KEYS = {"dim_video": ["video_id"], "dim_channel": ["channel_id"], "dim_asset": ["asset_id"], "dim_country": ["country_code"],
        "dim_traffic_source": ["traffic_source_type"], "dim_device": ["device_type"], "dim_operating_system": ["operating_system"],
        "dim_ad_type": ["ad_type"]}
DESCRIBE = {
    "gold_video_daily": "Daily audience per video and country (views, watch time, subscribers, engagement); batch history plus streamed days.",
    "gold_revenue_daily": "Daily estimated revenue per video and country (partner, ad, Premium revenue; monetized playbacks; impressions).",
    "gold_traffic_daily": "Daily views and watch time per channel and traffic source type.",
    "gold_device_daily": "Daily views and watch time per channel, device type and operating system.",
    "gold_ad_rates_daily": "Daily gross ad revenue and impressions per country and ad type.",
    "gold_claims": "The owner's claims: one row per claim of a video against an asset.",
    "dim_video": "One row per video: title, category, format (Shorts up to 60 s), publish date, length, content type.",
    "dim_channel": "One row per channel of the content owner, with its number of videos.",
    "dim_asset": "One row per asset: type, labels, match policy, ownership.",
    "dim_country": "Countries by ISO code, with name and region.",
    "dim_traffic_source": "YouTube's traffic source type codes and names.",
    "dim_device": "YouTube's device type codes and names.",
    "dim_operating_system": "YouTube's operating system codes and names.",
    "dim_ad_type": "YouTube's ad types (auction and reserved) and names.",
    "calendar": "Calendar of every day, for time intelligence (generated by ABI).",
}
JOINS = [("gold_video_daily.video_id", "dim_video.video_id"), ("gold_video_daily.channel_id", "dim_channel.channel_id"),
         ("gold_video_daily.country_code", "dim_country.country_code"), ("gold_revenue_daily.video_id", "dim_video.video_id"),
         ("gold_revenue_daily.channel_id", "dim_channel.channel_id"), ("gold_revenue_daily.country_code", "dim_country.country_code"),
         ("gold_traffic_daily.channel_id", "dim_channel.channel_id"), ("gold_traffic_daily.traffic_source_type", "dim_traffic_source.traffic_source_type"),
         ("gold_device_daily.channel_id", "dim_channel.channel_id"), ("gold_device_daily.device_type", "dim_device.device_type"),
         ("gold_device_daily.operating_system", "dim_operating_system.operating_system"), ("gold_ad_rates_daily.country_code", "dim_country.country_code"),
         ("gold_ad_rates_daily.ad_type", "dim_ad_type.ad_type"), ("gold_claims.video_id", "dim_video.video_id"),
         ("gold_claims.asset_id", "dim_asset.asset_id"), ("gold_claims.channel_id", "dim_channel.channel_id")]
V, R, T, Dv, A = "gold_video_daily", "gold_revenue_daily", "gold_traffic_daily", "gold_device_daily", "gold_ad_rates_daily"
MEASURES = [  # id, name, MEL, description (definitions follow YouTube's Reporting API metric descriptions; see the vault)
    ("views", "Views", f"SUM({V}.views)", "Legitimate views of the owner's claimed videos."),
    ("engaged_views", "Engaged views", f"SUM({V}.engaged_views)", "Views past the first frame or started by a click or tap."),
    ("watch_time_hours", "Watch time (hours)", f"SUM({V}.watch_time_minutes) / 60", "Minutes watched, in hours."),
    ("avg_view_duration_sec", "Average view duration (s)", f"DIVIDE(SUM({V}.watch_time_minutes) * 60, SUM({V}.views))", "Watch time per view, in seconds."),
    ("subscribers_gained", "Subscribers gained", f"SUM({V}.subscribers_gained)", None),
    ("subscribers_lost", "Subscribers lost", f"SUM({V}.subscribers_lost)", None),
    ("net_subscribers", "Net subscribers", "[subscribers_gained] - [subscribers_lost]", "Gained minus lost."),
    ("likes", "Likes", f"SUM({V}.likes)", None), ("comments", "Comments", f"SUM({V}.comments)", None), ("shares", "Shares", f"SUM({V}.shares)", None),
    ("engagement_rate", "Engagement rate", "DIVIDE([likes] + [comments] + [shares], [views])", "Likes, comments and shares per view."),
    ("partner_revenue", "Estimated partner revenue (USD)", f"SUM({R}.estimated_partner_revenue)", "The owner's net revenue from ads and YouTube Premium."),
    ("partner_ad_revenue", "Estimated partner ad revenue (USD)", f"SUM({R}.estimated_partner_ad_revenue)", None),
    ("youtube_ad_revenue", "Estimated YouTube ad revenue (USD)", f"SUM({R}.estimated_youtube_ad_revenue)", "Gross ad revenue before the revenue split."),
    ("premium_revenue", "Estimated YouTube Premium revenue (USD)", f"SUM({R}.estimated_partner_red_revenue)", None),
    ("monetized_playbacks", "Estimated monetized playbacks", f"SUM({R}.estimated_monetized_playbacks)", "Playbacks that showed at least one ad."),
    ("ad_impressions", "Ad impressions", f"SUM({R}.ad_impressions)", None),
    ("cpm", "CPM (USD)", "DIVIDE([youtube_ad_revenue] * 1000, [ad_impressions])", "Gross ad revenue per thousand ad impressions."),
    ("playback_cpm", "Playback-based CPM (USD)", "DIVIDE([youtube_ad_revenue] * 1000, [monetized_playbacks])", "Gross ad revenue per thousand monetized playbacks."),
    ("rpm", "RPM (USD)", "DIVIDE([partner_revenue] * 1000, [views])", "Partner revenue per thousand views, after YouTube's share."),
    ("traffic_views", "Views by traffic source", f"SUM({T}.views)", "Views at channel x traffic source grain."),
    ("device_views", "Views by device", f"SUM({Dv}.views)", "Views at channel x device x OS grain."),
    ("ad_type_revenue", "YouTube ad revenue by ad type (USD)", f"SUM({A}.estimated_youtube_ad_revenue)", None),
    ("claims", "Claims", "COUNT(gold_claims.claim_id)", None),
    ("active_claims", "Active claims", "WHERE([claims], gold_claims.claim_status = 'active')", None),
    ("videos", "Videos with views", f"DISTINCT_COUNT({V}.video_id)", None),
    ("views_prev_year", "Views, previous year", "PREVIOUS_YEAR([views])", None),
    ("revenue_prev_year", "Partner revenue, previous year", "PREVIOUS_YEAR([partner_revenue])", None),
]


def step(msg: str, fn, *a, **k):
    t = time.time()
    try:
        out = fn(*a, **k)
        print(f"ok   {msg} ({time.time() - t:.0f}s)", flush=True)
        return out
    except abi.ActionError as e:
        print(f"FAIL {msg}: {e}", flush=True)
        return None


def main() -> None:
    step(f"project {WS}/{P}", abi.new_project, WS, P, "YouTube CMS (demo)")
    abi.sync(P)  # a rerun skips what ABI has already committed (sources and step tables)
    dest = abi.folder(P)
    built = {f.stem for d in ("sources", "tables") for f in (dest / d).glob("*.json")}
    for table, folder in BRONZE.items():
        if table in built:
            continue
        step(f"bronze {table}", abi.call, "add_data", project=P, table=table, location=f"{BUCKET}/{folder}/*.parquet", format="parquet", s3=S3)
    for table, (steps, kind) in TABLES.items():
        if table not in built:
            step(f"{table}", abi.call, "set_steps", project=P, table=table, steps=steps)
    step("build_model", abi.call, "build_model", project=P)
    hide = [t for t in BRONZE] + [t for t, (_, k) in TABLES.items() if k is None]
    for t in hide:
        step(f"hide {t}", abi.call, "set_table", project=P, table=t, hidden=True)
    for t, (_, kind) in TABLES.items():
        if kind:
            step(f"{kind} {t}", abi.call, "set_table", project=P, table=t, kind=kind, key=KEYS.get(t), description=DESCRIBE.get(t))
    step("describe calendar", abi.call, "set_table", project=P, table="calendar", description=DESCRIBE["calendar"])
    sm = abi.call("semantic_model", project=P)
    have = {(r["from"]["table"] + "." + r["from"]["columns"][0], r["to"]["table"] + "." + r["to"]["columns"][0]) for r in sm.get("relationships", [])}
    for a, b in JOINS:
        if (a, b) not in have:
            step(f"join {a} -> {b}", abi.call, "add_relationship", project=P, **{"from": a, "to": b})
    for mid, name, mel, desc in MEASURES:
        step(f"measure {mid}", abi.call, "set_measure", project=P, id=mid, name=name, expression=mel, description=desc)
    print(json.dumps(abi.call("readiness", project=P)))
    abi.sync(P)


if __name__ == "__main__":
    main()
