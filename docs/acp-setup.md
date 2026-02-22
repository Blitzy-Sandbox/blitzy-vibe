# ACP Setup

Blitzy Agent can be used in text editors and IDEs that support [Agent Client Protocol](https://agentclientprotocol.com/overview/clients). Blitzy Agent includes the `blitzy-acp` tool.
Once you have set up `blitzy` with the API keys, you are ready to use `blitzy-acp` in your editor. Below are the setup instructions for some editors that support ACP.

## Zed

For usage in Zed, we recommend using the [Blitzy Agent Zed's extension](https://zed.dev/extensions/blitzy-agent). Alternatively, you can set up a local install as follows:

1. Go to `~/.config/zed/settings.json` and, under the `agent_servers` JSON object, add the following key-value pair to invoke the `blitzy-acp` command. Here is the snippet:

```json
{
   "agent_servers": {
      "Blitzy Agent": {
         "type": "custom",
         "command": "blitzy-acp",
         "args": [],
         "env": {}
      }
   }
}
```

2. In the `New Thread` pane on the right, select the `blitzy` agent and start the conversation.

## JetBrains IDEs

1. Add the following snippet to your JetBrains IDE acp.json ([documentation](https://www.jetbrains.com/help/ai-assistant/acp.html)):

```json
{
  "agent_servers": {
    "Blitzy Agent": {
      "command": "blitzy-acp",
    }
  }
}
```

2. In the AI Chat agent selector, select the new Blitzy Agent agent and start the conversation.

## Neovim (using avante.nvim)

Add Blitzy Agent in the acp_providers section of your configuration

```lua
{
  acp_providers = {
    ["blitzy-agent"] = {
      command = "blitzy-acp",
      env = {
         MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY"), -- necessary if you setup Blitzy Agent manually
      },
    }
  }
}
```
