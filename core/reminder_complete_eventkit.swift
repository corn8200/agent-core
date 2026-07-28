// Exact EventKit completion primitives for the Duffields Warboard.
//
// Reads require an existing Reminders Full Access grant. Mutations require
// REMINDER_COMPLETE_LIVE=1. The helper never requests access and never selects
// a reminder by title, list position, or another mutable display field.

import CryptoKit
import EventKit
import Foundation

private let utc = TimeZone(secondsFromGMT: 0)!
private let canaryList = "Claude"
private let canaryTitle = "Duffields reminder completion canary"
private let canaryMarkerPrefix = "[warboard-reminder-complete-canary:"

private struct RecurrenceDayValue: Codable, Equatable {
    let day: Int
    let week: Int
}

private struct RecurrenceEndValue: Codable, Equatable {
    let occurrenceCount: Int
    let endDate: String?

    enum CodingKeys: String, CodingKey {
        case occurrenceCount = "occurrence_count"
        case endDate = "end_date"
    }
}

private struct RecurrenceRuleValue: Codable, Equatable {
    let frequency: Int
    let interval: Int
    let firstDay: Int
    let daysOfWeek: [RecurrenceDayValue]
    let daysOfMonth: [Int]
    let monthsOfYear: [Int]
    let weeksOfYear: [Int]
    let daysOfYear: [Int]
    let setPositions: [Int]
    let end: RecurrenceEndValue?

    enum CodingKeys: String, CodingKey {
        case frequency, interval, end
        case firstDay = "first_day"
        case daysOfWeek = "days_of_week"
        case daysOfMonth = "days_of_month"
        case monthsOfYear = "months_of_year"
        case weeksOfYear = "weeks_of_year"
        case daysOfYear = "days_of_year"
        case setPositions = "set_positions"
    }
}

private struct ReminderRecord: Codable, Equatable {
    let identifier: String
    let externalIdentifier: String
    let listId: String
    let list: String
    let title: String
    let dueAt: String?
    let dueState: String
    let priority: Int
    let completed: Bool
    let completionDate: String?
    let lastModifiedDate: String
    let recurrenceFingerprint: String
    let recurrence: [RecurrenceRuleValue]

    enum CodingKeys: String, CodingKey {
        case identifier
        case externalIdentifier = "external_identifier"
        case listId = "list_id"
        case list, title, priority, completed
        case dueAt = "due_at"
        case dueState = "due_state"
        case completionDate = "completion_date"
        case lastModifiedDate
        case recurrenceFingerprint = "recurrence_fingerprint"
        case recurrence
    }
}

private struct GetInput: Decodable {
    let identifier: String
}

private struct SetCompletionInput: Decodable {
    let identifier: String
    let expected: ReminderRecord
    let completed: Bool
}

private struct RestoreRecurringInput: Decodable {
    let identifier: String
    let expected: ReminderRecord
    let restore: ReminderRecord
    let generatedOccurrence: ReminderRecord

    enum CodingKeys: String, CodingKey {
        case identifier, expected, restore
        case generatedOccurrence = "generated_occurrence"
    }
}

private struct CanaryInput: Decodable {
    let marker: String
    let recurring: Bool?
}

private struct RecordResponse: Encodable {
    let ok = true
    let record: ReminderRecord?
}

private struct RecordsResponse: Encodable {
    let ok = true
    let records: [ReminderRecord]
}

private struct MutationResponse: Encodable {
    let ok = true
    let before: ReminderRecord
    let record: ReminderRecord
    let generatedOccurrence: ReminderRecord?

    enum CodingKeys: String, CodingKey {
        case ok, before, record
        case generatedOccurrence = "generated_occurrence"
    }
}

private struct CanaryDeleteResponse: Encodable {
    let ok = true
    let deleted: Int
}

private struct ErrorBody: Encodable {
    let code: String
    let message: String
}

private struct ErrorResponse: Encodable {
    let ok = false
    let error: ErrorBody
}

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

private let isoFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    formatter.timeZone = utc
    return formatter
}()

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
    guard !data.isEmpty, data.count <= 131_072 else {
        throw HelperError.rejected("invalid_request", "request must contain 1-131072 bytes")
    }
    do {
        return try JSONDecoder().decode(type, from: data)
    } catch {
        throw HelperError.rejected("invalid_request", "invalid JSON input: \(error)")
    }
}

