"""Account and panel state after a model-scoped upstream 429.

Run with: python _test_model_cooldowns.py
No upstream credentials or outbound network are used.
"""
import atexit
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error

_startup_dir = tempfile.TemporaryDirectory(prefix="model-cooldowns-")
atexit.register(_startup_dir.cleanup)
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as accounts
import wb_proxy as proxy
import wb_settings as settings

# The real 429 shapes from the incident environment (issue #70, sanitized).
# S1 is 229 of the 263 recorded 429s and carries no reset information at all;
# S2 is the one provably intl body with no reset field (the log keeps only the
# first 200 characters, so the JSON below is that prefix closed into valid
# JSON); S3 is a parsed control that names its reset instant.
S1_BARE = "usage exceeds frequency limit"
S2_INTL_14003 = ('{"code":14003,"msg":"too many requests",'
                 '"retry_policy":{"retry":true},'
                 '"displayMsg":{"en":"Too many requests. Please retry later.",'
                 '"zh":"请求过于频繁，请稍后重试。"}}')
# The same body as the gateway sees it: it reads up to 600 characters while the
# log kept 200, so the real body carries a metadata tail. accountId and
# credentialId are field names, not scope statements.
S2_INTL_FULL = ('{"code":14003,"msg":"too many requests",'
                '"accountId":"<redacted>","credentialId":"<redacted>",'
                '"requestId":"<redacted>","retry_policy":{"retry":true},'
                '"displayMsg":{"en":"Too many requests. Please retry later.",'
                '"zh":"请求过于频繁，请稍后重试。"},"ts":1790500000}')
S3_INTL_6004 = ('{"code":6004,"msg":"usage exceeds frequency limit, but don\'t '
                'worry, your usage will reset at 2026-10-06 21:00:09 UTC+8, '
                'alternatively, you can switch to the other models to continue '
                'using it."}')


