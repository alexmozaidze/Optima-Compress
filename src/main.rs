// Prevent console window in addition to Slint window in Windows release builds when, e.g., starting the app via file manager. Ignored on other platforms.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::error::Error;
use std::path::{Path, PathBuf};

use slint::winit_030::{EventResult, WinitWindowAccessor, winit};
use slint::{DataTransfer, SharedString};

use import::Importer;

slint::include_modules!();

mod media;
mod import;

#[cfg(test)]
mod test_support;

#[cfg(test)]
mod import_tests;

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

fn imported_paths(transfer: &DataTransfer) -> Vec<PathBuf> {
    transfer.file_paths().into_iter().flatten().map(Path::to_path_buf).collect()
}

#[derive(Debug)]
enum DropAction {
    Hover,
    Cancel,
    Drop(PathBuf),
}

#[cfg(target_os = "linux")]
thread_local! {
    static WAYLAND_DROP: std::cell::RefCell<Option<(limn_dnd::FileDnd, slint::Weak<AppWindow>, Importer)>> =
        const { std::cell::RefCell::new(None) };
    static WAYLAND_DROP_TRIED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[cfg(target_os = "linux")]
fn attach_wayland_drop(window: &slint::Window, ui: &AppWindow, importer: &Importer) {
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
    WAYLAND_DROP.with_borrow_mut(|state| *state = Some((file_dnd, ui.as_weak(), importer.clone())));
}

#[cfg(target_os = "linux")]
fn poll_wayland_drop() {
    let events = WAYLAND_DROP.with_borrow(|state| {
        state.as_ref().map(|(file_dnd, _, _)| file_dnd.poll()).unwrap_or_default()
    });
    let Some((ui, importer)) = WAYLAND_DROP.with_borrow(|state| {
        state.as_ref().and_then(|(_, ui, importer)| ui.upgrade().map(|ui| (ui, importer.clone())))
    })
    else {
        return;
    };
    for event in events {
        match event {
            limn_dnd::DndEvent::Hovered { .. } => ui.set_file_drag_active(true),
            limn_dnd::DndEvent::Left | limn_dnd::DndEvent::DragEnded => ui.set_file_drag_active(false),
            limn_dnd::DndEvent::Dropped { paths, .. } => {
                ui.set_file_drag_active(false);
                importer.import_files(paths);
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

fn handle_drop_event(ui: &AppWindow, event: &winit::event::WindowEvent, importer: &Importer) {
    match drop_action(event) {
        Some(DropAction::Hover) => ui.set_file_drag_active(true),
        Some(DropAction::Cancel) => ui.set_file_drag_active(false),
        Some(DropAction::Drop(path)) => {
            ui.set_file_drag_active(false);
            importer.import_files(vec![path]);
        }
        None => {}
    }
}

fn connect_import(ui: &AppWindow, importer: &Importer) {
    let drop_importer = importer.clone();
    ui.on_files_dropped(move |transfer| {
        drop_importer.import_files(imported_paths(&transfer));
    });

    // NOTE: the winit backend never feeds DropArea, so file drops arrive as raw window events.
    let event_handle = ui.as_weak();
    let event_importer = importer.clone();
    ui.window().on_winit_window_event(move |window, event| {
        if let Some(ui) = event_handle.upgrade() {
            #[cfg(target_os = "linux")]
            attach_wayland_drop(window, &ui, &event_importer);
            handle_drop_event(&ui, event, &event_importer);
        }
        EventResult::Propagate
    });
}

fn connect_picker(ui: &AppWindow, importer: &Importer) {
    const VIDEO_EXTENSIONS: [&str; 6] = ["mp4", "mkv", "mov", "avi", "webm", "m4v"];

    #[cfg(target_os = "linux")]
    {
        use std::cell::RefCell;
        use std::rc::Rc;
        use std::sync::mpsc;
        use std::time::Duration;

        let pending: Rc<RefCell<Option<mpsc::Receiver<Vec<PathBuf>>>>> = Rc::new(RefCell::new(None));
        let timer = Rc::new(slint::Timer::default());
        let click_importer = importer.clone();
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

            let poll_importer = click_importer.clone();
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
                poll_importer.import_files(files);
            });
        });
    }

    #[cfg(not(target_os = "linux"))]
    {
        // NOTE: the native dialogs on Windows and macOS must run on the main thread.
        let picker_importer = importer.clone();
        ui.on_add_videos_requested(move || {
            let files = rfd::FileDialog::new()
                .add_filter("Videos", &VIDEO_EXTENSIONS)
                .pick_files()
                .unwrap_or_default();
            picker_importer.import_files(files);
        });
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let ui = AppWindow::new()?;
    let importer = Importer::new(&ui)?;
    connect_import(&ui, &importer);
    connect_picker(&ui, &importer);

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
    fn imported_paths_preserves_invalid_paths_for_rejection_feedback() {
        let directory = scratch_directory("transfer");
        let file = directory.join("clip.mp4");
        std::fs::write(&file, b"clip").expect("write clip");

        let mut transfer = DataTransfer::default();
        transfer.set_file_paths([&file, &directory, &directory.join("missing.mp4")]);

        assert_eq!(imported_paths(&transfer), vec![file, directory.clone(), directory.join("missing.mp4")]);

        std::fs::remove_dir_all(&directory).expect("remove scratch");
    }

    #[test]
    fn imported_files_rejects_non_video_with_a_video_extension() {
        let directory = scratch_directory("disguised");
        let file = directory.join("not-a-video.mp4");
        std::fs::write(&file, b"This is not a video.").expect("write non-video");
        let mut transfer = DataTransfer::default();
        transfer.set_file_paths([&file]);

        let batch = media::inspect_files(imported_paths(&transfer)).expect("inspect dropped files");
        assert!(batch.videos.is_empty(), "A renamed text file must not be imported");
        assert_eq!(batch.rejected.len(), 1);

        std::fs::remove_dir_all(&directory).expect("remove scratch");
    }

    #[test]
    fn imported_paths_is_empty_without_file_paths() {
        assert!(imported_paths(&DataTransfer::default()).is_empty());
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
