// contacts_fetch.swift — fast Contacts framework reader.
// Usage: swift contacts_fetch.swift
// Output: one contact per line, fields separated by \x1f, records terminated by \x1e.
// Fields: id, name, org, phones, emails, note, updated
//   - id:       CNContact.identifier (stable UUID)
//   - name:     full name ("First Last")
//   - org:      "Organization — Title" (either may be empty)
//   - phones:   comma-joined, digits+E.164 preserved
//   - emails:   comma-joined, lowercased
//   - note:     contact note (empty unless entitlement granted)
//   - updated:  empty (Contacts framework does not expose modification date)
//
// Why Swift/Contacts: CNContactStore is the modern replacement for the
// deprecated AddressBook framework. AppleScript `tell application "Contacts"`
// works but is slow and flaky on macOS 26.4.1. This talks directly to the
// contacts store.
//
// Notes field (CNContactNoteKey): requires the `com.apple.developer.contacts.notes`
// entitlement on macOS 14+. A freshly-compiled `swift` run does NOT have this
// entitlement, so the note field will be empty. The `id` stays stable across runs.
import Foundation
import Contacts

let store = CNContactStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestAccess(for: .contacts) { ok, _ in granted = ok; sem.signal() }
sem.wait()
if !granted {
    FileHandle.standardError.write("no contacts access\n".data(using: .utf8)!)
    exit(2)
}

let keys: [CNKeyDescriptor] = [
    CNContactIdentifierKey as CNKeyDescriptor,
    CNContactGivenNameKey as CNKeyDescriptor,
    CNContactFamilyNameKey as CNKeyDescriptor,
    CNContactMiddleNameKey as CNKeyDescriptor,
    CNContactOrganizationNameKey as CNKeyDescriptor,
    CNContactJobTitleKey as CNKeyDescriptor,
    CNContactPhoneNumbersKey as CNKeyDescriptor,
    CNContactEmailAddressesKey as CNKeyDescriptor,
    CNContactNoteKey as CNKeyDescriptor,
]

func clean(_ s: String?) -> String {
    guard let s = s else { return "" }
    return s.replacingOccurrences(of: "\u{1f}", with: " ")
            .replacingOccurrences(of: "\u{1e}", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespaces)
}

let request = CNContactFetchRequest(keysToFetch: keys)
var lines: [String] = []
let fs = "\u{1f}"

do {
    try store.enumerateContacts(with: request) { contact, _ in
        let id = clean(contact.identifier)
        let parts = [contact.givenName, contact.middleName, contact.familyName]
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let name = clean(parts.joined(separator: " "))
        let orgName = contact.organizationName.trimmingCharacters(in: .whitespaces)
        let jobTitle = contact.jobTitle.trimmingCharacters(in: .whitespaces)
        var orgStr = ""
        if !orgName.isEmpty && !jobTitle.isEmpty {
            orgStr = "\(orgName) — \(jobTitle)"
        } else if !orgName.isEmpty {
            orgStr = orgName
        } else if !jobTitle.isEmpty {
            orgStr = jobTitle
        }
        let org = clean(orgStr)
        let phones = contact.phoneNumbers
            .map { clean($0.value.stringValue) }
            .filter { !$0.isEmpty }
            .joined(separator: ",")
        let emails = contact.emailAddresses
            .map { clean(($0.value as String).lowercased()) }
            .filter { !$0.isEmpty }
            .joined(separator: ",")
        var note = ""
        if contact.isKeyAvailable(CNContactNoteKey) {
            note = clean(contact.note)
        }
        let updated = ""
        lines.append("\(id)\(fs)\(name)\(fs)\(org)\(fs)\(phones)\(fs)\(emails)\(fs)\(note)\(fs)\(updated)\u{1e}")
    }
} catch {
    FileHandle.standardError.write("enumerate failed: \(error)\n".data(using: .utf8)!)
    exit(3)
}

print(lines.joined(separator: "\n"))
