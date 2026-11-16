use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};

use slint::{ComponentHandle, Model, ModelRc, SharedString, VecModel};

use crate::media::{ImportBatch, VideoFile};
use crate::{AppWindow, ClipData, ClipState, EncodingOptions, size_label};

#[derive(Clone)]
pub struct Importer {
    sender: Sender<Vec<PathBuf>>,
    pending: Arc<AtomicUsize>,
    ui: slint::Weak<AppWindow>,
}

impl Importer {
    pub fn new(ui: &AppWindow) -> std::io::Result<Self> {
        let (sender, receiver) = mpsc::channel();
        let pending = Arc::new(AtomicUsize::new(0));
        let worker_pending = pending.clone();
        let handle = ui.as_weak();
        std::thread::Builder::new()
            .name("video-discovery".into())
            .spawn(move || run_imports(receiver, handle, worker_pending))?;
        Ok(Self {
            sender,
            pending,
            ui: ui.as_weak(),
        })
    }

    pub fn import_files(&self, files: Vec<PathBuf>) {
        if files.is_empty() {
            return;
        }
        let Some(ui) = self.ui.upgrade() else { return };
        if self.pending.fetch_add(1, Ordering::Relaxed) == 0 {
            ui.set_import_message(SharedString::default());
        }
        ui.set_importing(true);
        if let Err(error) = self.sender.send(files) {
            let remaining = self.pending.fetch_sub(1, Ordering::Relaxed) - 1;
            ui.set_importing(remaining > 0);
            add_message(&ui, format!("Cannot queue video discovery: {error}"));
        }
    }
}

fn run_imports(
    receiver: Receiver<Vec<PathBuf>>,
    handle: slint::Weak<AppWindow>,
    pending: Arc<AtomicUsize>,
) {
    for files in receiver {
        let result = crate::media::inspect_files(files);
        let completed_pending = pending.clone();
        let delivered = handle.upgrade_in_event_loop(move |ui| {
            complete_import(&ui, result);
            let remaining = completed_pending.fetch_sub(1, Ordering::Relaxed) - 1;
            ui.set_importing(remaining > 0);
        });
        if let Err(error) = delivered {
            eprintln!("Cannot deliver video discovery results: {error}");
            break;
        }
    }
}

fn complete_import(ui: &AppWindow, result: Result<ImportBatch, String>) {
    match result {
        Ok(batch) => {
            append_clips(ui, &batch.videos);
            let message = batch
                .rejected
                .iter()
                .map(|rejection| format!("{}: {}", rejection.path.display(), rejection.error))
                .collect::<Vec<_>>()
                .join("\n");
            add_message(ui, message);
        }
        Err(error) => add_message(ui, error),
    }
}

fn add_message(ui: &AppWindow, message: String) {
    if message.is_empty() {
        return;
    }
    let previous = ui.get_import_message();
    let combined = if previous.is_empty() {
        message
    } else {
        format!("{previous}\n{message}")
    };
    ui.set_import_message(combined.into());
}

fn append_clips(ui: &AppWindow, files: &[VideoFile]) {
    if files.is_empty() {
        return;
    }
    let mut clips: Vec<ClipData> = ui.get_clips().iter().collect();
    let options = ui.get_default_options();
    clips.extend(files.iter().map(|file| clip_from_video(file, &options)));
    ui.set_clips(ModelRc::new(VecModel::from(clips)));
}

fn clip_from_video(video: &VideoFile, options: &EncodingOptions) -> ClipData {
    let path = video.path();
    let file_name = path
        .file_name()
        .unwrap_or(path.as_os_str())
        .to_string_lossy();
    ClipData {
        file_name: file_name.into_owned().into(),
        source_path: path.to_string_lossy().into_owned().into(),
        output_path: SharedString::default(),
        metadata: size_label(video.size_bytes()),
        state: ClipState::Waiting,
        progress: 0.0,
        selected: false,
        audio_tracks: ModelRc::default(),
        options: options.clone(),
    }
}
