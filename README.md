# dizziee.opencode-model-usage

OpenCode token usage stats in the Omarchy bar. Displays today, weekly, and all-time token counts per model.

## Requirements

- Python 3
- OpenCode with existing session data in `~/.local/share/opencode/opencode.db`

## Installation

```sh
omarchy plugin add https://github.com/JJDizz1L/dizziee.opencode-model-usage.git --enable
```

Then place it in your bar layout with `omarchy bar plugin add dizziee.opencode-model-usage [--section <left|center|right>]`.

You can validate the plugin at any time with:

```sh
omarchy plugin validate ~/.config/omarchy/plugins/dizziee.opencode-model-usage
```

## Configuration

| Key | Type | Default | Description |
|---|---|---|---|
| `refreshIntervalSec` | integer (30–3600) | 300 | How often to re-query the OpenCode database (seconds) |

## License

MIT
