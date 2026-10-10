"""Remaining-usage estimate: budget learned from cap events, counted per window.

The upstream caps each account's usage of a model inside a 24h window and names
the reset clock on the 429 (code 6004). The budget itself is never told to the
gateway, so this feature infers it from accounts that actually hit the cap: the
usage of that model in the 24h ending at the reset is one sample, and the
per-realm average of those samples is the estimated budget. Remaining usage for
an account is then the budget minus what it burned since its own window started
(the reset clock it was last handed).

Pinned here, with the usage log synthesised in a temp directory and `now`
injected so nothing depends on the wall clock:

  - the sample is the 24h before the reset, successful rows only, and the 429
    rows themselves never count as usage;
  - an account that capped repeatedly weighs once (per-account mean first, then
    across accounts) and repeated 429 rows for one reset collapse to one event;
  - both the intl and the cn reset wordings produce cap events;
  - the window usage counts from the last reset; a pair still cooling reports
    remaining 0 with the reset clock that hands the quota back;
  - a pair with no cap event falls back to the trailing 24h (conservative) and
    is flagged estimated;
  - the fold resumes incrementally from its byte offset.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-remaining-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts
import wb_proxy as P

MODEL = "deepseek-v4.1-flash"
HOUR = 3600


def reset_epoch(stamp):
    """The epoch parse_rate_limit_reset() reads out of an intl 6004 body.

    Built through the parser itself so the expectation never depends on the
    host timezone: the body names a wall clock, and the parser is the one
    thing that decides what that clock means on this machine.
    """
    detail = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
              'worry, your usage will reset at %s UTC+8, alternatively, you can '
              'switch to the other models to continue using it.","requestId":"x"}'
              % stamp)
    parsed = P.parse_rate_limit_reset(detail)
    assert parsed is not None, "test body must parse"
    return parsed, detail


def usage_row(at, account, tokens, model=MODEL, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "stream": True, "outcome": "completed",
        "total_tokens": tokens, "account": account, "realm": realm,
    }


def cap_row(at, account, detail, model=MODEL, realm="intl"):
    return {
        "at": at, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)),
        "model": model, "error": True, "outcome": "failed", "status": 429,
        "message": detail, "account": account, "realm": realm,
    }


def write_log(rows):
    with io.open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def clear_events():
    try:
        os.unlink(P.LIMIT_EVENTS_FILE)
    except OSError:
        pass


def restart_fold():
    """Drop the in-memory fold the way a process restart would."""
    with P._remaining_state_lock:
        P._remaining_state.clear()
        P._remaining_state.update({
            "events": [], "keys": set(), "usage": {},
            "log": {"offset": 0, "key": None, "tail": b""},
            "file": {"offset": 0, "key": None, "tail": b""},
        })
    with P._remaining_cache_lock:
        P._remaining_cache.update({"at": 0.0, "built_at": 0.0, "data": None})


def account(uid, nickname="", realm="intl"):
    return wb_accounts.Account({"uid": uid, "nickname": nickname,
                                "accessToken": "t", "realm": realm})


def payload(accounts, now):
    return P._remaining_payload(now=now, accounts=accounts)


def find_row(data, uid, model=MODEL):
    for row in data["rows"]:
        if row["uid"] == uid and row["model"] == model:
            return row
    return None


class RemainingUsageTests(unittest.TestCase):
    def setUp(self):
        clear_events()
        restart_fold()
    def test_sample_is_the_24h_before_the_reset(self):
        reset, detail = reset_epoch("2026-10-10 15:18:17")
        write_log([
            # outside the window: must not count
            usage_row(reset - 25 * HOUR, "acct-A", 111111),
            # inside the window: counted
            usage_row(reset - 23 * HOUR, "acct-A", 1000),
            usage_row(reset - 2 * HOUR, "acct-A", 2000),
            # failed rows never count
            dict(usage_row(reset - HOUR, "acct-A", 999999),
                 error=True, outcome="failed", status=500),
            # the 429 that reports the cap
            cap_row(reset - HOUR + 60, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        self.assertEqual(len(data["budgets"]), 1)
        budget = data["budgets"][0]
        self.assertEqual(budget["model"], MODEL)
        self.assertEqual(budget["realm"], "intl")
        self.assertEqual(budget["avg"], 3000)
        self.assertEqual(budget["n"], 1)

    def test_budget_averages_accounts_not_events(self):
        r1, d1 = reset_epoch("2026-10-09 10:00:00")
        r2, d2 = reset_epoch("2026-10-10 10:00:00")
        r3, d3 = reset_epoch("2026-10-10 12:00:00")
        write_log([
            # acct-A caps twice: its own samples average 1500
            usage_row(r1 - 2 * HOUR, "acct-A", 1000),
            cap_row(r1 - HOUR, "acct-A", d1),
            usage_row(r2 - 2 * HOUR, "acct-A", 2000),
            cap_row(r2 - HOUR, "acct-A", d2),
            # acct-B caps once: sample 3000
            usage_row(r3 - 2 * HOUR, "acct-B", 3000),
            cap_row(r3 - HOUR, "acct-B", d3),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=r3 + HOUR)
        budget = data["budgets"][0]
        # mean(1500, 3000) — the twice-capped account weighs once.
        self.assertEqual(budget["avg"], 2250)
        self.assertEqual(budget["min"], 1500)
        self.assertEqual(budget["max"], 3000)
        self.assertEqual(budget["n"], 2)

    def test_repeated_429_rows_for_one_reset_collapse(self):
        reset, detail = reset_epoch("2026-10-10 09:00:00")
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 500),
            cap_row(reset - HOUR, "acct-A", detail),
            cap_row(reset - HOUR + 5, "acct-A", detail),
            cap_row(reset - HOUR + 9, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        self.assertEqual(data["budgets"][0]["n"], 1)
        self.assertEqual(data["budgets"][0]["avg"], 500)

    def test_cn_reset_wording_is_a_cap_event(self):
        reset, _ = reset_epoch("2026-10-10 14:44:59")
        detail = ('{"code":6004,"msg":"usage exceeds frequency limit，将在 '
                  '2026-10-10 14:44:59 UTC+8 重置，或切换其他模型继续使用"}')
        write_log([
            usage_row(reset - 3 * HOUR, "acct-C", 700, realm="cn"),
            cap_row(reset - HOUR, "acct-C", detail, realm="cn"),
        ])
        data = payload([account("acct-C", realm="cn")], now=reset + HOUR)
        self.assertEqual(data["budgets"][0]["realm"], "cn")
        self.assertEqual(data["budgets"][0]["avg"], 700)

    def test_remaining_counts_from_the_reset(self):
        reset, detail = reset_epoch("2026-10-10 08:00:00")
        write_log([
            # the spent window that produced the cap: 5000 tokens total
            usage_row(reset - 20 * HOUR, "acct-A", 2000),
            usage_row(reset - 2 * HOUR, "acct-A", 3000),
            cap_row(reset - HOUR, "acct-A", detail),
            # the new window, after the reset: 1200 burned so far
            usage_row(reset + 30 * 60, "acct-A", 700),
            usage_row(reset + 45 * 60, "acct-A", 500),
        ])
        data = payload([account("acct-A")], now=reset + HOUR)
        row = find_row(data, "acct-A")
        self.assertEqual(row["budget"], 5000)
        self.assertEqual(row["used"], 1200)
        self.assertEqual(row["remaining"], 3800)
        self.assertFalse(row["estimated"])
        self.assertFalse(row["cooling"])
        self.assertEqual(row["window_start"], reset)
        self.assertEqual(row["reset_at"], reset)

    def test_cooling_pair_reports_zero_remaining(self):
        reset, detail = reset_epoch("2026-10-10 16:00:00")
        write_log([
            usage_row(reset - 20 * HOUR, "acct-A", 4000),
            usage_row(reset - HOUR, "acct-A", 1000),
            cap_row(reset - 30 * 60, "acct-A", detail),
        ])
        # now is inside the spent window: the reset clock is still ahead
        now = reset - 10 * 60
        data = payload([account("acct-A")], now=now)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["reset_at"], reset)
        self.assertEqual(row["window_start"], reset - P.LIMIT_WINDOW_SECONDS)
        self.assertEqual(row["used"], 5000)

    def test_unknown_window_uses_trailing_24h(self):
        # acct-A provides the budget for the model (5000) through a cap event
        # that happened on a *different* day; acct-B never capped.
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 40 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 5000),
            cap_row(reset - HOUR, "acct-A", detail),
            # acct-B: 900 inside the trailing 24h, 400 outside it
            usage_row(now - 30 * HOUR, "acct-B", 400),
            usage_row(now - 20 * HOUR, "acct-B", 900),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=now)
        row = find_row(data, "acct-B")
        self.assertTrue(row["estimated"])
        self.assertFalse(row["cooling"])
        self.assertEqual(row["window_start"], now - P.LIMIT_WINDOW_SECONDS)
        self.assertEqual(row["used"], 900)
        self.assertEqual(row["budget"], 5000)
        self.assertEqual(row["remaining"], 4100)
        # acct-A's reset is 40h old: that window expired long ago and the
        # current one cannot be anchored either, so it is estimated too - the
        # trailing 24h is a lower bound, never a claim to know the window.
        stale = find_row(data, "acct-A")
        self.assertTrue(stale["estimated"])
        self.assertEqual(stale["window_start"], now - P.LIMIT_WINDOW_SECONDS)
        self.assertEqual(stale["used"], 0)

    def test_budgeted_model_with_zero_usage_is_reported(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        data = payload([account("acct-A"), account("acct-B", nickname="fresh")],
                       now=now)
        row = find_row(data, "acct-B")
        self.assertIsNotNone(row)
        self.assertEqual(row["used"], 0)
        self.assertEqual(row["remaining"], 600)
        self.assertEqual(row["samples"], 1)

    def test_models_without_a_budget_keep_their_usage_row(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
            usage_row(now - HOUR, "acct-B", 42, model="glm-5.3"),
        ])
        data = payload([account("acct-A"), account("acct-B")], now=now)
        row = find_row(data, "acct-B", model="glm-5.3")
        self.assertIsNotNone(row)
        self.assertIsNone(row["budget"])
        self.assertIsNone(row["remaining"])
        self.assertEqual(row["used"], 42)

    def test_realm_budgets_stay_separate(self):
        r1, d1 = reset_epoch("2026-10-09 08:00:00")
        r2, d2 = reset_epoch("2026-10-09 20:00:00")
        write_log([
            usage_row(r1 - 2 * HOUR, "acct-A", 1000),
            cap_row(r1 - HOUR, "acct-A", d1),
            usage_row(r2 - 2 * HOUR, "acct-C", 4000, realm="cn"),
            cap_row(r2 - HOUR, "acct-C", d2, realm="cn"),
        ])
        data = payload([account("acct-A"), account("acct-C", realm="cn")],
                       now=r2 + HOUR)
        by_realm = {b["realm"]: b for b in data["budgets"]}
        self.assertEqual(by_realm["intl"]["avg"], 1000)
        self.assertEqual(by_realm["cn"]["avg"], 4000)

    def test_fold_resumes_incrementally(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        data = payload([account("acct-A")], now=now)
        self.assertEqual(find_row(data, "acct-A")["used"], 0)

        # A row appended after the first pass is folded in without a rescan.
        with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(usage_row(now - HOUR, "acct-A", 123),
                                ensure_ascii=False) + "\n")
        data = payload([account("acct-A")], now=now)
        self.assertEqual(find_row(data, "acct-A")["used"], 123)

    def test_shrunk_log_starts_over(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        self.assertEqual(payload([account("acct-A")], now=now)["budgets"][0]["avg"], 600)

        # The log was replaced by a shorter one (rotation / a copy in place):
        # the cached offset points past its end, so the fold starts over
        # instead of resuming into unrelated bytes.
        write_log([usage_row(reset - 2 * HOUR, "acct-A", 800)])
        data = payload([account("acct-A")], now=now)
        self.assertEqual(data["budgets"], [])
        row = find_row(data, "acct-A")
        self.assertIsNone(row["budget"])
        self.assertEqual(row["used"], 800)

    def test_range_sum_matches_a_naive_sum(self):
        # The window sums are cumulative-sum lookups now; pin them against a
        # plain sum over the same rows, head-trim and compaction included.
        reset, detail = reset_epoch("2026-10-10 08:00:00")
        rows = [usage_row(reset - 25 * HOUR + i * 137, "acct-A", 100 + i * 7)
                for i in range(600)]
        write_log(rows)
        now = reset + HOUR
        payload([account("acct-A")], now=now)

        def naive(lo, hi):
            return sum(r["total_tokens"] for r in rows if lo <= r["at"] <= hi)

        with P._remaining_state_lock:
            buf = P._remaining_state["usage"][("acct-A", MODEL)]
            for lo, hi in ((rows[0]["at"] - 1, now), (reset - 24 * HOUR, now),
                           (now - HOUR, now), (rows[300]["at"], rows[400]["at"]),
                           (rows[-1]["at"] + 1, now), (rows[0]["at"], rows[5]["at"])):
                self.assertEqual(P._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))
            # Move the head forward: entries before it are out of the window
            # by construction, and the sums must agree from there on.
            P._pair_trim(buf, rows[100]["at"])
        self.assertEqual(buf["head"], 100)
        with P._remaining_state_lock:
            for lo, hi in ((rows[100]["at"], now), (rows[150]["at"], now),
                           (now - HOUR, now)):
                self.assertEqual(P._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))
            # Past the batch threshold the dead prefix is compacted away, and
            # the cumulative sums must still subtract correctly.
            P._pair_trim(buf, rows[550]["at"])
        self.assertEqual(buf["head"], 0)
        self.assertEqual(len(buf["at"]), 50)
        with P._remaining_state_lock:
            for lo, hi in ((rows[550]["at"], now), (rows[580]["at"], now),
                           (rows[560]["at"], rows[590]["at"])):
                self.assertEqual(P._range_sum(buf, lo, hi), naive(lo, hi), (lo, hi))

    def test_payload_is_served_from_cache_within_the_ttl(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        now = reset + 20 * HOUR
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])

        class _Pool(object):
            accounts = [account("acct-A")]

        old_pool = P.POOL
        P.POOL = _Pool()
        try:
            first = P.remaining_usage(ttl=30)
            self.assertEqual(find_row(first, "acct-A")["used"], 0)

            # A row appended behind the cache must NOT be picked up inside the
            # TTL (that is the whole point: no rescan per poll)...
            with io.open(P.USAGE_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(usage_row(now - HOUR, "acct-A", 123),
                                    ensure_ascii=False) + "\n")
            cached = P.remaining_usage(ttl=30)
            self.assertIs(cached, first)
            self.assertEqual(find_row(cached, "acct-A")["used"], 0)

            # ...and an expired entry rebuilds and picks it up.
            rebuilt = P.remaining_usage(ttl=0)
            self.assertIsNot(rebuilt, first)
            self.assertEqual(find_row(rebuilt, "acct-A")["used"], 123)
        finally:
            P.POOL = old_pool

    def test_etag_is_stable_until_a_rebuild(self):
        reset, detail = reset_epoch("2026-10-09 08:00:00")
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 600),
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        P.remaining_usage(ttl=30)
        tag = P.remaining_usage_etag()
        self.assertTrue(tag and tag.startswith('"'))
        # Same cache entry -> same validator (the poll gets a 304)…
        P.remaining_usage(ttl=30)
        self.assertEqual(P.remaining_usage_etag(), tag)
        # …and a rebuild mints a new one.
        time.sleep(0.01)
        P.remaining_usage(ttl=0)
        self.assertNotEqual(P.remaining_usage_etag(), tag)

    def test_live_cap_event_is_persisted_and_survives_restart(self):
        # The request path records the cap the moment it is classified: the
        # rows that led to it are in the log but may not be scanned yet.
        reset = time.time() + 30 * 60          # the window is spent, resets later
        write_log([
            usage_row(reset - 25 * HOUR, "acct-A", 111111),   # outside: ignored
            usage_row(reset - 3 * HOUR, "acct-A", 4000),
            usage_row(reset - HOUR, "acct-A", 1500),
        ])
        P.note_limit_event(account("acct-A"), MODEL, reset)

        data = payload([account("acct-A")], now=reset - 10 * 60)
        budget = data["budgets"][0]
        self.assertEqual(budget["avg"], 5500)   # 4000 + 1500, live sample
        self.assertEqual(budget["n"], 1)
        row = find_row(data, "acct-A")
        self.assertTrue(row["cooling"])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["reset_at"], reset)

        # A restart re-reads the journal: the event (and its sample) survives.
        restart_fold()
        data = payload([account("acct-A")], now=reset - 10 * 60)
        self.assertEqual(data["budgets"][0]["avg"], 5500)
        self.assertTrue(find_row(data, "acct-A")["cooling"])

    def test_live_event_dedupes_against_the_log_row(self):
        reset = time.time() + 30 * 60
        _, detail = reset_epoch(time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(reset)))
        write_log([
            usage_row(reset - 2 * HOUR, "acct-A", 900),
            # The 429 row the log keeps for this same cap (the last account of
            # the retry batch keeps its attribution).
            cap_row(reset - HOUR, "acct-A", detail),
        ])
        P.note_limit_event(account("acct-A"), MODEL, reset)
        data = payload([account("acct-A")], now=reset - 10 * 60)
        # One cap, one sample - the live event and the log row are the same
        # (account, model, reset) and must not double-count.
        self.assertEqual(len(data["budgets"]), 1)
        self.assertEqual(data["budgets"][0]["n"], 1)

    def test_live_event_for_a_different_reset_is_a_second_sample(self):
        # Two caps on the same pair a day apart (the 24h window in between),
        # with the clock frozen so each hook call sees the usage of its own
        # window only.
        real_time = time.time
        clock = {"now": real_time()}
        time.time = lambda: clock["now"]
        try:
            t0 = clock["now"]
            reset1 = t0 + 30 * 60
            write_log([usage_row(t0 - 2 * HOUR, "acct-A", 700)])
            P.note_limit_event(account("acct-A"), MODEL, reset1)

            clock["now"] = t0 + 25 * HOUR          # the next day
            reset2 = clock["now"] + 30 * 60
            write_log([
                usage_row(t0 - 2 * HOUR, "acct-A", 700),      # window 1
                usage_row(t0 + 3 * HOUR, "acct-A", 1300),     # window 2
            ])
            P.note_limit_event(account("acct-A"), MODEL, reset2)

            data = payload([account("acct-A")], now=clock["now"])
        finally:
            time.time = real_time
        budget = data["budgets"][0]
        self.assertEqual(budget["n"], 1)                 # one account
        self.assertEqual(budget["avg"], 1000)            # mean(700, 1300)
        # The window in force is the later reset's, and it is still cooling.
        row = find_row(data, "acct-A")
        self.assertEqual(row["reset_at"], reset2)
        self.assertTrue(row["cooling"])
        self.assertEqual(row["used"], 1300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
