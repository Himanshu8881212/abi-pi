#!/usr/bin/env python3
"""Outside the app: a generated YouTube CMS (content owner) export, on the column names of YouTube's Reporting API
(content_owner_basic_a4, _traffic_source_a3, _device_os_a3, _estimated_revenue_a1, _ad_rates_a1 and the
system-managed content_owner_video_metadata_a4, _asset_a3, _active_claims_a3). The numbers are generated, not real:
one fictional content owner, 120 channels, 80,000 videos, 3 years of days. One Parquet file per report per day.

    .venv/bin/python converters/gen_cms.py <out folder> [--stream-days 30] [--scale 1.0]

Layout:  batch/<report>/<report>.parquet (dimensions), batch/<report>/<day>.parquet (facts, days before the stream
window), pending/<report>/<day>.parquet (the last --stream-days days of basic and estimated revenue, for replay.py),
batch/reference/*.parquet (countries and YouTube's code lists).

Planted for analysis: Shorts (traffic source 24) grow through 2026; channel #7 loses monetization on 2026-09-01;
Q4 lifts CPMs; weekends lift views.
"""
import argparse
import time
from pathlib import Path

import duckdb

START, END = "2023-10-30", "2026-10-28"
# ISO country, name, region, audience weight, base CPM (USD)
COUNTRIES = [("US", "United States", "Americas", 30, 18.0), ("IN", "India", "Asia", 22, 1.6), ("GB", "United Kingdom", "Europe", 7, 12.5),
             ("DE", "Germany", "Europe", 6, 11.0), ("BR", "Brazil", "Americas", 6, 2.8), ("CA", "Canada", "Americas", 4, 13.0),
             ("FR", "France", "Europe", 4, 9.5), ("JP", "Japan", "Asia", 4, 8.0), ("MX", "Mexico", "Americas", 3, 2.4),
             ("ID", "Indonesia", "Asia", 3, 1.2), ("AU", "Australia", "Oceania", 2, 14.0), ("ES", "Spain", "Europe", 2, 6.0),
             ("IT", "Italy", "Europe", 2, 6.5), ("PH", "Philippines", "Asia", 2, 1.1), ("NL", "Netherlands", "Europe", 1, 10.5),
             ("PL", "Poland", "Europe", 1, 4.0), ("TR", "Turkey", "Asia", 1, 1.5), ("KR", "South Korea", "Asia", 1, 7.5),
             ("SE", "Sweden", "Europe", 1, 11.5), ("ZA", "South Africa", "Africa", 1, 3.0)]
TRAFFIC = [(0, "Direct or unknown", 6), (1, "YouTube advertising", 2), (3, "Browse features", 24), (4, "YouTube channels", 4),
           (5, "YouTube search", 16), (7, "Suggested videos", 26), (8, "Other YouTube features", 2), (9, "External", 5),
           (11, "Video cards and annotations", 1), (14, "Playlists", 4), (17, "Notifications", 3), (18, "Playlist pages", 1),
           (24, "Shorts", 6)]
DEVICES = [(101, "Computer", 22), (102, "TV", 14), (103, "Game console", 1), (104, "Mobile phone", 56), (105, "Tablet", 7)]
OS = [(2, "Windows", 101), (9, "Macintosh", 101), (13, "Linux", 101), (19, "Smart TV", 102), (21, "Chromecast", 102),
      (26, "Roku", 102), (17, "Xbox", 103), (10, "PlayStation", 103), (4, "Android", 104), (5, "iOS", 104),
      (4, "Android", 105), (5, "iOS", 105)]
AD_TYPES = [(1, "auctionTrueviewInstream", "Skippable video ads (auction)", 55), (2, "auctionDisplay", "Display ads (auction)", 8),
            (3, "auctionInstream", "Non-skippable video ads (auction)", 15), (19, "auctionBumperInstream", "Bumper ads (auction)", 12),
            (15, "reservedInstreamSelect", "Skippable video ads (reserved)", 6), (6, "reservedInstream", "Non-skippable video ads (reserved)", 4)]
