// Fail-closed EventKit primitives for core.reminder_upsert.
//
// Reads require an existing Reminders Full Access grant. Mutations additionally
// require REMINDER_UPSERT_LIVE=1. This helper never requests TCC access and may
// write only to the unshared "Claude" canary list.

import EventKit
import Foundation

private let allowedList = "Claude"
private let markerPrefix = "[reminder-upsert:"
private let utc = TimeZone(secondsFromGMT: 0)!

private struct ReminderValue: Codable, Equatable {
    let list: String
    let title: String
    let notes: String
    let dueAt: String?
    let priority: Int
    let completed: Bool

    enum CodingKeys: String, CodingKey {
        case list, title, notes, priority, completed
        case dueAt = "due_at"
    }
}

private struct ReminderRecord: Codable {
    let identifier: String
    let list: String
    let title: String
    let notes: String
    let dueAt: String?
    let priority: Int
    let completed: Bool
    let lastModifiedAt: String?

    enum CodingKeys: String, CodingKey {
        case identifier, list, title, notes, priority, completed
        case dueAt = "due_at"
        case lastModifiedAt = "last_modified_at"
    }

    var value: ReminderValue {
        ReminderValue(
            list: list,
            title: title,
            notes: notes,
            dueAt: dueAt,
            priority: priority,
            completed: completed
        )
    }
}

private struct FindInput: Decodable { let marker: String }
private struct GetInput: Decodable { let identifier: String }
private struct CreateInput: Decodable { let value: ReminderValue; let marker: String }
private struct UpdateInput: Decodable {
    let identifier: String
    let value: ReminderValue
    let expected: ReminderRecord
    let marker: String
}
private struct DeleteInput: Decodable {
    let identifier: String
    let expected: ReminderRecord
    let marker: String
}

private struct RecordsResponse: Encodable { let ok = true; let records: [ReminderRecord] }
private struct RecordResponse: Encodable { let ok = true; let record: ReminderRecord? }
private struct DeleteResponse: Encodable { let ok = true; let deleted: Bool }
private struct ErrorBody: Encodable { let code: String; let message: String }
private struct ErrorResponse: Encodable { let ok = false; let error: ErrorBody }

private enum HelperError: Error {
    case rejected(String, String)

    var code: String {
        switch self {
        case .rejected(let code, _): return code
        }
    }

    var message: String {
        switch self {
        case .rejected(_, let message): return message
        }
    }
}

private func emit<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    guard let data = try? encoder.encode(value) else {
        FileHandle.standardError.write(Data("response encoding failed\n".utf8))
        Foundation.exit(70)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

private func fail(_ error: HelperError, exitCode: Int32 = 2) -> Never {
    emit(ErrorResponse(error: ErrorBody(code: error.code, message: error.message)))
    Foundation.exit(exitCode)
}

private func decode<T: Decodable>(_ type: T.Type) throws -> T {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    do {
        return try JSONDecoder().decode(type, from: data)
    } catch {
        throw HelperError.rejected("invalid_request", "invalid JSON input: \(error)")
    }
}

private func requireExistingAccess() throws {
    let status = EKEventStore.authorizationStatus(for: .reminder)
    // 3 is the pre-macOS 14 authorized state; 4 is Full Access on macOS 14+.
    // Checking raw values avoids referencing the deprecated `.authorized` case.
    let granted = status.rawValue == 3 || status.rawValue == 4
    guard granted else {
        throw HelperError.rejected(
            "tcc_not_granted",
            "Reminders Full Access is not already granted to this TCC carrier (status=\(status.rawValue)); refusing without prompting"
        )
    }
}

private func requireMutationGate() throws {
    guard ProcessInfo.processInfo.environment["REMINDER_UPSERT_LIVE"] == "1" else {
        throw HelperError.rejected(
            "live_disabled",
            "mutation requires REMINDER_UPSERT_LIVE=1 from the guarded Python adapter"
        )
    }
}

private func validateMarker(_ marker: String) throws {
    guard marker.hasPrefix(markerPrefix), marker.hasSuffix("]") else {
        throw HelperError.rejected("invalid_marker", "invalid reminder-upsert marker")
    }
    let keyStart = marker.index(marker.startIndex, offsetBy: markerPrefix.count)
    let keyEnd = marker.index(before: marker.endIndex)
    let key = String(marker[keyStart..<keyEnd])
    guard key.range(
        of: #"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"#,
        options: .regularExpression
    ) != nil else {
        throw HelperError.rejected("invalid_marker", "invalid reminder-upsert marker")
    }
}

private func markerIsLine(_ marker: String, in notes: String) -> Bool {
    notes.components(separatedBy: .newlines).contains(marker)
}

private func canaryCalendar(_ store: EKEventStore) throws -> EKCalendar {
    let matches = store.calendars(for: .reminder).filter { $0.title == allowedList }
    guard matches.count == 1, let calendar = matches.first else {
        throw HelperError.rejected(
            "list_ambiguous",
            "expected exactly one \(allowedList.debugDescription) Reminders list; found \(matches.count)"
        )
    }
    return calendar
}

private func fetchAll(_ store: EKEventStore, calendar: EKCalendar) throws -> [EKReminder] {
    let semaphore = DispatchSemaphore(value: 0)
    var result: [EKReminder] = []
    let predicate = store.predicateForReminders(in: [calendar])
    store.fetchReminders(matching: predicate) { reminders in
        result = reminders ?? []
        semaphore.signal()
    }
    guard semaphore.wait(timeout: .now() + 20) == .success else {
        throw HelperError.rejected("fetch_timeout", "EventKit reminder fetch timed out")
    }
    return result
}

private let isoFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    formatter.timeZone = utc
    return formatter
}()

