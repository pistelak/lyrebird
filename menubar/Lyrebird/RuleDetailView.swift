import AppKit
import SwiftUI

struct RuleDetailView: View {
    let model: AppModel
    let ruleSelection: RuleFormatting.ListSelection?
    @Binding var pickedStep: RuleFormatting.StepPick?
    let openRule: (RuleFormatting.Destination) -> Void

    var body: some View {
        ruleDetail.navigationSplitViewColumnWidth(min: 320, ideal: 420)
    }

    private var snapshot: RulesSnapshot? {
        if case .ok(let snapshot) = model.rulesRead { return snapshot }
        return nil
    }

    @ViewBuilder
    private var ruleDetail: some View {
        if let rule = RuleFormatting.detailRule(selection: ruleSelection, in: snapshot) {
            ScrollViewReader { scroller in
                Form {
                    requestSection(rule.match)
                    if let transition = selectedFlow?.transition { transitionSection(transition) }
                    if let sequence = rule.rewrite.sequence {
                        if selectedFlow?.step == nil { stepSelector(rule, sequence, scroller: scroller) }
                        SequenceResponseSection(
                            sequence: sequence, shown: shownStep(rule),
                            kind: RuleFormatting.responseKind(rule.rewrite),
                            delay: RuleFormatting.delayPhrase(rule.rewrite))
                    } else {
                        RuleResponseSection(rule: rule)
                    }
                    if let ending = selectedFlow?.endingTransition {
                        transitionSection(ending, isEnding: true)
                    }
                    notesSection(rule.notes)
                }
                .formStyle(.grouped)
            }
        } else if let transition = selectedFlow?.transition {
            Form { transitionSection(transition) }.formStyle(.grouped)
        } else if let vacancy = RuleFormatting.rulesVacancy(
            status: model.status, read: model.rulesRead, controlPort: model.ownHealth?.proxyPort)
        {
            VacancyView(vacancy: vacancy)
        } else {
            VacancyView(
                vacancy: .init(
                    message: "No rule selected.",
                    hint: "Pick one on the left to see what it answers with."))
        }
    }

    private var selectedFlow: RuleFormatting.FlowListRow? {
        RuleFormatting.flowRow(ruleSelection, in: snapshot)
    }

