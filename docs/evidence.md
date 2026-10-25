# Evidence: hbstore-tools

## Lessons from hardware (2026-10-04/05)
- **Icons must be baseline JPEGs.** ns shrinks the icon to 174×174 through capsrv's jpegdec,
  which runs out of memory on progressive JPEGs: the title never gets its name/icon cache,
  the HOME tile stays blank and launch fails **2206-1017**. `jpeg_256` re-encodes anything
  that is not `JPEG 256 256 None`.
- **The loader's exit**, in order: end the process before unmapping the app (its threads
  data-abort otherwise), zero the heap used as copy buffer, and send
  `ISelfController::Exit` (appletOE → OpenApplicationProxy → GetSelfController → Exit)
  before `svcExitProcess` — an NRO never does it, and without it every exit is "The
  software was closed because an error occurred".
- A stuck half-installed title is removed with **Goldleaf** (ns `DeleteApplicationCompletely`);
  the tile's + menu and Data Management cannot.
- ftpd 3.2.1 crashes on exit on 22.5.0 everywhere (upstream, not the loader).
