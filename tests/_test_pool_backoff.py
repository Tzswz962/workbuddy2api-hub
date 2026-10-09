"""Pin the account-level failure governance: soft backoff, breaker, degrade.

Repeated soft limits used to get the same short cooldown every time, so an
account that was clearly being throttled kept getting handed out; hard and
unknown failures had no accumulated penalty at all. The panel project's pool
cools a soft-limited credential exponentially (10m, 20m, ... capped at 2h),
trips a breaker after 3 consecutive hard failures (30m doubling to 6h) and
degrades after 5 unknown ones (10m doubling to 2h). A served request clears
everything.

No network: accounts are built from dicts and written to a temp directory.
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-pool-backoff-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_accounts as A
import wb_proxy


INTL_ISS = "https://www.workbuddy.ai/auth/realms/copilot"


def jwt(iss=INTL_ISS, sub="u-1"):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": iss, "sub": sub, "exp": 4102444800}))


def load_account(uid="u-backoff-1"):
    directory = os.path.join(_TMP, uid)
    os.makedirs(directory, exist_ok=True)
    data = {"uid": uid, "domain": "www.workbuddy.ai", "realm": "intl",
            "accessToken": jwt()}
    with open(os.path.join(directory, uid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    accounts = A.AccountPool(directory).load()
    assert len(accounts) == 1
    return accounts[0]


class BackoffMathTests(unittest.TestCase):
    def test_soft_backoff_doubles_and_caps(self):
        self.assertEqual(A.soft_backoff(1), A.SOFT_RATE_BASE)
        self.assertEqual(A.soft_backoff(2), A.SOFT_RATE_BASE * 2)
        self.assertEqual(A.soft_backoff(4), A.SOFT_RATE_BASE * 8)
        self.assertEqual(A.soft_backoff(50), A.SOFT_RATE_MAX)

    def test_breaker_starts_at_the_threshold_and_caps(self):
        self.assertEqual(A.breaker_backoff(A.BREAKER_THRESHOLD), A.BREAKER_COOLDOWN)
        self.assertEqual(A.breaker_backoff(A.BREAKER_THRESHOLD + 1),
                         A.BREAKER_COOLDOWN * 2)
        self.assertEqual(A.breaker_backoff(99), A.BREAKER_COOLDOWN_MAX)

    def test_degrade_starts_at_the_threshold_and_caps(self):
        self.assertEqual(A.degrade_backoff(A.DEGRADE_THRESHOLD), A.DEGRADE_COOLDOWN)
        self.assertEqual(A.degrade_backoff(A.DEGRADE_THRESHOLD + 2),
                         A.DEGRADE_COOLDOWN * 4)
        self.assertEqual(A.degrade_backoff(99), A.DEGRADE_COOLDOWN_MAX)

    def test_account_level_429_is_the_one_without_a_reset_clock(self):
        self.assertTrue(wb_proxy.rate_limit_is_account_level("", None))
        self.assertFalse(wb_proxy.rate_limit_is_account_level("...", time.time() + 60))

    def test_reset_clock_is_parsed_for_both_realm_wordings(self):
        cn = ('{"code":6004,"msg":"您的使用量已超出频率限制，'
              '将在 2026-10-09 14:44:59 UTC+8 重置。"}')
        intl = ('{"code":6004,"message":"your usage will reset at '
                '2026-10-09 14:44:59 UTC+8"}')
        cn_reset = wb_proxy.parse_rate_limit_reset(cn)
        self.assertEqual(cn_reset, wb_proxy.parse_rate_limit_reset(intl))
        self.assertIsNotNone(cn_reset)
        # 14:44:59 UTC+8 is the same wall clock as 06:44:59 UTC.
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cn_reset)),
                         "2026-10-09 06:44:59")
        self.assertIsNone(wb_proxy.parse_rate_limit_reset('{"code":429}'))


class AccountGovernanceTests(unittest.TestCase):
    def test_soft_rate_backs_off_and_counts_the_streak(self):
        account = load_account("u-soft")
        first = account.note_soft_rate("HTTP 429 (account soft rate)")
        second = account.note_soft_rate("HTTP 429 (account soft rate)")
        self.assertEqual(account.soft_streak, 2)
        self.assertEqual(first, A.SOFT_RATE_BASE)
        self.assertEqual(second, A.SOFT_RATE_BASE * 2)
        self.assertGreater(account.throttle_wait(), 0)
        view = account.public()
        self.assertEqual(view["softStreak"], 2)
        self.assertTrue(view["inCooldown"])

    def test_breaker_needs_three_hard_failures(self):
        account = load_account("u-breaker")
        account.note_failure("HTTP 502")
        account.note_failure("HTTP 502")
        self.assertEqual(account.throttle_wait(), 0)
        account.note_failure("HTTP 503")
        self.assertGreater(account.breaker_until, 0)
        self.assertGreater(account.throttle_wait(), 0)
        self.assertTrue(account.public()["breakerFor"])

    def test_unknown_failures_degrade_after_the_threshold(self):
        account = load_account("u-degrade")
        account.note_unknown_failure("connection: TimeoutError")
        account.note_unknown_failure("connection: TimeoutError")
        self.assertEqual(account.throttle_wait(), 0)
        # Unknown failures also feed the shared breaker counter, so the wrap
        # is asserted past both thresholds; the degrade window itself starts
        # at DEGRADE_THRESHOLD.
        for _ in range(A.DEGRADE_THRESHOLD - 2):
            account.note_unknown_failure("connection: TimeoutError")
        self.assertEqual(account.degrade_count, A.DEGRADE_THRESHOLD)
        self.assertGreater(account.degrade_until, 0)
        self.assertGreater(account.throttle_wait(), 0)
        self.assertTrue(account.public()["degradeFor"])

    def test_success_clears_every_penalty(self):
        account = load_account("u-success")
        account.note_soft_rate("HTTP 429 (account soft rate)")
        for _ in range(A.BREAKER_THRESHOLD):
            account.note_failure("HTTP 502")
        account.note_unknown_failure("connection: TimeoutError")
        account.note_success(model="deepseek-v4.1-flash")
        self.assertEqual(account.throttle_wait(), 0)
        self.assertEqual(account.soft_streak, 0)
        self.assertEqual(account.fails, 0)
        self.assertEqual(account.degrade_count, 0)
        self.assertEqual(account.breaker_until, 0.0)
        self.assertEqual(account.degrade_until, 0.0)
        self.assertEqual(account.public()["lastError"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
