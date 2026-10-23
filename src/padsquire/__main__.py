# SPDX-License-Identifier: AGPL-3.0-or-later
"""`python -m padsquire`: the same as the padsquire command."""
import sys

from .cli import main

sys.exit(main())
