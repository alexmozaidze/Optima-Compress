use std::path::{Path, PathBuf};

use gst::prelude::*;
use gstreamer as gst;

pub struct TestDirectory(pub PathBuf);

impl TestDirectory {
    pub fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!("optima-media-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&path).expect("create media test directory");
        Self(path)
    }

    pub fn file(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).expect("remove media test directory");
    }
}

pub fn write_media(description: &str, path: &Path) {
    gst::init().expect("initialize GStreamer for fixtures");
    let pipeline = gst::parse::launch(description)
        .expect("create fixture pipeline")
        .downcast::<gst::Pipeline>()
        .expect("fixture must be a pipeline");
    pipeline
        .by_name("output")
        .expect("fixture output")
        .set_property("location", path.to_str().expect("UTF-8 fixture path"));
    pipeline
        .set_state(gst::State::Playing)
        .expect("start fixture pipeline");
    let message = pipeline.bus().expect("fixture bus").timed_pop_filtered(
        gst::ClockTime::from_seconds(10),
        &[gst::MessageType::Eos, gst::MessageType::Error],
    );
    pipeline
        .set_state(gst::State::Null)
        .expect("stop fixture pipeline");
    match message.as_ref().map(|message| message.view()) {
        Some(gst::MessageView::Eos(_)) => {}
        Some(gst::MessageView::Error(error)) => panic!("fixture failed: {}", error.error()),
        _ => panic!("fixture pipeline timed out"),
    }
}
