"""Allow `python -m dronecli` alongside the installed `drone-cli` script.

Not a convenience: the integration harness shells the CLI as a subprocess via
`[sys.executable, "-m", "dronecli", ...]`, which exercises the real argv path
(including `_pop_globals`) without depending on a console script being on PATH.
"""

from .cli import main

main()
