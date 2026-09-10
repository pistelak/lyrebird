# macOS interface review

The AppKit rewrite was checkpointed at `c96f1df` before this review began.
The review covers the scenario browser, detail reader, menu-bar controls, Settings,
keyboard navigation, appearance changes, and window lifecycle. A passing unit test
alone does not establish visual completion.

## Guidance

- [Designing for macOS](https://developer.apple.com/design/human-interface-guidelines/designing-for-macos): window flexibility, menu commands, keyboard input, and comfortable density.
- [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars): standard show/hide behavior, manageable hierarchy, disclosure controls, and system sidebar sizing.
- [Toolbars](https://developer.apple.com/design/human-interface-guidelines/toolbars): deliberate item selection, contextual titles, and menu equivalents for actions.
- [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility): contrast, readable text, multiple status cues, keyboard access, and VoiceOver descriptions.
- [Color](https://developer.apple.com/design/human-interface-guidelines/color): semantic colors and adaptation to light, dark, and increased contrast appearances.

## Verification ledger

| Area | Finding or requirement | Evidence needed |
| --- | --- | --- |
| Detail repaint | Rounded card edges can remain after content shrinks or reflows. | Repaint regression test; repeated long/short selection and scrolling in both appearances. |
| Transition cards | Response links lack the inset used by surrounding content. | Paragraph geometry test plus rendered transition with link. |
| JSON contrast | Bright system green is insufficient on the light card. | Measured syntax contrast in light, dark, and increased contrast appearances. |
| Selection | Colored badges must remain readable in focused and unfocused selected rows. | Screenshots and contrast checks for both selection states. |
| Sidebar | System sizing and disclosure behavior must remain usable with many grouped scenarios. | Small/medium/large sizing review; keyboard expand/collapse and selection checks. |
| Commands | Browsing and activating scenarios must be distinct and activation discoverable without a mouse-only gesture. | Menu and keyboard interaction checks, with no accidental activation on selection. |
| Toolbar | Search stays removed; status remains legible; divider stops below toolbar. | Windowed/full-screen screenshots at narrow and wide sizes. |
| Settings | Fields, explanatory text, errors, and buttons must fit and be keyboard accessible. | Rendered settings window, invalid-input and save/cancel checks. |
| Accessibility | Controls have meaningful names and navigation order; information is not color-only. | Accessibility tree and keyboard review for every window and menu. |
| Appearance | Surfaces, text, separators, and controls adapt without stale rendering. | Light/dark and increased contrast review, including live appearance changes. |
| Lifecycle | Launch, reopen, resize, collapse, and full-screen transitions remain stable. | App tests and installed Release smoke check. |

Open rows remain part of the work; they are not waived by the existing test suite.

## Verified changes

- Sidebar rows now use AppKit's default sizing preference. Existing cells receive
  the effective size through `NSTableCellView.rowSizeStyle`; labels and symbols
  follow it. Header-fit checks pass at small, medium, and large sizes. The light
  browser screenshot from the UI suite was inspected; live system-preference
  changes and keyboard disclosure still need review.
- Browsing does not activate a scenario. A visible Activate button and a File
  menu command with Command-Return provide explicit activation; validation rejects
  active, missing, or stale selections. Controller tests cover those conditions.
- Patch details explain merging once, label the payload as Changes, and retain
  configured status, array strategy, and delay. A detail-rendering test checks
  both the removed repetition and retained behavior information.
- Menu-bar glyphs remain template images in every status. The colored indicator
  is a separate, non-interactive view, allowing AppKit to tint the glyph for the
  menu-bar background and selection. Template tests cover all six status cases;
  installed appearance still requires visual confirmation.

The latest `make check-app` run passed 206 tests (241 executions including
parameters), with no failures or skips. This does not close the remaining visual,
keyboard, Settings, or accessibility review items above.
