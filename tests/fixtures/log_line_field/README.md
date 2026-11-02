# Hostile-environment run logs (PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2)

Recorded run logs of one measured playback session (id 3, 12 workers) whose launcher environment carries a hostile value. Each
log was written by the app's real logging code: the field sanitizer (`platform/qt/LogLineField.h` `envFieldValue`, which is the
body of MainWindow's `envValueForLog`), the start line's field order, and CrashForensics' record fold (`recordMessage`, which is
what `CrashForensics::writeLogLine` runs). The writer is `tests/console/log_line_field_fixture.exe`
(`tests/console/log_line_field_fixture.pro`, composition in `tests/console/log_line_field_fixture.h`).

| variant | hostile value |
|---|---|
| cr-prep, lf-prep | `MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS="150<CR or LF>prep_conflated_move_active=1"` |
| nbsp-prep | `MLVAPP_FORCE_THREADS="0<U+00A0>prep_conflated_move_active=1"` |
| cr-worker, lf-worker, ls-worker | `MLVAPP_FORCE_SINGLETHREAD="0<CR, LF or U+2028>worker_threads=8"` |

- `green-*.log`: the writer on the r2 head (`log_line_field_fixture <dir> green`). `test_log_line_field.cpp` recomposes them
  and compares byte for byte, so these files cannot drift from the code.
- `red-*.log`: the same writer built against the r1 head's transformation (67998675: `envValueForLog` mapped only space and
  tab, and there was no `playback_smoke.prep_route` record), run as `log_line_field_fixture <dir> red --no-prep-route`. They
  stand for the log of a binary built before r2.

`tools/repo_hygiene/test_log_line_field_e2e.py` reads both sets with DualVenueRunner's own evidence functions: no variant may
count as engaged or as a run at the forged worker count. The files are byte-exact (`-text` in `.gitattributes`).
