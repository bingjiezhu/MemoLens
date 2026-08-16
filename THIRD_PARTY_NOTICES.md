# Third-party runtime notices

MemoLens source code is distributed under a dual-license model: PolyForm
Noncommercial License 1.0.0 for non-commercial use, and a separate commercial
license for commercial use (see LICENSE and COMMERCIAL-LICENSE.md). The media
workflow invokes an FFmpeg/ffprobe installation supplied by the user or package
manager; the repository does not bundle an FFmpeg binary, codec library, or
model file.

## FFmpeg

- Project: <https://ffmpeg.org/>
- License information: <https://ffmpeg.org/legal.html>
- Source: <https://ffmpeg.org/download.html#get-sources>

FFmpeg builds can enable components under different licenses. MemoLens's P0
render profile requests the external `libx264` and AAC encoders, so distributors
must review the exact binary's configuration and satisfy the terms that apply to
that build. Inspect the selected runtime with:

```bash
ffmpeg -version
ffmpeg -buildconf
ffprobe -version
```

`npm run setup:mac` installs the Homebrew FFmpeg formula only when FFmpeg is not
already present. MemoLens never silently swaps the selected binary during a job.

This notice records the runtime boundary; it is not legal advice and does not
replace the upstream licenses.
