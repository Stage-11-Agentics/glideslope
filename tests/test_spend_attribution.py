"""Who paid for a Claude request: the transcript, then the token, then the login."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import claude_account
import glideslope
import pricing
import spend
import spend_collect as collector
import turn_trigger
from pricing import SpeedLedger

BEACON_PATH = Path(__file__).parents[1] / "tools" / "satellite_beacon.py"
SPEC = importlib.util.spec_from_file_location("glideslope_beacon_tokens", BEACON_PATH)
beacon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(beacon)

ORG_WORK = "11111111-1111-4111-8111-111111111111"
ORG_PERSONAL = "22222222-2222-4222-8222-222222222222"
FAKE_TOKEN = "not-a-real-token-value"


def _assistant(message_id: str, when: str, out: int = 10) -> dict:
    return {"type": "assistant", "timestamp": when, "requestId": "req-" + message_id,
            "message": {"id": message_id, "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 100, "output_tokens": out}}}


def _org(org: str, when: str) -> dict:
    return {"type": "attachment", "timestamp": when,
            "attachment": {"type": "credential_org", "organizationUuid": org}}


def _user(text: str, when: str, **extra) -> dict:
    return {"type": "user", "timestamp": when, "message": {"role": "user", "content": text}, **extra}


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _collect(tmp_path: Path, home: Path) -> dict:
    result = collector.collect(home=home, machine="laptop", cache_dir=tmp_path / "cache",
                               speed_ledger=SpeedLedger(tmp_path / "ledger.json"))
    return {record["key"].split("|")[0]: record for record in result.records}


# ------------------------------------------------------------------ transcript evidence

def test_each_request_takes_the_latest_org_at_or_before_it(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "p" / "sess-1.jsonl", [
        _assistant("before", "2026-10-01T09:00:00Z"),
        _org(ORG_WORK.upper(), "2026-10-01T10:00:00Z"),
        _assistant("first", "2026-10-01T10:01:00Z"),
        _org(ORG_PERSONAL, "2026-10-01T11:00:00Z"),
        _assistant("second", "2026-10-01T11:01:00Z"),
    ])
    records = _collect(tmp_path, home)
    assert records["before"]["org"] is None
    assert records["first"]["org"] == ORG_WORK
    assert records["second"]["org"] == ORG_PERSONAL
    assert {record["sid"] for record in records.values()} == {"sess-1"}


def test_a_subagent_inherits_its_parent_at_the_request_time(tmp_path):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "p"
    _write(projects / "sess-1.jsonl", [
        _org(ORG_WORK, "2026-10-01T10:00:00Z"),
        _assistant("parent", "2026-10-01T10:01:00Z"),
        _org(ORG_PERSONAL, "2026-10-01T12:00:00Z"),
    ])
    _write(projects / "sess-1" / "subagents" / "agent-a1.jsonl", [
        _assistant("early", "2026-10-01T11:00:00Z"),
        _assistant("late", "2026-10-01T13:00:00Z"),
    ])
    records = _collect(tmp_path, home)
    assert records["early"]["org"] == ORG_WORK
    assert records["late"]["org"] == ORG_PERSONAL
    assert records["early"]["sid"] == "sess-1"
    assert records["early"]["trigger"] == turn_trigger.SUBAGENT


def test_a_resumed_copy_keeps_the_original_org_and_trigger(tmp_path):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "p"
    _write(projects / "new.jsonl", [_assistant("shared", "2026-10-01T10:00:05Z", out=20)])
    _write(projects / "old.jsonl", [
        _org(ORG_WORK, "2026-10-01T09:00:00Z"),
        _user("hello", "2026-10-01T09:59:00Z"),
        _assistant("shared", "2026-10-01T10:00:00Z"),
    ])
    records = _collect(tmp_path, home)
    assert records["shared"]["org"] == ORG_WORK
    assert records["shared"]["sid"] == "old"
    assert records["shared"]["trigger"] == turn_trigger.TYPED
    assert records["shared"]["out"] == 20


# ------------------------------------------------------------------ crediting

def _roster(tmp_path: Path, monkeypatch, entries: dict | None = None) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(entries or {
        "work": {"email": "work@example.test", "org_uuid": ORG_WORK},
        "personal": {"email": "personal@example.test", "org_uuid": ORG_PERSONAL},
    }), encoding="utf-8")
    monkeypatch.setattr(glideslope, "CLAUDE_ROSTER", roster)


MS = int(dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc).timestamp() * 1000)
TIMELINE = ([MS - 1000], ["Timeline"])


def test_the_transcript_beats_the_token_and_the_token_beats_the_login(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch)
    orgs = spend._org_names()
    evidence = {"sess": [(MS - 60_000, MS + 60_000, ORG_WORK)]}
    work, personal = orgs[ORG_WORK], orgs[ORG_PERSONAL]
    assert spend._credit({"org": ORG_PERSONAL, "sid": "sess"}, MS, orgs, evidence, TIMELINE) == (personal, "transcript")
    assert spend._credit({"org": None, "sid": "sess"}, MS, orgs, evidence, TIMELINE) == (work, "token")
    assert spend._credit({"org": None, "sid": "other"}, MS, orgs, evidence, TIMELINE) == ("Timeline", "timeline")
    assert spend._credit({"org": None, "sid": "other"}, MS, orgs, {}, None) == ("unattributed", "none")


def test_token_evidence_binds_only_inside_its_span(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch)
    orgs = spend._org_names()
    evidence = {"sess": [(MS + 1, MS + 60_000, ORG_WORK)]}
    assert spend._credit({"org": None, "sid": "sess"}, MS, orgs, evidence, TIMELINE) == ("Timeline", "timeline")


def test_an_org_two_seats_share_names_no_account_and_never_the_login(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch, {
        "one": {"email": "one@example.test", "org_uuid": ORG_WORK},
        "two": {"email": "two@example.test", "org_uuid": ORG_WORK},
    })
    assert spend._org_names() == {}
    evidence = {"s": [(MS - 1, MS + 1, ORG_PERSONAL)]}
    assert spend._credit({"org": ORG_WORK, "sid": "s"}, MS, {}, evidence, TIMELINE) == (
        "unattributed", "unowned org")


def test_an_org_the_roster_lacks_is_unowned(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch)
    record = {"org": "33333333-3333-4333-8333-333333333333", "sid": "s"}
    assert spend._credit(record, MS, spend._org_names(), {}, TIMELINE) == ("unattributed", "unowned org")


def test_a_late_named_token_credits_sessions_seen_before_it_was_named(tmp_path):
    path = tmp_path / "session-tokens.json"
    path.write_text(json.dumps({
        "schema": 1, "tokens": {"abc": ORG_WORK.upper(), "zzz": ORG_PERSONAL},
        "sessions": {"sess": [{"fp": "abc", "start": "2026-10-01T10:00:00Z",
                               "first_seen": "2026-10-01T11:00:00Z", "last_seen": "2026-10-01T12:00:00Z"}],
                     "unnamed": [{"fp": "nobody", "start": "2026-10-01T10:00:00Z",
                                  "first_seen": "2026-10-01T11:00:00Z", "last_seen": "2026-10-01T12:00:00Z"}]},
    }), encoding="utf-8")
    evidence = spend._token_evidence(path)
    assert list(evidence) == ["sess"]
    assert evidence["sess"][0][2] == ORG_WORK
    assert evidence["sess"][0][0] == spend._iso_ms("2026-10-01T10:00:00Z")
    assert evidence["sess"][0][1] == spend._iso_ms("2026-10-01T12:00:00Z") + spend.TOKEN_END_GRACE_MS


@pytest.mark.parametrize("body", ["", "{", "[]", '{"tokens": 1, "sessions": 2}', '{"sessions": {}}'])
def test_unusable_token_evidence_is_no_evidence(tmp_path, body):
    path = tmp_path / "session-tokens.json"
    path.write_text(body, encoding="utf-8")
    assert spend._token_evidence(path) == {}


def test_build_counts_each_basis_and_each_trigger(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch)
    now = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
    stamp = int((now - dt.timedelta(hours=2)).timestamp() * 1000)
    def request(key, org, sid, trigger, turn):
        return {**{field: None for field in collector.RECORD_FIELDS},
                "tool": "claude", "model": "claude-opus-5-5", "ts": stamp, "in": 1000, "out": 100,
                "cr": 0, "cw5": 0, "cw1": 0, "think": 0, "prompt": 1000, "key": key, "machine": "laptop",
                "org": org, "sid": sid, "trigger": trigger, "turn": turn}
    db = tmp_path / "samples.db"
    (tmp_path / spend.TOKEN_EVIDENCE_NAME).write_text(json.dumps({
        "tokens": {"abc": ORG_PERSONAL},
        "sessions": {"seat": [{"fp": "abc", "start": "2026-10-01T00:00:00Z",
                               "last_seen": "2026-10-02T00:00:00Z"}]},
    }), encoding="utf-8")
    monkeypatch.setattr(spend, "_fast_mode_armed", lambda: {})
    monkeypatch.setattr(pricing, "prices_age_h", lambda: 1.5)
    lite = {"claude-opus-5-5": {"input_cost_per_token": 5e-6, "output_cost_per_token": 25e-6}}
    collection = {"records": [
        request("a", ORG_WORK, "login", turn_trigger.TYPED, 1),
        request("b", ORG_WORK, "login", turn_trigger.TYPED, 0),
        request("c", None, "seat", turn_trigger.SHELL, 1),
        request("d", None, "elsewhere", turn_trigger.SHELL, 1),
    ], "collected_at": {}}
    data = spend.build(collection, now=now, db=db, pricer=pricing.Pricer(lite=lite))
    basis = data["attribution_basis"]["d1"]
    assert basis["transcript"]["requests"] == 2
    assert basis["token"]["requests"] == 1
    assert basis["none"]["requests"] == 1
    accounts = {account["name"]: account for account in data["accounts"]}
    work, personal = spend._org_names()[ORG_WORK], spend._org_names()[ORG_PERSONAL]
    assert accounts[work]["d1"]["requests"] == 2
    assert accounts[personal]["d1"]["requests"] == 1
    rows = {row["trigger"]: row for row in data["triggers"]["d1"]}
    assert rows[turn_trigger.TYPED]["turns"] == 1
    assert rows[turn_trigger.TYPED]["requests"] == 2
    assert rows[turn_trigger.SHELL]["turns"] == 2
    assert [row["session"] for row in rows[turn_trigger.SHELL]["top_sessions"]] != []
    assert "What woke the agent" in spend.summary(data)


# ------------------------------------------------------------------ the beacon

def _ps(stdout: str):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _process(argv: str, environment: str):
    """`ps` for one process: plain argv first, then argv followed by its environment."""
    return mock.patch.object(beacon.subprocess, "run", side_effect=[
        _ps(argv + "\n"), _ps(argv + " " + environment + "\n")])


def test_the_fingerprint_matches_claude_accounts_and_the_value_goes_nowhere(tmp_path, capsys):
    with _process("/bin/claude --model opus", f"TERM=xterm CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN} HOME=/x"):
        fingerprint = beacon.process_token_fingerprint(42)
    assert fingerprint == claude_account.token_fingerprint(FAKE_TOKEN)

    orgs = tmp_path / "orgs.json"
    orgs.write_text(json.dumps({fingerprint: ORG_WORK}), encoding="utf-8")
    evidence = tmp_path / "session-tokens.json"
    counts, unnamed = beacon.record_token_sessions(
        [{"session": "sess", "fp": fingerprint, "start": "2026-10-01T10:00:00Z"}],
        "2026-10-01T10:05:00Z", path=evidence, orgs_path=orgs)
    assert counts == {fingerprint: 1} and unnamed == []
    assert FAKE_TOKEN not in evidence.read_text()
    assert FAKE_TOKEN not in capsys.readouterr().out
    assert evidence.stat().st_mode & 0o777 == 0o600


def test_a_prompt_that_quotes_the_variable_is_argv_not_a_token():
    argv = "/bin/claude say CLAUDE_CODE_OAUTH_TOKEN=quoted-in-a-prompt\nsecond line"
    with _process(argv, "TERM=xterm HOME=/x"):
        assert beacon.process_token_fingerprint(42) is None
    with _process(argv, f"HOME=/x CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN}"):
        assert beacon.process_token_fingerprint(42) == claude_account.token_fingerprint(FAKE_TOKEN)


def test_an_empty_token_is_no_token():
    with _process("/bin/claude", "CLAUDE_CODE_OAUTH_TOKEN= HOME=/x"):
        assert beacon.process_token_fingerprint(42) is None
    with _process("/bin/claude", "HOME=/x CLAUDE_CODE_OAUTH_TOKEN="):
        assert beacon.process_token_fingerprint(42) is None


def test_argv_that_changed_between_reads_is_no_sighting():
    with mock.patch.object(beacon.subprocess, "run", side_effect=[
            _ps("/bin/claude one\n"), _ps(f"/bin/claude two CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN}\n")]):
        assert beacon.process_token_fingerprint(42) is None


def test_a_ps_failure_is_no_sighting():
    with mock.patch.object(beacon.subprocess, "run", side_effect=OSError("no ps")):
        assert beacon.process_token_fingerprint(42) is None


def test_spans_extend_and_a_new_process_opens_a_new_one(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    orgs = tmp_path / "orgs.json"
    sighting = {"session": "sess", "fp": "abc", "start": "2026-10-01T10:00:00Z"}
    beacon.record_token_sessions([sighting], "2026-10-01T10:05:00Z", path=evidence, orgs_path=orgs)
    beacon.record_token_sessions([sighting], "2026-10-01T10:10:00Z", path=evidence, orgs_path=orgs)
    beacon.record_token_sessions([{**sighting, "start": "2026-10-01T11:00:00Z"}],
                                 "2026-10-01T11:05:00Z", path=evidence, orgs_path=orgs)
    beacon.record_token_sessions([], "2026-10-01T12:00:00Z", path=evidence, orgs_path=orgs)
    spans = json.loads(evidence.read_text())["sessions"]["sess"]
    assert [(span["start"], span["last_seen"]) for span in spans] == [
        ("2026-10-01T10:00:00Z", "2026-10-01T10:10:00Z"),
        ("2026-10-01T11:00:00Z", "2026-10-01T11:05:00Z"),
    ]


def test_a_learned_name_survives_a_map_that_lost_it(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    orgs = tmp_path / "orgs.json"
    orgs.write_text(json.dumps({"abc": ORG_WORK}), encoding="utf-8")
    sighting = {"session": "sess", "fp": "abc", "start": "2026-10-01T10:00:00Z"}
    beacon.record_token_sessions([sighting], "2026-10-01T10:05:00Z", path=evidence, orgs_path=orgs)
    orgs.write_text("{}", encoding="utf-8")
    counts, unnamed = beacon.record_token_sessions([sighting], "2026-10-01T10:10:00Z",
                                                   path=evidence, orgs_path=orgs)
    assert unnamed == []
    assert json.loads(evidence.read_text())["tokens"] == {"abc": ORG_WORK}


def test_an_unnamed_fingerprint_is_reported_and_credits_nothing(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    counts, unnamed = beacon.record_token_sessions(
        [{"session": "sess", "fp": "abc", "start": None}], "2026-10-01T10:05:00Z",
        path=evidence, orgs_path=tmp_path / "missing.json")
    assert counts == {"abc": 1} and unnamed == ["abc"]
    assert json.loads(evidence.read_text())["tokens"] == {}
    assert spend._token_evidence(evidence) == {}


def test_a_damaged_evidence_file_is_kept_and_the_tick_is_lost(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    evidence.write_text("{damaged", encoding="utf-8")
    with pytest.raises(ValueError):
        beacon.record_token_sessions([], "2026-10-01T10:05:00Z", path=evidence,
                                     orgs_path=tmp_path / "missing.json")
    assert evidence.read_text() == "{damaged"


def test_a_subagent_inherits_from_a_parent_that_made_no_request(tmp_path):
    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "p"
    _write(projects / "sess-1.jsonl", [_org(ORG_WORK, "2026-10-01T10:00:00Z")])
    _write(projects / "sess-1" / "subagents" / "agent-a1.jsonl", [_assistant("child", "2026-10-01T11:00:00Z")])
    assert _collect(tmp_path, home)["child"]["org"] == ORG_WORK


def test_sightings_read_the_session_files(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "7.json").write_text(json.dumps(
        {"pid": 7, "sessionId": "sess", "startedAt": 1790000000000}), encoding="utf-8")
    (sessions / "8.json").write_text("not json", encoding="utf-8")
    (sessions / "9.json").write_text(json.dumps({"pid": 9, "sessionId": "login"}), encoding="utf-8")
    monkeypatch.setattr(beacon, "SESSION_DIRS", (sessions,))
    monkeypatch.setattr(beacon, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(beacon, "process_token_fingerprint", lambda pid: "abc" if pid == 7 else None)
    assert beacon.token_sightings() == [
        {"session": "sess", "fp": "abc", "pid": 7, "start": "2026-09-21T14:13:20Z"}]


def test_a_session_with_no_start_keeps_one_span_per_process(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    orgs = tmp_path / "orgs.json"
    sighting = {"session": "sess", "fp": "abc", "pid": 7, "start": None}
    for minute in ("05", "10", "15"):
        beacon.record_token_sessions([sighting], "2026-10-01T10:%s:00Z" % minute, path=evidence, orgs_path=orgs)
    beacon.record_token_sessions([{**sighting, "pid": 8}], "2026-10-01T10:20:00Z", path=evidence, orgs_path=orgs)
    spans = json.loads(evidence.read_text())["sessions"]["sess"]
    assert [(span["process"], span["first_seen"], span["last_seen"]) for span in spans] == [
        ("pid:7", "2026-10-01T10:05:00Z", "2026-10-01T10:15:00Z"),
        ("pid:8", "2026-10-01T10:20:00Z", "2026-10-01T10:20:00Z"),
    ]


def test_spans_the_old_beacon_split_per_tick_are_merged_and_others_kept(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    def tick(minute):
        stamp = "2026-10-01T10:%s:00Z" % minute
        return {"fp": "abc", "start": stamp, "first_seen": stamp, "last_seen": stamp}
    known = {"fp": "abc", "start": "2026-10-01T09:00:00Z", "first_seen": "2026-10-01T09:05:00Z",
             "last_seen": "2026-10-01T09:30:00Z"}
    evidence.write_text(json.dumps({"schema": 1, "tokens": {}, "sessions": {
        "split": [tick("05"), tick("10"), tick("15")], "kept": [known]}}), encoding="utf-8")
    beacon.record_token_sessions([], "2026-10-01T11:00:00Z", path=evidence, orgs_path=tmp_path / "none.json")
    sessions = json.loads(evidence.read_text())["sessions"]
    assert [(s["first_seen"], s["last_seen"]) for s in sessions["split"]] == [
        ("2026-10-01T10:05:00Z", "2026-10-01T10:15:00Z")]
    assert sessions["kept"] == [known]


def test_an_old_span_keyed_by_start_is_extended_not_split(tmp_path):
    evidence = tmp_path / "session-tokens.json"
    evidence.write_text(json.dumps({"schema": 1, "tokens": {}, "sessions": {"sess": [
        {"fp": "abc", "start": "2026-10-01T09:00:00Z", "first_seen": "2026-10-01T09:05:00Z",
         "last_seen": "2026-10-01T09:05:00Z"}]}}), encoding="utf-8")
    beacon.record_token_sessions([{"session": "sess", "fp": "abc", "pid": 7, "start": "2026-10-01T09:00:00Z"}],
                                 "2026-10-01T09:10:00Z", path=evidence, orgs_path=tmp_path / "none.json")
    spans = json.loads(evidence.read_text())["sessions"]["sess"]
    assert len(spans) == 1 and spans[0]["last_seen"] == "2026-10-01T09:10:00Z"


def test_whose_remembers_the_fingerprint(tmp_path, monkeypatch):
    orgs = tmp_path / ".meter-orgs.json"
    monkeypatch.setattr(claude_account, "METER_ORGS", orgs)
    claude_account.remember_token_org("abc", ORG_WORK)
    claude_account.remember_token_org("def", ORG_PERSONAL)
    assert json.loads(orgs.read_text()) == {"abc": ORG_WORK, "def": ORG_PERSONAL}


# ------------------------------------------------------------------ what woke the agent

def _queued(prompt: str, kind: str) -> dict:
    return {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": prompt, "origin": {"kind": kind}}}


NOTE = "<task-notification>\n<task-id>x</task-id>\n<status>completed</status>\n<summary>{}</summary>"


@pytest.mark.parametrize(("row", "label"), [
    (_user("please look at this", "t", origin={"kind": "human"}), turn_trigger.TYPED),
    (_user(NOTE.format('Background command "tests" finished'), "t"), turn_trigger.SHELL),
    (_user(NOTE.format('Agent "review" finished'), "t"), turn_trigger.AGENT),
    (_user(NOTE.format('Background agent "review" stopped'), "t"), turn_trigger.AGENT),
    (_user(NOTE.format('Monitor event: "logs"') + "<event>x</event>", "t"), turn_trigger.MONITOR),
    (_user("summary of before", "t", isCompactSummary=True), turn_trigger.COMPACTION),
    (_user("This session is being continued from a previous one", "t"), turn_trigger.COMPACTION),
    (_user("<command-name>/model</command-name>", "t"), turn_trigger.SLASH),
    (_user("Stop hook feedback: keep going", "t"), turn_trigger.STOP_HOOK),
    (_user("<<autonomous-loop-dynamic>>", "t"), turn_trigger.SCHEDULED),
    (_user("run", "t", origin={"kind": "scheduled"}), turn_trigger.SCHEDULED),
    (_user("watch this machine", "t", turnOrigin="sdk", promptSource="sdk"), turn_trigger.SCRIPTED),
    (_user("Read the file at /tmp/runtime/launch-prompts/a/b.txt and follow it.", "t"), turn_trigger.C11_LAUNCH),
])
def test_row_labels(row, label):
    assert turn_trigger.row_trigger(row)[0] == label


@pytest.mark.parametrize("row", [
    {"type": "assistant", "message": {}},
    {"type": "attachment", "attachment": {"type": "hook_success"}},
    _user("meta", "t", isMeta=True),
    _user("side", "t", isSidechain=True),
    _user("[Request interrupted by user]", "t"),
    _queued(NOTE.format('Background command "x" failed'), "task-notification"),
    _queued("c11 mail waiting: run c11 mailbox recv --ack", "peer"),
    _queued("typed while the agent worked", "human"),
    {"type": "user", "message": "not a mapping"},
    {"type": "user", "message": {"content": 7}, "origin": "not a mapping"},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
])
def test_rows_that_open_no_turn(row):
    assert turn_trigger.row_trigger(row) is None


def test_wakes_from_real_shaped_rows(tmp_path):
    """Rows shaped from Claude Code 2.1.31x transcripts (values are fake): idle peer messages and
    scheduled wakeups are meta rows that open turns; queued arrivals and system inserts do not."""
    home = tmp_path / "home"
    target = home / ".claude" / "projects" / "p" / "00000000-0000-4000-8000-00000000aaaa.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text((Path(__file__).parent / "fixtures" / "claude-wakes.jsonl").read_text())
    records = {key.split("_", 1)[1]: record for key, record in _collect(tmp_path, home).items()}
    assert {name: (record["trigger"], record["turn"]) for name, record in records.items()} == {
        "typed_1": (turn_trigger.TYPED, 1),
        "typed_2": (turn_trigger.TYPED, 0),
        "doorbell": (turn_trigger.DOORBELL, 1),
        "peer": (turn_trigger.PEER, 1),
        "scheduled": (turn_trigger.SCHEDULED, 1),
        "shell": (turn_trigger.SHELL, 1),
        "shell_2": (turn_trigger.SHELL, 0),
    }


def test_requests_carry_their_turn_and_only_the_first_counts_it(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "p" / "sess.jsonl", [
        _assistant("opening", "2026-10-01T09:00:00Z"),
        _user("hello", "2026-10-01T10:00:00Z"),
        _assistant("one", "2026-10-01T10:00:01Z"),
        _assistant("two", "2026-10-01T10:00:02Z"),
        _user(NOTE.format('Background command "x" finished'), "2026-10-01T10:01:00Z"),
        _assistant("three", "2026-10-01T10:01:01Z"),
    ])
    records = _collect(tmp_path, home)
    assert records["opening"]["trigger"] == turn_trigger.SESSION_START
    assert (records["one"]["trigger"], records["one"]["turn"]) == (turn_trigger.TYPED, 1)
    assert (records["two"]["trigger"], records["two"]["turn"]) == (turn_trigger.TYPED, 0)
    assert (records["three"]["trigger"], records["three"]["turn"]) == (turn_trigger.SHELL, 1)


def _tool_use(message_id: str, when: str) -> dict:
    row = _assistant(message_id, when)
    row["message"]["stop_reason"] = "tool_use"
    row["message"]["content"] = [{"type": "tool_use", "id": "tool-" + message_id, "name": "Bash"}]
    return row


def _tool_result(tool_id: str, when: str) -> dict:
    return {"type": "user", "timestamp": when, "toolUseResult": {},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def test_a_notification_that_lands_mid_turn_wakes_nothing(tmp_path):
    """A background shell finishing while the agent works is folded into the running turn."""
    home = tmp_path / "home"
    queued = _queued(NOTE.format('Background command "tests" finished'), "task-notification")
    queued["timestamp"] = "2026-10-01T10:00:03Z"
    _write(home / ".claude" / "projects" / "p" / "sess.jsonl", [
        _user("fix the build", "2026-10-01T10:00:00Z", origin={"kind": "human"}),
        _tool_use("one", "2026-10-01T10:00:01Z"),
        _tool_result("tool-one", "2026-10-01T10:00:02Z"),
        queued,
        _tool_use("two", "2026-10-01T10:00:04Z"),
        _tool_result("tool-two", "2026-10-01T10:00:05Z"),
        _assistant("three", "2026-10-01T10:00:06Z"),
        _user(NOTE.format('Background command "tests" finished'), "2026-10-01T10:05:00Z",
              origin={"kind": "task-notification"}),
        _assistant("woken", "2026-10-01T10:05:01Z"),
    ])
    records = _collect(tmp_path, home)
    assert [(records[k]["trigger"], records[k]["turn"]) for k in ("one", "two", "three")] == [
        (turn_trigger.TYPED, 1), (turn_trigger.TYPED, 0), (turn_trigger.TYPED, 0)]
    assert (records["woken"]["trigger"], records["woken"]["turn"]) == (turn_trigger.SHELL, 1)


def test_a_malformed_row_is_skipped_and_the_rest_collected(tmp_path, capsys):
    home = tmp_path / "home"
    bad = _assistant("bad", "2026-10-01T10:00:00Z")
    bad["message"]["usage"]["cache_creation"] = ["not", "a", "mapping"]
    odd_org = {"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid": ["x"]}}
    _write(home / ".claude" / "projects" / "p" / "sess.jsonl", [
        bad, odd_org, ["a", "list"], _assistant("good", "2026-10-01T10:00:01Z")])
    records = _collect(tmp_path, home)
    assert list(records) == ["good"]
    assert "skipped 1 malformed transcript row" in capsys.readouterr().err


@pytest.mark.parametrize(("version", "expected"), [
    ("2.1.281", ("unattributed", "unseen token")),
    ("2.1.317", ("unattributed", "unseen token")),
    ("2.2.0-beta", ("unattributed", "unseen token")),
    ("2.1.280", ("Timeline", "timeline")),
    (None, ("Timeline", "timeline")),
    ("garbage", ("Timeline", "timeline")),
])
def test_no_org_from_a_version_that_always_records_one_is_an_unseen_token(tmp_path, monkeypatch,
                                                                           version, expected):
    _roster(tmp_path, monkeypatch)
    record = {"org": None, "sid": "s", "version": version}
    assert spend._credit(record, MS, spend._org_names(), {}, TIMELINE) == expected


def test_a_token_sighting_beats_the_unseen_token_rule(tmp_path, monkeypatch):
    _roster(tmp_path, monkeypatch)
    orgs = spend._org_names()
    record = {"org": None, "sid": "s", "version": "2.1.317"}
    evidence = {"s": [(MS - 1, MS + 1, ORG_WORK)]}
    assert spend._credit(record, MS, orgs, evidence, TIMELINE) == (orgs[ORG_WORK], "token")


def test_a_typed_prompt_c11_sent_is_labelled_a_c11_send(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude" / "projects" / "p" / "sess.jsonl", [
        _user('<pasted_content id="a">\nmulti\nline\n</pasted_content>', "2026-10-01T10:00:00Z"),
        _assistant("sent", "2026-10-01T10:00:01Z"),
        _assistant("sent-too", "2026-10-01T10:00:02Z"),
        _user("typed by a person", "2026-10-01T10:01:00Z"),
        _assistant("typed", "2026-10-01T10:01:01Z"),
    ])
    events = home / collector.C11_EVENTS
    events.mkdir(parents=True)
    (events / "events-a.ndjson").write_text(json.dumps(
        {"type": "panel.input_sent", "payload": {"kind": "text", "text": "multi\nline\n"}}) + "\n",
        encoding="utf-8")
    records = _collect(tmp_path, home)
    assert records["sent"]["trigger"] == turn_trigger.C11_SEND
    assert (records["sent-too"]["trigger"], records["sent-too"]["turn"]) == (turn_trigger.C11_SEND, 0)
    assert records["typed"]["trigger"] == turn_trigger.TYPED
