"""Hermetic pricing, refresh, and tier-ledger coverage."""

from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import pricing


M = 1_000_000
ASTRA = {
    "input_cost_per_token": 10e-6,
    "output_cost_per_token": 50e-6,
    "cache_read_input_token_cost": 1e-6,
    "cache_creation_input_token_cost": 12.5e-6,
    "input_cost_per_token_priority": 20e-6,
    "output_cost_per_token_priority": 100e-6,
    "cache_read_input_token_cost_priority": 2e-6,
    "cache_creation_input_token_cost_priority": 25e-6,
    "input_cost_per_token_ultrafast": 60e-6,
    "output_cost_per_token_ultrafast": 300e-6,
    "cache_read_input_token_cost_ultrafast": 6e-6,
    "cache_creation_input_token_cost_ultrafast": 75e-6,
    "input_cost_per_token_above_272k_tokens": 20e-6,
    "output_cost_per_token_above_272k_tokens": 75e-6,
    "cache_read_input_token_cost_above_272k_tokens": 2e-6,
    "input_cost_per_token_above_272k_tokens_ultrafast": 120e-6,
    "output_cost_per_token_above_272k_tokens_ultrafast": 450e-6,
    "cache_read_input_token_cost_above_272k_tokens_ultrafast": 12e-6,
}
SOL = {
    "input_cost_per_token": 2e-6,
    "output_cost_per_token": 10e-6,
    "cache_read_input_token_cost": 0.2e-6,
    "input_cost_per_token_priority": 4e-6,
    "output_cost_per_token_priority": 20e-6,
}
PLAIN = {"input_cost_per_token": 1e-6, "output_cost_per_token": 4e-6}


def make_pricer() -> pricing.Pricer:
    return pricing.Pricer(lite={
        "gpt-6-astra": ASTRA,
        "gpt-6.1-sol": SOL,
        "gpt-plain": PLAIN,
        "gpt-5": PLAIN,
    })


def usd(cost: tuple[float, ...]) -> float:
    return sum(cost) / M


def test_rates_cover_list_derived_estimated_standard_and_unpriced_statuses():
    p = make_pricer()

    assert p.rates("gpt-6-astra", "fast")[1] == "list"

    derived, derived_status = p.rates("gpt-6.1-sol", "fast")
    assert derived_status == "derived"
    assert abs(derived[2] - 0.4) < 1e-12

    estimated, estimated_status = p.rates("gpt-6.1-sol", "ultrafast")
    assert estimated_status == "estimated"
    assert abs(estimated[0] - 12.0) < 1e-12
    assert abs(estimated[1] - 60.0) < 1e-12

    standard, standard_status = p.rates("gpt-plain", "ultrafast")
    assert standard_status == "standard"
    assert standard[:2] == (1.0, 4.0)
    assert p.rates("model-without-a-rate")[1] is None
    assert p.cost("model-without-a-rate", 1, 0, 0, 0, 0, 1) is None
    assert p.unpriced["model-without-a-rate"] == 1


def test_premium_tiers_and_cache_cost_products_keep_their_old_values():
    p = make_pricer()
    args = ("gpt-6-astra", 1000, 1000, 1000, 0, 0, 3000)
    standard = p.cost(*args)
    assert standard == (10_000, 50_000, 1_000, 0)
    assert usd(p.cost(*args, "fast")) == 2 * usd(standard)
    assert usd(p.cost(*args, "ultrafast")) == 6 * usd(standard)

    claude = ("claude-opus-5-5", 10, 100, 1000, 500, 500, 2110)
    assert abs(usd(p.cost(*claude, "fast")) - 2 * usd(p.cost(*claude))) < 1e-12
    assert not p.underpriced


def test_long_context_uses_the_request_prompt_strictly_above_the_threshold():
    p = make_pricer()
    short, short_status = p.rates("gpt-6-astra", "ultrafast", 272_000)
    long, long_status = p.rates("gpt-6-astra", "ultrafast", 272_001)
    assert short_status == long_status == "list"
    assert short[0] == 60.0
    assert long[0] == 120.0

    merged = p.cost("gpt-6-astra", 400_000, 0, 0, 0, 0, 400_000, prompt=50_000)
    assert usd(merged) == 400_000 * 10 / M


def test_optional_extra_prices_are_empty_by_default_and_caller_supplied():
    plain = pricing.Pricer(lite={})
    assert plain.rates("custom-model")[0] is None

    injected = pricing.Pricer(lite={}, extra_prices={"custom-model": (3, 15, 0.3, 0, 0)})
    assert injected.rates("custom-model") == ((3, 15, 0.3, 0, 0), "list")
    assert injected.rates("custom-model", "fast")[1] == "standard"