private let timestampFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    formatter.timeZone = utc
    return formatter
}()

private func canonicalDue(_ components: DateComponents?) -> String? {
    guard let components else { return nil }
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = components.timeZone ?? .current
    guard let date = calendar.date(from: components) else { return nil }
    return isoFormatter.string(from: date)
}

private func dueComponents(_ value: String?) throws -> DateComponents? {
    guard let value else { return nil }
    guard let date = isoFormatter.date(from: value) else {
        throw HelperError.rejected("invalid_due", "due_at must be canonical ISO 8601")
    }
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = utc
    var components = calendar.dateComponents(
        [.year, .month, .day, .hour, .minute, .second],
        from: date
    )
    components.calendar = calendar
    components.timeZone = utc
    return components
}

private func record(_ reminder: EKReminder) throws -> ReminderRecord {
    let identifier = reminder.calendarItemIdentifier
    guard !identifier.isEmpty else {
        throw HelperError.rejected("missing_identifier", "EventKit reminder has no identifier")
    }
    return ReminderRecord(
        identifier: identifier,
        list: reminder.calendar.title,
        title: reminder.title ?? "",
        notes: reminder.notes ?? "",
        dueAt: canonicalDue(reminder.dueDateComponents),
        priority: reminder.priority,
        completed: reminder.isCompleted,
        lastModifiedAt: reminder.lastModifiedDate.map { timestampFormatter.string(from: $0) }
    )
}

private func reminder(
    _ store: EKEventStore,
    identifier: String
) -> EKReminder? {
    store.calendarItem(withIdentifier: identifier) as? EKReminder
}

private func validateValue(_ value: ReminderValue, marker: String) throws {
    guard value.list == allowedList else {
        throw HelperError.rejected("list_forbidden", "canary may write only to the Claude list")
    }
    guard !value.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        throw HelperError.rejected("invalid_title", "title is required")
    }
    guard [0, 1, 5, 9].contains(value.priority) else {
        throw HelperError.rejected("invalid_priority", "EventKit priority must be 0, 1, 5, or 9")
    }
    guard !value.completed else {
        throw HelperError.rejected("completion_forbidden", "canary does not create or restore completed reminders")
    }
    guard markerIsLine(marker, in: value.notes) else {
        throw HelperError.rejected("marker_missing", "stored notes must contain the exact idempotency marker line")
    }
    _ = try dueComponents(value.dueAt)
}

private func apply(_ value: ReminderValue, to reminder: EKReminder, calendar: EKCalendar) throws {
    reminder.calendar = calendar
    reminder.title = value.title
    reminder.notes = value.notes
    reminder.dueDateComponents = try dueComponents(value.dueAt)
    reminder.priority = value.priority
    reminder.isCompleted = false
}

