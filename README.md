## TODO

A user should be able to connect to kitsu, select revisions to review, download them, annotate, comment, and export back to kitsu.

## 1. Connect to Kitsu

- Wrap `gazu.login` (host, credentials) in the service layer
- Store access token in OS keychain so login persists across sessions
- Build login form in RV (host, credentials, "remember me")
- Handle auth failure / expired token → re-prompt login

## 2. Select Revisions to Review

- Wrap "tasks assigned to me" gazu query
- Wrap per-task revision/preview-file listing (revision #, status, uploader, date)
- Build a flat "My Tasks" panel UI
- Build revision list under each task with multi-select
- Wrap + render thumbnails via `download_preview_file_thumbnail` so selection is visual

## 3. Download

- Wrap `gazu.files.download_preview_file` (movie/image) in the service layer
- Local cache dir keyed by project/task/revision ID, with a hit-check to skip re-downloads
- Handle clip (mp4/mov) vs. image-sequence previews with consistent naming/padding
- Download queue with basic progress UI for multi-select batch downloads

## 4. Load & Annotate

- Load a downloaded file as a new RV Source via source-creation commands
- Load multi-selected files as Sequence (linear) or Stack (compare)
- Maintain in-memory `RV source ID → Kitsu task/preview-file ID` mapping (skip persisting into `.rv` session metadata for now)
- Capture annotation/paint strokes on current frame as a frame grab tied to a source

## 5. Comment

- Notes input UI in RV tied to a specific source: text + optional status change
- Hold pending note (text + status + annotated frame) in local state until export

## 6. Export Back to Kitsu

- Wrap `gazu.task.add_comment` (text + status)
- frame annotation mapping
- Batch export: submit all pending notes for the session in one action
- Basic failure handling: report which notes succeeded/failed on network error mid-batch

## 7. Packaging

- `.rvpkg` with PACKAGE manifest + Python mode, installable via `rvpkg` CLI or Preferences → Packages

## 8. Testing

- Mocked unit tests for the service wrapper, RV-independent
- Manual round-trip QA: login → select → download → annotate → comment → export → verify in Kitsu web UI