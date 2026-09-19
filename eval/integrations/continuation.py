"""Evaluation-only generic follow-ups inside one native CLI/Agent lifetime.

The frozen wheel must expose CLI's one-shot turn boundary. Keeping that boundary
lets the ordinary CLI own provider setup, prompt resolution, and final cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

CONTINUATION = (
    'Continue toward the original requested objective. Independently check the deliverables '
    'against every original requirement and finish any remaining work. Do not redefine the '
    'objective or change already-correct work unnecessarily. Report the verification evidence.\n'
)


def run(argv: list[str]) -> int:
    from ava.app import cli
    from ava.llm import Item, Role, make_text_block

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('continuations', type=int, choices=range(1, 11))
    parser.add_argument('logs', type=Path)
    parser.add_argument('cli_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    args.logs.mkdir(parents=True, exist_ok=True)
    receipt = args.logs / 'continuations.json'
    native_turn = cli._run_one_shot

    async def turns(agent, item):
        for turn in range(args.continuations + 1):
            receipt.write_text(json.dumps({'continuations_sent': turn}) + '\n')
            if turn:
                item = Item(role=Role.user, blocks=[make_text_block(CONTINUATION)])
            result = await native_turn(agent, item)
            if result != cli.EXIT_OK:
                return result
            if turn < args.continuations:
                process = await asyncio.create_subprocess_exec(
                    'tar', '-C', str(Path.cwd()), '-czf',
                    str(args.logs / f'checkpoint-{turn}.tar.gz'), '.',
                )
                try:
                    if await process.wait() != 0:
                        raise RuntimeError('Could not preserve pre-continuation workspace')
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        return cli.EXIT_OK

    cli._run_one_shot = turns
    try:
        return cli.run(args.cli_args)
    finally:
        cli._run_one_shot = native_turn


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
