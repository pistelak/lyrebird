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
