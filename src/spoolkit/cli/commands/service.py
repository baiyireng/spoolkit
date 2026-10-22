"""`spool service`: start / stop / list local [[service]] recipes.

Hub does not start llama-server or tunnels. Recipes live in the user config.
Start/stop decisions use the pid record written by this command; probes report
status only. Details: `spoolkit.services`.
"""

from __future__ import annotations

import argparse

from rich.markup import escape

from spoolkit import settings
from spoolkit.cli.term import print_err, print_out
from spoolkit.services import render_status, start_service, stop_service


def service_command(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "list"
    name = str(getattr(args, "name", "") or "").strip()
    _, problem = settings.read_config()
    if problem:
        print_err(problem, markup=False)
        return 2

    if action == "list":
        print_out(render_status())
        return 0

    items = settings.load_services()
    force = bool(getattr(args, "force", False))
    if force and action != "stop":
        print_err("--force only applies to stop.", markup=False)
        return 2

    if action == "start":
        if name:
            names = [name]
        else:
            names = [item.name for item in items if item.command]
            if not names:
                print_err("No recipes with a start command.", markup=False)
                return 2
    else:
        names = [name] if name else [item.name for item in items]
        if not names:
            print_err(
                "No [[service]] recipes. Add them in the user config.",
                markup=False,
            )
            return 2

    worst = 0
    for item in names:
        if action == "start":
            code, message = start_service(item)
        else:
            code, message = stop_service(item, force=force)
        styled = f"[{'bad' if code else 'ok'}]{escape(message)}[/]"
        (print_err if code else print_out)(styled)
        worst = max(worst, code)
    return worst
