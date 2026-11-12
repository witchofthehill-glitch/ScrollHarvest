# ScrollHarvest — development notes

**Read this file before modifying capture, scrolling, stitching, DPI, hiding the window, PDF export, or folder-open behavior.**

The capture/stitch/export system is stable. Polish and packaging are welcome. Do not redesign scrolling or stitching unless there is a confirmed bug.

## Hard-won bugs — do not reintroduce

- **Do not auto-size scroll from capture height.** It jumped too far, tore AO3 covers into bands, and stacked/doubled text. Reverted.
- **Do not change the current fixed scroll-distance behavior unless explicitly requested.**
- **Current default Scroll steps = 80.**
- **`pyautogui.scroll(n)` on Windows sends `n` as raw wheel `dwData`, not mouse notches.** One notch is 120. The value 80 is only a fraction of a notch; that large overlap is why stitch works. Do not “correct” this by multiplying by 120 or targeting capture-box pixels.
- **Do not invent weak-overlap joins.** If frames do not overlap, skip adding that strip. Never invent a join.
- **A stitch seam is one document-space Y.** After alignment, `final_seam_a == final_seam_b + shift` (down) or `final_seam_b == final_seam_a + shift` (up) via telescoping `prev_bound` crops — never independently quiet-row / crop A and B, and never re-crop `pieces[-1]`. Seams are placed inside `matching_overlap_span` (excluding sticky top headers, sticky bottom composers/status bars, and fade masks) at the center of a contiguous `STITCH_SEAM_BAND`-row blank/low-occupancy band (`find_quiet_band_center`). Diagnostic flags: `STITCH_MOVE_SEAM`, `STITCH_JOIN_LOG`, `STITCH_JOIN_IMAGE`. Capture-frame cleanup must not delete `stitch_join.jsonl` / `stitch_join_####.png`.
- **Do not reintroduce the old 35% fallback join.** That sliced cover art (`guess = height * 0.35`).
- **Do not use Tk `withdraw()` alone to hide ScrollHarvest during screenshots.** It still appeared in the grab.
- **Continue parking ScrollHarvest HWNDs off-screen with Win32 `SetWindowPos`** (about `-32000, -32000`), and block the UI thread until the grab finishes.
- **Preserve DPI conversion** between Tk logical coordinates and `ImageGrab` physical pixels (`to_physical_bbox` / virtual screen metrics).
- **Keep PDFs paged.** A single gigantic PDF page fails with `broken data stream when writing image file` on very tall harvests. Split on quiet rows.
- **Do not use `os.startfile` on the save folder named ScrollHarvest.** The Desktop also has `ScrollHarvest.lnk`; ShellExecute opened the shortcut (a second app window) instead of Explorer. Open folders with `explorer.exe` and `/n,` (and `/select,` for a file). Open PDF/PNG with `ShellExecuteW` on the **file path**, never the parent folder name.
- **Do not auto-increment harvest names** (`chp3` → `chp4`). Not everyone names that way.
- **Do not attempt a phone/mobile port** of this script.
- **Do not rework scroll-distance automation.**

Explorer windows titled “ScrollHarvest” must not be treated as app HWNDs (`CabinetWClass` / `ExploreWClass` are skipped in `list_scrollharvest_hwnds`).

Tk Spinbox: do not pass `value=` at construction (it crashed startup). Force the value after create with delete/insert (`apply_scroll_steps`, `apply_max_shots`).

Pillow `Image.MAX_IMAGE_PIXELS` is raised so long harvests can load.

After a successful long PNG, `capture_####.png` frames are deleted. If stitch fails, frames stay.

After code changes, fully close ScrollHarvest (the X, not just Stop). The running process does not reload `scrollharvest.py`.

## Product constraints

- Local only. No cloud, accounts, analytics, login, sync, or telemetry.
- Quality of the stitched PNG/PDF matters more than harvest speed.
- Do not add OCR unless explicitly requested.

## Packaging (not built yet)

Goal eventually: `ScrollHarvestSetup.exe` so the user does not need Python, Pillow, PyAutoGUI, Cursor, or a terminal.

Suggested freeze (PyInstaller, when ready):

```text
pyinstaller --noconfirm --windowed --onefile
  --name ScrollHarvest
  --icon scrollharvest_v2.ico
  --add-data "scrollharvest_v2.ico;."
  --add-data "scrollharvest.ico;."
  --add-data "scrollharvest.png;."
  scrollharvest.py
```

On Windows `--add-data` uses `;` as the separator.

Code is freeze-aware:

- `resource_path()` — icons/assets from `sys._MEIPASS` when frozen, else the script folder.
- `app_base_dir()` — directory of the `.exe` when frozen.
- Settings stay in `%APPDATA%\ScrollHarvest\settings.json`.
- Default harvest folder is `%USERPROFILE%\Desktop\ScrollHarvest`.
- Do not depend on the process working directory for assets or settings.

Installer (later): write the exe, icon, and a Start Menu / Desktop shortcut that points at `ScrollHarvest.exe`, **not** at `pythonw` and the `.py` file. Keep the shortcut name distinct from the save folder if both live on the Desktop, or keep using `explorer.exe` (already required).

## Release testing checklist

Close any old ScrollHarvest process, then launch the build under test. Confirm the dark panel, gold **Start Harvest** button, and Recent harvests list.

Capture quality (stitch should be one continuous page, no doubled lines, no sliced cover art):

- [ ] Long AO3 chapter
- [ ] ChatGPT conversation
- [ ] Claude conversation
- [ ] Reddit thread
- [ ] Long webpage/article
- [ ] Discord or similar scrolling conversation
- [ ] Page with many images
- [ ] Light mode webpage
- [ ] Dark mode webpage

Display scaling (region overlay must match the pointer; screenshots must match the box):

- [ ] Windows scaling 100%
- [ ] Windows scaling 125%
- [ ] Windows scaling 150%

App behavior:

- [ ] Stopping manually (Stop button)
- [ ] Pausing by moving the mouse, then Continue
- [ ] Pausing by moving the mouse, then Stop (keeps captures and stitches)
- [ ] Max-shot Continue (another batch)
- [ ] Max-shot Stop (stitches what you have)
- [ ] Stitch failure preserves `capture_####` frames
- [ ] Successful stitch deletes `capture_####` frames
- [ ] PDF rebuild from an existing long PNG (Stitch Folder or Recent)
- [ ] Custom save folder
- [ ] Restored capture region on launch
- [ ] Missing monitor / invalid saved region is dropped
- [ ] App closing and reopening restores settings
- [ ] Recent harvests list after restart
- [ ] Harvest complete panel: Open PDF / Open PNG / Open Folder (nothing auto-opens)
- [ ] Open PDF/PNG use the default Windows apps
- [ ] Open Folder opens Explorer, not a second ScrollHarvest window
