// calendar_fetch.swift — fast EventKit-based calendar reader.
// Usage: swift calendar_fetch.swift <days_back> <days_fwd>
// Output: one event per line, fields separated by \x1f, records terminated by \x1e.
// Fields: title, start_iso, end_iso, calendar, location, notes, uid, all_day(0/1)
// Unlike AppleScript, this EXPANDS recurring event instances and runs in ~1s.
import Foundation
import EventKit

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { ok, _ in granted = ok; sem.signal() }
} else {
    store.requestAccess(to: .event) { ok, _ in granted = ok; sem.signal() }
}
sem.wait()
if !granted {
    FileHandle.standardError.write("no calendar access\n".data(using: .utf8)!)
    exit(2)
}

let args = CommandLine.arguments
let daysBack = Int(args.count > 1 ? args[1] : "0") ?? 0
let daysFwd = Int(args.count > 2 ? args[2] : "8") ?? 8

let cal = Calendar.current
let startBase = cal.startOfDay(for: Date())
let start = cal.date(byAdding: .day, value: -daysBack, to: startBase)!
let end = cal.date(byAdding: .day, value: daysFwd, to: startBase)!

let calendars = store.calendars(for: .event)
let skip: Set<String> = ["Siri Suggestions", "US Holidays", "Birthdays"]
let filtered = calendars.filter { !skip.contains($0.title) }

let pred = store.predicateForEvents(withStart: start, end: end, calendars: filtered)
let events = store.events(matching: pred)

let fmt = ISO8601DateFormatter()
fmt.formatOptions = [.withInternetDateTime]

func clean(_ s: String?) -> String {
    guard let s = s else { return "" }
    return s.replacingOccurrences(of: "\u{1f}", with: " ")
            .replacingOccurrences(of: "\u{1e}", with: " ")
}

var lines: [String] = []
for e in events {
    let title = clean(e.title)
    let loc = clean(e.location)
    let notes = clean(e.notes)
    let calName = clean(e.calendar.title)
    let uid = clean(e.eventIdentifier)
    let allDay = e.isAllDay ? "1" : "0"
    let s = fmt.string(from: e.startDate)
    let en = fmt.string(from: e.endDate)
    let fs = "\u{1f}"
    lines.append("\(title)\(fs)\(s)\(fs)\(en)\(fs)\(calName)\(fs)\(loc)\(fs)\(notes)\(fs)\(uid)\(fs)\(allDay)\u{1e}")
}
print(lines.joined(separator: "\n"))
