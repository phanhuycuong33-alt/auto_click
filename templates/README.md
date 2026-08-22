# templates/

The script finds buttons by **template matching**: it compares each PNG in
this folder against a full-screen capture and clicks where it matches best.

## Required templates (create them with `capture_template.py`)

| file                   | what to crop                                    |
|------------------------|-------------------------------------------------|
| `plus_button.png`      | the "+" button in the Copilot message box      |
| `attach_button.png`    | the "attach image/document" button in the menu after "+" |
| `add_image_button.png` | the "Add image" button in the dialog           |
| `send_button.png`      | the Send button (paper plane)                  |
| `message_box.png`      | the message input box (recommended)            |
| `open_button.png`      | the "Open" button of the file chooser (optional; fallback = Enter) |

## How to create one

```bash
python3 capture_template.py plus_button
```

1. Move the mouse to the top-left corner of the button, press Enter in the terminal.
2. Move the mouse to the bottom-right corner, press Enter again.

## Rules for good templates

- Capture on the same machine / resolution / theme you will run the flow with.
- A few pixels of margin around the button — but nothing else in the crop.
- No cursor, no drop-shadow overlap from other windows.
- If clicks land a few pixels off, just re-capture the template tighter.
- If the button is not found at all, run with `--threshold 0.6` or re-capture.
