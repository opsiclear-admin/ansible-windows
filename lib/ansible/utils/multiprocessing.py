# Copyright (c) 2019 Matt Martz <matt@sivel.net>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import multiprocessing
import sys

# Explicit multiprocessing context.
#
# POSIX: 'fork' — ansible's executor historically assumes fork copy-on-write
# semantics for handing off loaders, inventory, and the RPC manager to workers.
#
# Windows: 'spawn' — fork is unavailable. The windows-controller branch is
# rewriting the worker handoff to survive spawn (explicit bootstrap + re-init
# of plugin loaders in the child). Until that lands, import-level code paths
# work but multi-worker execution will break on pickling.
#
# This lives in utils so it can be imported widely without circular deps.
_start_method = 'spawn' if sys.platform == 'win32' else 'fork'
context = multiprocessing.get_context(_start_method)
