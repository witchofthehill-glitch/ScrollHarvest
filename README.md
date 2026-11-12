
# ScrollHarvest v1

**If you can scroll it, you can save it.**

ScrollHarvest is a small local desktop utility for capturing content from software that does not provide a useful export option.

## What v1 does

- Lets you draw a rectangle around the content you want to capture, and remembers that area next time.
- Dark forest-green panel with cream text and gold accents; Start Harvest is the main action.
- Takes numbered PNG screenshots of that region, then removes those frames once the long PNG is saved.
- Automatically scrolls the selected area.
- The control panel hides for each shot so it is not in the picture.
- Stitches overlapping shots into one PNG and one PDF, named if you gave the harvest a name.
- Lets you name a harvest so the folder, PNG, and PDF are easy to find later.
- Shows a completion summary with Open PDF / Open PNG / Open Folder — files do not open by themselves.
- Lists recent harvests on the main window.
- Stores everything locally in a timestamped folder.
- Does not upload your screenshots anywhere.

## Requirements

- Python 3.10+
- Pillow
- PyAutoGUI

Install dependencies:

```bash
pip install -r requirements.txt
```

Run:

```bash
python scrollharvest.py
```

Or double-click **ScrollHarvest** on the Desktop. You can also run `ScrollHarvest.bat` in the project folder.

## How to use it

1. Open ScrollHarvest.
2. Click **Select Capture Area** and drag around the page you want to save. Press **Esc** to cancel. Next time, that box comes back — you can **Start Harvest** right away, or open Select Capture Area again (gold outline, **Enter** keeps it).
3. Put the target conversation at the top of the capture area.
4. Choose **down** (newer content) or **up** (older messages in many chat apps).
5. Type a **Harvest name** if you want (for example `Neverbound_ch3`).
6. Click **Start Harvest** (top row, next to Select Capture Area).
7. You have 3 seconds to click the page you want saved so it can receive scroll.
8. ScrollHarvest captures, scrolls, and repeats until frames stop changing. It also pauses at **Max shots** (default 400) so a noisy page cannot run forever — Continue for another batch, or Stop and keep what you have.
9. Move the mouse to pause, then choose **Stop** or **Continue**.
10. When it finishes, the app shows a completion summary. Use **Open PDF**, **Open PNG**, or **Open Folder** — nothing opens by itself.

Recent harvests on the main window open PDF, PNG, or the folder without using **Stitch Folder**. You can still click **Stitch Folder** for an older harvest. Technical history and packaging notes are in `DEVELOPMENT_NOTES.md`.

Named captures are stored as `Desktop\ScrollHarvest\YourName_YYYYMMDD_HHMMSS\`. Unnamed ones still use `Harvest_YYYYMMDD_HHMMSS`.

If harvest never stops (typing indicators, GIFs, clocks), raise **Stop when change ≤**, or wait for the **Max shots** pause and hit Stop.

Move the mouse into a screen corner to abort.

## Notes

This is an early prototype. Some apps may:
- scroll in unusual increments,
- animate content,
- lazy-load new content,
- block screenshots,
- or require accessibility/screen-recording permission.

Those cases may need per-platform improvements.

## Privacy

All captures stay on the computer unless the user manually moves or uploads them.
