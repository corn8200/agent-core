# cockpit-imessage-sender LaunchAgent receipt

- Label: `com.john.cockpit-imessage-sender`
- Source plist: `cockpit/com.john.cockpit-imessage-sender.plist`
- Intended install path: `/Users/johncornelius/Library/LaunchAgents/com.john.cockpit-imessage-sender.plist`
- Live load status: not loaded by this change.
- Reason: live load was not safe during validation because the production queue was not empty. `/srv/data/cp/cockpit.db` contained a stale claimed row (`id=1`, `thread_id=+15555550000`, body prefix `w54 smoke test`) that the sender would reclaim and send.
