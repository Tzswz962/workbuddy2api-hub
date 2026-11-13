"""Operational campaign task tests (Starbucks / reporter benefit).

Run with:
    python tests/_test_campaign_tasks.py
"""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_tasks as T  # noqa: E402


class FakeAccount(object):
    uid = "uid-campaign"
    nickname = "tester"
    realm = "cn"
    proxy = ""
    access_token = "token"
    credits = {"remain": 100}

    def headers(self, kind="chat"):
        return {"Authorization": "Bearer token"}

    def is_enterprise(self):
        return False

    def fetch_credits(self):
        pass


class _Resp(object):
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def campaign_payload(campaign):
    if campaign == "campaign_starbucks_2026":
        return {
            "code": 0,
            "data": {
                "tasks": [
                    {"task_code": "ai_chat", "target_count": 3,
                     "reward_credit": 100, "need_view": True},
                    {"task_code": "expert", "target_count": 1,
                     "reward_credit": 100, "need_view": True},
                    {"task_code": "app", "target_count": 1,
                     "reward_credit": 100, "need_view": True},
                ],
                "progress": [],
            },
        }
    return {
        "code": 0,
        "data": {
            "tasks": [
                {"task_code": "expert_chat", "target_count": 1,
                 "reward_credit": 50, "need_view": True},
                {"task_code": "skill_use", "target_count": 1,
                 "reward_credit": 50, "need_view": True},
                {"task_code": "desktop_same", "target_count": 1,
                 "reward_credit": 100, "need_view": True},
            ],
            "progress": [],
        },
    }


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.reports = []
        self.progress = set()
        self.claimed = set()
        self._orig = (T._accounts.urlopen, T._post_mp_events,
                      T._report_desktop_events, T.time.sleep)
        T.time.sleep = lambda _seconds: None

    def tearDown(self):
        (T._accounts.urlopen, T._post_mp_events,
         T._report_desktop_events, T.time.sleep) = self._orig

    def install_fake(self):
        def fake_urlopen(req, timeout=20, proxy=""):
            self.requests.append(req.full_url)
            if "/portal/activity/tasks?" in req.full_url:
                campaign = req.full_url.split("activity_code=", 1)[1]
                payload = campaign_payload(campaign)
                for task in payload["data"]["tasks"]:
                    code = task["task_code"]
                    if code in self.progress or code in self.claimed:
                        payload["data"]["progress"].append({
                            "task_code": code,
                            "progress": task["target_count"] if code in self.progress else 0,
                            "completed_at": "2026-11-13T00:00:00Z" if code in self.progress else None,
                            "claimed_at": "2026-11-13T00:00:01Z" if code in self.claimed else None,
                        })
                return _Resp(payload)
            if "/viewed?" in req.full_url:
                return _Resp({"code": 0})
            if "/claim?" in req.full_url:
                task = req.full_url.split("/tasks/", 1)[1].split("/claim", 1)[0]
                self.claimed.add(task)
                return _Resp({"code": 0})
            return _Resp({"code": 0})

        def fake_mp(account, events, base, env_extra=None, headers=None):
            self.reports.append((events, base, env_extra))
            if any(e.get("eventCode") == "chat_request_send" for e in events):
                task = "ai_chat" if "starbucks" in str(env_extra) else "skill_use"
                self.progress.add(task)
            if any(e.get("eventCode") == "expert_actual_use" for e in events):
                task = ("expert" if "starbucks" in str(env_extra)
                        else "expert_chat")
                self.progress.add(task)
            return True

        def fake_desktop(account, events):
            self.reports.append((events, T.CHAT_BASE, None))
            self.progress.add("desktop_same")
            return True

        T._accounts.urlopen = fake_urlopen
        T._post_mp_events = fake_mp
        T._report_desktop_events = fake_desktop

    def test_campaign_catalog_and_task_shapes(self):
        codes = {spec["code"] for spec in T.CAMPAIGN_SPECS}
        self.assertEqual(codes, {"campaign_starbucks_2026",
                                 "campaign_reporter_2026"})
        starbucks = T.CAMPAIGN_SPECS[0]["tasks"]
        self.assertEqual([task["task"] for task in starbucks],
                         ["ai_chat", "expert", "app"])
        reporter = T.CAMPAIGN_SPECS[1]["tasks"]
        self.assertEqual(reporter[0]["expert_id"], "ex_NaiDbO06Adf3")
        self.assertEqual(reporter[1]["skill_name"],
                         "interview-material-organizer")

    def test_view_and_claim_require_claimed_at(self):
        self.install_fake()
        rows = T.fetch_campaign_tasks(
            FakeAccount(), "campaign_starbucks_2026")
        self.assertEqual(rows["ai_chat"]["target"], 3)
        self.assertTrue(T._campaign_view(
            FakeAccount(), "campaign_starbucks_2026", "ai_chat"))

        def claim_without_receipt(req, timeout=20, proxy=""):
            if "/claim?" in req.full_url:
                return _Resp({"code": 0})
            return _Resp(campaign_payload("campaign_starbucks_2026"))

        T._accounts.urlopen = claim_without_receipt
        logs = []
        self.assertFalse(T._campaign_claim(
            FakeAccount(), "campaign_starbucks_2026", "ai_chat",
            logs.append, tries=1, gap=0))
        self.assertTrue(any("claimed_at" in line for line in logs))

    def test_starbucks_chat_expert_and_app_boundary(self):
        self.install_fake()
        logs = []
        result = T.run_campaign_tasks(FakeAccount())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.progress,
                         {"ai_chat", "expert", "expert_chat",
                          "skill_use", "desktop_same"})
        self.assertNotIn("app", self.progress)
        self.assertTrue(any("手机端 App" in line for line in result["logs"]))
        self.assertFalse(any("app" in str(item).lower()
                             for item in self.reports))

    def test_reporter_expert_skill_and_desktop_events(self):
        self.install_fake()
        result = T.run_campaign_tasks(FakeAccount())
        self.assertTrue(result["ok"], result)
        expert_events = next(events for events, base, page in self.reports
                             if any(e.get("eventCode") == "expert_actual_use"
                                    for e in events)
                             and "journalist" in str(page))
        self.assertEqual(expert_events[0]["id"], "ex_NaiDbO06Adf3")
        skill_events = next(events for events, base, page in self.reports
                            if any(e.get("eventCode") == "skill_info"
                                   for e in events))
        self.assertEqual(skill_events[0]["skillId"],
                         "skill_2117491159283752960")
        desktop_events = next(events for events, base, page in self.reports
                              if any(e.get("eventCode") == "playbook_prompt_send"
                                     for e in events))
        self.assertEqual(desktop_events[0]["id"], "cnb-content-calendar")

    def test_desktop_report_uses_the_desktop_endpoint_and_fingerprint(self):
        calls = []

        def fake_urlopen(req, timeout=20, proxy=""):
            calls.append(req)
            return _Resp({"code": 0})

        T._accounts.urlopen = fake_urlopen
        self.assertTrue(T._report_desktop_events(
            FakeAccount(), [{"eventCode": "playbook_prompt_send"}]))
        self.assertEqual(calls[0].full_url, T.CHAT_BASE + "/v2/report")
        body = json.loads(calls[0].data.decode("utf-8"))
        self.assertEqual(body[0]["ideVersion"], T.wb_identity.DESKTOP_VERSION_CN)
        self.assertEqual(body[0]["extVersion"], T.wb_identity.DESKTOP_VERSION_CN)
        self.assertEqual(body[0]["userId"], FakeAccount.uid)
        self.assertEqual(calls[0].get_header("X-domain"), "copilot.tencent.com")

    def test_desktop_report_accepts_empty_success_body(self):
        class EmptyResponse(object):
            status = 204

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        calls = []

        def fake_urlopen(req, timeout=20, proxy=""):
            calls.append(req)
            return EmptyResponse()

        T._accounts.urlopen = fake_urlopen
        self.assertTrue(T._report_desktop_events(
            FakeAccount(), [{"eventCode": "template_used"}]))
        self.assertEqual(calls[0].full_url, T.CHAT_BASE + "/v2/report")

    def test_growth_fetch_failure_does_not_skip_campaign(self):
        self.install_fake()
        original = (T.fetch_growth_tasks, T.run_miniprogram_tasks,
                    T.run_campaign_tasks)
        T.fetch_growth_tasks = lambda account: []
        T.run_miniprogram_tasks = lambda account, gap=1.0, claim=True: {
            "ok": True, "logs": [], "earned_credit": 0}
        try:
            result = T.run_growth_tasks(FakeAccount(), gap=0)
        finally:
            (T.fetch_growth_tasks, T.run_miniprogram_tasks,
             T.run_campaign_tasks) = original
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("运营活动" in line or "🎪" in line
                            for line in result["logs"]))

    def test_non_cn_and_enterprise_skip(self):
        class Intl(FakeAccount):
            realm = "intl"

        class Enterprise(FakeAccount):
            def is_enterprise(self):
                return True

        self.assertEqual(T.run_campaign_tasks(Intl())["logs"], [])
        self.assertEqual(T.run_campaign_tasks(Enterprise())["logs"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
