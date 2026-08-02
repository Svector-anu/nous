# phase 0: visibility/aliveness pass

issue: #1

this slice is already implemented in main (c46cf46). this pr records the plan note and closes the tracker issue.

verification in main:
- pytest -q: 378 passed
- node scripts/verify_world3d.mjs: 39/39
- fresh-world screenshot shows build flashes, world log, and director following movement.