class ModelCooldownTests(unittest.TestCase):
    def account(self):
        return accounts.Account({"uid": "synthetic-cn", "realm": "cn", "accessToken": "token"})

    def test_account_snapshot_and_selection(self):
        account = self.account()
        now = time.time()
        account.note_error("429", model="glm-5.3", until=now + 600)
        account.note_error("429", model="glm-5.2", until=now + 60)
        state = account.public()

        self.assertEqual([item["model"] for item in state["modelCooldowns"]],
                         ["glm-5.2", "glm-5.3"])
        self.assertTrue(all(isinstance(item["expiresAt"], int)
                            for item in state["modelCooldowns"]))
        self.assertFalse(state["inCooldown"])
        self.assertTrue(account.ready(model="another-model"))
        self.assertFalse(account.ready(model="glm-5.3"))

        account.clear_error(model="glm-5.3")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         ["glm-5.2"])
        with account._throttle_lock:
            account.model_cooldowns["glm-5.2"] = time.time() - 1
        self.assertEqual(account.model_cooldowns_snapshot(), [])

        with account._throttle_lock:
            account.cooldown_until = 1000.0
        with mock.patch.object(accounts.time, "time", return_value=1000.0):
            at_deadline = account.public()
        self.assertFalse(at_deadline["inCooldown"])
        self.assertIsNone(at_deadline["cooldownFor"])

    def test_snapshot_does_not_wait_for_token_refresh(self):
        account = self.account()
        account.note_error("429", model="glm-5.3", until=time.time() + 60)
        with account._refresh_lock:
            start = time.monotonic()
            self.assertEqual(account.public()["modelCooldowns"][0]["model"], "glm-5.3")
            self.assertLess(time.monotonic() - start, 1)

    def test_concurrent_updates_and_panel_reads(self):
        account = self.account()
        stop = threading.Event()
        failures = []

        def writer():
            n = 0
            while not stop.is_set():
                account.note_error("429", model="m%d" % (n % 20), until=time.time() + 5)
                account.clear_error(model="m%d" % ((n + 1) % 20))
                n += 1

        def reader():
            try:
                while not stop.is_set():
                    account.public()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader)
                                                      for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            time.sleep(0.5)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "worker did not stop")
        self.assertEqual(failures, [])

    def drive_one_429(self, account, detail="usage exceeds frequency limit",
                      stub_parser=True):
        """Drive open_upstream into an upstream 429 with a stubbed urlopen.

        Returns the reset instant the stubbed parser reported, so callers can
        assert the cooldown the gateway recorded. `stub_parser=False` leaves the
        real parser in place, which is what the unparsed-body case needs.
        """
        reset = time.time() + 600
        error = urllib.error.HTTPError("https://upstream.invalid", 429, "rate limit", {},
                                       io.BytesIO(detail.encode("utf-8")))

        class Pool(object):
            accounts = [account]
            affinity = types.SimpleNamespace(unbind=lambda _key: None)

            def count_ready(self, realm, model=None):
                return sum(a.ready(model=model) for a in self.accounts)

            def pick_for_session(self, realm, session_key=None, exclude=(), model=None):
                return next((a for a in self.accounts if a.uid not in exclude
                             and a.realm == realm and a.ready(model=model)), None)

            def list_public(self):
                return [a.public() for a in self.accounts]

            def apply_daily_token_limit(self, value=None, usage=None):
                # The production path pushes the daily guard into the pool before
                # picking; this stub only needs to answer the call.
                return value or 0

            def apply_daily_credit_limit(self, value=None, credits=None,
                                         free_models=None):
                return value or 0

            def apply_model_daily_token_limit(self, value=None, per_model=None):
                return value or 0

        old_pool, old_urlopen = proxy.POOL, accounts.urlopen
        old_parser = proxy.parse_rate_limit_reset
        proxy.POOL = Pool()
        accounts.urlopen = lambda *args, **kwargs: (_ for _ in ()).throw(error)
        if stub_parser:
            # Keep these tests on the model-cooldown path, independent of the
            # existing parser's timezone handling.
            proxy.parse_rate_limit_reset = lambda _detail: reset
        try:
            with self.assertRaises(proxy.RateLimited):
                proxy.open_upstream({"model": "glm-5.3", "messages": [
                    {"role": "user", "content": "hello"}]}, target_realm="cn")
        finally:
            proxy.POOL, accounts.urlopen = old_pool, old_urlopen
            proxy.parse_rate_limit_reset = old_parser
            error.close()
        return reset

    def test_upstream_429_reaches_accounts_payload(self):
        account = self.account()
        reset = self.drive_one_429(account)
        row = account.public()
        self.assertFalse(row["inCooldown"])
        self.assertEqual(row["modelCooldowns"][0]["model"], "glm-5.3")
        self.assertLess(abs(row["modelCooldowns"][0]["expiresAt"] - reset), 2)
        self.assertTrue(account.ready(model="another-model"))

    def test_the_real_bare_429_parks_only_the_model(self):
        """S1 - 87% of the recorded 429s, no reset information at all.

        It used to be read as an account-level soft limit purely because there
        was no timestamp, which is how a realm ended up fully cooling while the
        panel test kept reporting every account healthy.
        """
        account = self._drive_unparsed(S1_BARE, "bare-shape")
        row = account.public()
        self.assertEqual(account.unscoped_streak, 1,
                         "the repeat is counted on the model ladder")
        self.assertEqual(account.soft_streak, 0,
                         "and never on the credential one")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 61,
                             "first tier is the short window, not 600s")
        self.assertTrue(account.ready(model="another-model"))

    def test_the_full_intl_14003_body_with_metadata_parks_only_the_model(self):
        """S2 as the gateway really sees it: the recorded prefix plus the
        metadata tail the log truncated (accountId / credentialId).

        Field names are not scope evidence - reading them as such put the
        600s -> 7200s credential ladder back on the most common intl shape.
        """
        self.assertIn('"accountId"', S2_INTL_FULL)
        self.assertIn('"credentialId"', S2_INTL_FULL)
        account = self._drive_unparsed(S2_INTL_FULL, "intl-14003-full")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "metadata is not scope evidence")
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_the_real_intl_14003_429_parks_only_the_model(self):
        """S2 - the intl body, recognised by code 14003 (and by its message)."""
        account = self._drive_unparsed(S2_INTL_14003, "intl-14003")
        row = account.public()
        self.assertEqual(account.soft_streak, 0)
        self.assertFalse(row["inCooldown"])
        self.assertIsNone(row["cooldownFor"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        self.assertTrue(account.ready(model="another-model"))

    def test_a_parsed_intl_429_still_parks_until_its_clock(self):
        """S3 - the control: a named reset instant still drives a model park."""
        expected = proxy.parse_rate_limit_reset(S3_INTL_6004)
        self.assertEqual(expected, 1791291609)
        account = self._drive_unparsed(S3_INTL_6004, "intl-6004")
        row = account.public()
        self.assertEqual(account.soft_streak, 0, "a parsed clock is not a streak")
        self.assertFalse(row["inCooldown"])
        self.assertEqual([item["model"] for item in row["modelCooldowns"]], ["glm-5.3"])
        # The named instant is already past for this run, so the park is the 1s
        # floor - which is what tells it apart from the 60s unparsed window and
        # from the credential ladder.
        self.assertLessEqual(row["modelCooldowns"][0]["expiresAt"] - time.time(), 2)

    def _drive_unparsed(self, detail, tag):
        """Feed one real 429 body through open_upstream, parser left in place."""
        directory = tempfile.mkdtemp(prefix="real-429-%s-" % tag)
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            self.drive_one_429(account, detail=detail, stub_parser=False)
            return account
        finally:
            proxy.ACCOUNTS_DIR = old_dir

    def test_repeated_real_429s_do_not_amplify_into_an_account_park(self):
        """S1 three times in a row used to mean 600 -> 1200 -> 2400s on the
        whole credential. It has to stay three short windows on one model."""
        directory = tempfile.mkdtemp(prefix="real-429-repeat-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        windows = []
        try:
            account = self.account()
            for round_number in range(3):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                self.assertIsNone(account.public()["cooldownFor"],
                                  "round %d parked the credential" % round_number)
                self.assertTrue(account.ready(model="another-model"),
                                "round %d took a sibling model down" % round_number)
                windows.append(account.model_cooldowns["glm-5.3"] - time.time())
                # Let the window lapse, the way it does in production when the
                # same shape comes back; the streak is what must survive.
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.unscoped_streak, 3)
        self.assertEqual(account.soft_streak, 0, "the credential counter is untouched")
        self.assertLessEqual(windows[0], 61, "first tier stays short")
        self.assertLess(windows[0], windows[1], "repetition escalates")
        self.assertLess(windows[1], windows[2], "repetition keeps escalating")
        self.assertLessEqual(windows[2], 241, "and stays far below the 2h ceiling")

    def test_unscoped_hits_do_not_lift_the_credential_ladder(self):
        """Four unscoped S1s, then one credential-scoped body, end to end.

        Sharing one counter made this sequence start the credential ladder at
        600 * 2**4 - the 7200s ceiling - instead of at 600s.
        """
        directory = tempfile.mkdtemp(prefix="mixed-seq-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        try:
            account = self.account()
            for _ in range(4):
                self.drive_one_429(account, detail=S1_BARE, stub_parser=False)
                with account._throttle_lock:
                    account.model_cooldowns["glm-5.3"] = time.time() - 1
            self.assertEqual(account.unscoped_streak, 4)
            self.assertEqual(account.soft_streak, 0)

            self.drive_one_429(account,
                               detail='{"msg":"too many requests for this account"}',
                               stub_parser=False)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
        self.assertEqual(account.soft_streak, 1)
        self.assertEqual(account.unscoped_streak, 4, "the other ladder is untouched")
        self.assertLessEqual(
            account.cooldown_until - time.time(), accounts.SOFT_RATE_BASE + 1,
            "the credential ladder starts at its own base, not at the ceiling")
        self.assertEqual([item["model"] for item in account.public()["modelCooldowns"]],
                         [], "and adds no model window")

    def test_the_auto_switch_setting_is_opt_in(self):
        """Off on a fresh install, and only a real JSON boolean turns it on."""
        directory = tempfile.mkdtemp(prefix="auto-switch-setting-")
        self.assertFalse(settings.auto_switch_product(directory))
        self.assertTrue(settings.set_auto_switch_product(directory, True))
        self.assertTrue(settings.auto_switch_product(directory))
        self.assertFalse(settings.set_auto_switch_product(directory, False))
        self.assertFalse(settings.auto_switch_product(directory))
        with open(settings.settings_path(directory), "w", encoding="utf-8") as fh:
            json.dump({"auto_switch_product": "false"}, fh)
        self.assertFalse(settings.auto_switch_product(directory),
                         "a hand-edited string must not read as enabled")

    def test_a_429_keeps_the_identity_while_the_setting_is_off(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-off-")
        old_dir = proxy.ACCOUNTS_DIR
        proxy.ACCOUNTS_DIR = directory
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "workbuddy")
            self.assertEqual(proxy._SWITCH_LOG, {})
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy._SWITCH_LOG.clear()

    def test_a_429_rotates_the_identity_once_the_setting_is_on(self):
        directory = tempfile.mkdtemp(prefix="auto-switch-on-")
        settings.set_auto_switch_product(directory, True)
        old_dir = proxy.ACCOUNTS_DIR
        old_budget = proxy.MAX_PRODUCT_SWITCHES
        proxy.ACCOUNTS_DIR = directory
        # One switch makes the assertion exact and independent of how large the
        # real budget is (an even number of rotations ends back at workbuddy).
        proxy.MAX_PRODUCT_SWITCHES = 1
        proxy._SWITCH_LOG.clear()
        try:
            account = self.account()
            self.drive_one_429(account)
            self.assertEqual(account.product, "vscode")
            self.assertTrue(proxy._SWITCH_LOG)
        finally:
            proxy.ACCOUNTS_DIR = old_dir
            proxy.MAX_PRODUCT_SWITCHES = old_budget
            proxy._SWITCH_LOG.clear()


if __name__ == "__main__":
    unittest.main()
