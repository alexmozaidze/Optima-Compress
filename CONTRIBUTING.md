# Contributing

## Formatting

`.editorconfig` is authoritative: spaces, 4 per level, LF, final newline. The `.slint` file is
hand-written and follows the same rules; it is not a generated artifact.

## UI and backend boundary

Keep UI behavior and backend logic separate at all times. UI state, selection, option editing,
and interaction handling belong in `.slint` files and must work in the standalone viewer
without Rust callback handlers.

Rust hosts the window and executes backend processing only when the user requests compression.
Exchange inputs and results through explicit data and callback interfaces. Backend code must
not own UI state, and UI components must not perform encoding work.

## State

Window properties are the single source of truth. Components take `in` / `in-out` properties and
report changes through callbacks, never by reaching into a sibling.

## Design tokens

Colours and metrics shared by more than one element belong in the `Theme` global. A literal is
acceptable for a one-off shade that nothing else uses.

## UI patterns

- **Segmented selectors** — a bar with a single sliding pill, cells built from `SegmentedOption`.
  Cells set `preferred-width: 0` so the layout splits the bar into equal parts, and the pill's
  `x` / `width` are computed from the bar width and the cell count. One `TouchArea` per bar
  handles both click and drag; per-cell touch areas cannot track a drag across cells.
- **Reveals** — bind the container's `height` to the state that reveals it, set `clip: true`, and
  give the children fixed heights. Text without an explicit height re-centres as the container
  grows and visibly drifts during the animation.
- **Ids on interactive elements** — they are how the runtime inspector finds controls; a
  component loses that ability if only its instances are anonymous.
- **No image assets** — illustrative imagery is drawn with gradients and paths, so the UI stays a
  single file and needs no asset pipeline.

## Slint behaviours that fail silently

- There is no dashed border; dash strips are drawn from repeated rectangles.
- `viewbox-width` / `viewbox-height` are unitless path coordinates, not lengths.
- Layouts place fixed-size children at the start of the cross axis. Wrap them in a layout with
  `alignment: center` to centre them.
- Cells are sized by their preferred width even when stretched; overriding preferred width is
  what equalises them.
- `root` is the component root, not the enclosing element, so geometry must come from an element
  id.
- `Math.floor` returns an `int`, so index maths from a pointer position needs no conversion.

## Slint sizing and layout traps

- `rem` resolves against `Window.default-font-size`, which a `global` does not have, so the build
  rejects a `rem` literal in a `Theme` property of type `length` ("Cannot convert between rem and
  logical length in a global component"). Declare the token `<relative-font-size>` and let the
  element that uses it convert.
- A property that feeds a layout while also reading that layout's computed `height` is a binding
  loop, reported at build time. Derive the value from intrinsic properties instead, such as a
  reveal's own height.
- A `ScrollView` shows its scrollbar whenever `content-height` exceeds `visible-height`, with no
  tolerance for rounding. Clamp `content-height` to `Math.max(self.visible-height, ...)` so a
  fractional overshoot cannot surface it.
