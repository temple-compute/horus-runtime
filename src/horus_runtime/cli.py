#
# horus-runtime
# Copyright (C) 2026 Temple Compute
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""
Entrypoint for horus-runtime.
"""

import asyncio
from pathlib import Path

import click

from horus_builtin.tui import render_workflow
from horus_runtime.context import HorusContext
from horus_runtime.core.workflow.base import BaseWorkflow
from horus_runtime.i18n import tr as _
from horus_runtime.logging import horus_logger
from horus_runtime.packaging import package_workflow
from horus_runtime.sanitize import find_root_inputs, sanitize_workflow
from horus_runtime.version import __version__ as horus_version


@click.group(invoke_without_command=True)
@click.version_option(version=horus_version, prog_name="Horus Runtime")
@click.pass_context
def main(ctx: click.Context) -> None:
    """
    Run workflows and tasks using the Horus Runtime.
    """
    # Bare `horus` (no subcommand) shows help rather than doing nothing.
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument(
    "workflow_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--trigger",
    "trigger_id",
    default=None,
    help="Task id to trigger. Defaults to the first task in the workflow.",
)
@click.option(
    "--no-tui",
    is_flag=True,
    help="Disable the live TUI; stream log output only.",
)
@click.option(
    "--no-skip",
    "no_skip_ids",
    multiple=True,
    metavar="TASK_ID",
    help=(
        "Force the given task to run even if already complete. "
        "Repeat for each task ID."
    ),
)
@click.option(
    "--no-skip-all",
    is_flag=True,
    help="Force all tasks to run, ignoring completion status.",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging.",
)
def run(
    workflow_yaml: Path,
    trigger_id: str | None,
    no_tui: bool,
    no_skip_ids: tuple[str, ...],
    no_skip_all: bool,
    debug: bool,
) -> None:
    """
    Run the workflow defined in WORKFLOW_YAML.

    Boots the runtime, loads the workflow, and executes it from the trigger
    task downstream. Exits non-zero if the workflow fails.
    """
    if debug:
        horus_logger.set_level("DEBUG")

    ctx = HorusContext.boot()
    try:
        workflow = BaseWorkflow.from_yaml(workflow_yaml)
        if not workflow.tasks:
            raise click.ClickException(_("Workflow has no tasks to run."))
        trigger = trigger_id or workflow.tasks[0].id

        if no_skip_ids:
            known_ids = {task.id for task in workflow.tasks}
            unknown_ids = sorted(set(no_skip_ids) - known_ids)
            if unknown_ids:
                raise click.ClickException(
                    _(
                        "Unknown task id(s) for --no-skip: %(unknown)s. "
                        "Valid task ids: %(valid)s"
                    )
                    % {
                        "unknown": ", ".join(unknown_ids),
                        "valid": ", ".join(sorted(known_ids)),
                    }
                )
        force_ids = set(no_skip_ids)
        for task in workflow.tasks:
            if no_skip_all or task.id in force_ids:
                task.skip_if_complete = False

        if no_tui:
            asyncio.run(workflow.run(trigger_id=trigger))
        else:
            render_workflow(workflow, trigger_id=trigger)

    except click.ClickException:
        raise
    except Exception as exc:
        # Surface any workflow/loading failure as a non-zero CLI exit.
        raise click.ClickException(str(exc)) from exc
    finally:
        ctx.shutdown()


@main.command()
@click.argument(
    "workflow_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Zip to write. Defaults to <workflow-dir-name>.zip beside it.",
)
def package(workflow_yaml: Path, output: Path | None) -> None:
    """
    Bundle WORKFLOW_YAML and the files it references into a zip.

    Collects every external input artifact, script, and executor environment
    file, so the workflow can run on a machine that never had this directory.
    Run output is excluded. Exits non-zero if a referenced file is missing.
    """
    ctx = HorusContext.boot()
    try:
        archive, members, skipped = package_workflow(workflow_yaml, output)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        ctx.shutdown()

    click.echo("  + workflow.yaml")
    for rel in members:
        click.echo(f"  + {rel}")
    for rel in skipped:
        # Usually a path a plugin pins into the run root when it expands
        # (``map:`` does this), but it can also be a genuinely missing input.
        click.echo(f"  - {rel} " + _("(not found; assumed generated)"))
    click.echo(
        _("Wrote %(archive)s (%(count)d file(s))")
        % {"archive": archive, "count": len(members) + 1}
    )

    # The files travel either way; undeclared, they just arrive unlabelled,
    # so an importing UI cannot offer them as inputs. Warn, never rewrite.
    root_inputs, _missing = find_root_inputs(
        BaseWorkflow.from_yaml(workflow_yaml)
    )
    if root_inputs:
        click.echo(
            _(
                "%(count)d undeclared root input(s); "
                "run 'horus sanitize' to promote them"
            )
            % {"count": len(root_inputs)}
        )


@main.command()
@click.argument(
    "workflow_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="YAML to write. Defaults to <name>.sanitized.yaml beside it.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Promote every root input without asking.",
)
def sanitize(workflow_yaml: Path, output: Path | None, yes: bool) -> None:
    """
    Declare WORKFLOW_YAML's implicit root inputs as root artifacts.

    A task input that no edge feeds is a file you supply rather than one the
    run produces. Promoting it to the top-level `artifacts:` list, wired by an
    edge, is what lets a UI show it as a workflow input. Task inputs are left
    untouched, so command substitutions keep working.
    """
    ctx = HorusContext.boot()
    try:
        workflow = BaseWorkflow.from_yaml(workflow_yaml)
        candidates, missing = find_root_inputs(workflow)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        ctx.shutdown()

    for gap in missing:
        # Not a root input: the path is run output. Only the author can say
        # whether the missing dependency or the path is the mistake.
        click.echo(
            f"  ! {gap.task_id}.{gap.input_id} -> {gap.path} "
            + _("produced by %(producer)s; missing edge")
            % {"producer": gap.producer}
        )

    if not candidates:
        click.echo(_("No root inputs to promote."))
        return

    accept = {
        root.root_id
        for root in candidates
        if yes
        or click.confirm(
            f"  {root.path} -> artifacts/{root.root_id}?", default=True
        )
    }
    if not accept:
        click.echo(_("Nothing promoted."))
        return

    try:
        written, promoted, _missing = sanitize_workflow(
            workflow_yaml, output, accept
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    for root in promoted:
        click.echo(f"  + artifacts/{root.root_id} ({root.path})")
        for task_id, input_id in root.consumers:
            click.echo(f"  + edge -> {task_id}.{input_id}")
    click.echo(
        _("Wrote %(path)s (%(count)d root input(s))")
        % {"path": written, "count": len(promoted)}
    )


if __name__ == "__main__":
    # Call the main function to start the runtime
    main()