private func verified(
    _ store: EKEventStore,
    identifier: String,
    expected: ReminderValue
) throws -> ReminderRecord {
    guard let saved = reminder(store, identifier: identifier) else {
        throw HelperError.rejected("readback_missing", "saved reminder is not readable by identifier")
    }
    let actual = try record(saved)
    guard actual.value == expected else {
        throw HelperError.rejected(
            "readback_mismatch",
            "saved reminder does not exactly match title/list/notes/due/priority/completion"
        )
    }
    return actual
}

private func runFind(_ store: EKEventStore) throws {
    let input = try decode(FindInput.self)
    try validateMarker(input.marker)
    let calendar = try canaryCalendar(store)
    let matches = try fetchAll(store, calendar: calendar).filter {
        markerIsLine(input.marker, in: $0.notes ?? "")
    }
    emit(RecordsResponse(records: try matches.map(record)))
}

private func runGet(_ store: EKEventStore) throws {
    let input = try decode(GetInput.self)
    guard let item = reminder(store, identifier: input.identifier), item.calendar.title == allowedList else {
        emit(RecordResponse(record: nil))
        return
    }
    emit(RecordResponse(record: try record(item)))
}

private func runCreate(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(CreateInput.self)
    try validateMarker(input.marker)
    try validateValue(input.value, marker: input.marker)
    let calendar = try canaryCalendar(store)
    let duplicates = try fetchAll(store, calendar: calendar).filter {
        markerIsLine(input.marker, in: $0.notes ?? "")
    }
    guard duplicates.isEmpty else {
        throw HelperError.rejected("duplicate_marker", "idempotency marker already exists")
    }
    let item = EKReminder(eventStore: store)
    try apply(input.value, to: item, calendar: calendar)
    try store.save(item, commit: true)
    let identifier = item.calendarItemIdentifier
    guard !identifier.isEmpty else {
        throw HelperError.rejected("missing_identifier", "saved reminder has no identifier")
    }
    emit(RecordResponse(record: try verified(store, identifier: identifier, expected: input.value)))
}

private func runUpdate(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(UpdateInput.self)
    try validateMarker(input.marker)
    try validateValue(input.value, marker: input.marker)
    guard let item = reminder(store, identifier: input.identifier), item.calendar.title == allowedList else {
        throw HelperError.rejected("not_found", "canary reminder was not found")
    }
    let current = try record(item)
    guard markerIsLine(input.marker, in: current.notes) else {
        throw HelperError.rejected("marker_missing", "target reminder lacks the exact idempotency marker")
    }
    guard current.value == input.expected.value,
          current.lastModifiedAt == input.expected.lastModifiedAt else {
        throw HelperError.rejected("compare_and_swap_failed", "target reminder changed before update")
    }
    let calendar = try canaryCalendar(store)
    try apply(input.value, to: item, calendar: calendar)
    try store.save(item, commit: true)
    emit(RecordResponse(record: try verified(store, identifier: input.identifier, expected: input.value)))
}

private func runDelete(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(DeleteInput.self)
    try validateMarker(input.marker)
    guard let item = reminder(store, identifier: input.identifier), item.calendar.title == allowedList else {
        emit(DeleteResponse(deleted: false))
        return
    }
    let current = try record(item)
    guard markerIsLine(input.marker, in: current.notes) else {
        throw HelperError.rejected("marker_missing", "target reminder lacks the exact idempotency marker")
    }
    guard current.value == input.expected.value,
          current.lastModifiedAt == input.expected.lastModifiedAt else {
        throw HelperError.rejected("compare_and_swap_failed", "target reminder changed before delete")
    }
    try store.remove(item, commit: true)
    guard reminder(store, identifier: input.identifier) == nil else {
        throw HelperError.rejected("delete_readback_failed", "deleted reminder remains readable")
    }
    emit(DeleteResponse(deleted: true))
}

do {
    let arguments = CommandLine.arguments
    guard arguments.count == 2 else {
        throw HelperError.rejected("usage", "usage: swift reminder_eventkit.swift find|get|create|update|delete")
    }
    try requireExistingAccess()
    let store = EKEventStore()
    switch arguments[1] {
    case "find": try runFind(store)
    case "get": try runGet(store)
    case "create": try runCreate(store)
    case "update": try runUpdate(store)
    case "delete": try runDelete(store)
    default: throw HelperError.rejected("usage", "unknown operation \(arguments[1])")
    }
} catch let error as HelperError {
    fail(error)
} catch {
    fail(.rejected("eventkit_error", String(describing: error)))
}
