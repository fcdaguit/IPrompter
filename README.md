# TermPrompter for Windows (64-bit)

Offline, voice-following teleprompter. Black screen, neon-green terminal text, no ads, no login, no network use.
Same matching engine as the Android version: English, Filipino and Bisaya; follows your pace; pauses on ad-libs.

## Get the app

1. Put the **contents** of this folder in a new GitHub repo (not the folder itself). Make sure `.github/workflows/build-windows.yml` is there.
2. Actions > **Build Windows** > Run workflow.
3. When green, open the run and download **TermPrompter-Windows-x64** under *Artifacts*.
4. Unzip anywhere and run `TermPrompter\TermPrompter.exe`. Keep the folder together; there is no installer.

Windows SmartScreen may warn about an unsigned app: More info > Run anyway.

## Languages / models

| Language | Model |
|---|---|
| English | `vosk-model-small-en-us-0.15`, bundled by the build |
| Filipino | Import a Vosk Filipino model `.zip` (see alphacephei.com/vosk/models; check the current list) |
| Bisaya | Import a Bisaya model if you have one. I know of no ready-made one, so Bisaya falls back to the Filipino model |

Import: **H** (panel) > Lang > Import voice model (.zip) > pick the language > pick the zip. Imported models are stored in `%APPDATA%\TermPrompter\models`.
Bisaya mode also treats o/u, e/i, c/k, ph/f and doubled letters as equal and allows more spelling drift. If it pauses too often, raise tolerance to 4-5.

## Keys

Space or click: play / pause / resume. R: reset. H: settings panel. F11: full screen. Esc: leave full screen. + / -: text size. Ctrl+Q: quit.
Mouse wheel scrolls manually at any time. `[text in brackets]` is shown but not expected to be spoken.

## Notes

- Needs a working microphone (set as Windows default input). Timer mode needs none.
- Scripts are saved in `%APPDATA%\TermPrompter\scripts`. A crash log, if any, is `%APPDATA%\TermPrompter\crash.log`.
- Write numbers as words in your script ("twenty twenty-six"); digits will not match speech.
- Not yet run on Windows. The matcher is tested; the window, microphone and packaging are untested, so expect small fixes on first build.
