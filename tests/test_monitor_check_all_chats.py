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

    def test_generate_auto_reply_with_persona_and_model(self):
        config = {
            "ai": {
                "provider": "openai_compatible",
                "api_key": "test-key-123",
                "base_url": "https://api.example.com",
                "model": "base-model",
            },
            "monitor": {
                "auto_reply_persona": "5年资深前端，擅长React与性能优化",
                "auto_reply_model": "custom-chat-model",
            },
            "profile": {
                "resume_path": "./non_existent_resume.md",
            },
        }
        messages = [
            {"sender": "hr", "text": "你好，请问做过性能优化吗？"},
        ]
        job = {"id": "job-1", "title": "前端开发", "company": "TechCo"}

        captured_prompt = []
        captured_config = []

        def mock_call(prompt, cfg):
            captured_prompt.append(prompt)
            captured_config.append(cfg)
            return "是的，我做过很多前端性能优化项目。"

        with patch.object(monitor, "_call_claude", side_effect=mock_call):
            reply = monitor._generate_auto_reply(messages, job, config)
            self.assertEqual(reply, "是的，我做过很多前端性能优化项目。")
            self.assertEqual(len(captured_prompt), 1)
            # Verify persona is injected into prompt
            self.assertIn("## 设定人设\n5年资深前端，擅长React与性能优化", captured_prompt[0])
            self.assertIn("严格符合上述设定人设", captured_prompt[0])
            # Verify custom model is passed while keeping api_key and base_url
            self.assertEqual(captured_config[0]["ai"]["model"], "custom-chat-model")
            self.assertEqual(captured_config[0]["ai"]["api_key"], "test-key-123")
            self.assertEqual(captured_config[0]["ai"]["base_url"], "https://api.example.com")

    def test_handle_conversation_auto_reply_disabled_records_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("job-10", status="sent"))
            finally:
                db.close()

            config = {
                "monitor": {
                    "auto_reply_hr_questions": False,
                },
            }
            messages = [{"sender": "hr", "text": "方便发个简历吗？"}]
            job = _job("job-10")

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_open_scanned_conversation", return_value="tab-1"), \
                 patch.object(monitor, "_wait_or_stop", return_value=False), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(messages)), \
                 patch.object(monitor, "_detect_rejection", return_value=False), \
                 patch.object(monitor, "_detect_resume_request", return_value=False), \
                 patch.object(monitor, "_generate_auto_reply", return_value="可以的，稍等。"), \
                 patch.object(monitor, "_send_message_in_chat") as mock_send, \
                 patch.object(monitor, "close_tab"):
                result = monitor._handle_conversation(job, config)
                self.assertEqual(result, "reply_pending")
                mock_send.assert_not_called()

                # Verify history in DB
                check_db = get_db(db_path)
                try:
                    row = check_db.execute("SELECT * FROM history WHERE job_id = 'job-10'").fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["action"], "reply_pending")
                finally:
                    check_db.close()

    def test_handle_conversation_auto_reply_enabled_with_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("job-11", status="sent"))
            finally:
                db.close()

            config = {
                "monitor": {
                    "auto_reply_hr_questions": True,
                    "auto_reply_delay_seconds": 10,
                },
            }
            messages = [{"sender": "hr", "text": "何时能来面试？"}]
            job = _job("job-11")

            waited_seconds = []
            def mock_wait(cfg, seconds):
                waited_seconds.append(seconds)
                return False  # not stopped

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_open_scanned_conversation", return_value="tab-1"), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(messages)), \
                 patch.object(monitor, "_detect_rejection", return_value=False), \
                 patch.object(monitor, "_detect_resume_request", return_value=False), \
                 patch.object(monitor, "_generate_auto_reply", return_value="周三下午随时可以。"), \
                 patch.object(monitor, "_wait_or_stop", side_effect=mock_wait), \
                 patch.object(monitor, "_send_message_in_chat", return_value=True) as mock_send, \
                 patch.object(monitor, "close_tab"):
                result = monitor._handle_conversation(job, config)
                self.assertEqual(result, "auto_replied")
                # Verify delay was called with 10 seconds (in addition to 2s page wait)
                self.assertIn(10.0, waited_seconds)
                mock_send.assert_called_once_with("tab-1", "周三下午随时可以。")

                # Verify history in DB
                check_db = get_db(db_path)
                try:
                    row = check_db.execute("SELECT * FROM history WHERE job_id = 'job-11'").fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(row["action"], "auto_replied")
                finally:
                    check_db.close()

    def test_handle_conversation_auto_reply_stopped_during_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data" / "bosshunter.db"
            db = get_db(db_path)
            try:
                insert_job(db, _job("job-12", status="sent"))
            finally:
                db.close()

            config = {
                "monitor": {
                    "auto_reply_hr_questions": True,
                    "auto_reply_delay_seconds": 10,
                },
            }
            messages = [{"sender": "hr", "text": "您好"}]
            job = _job("job-12")

            def mock_wait(cfg, seconds):
                return seconds == 10.0

            with patch.object(monitor, "get_db", side_effect=lambda: get_db(db_path)), \
                 patch.object(monitor, "_open_scanned_conversation", return_value="tab-1"), \
                 patch.object(monitor, "evaluate", return_value=json.dumps(messages)), \
                 patch.object(monitor, "_detect_rejection", return_value=False), \
                 patch.object(monitor, "_detect_resume_request", return_value=False), \
                 patch.object(monitor, "_generate_auto_reply", return_value="您好！"), \
                 patch.object(monitor, "_wait_or_stop", side_effect=mock_wait), \
                 patch.object(monitor, "_send_message_in_chat") as mock_send, \
                 patch.object(monitor, "close_tab"):
                result = monitor._handle_conversation(job, config)
                self.assertEqual(result, "stopped")
                mock_send.assert_not_called()

    def test_api_monitor_auto_reply_config(self):
        from bosshunter.web import server

        test_cfg = {
            "ai": {"model": "gpt-4o", "provider": "openai_compatible"},
            "monitor": {
                "auto_reply_hr_questions": False,
                "auto_reply_delay_seconds": 10,
                "auto_reply_model": "",
                "auto_reply_persona": "",
            },
        }

        def mock_load(path=None):
            return dict(test_cfg)

        def mock_write(cfg):
            test_cfg.update(cfg)

        with patch.object(server, "load_config", side_effect=mock_load), \
             patch.object(server, "_write_config", side_effect=mock_write):
            # Test GET
            get_res = json.loads(server.api_monitor_get_auto_reply_config())
            self.assertEqual(get_res["auto_reply_hr_questions"], False)
            self.assertEqual(get_res["auto_reply_delay_seconds"], 10)
            self.assertEqual(get_res["default_ai_model"], "gpt-4o")

            # Test POST
            with patch.object(server, "request") as mock_req:
                mock_req.json = {
                    "auto_reply_hr_questions": True,
                    "auto_reply_delay_seconds": 15,
                    "auto_reply_model": "deepseek-coder",
                    "auto_reply_persona": "资深后端工程师",
                }
                post_res = json.loads(server.api_monitor_post_auto_reply_config())
                self.assertEqual(post_res["success"], True)
                self.assertEqual(post_res["auto_reply_hr_questions"], True)
                self.assertEqual(post_res["auto_reply_delay_seconds"], 15)
                self.assertEqual(post_res["auto_reply_model"], "deepseek-coder")
                self.assertEqual(post_res["auto_reply_persona"], "资深后端工程师")

            # Test GET after POST
            get_res2 = json.loads(server.api_monitor_get_auto_reply_config())
            self.assertEqual(get_res2["auto_reply_hr_questions"], True)
            self.assertEqual(get_res2["auto_reply_delay_seconds"], 15)
            self.assertEqual(get_res2["auto_reply_model"], "deepseek-coder")
            self.assertEqual(get_res2["auto_reply_persona"], "资深后端工程师")


if __name__ == "__main__":
    unittest.main()