private func requireExistingAccess() throws {
    let status = EKEventStore.authorizationStatus(for: .reminder)
    let granted = status.rawValue == 3 || status.rawValue == 4
    guard granted else {
        throw HelperError.rejected(
            "tcc_not_granted",
            "Reminders Full Access is not already granted to this TCC carrier (status=\(status.rawValue))"
        )
    }
}

private func requireMutationGate() throws {
    guard ProcessInfo.processInfo.environment["REMINDER_COMPLETE_LIVE"] == "1" else {
        throw HelperError.rejected(
            "live_disabled",
            "mutation requires REMINDER_COMPLETE_LIVE=1 from the guarded adapter"
        )
    }
}

private func validateCanaryMarker(_ marker: String) throws {
    guard marker.hasPrefix(canaryMarkerPrefix), marker.hasSuffix("]"),
          marker.range(
              of: #"^\[warboard-reminder-complete-canary:[a-f0-9]{32}\]$"#,
              options: .regularExpression
          ) != nil else {
        throw HelperError.rejected("invalid_canary_marker", "canary marker is invalid")
    }
}

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
        throw HelperError.rejected("invalid_due", "stored due_at is not canonical ISO 8601")
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

private func numbers(_ values: [NSNumber]?) -> [Int] {
    (values ?? []).map { $0.intValue }.sorted()
}

private func recurrenceValues(_ reminder: EKReminder) -> [RecurrenceRuleValue] {
    (reminder.recurrenceRules ?? []).map { rule in
        let days = (rule.daysOfTheWeek ?? []).map { day in
            RecurrenceDayValue(
                day: day.dayOfTheWeek.rawValue,
                week: day.weekNumber
            )
        }.sorted {
            if $0.day != $1.day {
                return $0.day < $1.day
            }
            return $0.week < $1.week
        }
        let recurrenceEnd = rule.recurrenceEnd.map {
            RecurrenceEndValue(
                occurrenceCount: $0.occurrenceCount,
                endDate: $0.endDate.map { isoFormatter.string(from: $0) }
            )
        }
        return RecurrenceRuleValue(
            frequency: rule.frequency.rawValue,
            interval: rule.interval,
            firstDay: rule.firstDayOfTheWeek,
            daysOfWeek: days,
            daysOfMonth: numbers(rule.daysOfTheMonth),
            monthsOfYear: numbers(rule.monthsOfTheYear),
            weeksOfYear: numbers(rule.weeksOfTheYear),
            daysOfYear: numbers(rule.daysOfTheYear),
            setPositions: numbers(rule.setPositions),
            end: recurrenceEnd
        )
    }
}

private func recurrenceFingerprint(_ values: [RecurrenceRuleValue]) throws -> String {
    guard !values.isEmpty else { return "none" }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let data: Data
    do {
        data = try encoder.encode(values)
    } catch {
        throw HelperError.rejected(
            "recurrence_encoding_failed",
            "could not encode reminder recurrence"
        )
    }
    let digest = SHA256.hash(data: data)
    return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
}

private func recurrenceRules(
    _ values: [RecurrenceRuleValue]
) throws -> [EKRecurrenceRule] {
    try values.map { value in
        guard let frequency = EKRecurrenceFrequency(rawValue: value.frequency) else {
            throw HelperError.rejected(
                "unsupported_recurrence",
                "recurrence frequency is not supported"
            )
        }
        let days: [EKRecurrenceDayOfWeek]? = value.daysOfWeek.isEmpty ? nil :
            try value.daysOfWeek.map { day in
                guard let weekday = EKWeekday(rawValue: day.day) else {
                    throw HelperError.rejected(
                        "unsupported_recurrence",
                        "recurrence weekday is not supported"
                    )
                }
                return EKRecurrenceDayOfWeek(weekday, weekNumber: day.week)
            }
        let recurrenceEnd: EKRecurrenceEnd?
        if let end = value.end {
            if let endDate = end.endDate {
                guard let date = isoFormatter.date(from: endDate) else {
                    throw HelperError.rejected(
                        "unsupported_recurrence",
                        "recurrence end date is invalid"
                    )
                }
                recurrenceEnd = EKRecurrenceEnd(end: date)
            } else if end.occurrenceCount > 0 {
                recurrenceEnd = EKRecurrenceEnd(occurrenceCount: end.occurrenceCount)
            } else {
                recurrenceEnd = nil
            }
        } else {
            recurrenceEnd = nil
        }
        func boxed(_ numbers: [Int]) -> [NSNumber]? {
            numbers.isEmpty ? nil : numbers.map(NSNumber.init(value:))
        }
        return EKRecurrenceRule(
            recurrenceWith: frequency,
            interval: value.interval,
            daysOfTheWeek: days,
            daysOfTheMonth: boxed(value.daysOfMonth),
            monthsOfTheYear: boxed(value.monthsOfYear),
            weeksOfTheYear: boxed(value.weeksOfYear),
            daysOfTheYear: boxed(value.daysOfYear),
            setPositions: boxed(value.setPositions),
            end: recurrenceEnd
        )
    }
}

