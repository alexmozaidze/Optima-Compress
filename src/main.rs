// Prevent console window in addition to Slint window in Windows release builds when, e.g., starting the app via file manager. Ignored on other platforms.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::error::Error;
use std::path::{Path, PathBuf};

use slint::{DataTransfer, Model, ModelRc, SharedString, VecModel};

slint::include_modules!();

#[cfg(feature = "demo-clips")]
mod demo_clips;

fn size_label(bytes: u64) -> SharedString {
    const KILOBYTE: f64 = 1024.0;
    const MEGABYTE: f64 = KILOBYTE * 1024.0;
    let bytes = bytes as f64;
    if bytes >= MEGABYTE {
        format!("{:.0} MB", bytes / MEGABYTE).into()
    } else if bytes >= KILOBYTE {
        format!("{:.0} KB", bytes / KILOBYTE).into()
    } else {
        format!("{bytes:.0} B").into()
    }
}

fn clip_from_path(path: &Path, options: &EncodingOptions) -> ClipData {
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.display().to_string());
    let metadata = std::fs::metadata(path)
        .map(|metadata| size_label(metadata.len()))
        .unwrap_or_else(|_| SharedString::from("Unknown size"));
    ClipData {
        file_name: file_name.into(),
        source_path: path.to_string_lossy().into_owned().into(),
        output_path: SharedString::default(),
        metadata,
        state: ClipState::Waiting,
        progress: 0.0,
        selected: false,
        audio_tracks: ModelRc::default(),
        options: options.clone(),
    }
}

fn imported_files(transfer: &DataTransfer) -> Vec<PathBuf> {
    transfer
        .file_paths()
        .into_iter()
        .flatten()
        .filter(|path| path.is_file())
        .map(Path::to_path_buf)
        .collect()
}

fn append_clips(ui: &AppWindow, files: &[PathBuf]) {
    if files.is_empty() {
        return;
    }
    let mut clips: Vec<ClipData> = ui.get_clips().iter().collect();
    let options = ui.get_default_options();
    clips.extend(files.iter().map(|file| clip_from_path(file, &options)));
    ui.set_clips(ModelRc::new(VecModel::from(clips)));
}

fn connect_import(ui: &AppWindow) {
    let drop_handle = ui.as_weak();
    ui.on_files_dropped(move |transfer| {
        let Some(ui) = drop_handle.upgrade() else { return };
        append_clips(&ui, &imported_files(&transfer));
    });
}

fn main() -> Result<(), Box<dyn Error>> {
    let ui = AppWindow::new()?;
    connect_import(&ui);

    #[cfg(feature = "demo-clips")]
    demo_clips::seed(&ui);

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

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch_directory(name: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("optima-compress-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&directory).expect("scratch directory");
        directory
    }

    #[test]
    fn size_label_never_reports_a_fraction() {
        assert_eq!(size_label(0), "0 B");
        assert_eq!(size_label(512), "512 B");
        assert_eq!(size_label(2048), "2 KB");
        assert_eq!(size_label(2 * 1024 * 1024), "2 MB");
    }

    #[test]
    fn clip_from_path_reports_the_file_and_its_size() {
        let directory = scratch_directory("clip");
        let file = directory.join("clip.mp4");
        std::fs::write(&file, vec![0u8; 2048]).expect("write clip");

        let clip = clip_from_path(&file, &EncodingOptions::default());

        assert_eq!(clip.file_name, "clip.mp4");
        assert_eq!(clip.source_path, file.to_string_lossy().as_ref());
        assert_eq!(clip.output_path, "");
        assert_eq!(clip.metadata, "2 KB");
        assert_eq!(clip.state, ClipState::Waiting);
        assert_eq!(clip.progress, 0.0);
        assert!(!clip.selected);
        assert_eq!(clip.audio_tracks.row_count(), 0);

        std::fs::remove_dir_all(&directory).expect("remove scratch");
    }

    #[test]
    fn imported_files_skips_directories_and_missing_paths() {
        let directory = scratch_directory("transfer");
        let file = directory.join("clip.mp4");
        std::fs::write(&file, b"clip").expect("write clip");

        let mut transfer = DataTransfer::default();
        transfer.set_file_paths([&file, &directory, &directory.join("missing.mp4")]);

        assert_eq!(imported_files(&transfer), vec![file]);

        std::fs::remove_dir_all(&directory).expect("remove scratch");
    }

    #[test]
    fn imported_files_is_empty_without_file_paths() {
        assert!(imported_files(&DataTransfer::default()).is_empty());
    }

    #[test]
    fn dropping_files_appends_clips_to_an_empty_window() {
        let directory = scratch_directory("window");
        let file = directory.join("dropped.mp4");
        std::fs::write(&file, b"clip").expect("write clip");

        // SAFETY: only this test opens a window, and it reads the backend choice it just set.
        unsafe { std::env::set_var("SLINT_BACKEND", "headless") };
        let ui = AppWindow::new().expect("window");
        connect_import(&ui);
        assert_eq!(ui.get_clips().row_count(), 0);

        let mut transfer = DataTransfer::default();
        transfer.set_file_paths([&file]);
        ui.invoke_files_dropped(transfer);

        let clips = ui.get_clips();
        assert_eq!(clips.row_count(), 1);
        assert_eq!(clips.row_data(0).expect("imported clip").file_name, "dropped.mp4");

        std::fs::remove_dir_all(&directory).expect("remove scratch");
    }
}
