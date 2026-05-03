# cockpit-imessage-sender LaunchAgent receipt

- Label: `com.john.cockpit-imessage-sender`
- Source plist: `cockpit/com.john.cockpit-imessage-sender.plist`
- Intended install path: `/Users/johncornelius/Library/LaunchAgents/com.john.cockpit-imessage-sender.plist`
- Live load status: installed and loaded on 2026-05-03.
- Install path: `/Users/johncornelius/Library/LaunchAgents/com.john.cockpit-imessage-sender.plist`
- Preload safety action: stale smoke-test row `id=1` in `/srv/data/cp/cockpit.db` was marked `failed` with no live send.
- Dry run before load: `claimed=0 sent=0 failed=0`.
- LaunchAgent verification: `launchctl print gui/501/com.john.cockpit-imessage-sender` reported `last exit code = 0`; log showed `claimed=0 sent=0 failed=0`.
