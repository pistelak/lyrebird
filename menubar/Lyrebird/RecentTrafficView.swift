import Foundation

struct RecentTrafficItem: Identifiable {
    enum ID: Hashable {
        case event(RecentEntry.Key)
        case legacy(Int)
    }

    let id: ID
    let entry: RecentEntry

    static func items(_ entries: [RecentEntry]) -> [Self] {
        entries.enumerated().map { index, entry in
            // Older engines have no event IDs; these rows cannot retain a selection across reads.
            Self(id: entry.selectionKey.map(ID.event) ?? .legacy(index), entry: entry)
        }
    }
}
