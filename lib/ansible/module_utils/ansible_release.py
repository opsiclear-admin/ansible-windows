# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Upstream ships this file as a symlink to `../release.py` so module code can
# import `__version__` from module_utils without a direct dependency on the
# controller-side `ansible` package. git-on-Windows clones without
# `core.symlinks=true` materialize that symlink as the plain-text string
# `../release.py`, which then breaks every importer (ansible-galaxy crashes
# at startup, the user-agent fails to construct, etc).
#
# To keep a Windows clone working out of the box, reexport the same names as
# a normal Python shim. Behavior is identical on POSIX clones where the
# symlink was honored, because both paths land in the same `ansible.release`
# module.
from __future__ import annotations

from ansible.release import __author__, __codename__, __version__  # noqa: F401
