import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct BrowserTokenTests {
        /// Sharing row insets must not merge a wrapping traffic path with the heavier, truncated flow path,
        /// or move the numbered flow gutter; these are the original insets, gaps and fonts at both widths.
        @Test func requestCellsKeepTheirDistinctTypographyAndGeometry() throws {
            let entry = RecentEntry(id: "request", method: "GET", path: "/api/items/🚀/details", status: 200)
            var flow = try #require(
                RuleFormatting.flowSections(BrowserPreview.snapshot("remove-an-item")).first?.rows.first)
            flow.request.path = entry.path
            for width: CGFloat in [300, 600] {
                let recent = RecentRequestCell(entry)
                recent.setFrameSize(NSSize(width: width, height: RecentRequestCell.height(entry, width: width)))
                recent.layoutSubtreeIfNeeded()
                let path = try #require(fields(recent).first { $0.stringValue == entry.path })
                #expect(path.font == NSFont.monospacedSystemFont(ofSize: 12, weight: .regular))
                #expect(path.lineBreakMode == .byWordWrapping)
                #expect(path.maximumNumberOfLines == 2)
                // The inset positions the label's alignment rect; an NSTextField's frame overhangs it by
                // its alignmentRectInsets, so comparing frames measures AppKit's padding, not the inset.
                let aligned = path.alignmentRect(forFrame: path.frame)
                #expect(aligned.minX == 16)
                #expect(aligned.maxX == width - 16)
                let header = try #require(recent.subviews.first as? NSStackView)
                #expect(header.frame.minX == 16)
                #expect(header.frame.maxX == width - 16)
                #expect(topInset(header, in: recent) == 8)
                #expect((recent.isFlipped ? aligned.minY - header.frame.maxY : header.frame.minY - aligned.maxY) == 6)
                #expect(bottomInsetLimit(path, in: recent) == -8)

                for number: Int? in [nil, 1] {
                    flow.number = number
                    let cell = FlowRequestCell(flow)
                    cell.setFrameSize(NSSize(width: width, height: FlowRequestCell.height(flow, width: width)))
                    cell.layoutSubtreeIfNeeded()
                    let labels = fields(cell)
                    let flowPath = try #require(labels.first { $0.stringValue == entry.path })
                    let subtitle = try #require(labels.first { $0.stringValue == flow.subtitle })
                    #expect(flowPath.font == NSFont.monospacedSystemFont(ofSize: 13, weight: .medium))
                    #expect(flowPath.lineBreakMode == .byTruncatingMiddle)
                    #expect(flowPath.maximumNumberOfLines == 2)
                    #expect(subtitle.font == NSFont.systemFont(ofSize: 12))
                    let column = try #require(cell.subviews.first as? NSStackView)
                    #expect(column.frame.minX == (number == nil ? 16 : 48))
                    #expect(column.frame.maxX == width - 16)
                    #expect(topInset(column, in: cell) == 8)
                    #expect(bottomInsetLimit(column, in: cell) == -8)
                    if number != nil {
                        let badge = try #require(cell.subviews.compactMap { $0 as? BadgeView }.first)
                        #expect(badge.frame.minX == 14)
                        #expect(topInset(badge, in: cell) == 8)
                    }
                    #expect(column.spacing == 4)
                    #expect(column.arrangedSubviews.compactMap { ($0 as? NSStackView)?.spacing } == [8, 8])
                }
            }
        }

        /// Badge extraction must preserve the circle's regular numeral and the pill's semibold text,
        /// including the distinct neutral and tinted fills when the very same view changes appearance.
        @Test func badgesKeepOriginalFontsDimensionsAndPixels() throws {
            for circle in [false, true] {
                for tint: NSColor? in [nil, BrowserAppearance.sequence] {
                    let badge = BadgeView("GET", tint: tint, circle: circle)
                    let font = NSFont.systemFont(ofSize: 11, weight: circle ? .regular : .semibold)
                    // Measured the way the badge measures it, centring included: AppKit widens a centred
                    // label's intrinsicContentSize by 4 points, so a left-aligned reference would pin a
                    // width the badge never had and fail for a reason that is not a design change.
                    let label = NSTextField(labelWithString: "GET")
                    label.font = font
                    label.alignment = .center
                    let width = circle ? 23 : ceil(label.intrinsicContentSize.width) + 10
                    let height: CGFloat = circle ? 23 : 18
                    #expect(badge.label.font == font)
                    #expect(badge.constraints.first { $0.firstAttribute == .width }?.constant == width)
                    #expect(badge.constraints.first { $0.firstAttribute == .height }?.constant == height)
                    badge.setFrameSize(NSSize(width: width, height: height))
                    for highlighted in [false, true] {
                        badge.highlighted = highlighted
                        for name: NSAppearance.Name in [
                            .aqua, .darkAqua, .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua, .aqua,
                        ] {
                            badge.appearance = NSAppearance(named: name)
                            let actual = try bitmap(badge) { badge.draw(badge.bounds) }
                            let expected = try bitmap(badge) {
                                let rect = badge.bounds.insetBy(dx: 0.5, dy: 0.5)
                                if circle {
                                    (highlighted ? NSColor.alternateSelectedControlTextColor : NSColor.separatorColor)
                                        .setStroke()
                                    NSBezierPath(ovalIn: rect).stroke()
                                } else {
                                    (highlighted
                                        ? NSColor.alternateSelectedControlTextColor.withAlphaComponent(0.14)
                                        : tint?.withAlphaComponent(0.14) ?? NSColor.labelColor.withAlphaComponent(0.12))
                                        .setFill()
                                    NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4).fill()
                                }
                            }
                            #expect(
                                actual == expected,
                                Comment(rawValue: bitmapDifference(actual, expected, appearance: name)))
                        }
                    }
                }
            }
        }

        private func topInset(_ view: NSView, in parent: NSView) -> CGFloat {
            parent.isFlipped ? view.frame.minY - parent.bounds.minY : parent.bounds.maxY - view.frame.maxY
        }

        /// This is a minimum margin, not the laid-out gap: short content need not reach the bottom.
        private func bottomInsetLimit(_ view: NSView, in parent: NSView) -> CGFloat? {
            parent.constraints.first {
                $0.firstItem === view && $0.firstAttribute == .bottom
                    && $0.secondItem === parent && $0.secondAttribute == .bottom
                    && $0.relation == .lessThanOrEqual && $0.multiplier == 1
            }?.constant
        }
    }
}
