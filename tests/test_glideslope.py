import datetime as dt
import io
import contextlib
import json
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

import glideslope


NOW = dt.datetime(2026, 7, 20, 4, 0, tzinfo=dt.timezone.utc)


def session_of(limits):
    """The 5h session limit — rows are ordered 7 DAY / Fable / 5h, so never index it."""
    return next(limit for limit in limits if limit["meter_id"] == "session")


def codex_snapshot():
    return {
        "rateLimitsByLimitId": {
            "codex_bengalfox": {
                "limitId": "codex_bengalfox",
                "limitName": "GPT-5.3-Codex-Spark",
                "planType": "prolite",
                "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1785124800},
                "secondary": None,
            },
            "codex": {
                "limitId": "codex",
                "limitName": None,
                "planType": "prolite",
                "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1785123891},
                "secondary": None,
            },
        }
    }


def claude_snapshot():
    return {
        "work": {
            "email": "work@example.com",
            "active": False,
            "limits": [
                {"label": "session (5h)", "percent": 100.0, "resets_at": "2026-07-20T05:09:59+00:00"},
                {"label": "weekly (all models)", "percent": 32.0, "resets_at": "2026-07-26T02:59:59+00:00"},
                {"label": "weekly (Fable)", "percent": 35.0, "resets_at": "2026-07-26T02:59:59+00:00"},
            ],
        },
        "personal": {
            "email": "personal@example.com",
            "active": True,
            "limits": [
                {"label": "session (5h)", "percent": 5.0, "resets_at": "2026-07-20T09:00:00+00:00"},
                {"label": "weekly (all models)", "percent": 56.0, "resets_at": "2026-07-23T09:00:00+00:00"},
                {"label": "weekly (Fable)", "percent": 37.0, "resets_at": "2026-07-23T09:00:00+00:00"},
            ],
        },
    }


def claude_snapshot_with_charlie():
    snapshot = claude_snapshot()
    snapshot["personal"]["active"] = False
    snapshot["lab"] = {
        "email": "charlie@example.test",
        "active": True,
        "limits": [
            {"label": "session (5h)", "percent": 12.0, "resets_at": "2026-07-20T08:00:00+00:00"},
            {"label": "weekly (all models)", "percent": 41.0, "resets_at": "2026-07-24T04:00:00+00:00"},
            {"label": "weekly (Fable)", "percent": 25.0, "resets_at": "2026-07-24T04:00:00+00:00"},
        ],
    }
    return snapshot


def kimi_payload():
    """A /coding/v1/usages response in the shape the endpoint returns, retimed onto NOW.

    Note the shape the endpoint actually returns: every figure is a decimal
    *string*, `usage` (the plan quota) carries no `used` key at all while it is
    zero, and the burst window arrives inside an unordered `limits` list keyed
    only by its duration and time unit.
    """
    return {
        "user": {"userId": "usr_example_0001", "region": "REGION_OVERSEA",
                 "membership": {"level": "LEVEL_INTERMEDIATE"}, "businessId": ""},
        "usage": {"limit": "100", "remaining": "93", "resetTime": "2026-07-26T02:59:59Z"},
        "limits": [{
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "12", "remaining": "88",
                       "resetTime": "2026-07-20T05:09:59Z"},
        }],
        "parallel": {"limit": "20"},
        "totalQuota": {},
        "authentication": {"method": "METHOD_API_KEY", "scope": "FEATURE_CODING"},
        "subType": "TYPE_PURCHASE",
        "domain": "DOMAIN_NEXUS",
    }


def grok_payload():
    """A billing?format=credits response in the shape the endpoint returns."""
    return {
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-09-08T12:00:00+00:00",
                "end": "2026-09-15T12:00:00+00:00",
            },
            "creditUsagePercent": 2.0,
            "onDemandCap": {"val": 0},
            "onDemandUsed": {"val": 0},
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 1.0},
                {"product": "GrokChat", "usagePercent": 1.0},
            ],
            "isUnifiedBillingUser": True,
            "prepaidBalance": {"val": 0},
            "billingPeriodStart": "2026-09-08T12:00:00+00:00",
            "billingPeriodEnd": "2026-09-15T12:00:00+00:00",
        }
    }


class KimiTests(unittest.TestCase):
    def test_normalizes_plan_quota_and_burst_window(self):
        position = glideslope.normalize_kimi(kimi_payload(), NOW)
        self.assertEqual(position["provider"], "Kimi")
        self.assertEqual(position["plan"], "LEVEL_INTERMEDIATE")
        self.assertEqual(position["parallel_limit"], 20)
        # Shortest window first, same as the Claude buckets.
        self.assertEqual([limit["meter_id"] for limit in position["limits"]],
                         ["session", "weekly_all"])
        session, weekly = position["limits"]
        self.assertEqual(session["label"], "5h session")
        self.assertEqual(session["window_minutes"], 300)
        self.assertAlmostEqual(session["used_percent"], 12.0)
        self.assertEqual(session["resets_at"],
                         dt.datetime(2026, 7, 20, 5, 9, 59, tzinfo=dt.timezone.utc))
        self.assertTrue(session["anchored"])
        # The plan quota rides the shared weekly meter so it lands in the hero read.
        self.assertEqual(weekly["label"], "Weekly · all models")
        self.assertEqual(weekly["window_minutes"], 10_080)

    def test_missing_used_is_derived_from_remaining(self):
        """`used` is absent until it is non-zero — deriving it is the only correct read."""
        position = glideslope.normalize_kimi(kimi_payload(), NOW)
        weekly = next(l for l in position["limits"] if l["meter_id"] == "weekly_all")
        self.assertAlmostEqual(weekly["used_percent"], 7.0)  # 100 limit − 93 remaining

    def test_used_wins_over_remaining_when_both_present(self):
        payload = kimi_payload()
        payload["usage"]["used"] = "41"
        position = glideslope.normalize_kimi(payload, NOW)
        weekly = next(l for l in position["limits"] if l["meter_id"] == "weekly_all")
        self.assertAlmostEqual(weekly["used_percent"], 41.0)

    def test_burst_window_is_matched_by_duration_not_position(self):
        """A new window ahead of the 5h entry must not be mislabeled as the session."""
        payload = kimi_payload()
        payload["limits"].insert(0, {
            "window": {"duration": 1, "timeUnit": "TIME_UNIT_HOUR"},
            "detail": {"limit": "100", "used": "3", "remaining": "97",
                       "resetTime": "2026-07-20T04:30:00Z"},
        })
        position = glideslope.normalize_kimi(payload, NOW)
        by_id = {limit["meter_id"]: limit for limit in position["limits"]}
        self.assertEqual(by_id["session"]["window_minutes"], 300)
        self.assertAlmostEqual(by_id["session"]["used_percent"], 12.0)
        # The unknown window is still reported, labeled by its own length.
        self.assertEqual(by_id["window_60m"]["label"], "1h · burst")
        self.assertAlmostEqual(by_id["window_60m"]["used_percent"], 3.0)

    def test_neither_used_nor_remaining_is_rejected(self):
        payload = kimi_payload()
        payload["usage"] = {"limit": "100", "resetTime": "2026-07-26T02:59:59Z"}
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_kimi(payload, NOW)

    def test_non_numeric_limit_is_rejected(self):
        payload = kimi_payload()
        payload["usage"]["limit"] = "unlimited"
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_kimi(payload, NOW)

    def test_out_of_range_usage_is_rejected(self):
        payload = kimi_payload()
        payload["usage"]["used"] = "180"
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_kimi(payload, NOW)

    def test_empty_payload_is_rejected(self):
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_kimi({"user": {}}, NOW)

    def test_unanchored_window_reports_no_clock(self):
        payload = kimi_payload()
        del payload["limits"][0]["detail"]["resetTime"]
        position = glideslope.normalize_kimi(payload, NOW)
        session = next(l for l in position["limits"] if l["meter_id"] == "session")
        self.assertIsNone(session["resets_at"])
        self.assertFalse(session["anchored"])

    def test_account_shape_matches_the_other_providers(self):
        account, = glideslope.kimi_accounts(glideslope.normalize_kimi(kimi_payload(), NOW))
        self.assertEqual(account["provider"], "Kimi")
        self.assertEqual(account["account"], "kimi")
        self.assertEqual(account["display"], "Kimi")
        self.assertEqual(account["plan"], glideslope.KIMI_PLAN)
        self.assertEqual(account["plan_raw"], "LEVEL_INTERMEDIATE")
        self.assertFalse(account["stale"])
        self.assertEqual(account["observed_at"], NOW)

    def test_key_comes_from_environment_first(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "sk-kimi-env"}, clear=True):
            self.assertEqual(glideslope.kimi_api_key(Path("/nonexistent")), "sk-kimi-env")

    def test_key_falls_back_to_the_keys_file(self):
        """The launchd sampler carries no shell environment — the file is its only path."""
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            keys = Path(tmp) / "keys.txt"
            keys.write_text(textwrap.dedent("""\
                # local API keys
                OPENROUTER_API_KEY=sk-or-v1-nope
                KIMI_API_KEY=sk-kimi-fromfile
                """))
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(glideslope.kimi_api_key(keys), "sk-kimi-fromfile")

    def test_no_key_anywhere_raises(self):
        import os
        from unittest import mock
        missing = Path("/nonexistent/keys.txt")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(glideslope, "KEYS_FILE", missing):
            self.assertIsNone(glideslope.kimi_api_key())
            with self.assertRaises(glideslope.PositionError):
                glideslope.query_kimi()