private func reminder(_ store: EKEventStore, identifier: String) -> EKReminder? {
    store.calendarItem(withIdentifier: identifier) as? EKReminder
}

private func fetchAll(_ store: EKEventStore) throws -> [EKReminder] {
    let calendars = store.calendars(for: .reminder)
    let predicate = store.predicateForReminders(in: calendars)
    let semaphore = DispatchSemaphore(value: 0)
    var records: [EKReminder] = []
    store.fetchReminders(matching: predicate) { reminders in
        records = reminders ?? []
        semaphore.signal()
    }
    guard semaphore.wait(timeout: .now() + 30) == .success else {
        throw HelperError.rejected("fetch_timeout", "EventKit reminder fetch timed out")
    }
    return records
}

private func canaryCalendar(_ store: EKEventStore) throws -> EKCalendar {
    let matches = store.calendars(for: .reminder).filter { $0.title == canaryList }
    guard matches.count == 1, let calendar = matches.first else {
        throw HelperError.rejected(
            "canary_list_ambiguous",
            "expected exactly one Claude reminder list; found \(matches.count)"
        )
    }
    return calendar
}

private func canaryMatches(
    _ store: EKEventStore,
    marker: String
) throws -> [EKReminder] {
    try validateCanaryMarker(marker)
    return try fetchAll(store).filter {
        $0.calendar.title == canaryList &&
            $0.title == canaryTitle &&
            ($0.notes ?? "").components(separatedBy: .newlines).contains(marker)
    }
}

private func record(_ reminder: EKReminder) throws -> ReminderRecord {
    let identifier = reminder.calendarItemIdentifier
    guard !identifier.isEmpty else {
        throw HelperError.rejected("missing_identifier", "EventKit reminder has no identifier")
    }
    let listId = reminder.calendar.calendarIdentifier
    guard !listId.isEmpty else {
        throw HelperError.rejected("missing_list_identifier", "EventKit reminder list has no identifier")
    }
    guard let modified = reminder.lastModifiedDate else {
        throw HelperError.rejected(
            "missing_revision",
            "EventKit reminder has no lastModifiedDate and cannot be changed safely"
        )
    }
    let due = canonicalDue(reminder.dueDateComponents)
    let recurrence = recurrenceValues(reminder)
    return ReminderRecord(
        identifier: identifier,
        externalIdentifier: reminder.calendarItemExternalIdentifier,
        listId: listId,
        list: reminder.calendar.title,
        title: reminder.title ?? "",
        dueAt: due,
        dueState: due == nil ? "undated" : "dated",
        priority: reminder.priority,
        completed: reminder.isCompleted,
        completionDate: reminder.completionDate.map { isoFormatter.string(from: $0) },
        lastModifiedDate: isoFormatter.string(from: modified),
        recurrenceFingerprint: try recurrenceFingerprint(recurrence),
        recurrence: recurrence
    )
}

private func stableFieldsMatch(_ left: ReminderRecord, _ right: ReminderRecord) -> Bool {
    left.identifier == right.identifier &&
        left.externalIdentifier == right.externalIdentifier &&
        left.listId == right.listId &&
        left.list == right.list &&
        left.title == right.title &&
        left.dueAt == right.dueAt &&
        left.dueState == right.dueState &&
        left.priority == right.priority &&
        left.recurrenceFingerprint == right.recurrenceFingerprint
}

