import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys

for mod in ["rich", "rich.console", "rich.progress", "yaml", "pypdf", "pypdf.errors"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

if "bottle" not in sys.modules:
    mock_bottle = MagicMock()
    def _pass_through(*args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator
    mock_bottle.Bottle.return_value.route.side_effect = _pass_through
    mock_bottle.Bottle.return_value.error.side_effect = _pass_through
    mock_bottle.route.side_effect = _pass_through
    mock_bottle.error.side_effect = _pass_through
    sys.modules["bottle"] = mock_bottle

from bosshunter.db import add_history, get_db, insert_job, update_job_status
from bosshunter.executor import monitor


def _job(job_id: str, company: str = "Example", hr_name: str = "HR", status: str = "sent") -> dict:
    return {
        "id": job_id,
        "title": "Python开发",
        "company": company,
        "salary": "20-30K",
        "city": "北京",
        "experience": "1-3年",
        "jd": "负责后台开发",
        "hr_name": hr_name,
        "hr_title": "招聘HR",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
        "status": status,
    }


class MonitorCheckAllChatsTests(unittest.TestCase):
    def test_check_all_chats_false_ignores_untracked_chat(self):
        conversations = [
            {
                "hr_name": "UnknownHR",
                "company": "UnknownCo",
                "hr_title": "HRBP",
                "last_message": "你好，看你简历挺合适",
                "has_reply": True,
                "last_direction": "hr",
                "element_index": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("job-1", company="OtherCo", hr_name="OtherHR", status="sent"))
            finally:
                db.close()

            config = {
                "monitor": {
                    "check_all_chats": False,
                    "max_conversations_per_cycle": 5,
                },
                "_monitor_reuse_chat_tab": True,
            }

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_get_monitor_chat_target", return_value=("tab-1", True)), \
                 patch.object(monitor, "_wait_for_page_or_stop", return_value=True), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(conversations)), \
                 patch.object(monitor, "_inspect_monitor_page"):
                results = monitor.check_replies(config)
                self.assertEqual(len(results), 0)

    def test_check_all_chats_true_scans_and_detects_untracked_chat(self):
        conversations = [
            {
                "hr_name": "NewHR",
                "company": "NewCo",
                "hr_title": "招聘专员",
                "last_message": "你好，想了解一下你的近况",
                "has_reply": True,
                "last_direction": "hr",
                "element_index": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            db.close()

            config = {
                "monitor": {
                    "check_all_chats": True,
                    "max_conversations_per_cycle": 5,
                },
                "_monitor_reuse_chat_tab": True,
            }

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_get_monitor_chat_target", return_value=("tab-1", True)), \
                 patch.object(monitor, "_wait_for_page_or_stop", return_value=True), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(conversations)), \
                 patch.object(monitor, "_inspect_monitor_page"):
                results = monitor.check_replies(config)
                self.assertEqual(len(results), 1)
                matched_job = results[0]["job"]
                self.assertEqual(matched_job["company"], "NewCo")
                self.assertEqual(matched_job["hr_name"], "NewHR")
                self.assertTrue(matched_job["id"].startswith("boss_chat_"))

                # Verify synthesized job exists in database
                check_db = get_db(db_path)
                try:
                    row = check_db.execute("SELECT * FROM jobs WHERE id = ?", (matched_job["id"],)).fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["company"], "NewCo")
                    self.assertEqual(row["status"], "replied")
                finally:
                    check_db.close()

    def test_check_all_chats_true_matches_existing_untracked_status_job(self):
        conversations = [
            {
                "hr_name": "ScoredHR",
                "company": "ScoredCo",
                "hr_title": "HR",
                "last_message": "请问还在找工作吗？",
                "has_reply": True,
                "last_direction": "hr",
                "element_index": 0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                # Job exists in DB as 'scored' (not sent or replied)
                insert_job(db, _job("scored-1", company="ScoredCo", hr_name="ScoredHR", status="scored"))
            finally:
                db.close()

            config = {
                "monitor": {
                    "check_all_chats": True,
                    "max_conversations_per_cycle": 5,
                },
                "_monitor_reuse_chat_tab": True,
            }

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_get_monitor_chat_target", return_value=("tab-1", True)), \
                 patch.object(monitor, "_wait_for_page_or_stop", return_value=True), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(conversations)), \
                 patch.object(monitor, "_inspect_monitor_page"):
                results = monitor.check_replies(config)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["job"]["id"], "scored-1")

    def test_follow_up_skips_synthesized_boss_chat_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                # Add synthesized boss_chat_ job with old updated_at
                insert_job(db, _job("boss_chat_123456", company="ChatOnlyCo", hr_name="ChatHR", status="sent"))
                db.execute(
                    "UPDATE jobs SET updated_at = datetime('now', '-72 hours') WHERE id = 'boss_chat_123456'"
                )
                db.commit()
            finally:
                db.close()

            config = {
                "follow_up": {
                    "enabled": True,
                    "interval_hours": 24,
                    "skip_weekends": False,
                },
            }

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_open_conversation") as mock_open:
                throttle = MagicMock()
                count = monitor._check_follow_ups(config, throttle)
                self.assertEqual(count, 0)
                mock_open.assert_not_called()

    def test_api_monitor_check_all_chats(self):
        from bosshunter.web import server

        test_cfg = {"monitor": {"check_all_chats": False}}
        saved_configs = []

        def mock_load(path=None):
            return dict(test_cfg)

        def mock_write(cfg):
            test_cfg.update(cfg)
            saved_configs.append(cfg)

        with patch.object(server, "load_config", side_effect=mock_load), \
             patch.object(server, "_write_config", side_effect=mock_write):
            # Test GET
            get_res = server.api_monitor_get_check_all_chats()
            self.assertEqual(json.loads(get_res)["check_all_chats"], False)

            # Test POST to enable
            with patch.object(server, "request") as mock_req:
                mock_req.json = {"check_all_chats": True}
                post_res = server.api_monitor_post_check_all_chats()
                self.assertEqual(json.loads(post_res)["success"], True)
                self.assertEqual(json.loads(post_res)["check_all_chats"], True)
                self.assertEqual(test_cfg["monitor"]["check_all_chats"], True)

            # Test GET after POST
            get_res2 = server.api_monitor_get_check_all_chats()
            self.assertEqual(json.loads(get_res2)["check_all_chats"], True)


if __name__ == "__main__":
    unittest.main()
