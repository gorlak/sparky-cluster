"""measure/record — where a measurement GOES: persist, then present.

`store` (the SQLite trend db and its Row schema), `report` (direction-aware A/B compare
between two labels), `scoreboard` (the ranking view across profiles). The instruments write
here; the CLI reads here.
"""