    private func transitionSection(_ transition: ScenarioOutline.Transition, isEnding: Bool = false) -> some View {
        let title: LocalizedStringKey =
            isEnding
            ? "Advances past the final response when this request matches"
            : "Advances the sequence when this request matches"
        return Section(title) {
            RequestLineView(line: transition.request)
            ConditionLinesView(conditions: transition.conditions)
            if transition.conditions.isEmpty {
                Text("Any query or body can advance this sequence.").font(.callout).foregroundStyle(.secondary)
            }
            if isEnding || selectedFlow?.ruleId == nil {
                if transition.relatedResponses.isEmpty {
                    Text("No response rule was identified. Advancement does not depend on a successful response.")
                }
                ForEach(transition.relatedResponses) { rule in
                    Button {
                        openRule(RuleFormatting.destination(rule: rule.id, drawnFrom: snapshot?.scenario ?? ""))
                    } label: {
                        HStack {
                            Text(rule.behaviour.isEmpty ? "Open response rule" : rule.behaviour)
                            if rule.inactive {
                                Text("Inactive").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    ConditionLinesView(conditions: rule.conditions)
                }
            }
        }
    }

    private func requestSection(_ match: RuleMatch?) -> some View {
        Section("Request") {
            RequestMatchView(match: match)
        }
    }

    @ViewBuilder
    private func notesSection(_ notes: String?) -> some View {
        if let notes, !notes.isEmpty {
            Section("Notes") {
                Text(notes).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
    }

    @ViewBuilder
    private func stepSelector(
        _ rule: RuleRow, _ sequence: RewriteSequence, scroller: ScrollViewProxy
    ) -> some View {
        if let shown = shownStep(rule) {
            Section {
                let picked = Binding(
                    get: { shown },
                    set: {
                        pickStep($0, of: rule) { anchor in withAnimation { scroller.scrollTo(anchor, anchor: .top) } }
                    })
                if sequence.steps.count <= RuleFormatting.segmentedStepLimit {
                    Picker(
                        RuleFormatting.stepSelectorLabel(step: shown, of: sequence.steps.count),
                        selection: picked
                    ) {
                        ForEach(Array(sequence.steps.indices), id: \.self) { index in
                            Text("\(index + 1)").tag(index + 1)
                        }
                    }
                    .pickerStyle(.segmented)
                } else {
                    Picker(
                        RuleFormatting.stepSelectorLabel(step: shown, of: sequence.steps.count),
                        selection: picked
                    ) {
                        ForEach(Array(sequence.steps.indices), id: \.self) { index in
                            Text(
                                RuleFormatting.stepMenuLabel(
                                    number: index + 1, step: sequence.steps[index])
                            )
                            .tag(index + 1)
                        }
                    }
                }
            }
        }
    }

    private func shownStep(_ rule: RuleRow) -> Int? {
        selectedFlow?.step ?? RuleFormatting.shownStep(rule, in: snapshot?.scenario, pick: pickedStep)
    }

    static let responseSectionId = "step-response"

    // Keep the changed response visible; see `RuleDetailViewTests`.
    func pickStep(_ number: Int, of rule: RuleRow, scrollTo: (String) -> Void) {
        pickedStep = RuleFormatting.StepPick(
            scenario: snapshot?.scenario ?? "", rule: rule.id, step: number)
        scrollTo(Self.responseSectionId)
    }
}

struct JSONBlock: View {
    let title: String
    let value: JSONValue
    let note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.snug) {
            HStack(alignment: .top, spacing: RuleFormatting.Space.snug) {
                VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                    Text(title).font(.callout.weight(.medium))
                    if let note {
                        Text(note).font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: RuleFormatting.Space.tight)
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(RuleFormatting.jsonText(value), forType: .string)
                }
                .controlSize(.small)
            }
            ScrollView(.horizontal) {
                Text(RuleFormatting.attributedJSON(value))
                    .font(.callout.monospaced())
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ResponseKindHeader: View {
    let kind: RuleFormatting.ResponseKind

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            ResponseKindBadge(kind: kind)
            Text(kind.explanation).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct ResponseBehaviourView: View {
    let behaviour: String
    let clauses: String
    let meta: String?

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
            if !behaviour.isEmpty {
                Text(behaviour).font(.body).fixedSize(horizontal: false, vertical: true)
            }
            if !clauses.isEmpty {
                Text(clauses)
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let meta { Text(meta).font(.callout).foregroundStyle(.secondary) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct RuleResponseSection: View {
    let rule: RuleRow

    var body: some View {
        Section("Response") {
            VStack(alignment: .leading, spacing: RuleFormatting.Space.step) {
                ResponseKindHeader(kind: RuleFormatting.responseKind(rule.rewrite))
                ResponseBehaviourView(
                    behaviour: RuleFormatting.behaviourLine(rule.rewrite),
                    clauses: RuleFormatting.clauseLine(rule.rewrite), meta: RuleFormatting.metaLine(rule.rewrite))
            }
            ForEach(RuleFormatting.headerRows(rule.headers)) { header in
                LabeledContent(header.label) {
                    Text(header.value).monospaced().textSelection(.enabled)
                }
            }
            if rule.rewrite.mode == "patch" {
                if let patch = rule.patch {
                    JSONBlock(title: "Patch", value: patch, note: "merged into the real response")
                }
            } else if let body = rule.body {
                JSONBlock(title: "Body", value: body, note: nil)
            }
        }
    }
}

private struct SequenceResponseSection: View {
    let sequence: RewriteSequence
    let shown: Int?
    let kind: RuleFormatting.ResponseKind
    let delay: String

    var body: some View {
        if let shown, sequence.steps.indices.contains(shown - 1) {
            let step = sequence.steps[shown - 1]
            Section("Response") {
                VStack(alignment: .leading, spacing: RuleFormatting.Space.step) {
                    ResponseKindHeader(kind: kind)
                    ResponseBehaviourView(
                        behaviour: RuleFormatting.stepBehaviourLine(step) + delay, clauses: "",
                        meta: RuleFormatting.metaLine(kind: step.bodyKind, bytes: step.bodyBytes))
                }
                if let caption = RuleFormatting.inheritedCaption(step, field: "status") {
                    Text(caption).font(.callout).foregroundStyle(.secondary)
                }
                ForEach(RuleFormatting.headerRows(step.headers)) { header in
                    LabeledContent(header.label) {
                        Text(header.value).monospaced().textSelection(.enabled)
                    }
                }
                if let caption = RuleFormatting.inheritedCaption(step, field: "headers") {
                    Text(caption).font(.callout).foregroundStyle(.secondary)
                }
                switch RuleFormatting.stepBody(step) {
                case .none:
                    Text("No body").font(.callout).foregroundStyle(.secondary)
                case .omitted(let line):
                    Text(line).font(.callout).foregroundStyle(.secondary)
                case .json(let value):
                    JSONBlock(
                        title: "Body", value: value,
                        note: RuleFormatting.inheritedCaption(step, field: "body"))
                }
            }
            .id(RuleDetailView.responseSectionId)
        } else {
            Section("Response") {
                Text("This sequence has no steps.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .id(RuleDetailView.responseSectionId)
        }
    }
}