def test_configured_store_is_resolved_lazily_and_home_override_wins(tmp_path, monkeypatch):
    configured = tmp_path / "configured-store"
    override = tmp_path / "override-store"
    config = tmp_path / "config.toml"
    config.write_text("store = '%s'\n" % configured)
    monkeypatch.setenv("GLIDESLOPE_CONFIG", str(config))
    monkeypatch.setenv("GLIDESLOPE_HOME", str(override))
    assert pricing._paths()["root"] == override / "pricing"
    assert not configured.exists()

    monkeypatch.delenv("GLIDESLOPE_HOME")
    assert pricing._paths()["root"] == configured / "pricing"
    assert not configured.exists()


def test_import_does_not_read_store_config_or_create_store(tmp_path, monkeypatch):
    store = tmp_path / "unused-store"
    monkeypatch.setenv("GLIDESLOPE_HOME", str(store))
    monkeypatch.setenv("GLIDESLOPE_CONFIG", str(tmp_path / "missing.toml"))

    def fail_read(*_args, **_kwargs):
        raise AssertionError("module import attempted a file read")

    module_path = Path(__file__).resolve().parents[1] / "pricing.py"
    spec = importlib.util.spec_from_file_location("pricing_import_probe", module_path)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", fail_read)
        patch.setattr(Path, "open", fail_read)
        patch.setattr(urllib.request, "urlopen", fail_read)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    assert not store.exists()


def test_speed_ledger_merges_runs_and_restores_logged_tiers(tmp_path):
    path = tmp_path / "speed-ledger.json"
    ledger = pricing.SpeedLedger(path)
    for index, tier in enumerate(("priority", "priority", "ultrafast")):
        ledger.apply({
            "tool": "codex", "key": "session|%d" % index, "machine": "machine-a",
            "tier": tier, "speed": pricing.CODEX_SPEED[tier], "speed_src": "log",
        })
    ledger.save()
    assert json.loads(path.read_text()) == {
        "machine-a|session": [[0, 1, "priority", "log"], [2, 2, "ultrafast", "log"]]
    }

    later = pricing.SpeedLedger(path)
    rows = [{
        "tool": "codex", "key": "session|%d" % index, "machine": "machine-a",
        "tier": "default", "speed": "standard", "speed_src": "rollout",
    } for index in range(3)]
    for row in rows:
        later.apply(row)
    assert [(row["speed"], row["speed_src"]) for row in rows] == [
        ("fast", "log"), ("fast", "log"), ("ultrafast", "log")
    ]


def test_refresh_skips_network_for_a_fresh_table(tmp_path, monkeypatch):
    monkeypatch.setenv("GLIDESLOPE_HOME", str(tmp_path))
    paths = pricing._paths()
    paths["root"].mkdir(parents=True)
    paths["prices"].write_text(json.dumps({"gpt-5": PLAIN}))

    def fail_network(*_args, **_kwargs):
        raise AssertionError("fresh table should not request the network")

    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    assert pricing.refresh_prices() == "fresh"


def test_refresh_appends_rate_changes_and_metadata_under_the_store_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("GLIDESLOPE_HOME", str(tmp_path))
    paths = pricing._paths()
    paths["root"].mkdir(parents=True)
    old = {"gpt-5": PLAIN}
    new = {"gpt-5": {**PLAIN, "input_cost_per_token": 2e-6}}
    for index in range(1000):
        model = "gpt-test-%d" % index
        old[model] = PLAIN
        new[model] = PLAIN
    paths["prices"].write_text(json.dumps(old))

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(new).encode()))
    assert pricing.refresh_prices(force=True) == "updated"
    change = json.loads(paths["changes"].read_text().splitlines()[0])
    assert change["model"] == "gpt-5" and change["change"] == "changed"
    assert json.loads(paths["meta"].read_text())["models"] == len(new)
    assert paths["root"].joinpath("prices.lock").exists()


def test_prices_cli_reports_age_and_rates_without_refreshing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GLIDESLOPE_HOME", str(tmp_path))
    paths = pricing._paths()
    paths["root"].mkdir(parents=True)
    paths["prices"].write_text(json.dumps({"gpt-6-astra": ASTRA}))

    assert pricing.main(["prices"]) == 0
    output = capsys.readouterr().out
    assert "LiteLLM table:" in output
    assert "gpt-6-astra" in output
    assert "fast" in output and "list" in output
