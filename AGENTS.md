# Optima-Compress

A Slint desktop app for compressing gameplay clips to the size limits of the places they get
shared to. The UI is currently the whole product: `ui/app-window.slint` composes the interface
from shared theme and component modules. The Rust side is still the template's entry point
with no application logic.

## Setup

Rust toolchain, plus the Slint CLI tools on `PATH`: `slint-viewer` (compile check, headless
render) and `slint-lsp` (editor support). Take both from the Slint release matching the version
pinned in `Cargo.toml`.

## Build

`cargo` is the entry point. `build.rs` compiles the UI markup into the binary, so `cargo build`
fails on `.slint` errors as well as Rust ones, and `cargo run` opens the window.

## Verify before finishing

Prototype interactions must work in `slint-viewer --auto-reload ui/app-window.slint`, which
does not run Rust callback handlers. A passing compiled-app check alone does not verify this
workflow.

UI work is not done until it has been looked at. Three levels, cheapest first:

- `slint-viewer --check ui/app-window.slint` — diagnostics for the markup alone.
- `slint-viewer --screenshot ~/tmp/ui.png ui/app-window.slint` — renders at the component's
  preferred size with no display server (no X, no Xvfb). Write the state to a JSON file and pass
  its path: `--load-data ~/tmp/state.json`, e.g. `{"codec":0}`. Inline JSON is not accepted.
- Interactive behaviour needs the app's embedded MCP server:

  ```
  SLINT_EMIT_DEBUG_INFO=1 cargo build --features slint/mcp
  SLINT_MCP_PORT=9315 SLINT_BACKEND=headless ./target/debug/slint-rust-template
  ```

  Point your MCP client at `http://127.0.0.1:9315/mcp`. `SLINT_EMIT_DEBUG_INFO` must be set at
  build time, otherwise element ids are unavailable to the inspector. Issue pointer actions one
  at a time; parallel clicks drop one.

  With that debug build ready, `python3 tests/ui_selection.py` runs the selection and per-video
  option regression checks in its own short-lived headless app processes.

## Map

- `ui/` — the entire interface: theme tokens, components, layout, interaction state.
- `src/` — host entry point and template glue.
- `build.rs` — compiles the UI markup.

## Conventions

Follow the [UI and backend boundary](CONTRIBUTING.md#ui-and-backend-boundary) for every change.

See [CONTRIBUTING.md](CONTRIBUTING.md) for formatting, the component and theme patterns, and the
Slint behaviours that fail silently.
