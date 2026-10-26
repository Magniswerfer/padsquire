## What changes

<!-- What this does for someone using PadSquire, in a sentence or two. -->

## How it was tested

<!-- pytest, the simulator (`padsquire gui --simulate ...`), and anything run on hardware:
     which command, firmware, connection and operating system, with the output. -->

## Checklist

- [ ] `pytest` passes, and new behaviour has a test that needs no hardware.
- [ ] A protocol finding is in PROTOCOL.md with its level (static analysis, seen once,
      confirmed, refuted), firmware, connection and date; the simulator and its tests match it,
      and `ASSUMPTION` comments are gone for anything now confirmed.
- [ ] A new capability has its place in the settings app, and a Setup and help entry if it
      needs something set up.
- [ ] Commands shown to users explain each part and link the official documentation.
- [ ] Copy is plain and in sentence case.
- [ ] No GameSir files, decompiled code, flash backups or personal data.
- [ ] The firmware safety checks are unchanged, or a test shows they still refuse.
