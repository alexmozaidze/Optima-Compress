// Prevent console window in addition to Slint window in Windows release builds when, e.g., starting the app via file manager. Ignored on other platforms.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::error::Error;

slint::include_modules!();

fn main() -> Result<(), Box<dyn Error>> {
    let ui = AppWindow::new()?;

    // The clipboard is opened on first use and kept for the app's lifetime: a Wayland offer ends
    // when its owner is dropped, so a per-click clipboard would copy and paste nothing.
    let mut clipboard: Option<arboard::Clipboard> = None;
    ui.on_copy_requested(move |path| {
        if clipboard.is_none() {
            match arboard::Clipboard::new() {
                Ok(opened) => clipboard = Some(opened),
                Err(error) => {
                    eprintln!("clipboard unavailable: {error}");
                    return;
                }
            }
        }
        if let Some(clipboard) = clipboard.as_mut() {
            if let Err(error) = clipboard.set_text(path.to_string()) {
                eprintln!("clipboard write failed: {error}");
            }
        }
    });

    ui.run()?;
    Ok(())
}