CATEGORIES = ["Gaming", "Music", "Education", "Entertainment", "Science & Technology", "Howto & Style", "Sports", "Comedy"]
WORDS = ["Night", "City", "Signal", "River", "Echo", "Atlas", "Pixel", "Summit", "Harbor", "Orbit", "Velvet", "Granite", "Ember",
         "Lumen", "Delta", "Nomad", "Quartz", "Cobalt", "Willow", "Vector"]
TOPICS = ["Live Session", "Full Walkthrough", "Explained", "Highlights", "Behind the Scenes", "Tutorial", "Review", "Remix",
          "Episode", "Top 10", "Challenge", "Documentary", "Unboxing", "Masterclass", "Q&A", "Official Video"]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--stream-days", type=int, default=30)
    ap.add_argument("--scale", type=float, default=1.0, help="1.0 = about 100M fact rows")
    a = ap.parse_args(argv)
    out = a.out.resolve()
    t0 = time.time()
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false; SET threads=6; SET memory_limit='10GB'")
    con.execute("SELECT setseed(0.42)")
    q = lambda sql: con.execute(sql)  # noqa: E731
    lit = lambda rows: ",".join("(" + ",".join(repr(v) if isinstance(v, str) else str(v) for v in r) + ")" for r in rows)  # noqa: E731
    q(f"CREATE TABLE countries AS SELECT * FROM (VALUES {lit(COUNTRIES)}) t(country_code, country_name, region, weight, base_cpm)")
    q(f"CREATE TABLE traffic AS SELECT * FROM (VALUES {lit(TRAFFIC)}) t(traffic_source_type, traffic_source_name, weight)")
    q(f"CREATE TABLE devices AS SELECT * FROM (VALUES {lit(DEVICES)}) t(device_type, device_name, weight)")
    q(f"CREATE TABLE os AS SELECT * FROM (VALUES {lit(OS)}) t(operating_system, operating_system_name, device_type)")
    q(f"CREATE TABLE ad_types AS SELECT * FROM (VALUES {lit(AD_TYPES)}) t(ad_type_code, ad_type, ad_type_name, weight)")
    n_ch, n_vid = 120, int(80000 * a.scale)

    # ---- dimensions: channels, videos (video_metadata_a4), assets (asset_a3), claims (active_claims_a3) ----
    q(f"""CREATE TABLE channels AS SELECT i AS channel_no,
          'UC' || substr(md5('ch' || i), 1, 22) AS channel_id,
          {WORDS!r}[1 + (i * 7) % 20] || ' ' || {WORDS!r}[1 + (i * 13 + 3) % 20] || CASE WHEN i % 3 = 0 THEN ' TV' WHEN i % 3 = 1 THEN ' Studio' ELSE ' Live' END AS channel_display_name,
          {CATEGORIES!r}[1 + i % 8] AS category, 0.4 + random() * 2.2 AS pop
        FROM range(1, {n_ch + 1}) t(i)""")
    q(f"""CREATE TABLE videos AS SELECT v AS video_no, substr(md5('v' || v), 1, 11) AS video_id, c.channel_id, c.channel_display_name,
          c.category, c.channel_no,
          {WORDS!r}[1 + (v * 3) % 20] || ' ' || {TOPICS!r}[1 + (v * 11) % 16] || ' #' || (v % 997) AS video_title,
          TIMESTAMP '2021-06-01' + INTERVAL (floor(pow(random(), 0.6) * 1975)) DAY + INTERVAL (floor(random() * 86400)) SECOND AS time_published,
          CASE WHEN random() < 0.22 THEN 'short' ELSE 'long' END AS format,
          exp(random() * 2.2 - 0.3) * c.pop AS pop
        FROM range(1, {n_vid + 1}) t(v) JOIN channels c ON c.channel_no = 1 + (hash(v) % {n_ch})""")
    q("""CREATE TABLE video_metadata AS SELECT video_id, channel_id, 'C' || substr(md5('cl' || video_no), 1, 10) AS claim_id,
          'A' || lpad(CAST(100000 + video_no % 30000 AS VARCHAR), 9, '0') AS asset_id, 'NWM-' || lpad(CAST(video_no AS VARCHAR), 7, '0') AS custom_id,
          category, channel_display_name, CASE WHEN video_no % 4 = 0 THEN 'Audio Match' ELSE 'Web Upload' END AS claim_origin,
          true AS claimed_by_this_owner, CASE WHEN video_no % 9 = 0 THEN 'UGC' ELSE 'Partner-provided' END AS content_type,
          video_no % 17 <> 0 AS display_ads_enabled, true AS embedding_allowed, video_no % 23 <> 0 AS in_video_ads_enabled,
          format = 'long' AND video_no % 5 <> 0 AS midrolls_enabled, time_published, time_published - INTERVAL 2 HOUR AS time_uploaded,
          CASE WHEN format = 'short' THEN 15 + (video_no % 45) ELSE 240 + (video_no * 37) % 2400 END AS video_length,
          CASE WHEN video_no % 61 = 0 THEN 'unlisted' ELSE 'public' END AS video_privacy_status, video_title,
          'https://www.youtube.com/watch?v=' || video_id AS video_url
        FROM videos""")
    q("""CREATE TABLE assets AS SELECT 'A' || lpad(CAST(100000 + i AS VARCHAR), 9, '0') AS asset_id, 'NWM-AS-' || lpad(CAST(i AS VARCHAR), 6, '0') AS custom_id,
          CASE WHEN i % 10 < 6 THEN 'web' WHEN i % 10 < 8 THEN 'music_video' WHEN i % 10 = 8 THEN 'sound_recording' ELSE 'episode' END AS asset_type,
          CASE WHEN i % 7 = 0 THEN 'catalog' WHEN i % 7 = 1 THEN 'premium' ELSE 'standard' END AS asset_labels,
          CASE WHEN i % 13 = 0 THEN 'Monetize in all countries' WHEN i % 13 = 1 THEN 'Track in all countries' ELSE 'Monetize in all countries' END AS match_policy,
          CASE WHEN i % 41 = 0 THEN 'partial' ELSE 'all' END AS ownership, i % 97 = 0 AS is_merged
        FROM range(0, 30000) t(i)""")
    q("""CREATE TABLE claims AS SELECT m.claim_id, m.channel_id, m.asset_id, m.video_id, m.custom_id, m.claim_origin,
          CASE WHEN hash(m.video_id) % 50 = 0 THEN 'inactive' WHEN hash(m.video_id) % 70 = 1 THEN 'pending' ELSE 'active' END AS claim_status,
          CASE WHEN m.content_type = 'UGC' THEN 'thirdParty' ELSE 'self' END AS uploader, m.channel_display_name, m.video_title,
          CASE WHEN m.claim_origin = 'Audio Match' THEN 'audio' ELSE 'audiovisual' END AS claim_type,
          a.match_policy LIKE 'Monetize%' AS claim_policy_monetize, true AS claim_policy_track, false AS claim_policy_block,
          CAST(m.time_uploaded AS DATE) + 1 AS claim_created_date, CAST(m.time_uploaded AS DATE) AS video_upload_date,
          m.video_length AS video_duration_sec
        FROM video_metadata m JOIN assets a USING (asset_id)""")

    # ---- facts: one row per day x video x country (x split) where the video is watched that day ----
    q(f"CREATE TABLE days AS SELECT CAST(d AS DATE) AS date FROM range(DATE '{START}', DATE '{END}' + 1, INTERVAL 1 DAY) t(d)")
    # activity: a video is watched on a day with a probability that falls with its age and rises with its popularity
    q("""CREATE TABLE active AS SELECT d.date, v.video_id, v.channel_id, v.channel_no, v.format, v.pop,
          date_diff('day', CAST(v.time_published AS DATE), d.date) AS age
        FROM days d JOIN videos v ON CAST(v.time_published AS DATE) <= d.date
        WHERE random() < least(0.8, 0.012 + 0.14 * v.pop / (1 + date_diff('day', CAST(v.time_published AS DATE), d.date) / 45.0))""")
    con.execute("SELECT count(*) FROM active")
    n_active = con.fetchone()[0]
    # country rows: each active video-day reaches 1-6 countries by popularity, picked by weight
    q("""CREATE TABLE cweights AS SELECT country_code, base_cpm, weight, sum(weight) OVER (ORDER BY weight DESC, country_code) AS cum,
          sum(weight) OVER () AS tot FROM countries""")
    q("""CREATE TABLE vc AS SELECT a.*, c.country_code, c.base_cpm, c.weight / c.tot AS share
        FROM active a, range(0, 1 + CAST(least(3, floor(a.pop * 0.8)) AS INT)) k(j)
        JOIN cweights c ON c.cum - c.weight < (hash(a.video_id, a.date, k.j) % 1000) / 1000.0 * c.tot AND (hash(a.video_id, a.date, k.j) % 1000) / 1000.0 * c.tot <= c.cum""")
    q("""CREATE TABLE vcd AS SELECT DISTINCT ON (date, video_id, country_code) * FROM vc""")
    # views: popularity x age decay x weekend x Shorts growth in 2026 x noise
    q("""CREATE TABLE base AS SELECT date, video_id, channel_id, channel_no, format, country_code, base_cpm,
          greatest(1, round(pop * 2400 * share * 6 / (1 + age / 45.0)
            * CASE WHEN dayofweek(date) IN (0, 6) THEN 1.18 ELSE 1 END
            * CASE WHEN format = 'short' AND date >= DATE '2026-01-01' THEN 1 + date_diff('day', DATE '2026-01-01', date) / 120.0 ELSE 1 END
            * (0.6 + random() * 0.8))) AS views
        FROM vcd""")
    q("""CREATE TABLE basic AS SELECT b.date, b.channel_id, b.video_id, 'claimed' AS claimed_status,
          CASE WHEN hash(b.video_id) % 9 = 0 THEN 'thirdParty' ELSE 'self' END AS uploader_type,
          CASE WHEN hash(b.video_id) % 31 = 0 THEN 'live' ELSE 'onDemand' END AS live_or_on_demand, s.subscribed_status, b.country_code,
          CAST(round(b.views * s.part * 0.93) AS BIGINT) AS engaged_views, CAST(round(b.views * s.part) AS BIGINT) AS views,
          CAST(round(b.views * s.part * 0.004 * (0.5 + random())) AS BIGINT) AS comments, CAST(round(b.views * s.part * 0.002 * (0.5 + random())) AS BIGINT) AS shares,
          round(b.views * s.part * CASE WHEN b.format = 'short' THEN 0.4 ELSE 4.2 END * (0.7 + random() * 0.6), 1) AS watch_time_minutes,
          CASE WHEN b.format = 'short' THEN 24 + random() * 10 ELSE 210 + random() * 120 END AS average_view_duration_seconds,
          CASE WHEN b.format = 'short' THEN 70 + random() * 25 ELSE 28 + random() * 20 END AS average_view_duration_percentage,
          CAST(round(b.views * s.part * 0.0025 * CASE WHEN s.subscribed_status = 'subscribed' THEN 0.2 ELSE 1 END * (0.5 + random())) AS BIGINT) AS subscribers_gained,
          CAST(round(b.views * s.part * 0.0004 * (0.5 + random())) AS BIGINT) AS subscribers_lost,
          CAST(round(b.views * s.part * 0.006 * (0.5 + random())) AS BIGINT) AS videos_added_to_playlists,
          CAST(round(b.views * s.part * 0.001 * (0.5 + random())) AS BIGINT) AS videos_removed_from_playlists,
          CAST(round(b.views * s.part * 0.042 * (0.6 + random() * 0.8)) AS BIGINT) AS likes,
          CAST(round(b.views * s.part * 0.0012 * (0.5 + random())) AS BIGINT) AS dislikes,
          CAST(round(b.views * s.part * 0.02) AS BIGINT) AS red_views,
          round(b.views * s.part * 0.02 * CASE WHEN b.format = 'short' THEN 0.4 ELSE 4.6 END, 1) AS red_watch_time_minutes
        FROM base b, (VALUES ('subscribed', 0.28), ('unsubscribed', 0.72)) s(subscribed_status, part)""")
    q("""CREATE TABLE traffic_source AS SELECT b.date, b.channel_id, b.video_id, 'claimed' AS claimed_status, 'self' AS uploader_type,
          'onDemand' AS live_or_on_demand, 'unsubscribed' AS subscribed_status, b.country_code, t.traffic_source_type,
          CASE t.traffic_source_type WHEN 5 THEN lower(split_part(v.video_title, ' ', 2)) WHEN 7 THEN substr(md5(b.video_id || t.traffic_source_type), 1, 11) ELSE '' END AS traffic_source_detail,
          CAST(round(x.v * 0.93) AS BIGINT) AS engaged_views, CAST(round(x.v) AS BIGINT) AS views,
          round(x.v * CASE WHEN b.format = 'short' THEN 0.4 ELSE 4.2 END, 1) AS watch_time_minutes,
          CASE WHEN b.format = 'short' THEN 26 ELSE 250 END + random() * 40 AS average_view_duration_seconds,
          CASE WHEN b.format = 'short' THEN 78 ELSE 35 END + random() * 10 AS average_view_duration_percentage,
          CAST(round(x.v * 0.02) AS BIGINT) AS red_views, round(x.v * 0.08, 1) AS red_watch_time_minutes
        FROM base b JOIN videos v USING (video_id) JOIN traffic t
          ON CASE WHEN b.format = 'short' THEN t.traffic_source_type IN (24, 3, 7) ELSE t.traffic_source_type <> 24 END
          AND hash(b.video_id, b.date, t.traffic_source_type) % 100 < t.weight * 2 + 2
        CROSS JOIN LATERAL (SELECT greatest(1, b.views * t.weight / 40.0 * CASE WHEN t.traffic_source_type = 24 THEN 6 ELSE 1 END) AS v) x
        WHERE b.views >= 5""")
    q("""CREATE TABLE device_os AS SELECT b.date, b.channel_id, b.video_id, 'claimed' AS claimed_status, 'self' AS uploader_type,
          'onDemand' AS live_or_on_demand, 'unsubscribed' AS subscribed_status, b.country_code, d.device_type, o.operating_system,
          CAST(round(x.v * 0.93) AS BIGINT) AS engaged_views, CAST(round(x.v) AS BIGINT) AS views,
          round(x.v * CASE WHEN d.device_type = 102 THEN 6.1 WHEN b.format = 'short' THEN 0.4 ELSE 3.6 END, 1) AS watch_time_minutes,
          CASE WHEN b.format = 'short' THEN 26 ELSE 240 END + random() * 60 AS average_view_duration_seconds,
          CASE WHEN b.format = 'short' THEN 75 ELSE 33 END + random() * 12 AS average_view_duration_percentage,
          CAST(round(x.v * 0.02) AS BIGINT) AS red_views, round(x.v * 0.08, 1) AS red_watch_time_minutes
        FROM base b JOIN devices d ON hash(b.video_id, b.date, d.device_type) % 100 < d.weight / 2 + 6
        JOIN os o ON o.device_type = d.device_type AND hash(b.video_id, o.operating_system) % 3 = 0
        CROSS JOIN LATERAL (SELECT greatest(1, b.views * d.weight / 60.0) AS v) x
        WHERE b.views >= 20""")
    # revenue: monetized playbacks x country CPM x Q4 lift; channel #7 demonetized from 2026-09-01; Shorts pay less
    q("""CREATE TABLE estimated_revenue AS SELECT b.date, b.channel_id, b.video_id, 'claimed' AS claimed_status,
          CASE WHEN hash(b.video_id) % 9 = 0 THEN 'thirdParty' ELSE 'self' END AS uploader_type, b.country_code,
          round(r.ad * 0.55 + r.red, 4) AS estimated_partner_revenue, round(r.ad * 0.55, 4) AS estimated_partner_ad_revenue,
          round(r.ad * 0.55 * 0.82, 4) AS estimated_partner_ad_auction_revenue, round(r.ad * 0.55 * 0.18, 4) AS estimated_partner_ad_reserved_revenue,
          round(r.ad, 4) AS estimated_youtube_ad_revenue, CAST(round(r.mp) AS BIGINT) AS estimated_monetized_playbacks,
          round(r.cpm * 1.08, 3) AS estimated_playback_based_cpm, CAST(round(r.mp * 1.4) AS BIGINT) AS ad_impressions, round(r.cpm, 3) AS estimated_cpm,
          round(r.red, 4) AS estimated_partner_red_revenue, 0.0 AS estimated_partner_transaction_revenue
        FROM base b CROSS JOIN LATERAL (SELECT
            b.views * CASE WHEN b.channel_no = 7 AND b.date >= DATE '2026-09-01' THEN 0.0 WHEN b.format = 'short' THEN 0.12 ELSE 0.46 END AS mp,
            b.base_cpm * CASE WHEN month(b.date) IN (11, 12) THEN 1.45 WHEN month(b.date) = 1 THEN 0.72 ELSE 1 END * (0.85 + random() * 0.3) AS cpm,
            b.views * 0.02 * 0.004 AS red) z
        CROSS JOIN LATERAL (SELECT z.mp AS mp, z.cpm AS cpm, z.mp / 1000.0 * z.cpm AS ad, z.red AS red) r
        WHERE b.views >= 3""")
    q("""CREATE TABLE ad_rates AS SELECT e.date, e.channel_id, e.video_id, e.claimed_status, e.uploader_type, e.country_code, t.ad_type,
          round(e.estimated_youtube_ad_revenue * t.weight / 100.0, 4) AS estimated_youtube_ad_revenue,
          CAST(round(e.ad_impressions * t.weight / 100.0) AS BIGINT) AS ad_impressions,
          round(e.estimated_cpm * CASE t.ad_type_code WHEN 19 THEN 0.6 WHEN 2 THEN 0.35 WHEN 6 THEN 1.6 ELSE 1 END, 3) AS estimated_cpm
        FROM estimated_revenue e JOIN ad_types t ON hash(e.video_id, e.date, t.ad_type_code) % 100 < t.weight / 2 + 6
        WHERE e.estimated_monetized_playbacks >= 40""")

    # ---- write: dimensions whole, facts one file per day; the last days of two reports wait for replay ----
    def write(table: str, folder: str, daily: bool, stream: bool = False):
        con.execute(f"SELECT count(*) FROM {table}")
        n = con.fetchone()[0]
        if not daily:
            p = out / "batch" / folder
            p.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY {table} TO '{p / (Path(folder).name + '.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
            return n
        cut = f"DATE '{END}' - INTERVAL {a.stream_days - 1} DAY" if stream else f"DATE '{END}' + 1"
        for part, cond in (("batch", f"date < {cut}"), ("pending", f"date >= {cut}")):
            if part == "pending" and not stream:
                continue
            p = out / part / folder
            p.mkdir(parents=True, exist_ok=True)
            con.execute(f"""COPY (SELECT *, strftime(date, '%Y-%m-%d') AS day_file FROM {table} WHERE {cond})
                TO '{p}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (day_file), FILENAME_PATTERN 'part', OVERWRITE_OR_IGNORE)""")
        # DuckDB writes day_file=<day>/part.parquet; flatten to <day>.parquet (one file per day, the report's own columns)
        for part in ("batch", "pending"):
            for f in (out / part / folder).glob("day_file=*/part*.parquet"):
                f.rename(f.parent.parent / (f.parent.name.removeprefix("day_file=") + ".parquet"))
                f.parent.rmdir()
        return n

    counts = {}
    for t, folder in (("countries", "reference/countries"), ("traffic", "reference/traffic_source_types"), ("devices", "reference/device_types"),
                      ("os", "reference/operating_systems"), ("ad_types", "reference/ad_types")):
        counts[folder] = write(t, folder, False)
    for t, folder in (("video_metadata", "content_owner_video_metadata_a4"), ("assets", "content_owner_asset_a3"), ("claims", "content_owner_active_claims_a3")):
        counts[folder] = write(t, folder, False)
    for t, folder, stream in (("basic", "content_owner_basic_a4", True), ("traffic_source", "content_owner_traffic_source_a3", False),
                              ("device_os", "content_owner_device_os_a3", False), ("estimated_revenue", "content_owner_estimated_revenue_a1", True),
                              ("ad_rates", "content_owner_ad_rates_a1", False)):
        counts[folder] = write(t, folder, True, stream)
        print(f"{folder}: {counts[folder]:,} rows ({time.time() - t0:.0f}s)", flush=True)
    print({"active_video_days": n_active, **counts, "seconds": round(time.time() - t0)})


if __name__ == "__main__":
    main()
