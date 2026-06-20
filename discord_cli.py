import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
CACHE_ROOT = PROJECT_ROOT / "cache"
STATE_ROOT = PROJECT_ROOT / "state"
CHANNEL_STATUS_PATH = STATE_ROOT / "channel_status.json"
LAST_RUNS_PATH = STATE_ROOT / "last_runs.json"
DEFAULT_EXPORT_TIMEOUT_SECONDS = 240
DEFAULT_TOOL_DEFAULTS = {
    "default_context_window": {"since_days": 7},
    "refresh_window": {"since_days": 7},
    "stale_threshold_hours": 24,
    "compact": True,
    "max_messages": 25,
    "max_chars": 12000,
    "output_language": "English",
    "auto_refresh_policy": "ask",
    "safety": {
        "export_timeout_seconds": DEFAULT_EXPORT_TIMEOUT_SECONDS,
        "server_parallel": 1,
        "stop_on_forbidden": True,
        "stop_on_unauthorized": True,
        "allow_auto_bootstrap": False,
        "retry_attempts": 0
    }
}
DEFAULT_OUTPUT_LIMITS = {
    "max_posts": 10,
    "max_snippets_per_post": 3,
    "max_total_snippets": 25,
    "max_chars_per_snippet": 500,
    "max_output_chars": 12000,
}
DANGEROUS_GENERIC_ALIASES = {
    "ai",
    "model",
    "cloud",
    "help",
    "course",
    "general",
}
COMMON_FILTER_WORDS = {
    "update",
    "me",
    "on",
    "about",
    "answer",
    "in",
    "hebrew",
    "summarize",
    "summary",
    "what",
    "new",
    "latest",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local Discord summary helper CLI."
    )
    parser.add_argument("--prompt", help="Reserved for later tasks.")
    parser.add_argument("--context", help="Emit bounded LLM-ready context for one configured channel.")
    parser.add_argument("--summary-context", help="Resolve a natural-language request and emit LLM-ready context.")
    parser.add_argument("--summarize", help="Reserved standalone summary mode; v1 fails clearly without an LLM provider.")
    parser.add_argument("--bootstrap-server", action="store_true", help="Run one explicit server bootstrap export.")
    parser.add_argument("--refresh-missing", action="store_true", help="Refresh configured channels with missing normalized cache.")
    parser.add_argument("--refresh-stale", action="store_true", help="Refresh configured channels whose cache is stale.")
    parser.add_argument("--since-days", type=int, help="One-off override: use messages after N days ago.")
    parser.add_argument("--after", help="One-off override: use messages after YYYY-MM-DD.")
    parser.add_argument("--max-messages", type=int, help="One-off override: maximum snippets/messages to include.")
    parser.add_argument("--max-chars", type=int, help="One-off override: maximum context output characters.")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="One-off override: exporter timeout in seconds.")
    parser.add_argument("--refresh-channel", help="Refresh exactly one configured channel.")
    parser.add_argument("--list-channels", action="store_true", help="List configured channels.")
    parser.add_argument("--validate-access", action="store_true", help="Reserved for later tasks.")
    parser.add_argument("--sync-server", action="store_true", help="Reserved for future manual server sync.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic details with secrets redacted.")
    parser.add_argument("--compact", dest="compact", action="store_true", default=None, help="One-off override: print compact context.")
    parser.add_argument("--no-compact", dest="compact", action="store_false", help="One-off override: print full context.")
    parser.add_argument("--status", nargs="?", const="__all__", help="Show cache status for all channels or one channel.")
    parser.add_argument("--validate-config", action="store_true", help="Validate config.json and alias routing safety.")
    return parser


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    with open_path(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_env_file(env_path: Path) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    with open_path(env_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_vars[key.strip()] = value.strip()
    return env_vars


def open_path(path: Path, mode: str, encoding: str | None = None):
    path_str = str(path.resolve())
    if path_str.startswith("\\\\?\\"):
        normalized_path = path_str
    elif path.is_absolute():
        normalized_path = "\\\\?\\" + path_str
    else:
        normalized_path = path_str

    if encoding is None:
        return open(normalized_path, mode)
    return open(normalized_path, mode, encoding=encoding)


def filesystem_path(path: Path) -> str:
    path_str = str(path.resolve())
    if path_str.startswith("\\\\?\\"):
        return path_str
    if path.is_absolute():
        return "\\\\?\\" + path_str
    return path_str


def remove_directory_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(filesystem_path(path))


def read_json_file(path: Path, default):
    if not path.exists():
        return default
    with open_path(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open_path(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def load_channel_status_state() -> dict:
    return read_json_file(CHANNEL_STATUS_PATH, {})


def save_channel_status_state(state: dict) -> None:
    write_json_file(CHANNEL_STATUS_PATH, state)


def append_last_run(run_entry: dict) -> None:
    runs = read_json_file(LAST_RUNS_PATH, [])
    if not isinstance(runs, list):
        runs = []
    runs.append(run_entry)
    write_json_file(LAST_RUNS_PATH, runs[-100:])


def current_timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def update_channel_run_status(channel_name: str, updates: dict) -> None:
    state = load_channel_status_state()
    current = state.get(channel_name, {})
    current.update(updates)
    state[channel_name] = current
    save_channel_status_state(state)


def record_run(command_name: str, status: str, details: dict) -> None:
    append_last_run(
        {
            "timestamp": current_timestamp(),
            "command": command_name,
            "status": status,
            "details": details,
        }
    )


def resolve_after_date(config: dict, since_days: int | None = None, after: str | None = None, since_hours: int | None = None) -> str:
    if after:
        try:
            datetime.fromisoformat(after)
        except ValueError as exc:
            raise ValueError("--after must be an ISO date such as YYYY-MM-DD") from exc
        return after
    if since_hours is not None:
        if since_hours <= 0:
            raise ValueError("since_hours must be a positive integer")
        after_date = datetime.now().astimezone() - timedelta(hours=since_hours)
        return after_date.replace(microsecond=0).isoformat()
    days = since_days if since_days is not None else config["default_time_range_days"]
    if days <= 0:
        raise ValueError("--since-days must be a positive integer")
    after_date = datetime.now().astimezone() - timedelta(days=days)
    return after_date.date().isoformat()


def requested_window_text(after_date: str, since_days: int | None = None) -> str:
    if since_days is not None:
        return f"after={after_date}, since_days={since_days}"
    return f"after={after_date}"


def requested_hours_window_text(after_date: str, since_hours: int) -> str:
    return f"after={after_date}, since_hours={since_hours}"


def parse_natural_since_days(text: str) -> int | None:
    match = re.search(r"(?:last|past)\s+(\d+)\s+days?", text.lower())
    if not match:
        return None
    return int(match.group(1))


def make_context_limits(config: dict, max_messages: int | None = None, max_chars: int | None = None) -> dict:
    limits = get_output_limits(config)
    if max_messages is not None:
        if max_messages <= 0:
            raise ValueError("--max-messages must be a positive integer")
        limits["max_total_snippets"] = max_messages
        limits["max_posts"] = max(limits["max_posts"], max_messages)
        limits["max_snippets_per_post"] = max(limits["max_snippets_per_post"], max_messages)
    if max_chars is not None:
        if max_chars <= 0:
            raise ValueError("--max-chars must be a positive integer")
        limits["max_output_chars"] = max_chars
    return limits


def tokenize_text(text: str) -> list[str]:
    return re.findall(r"[a-z0-9-]+", text.lower())


def get_output_limits(config: dict) -> dict:
    output_limits = config.get("output_limits", {})
    return {
        key: output_limits.get(key, default_value)
        for key, default_value in DEFAULT_OUTPUT_LIMITS.items()
    }


def merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def get_tool_defaults(config: dict) -> dict:
    legacy_defaults = {
        "default_context_window": {"since_days": config.get("default_time_range_days", 7)},
        "refresh_window": {"since_days": config.get("default_time_range_days", 7)},
        "stale_threshold_hours": config.get("cache_stale_after_hours", 24),
        "compact": True,
        "max_messages": get_output_limits(config)["max_total_snippets"],
        "max_chars": get_output_limits(config)["max_output_chars"],
        "output_language": "English",
        "auto_refresh_policy": "ask",
        "safety": {
            "export_timeout_seconds": DEFAULT_EXPORT_TIMEOUT_SECONDS,
            "server_parallel": 1,
            "stop_on_forbidden": True,
            "stop_on_unauthorized": True,
            "allow_auto_bootstrap": False,
            "retry_attempts": 0,
        },
    }
    return merge_dicts(merge_dicts(DEFAULT_TOOL_DEFAULTS, legacy_defaults), config.get("tool_defaults", {}))


def namespace_value(argparse_namespace, name: str, default=None):
    if isinstance(argparse_namespace, dict):
        return argparse_namespace.get(name, default)
    return getattr(argparse_namespace, name, default)


def resolve_window_from_config(config: dict, window_config: dict) -> tuple[str, str]:
    if not isinstance(window_config, dict):
        raise ValueError("window config must be an object")
    if "after" in window_config and window_config["after"]:
        after_date = resolve_after_date(config, None, window_config["after"])
        return after_date, f"after={after_date}"
    if "since_hours" in window_config:
        since_hours = int(window_config["since_hours"])
        after_date = resolve_after_date(config, None, None, since_hours)
        return after_date, requested_hours_window_text(after_date, since_hours)
    since_days = int(window_config.get("since_days", config.get("default_time_range_days", 7)))
    after_date = resolve_after_date(config, since_days, None)
    return after_date, requested_window_text(after_date, since_days)


def resolve_effective_config(config: dict, channel_config: dict, argparse_namespace) -> dict:
    defaults = get_tool_defaults(config)
    channel_overrides = channel_config.get("overrides", {}) if isinstance(channel_config, dict) else {}
    effective = merge_dicts(defaults, channel_overrides)

    cli_since_days = namespace_value(argparse_namespace, "since_days")
    cli_after = namespace_value(argparse_namespace, "after")
    if cli_since_days is not None or cli_after:
        context_after = resolve_after_date(config, cli_since_days, cli_after)
        refresh_after = context_after
        context_window = requested_window_text(context_after, cli_since_days)
        refresh_window = requested_window_text(refresh_after, cli_since_days)
    else:
        context_after, context_window = resolve_window_from_config(config, effective["default_context_window"])
        refresh_after, refresh_window = resolve_window_from_config(config, effective["refresh_window"])

    compact_arg = namespace_value(argparse_namespace, "compact")
    timeout_arg = namespace_value(argparse_namespace, "timeout_seconds")
    max_messages_arg = namespace_value(argparse_namespace, "max_messages")
    max_chars_arg = namespace_value(argparse_namespace, "max_chars")
    verbose_arg = namespace_value(argparse_namespace, "verbose", False)

    max_messages = max_messages_arg if max_messages_arg is not None else effective["max_messages"]
    max_chars = max_chars_arg if max_chars_arg is not None else effective["max_chars"]
    compact = compact_arg if compact_arg is not None else effective["compact"]
    timeout_seconds = timeout_arg if timeout_arg is not None else effective["safety"]["export_timeout_seconds"]

    return {
        "channel_name": channel_config.get("name", "unknown"),
        "channel_id": channel_config.get("id", "unknown"),
        "context_after": context_after,
        "refresh_after": refresh_after,
        "context_window": context_window,
        "refresh_window": refresh_window,
        "max_messages": int(max_messages),
        "max_chars": int(max_chars),
        "compact": bool(compact),
        "output_language": effective["output_language"],
        "auto_refresh_policy": effective["auto_refresh_policy"],
        "safety": effective["safety"],
        "timeout_seconds": int(timeout_seconds),
        "stale_threshold_hours": int(effective["stale_threshold_hours"]),
        "include_threads": resolve_include_threads(channel_config),
        "verbose": bool(verbose_arg),
        "cache_freshness": "unknown",
        "caveats": [],
    }


def build_effective_config_output(effective: dict) -> str:
    lines = [
        "effective config:",
        f"resolved channel: {effective['channel_name']}",
        f"resolved channel id: {effective.get('channel_id', 'unknown')}",
        f"resolved context window: {effective['context_window']}",
        f"resolved refresh window: {effective['refresh_window']}",
        f"max_messages: {effective['max_messages']}",
        f"max_chars: {effective['max_chars']}",
        f"compact mode: {effective['compact']}",
        f"timeout_seconds: {effective['timeout_seconds']}",
        f"stale_threshold_hours: {effective['stale_threshold_hours']}",
        f"include_threads: {effective['include_threads']}",
        f"output_language: {effective.get('output_language', 'English')}",
        f"auto_refresh_policy: {effective.get('auto_refresh_policy', 'ask')}",
        f"cache freshness: {effective.get('cache_freshness', 'unknown')}",
    ]
    caveats = effective.get("caveats") or []
    if caveats:
        lines.append("caveats/warnings:")
        lines.extend(f"- {caveat}" for caveat in caveats)
    else:
        lines.append("caveats/warnings: none")
    return "\n".join(lines)


def normalize_alias(value: str) -> str:
    return value.strip().lower()


def list_enabled_channels(config: dict) -> list[dict]:
    return [channel for channel in list_channels(config) if channel.get("enabled", True)]


def validate_alias_rules(config: dict) -> None:
    allowed_generic_aliases = {
        normalize_alias(value)
        for value in config.get("allowed_generic_aliases", [])
        if isinstance(value, str) and value.strip()
    }
    seen_aliases: dict[str, str] = {}
    seen_channel_ids: dict[str, str] = {}

    enabled_channels = list_enabled_channels(config)
    if not enabled_channels:
        raise ValueError("Config must include at least one enabled channel")

    for channel in enabled_channels:
        channel_name = channel["name"]
        channel_id = channel["id"]

        if channel_id in seen_channel_ids:
            raise ValueError(
                f"Duplicate enabled channel id detected: {channel_id} ({seen_channel_ids[channel_id]}, {channel_name})"
            )
        seen_channel_ids[channel_id] = channel_name

        candidate_aliases = [channel_name, *channel["aliases"]]
        for alias in candidate_aliases:
            normalized_alias = normalize_alias(alias)
            if not normalized_alias:
                raise ValueError(f"Channel {channel_name} has an empty alias")
            if normalized_alias in DANGEROUS_GENERIC_ALIASES and normalized_alias not in allowed_generic_aliases:
                raise ValueError(
                    f"Channel {channel_name} uses dangerously generic alias '{alias}'"
                )
            if normalized_alias in seen_aliases and seen_aliases[normalized_alias] != channel_name:
                raise ValueError(
                    f"Duplicate enabled alias detected (case-insensitive): '{alias}' is used by {seen_aliases[normalized_alias]} and {channel_name}"
                )
            seen_aliases[normalized_alias] = channel_name


def parse_iso_datetime(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_config(config: dict) -> None:
    required_top_level_keys = [
        "server_name",
        "server_id",
        "default_time_range_days",
        "discord_exporter_path",
        "env_path",
        "token_env_var",
        "channels",
    ]
    missing_top_level_keys = [key for key in required_top_level_keys if key not in config]
    if missing_top_level_keys:
        raise ValueError(f"Missing config keys: {', '.join(missing_top_level_keys)}")

    if config["server_name"] != "AI Performance Engineering IL":
        raise ValueError("Unexpected server_name in config.json")

    if "cache_stale_after_hours" not in config:
        raise ValueError("Config must include cache_stale_after_hours")
    if not isinstance(config["cache_stale_after_hours"], int) or config["cache_stale_after_hours"] <= 0:
        raise ValueError("cache_stale_after_hours must be a positive integer")
    if not isinstance(config["token_env_var"], str) or not config["token_env_var"].strip():
        raise ValueError("token_env_var must be a non-empty string")
    if "output_limits" not in config:
        raise ValueError("Config must include output_limits")
    output_limits = config["output_limits"]
    if not isinstance(output_limits, dict):
        raise ValueError("output_limits must be an object")
    for key, default_value in DEFAULT_OUTPUT_LIMITS.items():
        if key not in output_limits:
            raise ValueError(f"output_limits must include {key}")
        if key in output_limits and (not isinstance(output_limits[key], int) or output_limits[key] <= 0):
            raise ValueError(f"output_limits.{key} must be a positive integer")

    if "tool_defaults" not in config:
        raise ValueError("Config must include tool_defaults")
    tool_defaults = get_tool_defaults(config)
    for key in ("default_context_window", "refresh_window", "stale_threshold_hours", "compact", "max_messages", "max_chars", "output_language", "auto_refresh_policy", "safety"):
        if key not in tool_defaults:
            raise ValueError(f"tool_defaults must include {key}")
    for key in ("default_context_window", "refresh_window"):
        if not isinstance(tool_defaults[key], dict):
            raise ValueError(f"tool_defaults.{key} must be an object")
        resolve_window_from_config(config, tool_defaults[key])
    if tool_defaults["auto_refresh_policy"] not in {"ask", "never", "stale_or_missing"}:
        raise ValueError("tool_defaults.auto_refresh_policy must be one of: ask, never, stale_or_missing")
    if not isinstance(tool_defaults["compact"], bool):
        raise ValueError("tool_defaults.compact must be a boolean")
    for key in ("stale_threshold_hours", "max_messages", "max_chars"):
        if not isinstance(tool_defaults[key], int) or tool_defaults[key] <= 0:
            raise ValueError(f"tool_defaults.{key} must be a positive integer")
    safety = tool_defaults["safety"]
    for key in ("export_timeout_seconds", "server_parallel", "retry_attempts"):
        if not isinstance(safety.get(key), int) or safety[key] < 0:
            raise ValueError(f"tool_defaults.safety.{key} must be a non-negative integer")

    exporter_path = Path(config["discord_exporter_path"])
    if not exporter_path.exists():
        raise ValueError(f"Configured exporter path does not exist: {exporter_path}")

    env_path = Path(config["env_path"])
    if not env_path.exists():
        raise ValueError(f"Configured env path does not exist: {env_path}")

    channels = config["channels"]
    if not isinstance(channels, list) or not channels:
        raise ValueError("Config channels must be a non-empty list")

    for channel in channels:
        for key in ("name", "aliases", "id", "enabled"):
            if key not in channel:
                raise ValueError(f"Channel is missing required key: {key}")
        if not isinstance(channel["name"], str) or not channel["name"].strip():
            raise ValueError("Channel name must be a non-empty string")
        if not isinstance(channel["id"], str) or not channel["id"].strip():
            raise ValueError(f"Channel id must be a non-empty string for {channel['name']}")
        if not isinstance(channel["aliases"], list) or not channel["aliases"]:
            raise ValueError(f"Channel aliases must be a non-empty list for {channel['name']}")
        if not isinstance(channel["enabled"], bool):
            raise ValueError(f"Channel enabled must be a boolean for {channel['name']}")
        for alias in channel["aliases"]:
            if not isinstance(alias, str):
                raise ValueError(f"Channel aliases must be strings for {channel['name']}")
            if not alias.strip():
                raise ValueError(f"Channel {channel['name']} has an empty alias")

    validate_alias_rules(config)


def build_config_validation_report(config: dict) -> str:
    enabled_channels = list_enabled_channels(config)
    lines = [
        "config validation: PASS",
        f"config path: {CONFIG_PATH}",
        "json: PASS",
        f"exporter path exists: PASS ({config['discord_exporter_path']})",
        f"env path exists: PASS ({config['env_path']})",
        f"token env var name configured: PASS ({config['token_env_var']})",
        f"cache setting cache_stale_after_hours: PASS ({config['cache_stale_after_hours']})",
        "output_limits: PASS",
    ]
    for key, value in get_output_limits(config).items():
        lines.append(f"  - {key}: {value}")
    tool_defaults = get_tool_defaults(config)
    context_after, context_window = resolve_window_from_config(config, tool_defaults["default_context_window"])
    refresh_after, refresh_window = resolve_window_from_config(config, tool_defaults["refresh_window"])
    lines.extend([
        "tool_defaults: PASS",
        f"  - default_context_window: {context_window}",
        f"  - refresh_window: {refresh_window}",
        f"  - stale_threshold_hours: {tool_defaults['stale_threshold_hours']}",
        f"  - compact: {tool_defaults['compact']}",
        f"  - max_messages: {tool_defaults['max_messages']}",
        f"  - max_chars: {tool_defaults['max_chars']}",
        f"  - output_language: {tool_defaults['output_language']}",
        f"  - auto_refresh_policy: {tool_defaults['auto_refresh_policy']}",
        f"  - safety.export_timeout_seconds: {tool_defaults['safety']['export_timeout_seconds']}",
    ])
    lines.extend([
        f"enabled channels: PASS ({len(enabled_channels)})",
        "channel ids unique across enabled channels: PASS",
        "alias uniqueness (case-insensitive): PASS",
        "alias precision: PASS",
        "resolver ambiguity policy: PASS (ambiguous prompts raise a clear error; resolver does not guess)",
    ])
    for channel in enabled_channels:
        aliases = ", ".join(channel["aliases"])
        lines.append(
            f"channel: {channel['name']} | id: {channel['id']} | enabled: {channel['enabled']} | aliases: {aliases}"
        )
    return "\n".join(lines)


def run_validate_config_command() -> int:
    try:
        config = load_config()
        validate_config(config)
    except json.JSONDecodeError as exc:
        emit_text(
            "\n".join(
                [
                    "config validation: FAIL",
                    f"config path: {CONFIG_PATH}",
                    f"json: FAIL ({exc})",
                ]
            )
        )
        return 1
    except ValueError as exc:
        emit_text(
            "\n".join(
                [
                    "config validation: FAIL",
                    f"config path: {CONFIG_PATH}",
                    f"error: {exc}",
                ]
            )
        )
        return 1

    emit_text(build_config_validation_report(config))
    return 0


def list_channels(config: dict) -> list[dict]:
    return config["channels"]


def get_discord_token(config: dict) -> str:
    env_path = Path(config["env_path"])
    env_vars = load_env_file(env_path)
    token_env_var = config["token_env_var"]

    if token_env_var not in env_vars:
        raise ValueError(f"Missing token env var in env file: {token_env_var}")

    token = env_vars[token_env_var].strip().strip('"').strip("'").strip()
    if not token:
        raise ValueError(f"Configured token env var is empty after trimming: {token_env_var}")
    return token


def resolve_channel_alias(alias: str, config: dict) -> str:
    normalized_alias = normalize_alias(alias)
    for channel in list_enabled_channels(config):
        aliases = [channel["name"], *channel["aliases"]]
        if normalized_alias in {normalize_alias(item) for item in aliases}:
            return channel["name"]
    raise ValueError(
        f"Unknown channel/topic: {alias}. Available channels: {format_available_channels(config)}"
    )


def format_available_channels(config: dict) -> str:
    return ", ".join(channel["name"] for channel in list_enabled_channels(config))


def get_channel_config(channel_name: str, config: dict) -> dict:
    for channel in list_enabled_channels(config):
        if channel["name"] == channel_name:
            return channel
    raise ValueError(
        f"Unknown channel/topic: {channel_name}. Available channels: {format_available_channels(config)}"
    )


def resolve_channel(prompt_or_topic: str, config: dict) -> str:
    normalized_input = prompt_or_topic.strip().lower()

    exact_matches: list[str] = []
    partial_matches: list[str] = []

    for channel in list_enabled_channels(config):
        candidate_values = [channel["name"], *channel["aliases"]]
        normalized_values = {normalize_alias(value) for value in candidate_values}

        if normalized_input in normalized_values:
            exact_matches.append(channel["name"])
            continue

        for value in normalized_values:
            pattern = r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])"
            if re.search(pattern, normalized_input):
                partial_matches.append(channel["name"])
                break

    matches = sorted(set(exact_matches or partial_matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            "Ambiguous channel/topic match: "
            + ", ".join(matches)
            + ". Please specify one of: "
            + format_available_channels(config)
        )
    raise ValueError(
        f"Unknown channel/topic: {prompt_or_topic}. Available channels: {format_available_channels(config)}"
    )


def raw_path_for_channel(channel_name: str) -> Path:
    return CACHE_ROOT / "raw" / channel_name


def partial_raw_path_for_channel(channel_name: str) -> Path:
    safe_timestamp = current_timestamp().replace(":", "").replace("+", "_")
    return CACHE_ROOT / "raw" / ".partial" / f"{channel_name}-{safe_timestamp}"


def normalized_path_for_channel(channel_name: str) -> Path:
    return CACHE_ROOT / "normalized" / f"{channel_name}.json"


def load_normalized_cache(channel_name: str) -> dict:
    cache_path = normalized_path_for_channel(channel_name)
    if not cache_path.exists():
        raise ValueError(f"cache is missing; run --refresh-channel {channel_name} first")
    with open_path(cache_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def format_datetime_value(dt) -> str:
    if dt is None:
        return "unknown"
    return dt.isoformat()


def format_age(delta: timedelta | None) -> str:
    if delta is None:
        return "unknown"
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m"


def extract_topic_keywords(prompt: str, channel_name: str, config: dict) -> list[str]:
    normalized_prompt = prompt.lower()
    if " about " not in normalized_prompt:
        return []

    topic_source = normalized_prompt.split(" about ", 1)[1]
    ignored_tokens = set(COMMON_FILTER_WORDS)
    channel_config = get_channel_config(channel_name, config)

    for value in [channel_config["name"], *channel_config["aliases"]]:
        ignored_tokens.update(tokenize_text(value))

    keywords: list[str] = []
    for token in tokenize_text(topic_source):
        if token in ignored_tokens:
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


def build_after_date(config: dict) -> str:
    return resolve_after_date(config)


def get_cache_stale_after(config: dict) -> timedelta:
    tool_defaults = get_tool_defaults(config)
    if "stale_threshold_hours" in tool_defaults:
        return timedelta(hours=tool_defaults["stale_threshold_hours"])
    if "cache_stale_after_hours" in config:
        return timedelta(hours=config["cache_stale_after_hours"])
    return timedelta(minutes=config["cache_ttl_minutes"])


def resolve_include_threads(channel_config: dict) -> str:
    configured = channel_config.get("include_threads")
    if isinstance(configured, bool):
        return "Active" if configured else "None"
    if isinstance(configured, str) and configured in {"None", "Active", "All"}:
        return configured
    if channel_config.get("type") == "forum":
        return "Active"
    return "None"


def build_export_channel_command(channel_config: dict, config: dict, token: str, after_date: str, output_dir: Path) -> list[str]:
    output_arg = str(output_dir) + "\\"
    return [
        config["discord_exporter_path"],
        "export",
        "-c",
        channel_config["id"],
        "-t",
        token,
        "-f",
        "Json",
        "-o",
        output_arg,
        "--after",
        after_date,
        "--include-threads",
        resolve_include_threads(channel_config),
    ]


def redact_command(command: list[str]) -> str:
    redacted = list(command)
    for index, value in enumerate(redacted[:-1]):
        if value in {"-t", "--token"}:
            redacted[index + 1] = "<redacted>"
    return " ".join(str(part) for part in redacted)


def export_channel(channel_config: dict, config: dict, token: str, after_date: str | None = None, timeout_seconds: int = DEFAULT_EXPORT_TIMEOUT_SECONDS, verbose: bool = False) -> dict:
    output_dir = raw_path_for_channel(channel_config["name"])
    partial_output_dir = partial_raw_path_for_channel(channel_config["name"])
    if partial_output_dir.exists():
        remove_directory_tree(partial_output_dir)
    partial_output_dir.mkdir(parents=True, exist_ok=True)

    active_after_date = after_date or build_after_date(config)
    command = build_export_channel_command(channel_config, config, token, active_after_date, partial_output_dir)
    if verbose:
        emit_text("exporter command: " + redact_command(command))
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds)
    combined_output = f"{completed.stdout}\n{completed.stderr}".lower()

    if "forbidden" in combined_output:
        raise RuntimeError("DiscordChatExporter export failed: forbidden")
    if "unauthorized" in combined_output:
        raise RuntimeError("DiscordChatExporter export failed: unauthorized")
    if completed.returncode != 0:
        raise RuntimeError(
            f"DiscordChatExporter export failed with return code {completed.returncode}"
        )

    json_files = sorted(partial_output_dir.rglob("*.json"))
    if not json_files:
        raise RuntimeError("DiscordChatExporter export completed but no JSON files were created")

    if output_dir.exists():
        remove_directory_tree(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(filesystem_path(partial_output_dir), filesystem_path(output_dir))
    json_files = sorted(output_dir.rglob("*.json"))

    return {
        "channel": channel_config["name"],
        "channel_id": channel_config["id"],
        "after": active_after_date,
        "output_dir": str(output_dir),
        "json_file_count": len(json_files),
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def server_bootstrap_path() -> Path:
    return CACHE_ROOT / "raw" / "server-bootstrap"


def export_server_bootstrap(config: dict, token: str, after_date: str, timeout_seconds: int = DEFAULT_EXPORT_TIMEOUT_SECONDS) -> dict:
    output_dir = server_bootstrap_path()
    if output_dir.exists():
        remove_directory_tree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_arg = str(output_dir) + "\\"
    command = [
        config["discord_exporter_path"],
        "exportguild",
        "-g",
        config["server_id"],
        "-t",
        token,
        "-f",
        "Json",
        "-o",
        output_arg,
        "--after",
        after_date,
        "--include-threads",
        "Active",
        "--parallel",
        "1",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds)
        timed_out = False
        stdout = completed.stdout
        stderr = completed.stderr
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return_code = -1

    combined_output = f"{stdout}\n{stderr}".lower()
    if "forbidden" in combined_output:
        raise RuntimeError("DiscordChatExporter exportguild failed: forbidden")
    if "unauthorized" in combined_output:
        raise RuntimeError("DiscordChatExporter exportguild failed: unauthorized")
    if return_code not in (0, -1):
        raise RuntimeError(f"DiscordChatExporter exportguild failed with return code {return_code}")

    json_files = sorted(output_dir.rglob("*.json"))
    if not json_files:
        raise RuntimeError("DiscordChatExporter exportguild produced no JSON files")

    return {
        "after": after_date,
        "output_dir": str(output_dir),
        "json_file_count": len(json_files),
        "return_code": return_code,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
    }


def copy_bootstrap_files_for_configured_channels(config: dict) -> dict:
    bootstrap_dir = server_bootstrap_path()
    matched: dict[str, list[Path]] = {
        channel["name"]: []
        for channel in list_enabled_channels(config)
    }

    for json_file in bootstrap_dir.rglob("*.json"):
        try:
            with open_path(json_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        channel_data = data.get("channel") or {}
        source_id = str(channel_data.get("id") or "")
        source_name = str(channel_data.get("name") or "")
        source_category = str(channel_data.get("category") or "")
        for channel in list_enabled_channels(config):
            if source_id == channel["id"] or source_name == channel["name"] or source_category == channel["name"]:
                matched[channel["name"]].append(json_file)

    normalized_results: dict[str, dict] = {}
    for channel in list_enabled_channels(config):
        channel_name = channel["name"]
        files = matched[channel_name]
        if not files:
            continue
        raw_dir = raw_path_for_channel(channel_name)
        if raw_dir.exists():
            remove_directory_tree(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        for source_file in files:
            shutil.copy2(source_file, raw_dir / source_file.name)
        normalized_results[channel_name] = normalize_channel_export(raw_dir, channel)
    return normalized_results


def normalize_attachment(attachment: dict) -> dict:
    return {
        "fileName": attachment.get("fileName"),
        "url": attachment.get("url"),
        "fileSizeBytes": attachment.get("fileSizeBytes"),
    }


def normalize_reaction(reaction: dict) -> dict:
    emoji = reaction.get("emoji") or {}
    return {
        "emoji_name": emoji.get("name"),
        "emoji_code": emoji.get("code"),
        "count": reaction.get("count", 0),
    }


def normalize_message(message: dict) -> dict:
    author = message.get("author") or {}
    author_display_name = author.get("nickname") or author.get("name")
    attachments = message.get("attachments") or []
    reactions = message.get("reactions") or []

    return {
        "id": message.get("id"),
        "type": message.get("type"),
        "timestamp": message.get("timestamp"),
        "author_display_name": author_display_name,
        "content": message.get("content"),
        "is_pinned": message.get("isPinned", False),
        "attachments": [
            normalize_attachment(attachment)
            for attachment in attachments
            if isinstance(attachment, dict)
        ],
        "reactions": [
            normalize_reaction(reaction)
            for reaction in reactions
            if isinstance(reaction, dict)
        ],
    }


def normalize_channel_export(raw_dir: Path, channel_config: dict) -> dict:
    json_files = sorted(raw_dir.rglob("*.json"))
    normalized_posts: list[dict] = []
    warnings: list[str] = []
    total_messages = 0

    for json_file in json_files:
        try:
            with open_path(json_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            warnings.append(f"Failed to read {json_file.name}: {exc}")
            continue

        channel_data = data.get("channel") or {}
        raw_messages = data.get("messages") or []
        normalized_messages = [
            normalize_message(message)
            for message in raw_messages
            if isinstance(message, dict)
        ]
        total_messages += len(normalized_messages)

        normalized_posts.append(
            {
                "post_id": channel_data.get("id"),
                "post_name": channel_data.get("name"),
                "channel_name": channel_data.get("category") or channel_config["name"],
                "exported_at": data.get("exportedAt"),
                "date_range": data.get("dateRange"),
                "messages": normalized_messages,
                "source_file": json_file.name,
            }
        )

    normalized_output = {
        "channel": channel_config["name"],
        "channel_id": channel_config["id"],
        "raw_directory": str(raw_dir),
        "raw_json_files_read": len(json_files),
        "posts_count": len(normalized_posts),
        "total_messages": total_messages,
        "normalized_posts": normalized_posts,
        "warnings": warnings,
    }

    output_path = normalized_path_for_channel(channel_config["name"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open_path(output_path, "w", encoding="utf-8") as handle:
        json.dump(normalized_output, handle, ensure_ascii=False, indent=2)

    return normalized_output


def message_matches_keywords(message: dict, keywords: list[str]) -> bool:
    content = (message.get("content") or "").lower()
    attachment_names = " ".join(
        (attachment.get("fileName") or "").lower()
        for attachment in message.get("attachments", [])
        if isinstance(attachment, dict)
    )
    search_text = f"{content} {attachment_names}"
    return any(keyword in search_text for keyword in keywords)


def filter_cache_data(cache_data: dict, keywords: list[str]) -> dict:
    if not keywords:
        return cache_data

    filtered_posts: list[dict] = []
    total_messages = 0

    for post in cache_data.get("normalized_posts", []):
        post_name = (post.get("post_name") or "").lower()
        if any(keyword in post_name for keyword in keywords):
            copied_messages = list(post.get("messages", []))
            filtered_posts.append({**post, "messages": copied_messages})
            total_messages += len(copied_messages)
            continue

        matching_messages = [
            message
            for message in post.get("messages", [])
            if message_matches_keywords(message, keywords)
        ]
        if matching_messages:
            filtered_posts.append({**post, "messages": matching_messages})
            total_messages += len(matching_messages)

    return {
        **cache_data,
        "posts_count": len(filtered_posts),
        "total_messages": total_messages,
        "normalized_posts": filtered_posts,
    }


def filter_cache_data_by_after(cache_data: dict, after_date: str | None) -> dict:
    if not after_date:
        return cache_data

    after_dt = parse_iso_datetime(after_date)
    if after_dt is None:
        after_dt = datetime.fromisoformat(after_date).astimezone()
    elif after_dt.tzinfo is None:
        after_dt = after_dt.astimezone()

    filtered_posts: list[dict] = []
    total_messages = 0
    for post in cache_data.get("normalized_posts", []):
        filtered_messages = []
        for message in post.get("messages", []):
            message_dt = parse_iso_datetime(message.get("timestamp"))
            if message_dt is None:
                continue
            if message_dt.tzinfo is None:
                message_dt = message_dt.astimezone()
            if message_dt >= after_dt:
                filtered_messages.append(message)
        if filtered_messages:
            filtered_posts.append({**post, "messages": filtered_messages})
            total_messages += len(filtered_messages)

    return {
        **cache_data,
        "posts_count": len(filtered_posts),
        "total_messages": total_messages,
        "normalized_posts": filtered_posts,
    }


def cache_window_warning(cache_data: dict, requested_after: str | None) -> str | None:
    if not requested_after:
        return None
    metadata = derive_cache_metadata(cache_data)
    cached_after = metadata.get("after")
    if not cached_after:
        return "warning: cached export window is unknown; requested context may be incomplete."
    try:
        cached_after_dt = datetime.fromisoformat(cached_after)
        requested_after_dt = datetime.fromisoformat(requested_after)
    except ValueError:
        return "warning: could not compare cached export window with requested window."
    if cached_after_dt.tzinfo is None:
        cached_after_dt = cached_after_dt.astimezone()
    if requested_after_dt.tzinfo is None:
        requested_after_dt = requested_after_dt.astimezone()
    if cached_after_dt > requested_after_dt:
        return f"warning: cache starts at {cached_after}; requested context starts at {requested_after}, so older requested messages may be missing."
    return None


def derive_cache_metadata(cache_data: dict) -> dict:
    posts = cache_data.get("normalized_posts", [])
    exported_values = [post.get("exported_at") for post in posts if post.get("exported_at")]
    date_ranges = [post.get("date_range") for post in posts if isinstance(post.get("date_range"), dict)]
    after_values = [item.get("after") for item in date_ranges if item.get("after")]
    before_values = [item.get("before") for item in date_ranges if item.get("before")]
    return {
        "exported_at": max(exported_values) if exported_values else None,
        "after": min(after_values) if after_values else None,
        "before": max(before_values) if before_values else None,
    }


def derive_latest_message_timestamp(cache_data: dict) -> str | None:
    timestamps = []
    for post in cache_data.get("normalized_posts", []):
        for message in post.get("messages", []):
            timestamp = message.get("timestamp")
            if timestamp:
                timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def get_channel_cache_status(channel_config: dict, config: dict) -> dict:
    channel_name = channel_config["name"]
    raw_dir = raw_path_for_channel(channel_name)
    normalized_path = normalized_path_for_channel(channel_name)
    raw_json_files = sorted(raw_dir.rglob("*.json")) if raw_dir.exists() else []

    normalized_exists = normalized_path.exists()
    normalized_data = None
    if normalized_exists:
        with open_path(normalized_path, "r", encoding="utf-8") as handle:
            normalized_data = json.load(handle)

    exported_at_raw = None
    latest_message_raw = None
    posts_count = 0
    messages_count = 0

    if normalized_data:
        metadata = derive_cache_metadata(normalized_data)
        exported_at_raw = metadata["exported_at"]
        latest_message_raw = derive_latest_message_timestamp(normalized_data)
        posts_count = normalized_data.get("posts_count", len(normalized_data.get("normalized_posts", [])))
        messages_count = normalized_data.get("total_messages", 0)

    exported_at_dt = parse_iso_datetime(exported_at_raw)
    latest_message_dt = parse_iso_datetime(latest_message_raw)
    now = datetime.now().astimezone()
    cache_age = (now - exported_at_dt) if exported_at_dt else None
    stale_after = get_cache_stale_after(config)

    effective = resolve_effective_config(config, channel_config, {})
    coverage_warning = cache_window_warning(normalized_data, effective["context_after"]) if normalized_data else None

    if normalized_exists and coverage_warning:
        status = "incomplete"
        recommended_action = (
            f"Cache does not fully cover the requested context window; run python discord_cli.py --refresh-channel {channel_name}. "
            f"{coverage_warning}"
        )
    elif normalized_exists and cache_age is not None and cache_age > stale_after:
        status = "stale"
        recommended_action = f"Cache is stale; consider running python discord_cli.py --refresh-channel {channel_name}"
    elif normalized_exists:
        status = "ok"
        recommended_action = f"Use existing normalized cache for {channel_name}"
    else:
        status = "missing"
        recommended_action = f"Run python discord_cli.py --refresh-channel {channel_name}"

    notes: list[str] = []
    if raw_dir.exists() and not normalized_exists:
        notes.append("Raw cache exists but normalized cache is missing.")
        recommended_action = f"Run python discord_cli.py --refresh-channel {channel_name}"
    if normalized_exists and not raw_dir.exists():
        notes.append("Normalized cache exists and runtime can still use it, but raw cache is missing.")
        if status == "ok":
            recommended_action = f"Use normalized cache for {channel_name}; refresh manually only if needed"
    if not raw_dir.exists() and not normalized_exists:
        notes.append("Raw cache and normalized cache are missing.")

    state_entry = load_channel_status_state().get(channel_name, {})
    effective["cache_freshness"] = status
    caveats = []
    if notes:
        caveats.extend(notes)
    if coverage_warning:
        caveats.append(coverage_warning)
    if state_entry.get("last_warning"):
        caveats.append(state_entry["last_warning"])
    if state_entry.get("last_error"):
        caveats.append(f"last error: {state_entry['last_error']}")
    effective["caveats"] = caveats

    return {
        "channel_name": channel_name,
        "channel_id": channel_config["id"],
        "raw_cache_exists": raw_dir.exists(),
        "raw_cache_path": str(raw_dir),
        "raw_json_file_count": len(raw_json_files),
        "normalized_cache_exists": normalized_exists,
        "normalized_cache_path": str(normalized_path),
        "posts_count": posts_count,
        "messages_count": messages_count,
        "latest_exported_at": exported_at_raw or "unknown",
        "latest_message_timestamp": latest_message_raw or "unknown",
        "cache_age": format_age(cache_age),
        "status": status,
        "recommended_action": recommended_action,
        "notes": notes,
        "stale_after": format_age(stale_after),
        "exported_at_dt": exported_at_dt,
        "latest_message_dt": latest_message_dt,
        "last_attempted_refresh": state_entry.get("last_attempted_refresh", "unknown"),
        "last_successful_refresh": state_entry.get("last_successful_refresh", "unknown"),
        "requested_window": state_entry.get("requested_window", metadata.get("after") if normalized_data else "unknown"),
        "partial_export": state_entry.get("partial_export", False),
        "last_error": state_entry.get("last_error", ""),
        "last_warning": state_entry.get("last_warning", ""),
        "export_source": state_entry.get("export_source", "unknown"),
        "effective_config": effective,
    }


def build_status_output(status_entries: list[dict]) -> str:
    lines = []
    for entry in status_entries:
        lines.extend([
            f"channel: {entry['channel_name']}",
            f"channel id: {entry['channel_id']}",
        ])
        if "effective_config" in entry:
            lines.append(build_effective_config_output(entry["effective_config"]))
        lines.extend([
            f"raw cache exists: {entry['raw_cache_exists']}",
            f"raw cache path: {entry['raw_cache_path']}",
            f"raw json files: {entry['raw_json_file_count']}",
            f"normalized cache exists: {entry['normalized_cache_exists']}",
            f"normalized cache path: {entry['normalized_cache_path']}",
            f"posts/threads count: {entry['posts_count']}",
            f"message count: {entry['messages_count']}",
            f"latest exported_at: {entry['latest_exported_at']}",
            f"last message timestamp: {entry['latest_message_timestamp']}",
            f"last attempted refresh: {entry.get('last_attempted_refresh', 'unknown')}",
            f"last successful refresh: {entry.get('last_successful_refresh', 'unknown')}",
            f"current cached time window: {entry.get('requested_window', 'unknown')}",
            f"export source: {entry.get('export_source', 'unknown')}",
            f"partial export: {entry.get('partial_export', False)}",
            f"last error: {entry.get('last_error', '') or 'none'}",
            f"last warning: {entry.get('last_warning', '') or 'none'}",
            f"cache age: {entry['cache_age']}",
            f"stale after: {entry['stale_after']}",
            f"status: {entry['status']}",
            f"recommended action: {entry['recommended_action']}",
        ])
        for note in entry["notes"]:
            lines.append(f"note: {note}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_agent_summary_instructions() -> list[str]:
    return [
        "agent summary instructions:",
        "- Answer in English.",
        "- Use only the Discord evidence in this context.",
        "- Do not invent information.",
        "- Separate facts from conclusions.",
        "- Mention uncertainty when data is incomplete, stale, partial, or truncated.",
        "- Treat attachments as metadata only unless their content is visible in message text.",
        "- Preserve technical terms such as MLOps, LLM, deployment, inference, Kubernetes, cache, pipeline, CLI, and tenant.",
        "- Do not only list topics; explain what happened, what practical answer was given, and why it matters.",
        "- For forum channels, summarize per post/thread before writing the global takeaways.",
        "- Prefer practical answer bullets: concrete commands, config changes, decisions, deadlines, blockers, and next actions.",
        "- Include the source window and caveats.",
        "",
        "Use exactly these sections:",
        "# TL;DR",
        "# What Changed",
        "# Decisions",
        "# Action Items",
        "# Open Questions",
        "# Risks / Caveats",
        "# Source Window",
    ]


def build_hebrew_summary_instructions() -> list[str]:
    return [
        "legacy hebrew answer instructions:",
        "- Deprecated: use build_agent_summary_instructions() for current CLI context output.",
        "- Answer in Hebrew if explicitly requested by the user.",
        "- Do not invent information.",
    ]


def truncate_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def message_reaction_count(message: dict) -> int:
    return sum(
        reaction.get("count", 0)
        for reaction in message.get("reactions", [])
        if isinstance(reaction, dict)
    )


def summarize_attachments(message: dict) -> str | None:
    attachments = [
        attachment
        for attachment in message.get("attachments", [])
        if isinstance(attachment, dict)
    ]
    if not attachments:
        return None
    return ", ".join(
        attachment.get("fileName") or attachment.get("url") or "attachment"
        for attachment in attachments
    )


def is_meaningful_message(message: dict) -> bool:
    content = (message.get("content") or "").strip()
    return bool(content or summarize_attachments(message))


def select_post_snippets(post: dict, keywords: list[str], remaining_slots: int, max_snippets_per_post: int) -> list[dict]:
    if remaining_slots <= 0:
        return []

    messages = post.get("messages", [])
    if not messages:
        return []

    selected_ids: set[str | None] = set()
    selected: list[dict] = []

    def add_message(message: dict) -> None:
        message_id = message.get("id")
        if message_id in selected_ids or not is_meaningful_message(message):
            return
        selected_ids.add(message_id)
        selected.append(message)

    if keywords:
        for message in messages:
            if message_matches_keywords(message, keywords):
                add_message(message)
                if len(selected) >= min(max_snippets_per_post, remaining_slots):
                    return selected

    for message in messages:
        if message.get("is_pinned"):
            add_message(message)
            if len(selected) >= min(max_snippets_per_post, remaining_slots):
                return selected

    for message in sorted(messages, key=message_reaction_count, reverse=True):
        if message_reaction_count(message) > 0:
            add_message(message)
            if len(selected) >= min(max_snippets_per_post, remaining_slots):
                return selected

    for message in messages:
        add_message(message)
        if len(selected) >= min(max_snippets_per_post, remaining_slots):
            return selected

    return selected


def latest_post_timestamp(post: dict) -> str:
    timestamps = [
        message.get("timestamp")
        for message in post.get("messages", [])
        if message.get("timestamp")
    ]
    return max(timestamps) if timestamps else (post.get("exported_at") or "unknown")


def build_truncation_notice(channel_name: str) -> list[str]:
    return [
        "truncation notice: output was limited to reduce context size.",
        'Suggestion: use a more specific prompt, for example: python discord_cli.py --prompt "Update me on MLOps about tenant. Answer in Hebrew." --compact',
        f"channel: {channel_name}",
    ]


def build_full_output_warning(channel_name: str, max_output_chars: int) -> list[str]:
    return [
        f"warning: full output exceeded the configured max_output_chars threshold ({max_output_chars} chars).",
        "Full mode was not truncated to preserve existing behavior.",
        'Suggestion: use --compact or a more specific prompt, for example: python discord_cli.py --prompt "Update me on MLOps about tenant. Answer in Hebrew." --compact',
        f"channel: {channel_name}",
    ]


def apply_output_char_limit(text: str, max_output_chars: int, channel_name: str) -> str:
    if len(text) <= max_output_chars:
        return text
    notice = "\n".join(["", *build_truncation_notice(channel_name)])
    allowed = max_output_chars - len(notice) - 3
    if allowed < 0:
        allowed = 0
    return text[:allowed].rstrip() + "..." + notice


def build_compact_cache_context(channel_name: str, cache_data: dict, keywords: list[str] | None = None, limits: dict | None = None) -> str:
    posts = cache_data.get("normalized_posts", [])
    total_messages = cache_data.get("total_messages", 0)
    active_keywords = keywords or []
    metadata = derive_cache_metadata(cache_data)
    active_limits = limits or DEFAULT_OUTPUT_LIMITS
    remaining_slots = active_limits["max_total_snippets"]
    included_posts = 0
    included_snippets = 0
    truncated = False

    lines = [
        f"resolved channel: {channel_name}",
        "source: normalized cache",
        "context mode: compact",
        f"cache path: {normalized_path_for_channel(channel_name)}",
    ]

    if metadata["after"] or metadata["before"]:
        lines.append(
            f"time range: after={metadata['after'] or 'unknown'}, before={metadata['before'] or 'open'}"
        )
    if metadata["exported_at"]:
        lines.append(f"exported_at: {metadata['exported_at']}")

    if active_keywords:
        lines.append(f"filtered topic: {' '.join(active_keywords)}")

    if not posts:
        lines.extend([
            "posts/threads included: 0",
            "messages available: 0",
            "snippets included: 0",
            "",
            f'No matching cached messages found for topic "{" ".join(active_keywords)}" in channel {channel_name}.',
            "",
            *build_agent_summary_instructions(),
        ])
        return apply_output_char_limit("\n".join(lines), active_limits["max_output_chars"], channel_name)

    compact_post_blocks: list[str] = []
    for post in posts[: active_limits["max_posts"]]:
        if remaining_slots <= 0:
            truncated = True
            break
        selected_snippets = select_post_snippets(
            post,
            active_keywords,
            remaining_slots,
            active_limits["max_snippets_per_post"],
        )
        if not selected_snippets:
            continue

        included_posts += 1
        included_snippets += len(selected_snippets)
        remaining_slots -= len(selected_snippets)

        post_lines = [
            f"- post: {post.get('post_name') or 'Unknown post'}",
            f"  id: {post.get('post_id') or 'unknown'}",
            f"  latest timestamp: {latest_post_timestamp(post)}",
            f"  message count: {len(post.get('messages', []))}",
        ]
        if active_keywords:
            post_lines.append(f"  matched topic: {' '.join(active_keywords)}")

        for snippet_index, message in enumerate(selected_snippets, start=1):
            author_name = message.get("author_display_name") or "Unknown author"
            timestamp = message.get("timestamp") or "Unknown timestamp"
            content = truncate_text(message.get("content") or "", active_limits["max_chars_per_snippet"])
            attachment_summary = summarize_attachments(message)
            reaction_total = message_reaction_count(message)

            if not content and attachment_summary:
                content = "(attachment metadata only)"

            post_lines.append(f"  snippet {snippet_index}: [{timestamp}] {author_name}: {content}")
            if attachment_summary:
                post_lines.append(f"    attachments: {attachment_summary}")
            if reaction_total > 0:
                post_lines.append(f"    reactions total: {reaction_total}")

        compact_post_blocks.append("\n".join(post_lines))

    if len(posts) > active_limits["max_posts"]:
        truncated = True

    lines.extend([
        f"posts/threads included: {included_posts}",
        f"messages available: {total_messages}",
        f"snippets included: {included_snippets}",
        "",
        "grouped compact context:",
        *compact_post_blocks,
    ])
    if truncated:
        lines.extend(["", *build_truncation_notice(channel_name)])
    lines.extend(["", *build_agent_summary_instructions()])
    return apply_output_char_limit("\n".join(lines).rstrip(), active_limits["max_output_chars"], channel_name)


def build_full_cache_context(channel_name: str, cache_data: dict, keywords: list[str] | None = None, limits: dict | None = None) -> str:
    posts = cache_data.get("normalized_posts", [])
    total_messages = cache_data.get("total_messages", 0)
    active_keywords = keywords or []
    metadata = derive_cache_metadata(cache_data)
    active_limits = limits or DEFAULT_OUTPUT_LIMITS

    lines = [
        f"resolved channel: {channel_name}",
        "source: cache",
        "context mode: full",
        f"cache path: {normalized_path_for_channel(channel_name)}",
    ]

    if metadata["after"] or metadata["before"]:
        lines.append(
            f"time range: after={metadata['after'] or 'unknown'}, before={metadata['before'] or 'open'}"
        )
    if metadata["exported_at"]:
        lines.append(f"exported_at: {metadata['exported_at']}")

    lines.extend([
        f"posts/threads available: {len(posts)}",
        f"messages available: {total_messages}",
    ])

    if active_keywords:
        lines.append(f"filtered topic: {' '.join(active_keywords)}")

    if not posts:
        lines.append("")
        lines.append(
            f'No matching cached messages found for topic "{" ".join(active_keywords)}" in channel {channel_name}.'
        )
        lines.extend(["", *build_agent_summary_instructions()])
        text = "\n".join(lines)
        if len(text) > active_limits["max_output_chars"]:
            text = text.rstrip() + "\n\n" + "\n".join(
                build_full_output_warning(channel_name, active_limits["max_output_chars"])
            )
        return text

    lines.extend(["", "grouped context:"])

    for post in posts:
        post_name = post.get("post_name") or "Unknown post"
        messages = post.get("messages", [])
        lines.append(f"- post: {post_name} ({len(messages)} messages)")
        for message in messages:
            author_name = message.get("author_display_name") or "Unknown author"
            timestamp = message.get("timestamp") or "Unknown timestamp"
            content = message.get("content")
            if content is None:
                content = ""
            lines.append(f"  [{timestamp}] {author_name}: {content}")
            attachments = message.get("attachments", [])
            if attachments:
                attachment_summary = ", ".join(
                    attachment.get("fileName") or "unknown attachment"
                    for attachment in attachments
                    if isinstance(attachment, dict)
                )
                lines.append(f"    attachments: {attachment_summary}")
        lines.append("")

    lines.extend(["", *build_agent_summary_instructions()])
    text = "\n".join(lines).rstrip()
    if len(text) > active_limits["max_output_chars"]:
        text = text.rstrip() + "\n\n" + "\n".join(
            build_full_output_warning(channel_name, active_limits["max_output_chars"])
        )
    return text


def emit_text(text: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.write(text)
    sys.stdout.write("\n")


def build_summarize_not_configured_message(target: str) -> str:
    return "\n".join(
        [
            "Error: LLM summarization is not configured for this CLI.",
            f"Requested target: {target}",
            "Use --context <channel> --compact or --summary-context \"<request>\" and let the agent produce the final summary.",
        ]
    )


def build_context_output(channel_name: str, cache_data: dict, keywords: list[str], output_limits: dict, compact: bool, warning: str | None = None) -> str:
    if compact:
        text = build_compact_cache_context(channel_name, cache_data, keywords, output_limits)
    else:
        text = build_full_cache_context(channel_name, cache_data, keywords, output_limits)
    if warning:
        return warning + "\n" + text
    return text


def refresh_one_channel(channel_config: dict, config: dict, after_date: str, since_days: int | None, timeout_seconds: int, window_text: str | None = None, verbose: bool = False) -> dict:
    channel_name = channel_config["name"]
    attempted_at = current_timestamp()
    window = window_text or requested_window_text(after_date, since_days)
    update_channel_run_status(
        channel_name,
        {
            "last_attempted_refresh": attempted_at,
            "requested_window": window,
            "export_source": "channel",
            "partial_export": False,
            "last_error": "",
            "last_warning": "",
        },
    )
    try:
        token = get_discord_token(config)
        export_result = export_channel(channel_config, config, token, after_date, timeout_seconds, verbose)
        normalized_result = normalize_channel_export(raw_path_for_channel(channel_name), channel_config)
    except subprocess.TimeoutExpired as exc:
        update_channel_run_status(
            channel_name,
            {
                "partial_export": True,
                "last_error": f"timeout after {timeout_seconds}s",
                "last_warning": "Raw partial data may exist, but normalization did not complete.",
            },
        )
        raise RuntimeError(f"Channel refresh timed out for {channel_name} after {timeout_seconds}s") from exc
    except Exception as exc:
        update_channel_run_status(channel_name, {"last_error": str(exc)})
        raise

    update_channel_run_status(
        channel_name,
        {
            "last_successful_refresh": current_timestamp(),
            "raw_json_file_count": export_result["json_file_count"],
            "posts_count": normalized_result["posts_count"],
            "messages_count": normalized_result["total_messages"],
            "partial_export": False,
            "last_error": "",
        },
    )
    return {
        "channel": channel_name,
        "raw_json_files": export_result["json_file_count"],
        "posts_count": normalized_result["posts_count"],
        "messages_count": normalized_result["total_messages"],
    }


def run_refresh_channel_command(config: dict, channel_text: str, argparse_namespace) -> int:
    requested_channel_name = resolve_channel(channel_text, config)
    channel_config = get_channel_config(requested_channel_name, config)
    effective = resolve_effective_config(config, channel_config, argparse_namespace)
    after_date = effective["refresh_after"]
    since_days = namespace_value(argparse_namespace, "since_days")
    timeout_seconds = effective["timeout_seconds"]
    try:
        result = refresh_one_channel(
            channel_config,
            config,
            after_date,
            since_days,
            timeout_seconds,
            effective["refresh_window"],
            effective["verbose"],
        )
    except RuntimeError as exc:
        record_run("refresh-channel", "failed", {"channel": requested_channel_name, "error": str(exc)})
        emit_text(
            "\n".join(
                [
                    build_effective_config_output(effective),
                    f"refresh failed: {exc}",
                ]
            )
        )
        return 1
    record_run("refresh-channel", "ok", result)
    emit_text(
        "\n".join(
            [
                build_effective_config_output(effective),
                "",
                f"Refreshed channel: {result['channel']}",
                "Exporter calls: 1",
                f"Raw JSON files: {result['raw_json_files']}",
                f"Normalized posts/threads: {result['posts_count']}",
                f"Normalized messages: {result['messages_count']}",
                f"Window: {effective['refresh_window']}",
            ]
        )
    )
    return 0


def run_refresh_group_command(config: dict, mode: str, argparse_namespace) -> int:
    selected_channels = []
    for channel in list_enabled_channels(config):
        status = get_channel_cache_status(channel, config)
        if mode == "missing" and not status["normalized_cache_exists"]:
            selected_channels.append(channel)
        elif mode == "stale" and status["status"] == "stale":
            selected_channels.append(channel)

    lines = [f"refresh mode: {mode}", f"channels selected: {len(selected_channels)}"]
    results = []
    for channel in selected_channels:
        try:
            effective = resolve_effective_config(config, channel, argparse_namespace)
            result = refresh_one_channel(
                channel,
                config,
                effective["refresh_after"],
                namespace_value(argparse_namespace, "since_days"),
                effective["timeout_seconds"],
                effective["refresh_window"],
                effective["verbose"],
            )
            results.append(result)
            lines.append(f"ok: {channel['name']} raw={result['raw_json_files']} posts={result['posts_count']} messages={result['messages_count']}")
        except Exception as exc:
            lines.append(f"failed: {channel['name']} error={exc}")
            record_run(f"refresh-{mode}", "failed", {"channel": channel["name"], "error": str(exc)})
            emit_text("\n".join(lines))
            return 1

    record_run(f"refresh-{mode}", "ok", {"channels": [item["channel"] for item in results]})
    emit_text("\n".join(lines))
    return 0


def run_bootstrap_server_command(config: dict, argparse_namespace) -> int:
    attempted_at = current_timestamp()
    token = get_discord_token(config)
    defaults = get_tool_defaults(config)
    cli_since_days = namespace_value(argparse_namespace, "since_days")
    cli_after = namespace_value(argparse_namespace, "after")
    if cli_since_days is not None or cli_after:
        after_date = resolve_after_date(config, cli_since_days, cli_after)
        window_text = requested_window_text(after_date, cli_since_days)
    else:
        after_date, window_text = resolve_window_from_config(config, defaults["refresh_window"])
    timeout_seconds = namespace_value(argparse_namespace, "timeout_seconds") or defaults["safety"]["export_timeout_seconds"]
    try:
        export_result = export_server_bootstrap(config, token, after_date, timeout_seconds)
        normalized_results = copy_bootstrap_files_for_configured_channels(config)
    except Exception as exc:
        record_run("bootstrap-server", "failed", {"error": str(exc)})
        emit_text(f"Error: {exc}")
        return 1

    partial = bool(export_result.get("timed_out"))
    warning = "server export timed out; normalized data was built from preserved partial raw files" if partial else ""
    for channel_name, normalized_result in normalized_results.items():
        update_channel_run_status(
            channel_name,
            {
                "last_attempted_refresh": attempted_at,
                "last_successful_refresh": current_timestamp(),
                "requested_window": window_text,
                "export_source": "server-bootstrap",
                "partial_export": partial,
                "last_error": "",
                "last_warning": warning,
                "posts_count": normalized_result["posts_count"],
                "messages_count": normalized_result["total_messages"],
                "raw_json_file_count": normalized_result["raw_json_files_read"],
            },
        )

    record_run(
        "bootstrap-server",
        "partial" if partial else "ok",
        {
            "raw_json_files": export_result["json_file_count"],
            "normalized_channels": len(normalized_results),
            "partial": partial,
        },
    )
    lines = [
        "Bootstrap server export complete" if not partial else "Bootstrap server export partial",
        f"Raw JSON files: {export_result['json_file_count']}",
        f"Normalized configured channels: {len(normalized_results)}",
        f"Window: {window_text}",
    ]
    if warning:
        lines.append(f"warning: {warning}")
    for channel_name, normalized_result in normalized_results.items():
        lines.append(f"ok: {channel_name} posts={normalized_result['posts_count']} messages={normalized_result['total_messages']}")
    emit_text("\n".join(lines))
    return 0


def run_context_command(config: dict, channel_text: str, argparse_namespace, prompt_text: str | None = None) -> int:
    try:
        resolved_channel_name = resolve_channel(channel_text, config)
        channel_config = get_channel_config(resolved_channel_name, config)
        effective = resolve_effective_config(config, channel_config, argparse_namespace)
        cache_data = load_normalized_cache(resolved_channel_name)
        status = get_channel_cache_status(channel_config, config)
        effective["cache_freshness"] = status["status"]
        warning = cache_window_warning(cache_data, effective["context_after"])
        cache_data = filter_cache_data_by_after(cache_data, effective["context_after"])
        keywords = extract_topic_keywords(prompt_text or "", resolved_channel_name, config) if prompt_text else []
        filtered_cache_data = filter_cache_data(cache_data, keywords)
        output_limits = make_context_limits(config, effective["max_messages"], effective["max_chars"])
        if warning:
            effective["caveats"].append(warning)
        if status.get("last_warning"):
            effective["caveats"].append(status["last_warning"])
        effective_text = build_effective_config_output(effective)
    except ValueError as exc:
        emit_text(f"Error: {exc}")
        return 1
    emit_text(effective_text + "\n\n" + build_context_output(resolved_channel_name, filtered_cache_data, keywords, output_limits, effective["compact"], warning))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.validate_config:
        return run_validate_config_command()

    config = load_config()
    validate_config(config)

    if args.list_channels:
        for channel in list_channels(config):
            aliases = ", ".join(channel["aliases"])
            print(f"{channel['name']} ({channel['id']}) aliases: {aliases}")
        return 0

    if args.status is not None:
        if args.status == "__all__":
            target_channels = [channel for channel in list_channels(config) if channel.get("enabled", True)]
        else:
            resolved_channel_name = resolve_channel(args.status, config)
            target_channels = [get_channel_config(resolved_channel_name, config)]
        status_entries = [get_channel_cache_status(channel, config) for channel in target_channels]
        emit_text(build_status_output(status_entries))
        return 0

    if args.bootstrap_server:
        return run_bootstrap_server_command(config, args)

    if args.refresh_missing:
        return run_refresh_group_command(config, "missing", args)

    if args.refresh_stale:
        return run_refresh_group_command(config, "stale", args)

    if args.context:
        return run_context_command(config, args.context, args)

    if args.summary_context:
        natural_since_days = parse_natural_since_days(args.summary_context)
        effective_args = vars(args).copy()
        if natural_since_days is not None and args.since_days is None:
            effective_args["since_days"] = natural_since_days
        return run_context_command(
            config,
            args.summary_context,
            effective_args,
            args.summary_context,
        )

    if args.summarize:
        emit_text(build_summarize_not_configured_message(args.summarize))
        return 1

    if args.prompt:
        return run_context_command(config, args.prompt, args, args.prompt)

    if args.refresh_channel:
        return run_refresh_channel_command(config, args.refresh_channel, args)

    print(f"Loaded config for {config['server_name']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
