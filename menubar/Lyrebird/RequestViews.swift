import SwiftUI

struct RequestLineView: View {
    let line: RequestLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
            Text(line.method)
                .font(.callout.weight(.medium))
                .padding(.horizontal, RuleFormatting.Space.tight)
                .padding(.vertical, 1)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 4))
            Text(line.path)
                .font(.body.monospaced())
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}

/// `Query   a = 1 · b = 2` — one labelled line per kind of condition, wrapping beneath the request.
struct ConditionLinesView: View {
    let conditions: [RuleFormatting.Fact]

    /// Wide enough for "Body contains", which is the longest label there is.
    private static let labelColumn: CGFloat = 86

    var body: some View {
        ForEach(conditions) { condition in
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
                    label(condition).frame(width: Self.labelColumn, alignment: .leading)
                    value(condition)
                    Spacer(minLength: 0)
                }
                VStack(alignment: .leading, spacing: 0) {
                    label(condition)
                    value(condition)
                }
            }
        }
    }

    private func label(_ condition: RuleFormatting.Fact) -> some View {
        Text(condition.label).font(.callout).foregroundStyle(.secondary)
    }

    private func value(_ condition: RuleFormatting.Fact) -> some View {
        Text(condition.value)
            .font(.callout.monospaced())
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The endpoint and its matching conditions, with query keys kept distinct from their values.
struct RequestMatchView: View {
    let match: RuleMatch?

    var body: some View {
        VStack(alignment: .leading, spacing: RuleFormatting.Space.step) {
            HStack(alignment: .firstTextBaseline, spacing: RuleFormatting.Space.snug) {
                Text(RuleFormatting.method(of: match))
                    .font(.callout.weight(.semibold))
                    .padding(.horizontal, 7).padding(.vertical, 4)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 5))
                    .fixedSize()
                Text(RuleFormatting.path(of: match))
                    .font(.body.monospaced().weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let query = match?.query, !query.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: RuleFormatting.Space.snug) {
                    Text("Query parameters").font(.caption).foregroundStyle(.secondary)
                    ForEach(query.keys.sorted(), id: \.self) { key in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(key).font(.callout.monospaced())
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Text("=").font(.caption).foregroundStyle(.tertiary)
                            Text(RuleFormatting.scalarText(query[key]))
                                .font(.callout.monospaced())
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            if let contains = match?.bodyContains, !contains.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: RuleFormatting.Space.tight) {
                    Text("Body contains").font(.caption).foregroundStyle(.secondary)
                    Text(contains).font(.callout.monospaced())
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, RuleFormatting.Space.tight)
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }
}

struct ResponseKindBadge: View {
    let kind: RuleFormatting.ResponseKind
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.colorSchemeContrast) private var contrast

    private var color: Color {
        switch kind {
        case .replace: return .blue
        case .patch: return .orange
        case .sequence:
            if colorScheme == .dark {
                return contrast == .increased
                    ? Color(red: 0.62, green: 0.80, blue: 0.78)
                    : Color(red: 0.38, green: 0.62, blue: 0.60)
            }
            return contrast == .increased
                ? Color(red: 0.15, green: 0.34, blue: 0.32)
                : Color(red: 0.22, green: 0.43, blue: 0.41)
        case .unknown: return .secondary
        }
    }

    var body: some View {
        Text(kind.title)
            .font(.caption.weight(.medium))
            .foregroundStyle(color)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 4))
            .fixedSize()
            .help(kind.explanation)
            .accessibilityLabel("Response mode: \(kind.title)")
    }
}
