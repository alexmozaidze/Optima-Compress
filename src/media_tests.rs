use super::*;
use crate::test_support::{TestDirectory, write_media};

#[test]
fn content_not_extension_decides_which_files_in_a_mixed_batch_are_videos() {
    let directory = TestDirectory::new("mixed");
    let video = directory.file("game #1 % тест.txt");
    let audio = directory.file("audio.mp4");
    let image = directory.file("still.mp4");
    let text = directory.file("text.mp4");
    let empty = directory.file("empty.mp4");
    let corrupt = directory.file("truncated.mkv");
    let unsupported = directory.file("unsupported.mkv");
    let missing = directory.file("missing.mp4");
    write_media(
        "videotestsrc num-buffers=3 ! video/x-raw,width=16,height=16,framerate=1/1 ! vp8enc ! matroskamux ! filesink name=output",
        &video,
    );
    write_media(
        "audiotestsrc num-buffers=3 ! wavenc ! filesink name=output",
        &audio,
    );
    write_media(
        "videotestsrc num-buffers=1 ! video/x-raw,width=16,height=16 ! pngenc ! filesink name=output",
        &image,
    );
    std::fs::write(&text, b"This is not a video.").expect("write text fixture");
    std::fs::write(&empty, []).expect("write empty fixture");
    let mut truncated = std::fs::read(&video).expect("read video fixture");
    truncated.truncate(16);
    std::fs::write(&corrupt, truncated).expect("write truncated fixture");
    let mut unknown_codec = std::fs::read(&video).expect("read video fixture");
    let codec = b"V_VP8";
    let start = unknown_codec
        .windows(codec.len())
        .position(|bytes| bytes == codec)
        .expect("VP8 codec identifier");
    unknown_codec[start..start + codec.len()].copy_from_slice(b"V_XYZ");
    std::fs::write(&unsupported, unknown_codec).expect("write unsupported codec fixture");

    let batch = inspect_files(vec![
        video.clone(),
        audio.clone(),
        image.clone(),
        text.clone(),
        empty.clone(),
        corrupt.clone(),
        unsupported.clone(),
        missing.clone(),
        directory.0.clone(),
    ])
    .expect("inspect mixed batch");

    assert_eq!(batch.videos.len(), 1, "{batch:?}");
    assert_eq!(batch.videos[0].path(), video);
    assert_eq!(
        batch.videos[0].size_bytes(),
        std::fs::metadata(&video).unwrap().len()
    );
    assert_eq!(batch.rejected.len(), 8, "{batch:?}");
    for (path, expected) in [
        (audio, VideoError::NotVideo),
        (image, VideoError::StillImage),
        (unsupported, VideoError::MissingPlugins),
    ] {
        let rejection = batch
            .rejected
            .iter()
            .find(|rejection| rejection.path == path)
            .expect("file must be rejected");
        assert_eq!(rejection.error, expected, "{rejection:?}");
    }
    for path in [missing, directory.0.clone()] {
        let rejection = batch
            .rejected
            .iter()
            .find(|rejection| rejection.path == path)
            .unwrap();
        assert!(
            matches!(rejection.error, VideoError::Unreadable(_)),
            "{rejection:?}"
        );
    }
}

#[test]
fn missing_codecs_are_not_misreported_as_non_video_files() {
    for error in [
        gst::glib::Error::new(gst::CoreError::MissingPlugin, "Missing decoder"),
        gst::glib::Error::new(gst::StreamError::CodecNotFound, "Unknown codec"),
    ] {
        assert_eq!(discovery_error(error), VideoError::MissingPlugins);
    }
}

#[test]
fn an_empty_selection_needs_no_media_discovery() {
    let batch = inspect_files(vec![]).expect("empty batch");
    assert!(batch.videos.is_empty());
    assert!(batch.rejected.is_empty());
}