private func stableIdentityFieldsMatch(
    _ left: ReminderRecord,
    _ right: ReminderRecord
) -> Bool {
    left.identifier == right.identifier &&
        left.externalIdentifier == right.externalIdentifier &&
        left.listId == right.listId &&
        left.list == right.list &&
        left.title == right.title &&
        left.dueState == right.dueState &&
        left.priority == right.priority
}

private func recurringAdvanceConfirmed(
    _ before: ReminderRecord,
    _ after: ReminderRecord
) -> Bool {
    guard !before.recurrence.isEmpty,
          stableIdentityFieldsMatch(before, after),
          before.completed == false,
          after.completed == false,
          let beforeDue = before.dueAt.flatMap(isoFormatter.date),
          let afterDue = after.dueAt.flatMap(isoFormatter.date),
          afterDue > beforeDue else {
        return false
    }
    return true
}

private func generatedOccurrenceMatches(
    _ original: ReminderRecord,
    _ generated: ReminderRecord,
    completedAfter: Date,
    completedBefore: Date
) -> Bool {
    guard generated.identifier != original.identifier,
          generated.externalIdentifier != original.externalIdentifier,
          generated.listId == original.listId,
          generated.list == original.list,
          generated.title == original.title,
          generated.dueAt == original.dueAt,
          generated.dueState == original.dueState,
          generated.priority == original.priority,
          generated.completed,
          generated.recurrence.isEmpty,
          generated.recurrenceFingerprint == "none",
          let completionDate = generated.completionDate.flatMap(isoFormatter.date) else {
        return false
    }
    return completionDate >= completedAfter && completionDate <= completedBefore
}

private func stableFieldDifferences(
    _ left: ReminderRecord,
    _ right: ReminderRecord
) -> [String] {
    var fields: [String] = []
    if left.identifier != right.identifier { fields.append("identifier") }
    if left.externalIdentifier != right.externalIdentifier { fields.append("external_identifier") }
    if left.listId != right.listId { fields.append("list_id") }
    if left.list != right.list { fields.append("list") }
    if left.title != right.title { fields.append("title") }
    if left.dueAt != right.dueAt { fields.append("due_at") }
    if left.dueState != right.dueState { fields.append("due_state") }
    if left.priority != right.priority { fields.append("priority") }
    if left.recurrenceFingerprint != right.recurrenceFingerprint {
        fields.append("recurrence_fingerprint")
    }
    if left.completed == right.completed { fields.append("completion_state_unchanged") }
    return fields
}

private func runGet(_ store: EKEventStore) throws {
    let input = try decode(GetInput.self)
    guard !input.identifier.isEmpty, input.identifier.count <= 240 else {
        throw HelperError.rejected("invalid_identifier", "identifier is required")
    }
    guard let item = reminder(store, identifier: input.identifier) else {
        emit(RecordResponse(record: nil))
        return
    }
    emit(RecordResponse(record: try record(item)))
}

private func runList(_ store: EKEventStore) throws {
    let records = try fetchAll(store)
        .map(record)
        .sorted {
            if $0.list != $1.list {
                return $0.list.localizedCaseInsensitiveCompare($1.list) == .orderedAscending
            }
            if $0.title != $1.title {
                return $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending
            }
            return $0.identifier < $1.identifier
        }
    emit(RecordsResponse(records: records))
}

private func runCanaryFind(_ store: EKEventStore) throws {
    let input = try decode(CanaryInput.self)
    let records = try canaryMatches(store, marker: input.marker)
        .map(record)
        .sorted { $0.identifier < $1.identifier }
    emit(RecordsResponse(records: records))
}

