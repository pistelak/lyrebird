import AppKit
import Testing

@testable import Lyrebird

extension AppTests {
    @MainActor
    struct BrowserDesignTests {
        @Test func menuBarBirdRemainsATemplateInEveryStatus() throws {
            let states: [AppModel.Status] = [
                .intercepting, .pacDisabled, .down, .foreignProfile(running: "example"),
                .unreadable("Unavailable"), .profileUnknown("Choose a profile"),
            ]
            for status in states {
                let image = try #require(StatusItemController.templateImage(for: status, description: status.word))
                #expect(image.isTemplate)
            }
        }

        @Test func patchDetailExplainsMergingOnceAndKeepsBehaviorOptions() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                var snapshot = BrowserPreview.snapshot("orders-pending")
                let index = try #require(snapshot.rules.firstIndex { $0.id == "ovr_patch" })
                snapshot.rules[index].rewrite.status = 202
                snapshot.rules[index].rewrite.patchKeys = 1
                snapshot.rules[index].rewrite.patchStrategy = "appendToArray"
                snapshot.rules[index].rewrite.delayMs = 250
                model.rulesRead = .ok(snapshot)
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.ruleSelection = .rule("ovr_patch")
                let detail = RuleDetailController()
                detail.update(model: model, state: state)
                let text = detail.textView.string
                #expect(text.contains("Merges changes into the server’s JSON response."))
                #expect(!text.contains("Patches the real response"))
                #expect(!text.contains("merged into the real response"))
                #expect(!text.contains("JSON responses only"))
                #expect(text.contains("Changes · 1 key"))
                #expect(text.contains("sets status to 202 · appends to arrays"))
                #expect(text.contains("Delay: 250 ms"))
                #expect(text.contains("enabled"))
            }
        }

        @Test func selectedFlowUsesSystemSelectionTextAndRestoresItsColors() throws {
            let row = try #require(
                RuleFormatting.flowSections(BrowserPreview.snapshot("remove-an-item")).first?.rows.first)
            let cell = FlowRequestCell(row)
            func fields(_ view: NSView) -> [NSTextField] {
                (view as? NSTextField).map { [$0] } ?? view.subviews.flatMap(fields)
            }
            let labels = fields(cell)
            let original = labels.map(\.textColor)
            #expect(labels.count >= 5)
            cell.backgroundStyle = .emphasized
            #expect(labels.allSatisfy { $0.textColor == .alternateSelectedControlTextColor })
            cell.backgroundStyle = .normal
            #expect(labels.map(\.textColor) == original)
        }

        @Test func syntaxTextMeetsContrastInBothAppearances() throws {
            func luminance(_ color: NSColor) -> CGFloat {
                let rgb = color.usingColorSpace(.sRGB)!
                func linear(_ value: CGFloat) -> CGFloat {
                    value <= 0.04045 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
                }
                return 0.2126 * linear(rgb.redComponent) + 0.7152 * linear(rgb.greenComponent) + 0.0722
                    * linear(rgb.blueComponent)
            }
            for name: NSAppearance.Name in [
                .aqua, .darkAqua, .accessibilityHighContrastAqua, .accessibilityHighContrastDarkAqua,
            ] {
                let appearance = try #require(NSAppearance(named: name))
                appearance.performAsCurrentDrawingAppearance {
                    let background = luminance(BrowserAppearance.card)
                    for color in [
                        BrowserAppearance.jsonString, BrowserAppearance.jsonNumber, BrowserAppearance.jsonLiteral,
                    ] {
                        let foreground = luminance(color)
                        let contrast = (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
                        #expect(contrast >= 4.5)
                    }
                }
            }
        }

        @Test func detailRepaintClearsOldDecorations() throws {
            let controller = RuleDetailController()
            controller.loadViewIfNeeded()
            let text = controller.textView
            text.appearance = NSAppearance(named: .aqua)
            text.setFrameSize(NSSize(width: 240, height: 160))
            let bitmap = try #require(
                NSBitmapImageRep(
                    bitmapDataPlanes: nil, pixelsWide: 240, pixelsHigh: 160,
                    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            let context = try #require(NSGraphicsContext(bitmapImageRep: bitmap))
            NSGraphicsContext.saveGraphicsState()
            defer { NSGraphicsContext.restoreGraphicsState() }
            NSGraphicsContext.current = context
            text.effectiveAppearance.performAsCurrentDrawingAppearance {
                // Simulate pixels retained from the rounded edge of the previous, longer card.
                BrowserAppearance.card.setFill()
                NSRect(x: 0, y: 0, width: 240, height: 160).fill()
                text.draw(NSRect(x: 0, y: 0, width: 240, height: 160))
            }
            let pixel = try #require(bitmap.colorAt(x: 120, y: 80)?.usingColorSpace(.deviceRGB))
            var expected: NSColor!
            text.effectiveAppearance.performAsCurrentDrawingAppearance {
                expected = BrowserAppearance.pane.usingColorSpace(.deviceRGB)
            }
            #expect(abs(pixel.redComponent - expected.redComponent) < 0.01)
            #expect(abs(pixel.greenComponent - expected.greenComponent) < 0.01)
            #expect(abs(pixel.blueComponent - expected.blueComponent) < 0.01)
        }

        @Test func transitionLinksShareTheCardTextInset() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                let snapshot = BrowserPreview.snapshot("orders-pending")
                model.rulesRead = .ok(snapshot)
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.reconcile(snapshot, activeScenario: snapshot.scenario)
                let ending = try #require(
                    RuleFormatting.flowSections(snapshot).flatMap(\.rows).first { $0.endingTransition != nil })
                state.ruleSelection = ending.selection
                let detail = RuleDetailController()
                detail.update(model: model, state: state)
                let storage = try #require(detail.textView.textStorage)
                var linkCount = 0
                storage.enumerateAttribute(.link, in: NSRange(location: 0, length: storage.length)) { value, range, _ in
                    guard value != nil else { return }
                    linkCount += 1
                    let paragraph =
                        storage.attribute(.paragraphStyle, at: range.location, effectiveRange: nil) as? NSParagraphStyle
                    #expect(paragraph?.firstLineHeadIndent == 10)
                    #expect(paragraph?.headIndent == 10)
                }
                #expect(linkCount > 0)
            }
        }

        @Test(arguments: [NSTableView.RowSizeStyle.small, .medium, .large])
        func nestedSidebarHeadersFitInsideTheirRows(size: NSTableView.RowSizeStyle) async throws {
            try await withAppTestEnvironment {
                let sidebar = ScenarioSidebarController()
                let window = NSWindow(contentViewController: sidebar)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 230, height: 600))
                window.orderFront(nil)
                defer { window.close() }
                #expect(sidebar.outline.rowSizeStyle == .default)
                sidebar.outline.rowSizeStyle = size
                let names = ["default", "orders/pending", "orders/complete", "account/settings", "account/profile"]
                sidebar.update(
                    ScenarioList(
                        active: "default", scenarios: names.map { .init(name: $0, overrideCount: 1, verified: false) }),
                    selection: .scenario("orders/pending"), problems: [:], stale: false)
                window.contentView?.layoutSubtreeIfNeeded()
                var groups = 0
                for row in 0..<sidebar.outline.numberOfRows {
                    let item = try #require(sidebar.outline.item(atRow: row) as? ScenarioSidebarController.Item)
                    guard item.destination == nil else { continue }
                    groups += 1
                    let cell = try #require(
                        sidebar.outline.view(atColumn: 0, row: row, makeIfNecessary: true) as? NSTableCellView)
                    cell.layoutSubtreeIfNeeded()
                    let label = try #require(cell.textField)
                    #expect(label.frame.height >= label.intrinsicContentSize.height)
                    #expect(cell.bounds.insetBy(dx: -1, dy: -1).contains(label.frame))
                }
                #expect(groups == 3)
            }
        }

        @Test func requestColumnReflowsWhenItsViewportShrinks() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                let snapshot = BrowserPreview.snapshot("remove-an-item")
                model.rulesRead = .ok(snapshot)
                model.scenarios = ScenarioList(
                    active: snapshot.scenario,
                    scenarios: [
                        ScenarioSummary(
                            name: snapshot.scenario, overrideCount: 2, verified: false,
                            notes: String(
                                repeating: "Read the list, delete an item, then read the list again. ", count: 12))
                    ])
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.reconcile(snapshot, activeScenario: snapshot.scenario)
                let controller = RequestListController()
                let window = NSWindow(contentViewController: controller)
                window.isReleasedWhenClosed = false
                window.orderFront(nil)
                defer { window.close() }
                controller.update(model: model, state: state)
                var wideHeight: CGFloat = 0
                for width: CGFloat in [600, 300, 480, 300] {
                    window.setContentSize(NSSize(width: width, height: 800))
                    window.contentView?.layoutSubtreeIfNeeded()
                    let table = controller.table
                    let column = try #require(table.tableColumns.first)
                    #expect(abs(column.width - controller.scrollView.contentSize.width) < 1)
                    let row = try #require(controller.rows.firstIndex { $0.id == "notes" })
                    let cell = try #require(table.view(atColumn: 0, row: row, makeIfNecessary: true) as? TextCell)
                    cell.layoutSubtreeIfNeeded()
                    #expect(cell.frame.width <= controller.scrollView.contentSize.width + 1)
                    #expect(cell.label.frame.maxX <= cell.bounds.maxX)
                    let height = table.rect(ofRow: row).height
                    if width == 600 { wideHeight = height }
                    if width == 300 { #expect(height > wideHeight + 80) }
                }
            }
        }

        @Test func detailCardsKeepBodyCopyInsideTheResponseAndPreserveScrollOnRefresh() async throws {
            try await withAppTestEnvironment {
                let model = AppModel(autoStart: false, expectedFingerprint: "design")
                model.healthRead = .up(Health(proxyUp: true, intercepting: true, profileFingerprint: "design"))
                let snapshot = BrowserPreview.snapshot("remove-an-item")
                model.rulesRead = .ok(snapshot)
                let state = BrowserState()
                state.select(.scenario(snapshot.scenario))
                state.reconcile(snapshot, activeScenario: snapshot.scenario)
                let detail = RuleDetailController()
                let window = NSWindow(contentViewController: detail)
                window.isReleasedWhenClosed = false
                window.setContentSize(NSSize(width: 390, height: 460))
                window.orderFront(nil)
                defer { window.close() }
                detail.update(model: model, state: state)
                window.contentView?.layoutSubtreeIfNeeded()
                #expect(detail.textView.layoutManager is DetailLayoutManager)
                let storage = try #require(detail.textView.textStorage)
                let body = (storage.string as NSString).range(of: "\"items\"")
                let request = (storage.string as NSString).range(of: "/api/items")
                #expect(storage.attribute(.detailCard, at: body.location, effectiveRange: nil) != nil)
                #expect(storage.attribute(.detailCard, at: request.location, effectiveRange: nil) != nil)
                let copy = try #require(detail.textView.subviews.compactMap { $0 as? NSButton }.first)
                #expect(copy.title == "Copy")
                #expect(copy.frame.minY > 100)
                #expect(copy.frame.maxX <= detail.textView.bounds.width)
                let style = try #require(
                    storage.attribute(.paragraphStyle, at: body.location, effectiveRange: nil) as? NSParagraphStyle)
                #expect(style.lineBreakMode == .byWordWrapping)
                detail.scrollView.contentView.scroll(to: NSPoint(x: 0, y: 100))
                let position = detail.scrollView.contentView.bounds.origin
                detail.update(model: model, state: state)
                #expect(detail.scrollView.contentView.bounds.origin == position)
                let selectedText = detail.textView.string
                #expect(selectedText.contains("Notebook"))
                #expect(selectedText.contains("Sequence"))
            }
        }
    }
}
