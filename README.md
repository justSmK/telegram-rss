[![Generate Telegram RSS](https://github.com/justSmK/telegram-rss/actions/workflows/rss.yml/badge.svg)](https://github.com/justSmK/telegram-rss/actions/workflows/rss.yml)
# [telegram-rss](https://justsmk.github.io/telegram-rss/)

Telegram channels → RSS (via rss-bridge).

## Feed filtering

Generated feeds are filtered before publishing. Rules live in
[`config/ad_filters.toml`](config/ad_filters.toml), so new ad patterns can be
added without changing Python code.

Each `[[rules]]` entry removes a post when any matcher in that rule matches.
Supported matcher fields:

- `text_patterns`: regular expressions checked against decoded post text.
- `link_patterns`: regular expressions checked against decoded `href`/`src`
  URLs.
- `link_query_params`: exact URL query/fragment parameter names, for example
  `erid`.
- `link_query_param_value_patterns`: query parameter value matchers, for
  example `utm_medium = paid`.
- `link_domains` / `link_domain_patterns`: host allow/block style matchers.

Filtered posts are written to `_filtered.log` in the Pages artifact.
