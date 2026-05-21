// reminders_fetch.swift — fast EventKit-based incomplete-reminders reader.
// Usage: swift reminders_fetch.swift
// Output: one reminder per line, fields separated by \x1f, records terminated by \x1e.
// Fields: title, list_name, due_iso (empty if no due date), id, notes, priority
//
// Why Swift/EventKit: Reminders.app crashes on AppleScript `every reminder whose
// completed is false` queries under macOS 26.4.1 (Swift runtime assertion during
// NSScriptCommand evaluation of property accessors, see crash
// Reminders-2026-04-14-203044.ips). EventKit talks directly to the reminders
// store — no AppleEvents, no Reminders.app launch, no crash path.
import Foundation
import EventKit

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
if #available(macOS 14.0, *) {
    store.requestFullAccessToReminders { ok, _ in granted = ok; sem.signal() }
} else {
    store.requestAccess(to: .reminder) { ok, _ in granted = ok; sem.signal() }
}
sem.wait()
if !granted {
    FileHandle.standardError.write("no reminders access\n".data(using: .utf8)!)
    exit(2)
}

let calendars = store.calendars(for: .reminder)
let pred = store.predicateForIncompleteReminders(
    withDueDateStarting: nil, ending: nil, calendars: calendars
)

var reminders: [EKReminder] = []
let fetchSem = DispatchSemaphore(value: 0)
store.fetchReminders(matching: pred) { result in
    reminders = result ?? []
    fetchSem.signal()
}
fetchSem.wait()

let fmt = ISO8601DateFormatter()
fmt.formatOptions = [.withInternetDateTime]
let cal = Calendar.current

func clean(_ s: String?) -> String {
    guard let s = s else { return "" }
    return s.replacingOccurrences(of: "\u{1f}", with: " ")
            .replacingOccurrences(of: "\u{1e}", with: " ")
}

var lines: [String] = []
for r in reminders {
    let title = clean(r.title)
    if title.isEmpty { continue }
    let listName = clean(r.calendar?.title)
    let itemId = clean(r.calendarItemIdentifier)
    let notes = clean(r.notes)
    let priority = String(r.priority)
    var dueIso = ""
    if let comps = r.dueDateComponents, let date = cal.date(from: comps) {
        dueIso = fmt.string(from: date)
    }
    let fs = "\u{1f}"
    lines.append("\(title)\(fs)\(listName)\(fs)\(dueIso)\(fs)\(itemId)\(fs)\(notes)\(fs)\(priority)\u{1e}")
}
print(lines.joined(separator: "\n"))
