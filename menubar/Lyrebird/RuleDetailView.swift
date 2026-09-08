import AppKit
import SwiftUI

/// The right-hand pane: everything the snapshot says about the selected rule.
struct RuleDetailView: View {
    let rule: RuleRow
    /// Which step's stored JSON is printed. Nil until the pane picks the one the cursor is on.
    @State private var selectedStep: Int?

    private var storedSteps: [JSONValue] { rule.sequence?.steps ?? [] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: RuleFormatting.Space.section) {
                matches
                if let sequence = rule.rewrite.sequence {
                    self.sequence(sequence)
                } else {
                    answersWith
                }
                // The rule's own headers, which a sequence's steps inherit — so they belong to both
                // shapes, not only to the one that answers with a body of its own.
                if let headers = rule.headers, !headers.isEmpty {
                    section("HEADERS") { codeBlock { Text(RuleFormatting.headerBlock(headers)) } }
                }
                if rule.rewrite.sequence == nil, let stored = rule.patch ?? rule.body {
                    jsonSection(rule.patch == nil ? "BODY" : "PATCH", stored)
                }
                if let notes = rule.notes, !notes.isEmpty {
                    section("NOTES") {
                        Text(notes).font(.callout).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(RuleFormatting.Space.section)
        }
    }

    // MARK: - Matches

    /// The request as a person would write it, not a table of field names: `GET /api/v1/orders`,
    /// with the constraints that are not method-or-path as chips underneath.
    private var matches: some View {
        section("MATCHES") {
            VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                (Text(RuleFormatting.method(of: rule.match))
                    .font(.system(size: 13, design: .monospaced).weight(.semibold))
                    + Text(" " + RuleFormatting.path(of: rule.match))
                    .font(.system(size: 13, design: .monospaced)))
                    .textSelection(.enabled)
                let chips = RuleFormatting.matchChips(for: rule.match)
                if !chips.isEmpty { chipRow(chips) }
            }
        }
    }

    // MARK: - Answers with

    private var answersWith: some View {
        section("ANSWERS WITH") {
            VStack(alignment: .leading, spacing: RuleFormatting.Space.snug) {
                chipRow(RuleFormatting.answerChips(for: rule.rewrite))
                // The one place the run id appears: it is what binds a count to the boundary a
                // reset drew, and it answers nothing repeated down thirty rows of a list.
                Text(RuleFormatting.answerCaption(rule.answer) + " · " + RuleFormatting.runCaption(rule.answer))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    // MARK: - Sequence

    private func sequence(_ sequence: RewriteSequence) -> some View {
        section("SEQUENCE") {
            VStack(alignment: .leading, spacing: RuleFormatting.Space.snug) {
                Text(
                    sequence.advanceOn == "match"
                        ? "advances on its own matcher" : "advances when this rule answers"
                )
                .font(.callout)
                Text("exhausted → \(sequence.onExhausted ?? "error")").font(.callout)
                Text(rule.sequenceState?.runId.map { "run " + $0 } ?? "no run")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(Array(sequence.steps.enumerated()), id: \.offset) { index, step in
                    stepRow(number: index + 1, step: step)
                }
                if let step = shownStep, step >= 1, step <= storedSteps.count {
                    jsonSection("STEP " + String(step) + " AS STORED", storedSteps[step - 1])
                }
            }
        }
    }

    /// The step whose stored JSON is printed: the one clicked, else the one the cursor is on, else
    /// the first. A sequence that has run out has no next step, so the fallback matters.
    private var shownStep: Int? {
        selectedStep ?? rule.sequenceState?.nextStep ?? (storedSteps.isEmpty ? nil : 1)
    }

    private func stepRow(number: Int, step: StepSummary) -> some View {
        let isNext = rule.sequenceState?.nextStep == number
        return Button {
            selectedStep = number
        } label: {
            HStack(spacing: RuleFormatting.Space.snug) {
                Text(String(number)).font(.caption.monospaced()).frame(width: 18, alignment: .trailing)
                if let status = step.status {
                    Text(String(status))
                        .font(.system(.callout, design: .monospaced))
                        .foregroundStyle(RuleFormatting.statusColor(status))
                }
                Text(RuleFormatting.bodySummary(kind: step.bodyKind, bytes: step.bodyBytes))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let count = step.headerCount, count > 0 {
                    Text(String(count) + (count == 1 ? " header" : " headers"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let served = rule.sequenceState?.serves?[String(number)] {
                    Text("served " + String(served) + "×").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if isNext { tag("next") }
            }
            .contentShape(Rectangle())
            .padding(.vertical, 2)
            .padding(.horizontal, 4)
            .background(shownStep == number ? Color.accentColor.opacity(0.12) : Color.clear)
        }
        .buttonStyle(.plain)
    }

    // MARK: - Building blocks

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            sectionLabel(title)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A JSON block under its own label, with the Copy that hands over exactly what is on screen.
    private func jsonSection(_ title: String, _ value: JSONValue) -> some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            HStack {
                sectionLabel(title)
                Spacer()
                Button("Copy") { copy(RuleFormatting.jsonText(value)) }
                    .buttonStyle(.borderless)
                    .font(.caption)
            }
            codeBlock { Text(RuleFormatting.attributedJSON(value)) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The printed text, not the decoded value: what is copied is what the pane shows, so a body
    /// pasted back into a scenario file is the one that was being looked at.
    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private func sectionLabel(_ title: String) -> some View {
        Text(title).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
    }

    private func chipRow(_ chips: [RuleFormatting.Chip]) -> some View {
        HStack(spacing: RuleFormatting.Space.snug) {
            ForEach(Array(chips.enumerated()), id: \.offset) { _, chip in
                Text(chip.text)
                    .font(.system(.caption, design: .monospaced))
                    .fontWeight(chip.tint == nil ? .regular : .semibold)
                    .foregroundStyle(chip.tint ?? .primary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
            }
        }
    }

    private func tag(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.secondary.opacity(0.15), in: Capsule())
    }

    private func codeBlock<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .font(.system(size: 11.5, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 4))
    }
}
