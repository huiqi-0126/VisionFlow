# Provider connection rules

## Higgsfield

Prefer the configured MCP endpoint:

`https://mcp.higgsfield.ai/mcp`

Ask the user to connect/authorize the MCP. Never request, display, copy, or package Higgsfield OAuth credentials or API keys.

## Other providers

Ask for the provider name, then instruct the user to configure its official connector, environment variable, or local secret manager. Accept only a confirmation that the credential is configured. Never ask the user to paste a raw key into chat and never store secrets in the skill ZIP, project files, prompts, logs, or generated metadata.
