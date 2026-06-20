---
name: discord-summary-helper
description: Use when the user asks for Discord course updates, summaries, action items, decisions, or recent activity from configured Discord channels.
---

# Discord Summary Helper

Use this workflow from the `discord-summary-helper` directory.

## Workflow

1. Resolve the user's request to one configured Discord channel using the request text and `python discord_cli.py --list-channels` if needed.
2. Run `python discord_cli.py --status <channel>`.
3. Refresh only when cache is `missing`, `stale`, `incomplete`, or when the user explicitly asks for latest/current updates:
   - `python discord_cli.py --refresh-channel <channel>`
   - Do not run `--bootstrap-server` for normal summaries.
4. Get evidence:
   - Known channel: `python discord_cli.py --context <channel>`
   - Natural-language routing: `python discord_cli.py --summary-context "<user request>"`
5. Summarize only from the CLI evidence. For forum channels, read the evidence per post/thread before writing the final summary.

The CLI is config-first. Normal skill use should not pass policy override flags such as `--since-days`, `--after`, `--compact`, `--max-messages`, `--max-chars`, or `--timeout-seconds`. Use those flags only as one-off manual overrides when the user explicitly asks for a different window/bound or when debugging.

## Safety Rules

- Refresh one specific channel at a time.
- Ask before any server bootstrap or broad refresh.
- Stop and report clearly on `forbidden`, `unauthorized`, timeout, missing cache, or partial export.
- Treat attachments as metadata unless their content appears in the message text.
- Do not call an LLM API from the CLI; the agent produces the final summary.

## Final Answer

Answer in English with practical detail. Do not only list topics. For each major post/thread, extract what happened, the useful answer or decision, concrete commands/config details if visible, and what the student should do next.

Use these sections:

- `# TL;DR`
- `# Main Points`
- `# Decisions`
- `# Action Items`
- `# Open Questions`
- `# Risks / Caveats`
- `# Source Window`

Always mention the channel, cache/source window, evidence bounds, and any stale/partial/truncation caveats.
