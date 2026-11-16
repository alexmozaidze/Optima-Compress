use super::*;
use crate::test_support::{TestDirectory, write_media};
use slint::{Model, ModelRc, VecModel};

fn wait_for_import(ui: &AppWindow) {
    let timer = slint::Timer::default();
    let handle = ui.as_weak();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    timer.start(
        slint::TimerMode::Repeated,
        std::time::Duration::from_millis(10),
        move || {
            if !handle.unwrap().get_importing() || std::time::Instant::now() >= deadline {
                slint::quit_event_loop().expect("quit test event loop");
            }
        },
    );
    slint::run_event_loop().expect("run import event loop");
    timer.stop();
    assert!(
        !ui.get_importing(),
        "Import did not finish before the deadline"
    );
}

#[test]
fn imports_validate_off_thread_preserve_edits_and_report_rejections() {
    i_slint_backend_testing::init_integration_test_with_system_time();
    let directory = TestDirectory::new("import");
    let video = directory.file("game.mkv");
    let text = directory.file("not-a-video.mp4");
    write_media(
        "videotestsrc num-buffers=3 ! video/x-raw,width=16,height=16,framerate=1/1 ! vp8enc ! matroskamux ! filesink name=output",
        &video,
    );
    std::fs::write(&text, b"This is not a video.").expect("write non-video fixture");
    let ui = AppWindow::new().expect("create test window");
    let importer = Importer::new(&ui).expect("create test importer");
    connect_import(&ui, &importer);
    let mut transfer = DataTransfer::default();
    transfer.set_file_paths([&video, &text]);
    ui.invoke_files_dropped(transfer);
    assert!(ui.get_importing());
    assert_eq!(
        ui.get_clips().row_count(),
        0,
        "Discovery must not run synchronously"
    );
    handle_drop_event(
        &ui,
        &winit::event::WindowEvent::DroppedFile(video.clone()),
        &importer,
    );
    wait_for_import(&ui);
    assert_eq!(
        ui.get_clips().row_count(),
        2,
        "Consecutive drop events must all be imported"
    );
    let clip = ui.get_clips().row_data(0).unwrap();
    assert_eq!(clip.file_name, "game.mkv");
    assert_eq!(clip.source_path, video.to_string_lossy().as_ref());
    assert_eq!(
        clip.metadata,
        size_label(std::fs::metadata(&video).unwrap().len())
    );
    assert!(!clip.selected);
    assert_eq!(clip.output_path, "");
    assert_eq!(clip.state, ClipState::Waiting);
    assert_eq!(clip.progress, 0.0);
    assert_eq!(clip.audio_tracks.row_count(), 0);
    assert_eq!(clip.options, ui.get_default_options());
    assert!(ui.get_import_message().contains("not-a-video.mp4"));

    handle_drop_event(
        &ui,
        &winit::event::WindowEvent::DroppedFile(video.clone()),
        &importer,
    );
    let mut edited = clip;
    edited.options.codec = 1;
    ui.set_clips(ModelRc::new(VecModel::from(vec![edited])));
    wait_for_import(&ui);
    assert_eq!(ui.get_clips().row_count(), 2);
    assert_eq!(ui.get_clips().row_data(0).unwrap().options.codec, 1);
    assert_eq!(
        ui.get_clips().row_data(1).unwrap().options,
        ui.get_default_options()
    );
    assert!(
        ui.get_import_message().is_empty(),
        "A successful import clears previous rejections"
    );

    handle_drop_event(
        &ui,
        &winit::event::WindowEvent::DroppedFile(text),
        &importer,
    );
    wait_for_import(&ui);
    assert_eq!(
        ui.get_clips().row_count(),
        2,
        "A rejected file must not become a clip"
    );
    let message = ui.get_import_message();
    assert!(!message.is_empty());
    importer.import_files(vec![]);
    assert!(!ui.get_importing());
    assert_eq!(
        ui.get_import_message(),
        message,
        "Cancelling the picker preserves feedback"
    );
}
