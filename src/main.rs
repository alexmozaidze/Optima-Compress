// Prevent console window in addition to Slint window in Windows release builds when, e.g., starting the app via file manager. Ignored on other platforms.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::error::Error;
use std::path::{Path, PathBuf};

use slint::winit_030::{EventResult, WinitWindowAccessor, winit};
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

fn existing_files(paths: impl IntoIterator<Item = PathBuf>) -> Vec<PathBuf> {
    paths.into_iter().filter(|path| path.is_file()).collect()
}

fn imported_files(transfer: &DataTransfer) -> Vec<PathBuf> {
    existing_files(transfer.file_paths().into_iter().flatten().map(Path::to_path_buf))
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

#[derive(Debug)]
enum DropAction {
    Hover,
    Cancel,
    Drop(PathBuf),
}

#[cfg(target_os = "linux")]
thread_local! {
    static WAYLAND_DROP: std::cell::RefCell<Option<(limn_dnd::FileDnd, slint::Weak<AppWindow>)>> =
        const { std::cell::RefCell::new(None) };
    static WAYLAND_DROP_TRIED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[cfg(target_os = "linux")]
fn attach_wayland_drop(window: &slint::Window, ui: &AppWindow) {
    let already_tried = WAYLAND_DROP_TRIED.with(|tried| tried.replace(true));
    if already_tried {
        return;
    }
    let attached = window.with_winit_window(|winit_window| {
        limn_dnd::FileDnd::attach(winit_window, || {
            let _ = slint::invoke_from_event_loop(poll_wayland_drop);
        })
    });
    let Some(Some(file_dnd)) = attached else { return; };
    WAYLAND_DROP.with_borrow_mut(|state| *state = Some((file_dnd, ui.as_weak())));
}

#[cfg(target_os = "linux")]
fn poll_wayland_drop() {
    let events = WAYLAND_DROP.with_borrow(|state| {
        state.as_ref().map(|(file_dnd, _)| file_dnd.poll()).unwrap_or_default()
    });
    let Some(ui) = WAYLAND_DROP.with_borrow(|state| state.as_ref().and_then(|(_, ui)| ui.upgrade()))
    else {
        return;
    };
    for event in events {
        match event {
            limn_dnd::DndEvent::Hovered { .. } => ui.set_file_drag_active(true),
            limn_dnd::DndEvent::Left | limn_dnd::DndEvent::DragEnded => ui.set_file_drag_active(false),
            limn_dnd::DndEvent::Dropped { paths, .. } => {
                ui.set_file_drag_active(false);
                append_clips(&ui, &paths);
            }
        }
    }
}

fn drop_action(event: &winit::event::WindowEvent) -> Option<DropAction> {
    match event {
        winit::event::WindowEvent::HoveredFile(_) => Some(DropAction::Hover),
        winit::event::WindowEvent::HoveredFileCancelled => Some(DropAction::Cancel),
        winit::event::WindowEvent::DroppedFile(path) => Some(DropAction::Drop(path.clone())),
        _ => None,
    }
}

fn handle_drop_event(ui: &AppWindow, event: &winit::event::WindowEvent) {
    match drop_action(event) {
        Some(DropAction::Hover) => ui.set_file_drag_active(true),
        Some(DropAction::Cancel) => ui.set_file_drag_active(false),
        Some(DropAction::Drop(path)) => {
            ui.set_file_drag_active(false);
            append_clips(ui, std::slice::from_ref(&path));
        }
        None => {}
    }
}

fn connect_import(ui: &AppWindow) {
    let drop_handle = ui.as_weak();
    ui.on_files_dropped(move |transfer| {
        let Some(ui) = drop_handle.upgrade() else { return };
        append_clips(&ui, &imported_files(&transfer));
    });

    // NOTE: the winit backend never feeds DropArea, so file drops arrive as raw window events.
    let event_handle = ui.as_weak();
    ui.window().on_winit_window_event(move |window, event| {
        if let Some(ui) = event_handle.upgrade() {
            #[cfg(target_os = "linux")]
            attach_wayland_drop(window, &ui);
            handle_drop_event(&ui, event);
        }
        EventResult::Propagate
    });
}

fn connect_picker(ui: &AppWindow) {
    const VIDEO_EXTENSIONS: [&str; 6] = ["mp4", "mkv", "mov", "avi", "webm", "m4v"];

    #[cfg(target_os = "linux")]
    {
        use std::cell::RefCell;
        use std::rc::Rc;
        use std::sync::mpsc;
        use std::time::Duration;

        let pending: Rc<RefCell<Option<mpsc::Receiver<Vec<PathBuf>>>>> = Rc::new(RefCell::new(None));
        let timer = Rc::new(slint::Timer::default());
        let click_handle = ui.as_weak();
        let click_pending = pending.clone();
        let click_timer = timer.clone();
        ui.on_add_videos_requested(move || {
            if click_pending.borrow().is_some() {
                return;
            }
            // NOTE: the portal dialog lives in another process, so a worker thread can block on it.
            let (sender, receiver) = mpsc::channel();
            *click_pending.borrow_mut() = Some(receiver);
            std::thread::spawn(move || {
                let files = rfd::FileDialog::new()
                    .add_filter("Videos", &VIDEO_EXTENSIONS)
                    .pick_files()
                    .unwrap_or_default();
                let _ = sender.send(files);
            });

            let poll_handle = click_handle.clone();
            let poll_pending = click_pending.clone();
            let poll_timer = click_timer.clone();
            click_timer.start(slint::TimerMode::Repeated, Duration::from_millis(100), move || {
                let received = poll_pending
                    .borrow_mut()
                    .as_ref()
                    .and_then(|receiver| receiver.try_recv().ok());
                let Some(files) = received else { return; };
                *poll_pending.borrow_mut() = None;
                poll_timer.stop();
                if let Some(ui) = poll_handle.upgrade() {
                    append_clips(&ui, &existing_files(files));
                }
            });
        });
    }

    #[cfg(not(target_os = "linux"))]
    {
        // NOTE: the native dialogs on Windows and macOS must run on the main thread.
        let handle = ui.as_weak();
        ui.on_add_videos_requested(move || {
            let files = rfd::FileDialog::new()
                .add_filter("Videos", &VIDEO_EXTENSIONS)
                .pick_files()
                .unwrap_or_default();
            if let Some(ui) = handle.upgrade() {
                append_clips(&ui, &existing_files(files));
            }
        });
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let ui = AppWindow::new()?;
    connect_import(&ui);
    connect_picker(&ui);

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

    let result = ui.run();

    // NOTE: the worker must be joined while the window's surface is still alive; the thread-local
    // would otherwise only be dropped after `ui`.
    #[cfg(target_os = "linux")]
    WAYLAND_DROP.with_borrow_mut(|state| state.take());

    result?;
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
    fn drop_events_map_to_the_overlay_and_the_imported_path() {
        let file = PathBuf::from("/videos/clip.mp4");
        assert!(matches!(
            drop_action(&winit::event::WindowEvent::HoveredFile(file.clone())),
            Some(DropAction::Hover)
        ));
        assert!(matches!(
            drop_action(&winit::event::WindowEvent::HoveredFileCancelled),
            Some(DropAction::Cancel)
        ));
        match drop_action(&winit::event::WindowEvent::DroppedFile(file.clone())) {
            Some(DropAction::Drop(path)) => assert_eq!(path, file),
            other => panic!("expected a dropped path, got {other:?}"),
        }
        assert!(drop_action(&winit::event::WindowEvent::Focused(true)).is_none());
    }
}
