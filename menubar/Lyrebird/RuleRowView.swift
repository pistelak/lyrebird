import SwiftUI

/// A rule in the list: what it matches, what it answers with, and what it has done.
///
/// Three columns rather than three stacked lines, because the question the list answers is "which
/// of these is the one" — and that is read by scanning down a column, not along a row. The method
/// and the status keep fixed widths so they line up whatever the paths do.
struct RuleRowView: View {
    let rule: RuleRow

    var body: some View {
        HStack(alignment: .top, spacing: RuleFormatting.Space.snug) {
            MethodBadge(method: RuleFormatting.method(of: rule.match))
            VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                Text(RuleFormatting.path(of: rule.match))
                    .font(.system(.body, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                // Without the status: it has a column of its own on the right, and the same number
                // twice on one row reads as two different facts about the rule.
                Text(RuleFormatting.howLine(rule.rewrite, state: rule.sequenceState, includingStatus: false))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(RuleFormatting.answerCaption(rule.answer))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: RuleFormatting.Space.snug)
            statusColumn
        }
        .padding(.vertical, RuleFormatting.Space.tight)
    }

    /// Fixed width even when there is no status, so the column stays a column: a sequenced rule
    /// answers with its steps and a patch may force none, and letting those rows close the gap would
    /// leave the codes above and below them out of line.
    private var statusColumn: some View {
        Text(rule.rewrite.status.map(String.init) ?? "")
            .font(.system(.body, design: .monospaced))
            .foregroundStyle(rule.rewrite.status.map(RuleFormatting.statusColor) ?? Color.secondary)
            .frame(width: 34, alignment: .trailing)
    }
}

/// The method, in a badge wide enough for the longest one so the paths beside it all start at the
/// same place.
struct MethodBadge: View {
    let method: String

    var body: some View {
        Text(method)
            .font(.system(.caption, design: .monospaced).weight(.semibold))
            // One neutral tint for every method but the destructive one: colouring all of them
            // would spend the reader's attention on a field they can already read.
            .foregroundStyle(RuleFormatting.methodTint(method) ?? Color.secondary)
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .frame(width: 62)
            .padding(.vertical, 3)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
    }
}
