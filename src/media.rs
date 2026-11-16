use std::fmt;
use std::path::{Path, PathBuf};

use gstreamer as gst;
use gstreamer_pbutils::{Discoverer, DiscovererInfo, DiscovererResult};

#[derive(Debug)]
pub struct VideoFile {
    path: PathBuf,
    size_bytes: u64,
}

impl VideoFile {
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn size_bytes(&self) -> u64 {
        self.size_bytes
    }
}

#[derive(Debug, PartialEq)]
pub enum VideoError {
    NotVideo,
    StillImage,
    MissingPlugins,
    TimedOut,
    Unreadable(String),
    Discovery(String),
}

impl fmt::Display for VideoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotVideo => write!(formatter, "No video stream."),
            Self::StillImage => write!(formatter, "Still images are not videos."),
            Self::MissingPlugins => write!(formatter, "A required GStreamer plugin is missing."),
            Self::TimedOut => write!(formatter, "Checking this file timed out."),
            Self::Unreadable(error) => write!(formatter, "Cannot read this file: {error}"),
            Self::Discovery(error) => write!(formatter, "Cannot inspect this media: {error}"),
        }
    }
}

#[derive(Debug)]
pub struct RejectedFile {
    pub path: PathBuf,
    pub error: VideoError,
}

#[derive(Debug, Default)]
pub struct ImportBatch {
    pub videos: Vec<VideoFile>,
    pub rejected: Vec<RejectedFile>,
}

pub fn inspect_files(paths: Vec<PathBuf>) -> Result<ImportBatch, String> {
    if paths.is_empty() {
        return Ok(ImportBatch::default());
    }
    gst::init().map_err(|error| format!("Cannot initialize GStreamer: {error}"))?;
    let mut batch = ImportBatch::default();
    for path in paths {
        match inspect_file(&path) {
            Ok(video) => batch.videos.push(video),
            Err(error) => batch.rejected.push(RejectedFile { path, error }),
        }
    }
    Ok(batch)
}

fn inspect_file(path: &Path) -> Result<VideoFile, VideoError> {
    let metadata =
        std::fs::metadata(path).map_err(|error| VideoError::Unreadable(error.to_string()))?;
    if !metadata.is_file() {
        return Err(VideoError::Unreadable("Not a regular file.".into()));
    }
    std::fs::File::open(path).map_err(|error| VideoError::Unreadable(error.to_string()))?;
    let absolute_path = path
        .canonicalize()
        .map_err(|error| VideoError::Unreadable(error.to_string()))?;
    let uri = gst::glib::filename_to_uri(absolute_path, None)
        .map_err(|error| VideoError::Unreadable(error.to_string()))?;
    let info = discover_uri(&uri)?;
    match info.result() {
        DiscovererResult::Ok => {}
        DiscovererResult::Timeout => return Err(VideoError::TimedOut),
        DiscovererResult::MissingPlugins => return Err(VideoError::MissingPlugins),
        result => return Err(VideoError::Discovery(format!("{result:?}"))),
    }
    let streams = info.video_streams();
    if streams.is_empty() {
        return Err(VideoError::NotVideo);
    }
    if streams.iter().all(|stream| stream.is_image()) {
        return Err(VideoError::StillImage);
    }
    Ok(VideoFile {
        path: path.to_path_buf(),
        size_bytes: metadata.len(),
    })
}

fn discover_uri(uri: &str) -> Result<DiscovererInfo, VideoError> {
    let context = gst::glib::MainContext::new();
    context
        .with_thread_default(|| {
            let discoverer =
                Discoverer::new(gst::ClockTime::from_seconds(10)).map_err(discovery_error)?;
            let event_loop = gst::glib::MainLoop::new(Some(&context), false);
            let completion_loop = event_loop.clone();
            let (sender, receiver) = std::sync::mpsc::sync_channel(1);
            discoverer.connect_discovered(move |_, info, error| {
                let result = error.map_or_else(
                    || Ok(info.clone()),
                    |error| Err(discovery_error(error.clone())),
                );
                if sender.send(result).is_err() {
                    eprintln!("Video discovery result receiver was dropped");
                }
                completion_loop.quit();
            });
            discoverer.start();
            if let Err(error) = discoverer.discover_uri_async(uri) {
                discoverer.stop();
                return Err(VideoError::Discovery(error.to_string()));
            }
            event_loop.run();
            discoverer.stop();
            receiver
                .recv()
                .map_err(|error| VideoError::Discovery(error.to_string()))?
        })
        .map_err(|error| VideoError::Discovery(error.to_string()))?
}

fn discovery_error(error: gst::glib::Error) -> VideoError {
    if error.matches(gst::CoreError::MissingPlugin)
        || error.matches(gst::StreamError::CodecNotFound)
    {
        return VideoError::MissingPlugins;
    }
    if error.matches(gst::StreamError::TypeNotFound) {
        return VideoError::NotVideo;
    }
    if error.domain() == <gst::ResourceError as gst::glib::error::ErrorDomain>::domain() {
        return VideoError::Unreadable(error.to_string());
    }
    VideoError::Discovery(error.to_string())
}

#[cfg(test)]
#[path = "media_tests.rs"]
mod tests;
