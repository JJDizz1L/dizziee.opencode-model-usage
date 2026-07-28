# dizziee.opencode-model-usage

OpenCode token usage stats in the Omarchy bar. Displays today, weekly, and all-time token counts per model.

## Requirements

- Python 3
- OpenCode with existing session data in `~/.local/share/opencode/opencode.db`

## Installation

```sh
git clone https://github.com/JJDizz1L/dizziee.opencode-model-usage.git ~/.config/omarchy/plugins/dizziee.opencode-model-usage
```

Then enable **OpenCode Usage** in the Omarchy bar widget settings.

## Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `refreshIntervalSec` | integer (30–3600) | 300 | How often to re-query the OpenCode database (seconds) |

## License

MIT
