# Test fixtures

`face.mjpeg`: a camera picture with a face, for Chromium's fake camera
(`--use-file-for-fake-video-capture=face.mjpeg`; one JPEG frame, looped).
It is the 1984 U.S. Navy portrait of Commodore Grace M. Hopper, a work of the
U.S. federal government and therefore in the public domain (the copy shipped as
matplotlib's `grace_hopper.jpg` sample), scaled and padded to 640x480.

`speech.py`: English speech for Chromium's fake microphone
(`--use-file-for-fake-audio-capture=<wav>`, looped), spoken at test time by
macOS `say` (or `espeak-ng` on Linux) into a temporary directory, so no
recording is stored here and nothing new appears in the project during a run.
