---
name: claude-in-chrome
description: Coordinate browser-assisted work when a Chrome/browser connector is available.
allowed-tools: mcp_servers, mcp_call, web_search, fetch_url
when_to_use: Use when the user asks to inspect or automate a browser page through a configured browser connector.
context: inline
---
Coordinate browser-assisted work:

{{args}}

First check whether a browser/Chrome MCP server is configured. If available,
use it to inspect, click, type, or capture page state. If unavailable, explain
the missing connector and use web fetch/search only for public pages.
