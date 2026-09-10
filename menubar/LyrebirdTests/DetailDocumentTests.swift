import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct DetailDocumentTests {
        /// A presentation extraction can keep the words while shifting a card boundary, changing a badge weight,
        /// or losing UTF-16 ranges; this literal fixture records the controller's original attributed output.
        @Test func detailDocumentKeepsOriginalAttributesAndPixels() throws {
            let document = DetailDocument()
            document.section("Request")
            document.request("GET", "/api/items/🚀")
            document.query(["q": .string("café"), "page": .int(2)])
            document.bodyContains("needle")
            document.section("Response")
            document.mode(.replace)
            document.facts([.init("X-Trace", "sample")])
            document.body(.string("café 🚀"), title: "Payload", note: "Inherited")
            document.section("Related")
            document.link("Open item 🚀", token: "lyrebird-rule:0")
            document.line("End", secondary: true)
            let expected = try originalDocument()
            let actual = document.attributedString
            #expect(actual.isEqual(to: expected))
            #expect(document.copyText == "\"café 🚀\"")
            #expect(document.copyRange == (expected.string as NSString).range(of: "Payload"))

            let view = textView(actual)
            let reference = textView(expected)
            for width: CGFloat in [240, 480] {
                view.setFrameSize(NSSize(width: width, height: 1_200))
                reference.setFrameSize(NSSize(width: width, height: 1_200))
                for name: NSAppearance.Name in [
                    .aqua, .darkAqua, .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua, .aqua,
                ] {
                    view.appearance = NSAppearance(named: name)
                    reference.appearance = view.appearance
                    let rendered = try bitmap(view) { view.draw(view.bounds) }
                    let baseline = try bitmap(reference) { reference.draw(reference.bounds) }
                    #expect(rendered == baseline)
                }
            }
        }

        /// Closing an empty or final section must not manufacture an empty card or let a later section mutate
        /// a previously produced document, since a controller replaces the whole text storage on refresh.
        @Test func emptyAndFinalSectionsKeepTheirCardBoundaries() throws {
            let document = DetailDocument()
            #expect(document.attributedString.length == 0)
            #expect(document.copyText == nil)
            #expect(document.copyRange == nil)
            document.section("Empty")
            document.line("")
            document.facts([])
            let empty = document.attributedString
            #expect(empty.string == "Empty\n")
            #expect(empty.attribute(.detailCard, at: 0, effectiveRange: nil) == nil)
            document.section("Last")
            document.line("Value")
            let populated = document.attributedString
            let value = (populated.string as NSString).range(of: "Value")
            #expect(populated.attribute(.detailCard, at: value.location, effectiveRange: nil) as? Int == 0)
            #expect(document.attributedString.isEqual(to: populated))
            document.section("Next")
            document.line("Another")
            #expect(empty.string == "Empty\n")
            #expect(populated.string == "Empty\nLast\nValue\n")
            let next = document.attributedString
            let another = (next.string as NSString).range(of: "Another")
            #expect(next.attribute(.detailCard, at: another.location, effectiveRange: nil) as? Int == 1)
        }

        /// Reusing the same drawing objects across appearances must still resolve the dynamic palette, and
        /// extracting badge and content insets must leave the original card, separator and badge pixels intact.
        @Test func detailDecorationsKeepOriginalGeometryAcrossAppearances() throws {
            let view = textView(try originalDocument())
            let manager = try #require(view.layoutManager as? DetailLayoutManager)
            let container = try #require(view.textContainer)
            for width: CGFloat in [240, 480] {
                view.setFrameSize(NSSize(width: width, height: 1_200))
                manager.ensureLayout(for: container)
                var appearances: [Data] = []
                for name: NSAppearance.Name in [.aqua, .darkAqua, .aqua] {
                    view.appearance = NSAppearance(named: name)
                    let actual = try bitmap(view) {
                        manager.drawDecorations(in: view.bounds, at: view.textContainerOrigin)
                    }
                    let expected = try bitmap(view) { drawOriginalDecorations(view) }
                    #expect(actual == expected)
                    appearances.append(actual)
                }
                #expect(appearances[0] != appearances[1])
                #expect(appearances[0] == appearances[2])
            }
        }

        private func originalDocument() throws -> NSAttributedString {
            let lines = [
                "Request\n", " GET   /api/items/🚀\n", " \n", "Query parameters\n", "page  =  2\n", "q  =  café\n",
                " \n", "Body contains\n", "needle\n", "Response\n", " Replace \n",
                "Returns the configured response instead of calling the server.\n", " \n", " \n", "X-Trace\tsample\n",
                " \n", " \n", "Payload\n", "Inherited\n", "\"café 🚀\"", "\n", "Related\n", "Open item 🚀\n", "End\n",
            ]
            let result = NSMutableAttributedString()
            var ranges: [NSRange] = []
            for (index, line) in lines.enumerated() {
                let paragraph = NSMutableParagraphStyle()
                paragraph.firstLineHeadIndent = 10
                paragraph.headIndent = 10
                paragraph.tailIndent = -10
                paragraph.paragraphSpacing = 4
                paragraph.lineBreakMode = .byWordWrapping
                if [0, 9, 21].contains(index) {
                    paragraph.paragraphSpacingBefore = index == 0 ? 0 : 30
                    paragraph.paragraphSpacing = 20
                } else if index == 17 {
                    paragraph.paragraphSpacingBefore = 3
                    paragraph.paragraphSpacing = 12
                } else if index == 19 || index == 20 {
                    paragraph.paragraphSpacing = 0
                    paragraph.lineSpacing = index == 19 ? 2 : 0
                }
                let range = NSRange(location: result.length, length: line.utf16.count)
                ranges.append(range)
                result.append(
                    NSAttributedString(
                        string: line,
                        attributes: [
                            .font: NSFont.systemFont(ofSize: 13), .foregroundColor: NSColor.labelColor,
                            .paragraphStyle: paragraph,
                        ]))
                if ![0, 9, 21].contains(index) {
                    result.addAttribute(.detailCard, value: index < 9 ? 0 : index < 21 ? 1 : 2, range: range)
                }
            }
            for index in [0, 9, 21] {
                result.addAttribute(
                    .font, value: NSFont.systemFont(ofSize: 13, weight: .semibold), range: ranges[index])
            }
            for index in [1, 8] {
                result.addAttribute(
                    .font, value: NSFont.monospacedSystemFont(ofSize: 13, weight: .regular), range: ranges[index])
            }
            for index in [2, 6, 13, 15, 16] {
                result.addAttributes(
                    [.detailSeparator: true, .font: NSFont.systemFont(ofSize: 6)],
                    range: NSRange(location: ranges[index].location, length: 1))
            }
            for index in [3, 7, 11, 18, 23] {
                result.addAttribute(.foregroundColor, value: NSColor.secondaryLabelColor, range: ranges[index])
            }
            for index in [3, 7] {
                result.addAttribute(.font, value: NSFont.systemFont(ofSize: 11), range: ranges[index])
            }
            for index in [4, 5, 19] {
                result.addAttribute(
                    .font, value: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular), range: ranges[index])
            }
            func set(_ attributes: [NSAttributedString.Key: Any], on text: String) throws {
                let range = (result.string as NSString).range(of: text)
                try #require(range.location != NSNotFound)
                result.addAttributes(attributes, range: range)
            }
            try set(
                [.detailBadge: NSColor.labelColor, .font: NSFont.systemFont(ofSize: 11, weight: .semibold)], on: " GET "
            )
            try set([.foregroundColor: NSColor.secondaryLabelColor], on: "page")
            result.addAttribute(
                .foregroundColor, value: NSColor.secondaryLabelColor,
                range: NSRange(location: ranges[5].location, length: 1))
            for index in [4, 5] {
                result.addAttribute(
                    .foregroundColor, value: NSColor.tertiaryLabelColor,
                    range: NSRange(location: ranges[index].location + (index == 4 ? 4 : 1), length: 5))
            }
            try set(
                [
                    .detailBadge: NSColor.systemBlue, .foregroundColor: NSColor.systemBlue,
                    .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                ], on: " Replace ")
            result.addAttribute(.detailHeaderRow, value: true, range: ranges[14])
            try set(
                [
                    .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
                    .foregroundColor: NSColor.secondaryLabelColor,
                ], on: "sample")
            result.addAttribute(.font, value: NSFont.systemFont(ofSize: 12, weight: .semibold), range: ranges[17])
            result.addAttribute(.foregroundColor, value: BrowserAppearance.jsonString, range: ranges[19])
            result.addAttribute(.font, value: NSFont.systemFont(ofSize: 4), range: ranges[20])
            result.removeAttribute(.foregroundColor, range: ranges[20])
            try set([.link: "lyrebird-rule:0"], on: "Open item 🚀")
            return result
        }

        private func textView(_ text: NSAttributedString) -> DetailTextView {
            let controller = RuleDetailController()
            controller.loadViewIfNeeded()
            let view = controller.textView
            for child in view.subviews { child.removeFromSuperview() }
            view.didLayout = nil
            view.removeFromSuperview()
            view.textStorage!.setAttributedString(text)
            view.setFrameSize(NSSize(width: 240, height: 1_200))
            return view
        }

        private func bitmap(_ view: NSView, draw: () -> Void) throws -> Data {
            let bitmap = try #require(
                NSBitmapImageRep(
                    bitmapDataPlanes: nil, pixelsWide: Int(view.bounds.width), pixelsHigh: Int(view.bounds.height),
                    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                    bytesPerRow: 0, bitsPerPixel: 0))
            let context = try #require(NSGraphicsContext(bitmapImageRep: bitmap))
            NSGraphicsContext.saveGraphicsState()
            defer { NSGraphicsContext.restoreGraphicsState() }
            NSGraphicsContext.current = context
            view.effectiveAppearance.performAsCurrentDrawingAppearance {
                BrowserAppearance.pane.setFill()
                view.bounds.fill()
                draw()
            }
            return try #require(bitmap.representation(using: .png, properties: [:]))
        }

        private func drawOriginalDecorations(_ view: DetailTextView) {
            let storage = view.textStorage!
            let manager = view.layoutManager!
            let container = view.textContainer!
            let origin = view.textContainerOrigin
            let all = NSRange(location: 0, length: storage.length)
            func rect(_ range: NSRange) -> NSRect {
                manager.boundingRect(
                    forGlyphRange: manager.glyphRange(forCharacterRange: range, actualCharacterRange: nil),
                    in: container
                ).offsetBy(dx: origin.x, dy: origin.y)
            }
            storage.enumerateAttribute(.detailCard, in: all) { value, range, _ in
                guard value != nil else { return }
                let content = rect(range)
                BrowserAppearance.card.setFill()
                NSBezierPath(
                    roundedRect: NSRect(
                        x: origin.x, y: content.minY - 9, width: container.size.width, height: content.height + 18),
                    xRadius: 10, yRadius: 10
                ).fill()
            }
            storage.enumerateAttributes(in: all) { attributes, range, _ in
                let bounds = rect(range)
                if let color = attributes[.detailBadge] as? NSColor {
                    color.withAlphaComponent(0.14).setFill()
                    NSBezierPath(roundedRect: bounds.insetBy(dx: -2, dy: -1), xRadius: 4, yRadius: 4).fill()
                }
                if attributes[.detailSeparator] != nil {
                    NSColor.separatorColor.setFill()
                    NSRect(x: origin.x + 10, y: bounds.minY + 4, width: max(0, container.size.width - 20), height: 0.5)
                        .fill()
                }
            }
        }
    }
}
