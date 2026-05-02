# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Bubblewrap-based sandbox wrapper for the `claude` CLI. The single interesting artifact is `safe-claude.py`; the `.idea/` directory is PyCharm scaffolding unrelated to the sandbox work.

## Running the wrapper

```sh
./safe-claude.py                        # launch Claude Code in the sandbox, project = $PWD
./safe-claude.py --check                # built-in validation (id, env, fs visibility, write tests, DNS)
./safe-claude.py -- /bin/bash -lc '…'  # arbitrary command in the same sandbox
```

There is no build, lint, or test suite — iterate by editing `safe-claude.py` and re-running `--check`.
