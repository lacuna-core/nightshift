#!/bin/sh
# Claude Code status line: model | context window bar | session cost
jq -r '
  (.context_window // {}) as $cw
  | ($cw.current_usage // {}) as $u
  | (($u.input_tokens // 0) + ($u.cache_creation_input_tokens // 0) + ($u.cache_read_input_tokens // 0)) as $used
  | [ .model.display_name // "?", $used, $cw.context_window_size // 0, .cost.total_cost_usd // 0 ]
  | @tsv' |
  awk -F'\t' '
    function human(n) { return n >= 1000000 ? sprintf("%.1fM", n / 1000000) : sprintf("%dk", n / 1000) }
    {
      pct = $3 > 0 ? $2 * 100 / $3 : 0
      filled = int(pct / 10 + 0.5); if (filled > 10) filled = 10
      bar = ""; for (i = 0; i < 10; i++) bar = bar (i < filled ? "█" : "░")
      printf "%s | %s %d%% (%s/%s) | $%.2f", $1, bar, pct, human($2), human($3), $4
    }'