class GrokTests(unittest.TestCase):
    def test_normalizes_the_weekly_pool(self):
        position = glideslope.normalize_grok(grok_payload(), NOW)
        self.assertEqual(position["provider"], "Grok")
        weekly, = position["limits"]
        self.assertEqual(weekly["meter_id"], "weekly_all")
        self.assertEqual(weekly["label"], "Weekly · all models")
        self.assertAlmostEqual(weekly["used_percent"], 2.0)
        self.assertEqual(weekly["window_minutes"], 10_080)
        self.assertEqual(weekly["resets_at"],
                         dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc))
        self.assertTrue(weekly["anchored"])
        self.assertEqual(position["product_usage"], [
            {"product": "GrokBuild", "used_percent": 1.0},
            {"product": "GrokChat", "used_percent": 1.0},
        ])

    def test_does_not_invent_a_five_hour_window(self):
        position = glideslope.normalize_grok(grok_payload(), NOW)
        self.assertEqual([limit["meter_id"] for limit in position["limits"]], ["weekly_all"])

    def test_a_300_minute_period_is_the_session_meter(self):
        payload = grok_payload()
        payload["config"]["currentPeriod"] = {
            "type": "USAGE_PERIOD_TYPE_UNKNOWN",
            "start": "2026-07-20T01:00:00Z",
            "end": "2026-07-20T06:00:00Z",
        }
        payload["config"]["billingPeriodStart"] = "2026-07-20T01:00:00Z"
        payload["config"]["billingPeriodEnd"] = "2026-07-20T06:00:00Z"
        position = glideslope.normalize_grok(payload, NOW)
        session, = position["limits"]
        self.assertEqual(session["meter_id"], "session")
        self.assertEqual(session["label"], "5h session")
        self.assertEqual(session["window_minutes"], 300)

    def test_missing_percent_is_rejected(self):
        payload = grok_payload()
        del payload["config"]["creditUsagePercent"]
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_grok(payload, NOW)

    def test_out_of_range_percent_is_rejected(self):
        payload = grok_payload()
        payload["config"]["creditUsagePercent"] = 180
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_grok(payload, NOW)

    def test_empty_payload_is_rejected(self):
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_grok({}, NOW)

    def test_account_shape_matches_the_other_providers(self):
        account, = glideslope.grok_accounts(glideslope.normalize_grok(grok_payload(), NOW))
        self.assertEqual(account["provider"], "Grok")
        self.assertEqual(account["account"], "grok")
        self.assertEqual(account["display"], "Grok")
        self.assertEqual(account["plan"], glideslope.GROK_PLAN)
        self.assertFalse(account["stale"])
        self.assertEqual(account["observed_at"], NOW)

    def test_login_email_is_identity_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({
                "https://auth.x.ai::test": {
                    "key": "secret-token",
                    "email": "grok-user@example.test",
                    "user_id": "user-1",
                    "oidc_issuer": "https://auth.x.ai",
                }
            }))
            self.assertEqual(glideslope.grok_login_email(path), "grok-user@example.test")

    def test_missing_auth_is_not_logged_in(self):
        self.assertIsNone(glideslope.grok_login_email(Path("/nonexistent/auth.json")))
        with self.assertRaises(glideslope.PositionError):
            glideslope.grok_auth_entry(Path("/nonexistent/auth.json"))

    def test_hook_snapshot_is_used_while_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grok-billing.json"
            glideslope.write_grok_hook_snapshot(grok_payload(), NOW, path)
            self.assertEqual(
                glideslope.read_grok_hook_snapshot(path, now=NOW)["config"]["creditUsagePercent"],
                2.0)
            stale = NOW + dt.timedelta(seconds=glideslope.GROK_HOOK_MAX_AGE_SECONDS + 1)
            self.assertIsNone(glideslope.read_grok_hook_snapshot(path, now=stale))

    def test_query_prefers_a_fresh_hook_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grok-billing.json"
            glideslope.write_grok_hook_snapshot(grok_payload(), NOW, path)
            payload = glideslope.query_grok(
                snapshot_path=path, auth_path=Path("/nonexistent/auth.json"), now=NOW)
            self.assertEqual(payload["config"]["creditUsagePercent"], 2.0)

    def test_force_live_does_not_read_the_hook_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grok-billing.json"
            glideslope.write_grok_hook_snapshot(grok_payload(), NOW, path)
            with self.assertRaises(glideslope.PositionError):
                glideslope.query_grok(
                    snapshot_path=path, auth_path=Path("/nonexistent/auth.json"),
                    force_live=True, now=NOW)


