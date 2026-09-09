import Testing

@testable import Lyrebird

/// What a step's response section shows of the step it is drawn from. `SequenceDiagramTests` covers
/// the drawing itself.
struct StepResponseTests {

    private let steps = [
        StepSummary(status: 503, bodyKind: "json", bodyBytes: 251, inherited: ["headers"]),
        StepSummary(status: 200, bodyKind: "json", bodyBytes: 1229, inherited: []),
        StepSummary(status: 200, bodyKind: "none", inherited: ["body", "headers"]),
    ]

    // MARK: - What goes where a step's body is

    @Test
    func aStepWhoseBodyWasOmittedShowsItsSizeAndNoBlock() {
        // "Answers with no body" and "the body was too large to repeat in the snapshot" are two
        // facts about a response, and an empty block would show the second as the first.
        let omitted = StepSummary(body: nil, bodyOmitted: true, bodyKind: "json", bodyBytes: 1_258_291)

        #expect(RuleFormatting.stepBody(omitted) == .omitted("body of 1.2 MB not included in the snapshot"))
        #expect(RuleFormatting.stepBody(StepSummary(status: 204, bodyKind: "none")) == .none)
        #expect(
            RuleFormatting.stepBody(StepSummary(body: .object(["items": .array([])]), bodyKind: "json"))
                == .json(.object(["items": .array([])])))
    }

    @Test
    func aBodyLeftOutOfTheSnapshotIsNamedWithItsSizeOrWithoutOneIfThereIsNone() {
        #expect(RuleFormatting.omittedBodyLine(bytes: 262_144) == "body of 256.0 KB not included in the snapshot")
        #expect(RuleFormatting.omittedBodyLine(bytes: nil) == "body of unknown size not included in the snapshot")
    }

    // MARK: - What a step wrote and what it inherited

    @Test
    func aBlockIsLabelledInheritedOnlyWhereTheStepDidNotWriteIt() {
        // A pane showing an inherited body as the step's own invites an edit to the step that
        // changes nothing, because the value is not written there.
        let step = steps[0]

        #expect(RuleFormatting.inheritedCaption(step, field: "headers") == "inherited from the rule")
        #expect(RuleFormatting.inheritedCaption(step, field: "status") == nil)
        #expect(
            RuleFormatting.inheritedCaption(StepSummary(status: 200, bodyKind: "none"), field: "body") == nil,
            "no `inherited` at all is a snapshot that says nothing about inheritance, not one that denies it")
    }

    @Test func aStepReadsAsWhatItReturnsAndWhatThatIs() {
        // The same two lines every other rule is described with, so a step and a rule cannot come to
        // read differently.
        #expect(RuleFormatting.metaLine(kind: steps[0].bodyKind, bytes: steps[0].bodyBytes) == "JSON · 251 B")
        #expect(
            RuleFormatting.metaLine(kind: steps[2].bodyKind, bytes: steps[2].bodyBytes) == "No body",
            "spelled out: a blank there reads as a value that failed to render")
    }
}
