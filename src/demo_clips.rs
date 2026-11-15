use slint::{ModelRc, SharedString, VecModel};

use crate::{AppWindow, AudioTrack, ClipData, ClipState};

fn track(name: &str, info: &str, enabled: bool) -> AudioTrack {
    AudioTrack { name: name.into(), info: info.into(), enabled }
}

fn clip(
    ui: &AppWindow,
    file_name: &str,
    metadata: &str,
    state: ClipState,
    progress: f32,
    output_path: &str,
    audio_tracks: Vec<AudioTrack>,
) -> ClipData {
    ClipData {
        file_name: file_name.into(),
        source_path: SharedString::default(),
        output_path: output_path.into(),
        metadata: metadata.into(),
        state,
        progress,
        selected: false,
        audio_tracks: ModelRc::new(VecModel::from(audio_tracks)),
        options: ui.get_default_options(),
    }
}

pub fn seed(ui: &AppWindow) {
    let clips = vec![
        clip(ui, "ShadowPlay_Clip_01.mp4", "2:45 · 1080p · 142 MB", ClipState::Waiting, 0.0, "", vec![]),
        clip(
            ui,
            "Ranked_Highlights.mp4",
            "1:12 · 1080p · 86 MB",
            ClipState::Compressing,
            0.58,
            "",
            vec![
                track("Game audio", "AAC · Stereo · 48 kHz", true),
                track("Microphone", "AAC · Mono · 48 kHz", true),
                track("Party chat", "Opus · Stereo · 48 kHz", false),
                track("Music", "AAC · Stereo · 48 kHz", false),
                track("Commentary", "AAC · Mono · 48 kHz", false),
                track("System audio", "AAC · Stereo · 48 kHz", false),
                track("Alerts", "AAC · Mono · 48 kHz", false),
                track("Voice chat", "Opus · Mono · 48 kHz", true),
                track("Sound effects", "AAC · Stereo · 48 kHz", false),
            ],
        ),
        clip(
            ui,
            "Friend_Clip.mp4",
            "0:30 · 720p · 18 MB",
            ClipState::Finished,
            1.0,
            "/home/alex/Videos/Compressed/Friend_Clip.mp4",
            vec![
                track("Desktop audio", "AAC · Stereo · 48 kHz", true),
                track("Microphone", "AAC · Mono · 44.1 kHz", true),
            ],
        ),
    ];
    ui.set_clips(ModelRc::new(VecModel::from(clips)));
}