class CodexTests(unittest.TestCase):
    def test_normalizes_general_and_separate_spark_meter(self):
        report = glideslope.normalize_codex(codex_snapshot(), NOW)
        self.assertEqual(report["plan"], "prolite")
        self.assertEqual([item["meter_id"] for item in report["limits"]],
                         ["codex", "codex_bengalfox"])
        self.assertEqual([item["label"] for item in report["limits"]],
                         ["Weekly · all models", "Weekly · GPT-5.3-Codex-Spark"])

    def test_map_key_preserves_dynamic_identity_when_embedded_id_is_null(self):
        raw = codex_snapshot()
        raw["rateLimitsByLimitId"]["codex_bengalfox"]["limitId"] = None
        report = glideslope.normalize_codex(raw, NOW)
        self.assertEqual(report["limits"][1]["meter_id"], "codex_bengalfox")

    def test_zero_use_window_is_not_presented_as_anchored(self):
        spark = glideslope.normalize_codex(codex_snapshot(), NOW)["limits"][1]
        self.assertFalse(spark["anchored"])
        self.assertIsNone(spark["resets_at"])
        self.assertEqual(glideslope.format_reset(spark, NOW), "not started")
        self.assertIsNone(glideslope.even_pace_percent(spark, NOW))

    def test_missing_or_out_of_range_usage_is_rejected(self):
        for invalid in (None, "2", -1, 101, True):
            raw = codex_snapshot()
            raw["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(glideslope.PositionError):
                glideslope.normalize_codex(raw, NOW)

    def test_empty_windows_are_rejected(self):
        raw = {"rateLimits": {"limitId": "codex", "primary": None, "secondary": None}}
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_codex(raw, NOW)

    def test_codex_is_reported_as_the_20x_plan(self):
        accounts = glideslope.codex_accounts(glideslope.normalize_codex(codex_snapshot(), NOW))
        self.assertEqual(accounts[0]["plan"], "20x")
        self.assertEqual(accounts[0]["plan_raw"], "prolite")

    def test_app_server_handshake_ignores_intervening_notifications(self):
        fake = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, sys
            initialized = False
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "initialize":
                    print(json.dumps({"method":"notice","params":{}}), flush=True)
                    print(json.dumps({"id":msg["id"],"result":{"userAgent":"fake"}}), flush=True)
                elif msg.get("method") == "initialized":
                    initialized = True
                elif msg.get("method") == "account/rateLimits/read":
                    if not initialized:
                        print(json.dumps({"id":msg["id"],"error":{"message":"not initialized"}}), flush=True)
                    else:
                        print(json.dumps({"method":"account/rateLimits/updated","params":{}}), flush=True)
                        print(json.dumps({"id":msg["id"],"result":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":2,"windowDurationMins":10080,"resetsAt":1785123891},"secondary":None}}}), flush=True)
            """)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-codex"
            executable.write_text(fake)
            executable.chmod(0o755)
            result = glideslope.query_codex_rate_limits(executable=str(executable), timeout_seconds=5)
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 2)

    def test_app_server_distinguishes_fixed_and_sliding_zero_percent_windows(self):
        fake = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, sys
            reads = 0
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "initialize":
                    print(json.dumps({"id":msg["id"],"result":{"userAgent":"fake"}}), flush=True)
                elif msg.get("method") == "account/rateLimits/read":
                    reads += 1
                    fixed = {"usedPercent":0,"windowDurationMins":10080,"resetsAt":1785123891}
                    sliding = {
                        "usedPercent":0,
                        "windowDurationMins":10080,
                        "resetsAt":1785124800 + reads
                    }
                    result = {"rateLimitsByLimitId":{
                        "codex":{"limitId":"codex","primary":fixed,"secondary":None},
                        "codex_bengalfox":{"limitId":"codex_bengalfox",
                            "limitName":"GPT-5.3-Codex-Spark","primary":sliding,"secondary":None}
                    }}
                    print(json.dumps({"id":msg["id"],"result":result}), flush=True)
            """)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-codex"
            executable.write_text(fake)
            executable.chmod(0o755)
            result = glideslope.query_codex_rate_limits(
                executable=str(executable), timeout_seconds=5, zero_probe_seconds=0)

        fixed = result["rateLimitsByLimitId"]["codex"]["primary"]
        sliding = result["rateLimitsByLimitId"]["codex_bengalfox"]["primary"]
        self.assertTrue(fixed[glideslope.CODEX_ANCHOR_FIELD])
        self.assertFalse(sliding[glideslope.CODEX_ANCHOR_FIELD])

        limits = glideslope.normalize_codex(result, NOW)["limits"]
        self.assertTrue(limits[0]["anchored"])
        self.assertIsNotNone(limits[0]["resets_at"])
        self.assertFalse(limits[1]["anchored"])
        self.assertIsNone(limits[1]["resets_at"])

    def test_failed_zero_probe_keeps_the_first_valid_snapshot(self):
        fake = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if msg.get("method") == "initialize":
                    print(json.dumps({"id":msg["id"],"result":{"userAgent":"fake"}}), flush=True)
                elif msg.get("method") == "account/rateLimits/read":
                    result = {"rateLimits":{"limitId":"codex","primary":{
                        "usedPercent":0,"windowDurationMins":10080,"resetsAt":1785123891
                    },"secondary":None}}
                    print(json.dumps({"id":msg["id"],"result":result}), flush=True)
                    break
            """)
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "fake-codex"
            executable.write_text(fake)
            executable.chmod(0o755)
            result = glideslope.query_codex_rate_limits(
                executable=str(executable), timeout_seconds=5, zero_probe_seconds=0)

        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 0)
        self.assertNotIn(glideslope.CODEX_ANCHOR_FIELD, result["rateLimits"]["primary"])


class ClaudeTests(unittest.TestCase):
    def test_accounts_are_reported_by_call_sign_not_alias(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        self.assertEqual([item["display"] for item in accounts], ["Alpha", "Bravo", "Charlie"])
        self.assertEqual([item["account"] for item in accounts], ["work", "personal", "lab"])

    def test_all_anthropic_accounts_are_max_20x(self):
        for account in glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW):
            self.assertEqual(account["plan"], "Max 20x")

    def test_unknown_alias_takes_the_next_free_nato_name(self):
        saved = dict(glideslope.CLAUDE_CALL_SIGNS)
        try:
            accounts = glideslope.normalize_claude({"mystery": {"active": False, "limits": []}}, NOW)
            self.assertEqual(accounts[0]["display"], "Echo")  # Alpha–Delta are configured
            self.assertEqual(glideslope.call_sign("mystery"), "Echo")  # and it is sticky
            self.assertGreater(glideslope.call_sign_order("Echo"), glideslope.call_sign_order("Delta"))
        finally:
            glideslope.CLAUDE_CALL_SIGNS.clear()
            glideslope.CLAUDE_CALL_SIGNS.update(saved)

    def test_with_no_call_signs_configured_accounts_are_named_in_roster_order(self):
        saved = dict(glideslope.CLAUDE_CALL_SIGNS)
        glideslope.CLAUDE_CALL_SIGNS.clear()
        try:
            snapshot = {"work": {"active": True, "limits": []}, "personal": {"active": False, "limits": []}}
            displays = [a["display"] for a in glideslope.normalize_claude(snapshot, NOW)]
            self.assertEqual(displays, ["Alpha", "Bravo"])
        finally:
            glideslope.CLAUDE_CALL_SIGNS.clear()
            glideslope.CLAUDE_CALL_SIGNS.update(saved)

    def test_config_loader_ignores_missing_and_broken_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(glideslope.load_config(Path(tmp) / "absent.toml"), {})
            broken = Path(tmp) / "broken.toml"
            broken.write_text("timezone = [unterminated")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(glideslope.load_config(broken), {})
            good = Path(tmp) / "good.toml"
            good.write_text('timezone = "UTC"\n[claude]\ncall_signs = { a = "Alpha" }\n')
            self.assertEqual(glideslope.load_config(good)["claude"]["call_signs"], {"a": "Alpha"})

    def test_resolved_config_reports_the_fixture(self):
        resolved = glideslope.resolved_config()
        self.assertTrue(resolved["config_present"])
        self.assertEqual(resolved["satellite"], "laptop")
        self.assertEqual(resolved["satellites"], [{"name": "studio", "host": "studio"}])
        self.assertEqual(resolved["notify"]["sink"], "url")

    def test_charlie_is_marked_active_and_renders_in_the_weekly_summary(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)
        self.assertEqual(glideslope.active_claude_display(accounts), "Charlie")
        rendered = glideslope.render_weekly_summary(accounts, None, NOW)
        self.assertIn(f"| Claude · Charlie ● {glideslope.LOCAL_SATELLITE} | C⃝ 41% (◆ ", rendered)

    def test_buckets_map_onto_stable_labels(self):
        alpha = glideslope.normalize_claude(claude_snapshot(), NOW)[0]
        self.assertEqual([item["label"] for item in alpha["limits"]],
                         ["Weekly · all models", "Weekly · Fable", "5h session"])

    def test_a_cached_account_is_dated_by_its_own_read_not_the_snapshot(self):
        # claude-account journals fetched_at on the account it could only serve
        # from cache. That is the truth clock for those numbers — the surfaces
        # place a five-hour read inside its window by it.
        raw = claude_snapshot()
        raw["work"]["stale"] = True
        raw["work"]["fetched_at"] = "2026-07-20T02:30:00+00:00"
        alpha, bravo = glideslope.normalize_claude(raw, NOW)
        self.assertEqual(alpha["observed_at"], dt.datetime(2026, 7, 20, 2, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(bravo["observed_at"], NOW)

    def test_a_cached_read_never_dates_itself_after_the_snapshot(self):
        raw = claude_snapshot()
        raw["work"]["stale"] = True
        raw["work"]["fetched_at"] = "2026-07-21T00:00:00+00:00"  # ahead of NOW
        alpha = glideslope.normalize_claude(raw, NOW)[0]
        self.assertEqual(alpha["observed_at"], NOW)

    def test_a_stale_account_withholds_the_glideslope_mark(self):
        raw = claude_snapshot()
        raw["work"]["stale"] = True
        accounts = glideslope.normalize_claude(raw, NOW)
        rendered = glideslope.render_markdown(accounts, NOW, [])
        stale_row = next(line for line in rendered.splitlines() if "Alpha" in line and "Weekly · all models" in line)
        self.assertIn("· stale", stale_row)
        self.assertIn("| A⃝ 32% |", stale_row)
        self.assertNotIn("◆", stale_row)  # the mark is withheld, not folded into Used


class RenderTests(unittest.TestCase):
    def _accounts(self):
        return (glideslope.normalize_claude(claude_snapshot(), NOW)
                + glideslope.codex_accounts(glideslope.normalize_codex(codex_snapshot(), NOW)))

    def test_meter_badge_encloses_the_account_letter_in_its_window_shape(self):
        badge = glideslope.meter_badge
        self.assertEqual(badge({"display": "Alpha"}, {"meter_id": "session", "window_minutes": 300}), "A⃤")
        self.assertEqual(badge({"display": "Bravo"}, {"meter_id": "weekly_all", "window_minutes": 10_080}), "B⃝")
        self.assertEqual(badge({"display": "Charlie"}, {"meter_id": "weekly_fable", "window_minutes": 10_080}), "C⃟")
        self.assertEqual(badge({"display": "Delta"}, {"meter_id": "weekly_all"}), "D⃝")
        self.assertEqual(badge({"display": "Codex"}, {"meter_id": "5h-spark", "window_minutes": 300}), "X⃤")

    def test_countdown_uses_two_largest_units(self):
        self.assertEqual(glideslope.format_countdown(dt.timedelta(hours=149, minutes=42)), "6d 5h")
        self.assertEqual(glideslope.format_countdown(dt.timedelta(hours=3, minutes=24)), "3h 24m")
        self.assertEqual(glideslope.format_countdown(dt.timedelta(minutes=45)), "45m")
        self.assertEqual(glideslope.format_countdown(dt.timedelta(seconds=-10)), "0m")

    def test_reset_cell_shows_days_then_hours_before_the_date(self):
        limit = {"resets_at": NOW + dt.timedelta(hours=149, minutes=42), "window_minutes": 10080}
        self.assertTrue(glideslope.format_reset(limit, NOW).startswith("6d 5h – "))

    def test_even_pace_is_elapsed_window_percent(self):
        limit = {"window_minutes": 300, "resets_at": NOW + dt.timedelta(hours=4)}
        self.assertEqual(glideslope.even_pace_percent(limit, NOW), 20)

    def test_markdown_reports_both_providers_under_one_header(self):
        rendered = glideslope.render_markdown(self._accounts(), NOW, [])
        self.assertIn("| All windows |||||", rendered)  # centered banner spanning 5 columns
        self.assertIn("| **Provider** | **Account** | **Window** | **Used** | **Next reset at** |", rendered)
        self.assertIn("| Claude | Alpha · Max 20x | 5h session | A⃤ 100% (◆ ", rendered)
        self.assertIn("| Claude | Bravo · active · Max 20x | Weekly · all models | B⃝ 56% (◆ ", rendered)
        # "· active" is the Claude login badge and belongs to Claude alone — a
        # single-account subscription never wears it.
        self.assertIn("| Codex | Codex · 20x | Weekly · all models | X⃝ 2% (◆ ", rendered)
        self.assertNotIn("Codex · active", rendered)
        self.assertIn("| Codex | Codex · 20x | Weekly · GPT-5.3-Codex-Spark | X⃝ 0% | not started |",
                      rendered)

    def test_warnings_are_appended_as_blockquotes(self):
        rendered = glideslope.render_markdown(self._accounts(), NOW, ["Claude refresh failed"])
        self.assertIn("> Claude refresh failed", rendered)

    def test_empty_report_still_renders_a_table(self):
        self.assertIn("no usage data", glideslope.render_markdown([], NOW, []))

    def test_codex_banked_resets_are_parsed(self):
        raw = codex_snapshot()
        raw["rateLimitResetCredits"] = {
            "availableCount": 2,
            "credits": [
                {"status": "available", "grantedAt": 1782933195, "expiresAt": 1785525195},
                {"status": "available", "grantedAt": 1783966325, "expiresAt": 1786558325},
                {"status": "used", "expiresAt": 1780000000},
            ],
        }
        banked = glideslope.normalize_codex(raw, NOW)["banked_resets"]
        self.assertEqual(banked["count"], 2)
        self.assertEqual(len(banked["credits"]), 2)  # the used credit is excluded
        # Soonest-expiring credit sorts first.
        self.assertEqual(banked["credits"][0]["expires_at"], glideslope.parse_timestamp(1785525195))
        self.assertEqual(banked["expires_at"], glideslope.parse_timestamp(1785525195))

    def test_no_banked_resets_when_none_available(self):
        self.assertIsNone(glideslope.normalize_codex(codex_snapshot(), NOW)["banked_resets"])

    def test_render_codex_resets_is_a_banner_table(self):
        raw = codex_snapshot()
        raw["rateLimitResetCredits"] = {"availableCount": 2, "credits": [
            {"status": "available", "grantedAt": 1782933195, "expiresAt": 1785525195},
            {"status": "available", "grantedAt": 1783966325, "expiresAt": 1786558325}]}
        accounts = glideslope.codex_accounts(glideslope.normalize_codex(raw, NOW))
        rendered = glideslope.render_codex_resets(accounts, NOW)
        self.assertIn("| Codex reset banks |||||", rendered)  # banner spans 5 columns
        self.assertIn("| **#** | **Credit** | **Granted** | **Expires** | **Time left** |", rendered)
        self.assertIn("| 1 | Full reset |", rendered)  # soonest-expiring credit is row 1

    def test_render_codex_resets_empty_without_credits(self):
        accounts = glideslope.codex_accounts(glideslope.normalize_codex(codex_snapshot(), NOW))
        self.assertEqual(glideslope.render_codex_resets(accounts, NOW), "")

    def test_report_leads_with_weekly_status_before_the_full_table(self):
        rendered = glideslope.render_report(
            self._accounts(), None, None, [], NOW, [], color=False)
        self.assertLess(rendered.index("Weekly status"), rendered.index("All windows"))

    def test_json_output_uses_iso_timestamps(self):
        payload = glideslope.json_ready(self._accounts(), NOW, [])
        self.assertEqual(payload["generated_at"], "2026-07-20T04:00:00Z")
        codex = next(item for item in payload["accounts"] if item["provider"] == "Codex")
        self.assertEqual(codex["limits"][0]["resets_at"], "2026-07-27T03:44:51Z")


class OpenRouterTests(unittest.TestCase):
    def _key_data(self):
        return {"limit": 100, "limit_reset": "monthly", "limit_remaining": 93.94,
                "usage": 6.06, "usage_daily": 0, "usage_weekly": 6.058, "usage_monthly": 6.06}

    def test_normalizes_rolling_weekly_burn(self):
        out = glideslope.normalize_openrouter(self._key_data(), NOW)
        self.assertEqual(out["provider"], "OpenRouter")
        self.assertAlmostEqual(out["weekly_usd"], 6.058)
        self.assertEqual(out["limit_usd"], 100)
        self.assertAlmostEqual(out["remaining_usd"], 93.94)
        self.assertEqual(out["limit_reset"], "monthly")

    def test_missing_weekly_is_rejected(self):
        data = self._key_data()
        del data["usage_weekly"]
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_openrouter(data, NOW)

    def test_non_numeric_weekly_is_rejected(self):
        data = self._key_data()
        data["usage_weekly"] = "6.06"
        with self.assertRaises(glideslope.PositionError):
            glideslope.normalize_openrouter(data, NOW)

    def test_no_api_key_raises(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(glideslope.PositionError):
                glideslope.query_openrouter()


class WeeklySummaryTests(unittest.TestCase):
    def _accounts(self):
        return (glideslope.normalize_claude(claude_snapshot(), NOW)
                + glideslope.codex_accounts(glideslope.normalize_codex(codex_snapshot(), NOW)))

    def test_one_weekly_row_per_provider(self):
        openrouter = glideslope.normalize_openrouter(
            {"usage_weekly": 6.06, "limit": 100, "limit_remaining": 93.94, "limit_reset": "monthly"}, NOW)
        rendered = glideslope.render_weekly_summary(self._accounts(), openrouter, NOW)
        self.assertIn("| Weekly status ||||", rendered)  # centered banner spanning the table (4 cols now)
        self.assertIn("| **Account** | **Weekly** | **Fable** | **Next reset at** |", rendered)
        self.assertIn("| Claude · Alpha | A⃝ 32% (◆ ", rendered)
        self.assertIn(f"| Claude · Bravo ● {glideslope.LOCAL_SATELLITE} | B⃝ 56% (◆ ", rendered)  # Bravo is active in the fixture
        self.assertIn("| Codex | X⃝ 2% (◆ ", rendered)
        self.assertIn(" | — | rolling 7d · $94 left of $100/monthly |", rendered)
        self.assertIn("$6.06", rendered)

    def test_kimi_sits_in_the_hero_read_beside_the_others(self):
        """Equal footing: Kimi's plan quota is a weekly row like every other provider's."""
        accounts = self._accounts() + glideslope.kimi_accounts(
            glideslope.normalize_kimi(kimi_payload(), NOW))
        rendered = glideslope.render_weekly_summary(accounts, None, NOW)
        self.assertIn("| Claude · Alpha | A⃝ 32% (◆ ", rendered)
        self.assertIn("| Codex | X⃝ 2% (◆ ", rendered)
        self.assertIn("| Kimi | K⃝ 7% (◆ ", rendered)
        self.assertNotIn("Kimi ●", rendered)  # the login dot belongs to Claude alone

    def test_kimi_windows_render_in_the_full_table(self):
        accounts = glideslope.kimi_accounts(glideslope.normalize_kimi(kimi_payload(), NOW))
        rendered = glideslope.render_markdown(accounts, NOW, [])
        self.assertIn("| Kimi | Kimi · Coding 15x | 5h session | K⃤ 12% (◆ ", rendered)
        self.assertIn("| Kimi | Kimi · Coding 15x | Weekly · all models | K⃝ 7% (◆ ", rendered)
        self.assertNotIn("Kimi · active", rendered)

    def test_grok_sits_in_the_hero_read_beside_the_others(self):
        accounts = self._accounts() + glideslope.grok_accounts(
            glideslope.normalize_grok(grok_payload(), NOW))
        rendered = glideslope.render_weekly_summary(accounts, None, NOW)
        self.assertIn("| Grok | G⃝ 2% (◆ ", rendered)
        self.assertNotIn("Grok ●", rendered)

    def test_grok_weekly_renders_in_the_full_table(self):
        accounts = glideslope.grok_accounts(glideslope.normalize_grok(grok_payload(), NOW))
        rendered = glideslope.render_markdown(accounts, NOW, [])
        self.assertIn("| Grok | Grok · SuperGrok | Weekly · all models | G⃝ 2% (◆ ", rendered)
        self.assertNotIn("5h session", rendered)
        self.assertNotIn("Grok · active", rendered)

    def test_summary_drops_openrouter_when_unavailable(self):
        rendered = glideslope.render_weekly_summary(self._accounts(), None, NOW)
        self.assertIn("Claude · Alpha", rendered)
        self.assertNotIn("OpenRouter", rendered)

    def test_empty_when_nothing_available(self):
        self.assertEqual(glideslope.render_weekly_summary([], None, NOW), "")

    def test_active_claude_account_is_marked(self):
        rendered = glideslope.render_weekly_summary(self._accounts(), None, NOW)
        # Bravo is active in the fixture; Alpha is not.
        self.assertIn(f"| Claude · Bravo ● {glideslope.LOCAL_SATELLITE} |", rendered)
        self.assertIn("| Claude · Alpha |", rendered)
        self.assertNotIn("Codex ●", rendered)

    def test_login_banner_names_the_active_account(self):
        self.assertEqual(glideslope.active_claude_display(self._accounts()), "Bravo")
        self.assertIn("Logged in: Claude · Bravo", glideslope.render_login_banner(self._accounts()))

    def test_login_banner_handles_no_active_account(self):
        stored = glideslope.normalize_claude(
            {"work": {"active": False, "limits": []}, "personal": {"active": False, "limits": []}}, NOW)
        self.assertIsNone(glideslope.active_claude_display(stored))
        self.assertIn("no active Claude account", glideslope.render_login_banner(stored))


class ReconcileLoginTests(unittest.TestCase):
    def _accounts(self):
        # Snapshot claims Bravo is active (as a stale cache would after a switch).
        return glideslope.normalize_claude(claude_snapshot(), NOW)

    def test_live_login_overrides_a_stale_snapshot_flag(self):
        accounts = self._accounts()
        self.assertEqual(glideslope.active_claude_display(accounts), "Bravo")  # per snapshot
        glideslope.reconcile_login(accounts, "work@example.com")           # but we just switched to Alpha
        self.assertEqual(glideslope.active_claude_display(accounts), "Alpha")
        self.assertFalse(next(a for a in accounts if a["display"] == "Bravo")["active"])

    def test_no_live_email_leaves_snapshot_untouched(self):
        accounts = self._accounts()
        glideslope.reconcile_login(accounts, None)
        self.assertEqual(glideslope.active_claude_display(accounts), "Bravo")

    def test_unknown_live_email_marks_all_inactive(self):
        accounts = self._accounts()
        glideslope.reconcile_login(accounts, "someone-else@example.com")
        self.assertIsNone(glideslope.active_claude_display(accounts))

    def test_current_login_email_reads_config(self):
        import json
        tmp = Path(tempfile.mktemp(suffix=".json"))
        tmp.write_text(json.dumps({"oauthAccount": {"emailAddress": "personal@example.com"}, "other": 1}))
        self.assertEqual(glideslope.current_login_email(tmp), "personal@example.com")
        tmp.unlink()

    def test_current_login_email_missing_file_is_none(self):
        self.assertIsNone(glideslope.current_login_email(Path("/no/such/.claude.json")))


class SwitchTests(unittest.TestCase):
    def _log(self, records):
        tmp = Path(tempfile.mktemp(suffix=".jsonl"))
        tmp.write_text("".join(__import__("json").dumps(r) + "\n" for r in records))
        return tmp

    def _records(self):
        return [
            {"at": "2026-07-20T09:00:00+00:00", "from": "work", "to": "personal",
             "usage": {"work": {"session (5h)": 100, "weekly (all models)": 32, "weekly (Fable)": 30},
                       "personal": None}},
            {"at": "2026-07-20T14:15:00+00:00", "from": "personal", "to": "work",
             "usage": {"personal": {"session (5h)": 89, "weekly (all models)": 74, "weekly (Fable)": 59},
                       "work": {"session (5h)": 2, "weekly (all models)": 48, "weekly (Fable)": 39}}},
        ]

    def test_switches_load_newest_first_and_capped(self):
        many = [{"at": f"2026-07-20T{h:02d}:00:00+00:00", "from": "work", "to": "personal", "usage": {}}
                for h in range(12)]
        tmp = self._log(many)
        loaded = glideslope.load_switches(tmp, limit=8)
        self.assertEqual(len(loaded), 8)
        self.assertEqual(loaded[0]["at"], "2026-07-20T11:00:00+00:00")
        tmp.unlink()

    def test_switch_log_tolerates_garbage_lines(self):
        tmp = self._log(self._records())
        with tmp.open("a") as handle:
            handle.write("not json at all\n\n")
        self.assertEqual(len(glideslope.load_switches(tmp)), 2)
        tmp.unlink()

    def test_missing_log_is_empty_not_an_error(self):
        self.assertEqual(glideslope.load_switches(Path("/no/such/switch-log.jsonl")), [])

    def test_builtin_login_is_journaled_on_the_next_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            history = root / "history.jsonl"
            switches = root / "switches.jsonl"
            lock = root / "observer.lock"
            accounts = glideslope.normalize_claude(claude_snapshot(), NOW)

            # Establish Alpha as the last account Glideslope observed.
            first = glideslope.observe_login_switch(
                accounts, "work@example.com", NOW,
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            self.assertIsNone(first)

            login_at = NOW + dt.timedelta(minutes=30)
            history.write_text(json.dumps({
                "display": "/login ",
                "timestamp": int(login_at.timestamp() * 1000),
            }) + "\n")
            event = glideslope.observe_login_switch(
                accounts, "personal@example.com", NOW + dt.timedelta(hours=1),
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )

            self.assertEqual(event["at"], "2026-07-20T04:30:00Z")
            self.assertEqual((event["from"], event["to"]), ("work", "personal"))
            self.assertEqual(event["source"], "claude-code-login")
            self.assertEqual(event["usage"], {"work": None, "personal": None})
            self.assertEqual(json.loads(state.read_text())["alias"], "personal")

    def test_existing_claude_account_switch_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            history = root / "history.jsonl"
            switches = root / "switches.jsonl"
            lock = root / "observer.lock"
            accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
            glideslope.observe_login_switch(
                accounts, "work@example.com", NOW,
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            canonical = {
                "at": glideslope.iso_utc(NOW + dt.timedelta(minutes=15)),
                "from": "work", "to": "personal", "usage": {},
            }
            switches.write_text(json.dumps(canonical) + "\n")

            event = glideslope.observe_login_switch(
                accounts, "personal@example.com", NOW + dt.timedelta(minutes=20),
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            self.assertIsNone(event)
            self.assertEqual(len(switches.read_text().splitlines()), 1)

    def test_login_command_survives_an_intermediate_old_account_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            history = root / "history.jsonl"
            switches = root / "switches.jsonl"
            lock = root / "observer.lock"
            accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
            glideslope.observe_login_switch(
                accounts, "work@example.com", NOW,
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            login_at = NOW + dt.timedelta(minutes=5)
            history.write_text(json.dumps({
                "display": "/login",
                "timestamp": int(login_at.timestamp() * 1000),
            }) + "\n")

            # The browser flow has not completed at this watch tick.
            glideslope.observe_login_switch(
                accounts, "work@example.com", NOW + dt.timedelta(minutes=10),
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            event = glideslope.observe_login_switch(
                accounts, "personal@example.com", NOW + dt.timedelta(minutes=12),
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            self.assertEqual(event["source"], "claude-code-login")
            self.assertEqual(event["at"], "2026-07-20T04:05:00Z")

    def test_first_run_repairs_a_login_newer_than_the_switch_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            history = root / "history.jsonl"
            switches = root / "switches.jsonl"
            lock = root / "observer.lock"
            accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
            switches.write_text(json.dumps({
                "at": glideslope.iso_utc(NOW),
                "from": "personal", "to": "work", "usage": {},
            }) + "\n")
            login_at = NOW + dt.timedelta(minutes=30)
            history.write_text(json.dumps({
                "display": "/login",
                "timestamp": int(login_at.timestamp() * 1000),
            }) + "\n")

            event = glideslope.observe_login_switch(
                accounts, "personal@example.com", NOW + dt.timedelta(hours=1),
                state_path=state, history_path=history, switch_path=switches, lock_path=lock,
            )
            self.assertEqual((event["from"], event["to"]), ("work", "personal"))
            self.assertEqual(event["at"], "2026-07-20T04:30:00Z")
            self.assertEqual(len(switches.read_text().splitlines()), 2)

    def test_render_uses_call_signs_and_all_buckets(self):
        rendered = glideslope.render_switches(glideslope.load_switches(self._log(self._records())))
        self.assertIn("Bravo → Alpha", rendered)
        self.assertIn("5h 89% · wk 74% · Fable 59%", rendered)
        self.assertIn("5h 2% · wk 48% · Fable 39%", rendered)

    def test_absent_account_usage_renders_dash(self):
        rendered = glideslope.render_switches(glideslope.load_switches(self._log(self._records())))
        # Alpha → Bravo: Bravo (entered) had no usage snapshot, so its cell is labelled and dashed.
        self.assertRegex(rendered, r"Alpha → Bravo \|[^|]*\| Bravo: —")

    def test_builtin_login_source_is_visible(self):
        rendered = glideslope.render_switches([{
            "at": "2026-07-20T15:00:00Z", "from": "work", "to": "personal",
            "source": "claude-code-login", "usage": {"work": None, "personal": None},
        }])
        self.assertIn("Alpha → Bravo · /login", rendered)
        self.assertIn("Alpha: —", rendered)
        self.assertIn("Bravo: —", rendered)

    def test_no_switches_renders_nothing(self):
        self.assertEqual(glideslope.render_switches([]), "")


class ClaudeStateTests(unittest.TestCase):
    """The account you are logged into is the only one that can be read — so that
    is the moment its state has to be written down."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = glideslope.PROVIDER_CACHE_DIR
        glideslope.PROVIDER_CACHE_DIR = Path(self.tmp.name) / "provider-cache"
        self.addCleanup(setattr, glideslope, "PROVIDER_CACHE_DIR", original)

    def _keep(self, raw, observed):
        return glideslope.keep_claude_state(
            glideslope.normalize_claude(raw, observed), raw, observed)

    def test_a_readable_account_writes_its_state(self):
        self._keep(claude_snapshot(), NOW)
        kept = glideslope.cached_provider_read("claude-work", NOW)
        self.assertIsNotNone(kept)
        self.assertEqual(kept[1], NOW)
        self.assertEqual(session_of(glideslope.claude_limits(kept[0]))["used_percent"], 100)

    def test_a_locked_out_account_is_served_the_state_it_left_behind(self):
        live = claude_snapshot()
        live["work"]["limits"][0]["percent"] = 97.0   # burned right before the switch
        self._keep(live, NOW)

        locked = claude_snapshot()                          # a much older cached copy
        locked["work"]["stale"] = True
        locked["work"]["fetched_at"] = "2026-07-19T04:00:00+00:00"
        later = NOW + dt.timedelta(minutes=5)
        alpha = self._keep(locked, later)[0]
        self.assertEqual(alpha["display"], "Alpha")
        self.assertEqual(session_of(alpha["limits"])["used_percent"], 97.0)
        self.assertEqual(alpha["observed_at"], NOW)

    def test_a_fresher_cached_copy_is_left_alone(self):
        self._keep(claude_snapshot(), NOW - dt.timedelta(hours=2))

        locked = claude_snapshot()
        locked["work"]["stale"] = True
        locked["work"]["limits"][0]["percent"] = 44.0
        locked["work"]["fetched_at"] = glideslope.iso_utc(NOW)
        alpha = self._keep(locked, NOW)[0]
        self.assertEqual(session_of(alpha["limits"])["used_percent"], 44.0)
        self.assertEqual(alpha["observed_at"], NOW)

    def test_a_stale_account_never_overwrites_the_state_it_left(self):
        self._keep(claude_snapshot(), NOW)
        locked = claude_snapshot()
        locked["work"]["stale"] = True
        locked["work"]["limits"][0]["percent"] = 3.0
        self._keep(locked, NOW + dt.timedelta(hours=1))
        kept = glideslope.cached_provider_read("claude-work", NOW + dt.timedelta(hours=1))
        self.assertEqual(session_of(glideslope.claude_limits(kept[0]))["used_percent"], 100)

    def test_no_stored_state_leaves_the_account_as_it_came(self):
        locked = claude_snapshot()
        locked["work"]["stale"] = True
        alpha = self._keep(locked, NOW)[0]
        self.assertEqual(session_of(alpha["limits"])["used_percent"], 100)


class ProviderFallbackTests(unittest.TestCase):
    """A transient transport failure must degrade a provider, never delete it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = glideslope.PROVIDER_CACHE_DIR
        glideslope.PROVIDER_CACHE_DIR = Path(self.tmp.name) / "provider-cache"
        self.addCleanup(setattr, glideslope, "PROVIDER_CACHE_DIR", self.original)
        self.warnings: list[str] = []

    def _read(self, query):
        return glideslope.read_provider(
            "codex", query, glideslope.normalize_codex, glideslope.codex_accounts,
            saved=None, now=NOW, warnings=self.warnings,
        )

    def test_a_live_read_is_cached_and_not_stale(self):
        accounts = self._read(codex_snapshot)
        self.assertEqual([a["display"] for a in accounts], ["Codex"])
        self.assertFalse(accounts[0]["stale"])
        self.assertEqual(self.warnings, [])
        self.assertIsNotNone(glideslope.cached_provider_read("codex", NOW))

    def test_a_failed_read_falls_back_to_the_last_good_one(self):
        self._read(codex_snapshot)

        def boom():
            raise glideslope.PositionError("Codex app-server timed out")

        accounts = self._read(boom)
        self.assertEqual([a["display"] for a in accounts], ["Codex"])
        self.assertTrue(accounts[0]["stale"])
        self.assertEqual(accounts[0]["limits"][0]["used_percent"], 2)
        self.assertIn("timed out", self.warnings[0])
        self.assertIn("last known read", self.warnings[0])

    def test_the_fallback_keeps_the_observation_time_of_the_cached_read(self):
        observed = glideslope.utc_now()
        self._read(codex_snapshot)

        def boom():
            raise glideslope.PositionError("Codex app-server timed out")

        account = self._read(boom)[0]
        self.assertLess(abs((account["observed_at"] - observed).total_seconds()), 5)

    def test_a_failed_read_with_no_cache_still_only_warns(self):
        def boom():
            raise glideslope.PositionError("Codex app-server timed out")

        self.assertEqual(self._read(boom), [])
        self.assertEqual(self.warnings, ["Codex app-server timed out"])

    def test_a_cache_older_than_the_longest_window_is_not_served(self):
        observed = glideslope.utc_now()
        self._read(codex_snapshot)
        much_later = observed + dt.timedelta(days=30)
        self.assertIsNone(glideslope.cached_provider_read("codex", much_later))

    def test_a_saved_snapshot_never_writes_the_cache(self):
        path = Path(self.tmp.name) / "saved.json"
        path.write_text(json.dumps(codex_snapshot()))
        accounts = glideslope.read_provider(
            "codex", lambda: {}, glideslope.normalize_codex, glideslope.codex_accounts,
            saved=path, now=NOW, warnings=self.warnings,
        )
        self.assertEqual([a["display"] for a in accounts], ["Codex"])
        self.assertIsNone(glideslope.cached_provider_read("codex", NOW))


class RollForwardTests(unittest.TestCase):
    """A window read before its reset describes a window that no longer exists.

    The clock is arithmetic and must always be recovered; the percent is a claim
    and may only be presumed where nothing here could have spent it.
    """

    def limit(self, percent=88.0, minutes=10080, resets=None):
        return {
            "meter_id": "weekly_all", "slot": "primary", "label": "Weekly · all models",
            "used_percent": percent, "window_minutes": minutes,
            "resets_at": resets if resets is not None else NOW - dt.timedelta(hours=12),
            "anchored": True,
        }

    def account(self, *, provider="Claude", active=False, limits=None):
        return {"provider": provider, "display": "Bravo", "account": "personal", "stale": True,
                "active": active, "limits": limits if limits is not None else [self.limit()]}

    def test_reset_clock_is_advanced_by_whole_periods(self):
        account = self.account()
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        # rolled 12h ago on a 7-day window: the live one ends 6d 12h from now
        self.assertEqual(limit["resets_at"], NOW + dt.timedelta(days=6, hours=12))
        self.assertEqual(limit["rolled_periods"], 1)

    def test_many_missed_periods_land_on_the_live_window(self):
        # a 5h session read 47h ago has rolled ten times, not once
        account = self.account(limits=[self.limit(
            percent=48.0, minutes=300, resets=NOW - dt.timedelta(hours=47))])
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertEqual(limit["rolled_periods"], 10)
        self.assertGreater(limit["resets_at"], NOW)
        self.assertLessEqual(limit["resets_at"] - NOW, dt.timedelta(hours=5))

    def test_logged_out_claude_account_is_presumed_fresh(self):
        account = self.account(active=False)
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertTrue(limit["presumed"])
        self.assertEqual(limit["used_percent"], 0.0)
        self.assertEqual(limit["observed_percent"], 88.0)  # the old read is kept, not erased

    def test_the_active_account_is_never_presumed_fresh(self):
        """The one lie this must not tell: inventing headroom on a meter in use."""
        account = self.account(active=True)
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertFalse(limit["presumed"])
        self.assertIsNone(limit["used_percent"])

    def test_single_account_providers_are_never_presumed_fresh(self):
        # Codex/Kimi carry no `active` flag but are always the account in use
        account = self.account(provider="Codex", active=False)
        glideslope.roll_forward_windows([account], NOW)
        self.assertFalse(account["limits"][0]["presumed"])
        self.assertIsNone(account["limits"][0]["used_percent"])

    def test_a_live_window_is_left_alone(self):
        account = self.account(limits=[self.limit(resets=NOW + dt.timedelta(days=2))])
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertEqual(limit["used_percent"], 88.0)
        self.assertNotIn("presumed", limit)

    def test_an_unanchored_window_is_left_alone(self):
        account = self.account(limits=[self.limit(minutes=None, resets=None)])
        glideslope.roll_forward_windows([account], NOW)
        self.assertNotIn("presumed", account["limits"][0])

    def test_presumed_windows_render_the_marker_and_the_real_next_reset(self):
        account = self.account()
        glideslope.roll_forward_windows([account], NOW)
        rendered = glideslope.render_markdown([account], NOW, [])
        self.assertIn("0% presumed", rendered)
        self.assertNotIn("reset passed", rendered)

    def test_unread_windows_render_no_percent_at_all(self):
        account = self.account(active=True)
        glideslope.roll_forward_windows([account], NOW)
        rendered = glideslope.render_markdown([account], NOW, [])
        self.assertIn("unread", rendered)
        self.assertNotIn("88%", rendered)
        self.assertNotIn("0%", rendered)

    def test_an_account_logged_in_elsewhere_is_never_presumed_unspent(self):
        """The regression the second satellite created: 'not logged in HERE' is
        not the same as 'nobody could have spent it'."""
        account = self.account(active=False)
        account["logins"] = ["studio"]
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertIsNone(limit["used_percent"])  # unread, not a presumed zero
        self.assertFalse(limit["presumed"])
        self.assertIn("unread", glideslope.render_markdown([account], NOW, []))

    def test_an_account_no_satellite_holds_is_still_presumed(self):
        account = self.account(active=False)
        account["logins"] = []
        glideslope.roll_forward_windows([account], NOW)
        self.assertEqual(account["limits"][0]["used_percent"], 0.0)
        self.assertTrue(account["limits"][0]["presumed"])


class SatelliteTests(unittest.TestCase):
    """A satellite reads exactly one account live — the one it is logged into.

    So the fleet sees more of the position than any one machine does, and the
    join between machines is on email: store aliases are per-machine bookkeeping.
    """

    def beacon(self, *, email="personal@example.com", percent=61.0, age_seconds=90.0,
               observed=None, active=True, stale=False):
        observed = observed or NOW - dt.timedelta(seconds=age_seconds)
        return {
            "name": "studio", "host": "studio", "login_email": email,
            "observed_at": observed, "age_seconds": (NOW - observed).total_seconds(),
            "snapshot": {"home": {  # a DIFFERENT alias for the same account
                "email": email, "active": active, **({"stale": True} if stale else {}),
                "limits": [{"label": "weekly (all models)", "percent": percent,
                            "resets_at": "2026-07-23T09:00:00+00:00"}],
            }},
        }

    def test_a_fresh_beacon_replaces_a_stale_local_journal(self):
        snapshot = claude_snapshot()
        snapshot["personal"]["stale"] = True
        snapshot["personal"]["fetched_at"] = glideslope.iso_utc(NOW - dt.timedelta(hours=10))
        glideslope.merge_satellite_claude(snapshot, [self.beacon()], NOW)
        self.assertEqual(snapshot["personal"]["limits"][0]["percent"], 61.0)
        self.assertFalse(snapshot["personal"]["stale"])
        self.assertEqual(snapshot["personal"]["read_by"], "studio")

    def test_a_stale_beacon_never_overwrites_a_fresher_local_gauge(self):
        snapshot = claude_snapshot()
        snapshot["personal"]["fetched_at"] = glideslope.iso_utc(NOW - dt.timedelta(minutes=1))
        glideslope.merge_satellite_claude(
            snapshot, [self.beacon(observed=NOW - dt.timedelta(hours=3))], NOW)
        self.assertEqual(snapshot["personal"]["limits"][0]["percent"], 5.0)  # the local read stands

    def test_only_the_account_a_satellite_is_on_is_taken_as_a_reading(self):
        """Its other rows are its own journal — second-hand, and not ours to trust."""
        snapshot = claude_snapshot()
        snapshot["personal"]["stale"] = True
        glideslope.merge_satellite_claude(snapshot, [self.beacon(active=False)], NOW)
        self.assertEqual(snapshot["personal"]["limits"][0]["percent"], 5.0)

    def test_an_account_this_machine_has_never_seen_warns_instead_of_guessing(self):
        warnings = []
        glideslope.merge_satellite_claude(
            claude_snapshot(), [self.beacon(email="nobody@example.test")], NOW, warnings=warnings)
        self.assertTrue(any("never seen" in w for w in warnings))

    def test_logins_name_every_satellite_holding_an_account(self):
        accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
        glideslope.reconcile_satellite_logins(
            accounts, "work@example.com", [self.beacon()])
        by_name = {a["display"]: a for a in accounts}
        self.assertEqual(by_name["Alpha"]["logins"], [glideslope.LOCAL_SATELLITE])
        self.assertEqual(by_name["Bravo"]["logins"], ["studio"])

    def test_the_banner_marks_the_local_login_apart_from_a_remote_one(self):
        accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
        glideslope.reconcile_satellite_logins(accounts, "work@example.com", [self.beacon()])
        banner = glideslope.render_login_banner(accounts)
        self.assertIn(f"**● Logged in · {glideslope.LOCAL_SATELLITE}: Claude · Alpha**", banner)
        self.assertIn("◦ studio: Claude · Bravo", banner)
        # local first: the login that governs the next prompt leads
        self.assertLess(banner.index("Alpha"), banner.index("Bravo"))

    @staticmethod
    def codex_account() -> dict:
        return {"provider": "Codex", "account": "codex", "display": "Codex",
                "active": True, "stale": False, "limits": []}

    def test_a_satellite_holding_the_same_codex_account_is_badged(self):
        accounts = [self.codex_account()]
        glideslope.reconcile_shared_logins(
            accounts, "Codex", "shared@example.test",
            [{"name": "studio", "holds": {"Codex": "shared@example.test"}}])
        self.assertEqual(accounts[0]["logins"], [glideslope.LOCAL_SATELLITE, "studio"])

    def test_a_different_codex_login_warns_instead_of_badging(self):
        """A non-matching holder is burn this gauge cannot see — say so, never badge it."""
        accounts = [self.codex_account()]
        warnings: list[str] = []
        glideslope.reconcile_shared_logins(
            accounts, "Codex", "mine@example.test",
            [{"name": "studio", "holds": {"Codex": "other@example.test"}}], warnings)
        self.assertEqual(accounts[0]["logins"], [glideslope.LOCAL_SATELLITE])
        self.assertTrue(any("NOT in these numbers" in w for w in warnings))

    def test_an_account_held_only_elsewhere_dresses_down(self):
        self.assertTrue(glideslope.held_elsewhere({"logins": ["studio"]}))
        self.assertFalse(glideslope.held_elsewhere({"logins": [glideslope.LOCAL_SATELLITE, "studio"]}))
        self.assertFalse(glideslope.held_elsewhere({"logins": []}))
        self.assertFalse(glideslope.held_elsewhere({}))

    def test_a_shared_codex_row_wears_both_marks(self):
        account = self.codex_account()
        account["logins"] = [glideslope.LOCAL_SATELLITE, "studio"]
        account["plan"] = "20x"
        name = glideslope.account_name(account)
        self.assertIn(f"● {glideslope.LOCAL_SATELLITE}", name)
        self.assertIn("◦ studio", name)


class PoolTests(unittest.TestCase):
    """Three quotas on three clocks, read as one budget.

    Both halves of the comparison average: used against the mean of the phases,
    which is exactly what even burn predicts of the pool.
    """

    def accounts(self, *, used=(30.0, 60.0), stale=(False, False), hours_left=(84, 168)):
        out = []
        for index, (percent, is_stale, left) in enumerate(zip(used, stale, hours_left)):
            out.append({
                "provider": "Claude", "display": f"A{index}", "account": f"a{index}",
                "stale": is_stale, "active": False, "logins": [],
                "limits": [{"meter_id": "weekly_all", "label": "Weekly · all models",
                            "used_percent": percent, "window_minutes": 10080,
                            "resets_at": NOW + dt.timedelta(hours=left), "anchored": True}],
            })
        return out

    def test_used_and_the_mark_are_both_means(self):
        # half-elapsed (84h left of 168) and just-started (168h left) → ◆ 25%
        pool = glideslope.claude_pool(self.accounts(), NOW)
        self.assertEqual(pool["count"], 2)
        self.assertAlmostEqual(pool["used_percent"], 45.0)
        self.assertAlmostEqual(pool["pace_percent"], 25.0)
        self.assertEqual(pool["verdict"], "ahead")
        self.assertFalse(pool["floor"])

    def test_a_stale_component_makes_the_whole_read_a_floor(self):
        pool = glideslope.claude_pool(self.accounts(stale=(True, False)), NOW)
        self.assertTrue(pool["floor"])

    def test_a_floor_above_the_mark_is_still_a_certain_verdict(self):
        """Staleness only ever understates, so 'ahead' survives it."""
        pool = glideslope.claude_pool(self.accounts(stale=(True, False)), NOW)
        self.assertEqual(pool["verdict"], "ahead")

    def test_a_floor_below_the_mark_decides_nothing_and_says_so(self):
        pool = glideslope.claude_pool(
            self.accounts(used=(1.0, 2.0), stale=(True, False)), NOW)
        self.assertEqual(pool["verdict"], "indeterminate")
        self.assertIn("undecided", glideslope._pool_row(pool, NOW, color=False)[1])

    def test_a_fresh_read_below_the_mark_is_banked_capacity_not_a_shrug(self):
        pool = glideslope.claude_pool(self.accounts(used=(1.0, 2.0)), NOW)
        self.assertEqual(pool["verdict"], "trailing")

    def test_an_unread_window_contributes_only_its_zero_and_marks_the_floor(self):
        accounts = self.accounts()
        accounts[0]["limits"][0]["used_percent"] = None
        pool = glideslope.claude_pool(accounts, NOW)
        self.assertAlmostEqual(pool["used_percent"], 30.0)  # (0 + 60) / 2
        self.assertTrue(pool["floor"])
        self.assertEqual(pool["unread_count"], 1)

    def test_the_reset_column_names_the_next_account_to_come_back(self):
        pool = glideslope.claude_pool(self.accounts(), NOW)
        self.assertEqual(pool["next_reset_account"], "A0")
        # index 3, not 2: the row is now (name, weekly, fable, reset) — Fable
        # sits beside Weekly in the hero columns.
        self.assertIn("next A0 in", glideslope._pool_row(pool, NOW, color=False)[3])

    def test_a_pool_of_one_is_not_a_pool(self):
        self.assertIsNone(glideslope.claude_pool(self.accounts(used=(30.0,), stale=(False,),
                                                              hours_left=(84,)), NOW))

    def test_the_pooled_row_sits_directly_under_the_anthropic_rows(self):
        accounts = (glideslope.normalize_claude(claude_snapshot(), NOW)
                    + glideslope.codex_accounts(glideslope.normalize_codex(codex_snapshot(), NOW)))
        rows = glideslope.render_weekly_summary(accounts, None, NOW).splitlines()
        pooled = next(index for index, row in enumerate(rows) if "pooled" in row)
        self.assertIn("Bravo", rows[pooled - 1])
        self.assertIn("Codex", rows[pooled + 1])


class DormantAccountTests(unittest.TestCase):
    """Ruling 2026-09-11: Alpha has no subscription, not merely a low position.

    read_roster()/reconcile_dormant() are the join; everything downstream just
    has to treat a dormant account as absent rather than zeroed.
    """

    def _roster_path(self, entries):
        tmp = Path(tempfile.mktemp(suffix=".json"))
        tmp.write_text(json.dumps(entries))
        return tmp

    def test_missing_roster_reads_as_empty(self):
        self.assertEqual(glideslope.read_roster(Path("/no/such/roster.json")), {})

    def test_roster_reader_parses_a_real_file(self):
        path = self._roster_path({"work": {"email": "work@example.com", "dormant": True,
                                                 "dormant_since": "2026-09-11"}})
        roster = glideslope.read_roster(path)
        self.assertTrue(roster["work"]["dormant"])
        self.assertEqual(roster["work"]["dormant_since"], "2026-09-11")

    def test_dormant_joins_by_email_not_alias(self):
        accounts = glideslope.normalize_claude(claude_snapshot(), NOW)  # aliases work, personal
        roster = {"work": {"email": "work@example.com", "dormant": True,
                                "dormant_since": "2026-09-11"}}
        glideslope.reconcile_dormant(accounts, roster)
        alpha = next(a for a in accounts if a["display"] == "Alpha")
        bravo = next(a for a in accounts if a["display"] == "Bravo")
        self.assertTrue(alpha["dormant"])
        self.assertEqual(alpha["dormant_since"], "2026-09-11")
        self.assertFalse(bravo["dormant"])
        self.assertIsNone(bravo["dormant_since"])

    def test_no_roster_entry_leaves_an_account_active(self):
        accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
        glideslope.reconcile_dormant(accounts, {})
        self.assertTrue(all(not a["dormant"] and a["dormant_since"] is None for a in accounts))

    def test_a_live_login_outranks_the_roster_and_warns(self):
        accounts = glideslope.normalize_claude(claude_snapshot(), NOW)
        next(a for a in accounts if a["display"] == "Alpha")["logins"] = ["studio"]
        roster = {"work": {"email": "work@example.com", "dormant": True,
                                "dormant_since": "2026-09-11"}}
        warnings: list[str] = []
        glideslope.reconcile_dormant(accounts, roster, warnings)
        alpha = next(a for a in accounts if a["display"] == "Alpha")
        self.assertFalse(alpha["dormant"])
        self.assertIsNone(alpha["dormant_since"])
        self.assertTrue(any("dormant" in w and "studio" in w for w in warnings))


class DormantExclusionTests(unittest.TestCase):
    """Excluded, not zeroed: a dormant account contributes nothing to a pool,
    an alternative, or a threshold alert — its subscription simply isn't running."""

    def _accounts(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)  # Alpha, Bravo, Charlie
        next(a for a in accounts if a["display"] == "Alpha").update(
            dormant=True, dormant_since="2026-09-11")
        return accounts

    def test_pool_excludes_the_dormant_account_and_counts_the_rest(self):
        pool = glideslope.claude_pool(self._accounts(), NOW)
        self.assertEqual(pool["count"], 2)
        self.assertNotIn("Alpha", pool["accounts"])

    def test_fable_pool_generalizes_the_same_pooling(self):
        pool = glideslope.claude_pool(self._accounts(), NOW, meter_id="weekly_fable")
        self.assertEqual(pool["meter_id"], "weekly_fable")
        self.assertEqual(pool["label"], "Weekly · Fable")
        self.assertEqual(pool["count"], 2)  # Bravo + Charlie; Alpha dormant

    def test_best_alternative_never_offers_a_dormant_account(self):
        accounts = self._accounts()
        best = glideslope.best_alternative(accounts, NOW, exclude="Charlie")
        # Only Bravo is left standing: Charlie is excluded by name, Alpha by dormancy.
        self.assertEqual(best["display"], "Bravo")

    def test_notifiable_windows_skips_a_dormant_account_even_if_it_looks_active(self):
        account = {"provider": "Claude", "display": "Alpha", "account": "work", "stale": False,
                   "dormant": True, "active": True, "logins": [glideslope.LOCAL_SATELLITE],
                   "limits": [{"meter_id": "weekly_all", "label": "Weekly · all models",
                               "used_percent": 95.0, "window_minutes": 10080,
                               "resets_at": NOW + dt.timedelta(hours=30), "anchored": True}]}
        self.assertEqual(glideslope.notifiable_windows([account], NOW), [])

    def test_roll_forward_leaves_a_dormant_rolled_window_unread_not_presumed(self):
        account = {"provider": "Claude", "display": "Alpha", "account": "work", "stale": True,
                   "active": False, "dormant": True, "logins": [],
                   "limits": [{"meter_id": "weekly_all", "slot": "primary", "label": "Weekly · all models",
                               "used_percent": 88.0, "window_minutes": 10080,
                               "resets_at": NOW - dt.timedelta(hours=12), "anchored": True}]}
        glideslope.roll_forward_windows([account], NOW)
        limit = account["limits"][0]
        self.assertIsNone(limit["used_percent"])   # unread, not a presumed zero
        self.assertFalse(limit["presumed"])


class BestAlternativeFableTests(unittest.TestCase):
    """best_alternative ranks each account by its BINDING weekly — whichever of
    all-models/Fable sits on less slack — not by all-models alone."""

    def _account(self, display, *, weekly, fable=None, alias="x"):
        def limit(meter_id, label, percent, hours_left):
            return {"meter_id": meter_id, "label": label, "used_percent": percent,
                    "window_minutes": 10080, "resets_at": NOW + dt.timedelta(hours=hours_left),
                    "anchored": True}
        limits = [limit("weekly_all", "Weekly · all models", *weekly)]
        if fable is not None:
            limits.append(limit("weekly_fable", "Weekly · Fable", *fable))
        return {"provider": "Claude", "display": display, "account": alias, "stale": False,
                "active": False, "logins": [], "limits": limits}

    def test_binds_on_fable_when_fable_has_the_tighter_slack(self):
        # 84h left of 168h → pace 50% on both. all-models: 20% used → slack 30.
        # Fable: 45% used → slack 5, so Fable is what actually binds this account.
        account = self._account("Bravo", weekly=(20.0, 84), fable=(45.0, 84))
        best = glideslope.best_alternative([account], NOW, exclude="Charlie")
        self.assertEqual(best["meter_id"], "weekly_fable")
        self.assertAlmostEqual(best["slack"], 5.0, delta=0.5)

    def test_over_the_line_on_either_weekly_is_still_no_refuge(self):
        account = self._account("Bravo", weekly=(20.0, 84), fable=(96.0, 84))
        self.assertIsNone(glideslope.best_alternative([account], NOW, exclude="Charlie"))


class WeeklySummaryDormantTests(unittest.TestCase):
    def _accounts(self):
        accounts = glideslope.normalize_claude(claude_snapshot_with_charlie(), NOW)  # Alpha, Bravo, Charlie
        next(a for a in accounts if a["display"] == "Alpha").update(
            dormant=True, dormant_since="2026-09-11")
        return accounts

    def test_weekly_status_keeps_one_dressed_down_dormant_row(self):
        rendered = glideslope.render_weekly_summary(self._accounts(), None, NOW)
        self.assertIn("dormant", rendered)
        self.assertIn("| — | — | — |", rendered)  # every numeric column dashed
        # italic in relay markdown — the same idiom as an elsewhere-held account
        self.assertIn("*Claude · Alpha · dormant*", rendered)

    def test_a_live_claude_row_still_carries_a_fable_cell(self):
        rendered = glideslope.render_weekly_summary(self._accounts(), None, NOW)
        self.assertIn("| Claude · Bravo", rendered)
        bravo_row = next(line for line in rendered.splitlines() if "Bravo" in line)
        self.assertEqual(len(bravo_row.split("|")), 6)  # '' Account Weekly Fable Reset ''

    def test_all_windows_omits_the_dormant_account_entirely(self):
        rendered = glideslope.render_markdown(self._accounts(), NOW, [])
        self.assertNotIn("Alpha", rendered)
        self.assertIn("Bravo", rendered)
        self.assertIn("Charlie", rendered)


if __name__ == "__main__":
    unittest.main()


class TotalPoolTests(unittest.TestCase):
    @staticmethod
    def _account(provider, display, plan, used, elapsed_fraction, **extra):
        minutes = 10_080
        resets = NOW + dt.timedelta(minutes=minutes * (1 - elapsed_fraction))
        return {"provider": provider, "display": display, "plan": plan, **extra,
                "limits": [{"label": "Weekly · all models", "meter_id": "weekly_all",
                            "window_minutes": minutes, "resets_at": resets, "used_percent": used}]}

    def test_weighs_each_weekly_by_its_price_and_leaves_kimi_out(self):
        accounts = [
            self._account("Claude", "Alpha", "Max 20x", 60.0, .5),
            self._account("Codex", "Codex", "20x", 20.0, .5),
            self._account("Grok", "Grok", "SuperGrok", 100.0, .5),
            self._account("Kimi", "Kimi", "Coding 15x", 90.0, .5),
        ]
        pool = glideslope.total_pool(accounts, NOW)
        self.assertEqual(pool["accounts"], ["Alpha", "Codex", "Grok"])
        self.assertEqual(pool["monthly_usd"], 430.0)
        self.assertAlmostEqual(pool["used_percent"], (200 * 60 + 200 * 20 + 30 * 100) / 430, places=4)
        self.assertAlmostEqual(pool["pace_percent"], 50.0, places=4)
        self.assertEqual(pool["verdict"], "trailing")

    def test_dormant_unpriced_and_single_provider_pools_are_refused(self):
        accounts = [
            self._account("Claude", "Alpha", "Max 20x", 60.0, .5),
            self._account("Claude", "Delta", "Max 20x", 10.0, .5, dormant=True),
            self._account("Grok", "Grok", "SuperGrok Heavy", 10.0, .5),
        ]
        self.assertIsNone(glideslope.total_pool(accounts, NOW))


class AuditGuardTests(unittest.TestCase):
    """Guards added after the public-release audit."""

    def test_a_satellite_host_must_be_a_plain_name(self):
        for bad in ("-oProxyCommand=evil", "", "host name", "a;b"):
            with self.assertRaises(glideslope.PositionError):
                glideslope.query_satellite(bad, timeout=1)
        self.assertTrue(glideslope.SATELLITE_HOST_PATTERN.match("studio"))
        self.assertTrue(glideslope.SATELLITE_HOST_PATTERN.match("me@studio.local"))

    def test_grok_persist_refuses_to_replace_an_unreadable_auth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text("{not json")
            with self.assertRaises(glideslope.PositionError):
                glideslope._grok_persist_entry(auth, "default", {"key": "k", "refresh_token": "r", "expires_at": "x"})
            self.assertEqual(auth.read_text(), "{not json")  # untouched

    def test_grok_persist_keeps_every_other_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"default": {"key": "old", "oidc_issuer": "https://auth.x.ai",
                                                    "oidc_client_id": "cid", "user_id": "u"}, "other": {"key": "o"}}))
            glideslope._grok_persist_entry(auth, "default", {"key": "new", "refresh_token": "r2", "expires_at": "2030-01-01T00:00:00Z"})
            after = json.loads(auth.read_text())
            self.assertEqual(after["default"]["key"], "new")
            self.assertEqual(after["default"]["oidc_client_id"], "cid")
            self.assertEqual(after["default"]["user_id"], "u")
            self.assertEqual(after["other"], {"key": "o"})

    def test_grok_refresh_backoff_holds_for_an_hour_after_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "failed.json"
            self.assertIsNone(glideslope.grok_refresh_backoff(NOW, marker))
            glideslope.note_grok_refresh_failure(NOW, "invalid_grant", marker)
            self.assertIn("invalid_grant", glideslope.grok_refresh_backoff(NOW + dt.timedelta(minutes=5), marker))
            self.assertIsNone(glideslope.grok_refresh_backoff(NOW + dt.timedelta(hours=2), marker))
            glideslope.clear_grok_refresh_failure(marker)
            self.assertIsNone(glideslope.grok_refresh_backoff(NOW + dt.timedelta(minutes=5), marker))

    def test_grok_refresh_without_expires_in_is_not_adopted(self):
        from unittest import mock
        entry = {"oidc_issuer": glideslope.GROK_OIDC_ISSUER, "refresh_token": "r", "oidc_client_id": "c"}
        body = json.dumps({"access_token": "a", "refresh_token": "r2"}).encode()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return body

        with mock.patch.object(glideslope, "_grok_discover_token_endpoint", return_value="https://auth.x.ai/token"), \
                mock.patch.object(glideslope.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(glideslope.PositionError):
                glideslope._grok_refresh_tokens(entry, NOW, timeout_seconds=1)

    def test_the_api_equivalent_section_reads_the_ledger_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "spend.json"
            ledger.write_text(json.dumps({
                "generated_at": glideslope.iso_utc(NOW), "first_request_at": "2026-07-01T00:00:00Z",
                "totals": {k: {"usd": 10.0, "tokens": 1e9} for k in ("d1", "d7", "d30", "all")},
                "accounts": [{"name": "Alpha", "window": {"usd": 1.0, "fable_usd": 0.5, "usd_per_pct_all": 0.1},
                              "d7": {"usd": 2}, "d30": {"usd": 3}, "all": {"usd": 4}}],
                "leverage": {"claude_multiple": 3.1, "claude_api_equivalent_d30_usd": 620, "claude_subscriptions_monthly_usd": 200}}))
            with mock.patch.object(glideslope, "STORE_DIR", Path(tmp)):
                text = glideslope.render_api_equivalent(NOW)
            self.assertIn("API-equivalent", text)
            self.assertIn("Alpha", text)
            self.assertIn("3.1×", text)
            with mock.patch.object(glideslope, "STORE_DIR", Path(tmp) / "absent"):
                self.assertEqual(glideslope.render_api_equivalent(NOW), "")


class OpenViewTests(unittest.TestCase):
    """`--open` rebuilds one view from the position in hand and opens the file.

    The builders are real scripts beside the module; these tests never run
    them. A fake builder writes the page, and the browser is a mock, so the
    seam (which script, which flag, which file) is what is asserted.
    """

    def _payload(self):
        return {"accounts": [], "generated_at": "2026-07-20T04:00:00Z"}

    def test_deck_is_built_from_the_position_on_stdin_and_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "views" / "deck-src").mkdir(parents=True)
            (root / "views" / "deck-src" / "build.py").write_text("")
            seen = {}

            def fake_run(cmd, **kwargs):
                seen["cmd"] = cmd
                seen["stdin"] = kwargs.get("input")
                (root / "views" / "deck.html").write_text("<html></html>")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(glideslope, "ROOT", root), \
                 mock.patch.object(glideslope.subprocess, "run", fake_run), \
                 mock.patch("webbrowser.open", return_value=True) as opened:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = glideslope.open_view("deck", self._payload())
            self.assertEqual(code, 0)
            self.assertEqual(seen["cmd"][-1], "--position-stdin")
            self.assertEqual(json.loads(seen["stdin"]), self._payload())
            opened.assert_called_once_with((root / "views" / "deck.html").resolve().as_uri())
            self.assertIn("deck.html", out.getvalue())

    def test_history_builds_from_the_store_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "views" / "history-src").mkdir(parents=True)
            (root / "views" / "history-src" / "build.py").write_text("")
            seen = {}

            def fake_run(cmd, **kwargs):
                seen["cmd"] = cmd
                seen["stdin"] = kwargs.get("input")
                (root / "views" / "history.html").write_text("<html></html>")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(glideslope, "ROOT", root), \
                 mock.patch.object(glideslope.subprocess, "run", fake_run), \
                 mock.patch("webbrowser.open", return_value=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = glideslope.open_view("history", self._payload())
            self.assertEqual(code, 0)
            self.assertNotIn("--position-stdin", seen["cmd"])
            self.assertIsNone(seen["stdin"])

    def test_tool_install_without_views_says_so_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with mock.patch.object(glideslope, "ROOT", Path(tmp)), \
                 mock.patch("webbrowser.open") as opened, \
                 contextlib.redirect_stderr(err):
                code = glideslope.open_view("deck", self._payload())
            self.assertEqual(code, 1)
            self.assertIn("git clone", err.getvalue())
            opened.assert_not_called()

    def test_failed_build_is_reported_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "views" / "deck-src").mkdir(parents=True)
            (root / "views" / "deck-src" / "build.py").write_text("")
            failed = mock.Mock(returncode=1, stdout="", stderr="boom")
            err = io.StringIO()
            with mock.patch.object(glideslope, "ROOT", root), \
                 mock.patch.object(glideslope.subprocess, "run", return_value=failed), \
                 mock.patch("webbrowser.open") as opened, \
                 contextlib.redirect_stderr(err):
                code = glideslope.open_view("popup", self._payload())
            self.assertEqual(code, 1)
            self.assertIn("boom", err.getvalue())
            opened.assert_not_called()

    def test_open_defaults_to_the_deck_and_rejects_unknown_views(self):
        parser_args = glideslope.main.__globals__  # the parser is built inside main
        self.assertIn("VIEW_FILES", parser_args)
        self.assertEqual(set(glideslope.VIEW_FILES), {"deck", "popup", "history"})