private func runCanaryCreate(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(CanaryInput.self)
    try validateCanaryMarker(input.marker)
    guard try canaryMatches(store, marker: input.marker).isEmpty else {
        throw HelperError.rejected("canary_exists", "canary marker already exists")
    }
    let calendar = try canaryCalendar(store)
    let item = EKReminder(eventStore: store)
    item.calendar = calendar
    item.title = canaryTitle
    item.notes = input.marker
    var dueCalendar = Calendar(identifier: .gregorian)
    dueCalendar.timeZone = .current
    let dueDate = dueCalendar.date(byAdding: .day, value: 1, to: Date())!
    var due = dueCalendar.dateComponents(
        [.year, .month, .day],
        from: dueDate
    )
    due.hour = 9
    due.minute = 0
    due.calendar = dueCalendar
    due.timeZone = .current
    item.dueDateComponents = due
    if input.recurring == true {
        item.recurrenceRules = [
            EKRecurrenceRule(
                recurrenceWith: .weekly,
                interval: 1,
                end: EKRecurrenceEnd(occurrenceCount: 2)
            )
        ]
    }
    do {
        try store.save(item, commit: true)
    } catch {
        throw HelperError.rejected("canary_create_failed", "could not save canary")
    }
    let matches = try canaryMatches(store, marker: input.marker)
    guard matches.count == 1, let saved = matches.first else {
        throw HelperError.rejected(
            "canary_create_readback_failed",
            "canary readback did not find exactly one reminder"
        )
    }
    emit(RecordResponse(record: try record(saved)))
}

private func runCanaryDelete(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(CanaryInput.self)
    let matches = try canaryMatches(store, marker: input.marker)
    guard !matches.isEmpty, matches.count <= 3 else {
        throw HelperError.rejected(
            "canary_delete_refused",
            "canary cleanup expected 1-3 exact marker matches"
        )
    }
    do {
        for item in matches {
            try store.remove(item, commit: false)
        }
        try store.commit()
    } catch {
        throw HelperError.rejected("canary_delete_failed", "could not delete exact canary")
    }
    guard try canaryMatches(store, marker: input.marker).isEmpty else {
        throw HelperError.rejected(
            "canary_delete_readback_failed",
            "canary marker remains after cleanup"
        )
    }
    emit(CanaryDeleteResponse(deleted: matches.count))
}

private func runSetCompletion(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(SetCompletionInput.self)
    guard input.identifier == input.expected.identifier else {
        throw HelperError.rejected(
            "identity_mismatch",
            "request identifier does not match the expected reminder"
        )
    }
    guard let item = reminder(store, identifier: input.identifier) else {
        throw HelperError.rejected("not_found", "exact reminder was not found")
    }
    let before = try record(item)
    guard before == input.expected else {
        throw HelperError.rejected(
            "compare_and_swap_failed",
            "reminder identity or source version changed before completion"
        )
    }
    guard before.completed != input.completed else {
        throw HelperError.rejected(
            "state_conflict",
            "reminder already has the requested completion state"
        )
    }

    let identifiersBefore = Set(try fetchAll(store).map(\.calendarItemIdentifier))
    let mutationStarted = Date().addingTimeInterval(-2)
    item.isCompleted = input.completed
    do {
        try store.save(item, commit: true)
    } catch {
        throw HelperError.rejected("save_failed", "EventKit could not save completion")
    }
    guard let saved = reminder(store, identifier: input.identifier) else {
        throw HelperError.rejected(
            "readback_missing",
            "reminder was not readable after completion"
        )
    }
    let after = try record(saved)
    let normalCompletion = stableFieldsMatch(before, after) &&
        after.completed == input.completed
    let recurringCompletion = input.completed &&
        recurringAdvanceConfirmed(before, after)
    guard normalCompletion || recurringCompletion else {
        let fields = stableFieldDifferences(before, after).joined(separator: ",")
        throw HelperError.rejected(
            "readback_mismatch",
            "EventKit readback did not confirm the exact completion; changed fields: \(fields)"
        )
    }
    var generatedOccurrence: ReminderRecord?
    if recurringCompletion {
        let mutationEnded = Date().addingTimeInterval(5)
        let candidates = try fetchAll(store)
            .filter { !identifiersBefore.contains($0.calendarItemIdentifier) }
            .map(record)
            .filter {
                generatedOccurrenceMatches(
                    before,
                    $0,
                    completedAfter: mutationStarted,
                    completedBefore: mutationEnded
                )
            }
        guard candidates.count == 1, let candidate = candidates.first else {
            throw HelperError.rejected(
                "recurring_occurrence_ambiguous",
                "completion advanced the recurring reminder but did not expose exactly one generated occurrence"
            )
        }
        generatedOccurrence = candidate
    }
    emit(
        MutationResponse(
            before: before,
            record: after,
            generatedOccurrence: generatedOccurrence
        )
    )
}

