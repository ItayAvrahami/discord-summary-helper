import json
import unittest
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import discord_cli


class DiscordCliContractTests(unittest.TestCase):
    def test_parser_accepts_stabilized_cli_commands(self):
        parser = discord_cli.build_parser()

        args = parser.parse_args([
            "--context",
            "04-performance-engineering",
            "--since-days",
            "14",
            "--compact",
            "--max-messages",
            "5",
            "--max-chars",
            "1000",
        ])

        self.assertEqual(args.context, "04-performance-engineering")
        self.assertEqual(args.since_days, 14)
        self.assertTrue(args.compact)
        self.assertEqual(args.max_messages, 5)
        self.assertEqual(args.max_chars, 1000)

    def test_parser_accepts_no_compact_override(self):
        parser = discord_cli.build_parser()

        args = parser.parse_args(["--context", "performance", "--no-compact"])

        self.assertEqual(args.context, "performance")
        self.assertFalse(args.compact)

    def test_effective_config_uses_config_defaults_for_normal_context(self):
        config = {
            "tool_defaults": {
                "default_context_window": {"since_days": 3},
                "refresh_window": {"since_days": 5},
                "stale_threshold_hours": 12,
                "compact": True,
                "max_messages": 7,
                "max_chars": 2000,
                "output_language": "English",
                "auto_refresh_policy": "ask",
                "safety": {"export_timeout_seconds": 99, "server_parallel": 1},
            }
        }
        channel = {"name": "demo"}

        result = discord_cli.resolve_effective_config(config, channel, argparse_namespace={})

        self.assertEqual(result["context_after"], discord_cli.resolve_after_date(config, 3, None))
        self.assertEqual(result["refresh_after"], discord_cli.resolve_after_date(config, 5, None))
        self.assertEqual(result["max_messages"], 7)
        self.assertEqual(result["max_chars"], 2000)
        self.assertTrue(result["compact"])

    def test_effective_config_applies_channel_override(self):
        config = {
            "tool_defaults": {
                "default_context_window": {"since_days": 3},
                "refresh_window": {"since_days": 5},
                "stale_threshold_hours": 12,
                "compact": True,
                "max_messages": 7,
                "max_chars": 2000,
                "output_language": "English",
                "auto_refresh_policy": "ask",
                "safety": {"export_timeout_seconds": 99, "server_parallel": 1},
            }
        }
        channel = {"name": "demo", "overrides": {"max_messages": 4, "default_context_window": {"since_days": 1}}}

        result = discord_cli.resolve_effective_config(config, channel, argparse_namespace={})

        self.assertEqual(result["context_after"], discord_cli.resolve_after_date(config, 1, None))
        self.assertEqual(result["max_messages"], 4)

    def test_effective_config_cli_flags_override_config(self):
        config = {
            "tool_defaults": {
                "default_context_window": {"since_days": 3},
                "refresh_window": {"since_days": 5},
                "stale_threshold_hours": 12,
                "compact": True,
                "max_messages": 7,
                "max_chars": 2000,
                "output_language": "English",
                "auto_refresh_policy": "ask",
                "safety": {"export_timeout_seconds": 99, "server_parallel": 1},
            }
        }
        channel = {"name": "demo"}
        args = {"since_days": 2, "max_messages": 5, "max_chars": 1000, "compact": False}

        result = discord_cli.resolve_effective_config(config, channel, args)

        self.assertEqual(result["context_after"], discord_cli.resolve_after_date(config, 2, None))
        self.assertEqual(result["refresh_after"], discord_cli.resolve_after_date(config, 2, None))
        self.assertEqual(result["max_messages"], 5)
        self.assertEqual(result["max_chars"], 1000)
        self.assertFalse(result["compact"])

    def test_effective_config_output_is_printed(self):
        effective = {
            "channel_name": "demo",
            "channel_id": "1",
            "context_window": "after=2026-01-01",
            "refresh_window": "after=2026-01-01",
            "max_messages": 5,
            "max_chars": 1000,
            "compact": True,
            "timeout_seconds": 99,
            "stale_threshold_hours": 12,
            "include_threads": "None",
            "cache_freshness": "ok",
            "auto_refresh_policy": "ask",
            "output_language": "English",
            "caveats": ["cache ok"],
        }

        output = discord_cli.build_effective_config_output(effective)

        self.assertIn("resolved channel: demo", output)
        self.assertIn("resolved context window: after=2026-01-01", output)
        self.assertIn("max_messages: 5", output)
        self.assertIn("compact mode: True", output)
        self.assertIn("timeout_seconds: 99", output)
        self.assertIn("stale_threshold_hours: 12", output)

    def test_refresh_timeout_fails_without_traceback_or_token(self):
        config = {
            "default_time_range_days": 7,
            "tool_defaults": {
                "default_context_window": {"since_days": 3},
                "refresh_window": {"since_days": 1},
                "stale_threshold_hours": 12,
                "compact": True,
                "max_messages": 7,
                "max_chars": 2000,
                "output_language": "English",
                "auto_refresh_policy": "ask",
                "safety": {"export_timeout_seconds": 99, "server_parallel": 1},
            },
            "channels": [{"name": "demo", "id": "1", "aliases": ["demo"], "enabled": True}],
        }
        args = {"since_days": None, "after": None, "max_messages": None, "max_chars": None, "compact": None, "timeout_seconds": None}

        def raise_timeout(*_args, **_kwargs):
            raise RuntimeError("Channel refresh timed out for demo after 99s")

        output = StringIO()
        with patch.object(discord_cli, "refresh_one_channel", side_effect=raise_timeout):
            with patch.object(discord_cli, "record_run"):
                with redirect_stdout(output):
                    exit_code = discord_cli.run_refresh_channel_command(config, "demo", args)

        text = output.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("refresh failed: Channel refresh timed out for demo after 99s", text)
        self.assertIn("timeout_seconds: 99", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("-t", text)

    def test_since_hours_window_comes_from_config(self):
        config = {"default_time_range_days": 7}

        after_date, window_text = discord_cli.resolve_window_from_config(config, {"since_hours": 2})

        parsed_after = datetime.fromisoformat(after_date)
        self.assertLess(datetime.now().astimezone() - parsed_after, timedelta(hours=3))
        self.assertIn("since_hours=2", window_text)

    def test_export_command_is_channel_scoped_and_redacted(self):
        channel = {"name": "introduction", "id": "1480134530156335255", "type": "text"}
        config = {"discord_exporter_path": "DiscordChatExporter.Cli.exe"}

        command = discord_cli.build_export_channel_command(
            channel,
            config,
            "secret-token",
            "2026-06-13T20:00:00+03:00",
            Path("out"),
        )
        redacted = discord_cli.redact_command(command)

        self.assertIn("export", command)
        self.assertIn("-c", command)
        self.assertIn("1480134530156335255", command)
        self.assertNotIn("exportguild", command)
        self.assertIn("--after", command)
        self.assertIn("2026-06-13T20:00:00+03:00", command)
        self.assertIn("--include-threads", command)
        self.assertIn("None", command)
        self.assertNotIn("secret-token", redacted)
        self.assertIn("<redacted>", redacted)

    def test_forum_export_command_includes_active_threads(self):
        channel = {"name": "performance", "id": "1512013461956067458", "type": "forum"}
        config = {"discord_exporter_path": "DiscordChatExporter.Cli.exe"}

        command = discord_cli.build_export_channel_command(channel, config, "token", "2026-06-13", Path("out"))

        self.assertEqual(command[-1], "Active")

    def test_export_timeout_preserves_existing_raw_cache(self):
        channel = {"name": "demo", "id": "1", "type": "text"}
        config = {"discord_exporter_path": "DiscordChatExporter.Cli.exe"}

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            raw_dir = tmp_path / "raw"
            partial_dir = tmp_path / "partial"
            raw_dir.mkdir()
            existing_file = raw_dir / "existing.json"
            existing_file.write_text("{}", encoding="utf-8")

            with patch.object(discord_cli, "raw_path_for_channel", return_value=raw_dir):
                with patch.object(discord_cli, "partial_raw_path_for_channel", return_value=partial_dir):
                    with patch.object(discord_cli.subprocess, "run", side_effect=subprocess.TimeoutExpired(["export"], 1)):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            discord_cli.export_channel(channel, config, "token", "2026-06-13", 1)

            self.assertTrue(existing_file.exists())
            self.assertTrue(partial_dir.exists())

    def test_filter_cache_data_by_after_keeps_recent_messages(self):
        now = datetime.now().astimezone()
        cache_data = {
            "channel": "demo",
            "channel_id": "1",
            "posts_count": 1,
            "total_messages": 2,
            "normalized_posts": [
                {
                    "post_name": "demo post",
                    "messages": [
                        {"timestamp": (now - timedelta(days=1)).isoformat(), "content": "recent"},
                        {"timestamp": (now - timedelta(days=10)).isoformat(), "content": "old"},
                    ],
                }
            ],
        }

        result = discord_cli.filter_cache_data_by_after(cache_data, (now - timedelta(days=7)).date().isoformat())

        self.assertEqual(result["total_messages"], 1)
        self.assertEqual(result["normalized_posts"][0]["messages"][0]["content"], "recent")

    def test_status_output_includes_run_metadata_fields(self):
        entry = {
            "channel_name": "demo",
            "channel_id": "1",
            "raw_cache_exists": False,
            "raw_cache_path": "raw",
            "raw_json_file_count": 0,
            "normalized_cache_exists": False,
            "normalized_cache_path": "normalized",
            "posts_count": 0,
            "messages_count": 0,
            "latest_exported_at": "unknown",
            "latest_message_timestamp": "unknown",
            "cache_age": "unknown",
            "status": "missing",
            "recommended_action": "refresh",
            "notes": [],
            "stale_after": "24h 0m",
            "last_attempted_refresh": "2026-01-01T00:00:00+00:00",
            "last_successful_refresh": "unknown",
            "requested_window": "after=2026-01-01",
            "partial_export": True,
            "last_error": "timeout",
            "last_warning": "partial data preserved",
        }

        output = discord_cli.build_status_output([entry])

        self.assertIn("last attempted refresh: 2026-01-01T00:00:00+00:00", output)
        self.assertIn("partial export: True", output)
        self.assertIn("last error: timeout", output)

    def test_summarize_without_provider_fails_clearly(self):
        message = discord_cli.build_summarize_not_configured_message("04-performance-engineering")

        self.assertIn("LLM summarization is not configured", message)
        self.assertIn("--context", message)

    def test_context_instructions_are_english(self):
        instructions = "\n".join(discord_cli.build_agent_summary_instructions())

        self.assertIn("Answer in English", instructions)
        self.assertIn("Source Window", instructions)

    def test_context_limit_override_allows_reading_all_messages_in_large_threads(self):
        config = {
            "output_limits": {
                "max_posts": 10,
                "max_snippets_per_post": 3,
                "max_total_snippets": 25,
                "max_chars_per_snippet": 500,
                "max_output_chars": 12000,
            }
        }

        limits = discord_cli.make_context_limits(config, max_messages=100, max_chars=50000)

        self.assertEqual(limits["max_total_snippets"], 100)
        self.assertEqual(limits["max_posts"], 100)
        self.assertEqual(limits["max_snippets_per_post"], 100)
        self.assertEqual(limits["max_output_chars"], 50000)

    def test_summary_instructions_require_practical_main_points(self):
        instructions = "\n".join(discord_cli.build_agent_summary_instructions())

        self.assertIn("Do not only list topics", instructions)
        self.assertIn("practical answer", instructions)
        self.assertIn("per post/thread", instructions)

    def test_cache_window_warning_compares_naive_requested_date(self):
        cache_data = {
            "normalized_posts": [
                {
                    "exported_at": "2026-06-13T20:42:03+03:00",
                    "date_range": {"after": "2026-06-06T00:00:00+03:00"},
                    "messages": [],
                }
            ]
        }

        warning = discord_cli.cache_window_warning(cache_data, "2026-05-30")

        self.assertIn("requested context starts", warning)

    def test_status_marks_cache_incomplete_when_context_window_is_wider_than_cached_export(self):
        config = {
            "default_time_range_days": 7,
            "cache_stale_after_hours": 24,
            "cache_ttl_minutes": 30,
            "output_limits": {},
            "tool_defaults": {
                "default_context_window": {"after": "2026-06-10"},
                "refresh_window": {"after": "2026-06-12"},
                "stale_threshold_hours": 24,
                "compact": True,
                "max_messages": 25,
                "max_chars": 12000,
                "output_language": "English",
                "auto_refresh_policy": "ask",
                "safety": {"export_timeout_seconds": 240, "server_parallel": 1},
            },
        }
        channel = {"name": "demo-window", "id": "1", "aliases": ["demo-window"], "type": "text"}
        normalized_data = {
            "posts_count": 1,
            "total_messages": 1,
            "normalized_posts": [
                {
                    "exported_at": datetime.now().astimezone().isoformat(),
                    "date_range": {"after": "2026-06-12T00:00:00+03:00"},
                    "messages": [
                        {"timestamp": "2026-06-12T10:00:00+03:00", "content": "newer"}
                    ],
                }
            ],
        }

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            raw_dir = tmp_path / "raw"
            raw_dir.mkdir()
            (raw_dir / "export.json").write_text("{}", encoding="utf-8")
            normalized_path = tmp_path / "normalized.json"
            normalized_path.write_text(json.dumps(normalized_data), encoding="utf-8")

            with patch.object(discord_cli, "raw_path_for_channel", return_value=raw_dir):
                with patch.object(discord_cli, "normalized_path_for_channel", return_value=normalized_path):
                    with patch.object(discord_cli, "load_channel_status_state", return_value={}):
                        status = discord_cli.get_channel_cache_status(channel, config)

        self.assertEqual(status["status"], "incomplete")
        self.assertIn("older requested messages may be missing", status["recommended_action"])
        self.assertTrue(any("requested context starts" in caveat for caveat in status["effective_config"]["caveats"]))

    def test_remove_directory_tree_removes_existing_directory(self):
        with TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "raw-cache"
            target.mkdir()
            (target / "export.json").write_text("{}", encoding="utf-8")

            discord_cli.remove_directory_tree(target)

            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
