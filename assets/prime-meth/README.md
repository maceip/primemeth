# Prime METH boot assets

The interactive Synth boot header loads these files at runtime. `frames.txt` contains the ordered scanline-reveal frames, `timing.json` fixes the animation at 12.5 frames per second for 1.92 seconds, and `palette.json` defines the light-to-deep blue scanline colors.

The final frame is the static fallback for limited-color interactive terminals. Print, RPC, ACP, and daemon modes remain artwork-free so their machine-readable output contracts do not change.