private func runRestoreRecurring(_ store: EKEventStore) throws {
    try requireMutationGate()
    let input = try decode(RestoreRecurringInput.self)
    guard input.identifier == input.expected.identifier,
          input.identifier == input.restore.identifier else {
        throw HelperError.rejected(
            "identity_mismatch",
            "recurring restore identifiers do not match"
        )
    }
    guard !input.restore.recurrence.isEmpty, input.restore.completed == false else {
        throw HelperError.rejected(
            "restore_invalid",
            "recurring restore requires the prior incomplete recurrence"
        )
    }
    guard let item = reminder(store, identifier: input.identifier) else {
        throw HelperError.rejected("not_found", "exact recurring reminder was not found")
    }
    guard let generatedItem = reminder(
        store,
        identifier: input.generatedOccurrence.identifier
    ) else {
        throw HelperError.rejected(
            "generated_occurrence_missing",
            "generated completed occurrence was not found for recurring undo"
        )
    }
    let before = try record(item)
    guard before == input.expected else {
        throw HelperError.rejected(
            "compare_and_swap_failed",
            "recurring reminder changed before undo"
        )
    }
    guard stableIdentityFieldsMatch(before, input.restore) else {
        throw HelperError.rejected(
            "restore_identity_mismatch",
            "recurring restore would change identity or display fields"
        )
    }
    let generatedBefore = try record(generatedItem)
    guard generatedBefore == input.generatedOccurrence,
          generatedOccurrenceMatches(
              input.restore,
              generatedBefore,
              completedAfter: .distantPast,
              completedBefore: .distantFuture
          ) else {
        throw HelperError.rejected(
            "generated_occurrence_conflict",
            "generated completed occurrence changed before recurring undo"
        )
    }
    item.isCompleted = false
    item.dueDateComponents = try dueComponents(input.restore.dueAt)
    item.recurrenceRules = try recurrenceRules(input.restore.recurrence)
    do {
        try store.save(item, commit: false)
        try store.remove(generatedItem, commit: false)
        try store.commit()
    } catch {
        throw HelperError.rejected(
            "restore_save_failed",
            "EventKit could not restore the recurring reminder"
        )
    }
    guard let saved = reminder(store, identifier: input.identifier) else {
        throw HelperError.rejected(
            "restore_readback_missing",
            "recurring reminder was not readable after undo"
        )
    }
    let after = try record(saved)
    var expected = input.restore
    expected = ReminderRecord(
        identifier: expected.identifier,
        externalIdentifier: expected.externalIdentifier,
        listId: expected.listId,
        list: expected.list,
        title: expected.title,
        dueAt: expected.dueAt,
        dueState: expected.dueState,
        priority: expected.priority,
        completed: expected.completed,
        completionDate: after.completionDate,
        lastModifiedDate: after.lastModifiedDate,
        recurrenceFingerprint: expected.recurrenceFingerprint,
        recurrence: expected.recurrence
    )
    guard after == expected else {
        throw HelperError.rejected(
            "restore_readback_mismatch",
            "EventKit readback did not confirm recurring undo"
        )
    }
    guard reminder(
        store,
        identifier: input.generatedOccurrence.identifier
    ) == nil else {
        throw HelperError.rejected(
            "generated_occurrence_remains",
            "generated completed occurrence remains after recurring undo"
        )
    }
    emit(
        MutationResponse(
            before: before,
            record: after,
            generatedOccurrence: nil
        )
    )
}

private func main() {
    do {
        guard CommandLine.arguments.count == 2 else {
            throw HelperError.rejected("usage", "usage: reminder_complete_eventkit.swift OPERATION")
        }
        try requireExistingAccess()
        let store = EKEventStore()
        switch CommandLine.arguments[1] {
        case "list":
            try runList(store)
        case "canary-find":
            try runCanaryFind(store)
        case "canary-create":
            try runCanaryCreate(store)
        case "canary-delete":
            try runCanaryDelete(store)
        case "get":
            try runGet(store)
        case "set-completed":
            try runSetCompletion(store)
        case "restore-recurring":
            try runRestoreRecurring(store)
        default:
            throw HelperError.rejected("unsupported_operation", "unsupported EventKit operation")
        }
    } catch let error as HelperError {
        fail(error)
    } catch {
        fail(.rejected("unexpected_error", String(describing: error)))
    }
}

main()
